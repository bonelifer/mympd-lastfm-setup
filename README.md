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
