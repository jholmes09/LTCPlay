# ltcplay (LTC Player)

Timecode-driven FSEQ show player for Jeff Holmes Presents. main is developed for Fire & Ice 2026 on Windows.

GPL 2026 at Dollywood is frozen at tag `gpl-2026.09.15.10`, file-for-file what runs on its offline Mac, which is never updated. main no longer has to keep GPL behavior. Anything Dollywood needs comes from that tag (download its zip from the repo's Tags page), never from main.

## Every change

- Run `python3 selftest.py`. Every check must pass. Put its last line in the PR.
- Changes to `trigger.py`, `output.py`, `player.py`, `session.py`, `clock.py` or anything under `flamesafe/` also run `python3 mutate.py`. Every mutation must be caught.
- Work on your branch and open a PR. Never push to main, never force-push, never rewrite history. Merge only when Jeff says yes in the session.
- One PR does one thing. A PR never changes show behavior under a title that says something else.

## Never commit

Show renders and media (`.fseq`, `.xsq`, audio, video), `.venv/`, `LTC Player.app`, logs, `ltcplay_prefs.json`, `ltcplay_input.json`, `ltcplay_output.lock`, `VERSION`, `*.before_triggers`, tokens, passwords, keys, or anything from Claude-Brain.

## Show safety

- Nothing reaches the rig until the operator presses Run.
- In the GPL release: `on_lost` is freerun to the end of the show, and direct FSEQ playback is primary. Advatek scene triggering (sACN universe 6999, channels 101 to 123, multicast) is the alternate.
- The native window app stays BETA until Jeff releases it.

## Layout

- This repo is the flat dev layout. A shipped install moves the rarely used launchers into `Tools/` and adds `packaging/full/START HERE.txt`. The full show package root carries `packaging/full/READ ME FIRST.txt`.
- The update pack is `Apply this update.command` and `packaging/update/READ ME.txt` at its root, with the program under `_payload/`.
- `ltcplay/version.py` hashes the package, every top-level and `Tools/` `.command`, `ltc` and `selftest.py`. Moving or adding a launcher changes the build id.
- `VERSION` is written by `Cut a release.command` at packaging time. A release is also a tag on main named `gpl-YYYY.MM.DD.N`.

## Where the work happens

- Code changes, PRs and CI: a cloud session with this repo attached.
- Building the app, packaging, installing and testing on a Mac: a Cowork session linked to Jeff's Mac, which reads this repo and never runs git inside a Dropbox folder.

## Copy

Operator-facing text has no em or en dashes. Launchers are `.command` files Jeff double-clicks; never ask him to type into Terminal.
