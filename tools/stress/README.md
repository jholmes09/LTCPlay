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
| `witness/check_witness.py`, `Check witness.cmd` | the CHECKER: witness run plus the engine's machine log plus flamesafe's journal; flags nonzero channels with no right to be there and every gap or stall | Python (the Pico if it has one, else Jeff's Mac or the cloud session) |
| `witness/collect_evidence.ps1`, `Collect witness evidence.cmd` | zips the evidence to the Desktop (no media, no keys) for the checker and the summary elsewhere | the Pico |
| `soak/Start 48h soak.ps1`, `SOAK PLAN.md`, `soak_summary.py` | the 48 HOUR SOAK: registers and starts the installed unattended soak with containment on, watches it, stops it; the plan and pass criteria; the summary generator | the Pico (start, status, stop); Python for the summary |
| `breakit/breakit.ps1`, `BREAK-IT TABLE.md` | BREAK-IT: 12 faults on purpose, each logged and undoable, with the expected result of each | the Pico, elevated PowerShell |
| `rehearsals/rehearsals.ps1` | STAGE 4: 20+ back-to-back scheduled shows with random Aborts and Resets through the rack screen routes | the Pico |
| `sheets/PASS-FAIL sheets.html` | one printable PASS/FAIL sheet per stage, 1 to 5 | any browser; print, one page each |
| `PASTE PROMPT.md` | the message Jeff pastes into the Windows session on the Pico to install the newest bench build, start the witness and the soak, and report back; it refuses without Jeff's word DISCONNECTED | Jeff |

Tests: `python witness/test_witness.py` (19 tests: the recorder on a real
loopback socket, the parser, every checker rule) and `python
soak/test_soak_summary.py` (7 tests). Both standard library only.

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
   runs as on show night, the witness holds the wire. `breakit.ps1` kills
   the engine, flamesafe and the deck program, unplugs the deck and the
   audio interface (pnputil), drops the network adapter, fills the disk,
   and, once the Morning page build is installed, deletes and corrupts
   the lockout file and the morning config, closes the page mid-purge and
   starts a show under a held purge. Each row has flames off, banner,
   journal line, recovery. Pass: every row as the table says, and the
   checker PASSes (nothing is armed, so any nonzero slot is a FAIL).
3. **Purge and lockout hammering** (sheet 3): the Morning page's purge
   and locks worked hard by hand and from the deck, witnessed; the checker
   with `--locked-slots` and the machine log.
4. **Dress rehearsals** (`rehearsals.ps1`): 20+ scheduled shows back to
   back with random Aborts and Resets; the checker must show fire only in
   SHOW, nothing while an Abort stood, every armed slot backed by a press.
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
  fall of each slot with its group's name, gaps, STREAM STOPPED.
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
