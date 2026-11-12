# The night journal: what a show start, finish and stop look like

For whoever counts shows from the night journal on the server (the nightly
upload sends each closed night's files there). Every example below is real
output: it was made by running the real scheduler (`schedule.py` through
`schedule_service.py` and `journal.py`) on a test clock, and the selftest
(`test_journal_format_doc_is_real_output`) runs the same night again and
fails if any line here stops matching.

## The two files

Each night has two files, written from the same events, one line each:

- `night_YYYY-MM-DD.journal.txt`: one plain English line per event. The
  time (`HH:MM:SS`, local), two spaces, then the sentence.
- `night_YYYY-MM-DD.jsonl`: the same event as one JSON object, every
  field. Its `line` field is exactly the journal file's line.

The night is the show night, not the calendar day: a show after midnight
stays on the night it belongs to.

The fields of each `.jsonl` record:

| Field | What it is |
|---|---|
| `id` | unique: the run, a dash, a counter. A restart starts a new run |
| `at` | local time with its offset, to the millisecond |
| `night` | the show night, `YYYY-MM-DD` |
| `state`, `to_state` | the scheduler's state before and after the event |
| `actor` | `scheduler`, `operator`, `madmapper`, `safety`, `reader` or `system` |
| `action` | what happened (the table below) |
| `outcome` | how it ended (the table below) |
| `reason` | for a show: its status words, such as `FIRED` or `DONE (...)` |
| `show` | the show number, or `null` |
| `who`, `screen` | for an operator: who pressed, and on which screen |
| `fault` | `true` when the line is a fault |
| `text` | the sentence; `line` is the time and the sentence |

## How to count shows

| Event | `action` | `outcome` | `reason` starts with | `actor` |
|---|---|---|---|---|
| Scheduled start | `fire` | `fired` | `FIRED` | `scheduler` |
| Start now (an extra show) | `START_NOW` | `fired` | `EXTRA SHOW (operator)` | `operator` |
| Start now (the delayed show) | `START_NOW` | `fired` | `DELAYED START (operator hold)` | `operator` |
| Start at a cue | `START_NOW` | `fired` | `EXTRA SHOW (operator)`; the text says "Start at a cue" and names the cue | `operator` |
| Finished | `SHOW_ENDED` | `done` | `DONE` | `system` (the show runner) |
| Operator Stop | `STOP_SHOW` | `aborted` | `STOPPED (operator Stop)` | `operator` |
| Failed start | `SHOW_FAILED` | `fault` | `FAULT (` and why | `system` (the show runner) |
| Cut off by a restart | `BOOT_DONE` | `fault` | `FAULT (ltcplay restarted during the show)` | `system` |
| Hold during a show | `HOLD` | `paused` | `PAUSED (operator hold)` | `operator` |
| Resume after that Hold | `RESUME` | `resumed` | `RESUMED (paused ...)` | `operator` |

The rules:

1. A show STARTED once for each record with `outcome` `fired` and a
   `show` number (`action` `fire` or `START_NOW`). Count nothing else as a
   start. The `START_SHOW` lines after it are what was sent to MadMapper
   (`not performed` on a machine with none attached, `done` on the show
   PC); they are not a second start.
2. Each started show then ENDS exactly once, by one of: `SHOW_ENDED`
   `done` (finished), `STOP_SHOW` `aborted` (operator Stop),
   `SHOW_FAILED` `fault` (it never really started), or `BOOT_DONE`
   `fault` with that show's number (ltcplay restarted while it ran).
3. A failed start is a start that did not happen: count it as a failure,
   not as a show the audience saw. A show cut off by a restart did run,
   but not to its end: count it as cut short, never as finished. Either
   way the summary's table says FAULT.
4. Hold and Resume during a show pause the same show: never count them as
   a start or an end.
5. A `SHOW_FAILED` with `outcome` `recorded as a fault` came too late to
   be a failed start: the show kept running, and its end comes later.
6. Programming and rehearsal runs (Run in Programming, the Fireball
   practice, Play from in a Programming session) are not started by the
   scheduler: they never write a `fire` or `START_NOW` record and carry
   no show number. A counter that follows rule 1 never counts them. A
   "Start at a cue" from the Schedule tab IS a show: every play from a
   cue on the schedule counts as an extra show.
7. Show numbers above the night's list (13 and 14 below) are extra
   shows. A delayed show that never ran ends as `miss` `missed`.
8. If the summary says "Logging: INCOMPLETE" (and the upload says
   `"incomplete": true`), the count from the files may be short: compare
   with the summary's table of shows, and check the gap times it lists.

## Real examples

From one test night (Saturday 14 November 2026, Denver time, no
MadMapper attached, so the `START_SHOW` lines after each start say "Not
performed"; on the show PC they say what MadMapper did). On the show PC a
show's end and a failed start are reported by the show runner, so their
`actor` is `system`, as here.

### Scheduled start, and finished

```
18:00:00  Show 1 started on schedule at 18:00.
18:07:20  Show 1 finished at 18:07:20 (timecode reached the end).
```

```
{"id":"7cd4f519e51-8","at":"2026-11-14T18:00:00.000-07:00","night":"2026-11-14","state":"STANDBY","to_state":"SHOW","actor":"scheduler","action":"fire","outcome":"fired","reason":"FIRED","show":1,"screen":null,"who":null,"fault":false,"text":"Show 1 started on schedule at 18:00.","line":"18:00:00  Show 1 started on schedule at 18:00."}
{"id":"7cd4f519e51-11","at":"2026-11-14T18:07:20.000-07:00","night":"2026-11-14","state":"SHOW","to_state":"STANDBY","actor":"system","action":"SHOW_ENDED","outcome":"done","reason":"DONE (timecode reached the end)","show":1,"screen":null,"who":null,"fault":false,"text":"Show 1 finished at 18:07:20 (timecode reached the end).","line":"18:07:20  Show 1 finished at 18:07:20 (timecode reached the end)."}
```

### Operator Stop

```
18:22:00  Andy pressed Stop on the rack screen during show 2. Flame cues zeroed and lasers blanked; music, video and pixels fading to black over 1 s, then MadMapper stopped. Nothing is latched. The schedule is on Hold: no show starts by itself until Resume; a show whose time passes meanwhile is delayed and waits for Start now.
```

```
{"id":"7cd4f519e51-15","at":"2026-11-14T18:22:00.000-07:00","night":"2026-11-14","state":"SHOW","to_state":"HOLD","actor":"operator","action":"STOP_SHOW","outcome":"aborted","reason":"STOPPED (operator Stop)","show":2,"screen":"rack screen","who":"Andy","fault":false,"text":"Andy pressed Stop on the rack screen during show 2. Flame cues zeroed and lasers blanked; music, video and pixels fading to black over 1 s, then MadMapper stopped. Nothing is latched. The schedule is on Hold: no show starts by itself until Resume; a show whose time passes meanwhile is delayed and waits for Start now.","line":"18:22:00  Andy pressed Stop on the rack screen during show 2. Flame cues zeroed and lasers blanked; music, video and pixels fading to black over 1 s, then MadMapper stopped. Nothing is latched. The schedule is on Hold: no show starts by itself until Resume; a show whose time passes meanwhile is delayed and waits for Start now."}
```

The show's slot ends ABORTED, its reason "STOPPED (operator Stop)". The
Stop's Hold ends with Resume.

### Start now, then a failed start

```
18:26:00  Jeff pressed Start now on the Stream Deck. Started an extra show, 13, at 18:26. Show 3 at 18:40 stays where it is.
18:26:06  Show 13 did not start: no timecode came back within 10 s of the start. Reported by system. Slot marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped. No flame group is disarmed: every group stays as it was (Jeff, 2026-10-12), and the alert on the rack screen waits for an Acknowledge. Nothing is latched: Start now works at once, and the next show is still attempted.
```

```
{"id":"7cd4f519e51-24","at":"2026-11-14T18:26:00.000-07:00","night":"2026-11-14","state":"STANDBY","to_state":"SHOW","actor":"operator","action":"START_NOW","outcome":"fired","reason":"EXTRA SHOW (operator)","show":13,"screen":"Stream Deck","who":"Jeff","fault":false,"text":"Jeff pressed Start now on the Stream Deck. Started an extra show, 13, at 18:26. Show 3 at 18:40 stays where it is.","line":"18:26:00  Jeff pressed Start now on the Stream Deck. Started an extra show, 13, at 18:26. Show 3 at 18:40 stays where it is."}
{"id":"7cd4f519e51-26","at":"2026-11-14T18:26:06.000-07:00","night":"2026-11-14","state":"SHOW","to_state":"STANDBY","actor":"system","action":"SHOW_FAILED","outcome":"fault","reason":"FAULT (no timecode came back within 10 s of the start)","show":13,"screen":null,"who":null,"fault":true,"text":"Show 13 did not start: no timecode came back within 10 s of the start. Reported by system. Slot marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped. No flame group is disarmed: every group stays as it was (Jeff, 2026-10-12), and the alert on the rack screen waits for an Acknowledge. Nothing is latched: Start now works at once, and the next show is still attempted.","line":"18:26:06  Show 13 did not start: no timecode came back within 10 s of the start. Reported by system. Slot marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped. No flame group is disarmed: every group stays as it was (Jeff, 2026-10-12), and the alert on the rack screen waits for an Acknowledge. Nothing is latched: Start now works at once, and the next show is still attempted."}
```

### Start at a cue, Hold and Resume during it, finished

```
18:30:00  Andy pressed Start at a cue on the rack screen. Started an extra show, 14, from the cue Finale (00:05:10:00), at 18:30. Show 3 at 18:40 stays where it is.
18:32:00  Andy pressed Hold on the rack screen during show 14. The show is paused at its current frame: flame cues zeroed, lasers blanked, music fading out. Resume carries on from there.
18:33:00  Andy pressed Resume on the rack screen. Show 14 carries on from where it froze after 1m 0s paused; it now ends at 18:38:20.
18:40:00  Show 14 finished at 18:40 (timecode reached the end).
```

```
{"id":"7cd4f519e51-33","at":"2026-11-14T18:30:00.000-07:00","night":"2026-11-14","state":"STANDBY","to_state":"SHOW","actor":"operator","action":"START_NOW","outcome":"fired","reason":"EXTRA SHOW (operator)","show":14,"screen":"rack screen","who":"Andy","fault":false,"text":"Andy pressed Start at a cue on the rack screen. Started an extra show, 14, from the cue Finale (00:05:10:00), at 18:30. Show 3 at 18:40 stays where it is.","line":"18:30:00  Andy pressed Start at a cue on the rack screen. Started an extra show, 14, from the cue Finale (00:05:10:00), at 18:30. Show 3 at 18:40 stays where it is."}
{"id":"7cd4f519e51-35","at":"2026-11-14T18:32:00.000-07:00","night":"2026-11-14","state":"SHOW","to_state":"PAUSED","actor":"operator","action":"HOLD","outcome":"paused","reason":"PAUSED (operator hold)","show":14,"screen":"rack screen","who":"Andy","fault":false,"text":"Andy pressed Hold on the rack screen during show 14. The show is paused at its current frame: flame cues zeroed, lasers blanked, music fading out. Resume carries on from there.","line":"18:32:00  Andy pressed Hold on the rack screen during show 14. The show is paused at its current frame: flame cues zeroed, lasers blanked, music fading out. Resume carries on from there."}
{"id":"7cd4f519e51-40","at":"2026-11-14T18:33:00.000-07:00","night":"2026-11-14","state":"PAUSED","to_state":"SHOW","actor":"operator","action":"RESUME","outcome":"resumed","reason":"RESUMED (paused 1m 0s)","show":14,"screen":"rack screen","who":"Andy","fault":false,"text":"Andy pressed Resume on the rack screen. Show 14 carries on from where it froze after 1m 0s paused; it now ends at 18:38:20.","line":"18:33:00  Andy pressed Resume on the rack screen. Show 14 carries on from where it froze after 1m 0s paused; it now ends at 18:38:20."}
{"id":"7cd4f519e51-44","at":"2026-11-14T18:40:00.000-07:00","night":"2026-11-14","state":"SHOW","to_state":"STANDBY","actor":"system","action":"SHOW_ENDED","outcome":"done","reason":"DONE (timecode reached the end)","show":14,"screen":null,"who":null,"fault":false,"text":"Show 14 finished at 18:40 (timecode reached the end).","line":"18:40:00  Show 14 finished at 18:40 (timecode reached the end)."}
```

### Cut off by a restart

ltcplay stopped without warning at 19:00 while show 4 ran, and started
again at 19:03. First the program start line (its build words differ on
every build), then the show's end:

```
19:03:00  ltcplay started again (unreleased, build 9700051cf6 (112 files, newest 2026-11-12 01:42)). Its last line before this was at 19:00:00, 3m 0s earlier, and it did not say it was stopping, so it stopped without warning: a crash, a power cut or someone ending it by force.
```

```
19:03:00  ltcplay restarted during show 4. The show is over and marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped.
```

```
{"id":"7cd4f519e52-2","at":"2026-11-14T19:03:00.000-07:00","night":"2026-11-14","state":"BOOT","to_state":"STANDBY","actor":"system","action":"BOOT_DONE","outcome":"fault","reason":"FAULT (ltcplay restarted during the show)","show":4,"screen":null,"who":null,"fault":true,"text":"ltcplay restarted during show 4. The show is over and marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped.","line":"19:03:00  ltcplay restarted during show 4. The show is over and marked FAULT; flame cues zeroed, lasers blanked, the show faded to black and MadMapper stopped."}
```

The `program start` record has `"outcome":"restarted without warning"`
(or `"restarted after a clean stop"`) and `"restart":true` in its `data`.

### A Hold between shows, then Start now of the delayed show

```
19:40:01  Show 6 at 19:40 is delayed by the Hold. It waits, and starts only when someone presses Start now.
19:45:00  Andy pressed Start now on the rack screen. Started the delayed show 6, planned for 19:40, at 19:45.
```

```
{"id":"7cd4f519e52-17","at":"2026-11-14T19:40:01.000-07:00","night":"2026-11-14","state":"HOLD","to_state":"HOLD","actor":"scheduler","action":"delay","outcome":"delayed","reason":"DELAYED (on hold)","show":6,"screen":null,"who":null,"fault":false,"text":"Show 6 at 19:40 is delayed by the Hold. It waits, and starts only when someone presses Start now.","line":"19:40:01  Show 6 at 19:40 is delayed by the Hold. It waits, and starts only when someone presses Start now."}
{"id":"7cd4f519e52-18","at":"2026-11-14T19:45:00.000-07:00","night":"2026-11-14","state":"HOLD","to_state":"SHOW","actor":"operator","action":"START_NOW","outcome":"fired","reason":"DELAYED START (operator hold)","show":6,"screen":"rack screen","who":"Andy","fault":false,"text":"Andy pressed Start now on the rack screen. Started the delayed show 6, planned for 19:40, at 19:45.","line":"19:45:00  Andy pressed Start now on the rack screen. Started the delayed show 6, planned for 19:40, at 19:45."}
```


## The logging check

The morning summary (`.summary.md`) says "Logging: complete" only when
the journal file and the machine log have the same number of lines, every
line ltcplay made that night is in them, and there is no gap: no line for
more than 10 minutes while a show ran, and no show start or end without
its line. Otherwise it says "Logging: INCOMPLETE" with the gap times. The
upload sends every closed night either way, with `"incomplete"` and the
gap list in its `X-LTC-Meta` header.
