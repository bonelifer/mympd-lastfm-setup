#!/usr/bin/env python3
"""Set up Last.fm scrobbling in myMPD from a saved config, or save it.

myMPD scrobbles through the "lastfm" script from jcorporation/mympd-scripts.
That script needs four script variables (lastfm_api_key, lastfm_secret,
lastfm_session_key and the optional scrobble_genre_blacklist) and three
triggers (mpd_player, mympd_scrobble and mympd_feedback). This tool keeps
those values in setup-lastfm.cfg so a fresh myMPD can be set up in one step.

Modes:
  --setup   write the variables from the config into myMPD and add any
            missing triggers. Safe to run again: existing triggers are kept.
            With no saved session key, it then runs --login when started
            from a terminal.
  --login   get a session key: uses the Last.fm username and password from
            the config, asking for any not stored there, runs the lastfm
            script's "fetchkey" step in myMPD with them, waits for the new
            key and saves it into the config.
  --export  read the variables from a working myMPD and save them into the
            config (created from setup-lastfm.cfg.example, mode 600).
  --genres  show the genres that are not scrobbled.
  --add-genre / --remove-genre
            change that genre blacklist in myMPD and in the config, so a
            later --setup keeps it.

The lastfm script itself must be imported from the myMPD UI (Scripts ->
Import), which checks its signature; --setup stops and says so if it is
missing.

Inputs:  command-line options (run with no arguments for help) and the INI
         config file.
Outputs: status lines and prompts on stdout; errors on stderr.
Exit codes: 0 success, 1 myMPD error or lastfm script missing, 2 usage or
config error.
"""

import argparse
import configparser
import getpass
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "setup-lastfm.cfg"
EXAMPLE_CONFIG = SCRIPT_DIR / "setup-lastfm.cfg.example"
TIMEOUT_SECONDS = 15
SCRIPT_NAME = "lastfm"
# Config key in [lastfm] -> myMPD script variable
VARIABLES = {
    "api_key": "lastfm_api_key",
    "secret": "lastfm_secret",
    "session_key": "lastfm_session_key",
    "scrobble_genre_blacklist": "scrobble_genre_blacklist",
}
SECRET_KEYS = ("api_key", "secret", "session_key")
# Optional [lastfm] sign-in settings; never sent to myMPD as variables
LOGIN_KEYS = ("username", "password")
# Trigger name, myMPD event number, lastfm script "trigger" argument
TRIGGERS = (
    ("lastfm player", 8, "player"),        # mpd_player
    ("lastfm scrobble", -1, "scrobble"),   # mympd_scrobble
    ("lastfm feedback", -6, "feedback"),   # mympd_feedback
)
# lastfm script version 7 and older refuses to run, even for its own "key"
# step, while the session key is empty, so an empty key is replaced by this
# placeholder. Version 8 fixed that (mympd-scripts#48); it is harmless there.
SESSION_KEY_PLACEHOLDER = "x"
# myMPD runs scripts in the background; how long to wait for the new key
LOGIN_WAIT_SECONDS = 30
LOGIN_POLL_SECONDS = 1.0


class MympdError(Exception):
    """myMPD could not be reached or returned an error."""


def config_error(path: Path, message: str) -> None:
    """Exit with a usage-error code for a problem in the config file."""
    print(f"{path}: {message}", file=sys.stderr)
    sys.exit(2)


