# Break-it: what each fault must do (stage 2)

BENCH ONLY, flame hardware DISCONNECTED. `breakit.ps1` causes each fault
on purpose and logs what it saw; a person judges each row against this
table on the stage 2 sheet. The wire witness runs the whole time
(`Start witness.cmd`, normal mode: the soak is NOT running in stage 2).

"Flames off" means the witness shows every slot at zero, or falling to
zero within the time given. "Banner" is the rack screen's banner text.
"Journal" is the night journal (`nights\night_<date>.journal.txt`) or
flamesafe's (`logs\flamesafe.log`). "Recovery" is what brings it back and
how long that may take.

| # | Fault | Flames off | Banner shown | Journal line | Recovery |
|---|---|---|---|---|---|
| 1 | Kill the engine (hard) | flamesafe keeps sending, all zeros; every group disarmed within 0.5 s (frame_stale_ms) | rack screen goes stale until the engine is back; deck Start key reads ENGINE FAULT | flamesafe: `link: show program stopped answering: every group disarmed; cycle the arm to re-arm...`; supervisor log: `ltcplay.exe stopped by itself (exit code ...) ... starting it again in 2 s`; night journal: a new `ltcplay started` line | supervisor restarts it in 2 s (backoff doubles up to 60 s if it keeps dying); page answers within 60 s |
| 2 | Kill flamesafe (hard, the End task hazard) | the stream STOPS with no safe zeros; the witness says `STREAM STOPPED` (last packet zeros because nothing was armed). Had a group been armed the node would HOLD its last values: `STREAM STOPPED WITH NONZERO ON THE WIRE`. That is why this is only done disconnected, and why End task is forbidden on the show PC | flames chip or link banner red while flamesafe is gone (the deck's status link has no frames) | supervisor log: `flamesafe.exe stopped by itself...`; flamesafe: `start: flame universe N to 127.0.0.1:5568...` on return; witness: `STREAM BACK`, `SENDER CHANGED` | supervisor restarts it in 2 to 60 s; every group disarmed, a fresh arm cycle needed |
| 3 | Kill the deck program (the process) | every group disarmed within 0.5 s (arm_stale_ms): safety slots fall to 0 | none from the deck (it is the thing that died); the engine's deck-input reads stale | flamesafe: `arm-input: arm input stale: no fresh assertion...`; supervisor log restart line | supervisor restarts it in 2 to 60 s; keys redraw |
| 4 | Unplug the Stream Deck (hardware; pnputil disable) | NOTHING changes on the wire: armed groups stay armed (Jeff, 2026-10-07 and 2026-10-12: a dying deck must not disarm by itself; the deck process keeps the arm link steady) | `The Stream Deck is unplugged or not answering. Every flame group stays as it was. Press Acknowledge once you have seen it, or Disarm all.` | night journal fault line, action `deck`: `Stream Deck: ... unplugged ...` (once per outage) | re-enable: `The Stream Deck is back. Every flame group stayed as it was while it was away.`; redraw within 10 s; banner stays until Acknowledge |
| 5 | Remove the audio interface (pnputil disable) | no change (nothing armed); a show cannot start | `audio` banner naming the device and the outputs Windows can see; Audio chip `not attached` (within 30 s, CHECK_S) | `failed start` if a show was due; the audio watch's own line when it comes back | re-enable: found within 30 s; banner stays until Acknowledge |
| 6 | Drop the show network adapter | no change: flamesafe's stream to 127.0.0.1 is untouched (witness continuous, zeros); the flame link, status link and arm link are all loopback | network remote pages show their stale banner; pixel output errors may raise a fault banner | engine: `output` fault lines (send errors to the pixel controllers); send error counters climb in health | Enable-NetAdapter; sends resume within seconds, no restart |
| 7 | Fill the disk to the threshold | no change | disk chip low: `The disk holding ... has X GB free. Free some space...` under 5 GB; critical under 1 GB (`-Critical`): `... has only X GB free...` | supervisor log repeats the sentence; the journal keeps writing (a failing disk stops logging, never the show) | delete the fill file (`-Undo`); clears within a minute |
| 8 | Delete the lockout file while a lock is set (Morning build only) | the locked slots STAY locked at flamesafe (a deleted file never lifts a lock: PR 68 P2) | lockout file problem on every screen until fixed | lock, unlock, purge and laser test refused with the file named | restore the backup (`-Undo`) |
| 9 | Corrupt the lockout file (Morning build only) | nothing lifts: an unreadable file is refused, locks stand (PR 68 P1-3) | shown on every screen until fixed | every lock, unlock, purge and laser test refused, the file named | restore the backup |
| 10 | Corrupt the morning config (Morning build only) | no change | Morning page refuses with one sentence naming the file | the sentence in the journal | restore the backup |
| 11 | Close the page mid-purge (Morning build, operator holds the purge) | the purge fire slot falls to 0 within 0.25 s of the kill (heartbeat gap; the flame link drops the purge frame 0.25 s after the last beat; flamesafe's fire_hold_ms behind it) | none needed | purge released or stopped line | `LTC Player --start` reopens the page |
| 12 | Start a show while a purge is held (Morning build, operator holds the purge) | the purge is cleared the moment the scheduler enters SHOW: fire slot to 0 within 0.25 s of the press; the show fires only its own cues once the timecode moves | the show banner as usual | purge cleared, then the Start now, in that order | none needed |

## How to run it

In an elevated PowerShell on the Pico, with the witness already running:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\breakit\breakit.ps1 -List
    ... -Steps 1,2,3,7 -FlameHardwareDisconnected -Confirmation DISCONNECTED
    ... -Steps 5 -AudioDevice "Scarlett" -FlameHardwareDisconnected -Confirmation DISCONNECTED
    ... -Steps 6 -Adapter "Ethernet 2" ...        NEVER the adapter the remote session uses
    ... -Undo                                      put back files, devices, adapter, fill

Steps 8 to 12 skip themselves until the Morning page build is installed
(`ltcplay_morning.json` present). Steps 4 and 5 skip when the device is
not plugged in. Each step is refused while a show is running or starting
(steps 11 and 12 excepted, by design).

After the run: `Check witness.cmd` (or the checker where Python is). The
checker must say PASS: in stage 2 nothing is armed, so any nonzero slot is
a FAIL; the only stall allowed is step 2's, and it must read `STREAM
STOPPED`, never `WITH NONZERO ON THE WIRE`.

## Reading breakit-results.csv

One row per step: when, what was done, the expected text from this table,
what was seen 3 s after and after recovery (engine answering, flamesafe,
deck and supervisor alive, the banners, the last flamesafe and journal
lines), and the recovery time. The last column is `OBSERVED, judge
against the table`, `NOT RECOVERED`, `SKIPPED`, `REFUSED` or `ERROR`. A
person ticks PASS or FAIL per row on the stage 2 sheet.
