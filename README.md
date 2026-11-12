# mympd-lastfm-setup

Set up Last.fm scrobbling in [myMPD](https://github.com/jcorporation/myMPD) from one config file.

myMPD scrobbles to Last.fm through the `lastfm` script from [mympd-scripts](https://github.com/jcorporation/mympd-scripts/tree/main/last.fm). That script needs script variables, three triggers and a Last.fm session key. `setup-lastfm.py` does all of that for you through myMPD's API, and can save a working setup so you can restore it on a fresh myMPD.

## Requirements

- myMPD with scripting enabled, reachable over HTTP (default `http://localhost:8081`)
- Python 3.10 or newer (standard library only, nothing to install)
- A Last.fm API account: get an API key and shared secret at <https://www.last.fm/api>

## Quick start

1. In myMPD, import the script: **Main menu → Scripts → Import → last.fm → lastfm**. The import checks the script's signature, so this step stays in the UI.
2. Create your config and fill in `api_key` and `secret`:

   ```bash
   cp setup-lastfm.cfg.example setup-lastfm.cfg
   chmod 600 setup-lastfm.cfg
   ```

3. Run the setup:

   ```bash
   ./setup-lastfm.py --setup
   ```

   It sets the script variables, adds the triggers and, because there is no session key yet, signs in to Last.fm. It asks for your Last.fm username and password unless they are in the config.

Play a song to check: Last.fm shows it as "now playing" right away and scrobbles it after half its length, or 4 minutes.

## Radio scrobbling

`radio-scrobble.py` is an optional companion for continuous streams.
It recognizes Pandora tracks through the Pandora bridge, matches SomaFM
songs to station history, and reads structured song metadata where
iHeartRadio or TuneIn supplies it. The companion sends Last.fm Now Playing for those recognized songs and
for iBroadcast. The normal myMPD Last.fm script still handles completed
iBroadcast and local scrobbles, and Now Playing for local tracks.

Try it without sending scrobbles:

```sh
python3 radio-scrobble.py --config /path/to/setup-lastfm.cfg \
  --state /path/to/radio-scrobbles.sqlite3 --dry-run
```

Remove `--dry-run` to enable submissions. Keep the state database on a
local filesystem and run one instance. The default MPD address is
`127.0.0.1:6600`. Pandora metadata uses the local bridge on port 18765.

For Docker on a Linux host, use host networking so the container can reach
MPD and the Pandora bridge on loopback:

```sh
export RADIO_LASTFM_CONFIG=/path/to/setup-lastfm.cfg
install -d -m 700 data
export RADIO_UID="$(id -u)" RADIO_GID="$(id -g)"
docker compose -p mympd-radio-scrobble -f compose.radio.yaml up -d --build
```

The config is mounted read-only. The duplicate-prevention database is
stored in `./data/attempts.sqlite3`. The `data` directory must already
exist and be writable by the selected user. Stop any existing native instance
before starting the container.

GitHub Actions builds and checks the image on pull requests and publishes
`latest` and commit SHA tags from `main`. To use the published image,
replace the `build:` block with
`image: ghcr.io/bonelifer/mympd-lastfm-setup:latest`.

The companion skips ads, shows, and unidentified tracks. TuneIn stations
that provide only a plain title are skipped unless their IDs are listed
in `[lastfm] tunein_artist_title_stations`. For example,
`tunein_artist_title_stations = s33220` enables Rock 101.5. Separate
multiple IDs with commas. For these plain titles the companion must
observe both song boundaries and more than 30 seconds of playback before
scrobbling. Opt in only stations that consistently label music
`Artist - Song`.

For other feeds with no reported duration, the companion waits for four minutes of listening or
for a complete track observed between two song changes. The state
database suppresses repeat submissions for the same provider event or,
when an event ID is unavailable, the same track on the same stream within
15 minutes.

## Scan TuneIn favorites

Run this on a host with the `mpd` executable and access to the TuneIn bridge:

```sh
python3 scan-tunein.py scan --report tunein-scan.json
```

The scanner uses private MPD instances with silent output. It checks each
favorite for up to 12 minutes, four at a time, and reports candidates after
two complete plain-title tracks. Use `--station` to check selected favorites.
Structured metadata already works without an opt-in.

Review the candidates, then save the IDs you want to scrobble:

```sh
python3 scan-tunein.py save --report tunein-scan.json \
  --config /path/to/setup-lastfm.cfg --station STATION_ID
```

Repeat `--station` to save more than one candidate. Saving only adds to the
Last.fm allowlist. The TuneIn picker still offers all favorites. Recreate the
radio scrobbler container after saving so it sees the updated bind-mounted
config file.

## Modes

| Option | What it does |
| --- | --- |
| `-s`, `--setup` | Writes the variables from the config into myMPD and adds any missing triggers. Existing triggers are kept, so it is safe to run again. Without a saved session key it then signs in, as `--login` does. |
| `-l`, `--login` | Gets a new session key by running the script's sign-in step in myMPD, then saves the key in myMPD and in the config. |
| `-e`, `--export` | Copies the Last.fm variables from a working myMPD into the config, so you can restore them later with `--setup`. |
| `-g`, `--genres` | Shows the genres that are not scrobbled. |
| `-a`, `--add-genre GENRE...` | Stops scrobbling songs of these genres. Saves the change in myMPD and in the config. |
| `-r`, `--remove-genre GENRE...` | Scrobbles songs of these genres again. |
| `-c`, `--config FILE` | Uses another config file (default: `setup-lastfm.cfg` next to the script). |
| `-n`, `--dry-run` | Shows what `--setup`, `--export`, `--add-genre` or `--remove-genre` would change without changing anything. |

Run `./setup-lastfm.py` with no options for the full help.

## Config

`setup-lastfm.cfg.example` explains each setting. In short:

- `[mympd] url` and `partition`: where myMPD is.
- `[lastfm] api_key` and `secret`: from your Last.fm API account. Required.
- `[lastfm] session_key`: filled in by `--login`. Last.fm session keys do not expire, so once it is saved you never need to sign in again.
- `[lastfm] username` and `password`: optional. Anything left empty is asked for when signing in.
- `[lastfm] scrobble_genre_blacklist`: comma separated genres not to scrobble, `Christmas,Holiday,Holidays,Podcast` by default. Case does not matter, but a song's genre has to match one of them exactly. Change it with `--add-genre` and `--remove-genre`.

The config holds secrets. The tool refuses to read a key, secret or password from a config that other users can read; keep it at `chmod 600`.

## How it works

- Script variables are set with `MYMPD_API_SCRIPT_VAR_SET`: `lastfm_api_key`, `lastfm_secret`, `lastfm_session_key` and, when set, `scrobble_genre_blacklist`.
- Triggers are added with `MYMPD_API_TRIGGER_SAVE`: `mpd_player` (now playing), `mympd_scrobble` (scrobble) and `mympd_feedback` (likes become Last.fm "loves").
- Signing in runs the `lastfm` script's own `fetchkey` step with `MYMPD_API_SCRIPT_EXECUTE`, so the password goes to Last.fm the same way as from myMPD's dialog. The tool then waits for the new session key to appear.

Version 7 and older of the `lastfm` script stop with "No Last.fm Session Key set" before they reach their own sign-in step when no session key exists ([jcorporation/mympd-scripts#48](https://github.com/jcorporation/mympd-scripts/issues/48), fixed in version 8). For those versions the tool sets a placeholder session key first, which the sign-in then replaces. With version 8 and later the placeholder is not needed, but it does no harm.

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/mympd-lastfm-setup/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/mympd-lastfm-setup/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- [myMPD](https://github.com/jcorporation/myMPD) and the [mympd-scripts](https://github.com/jcorporation/mympd-scripts) `lastfm` script by Juergen Mang, which this tool configures.
- Code, tests, and documentation written with [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