def parse_args() -> argparse.Namespace:
    """Parse options; with no arguments, print help and exit."""
    parser = argparse.ArgumentParser(
        description="Set up Last.fm scrobbling in myMPD from "
                    "setup-lastfm.cfg, or save a working setup into it.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-s", "--setup", action="store_true",
        help="write the Last.fm variables into myMPD and add any missing "
             "lastfm triggers")
    mode.add_argument(
        "-l", "--login", action="store_true",
        help="sign in to Last.fm through myMPD's lastfm script to get a "
             "session key and save it (uses [lastfm] username/password "
             "from the config; asks for any not stored)")
    mode.add_argument(
        "-e", "--export", action="store_true",
        help="save the Last.fm variables from myMPD into the config file")
    mode.add_argument(
        "-g", "--genres", action="store_true",
        help="show the genres that are not scrobbled")
    mode.add_argument(
        "-a", "--add-genre", nargs="+", metavar="GENRE",
        help="stop scrobbling songs of these genres (saved in myMPD and "
             "the config; case does not matter)")
    mode.add_argument(
        "-r", "--remove-genre", nargs="+", metavar="GENRE",
        help="scrobble songs of these genres again")
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG, metavar="FILE",
        help=f"config file (default: {DEFAULT_CONFIG})")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="show what would change without changing myMPD or the config")
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(2)
    args = parser.parse_args()
    if not (args.setup or args.login or args.export or args.genres
            or args.add_genre or args.remove_genre):
        parser.error("choose a mode: --setup, --login, --export, --genres, "
                     "--add-genre or --remove-genre")
    if (args.login or args.genres) and args.dry_run:
        parser.error("--dry-run does not apply to --login or --genres")
    for genre in args.add_genre or args.remove_genre or ():
        # The lastfm script splits the blacklist on commas
        if "," in genre or not genre.strip():
            parser.error(f"genre must be non-empty and without commas: "
                         f"{genre!r}")
    return args


def read_config(path: Path) -> configparser.ConfigParser:
    """Read the config, refusing secrets in a file others can read."""
    config = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        return config
    try:
        config.read(path, encoding="utf-8")
    except configparser.Error as error:
        config_error(path, f"cannot parse: {error}")
    has_secret = any(config.get("lastfm", key, fallback="").strip()
                     for key in (*SECRET_KEYS, "password"))
    if has_secret and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        config_error(path, f"holds Last.fm keys but other users can read "
                           f"it; run: chmod 600 {path}")
    return config


class Mympd:
    """Minimal myMPD JSON-RPC client."""

    def __init__(self, url: str, partition: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise MympdError(f"myMPD url must start with http:// or "
                             f"https://: {url!r}")
        self.endpoint = f"{url.rstrip('/')}/api/{partition}"

    def call(self, method: str, params: dict) -> dict:
        """Call one API method and return its result object."""
        body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": method,
                           "params": params}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request,
                                        timeout=TIMEOUT_SECONDS) as reply:
                answer = json.load(reply)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise MympdError(f"{method}: cannot reach myMPD at "
                             f"{self.endpoint}: {error}") from error
        if "error" in answer:
            raise MympdError(f"{method}: {answer['error'].get('message')}")
        result = answer.get("result", {})
        if result.get("severity") == "error":
            raise MympdError(f"{method}: {result.get('message')}")
        return result

    def variables(self) -> dict[str, str]:
        """Return all script variables as {name: value}."""
        data = self.call("MYMPD_API_SCRIPT_VAR_LIST", {}).get("data", [])
        return {entry["key"]: entry.get("value", "") for entry in data}


def script_installed(mympd: Mympd) -> bool:
    """Return True if the lastfm script exists; explain if it does not."""
    scripts = mympd.call("MYMPD_API_SCRIPT_LIST", {"all": True}).get(
        "data", [])
    if any(script.get("name") == SCRIPT_NAME for script in scripts):
        return True
    print(f"The {SCRIPT_NAME!r} script is not installed in myMPD. Import "
          f"it first: Main menu -> Scripts -> Import -> last.fm -> "
          f"{SCRIPT_NAME}.", file=sys.stderr)
    return False


