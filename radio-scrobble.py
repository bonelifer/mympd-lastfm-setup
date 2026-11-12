#!/usr/bin/env python3
"""Scrobble recognized radio songs from MPD without touching its queue."""

import argparse
import configparser
import hashlib
import json
import logging
import re
import socket
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

INTERVAL = 3
LASTFM = "https://ws.audioscrobbler.com/2.0/"
BRIDGES = {"pandora": 18765, "somafm": 18768}


@dataclass(frozen=True)
class Song:
    """Metadata and optional duration from a recognized music segment."""

    artist: str
    title: str
    album: str = ""
    duration: float = 0
    provider_id: str = ""
    complete_track_only: bool = False


@dataclass
class Listening:
    """Accumulate only verified time on the same song and queue item."""

    source: tuple
    song: Song
    started: int
    observed_boundary: bool
    listened: float = 0
    submitted: bool = False

    def eligible(self, finished: bool = False) -> bool:
        """Apply Last.fm timing rules using a known or observed duration."""
        if self.song.complete_track_only:
            return finished and self.observed_boundary and self.listened > 30
        duration = self.song.duration
        if duration:
            return duration > 30 and self.listened >= min(duration / 2, 240)
        return self.listened >= 240 or (
            finished and self.observed_boundary and self.listened > 30
        )


def mpd_snapshot(host: str, port: int) -> tuple[dict, dict]:
    """Read MPD status and tags without issuing any playback commands."""
    with socket.create_connection((host, port), timeout=5) as connection:
        with connection.makefile("r", encoding="utf-8") as reader:
            if not reader.readline().startswith("OK MPD "):
                raise OSError("Unexpected MPD greeting")
            connection.sendall(
                b"command_list_ok_begin\nstatus\ncurrentsong\ncommand_list_end\n"
            )
            result = [{}]
            for line in reader:
                line = line.rstrip("\n")
                if line == "list_OK":
                    result.append({})
                elif line == "OK" and len(result) == 3:
                    return result[0], result[1]
                elif line.startswith("ACK"):
                    break
                elif ": " in line:
                    key, value = line.split(": ", 1)
                    result[-1].setdefault(key, value)
    raise OSError("MPD status request failed")


def json_request(url: str, payload: bytes | None = None):
    """Read a bounded JSON response from a bridge or Last.fm."""
    with urlopen(Request(url, payload), timeout=8) as response:
        return json.loads(response.read(1_000_000))


def normalize(value: str) -> str:
    """Normalize common stream separators for exact metadata matching."""
    return " ".join(re.sub(r"\s+[-–—]\s+", " - ", value).split()).casefold()


def seconds(value: str) -> float:
    """Parse the feed's hour, minute, second duration."""
    if not re.fullmatch(r"\d{1,2}:[0-5]\d:[0-5]\d(?:\.\d+)?", value):
        return 0
    hours, minutes, rest = map(float, value.split(":"))
    return hours * 3600 + minutes * 60 + rest


def iheart_song(raw: str) -> Song | None:
    """Accept structured iHeart song entries and skip shows and ads."""
    fields = {
        key.casefold(): value.strip()
        for key, value in re.findall(r'(\w+)="([^"]*)"', raw)
    }
    artist = fields.get("artist", "")
    title = fields.get("text") or fields.get("title", "")
    spot = fields.get("song_spot", "").upper()
    if (not artist or artist.casefold() == "null" or not title
            or title.casefold().startswith("spot block ")
            or spot not in ("", "M", "F")
            or {"adcontext", "adswizzcontext"} & fields.keys()):
        return None
    return Song(artist, title, fields.get("album", ""),
                seconds(fields.get("length", "")))


