#!/usr/bin/env python3
"""Scan TuneIn favorites for scrobbling without changing the live queue."""

import argparse
import configparser
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from runpy import run_path
from urllib.parse import urlsplit
from urllib.request import urlopen

RADIO = run_path(str(Path(__file__).with_name("radio-scrobble.py")))
STATION_ID = re.compile(r"s[0-9]+")
SETTING = "tunein_artist_title_stations"


def get_json(url: str):
    """Read JSON from the configured local TuneIn bridge."""
    with urlopen(url, timeout=12) as response:
        return json.loads(response.read(1_000_000))


def mpd_command(sock: Path, command: str) -> dict[str, str]:
    """Send a command to the private scanner MPD socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(4)
        connection.connect(str(sock))
        with connection.makefile("r", encoding="utf-8") as reader:
            if not reader.readline().startswith("OK MPD "):
                raise OSError("Unexpected MPD greeting")
            connection.sendall((command + "\n").encode())
            fields = {}
            for line in reader:
                if line == "OK\n":
                    return fields
                if line.startswith("ACK"):
                    raise OSError("Private MPD command failed")
                key, separator, value = line.rstrip("\n").partition(": ")
                if separator:
                    fields[key] = value
    raise OSError("Private MPD disconnected")


def observe(sock: Path, name: str, duration: int) -> dict:
    """Count complete consecutive music titles, ignoring the first partial song."""
    deadline = time.monotonic() + duration
    previous = None
    started = 0.0
    observed_start = False
    complete = 0
    structured = False
    played = False
    while time.monotonic() < deadline:
        status = mpd_command(sock, "status")
        now = time.monotonic()
        if status.get("state") == "play" and float(status.get("elapsed", 0)) > 0:
            played = True
            raw = mpd_command(sock, "currentsong").get("Title", "")
            song = RADIO["iheart_song"](raw)
            if song:
                structured = True
            plain = RADIO["tunein_song"](raw, name)
            identity = ((RADIO["normalize"](plain.artist),
                         RADIO["normalize"](plain.title)) if plain else None)
            if identity != previous:
                if previous and identity and observed_start and now - started > 33:
                    complete += 1
                elif not identity or observed_start:
                    complete = 0
                observed_start = previous is not None
                started = now
                previous = identity
            if complete >= 2:
                break
        elif previous:
            previous = None
            observed_start = False
            complete = 0
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    if complete >= 2:
        result = "plain-title-candidate"
    elif structured:
        result = "structured-metadata"
    elif played:
        result = "inconclusive"
    else:
        result = "unplayable"
    return {"result": result, "complete_plain_tracks": complete}


def probe(bridge: str, station: dict, duration: int) -> dict:
    """Start and clean up one private silent MPD instance."""
    entry = {"id": station["id"], "name": station["name"]}
    try:
        stream = get_json(f"{bridge}/stations/{station['id']}")["url"]
        if urlsplit(stream).scheme not in {"http", "https"}:
            raise ValueError("Unsupported stream URL")
        with tempfile.TemporaryDirectory(prefix="tunein-scan-") as directory:
            root = Path(directory)
            sock = root / "mpd.sock"
            (root / "music").mkdir()
            config = root / "mpd.conf"
            config.write_text(
                f'music_directory "{root}/music"\ndb_file "{root}/db"\n'
                f'bind_to_address "{sock}"\n'
                'audio_output {\n    type "null"\n    name "TuneIn scanner"\n}\n',
                encoding="utf-8",
            )
            process = subprocess.Popen(
                ["mpd", "--no-daemon", str(config)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while not sock.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise OSError("Private MPD did not start")
                    time.sleep(0.1)
                mpd_command(sock, "add " + json.dumps(stream, ensure_ascii=False))
                mpd_command(sock, "play")
                return {**entry, **observe(sock, station["name"], duration)}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    except (OSError, ValueError, KeyError) as error:
        return {**entry, "result": "unavailable", "error": type(error).__name__}


def replace_config(path: Path, selected: list[str]) -> None:
    """Merge candidate IDs into the existing INI file without rewriting secrets."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular existing Last.fm config file")
    with path.open(encoding="utf-8", newline="") as source:
        original = source.read()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(original)
    current = parser.get("lastfm", SETTING, fallback="")
    ids = list(dict.fromkeys(
        [part.strip() for part in current.split(",") if part.strip()] + selected
    ))
    heading = re.search(r"(?m)^\[lastfm\][ \t]*(?=\r?$)", original)
    if not heading:
        raise ValueError("Config must contain [lastfm]")
    newline = "\r\n" if "\r\n" in original else "\n"
    start = heading.end()
    following = re.search(r"(?m)^\[", original[start:])
    stop = start + following.start() if following else len(original)
    section = original[start:stop]
    line = f"{SETTING} = {','.join(ids)}"
    section, count = re.subn(
        rf"(?im)^[ \t]*{SETTING}[ \t]*=[^\r\n]*", lambda _: line, section
    )
    if not count:
        section = newline + newline + (
            "# TuneIn stations that supply plain Artist - Song metadata." + newline + line
        ) + section
    updated = original[:start] + section + original[stop:]
    if updated == original:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", newline="", delete=False
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
            output.write(updated)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_report(path: Path, report: dict, *, initial: bool = False) -> None:
    """Create a report exclusively, then update it atomically as stations finish."""
    data = json.dumps(report, indent=2) + "\n"
    if initial:
        with path.open("x", encoding="utf-8") as output:
            output.write(data)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, encoding="utf-8", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def scan(args) -> None:
    """Scan configured favorites and write a reviewable report."""
    if not shutil.which("mpd"):
        raise ValueError("The mpd executable is required")
    if args.seconds < 5 or not 1 <= args.jobs <= 6:
        raise ValueError("Use at least 5 seconds and between 1 and 6 workers")
    bridge = args.bridge.rstrip("/")
    print("Fetching TuneIn favorites…", flush=True)
    stations = [
        station for station in get_json(bridge + "/stations")
        if STATION_ID.fullmatch(station["id"])
        and (not args.station or station["id"] in args.station)
    ]
    if args.station and set(args.station) != {station["id"] for station in stations}:
        raise ValueError("Requested station ID is missing from TuneIn favorites")
    report = {"status": "running", "total": len(stations), "stations": []}
    write_report(args.report, report, initial=True)
    print(f"Scanning {len(stations)} stations with up to {args.jobs} workers, "
          f"up to {args.seconds} seconds per station.", flush=True)
    print(f"JSON report: {args.report.resolve()}", flush=True)

    def scan_station(station):
        print(f"Starting {station['id']}  {station['name']}", flush=True)
        return probe(bridge, station, args.seconds)

    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(scan_station, station) for station in stations]
            for future in as_completed(futures):
                result = future.result()
                report["stations"].append(result)
                count = len(report["stations"])
                print(f"[{count}/{len(stations)}] {result['id']}  "
                      f"{result['result']}  {result['name']}", flush=True)
                write_report(args.report, report)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        write_report(args.report, report)
        raise
    report["status"] = "complete"
    write_report(args.report, report)
    print("Scan complete.", flush=True)