def setup(mympd: Mympd, config: configparser.ConfigParser, path: Path,
          partition: str, dry_run: bool) -> int:
    """Write variables and add missing triggers. Returns an exit code."""
    values = {key: config.get("lastfm", key, fallback="").strip()
              for key in VARIABLES}
    for key in ("api_key", "secret"):
        if not values[key]:
            config_error(path, f"[lastfm] {key} is empty; get it from "
                               f"https://www.last.fm/api")

    if not script_installed(mympd):
        return 1

    needs_key = not values["session_key"]
    if needs_key:
        values["session_key"] = SESSION_KEY_PLACEHOLDER
    prefix = "would set" if dry_run else "set"
    for key, variable in VARIABLES.items():
        if key == "scrobble_genre_blacklist" and not values[key]:
            continue
        if not dry_run:
            mympd.call("MYMPD_API_SCRIPT_VAR_SET",
                       {"key": variable, "value": values[key]})
        shown = values[key] if key not in SECRET_KEYS else "(hidden)"
        if key == "session_key" and needs_key:
            shown = f"placeholder {SESSION_KEY_PLACEHOLDER!r}"
        print(f"{prefix} variable {variable} = {shown}")

    existing = mympd.call("MYMPD_API_TRIGGER_LIST", {}).get("data", [])
    for name, event, argument in TRIGGERS:
        present = any(
            trigger.get("script") == SCRIPT_NAME
            and trigger.get("event") == event
            and trigger.get("arguments", {}).get("trigger") == argument
            for trigger in existing)
        if present:
            print(f"trigger {name!r} already exists")
            continue
        if not dry_run:
            mympd.call("MYMPD_API_TRIGGER_SAVE", {
                "id": -1, "name": name, "event": event,
                "script": SCRIPT_NAME, "partition": partition,
                "arguments": {"trigger": argument}})
        print(f"{'would add' if dry_run else 'added'} trigger {name!r}")

    if needs_key:
        stored = all(config.get("lastfm", key, fallback="").strip()
                     for key in LOGIN_KEYS)
        if not dry_run and (stored or sys.stdin.isatty()):
            print("No session key saved yet; signing in to Last.fm.")
            return login(mympd, config, path)
        print("No session key saved yet. Run this tool with --login to "
              "sign in to Last.fm and save one.")
    return 0


def login(mympd: Mympd, config: configparser.ConfigParser,
          path: Path) -> int:
    """Get a session key through the lastfm script and save it.

    Uses [lastfm] username and password from the config when set and asks
    for any that are missing (the password without echo). They are passed
    to myMPD only for the script's "fetchkey" step; this tool never writes
    them anywhere.
    """
    current = mympd.variables()
    for key in ("api_key", "secret"):
        if not current.get(VARIABLES[key]):
            print(f"myMPD has no {VARIABLES[key]} variable yet; run --setup "
                  f"first.", file=sys.stderr)
            return 1
    if not script_installed(mympd):
        return 1

    username = config.get("lastfm", "username", fallback="").strip()
    password = config.get("lastfm", "password", fallback="")
    try:
        if not username:
            username = input("Last.fm username: ").strip()
        if not password:
            password = getpass.getpass("Last.fm password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nSign-in cancelled.", file=sys.stderr)
        return 1
    if not username or not password:
        print("Username and password are both required.", file=sys.stderr)
        return 2

    session_variable = VARIABLES["session_key"]
    before = current.get(session_variable, "")
    if not before:
        # Script version 7 and older stop early while the key is empty
        mympd.call("MYMPD_API_SCRIPT_VAR_SET",
                   {"key": session_variable,
                    "value": SESSION_KEY_PLACEHOLDER})
        before = SESSION_KEY_PLACEHOLDER
    mympd.call("MYMPD_API_SCRIPT_EXECUTE", {
        "script": SCRIPT_NAME, "event": "user",
        "arguments": {"trigger": "fetchkey", "Username": username,
                      "Password": password}})
    del password

    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    new_key = ""
    while time.monotonic() < deadline:
        value = mympd.variables().get(session_variable, "")
        if value and value not in (before, SESSION_KEY_PLACEHOLDER):
            new_key = value
            break
        time.sleep(LOGIN_POLL_SECONDS)
    if not new_key:
        print(f"No new session key after {LOGIN_WAIT_SECONDS} seconds. "
              f"Check the username and password; myMPD's log shows the "
              f"script's error.", file=sys.stderr)
        return 1

    write_config(path, {
        "api_key": current[VARIABLES["api_key"]],
        "secret": current[VARIABLES["secret"]],
        "session_key": new_key,
    })
    print(f"Signed in. Session key saved in myMPD and {path} (mode 600).")
    return 0