def tunein_song(raw: str, station_name: str) -> Song | None:
    """Parse plain titles only for a station explicitly opted in by the owner."""
    if re.search(r'(?:^|\s)[\w-]+\s*=\s*"', raw):
        return None
    parts = re.split(r"\s+[-–—]\s+", raw.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    artist, title = (part.strip() for part in parts)
    nonmusic = {"ad", "ad content", "advertisement", "commercial", "commercial break",
                "channel content", "unknown", "null", "station id", "news"}
    excluded = nonmusic | {normalize(station_name)}
    if not artist or not title or any(normalize(part) in excluded for part in parts):
        return None
    return Song(artist, title, complete_track_only=True)


def identify(uri: str, raw: str, cache: dict,
             tunein_stations: frozenset[str] = frozenset(),
             station_name: str = "") -> Song | None:
    """Resolve a song only when provider metadata corroborates it."""
    parsed = urlsplit(uri)
    flags = parse_qs(parsed.fragment)
    if "mympd-iheart" in flags or "mympd-tunein" in flags:
        structured = iheart_song(raw)
        station = flags.get("mympd-tunein", [""])[0]
        if structured is None and station in tunein_stations:
            return tunein_song(raw, flags.get("name", [station_name])[0])
        return structured
    if parsed.hostname == "127.0.0.1" and parsed.port == BRIDGES["pandora"]:
        station = re.fullmatch(r"/stream/([\w-]+)", parsed.path)
        if station:
            try:
                track = json_request(
                    f"http://127.0.0.1:{BRIDGES['pandora']}/controls/{station[1]}"
                )
            except HTTPError as error:
                if error.code == 409:
                    return None
                raise
            if track.get("songActions") and track.get("title") == raw:
                duration = track.get("duration")
                if type(duration) not in (int, float) or not 0 < duration <= 86400:
                    duration = 0
                return Song(track.get("artist") or "", track.get("song") or "",
                            track.get("album") or "", duration, track["id"])
    if parsed.hostname and parsed.hostname.endswith(".somafm.com"):
        station = re.fullmatch(
            r"/([a-z0-9-]+?)(?:-\d+(?:-(?:mp3|aacp?|flac))?)?", parsed.path, re.I
        )
        if station:
            key = (station[1], raw)
            if key in cache and time.monotonic() - cache[key][0] < 15:
                return cache[key][1]
            feed = json_request(f"https://api.somafm.com/songs/{station[1]}.json")
            match = None
            for index, row in enumerate(feed["songs"]):
                if normalize(f'{row["artist"]} - {row["title"]}') != normalize(raw):
                    continue
                duration = 0
                if index:
                    gap = int(feed["songs"][index - 1]["date"]) - int(row["date"])
                    duration = gap if 0 < gap <= 3600 else 0
                match = Song(row["artist"], row["title"], row.get("album") or "",
                             duration, f"{station[1]}:{row['date']}")
                break
            cache.clear()
            cache[key] = (time.monotonic(), match)
            return match
    return None


def lastfm_request(song: Song, settings: dict, method: str, **extra) -> dict:
    """Send a signed Last.fm request without exposing the session credentials."""
    fields = {
        "method": method, "api_key": settings["api_key"],
        "sk": settings["session_key"], "artist": song.artist,
        "track": song.title, **extra,
    }
    if song.album:
        fields["album"] = song.album
    signature = "".join(key + value for key, value in sorted(fields.items()))
    fields["api_sig"] = hashlib.md5(
        (signature + settings["secret"]).encode()
    ).hexdigest()
    fields["format"] = "json"
    return json_request(LASTFM, urlencode(fields).encode())


def now_playing(song: Song, settings: dict) -> None:
    """Report the current song once, without writing listening history."""
    try:
        result = lastfm_request(song, settings, "track.updateNowPlaying")
        status = result.get("nowplaying", {}).get("ignoredMessage", {})
        if str(status.get("code")) == "0":
            logging.info("Last.fm accepted Now Playing")
        else:
            logging.warning("Last.fm did not accept Now Playing")
    except (OSError, ValueError):
        logging.warning("Last.fm Now Playing response unavailable")


def is_ibroadcast(uri: str) -> bool:
    """Recognize tracks queued by the local iBroadcast bridge."""
    url = urlsplit(uri)
    return (url.scheme == "http" and url.hostname == "127.0.0.1"
            and url.port == 18769 and bool(re.fullmatch(r"/stream/\d+", url.path)))


def submit(entry: Listening, settings: dict, db: sqlite3.Connection) -> None:
    """Record the attempt before sending so an uncertain reply cannot duplicate it."""
    song = entry.song
    identity = song.provider_id or normalize(song.artist + " - " + song.title)
    source = entry.source[0]
    now = int(time.time())
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM attempts WHERE source=? AND identity=? AND sent_at>?",
            (source, identity, now - 900),
        ).fetchone():
            return
        db.execute("INSERT INTO attempts VALUES (?, ?, ?)", (source, identity, now))
        db.execute("DELETE FROM attempts WHERE sent_at<?", (now - 86400,))
    try:
        result = lastfm_request(song, settings, "track.scrobble",
                                timestamp=str(entry.started), chosenByUser="0")
        status = result.get("scrobbles", {}).get("@attr", {})
        if str(status.get("accepted")) == "1":
            logging.info("Last.fm accepted a radio scrobble")
        else:
            logging.warning("Last.fm did not accept the radio scrobble")
    except (OSError, ValueError):
        logging.warning("Last.fm response unavailable. No automatic resubmission.")


