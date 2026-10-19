# The 48 hour soak plan (stage 1 of the stress test)

BENCH ONLY. The Pico show PC, the installed bench build, flame hardware
DISCONNECTED (data cable to the flame node unplugged, gas off). Nothing in
this plan arms or fires real hardware: the soak's own flamesafe copy sends
its sACN to 127.0.0.1 and nowhere else, and the soak's listener on that
port judges every packet.

## What runs

The installed soak program, unattended, for two days:

    ltcplay-soak.exe --days 2

registered as the scheduled task "LTC Player unattended soak" by
`register_soak_task.ps1 -Days 2` (it survives a reboot and a crash and
carries on from `unattended_state.json`). Blocks of 110 minutes run back to
back, each a whole soak of its own in a stamped folder under
`%LOCALAPPDATA%\ltcplay\soak\`: the engine (`ltc serve --schedule`, the
scheduler, the show conductor, the flame link), the real `flamesafe.exe`,
the deck program with a virtual Stream Deck (or the real Mini if plugged
in), a 7 min 24 s show every 15 minutes, every group armed by the
exerciser through the screen's arm-hold, a Hold every 2 minutes, an Abort
and Reset on every other show.

Containment ON. `--contain on` is a command line switch for the timed
soak only; in `--days` mode it is read from `unattended.json`, so the start
script writes `"contain": true` there first. Containment puts MadMapper and
BEYOND at Below normal and keeps MadMapper off two CPUs; it is the setting
that goes to the show. With neither program installed on the Pico the soak
runs in fallback mode and counts their commands itself; containment then
has nothing to contain, and the summary says so rather than failing.

`restart_cmd` and `recover_cmds` (unattended.json): only when MadMapper
and BEYOND are installed on this PC. `restart_cmd` runs before every block
(the wrapper that starts both through explorer.exe); `recover_cmds` names
the command for each program that is not running for 30 s inside a block.
Leave both empty when they are not installed. The start script keeps
whatever is already there unless `-RestartCmd` is given.

## How to start it

From the kit folder on the Pico, in an elevated PowerShell (register needs
it), with the hardware confirmed disconnected:

    powershell -NoProfile -ExecutionPolicy Bypass -File ".\soak\Start 48h soak.ps1" -FlameHardwareDisconnected -Confirmation DISCONNECTED

It refuses without both words. It then: checks `ltcplay-soak.exe` and
prints its version; refuses if anything but flamesafe holds UDP 5568 (a
witness in normal mode would make the soak refuse its first block); writes
`unattended.json` (contain true, 110 minute blocks, keep 7 days, 20 GB
cap); removes a stale `STOP_UNATTENDED` and `unattended_state.json` so the
run starts at block 1; runs `register_soak_task.ps1 -Days 2`; starts the
task; starts the wire witness in COMPANION mode (processes and disk every
10 s, no port); waits for `status.json` and prints it.

The show audio interface (Scarlett or PreSonus, as the show file names it)
must stay plugged in: without it every show fails to start and the block
fails with "Show audio player".

## How to watch it

    ... "Start 48h soak.ps1" -Status

prints `status.json` (phase, block, last verdict, free disk), the last
lines of the day's file and the task state. The Desktop gets one line per
block in `LTC Player soak daily <date>.txt`. Each block's own report is
`block N report.txt` in its folder, rewritten every minute.

## How to stop it early

    ... "Start 48h soak.ps1" -Stop

(the soak finishes the block now running, up to 110 minutes, then starts
LTC Player again; the task is removed). Or Start menu, "Stop the
unattended soak", then `register_soak_task.ps1 -Remove`.

## Pass criteria for stage 1

Judged by `soak_summary.py` from the soak's own files (`python
soak_summary.py`, or collect the evidence and run it where Python is):

1. Zero faults: every block PASSED by the soak's report; no block crashed
   or needed a recovery command.
2. Zero flame violations: `flame-violations.txt` empty in every block (the
   soak's judge: a group's channels nonzero only while flamesafe reports it
   armed, fire only during a show, never in a Hold, never after an Abort).
3. Flat memory: no program's RSS grows faster than 10 MB per hour in any
   block (the soak's own MEM_GROWTH_MB_H); the summary prints the worst
   slope per program.
4. Timecode gaps inside the limit: the Art-Net timecode item never FAILs
   (no 3 frames missing inside a show, 100 ms), the flame link never gaps
   past 50 ms, the pixel output never gaps past 100 ms, no engine stall
   item fails.
5. Containment ON in `unattended.json` and in every block report.
6. At least 48 hours soaked (one block of slack allowed), free disk never
   under 5 GB.
7. A REAL show audio interface in every block: the block report's "Show
   audio" line names it, never "NO interface found" (2026-10-18: 48 hours
   with no interface, 0 shows played). `Start 48h soak.ps1` refuses to
   start without one. A run started with `-FakeAudio` (only when Jeff says
   so) plays on the engine's stand-in device: it proves the engine, the
   scheduler, the flame link, the timecode, the Art-Net outputs and the
   witness, NOT the real audio path; the status, the daily lines and the
   summary all say AUDIO WAS FAKE, its verdict reads "PASS (AUDIO WAS
   FAKE)", and it is not a show-night soak.

The summary writes `SOAK SUMMARY <stamp>.md` in the soak folder and on the
Desktop and exits 0 PASS, 1 FAIL, 2 still running.

## What the witness does during stage 1

The soak owns 127.0.0.1:5568 for its own judge and refuses to start if
anything else holds it. So in stage 1 the witness runs in companion mode:
it records each LTC Player program's working set, handles and CPU and the
free disk every 10 s (`procs.csv`, `disk.csv`), independent of the soak's
samples, and nothing from the wire. The wire judge for stage 1 is the
soak's own listener, a separate program from flamesafe (ltcplay-soak.exe)
but part of the same install. The witness takes the wire itself in stages
2 to 4, when the soak is not running.

## What stage 1 proves, and what it does not

Proves: the installed build runs the whole Fire & Ice stack for two days
with shows every 15 minutes, arming and firing through the real paths,
without a fault, a leak or a timing gap, and with flamesafe's output
obeying its rules as seen from a second program on the same PC.

Does not prove: real flame hardware, the real network path to the flame
node, the Stream Deck hardware (virtual unless plugged in), operator
mistakes, MadMapper and BEYOND unless installed here. Those are stages 2
to 5.
