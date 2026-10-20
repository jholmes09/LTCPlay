# The stress kit: witness, 48 hour soak, break-it, rehearsals, sheets

BENCH TOOLS for the Windows show PC (the Pico). Nothing in this folder
arms or fires real hardware; every script that could make the installed
programs arm or fire (the soak, break-it, the rehearsals) REFUSES unless
the person running it states the flame hardware is disconnected, and each
one says so in words. The kit is not part of the installed app and not run
by the selftest; it reads the app's files and talks to the engine's own
rack screen routes on 127.0.0.1 only.

## What is here

| Folder | What | Runs on |
|---|---|---|
| `witness/witness.ps1`, `Start witness.cmd`, `Stop witness.cmd` | the WIRE WITNESS: an independent recorder of the flame universe (flamesafe's sACN to this PC), every packet timestamped, every nonzero channel to CSV, a rolling log, LTC Player's processes and the disk every 10 s | the Pico, nothing to install |
| `witness/witness.py` | the same recorder in Python (reference; the same files) | any machine with Python |
| `witness/laser_listener.ps1`, `Start laser listener.cmd` | the LASER LISTENER: stands where BEYOND would (the Pico has MadMapper but no BEYOND yet), binds BEYOND's OSC port from ltcplay_fire_ice.json's `beyond` block (default 127.0.0.1:8100) and every local port ltcplay_morning.json's lasers name, decodes every OSC message with its time, address and arguments, names it (BLANK, UNBLANK, TEST ON/OFF/LOCK a laser, OTHER, FORBIDDEN) and writes `laser.log`, `laser.csv` and `laser.json` into the witness run folder; REFUSES with one sentence, and writes nothing, while real BEYOND or the soak holds the port | the Pico, nothing to install |
| `witness/laser_listener.py` | the same listener in Python (reference; the same files) | any machine with Python |
| `witness/check_witness.py`, `Check witness.cmd` | the CHECKER: witness run plus the engine's machine log plus flamesafe's journal; flags nonzero channels with no right to be there and every gap or stall; with the listener's files it judges the lasers too: a blank within 2 s of every accepted Abort and every Hold that paused a show, every blank the journal says was sent heard, no restore outside SHOW or while an Abort stood, a laser test's ON only while the Morning page had it on and at its configured value, never live, latched or locked out, dark at its OFF, a show start and an Abort, and nothing else sent to the lasers | Python (the Pico if it has one, else Jeff's Mac or the cloud session) |
| `witness/collect_evidence.ps1`, `Collect witness evidence.cmd` | zips the evidence to the Desktop (no media, no keys) for the checker and the summary elsewhere | the Pico |
| `soak/Start 48h soak.ps1`, `SOAK PLAN.md`, `soak_summary.py` | the 48 HOUR SOAK: registers and starts the installed unattended soak with containment on, watches it, stops it; the plan and pass criteria; the summary generator | the Pico (start, status, stop); Python for the summary |
| `breakit/breakit.ps1`, `BREAK-IT TABLE.md` | BREAK-IT: 12 faults on purpose, each logged and undoable, with the expected result of each | the Pico, elevated PowerShell |
| `rehearsals/rehearsals.ps1` | STAGE 4: 20+ back-to-back scheduled shows with random Aborts and Resets through the rack screen routes | the Pico |
| `sheets/PASS-FAIL sheets.html` | one printable PASS/FAIL sheet per stage, 1 to 5 | any browser; print, one page each |
| `PASTE PROMPT.md` | the message Jeff pastes into the Windows session on the Pico to install the newest bench build, start the witness and the soak, and report back; it refuses without Jeff's word DISCONNECTED | Jeff |

Tests: `python witness/test_witness.py` (40 tests: the recorder and the
laser listener on real loopback sockets, the sACN and OSC parsers, the
listener's refusals, every checker rule with a case that passes and one
that fails) and `python soak/test_soak_summary.py` (7 tests). Both
standard library only.

## Pre-flight, before any stage

Four lines, each printed and true, before the soak or any stage starts
(the 2026-10-18 soak ran 48 hours with no audio interface, played 0 shows
and tested almost nothing):

1. **Audio**: the show audio interface (Scarlett or PreSonus) attached and
   listed by Windows. `Start 48h soak.ps1` REFUSES without it; `-FakeAudio`
   (only when Jeff says so) runs the engine's stand-in device instead,
   which proves the engine, the scheduler, the flame link, the timecode,
   the Art-Net outputs and the witness, NOT the real audio path, and the
   status, the daily lines and `soak_summary.py` all say AUDIO IS FAKE.
2. **MadMapper 6.1.8** (licensed) set up once with the six video files. A
   restart wrapper for an older MadMapper Demo in `unattended.json` is
   stale: the starter detects a restart_cmd whose target is missing or
   older than the installed MadMapper, warns and blanks it (keep it with
   `-KeepRestartCmd`, replace it with `-RestartCmd`).
3. **BEYOND**: present (leave it alone, judge the lasers there) or absent
   (the laser listener stands in; "Lasers: blanking not set up" is then
   EXPECTED, not a FAIL).
4. **Flame hardware DISCONNECTED**, typed by Jeff as the bare word; every
   script that arms or fires refuses without it.

The starter also refuses an elevated PowerShell that is not the signed-in
user's (the task and the settings folder would be another account's) and
a stale `STOP_UNATTENDED` it could not remove; `Start 48h soak.ps1
-SelfTest` proves those checks on a temp folder.

## The 19 channel show track in the soak

The soak plays a generated show track. Left alone it is the old stereo
track. For the show's own layout (19 channels plus timecode) start the
soak with:

    powershell -NoProfile -ExecutionPolicy Bypass -File "Start 48h soak.ps1" `
        -FlameHardwareDisconnected -Confirmation DISCONNECTED `
        -AudioChannels 19 -LtcOutput 20

That writes `audio_channels` 19 and `ltc_output` 20 into `unattended.json`
and every block then plays a 19 channel 48 kHz 24-bit track, each channel
its own quiet tone (channel k at 110 x k Hz, so Music L is 110 Hz and
Sound Effects Atmo R is 2090 Hz), plus a mono LTC track on output 20,
through the real engine and the real interface for the whole soak. The
block's notes name each channel, its tone and its output:

| Channel | Track | Tone | Output (default) |
|---|---|---|---|
| 1, 2, 3 | Music L, C, R | 110, 220, 330 Hz | 1, 2, 3 |
| 4, 5 | Music Atmo L, R | 440, 550 Hz | 4, 5 |
| 6, 7, 8 | Lily L, C, R | 660, 770, 880 Hz | 6, 7, 8 |
| 9, 10, 11 | Jack L, C, R | 990, 1100, 1210 Hz | 9, 10, 11 |
| 12, 13, 14 | Narrator L, C, R | 1320, 1430, 1540 Hz | 12, 13, 14 |
| 15, 16, 17 | Sound Effects L, C, R | 1650, 1760, 1870 Hz | 15, 16, 17 |
| 18, 19 | Sound Effects Atmo L, R | 1980, 2090 Hz | 18, 19 |
| LTC | timecode | | 20 |

**Setting the channel map.** The map is the output each channel plays on,
one number per channel, in channel order. To send the channels somewhere
else, add `-AudioMap` with all 19 numbers written out, for example
`-AudioMap "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19"` (the
default) or the same list in another order. No output may appear twice
and the LTC output may not be one of them; a map that breaks either rule
stops the block with a sentence saying which. Leave out `-LtcOutput` to
play no timecode track (then 19 outputs are enough). Nothing assumes
pairs: an odd count is fine.

In the show file itself the same map is the stem's `channels` list
(`"channels": [1, 2, ..., 19]`, every number written out), the outputs
the show uses are `"channels": 20`, and the timecode output is
`"ltc_output": 20`. Existing show files without `ltc_output` play exactly
as before. The README at the top of the repo has the full example.

What it proves, and what it does not:

- The interface must offer 20 outputs through the way the engine opens it
  (ASIO first on the show PC). If it offers fewer, Run is refused and the
  block FAILS with the engine's sentence naming how many outputs each way
  to the interface has: never a quiet play on fewer outputs. PreSonus
  lists the StudioLive 16R's USB audio as 18 x 18, so expect that refusal
  on the 16R until the outputs are confirmed: the first block's notes and
  its refusal say exactly how many the 16R offers over ASIO.
- With `-FakeAudio` the stand-in device takes all 20, so only the engine
  side is proven.
- Each block writes a 19 channel track of about 1.2 GB into its folder
  (the soak's 20 GB cap removes the oldest folders), and the engine holds
  about 1.7 GB of decoded audio for a 7 min 24 s show.
- Listening is still a person's job: put a meter or headphones on each
  output once and check the tone matches the table.

## The five stages, in one paragraph each

1. **Soak, 48 h** (`SOAK PLAN.md`): the installed `ltcplay-soak.exe
   --days 2` as a scheduled task, containment on, blocks of 110 min, a
   7:24 show every 15 min, groups armed and fire cues played through the
   real programs, flamesafe's sACN forced to 127.0.0.1 where the soak's
   own listener judges it. The witness runs in companion mode (processes
   and disk only): the soak owns the flame port and refuses to start if
   anything else holds it. Pass: `soak_summary.py` says PASS on its six
   criteria (zero faults, zero violations, flat memory, timecode inside
   limits, containment on, 48 h).
2. **Break-it** (`BREAK-IT TABLE.md`): the soak is stopped, LTC Player
   runs as on show night, the witness holds the wire and the laser
   listener holds BEYOND's port. `breakit.ps1` kills the engine,
   flamesafe and the deck program, unplugs the deck and the audio
   interface (pnputil), drops the network adapter, fills the disk, and,
   once the Morning page build is installed, deletes and corrupts the
   lockout file and the morning config, closes the page mid-purge, starts
   a show under a held purge, and (step 13) turns a laser test on, starts
   a show and Aborts it with the listener judging. Each row has flames
   off, banner, journal line, recovery. Pass: every row as the table
   says, and the checker PASSes (nothing is armed, so any nonzero slot is
   a FAIL; the laser rules PASS or read UNVERIFIED only for the expected
   "blanking not set up").
3. **Purge and lockout hammering** (sheet 3): the Morning page's purge
   and locks worked hard by hand and from the deck, witnessed; the laser
   tests switched on, Aborted, locked and started over with the listener
   recording; the checker with `--locked-slots` and the machine log.
4. **Dress rehearsals** (`rehearsals.ps1`): 20+ scheduled shows back to
   back with random Aborts and Resets; the checker must show fire only in
   SHOW, nothing while an Abort stood, every armed slot backed by a press,
   and, from the listener, BEYOND's blank within 2 s of every Abort and
   the lasers restored only inside SHOW.
5. **Staged real hardware with Andy** (sheet 5): gas OFF first, the real
   node as the destination, Andy's eyes on the node's channels; gas ON
   only for the last two lines, Andy's hand on the e-stop.

## How Jeff reads results

- Stage 1: the Desktop gets one line per block (`LTC Player soak daily
  <date>.txt`) and, when the summary runs, `LTC Player SOAK SUMMARY
  <stamp>.md` with the verdict first and six PASS/FAIL lines.
- Stages 2 to 4: `check_report.txt` in the witness run folder, verdict
  first, then each violation episode in plain words with its time. The
  witness's own `witness.log` reads top to bottom: STREAM START, RISE and
  fall of each slot with its group's name, gaps, STREAM STOPPED. The
  listener's `laser.log` reads the same way: `listen: OSC on
  127.0.0.1:8100`, then one line per message, `BLANK:` (brightness 0,
  what every Abort, Hold and intermission must produce), `UNBLANK:`
  (brightness 100, only inside a show once the timecode moves), `TEST ON
  Laser 1:` / `TEST OFF Laser 1:` (the Morning page's test, the exact
  address and value from ltcplay_morning.json), `OTHER:` (anything else:
  always a question), and a `summary:` every 10 minutes. `laser.csv` has
  the same rows for the checker. The report's LASER LISTENER line counts
  them, and its "From the journals" lines count the standing laser fault
  apart from every other fault.
- Break-it: `breakit-results.csv` under Settings and logs\witness, one row
  per fault, judged against the table on sheet 2.
- Every stage: the printed sheet, signed.

## What the kit cannot see

- The witness hears only packets that end on this PC (127.0.0.1 or one of
  its own addresses). With flamesafe's destination set to the real node,
  it hears nothing: that is stage 5, judged at the node.
- During the soak the wire judge is the soak's own listener (a separate
  program, `ltcplay-soak.exe`, but from the same install); the witness is
  independent only in stages 2 to 4.
- The checker's purge and lockout windows come from the machine log's
  wording (PR 68 is still being finished); until MORNING.md's exact lines
  are confirmed, read each FIRE OUTSIDE SHOW episode against the witness
  log before calling it.
- No stage here proves the gas side, the e-stop chain, or the node's own
  hold behaviour on link loss. Only Andy's rig does.
- The laser listener proves that a laser command LEFT LTC Player at the
  right moment, to the right address, with the right value, and that
  nothing else was sent. It cannot prove what real BEYOND would do with
  it (that it goes dark at brightness 0, that its TC-IN toggle is on, that
  "keep running when timecode stops" is off): BEYOND answers nothing and
  none ran. During the soak it does not run at all (ltcplay-soak.exe holds
  port 8100 and judges the lasers itself). With no `beyond` block in
  ltcplay_fire_ice.json the conductor sends no blank anywhere, the engine
  says "Lasers: blanking not set up" (EXPECTED while BEYOND is absent, not
  a FAIL) and the listener hears only the Morning page's test messages;
  the blank-after-Abort rules then read UNVERIFIED, never PASS.
