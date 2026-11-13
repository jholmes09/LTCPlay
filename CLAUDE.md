# ltcplay (LTC Player)

Timecode-driven FSEQ show player for Jeff Holmes Presents. Runs GPL 2026 at Dollywood on a Mac. A Windows port for a new project is planned.

## Every change

- Run `python3 selftest.py`. Every check must pass. Put its last line in the PR.
- Run `python3 mutate.py --changed origin/bench-build` when your change touches a safety file (flamesafe, the conductor, the flame link, arming, Stop, the scheduler, the deck, users, fuel): the mutations your change can have broken. Every one must be caught. CI no longer runs them on a PR into bench-build: the nightly sweep does, a release PR into main runs them with the whole safety core, and the weekly sweep runs the whole list. TESTING.md says what runs when and what a failure means.
- Add a mutation only for safety logic: arming, Stop, Hold, Abort, interlocks and refusals, permissions, what reaches the rig (flame, laser, hazer, pump output), fuel and accounting math, persistence and atomic writes. Never for wording, layout or looks: exact text is checked by the selftest itself. TESTING.md, "What deserves a mutation".
- Work on your branch and open a PR. Never push to main, never force-push, never rewrite history. Merge only when Jeff says yes in the session.
- One PR does one thing. A PR never changes show behavior under a title that says something else.

## Never commit

Show renders and media (`.fseq`, `.xsq`, audio, video), `.venv/`, `LTC Player.app`, logs, `ltcplay_prefs.json`, `ltcplay_input.json`, `ltcplay_output.lock`, `VERSION`, `*.before_triggers`, tokens, passwords, keys, or anything from Claude-Brain.

## Show safety

- Nothing reaches the rig until the operator presses Run.
- `on_lost` for GPL is freerun to the end of the show.
- Direct FSEQ playback is primary. Advatek scene triggering (sACN universe 6999, channels 101 to 123, multicast) is the alternate.
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