def write_config(path: Path, values: dict[str, str]) -> None:
    """Fill [lastfm] values into the config, keeping comments; mode 600.

    Starts from the existing config, or the .example template when there
    is none, and replaces only the "key =" lines inside [lastfm].
    """
    source = path if path.exists() else EXAMPLE_CONFIG
    if not source.exists():
        config_error(source, "template not found")
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    section, written = "", set()
    for index, line in enumerate(lines):
        header = re.match(r"\s*\[([^]]+)\]", line)
        if header:
            section = header.group(1).strip()
            continue
        match = re.match(r"(\s*)([A-Za-z_]+)(\s*)=", line)
        if section == "lastfm" and match and match.group(2) in values:
            key = match.group(2)
            lines[index] = (f"{key} = {values[key]}\n" if values[key]
                            else f"{key} =\n")
            written.add(key)
    missing = [key for key in values if key not in written]
    if missing:
        config_error(source, f"[lastfm] is missing: {', '.join(missing)}")

    # Write a 600 temp file beside the config, then swap it in atomically
    handle, temp_name = tempfile.mkstemp(dir=path.parent,
                                         prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            temp.writelines(lines)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def export(mympd: Mympd, path: Path, dry_run: bool) -> int:
    """Save myMPD's Last.fm variables into the config."""
    current = mympd.variables()
    values = {key: current.get(variable, "")
              for key, variable in VARIABLES.items()}
    if values["session_key"] == SESSION_KEY_PLACEHOLDER:
        values["session_key"] = ""
    for key in ("api_key", "secret"):
        if not values[key]:
            print(f"myMPD has no {VARIABLES[key]} variable; nothing to "
                  f"export.", file=sys.stderr)
            return 1
    for key, value in values.items():
        state = "(hidden)" if key in SECRET_KEYS and value else value
        print(f"{'would save' if dry_run else 'saved'} {key} = "
              f"{state or '(empty)'}")
    if not dry_run:
        write_config(path, values)
        print(f"Wrote {path} (mode 600)")
    return 0


def split_genres(text: str) -> list[str]:
    """Split a comma separated genre list, dropping blanks and repeats.

    Repeats are matched without regard to case, like the lastfm script
    does; the first spelling is kept.
    """
    result: list[str] = []
    for genre in text.split(","):
        genre = genre.strip()
        if genre and genre.lower() not in (kept.lower() for kept in result):
            result.append(genre)
    return result


def genres(mympd: Mympd, path: Path, add: list[str] | None,
           remove: list[str] | None, dry_run: bool) -> int:
    """Show or change the scrobble genre blacklist.

    Starts from myMPD's current value. Changes are written to myMPD and,
    when the config file exists, to its [lastfm] scrobble_genre_blacklist.
    """
    variable = VARIABLES["scrobble_genre_blacklist"]
    current = split_genres(mympd.variables().get(variable, ""))
    if add is None and remove is None:
        print(", ".join(current) if current else
              "No genres blocked; every genre is scrobbled.")
        return 0

    updated = list(current)
    for genre in split_genres(",".join(add or ())):
        if genre.lower() in (kept.lower() for kept in updated):
            print(f"{genre!r} is already blocked")
        else:
            updated.append(genre)
    for genre in split_genres(",".join(remove or ())):
        matches = [kept for kept in updated if kept.lower() == genre.lower()]
        if not matches:
            print(f"{genre!r} is not blocked")
        for kept in matches:
            updated.remove(kept)
    if updated == current:
        print("Nothing to change.")
        return 0

    value = ",".join(updated)
    prefix = "would set" if dry_run else "set"
    print(f"{prefix} variable {variable} = {value or '(empty)'}")
    if dry_run:
        return 0
    mympd.call("MYMPD_API_SCRIPT_VAR_SET", {"key": variable, "value": value})
    if path.exists():
        write_config(path, {"scrobble_genre_blacklist": value})
        print(f"Saved in {path}")
    else:
        print(f"{path} does not exist; only myMPD was changed. Run --export "
              f"to save the setup.")
    return 0


def main() -> int:
    """Run the chosen mode."""
    args = parse_args()
    config = read_config(args.config)
    if args.setup and not args.config.exists():
        config_error(args.config, "config file not found; copy "
                                  f"{EXAMPLE_CONFIG.name} or use --export")
    url = config.get("mympd", "url", fallback="http://localhost:8081").strip()
    partition = config.get("mympd", "partition", fallback="default").strip()
    try:
        mympd = Mympd(url, partition)
        if args.setup:
            return setup(mympd, config, args.config, partition, args.dry_run)
        if args.login:
            return login(mympd, config, args.config)
        if args.genres or args.add_genre or args.remove_genre:
            return genres(mympd, args.config, args.add_genre,
                          args.remove_genre, args.dry_run)
        return export(mympd, args.config, args.dry_run)
    except MympdError as error:
        print(f"myMPD error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
