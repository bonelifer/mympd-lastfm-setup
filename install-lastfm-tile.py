#!/usr/bin/env python3
"""Install a myMPD Home tile linking to a Last.fm profile."""

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def rpc(endpoint: str, method: str, params: dict) -> dict:
    """Call myMPD's Home API and return its result."""
    headers = {"Content-Type": "application/json"}
    if session := os.environ.get("MYMPD_SESSION"):
        headers["X-myMPD-Session"] = session
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode()
    with urlopen(Request(endpoint, body, headers), timeout=10) as response:
        data = json.load(response)
    if "error" in data or "result" not in data:
        raise ValueError(f"myMPD rejected {method}")
    return data["result"]


def install(endpoint: str, thumbs: Path, username: str) -> None:
    """Copy the MIT-licensed icon and create or update the profile tile."""
    source = Path(__file__).resolve().parent / "assets" / "lastfm.png"
    target = thumbs / source.name
    tiles = rpc(endpoint, "MYMPD_API_HOME_ICON_LIST", {}).get("data") or []
    params = {
        "replace": False, "oldpos": 0, "name": "Last.fm", "ligature": "",
        "bgcolor": "#d51007", "color": "#ffffff", "image": source.name,
        "cmd": "openExternalLink",
        "options": [f"https://www.last.fm/user/{quote(username, safe='')}", "true"],
    }
    for position, tile in enumerate(tiles):
        options = tile.get("options") or []
        if (tile.get("name") == "Last.fm"
                and tile.get("cmd") == "openExternalLink" and options
                and options[0].startswith("https://www.last.fm/user/")):
            params.update(replace=True, oldpos=position)
            break
    if target.is_symlink():
        raise OSError(f"Refusing to overwrite a symlink: {target}")
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise OSError(f"A different image already exists at {target}")
    else:
        shutil.copyfile(source, target)
        target.chmod(0o644)
    rpc(endpoint, "MYMPD_API_HOME_ICON_SAVE", params)


def main() -> None:
    """Validate the profile and install its Home tile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-u", "--username", required=True, help="Last.fm username")
    parser.add_argument("-m", "--mympd", required=True, help="myMPD base URL")
    parser.add_argument("-w", "--workdir", required=True, type=Path)
    parser.add_argument("-p", "--partition", default="default")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.username):
        parser.error("Enter a Last.fm username, not a profile URL")
    try:
        base = urlsplit(args.mympd)
        if (base.scheme not in {"http", "https"} or not base.hostname
                or base.username is not None or base.query or base.fragment
                or any(ord(char) <= 32 for char in args.mympd)):
            parser.error("--mympd must be an HTTP(S) URL without credentials or queries")
        thumbs = args.workdir / "pics" / "thumbs"
        if not thumbs.is_dir():
            parser.error("--workdir must contain myMPD's pics/thumbs directory")
        endpoint = f"{args.mympd.rstrip('/')}/api/{quote(args.partition, safe='')}"
        install(endpoint, thumbs, args.username)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Installation failed: {error}\n")
    print(f"Last.fm Home tile installed for {args.username}.")


if __name__ == "__main__":
    main()