def run(args, settings: dict, db: sqlite3.Connection) -> None:
    """Follow MPD playback and submit each eligible radio song once."""
    previous = None
    last_announced = None
    last_sample = time.monotonic()
    cache = {}
    genres = {name.strip().casefold() for name in
              settings.get("scrobble_genre_blacklist", "").split(",") if name.strip()}
    tunein_stations = frozenset(
        item.strip() for item in settings.get("tunein_artist_title_stations", "").split(",")
        if re.fullmatch(r"s[0-9]+", item.strip())
    )
    was_playing = False
    while True:
        try:
            status, tags = mpd_snapshot(args.host, args.port)
            now = time.monotonic()
            interval = now - last_sample
            last_sample = now
            source = (tags.get("file", ""), status.get("songid"))
            raw = tags.get("Title", "")
            playing = (status.get("state") == "play" and bool(raw)
                       and tags.get("Genre", "").casefold() not in genres)
            if not playing:
                if status.get("state") == "stop":
                    last_announced = None
                if previous:
                    if status.get("state") == "pause" and previous.source == source:
                        previous.observed_boundary = False
                    else:
                        previous = None
                was_playing = False
                time.sleep(INTERVAL)
                continue
            continuous = was_playing and interval <= INTERVAL * 2
            was_playing = True
            if previous and previous.source != source:
                previous = None
            native_scrobble = is_ibroadcast(source[0])
            song = (Song(tags.get("Artist", ""), raw, tags.get("Album", ""))
                    if native_scrobble else identify(source[0], raw, cache,
                                                       tunein_stations, tags.get("Name", "")))
            if song and song.artist and song.title:
                announcement = (source, normalize(song.artist), normalize(song.title),
                                song.provider_id)
                if announcement != last_announced:
                    last_announced = announcement
                    if args.dry_run:
                        logging.info("Eligible for Last.fm Now Playing")
                    else:
                        now_playing(song, settings)
            elif last_announced and last_announced[0] != source:
                last_announced = None
            if native_scrobble or not song or not song.artist or not song.title:
                previous = None
            else:
                same = previous and (
                    normalize(previous.song.artist + " - " + previous.song.title)
                    == normalize(song.artist + " - " + song.title)
                    and previous.song.provider_id == song.provider_id
                )
                if same:
                    if continuous:
                        previous.listened += interval
                    else:
                        previous.observed_boundary = False
                    previous.song = song
                else:
                    boundary = bool(previous and continuous)
                    if boundary and not previous.submitted and previous.eligible(True):
                        if not args.dry_run:
                            submit(previous, settings, db)
                        else:
                            logging.info("Eligible radio song at track boundary")
                    previous = Listening(source, song, int(time.time()), boundary)
                if not previous.submitted and previous.eligible():
                    previous.submitted = True
                    if args.dry_run:
                        logging.info("Eligible radio song")
                    else:
                        submit(previous, settings, db)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            previous = None
            was_playing = False
            logging.warning("Radio sample unavailable (%s)", type(error).__name__)
        time.sleep(INTERVAL)


def main() -> None:
    """Load credentials and run the radio scrobbler until stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", "-c", type=Path, required=True)
    parser.add_argument("--state", "-s", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)
    settings = dict(config["lastfm"]) if config.has_section("lastfm") else {}
    if not args.dry_run and not all(settings.get(k) for k in (
        "api_key", "session_key", "secret"
    )):
        parser.error("The Last.fm config needs an API key, secret, and session key")
    with sqlite3.connect(":memory:" if args.dry_run else args.state) as db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts "
                   "(source TEXT, identity TEXT, sent_at INTEGER)")
        try:
            run(args, settings, db)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