def save(args) -> None:
    """Add only explicitly selected positive results to the Last.fm config."""
    report = json.loads(args.report.read_text(encoding="utf-8"))
    candidates = {row["id"] for row in report["stations"]
                  if row["result"] == "plain-title-candidate"
                  and row.get("complete_plain_tracks", 0) >= 2}
    if any(not STATION_ID.fullmatch(item) or item not in candidates
           for item in args.station):
        raise ValueError("Only candidate station IDs from the report can be saved")
    replace_config(args.config, args.station)
    print("Saved Last.fm opt-ins. Recreate the Docker scrobbler to reload its config.")


def main() -> None:
    """Run the selected scan or save operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation")
    scanner = commands.add_parser("scan", help="Scan TuneIn favorites")
    scanner.add_argument("-b", "--bridge", default="http://127.0.0.1:18766")
    scanner.add_argument("-s", "--station", action="append", help="Favorite station ID")
    scanner.add_argument("-t", "--seconds", type=int, default=720)
    scanner.add_argument("-j", "--jobs", type=int, default=4)
    scanner.add_argument("-r", "--report", type=Path, required=True)
    saver = commands.add_parser("save", help="Add selected scan candidates to Last.fm")
    saver.add_argument("-r", "--report", type=Path, required=True)
    saver.add_argument("-c", "--config", type=Path, required=True)
    saver.add_argument("-s", "--station", action="append", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "scan":
            if args.report.exists() or args.report.is_symlink():
                raise ValueError("Report already exists. Choose a new path.")
            scan(args)
        elif args.operation == "save":
            save(args)
        else:
            parser.print_help()
    except (OSError, ValueError, KeyError, TypeError, configparser.Error) as error:
        parser.exit(1, f"TuneIn scan failed: {error}\n")


if __name__ == "__main__":
    main()
