# Morning and Shutdown: design note and threat analysis

Jeff's rulings of 2026-10-17 (RULINGS 2026-10-17 in the decisions file), built
as one page in the rack screen web interface. Written before the code, and
kept with it. Read this with flamesafe/CONTRACT.md: nothing here loosens a
rule written there.

## What the page does

1. **Laser calibration test**, one toggle per laser (4). ON keeps the
   alignment grid on at low power until toggled off. The mechanism (direct
   BEYOND OSC or a timeline cue), each laser's address or cue and the low
   power level are PLACEHOLDERS in `ltcplay_morning.json` (`lasers`). A
   laser whose mechanism is `"none"` (the shipped default) refuses the
   test in words and sends nothing. A test turns off when a show starts,
   when Abort is pressed, when the laser is locked out, and when the
   engine stops. A locked-out laser's test is refused.
2. **Purge**, momentary. Click a machine to arm it; it fires ONLY while the
   Space bar (the page) or the Stream Deck's FIRE key is held. Release, tab
   blur, window focus loss, the page hidden, a heartbeat gap, a connection
   drop or any error stops the fire within 0.25 s; flamesafe's own
   `fire_hold_ms` and `frame_stale_ms` cutoffs stand behind that. No time
   limit on a purge (Jeff). Fire is the same fire value a G-Flame cue
   carries; a Waver's head goes straight up first (placeholder pan and
   tilt values, a placeholder settle time); the Fireball has placeholder
   values. Every value is in `ltcplay_morning.json` (`purge`).
3. **Groups**, with Jeff's exact names: Ground G-Flames, Scaffold G-Flames,
   Scaffold Wavers, Fireball. A group button is a fast multi-select
   (nothing saved); every machine can also be clicked on its own; any
   collection can be armed.
4. **Layout**: the machines drawn labelled in a top-down positional view.
   The machine list comes from `ltcplay_morning.json` (or, when it names
   none, one machine per fire slot of flamesafe's own config), positions
   from `ltcplay_morning_layout.json`, editable in the page (Arrange, drag,
   Save). A sensible default is laid out when there is no layout file.
5. **Lockout**: any single flame machine or laser can be locked out until
   it is unlocked, across restarts. A locked unit cannot be armed or fired
   from any surface: this page, the deck, show cues, the remote. Enforced
   at flamesafe for flames (it is the only writer of the flame universe)
   and in the laser output path for lasers. A lock icon on the machine, a
   banner on every screen, a journal line on lock and unlock; unlock needs
   one deliberate confirm.
6. **Stream Deck**: while this page is open the deck re-maps itself (the
   group keys show PURGE ARMED / OFF / LOCKED, the Start key becomes the
   FIRE key) and restores when the page is left or the engine stops saying
   it is open.
7. **Checklists**: a Morning and a Shutdown list, steps editable in
   `ltcplay_morning.json` (`checklists`), ticks reset each night, every tick
   recorded for audit (below).

## Where each value lives

| Thing | File | Key |
|---|---|---|
| Machines, kinds, channels, groups | `ltcplay_morning.json` | `machines`, `groups` |
| Purge values (G-Flame fire, Waver pan/tilt up and settle, Fireball) | `ltcplay_morning.json` | `purge` |
| Lasers (mechanism, on, off and lock messages, low power) | `ltcplay_morning.json` | `lasers` |
| Checklist steps | `ltcplay_morning.json` | `checklists` |
| Names allowed to tick | `ltcplay_morning.json` | `names` |
| Tally server (off by default) | `ltcplay_morning.json` | `tally` |
| Machine positions | `ltcplay_morning_layout.json` | (the page writes it) |
| Lock state | `ltcplay_lockout.json` | (the engine writes it; flamesafe reads it) |
| Purge arming from a network device | `ltcplay_remote.json` | `screen_arming` (the existing switch) |
| Purge arming at all | `ltcplay_remote.json` | `purge_arming` (default true) |
| flamesafe's lockout file | `flamesafe.json` | `lockout_file` |

All of these live in the machine's settings folder (`%LOCALAPPDATA%\ltcplay`
on the show PC). `ltcplay/ltcplay_morning.example.json` is the annotated
example. Nothing here is in the show file.

## How it works

### Arming for a purge: the deck still owns the arm link

flamesafe has one arm sender, the Stream Deck process, and that does not
change. A click on a machine asks the ENGINE (`/api/remote/purge-select`)
to add it to the purge selection. The engine publishes the selection's
groups on `deck-input` (`purge.wanted_groups`), and the deck process sets
those groups' `wanted` bits on its own arm socket, exactly as it would for
a completed hold, with its own refractory window and latched refusal still
applied. flamesafe then runs every consent rule it has. This is the
Jeff-approved exception: no hold-to-arm for a purge, a click instead. The
deck disarms a group the moment the engine stops listing it: the machine is
deselected, the page stops saying it is open (2 s), the engine is
unreachable (0.3 s), a show goes live, an Abort lands.

### Firing: the flame link carries a purge frame, the same way it carries cues

`FlameLink.purge_set(values)` holds a 512 channel frame for at most
`PURGE_HOLD_S` (0.25 s) on the link's own clock. `values_now()` sends it
only while the show is NOT live (no show cue playing); while a show is live
the cue path is the only source of values and the purge frame is ignored.
`zero()` and `disarm_all()` clear it. Every frame the link sends still
passes flamesafe, which sends fire values only for armed groups, zero for
locked slots, and zeros everything `fire_hold_ms` after the last frame.

The engine's `purge-fire` heartbeat (every 100 ms from the page while Space
is down, or from the deck while FIRE is held) re-asserts the frame: the
page's status must be under 1 s old, flamesafe's status under 1 s old, the
heartbeat must belong to the device that made the selection, and a gap of
more than 0.25 s ends that press (the next heartbeat is refused: release
and press again). Only machines whose group flamesafe reports ARMED get a
value; locked machines never do.

### Lockout: one writer, enforced where the wire is written

The engine (`ltc serve`) is the only writer of `ltcplay_lockout.json`,
atomically (write, fsync, replace). flamesafe reads it (its config's
`lockout_file`), polls its mtime twice a second, and:

- forces every locked fire slot to zero on every packet, armed or not;
- refuses to arm a group every one of whose fire slots is locked (held,
  reason "every machine in this group is locked out");
- keeps the LAST lock set when the file cannot be read or parsed, and
  journals it once; a file that is missing reads as no locks (a lock set
  that disappears is journaled).

The status frame gains `lockout` (the slots flamesafe is enforcing), so
the rack screen shows whether flamesafe has taken the lock. ltcplay's flame
link also zeroes locked channels in every frame it sends (belt), so show
cues never command fire on a locked machine; flamesafe is the braces.

Lasers: ltcplay never writes a laser's output; BEYOND does. The only per
laser path ltcplay has is the calibration test. A locked laser's test is
refused at the engine and again at the sender (`lasertest.LaserTests`
refuses an ON for a locked id, and sends the laser's `lock` messages, a
placeholder, when it is locked and at every show start while locked). A
per laser show lockout inside BEYOND needs Jeff and Andy to say what BEYOND
accepts; until then the `lock` list is empty and the lock is visible on
every screen and in the journal.

### Checklists: append-only, hash-chained, exported

Every tick, untick and note is one record in
`<logs>/checklists/records.jsonl`: UTC and local time, show date, checklist
id, step id, the step's text at the time, state, who (a name from the
configured list, required), the device and address the engine saw, seq,
`prev` (the previous record's hash) and `hash` (SHA-256 over `prev` plus
the record's canonical JSON). Nothing is edited in place; a correction is
a new record. `ltc checklist-verify` and the page's Verify both walk the
chain. A completed routine writes a per-day record (`records/<date>/
checklist_<id>_<date>.json` and `.pdf`, pure Python PDF, page numbers,
the verification result), kept out of log pruning (`journal.prune` only
touches `nights/`) and out of the diagnostics bundle except as a count.
Export all: one zip of every day's PDF, a combined PDF with a table of
contents, and `VERIFY.txt` for the whole chain; a date range is a query.

The tally forwarder is OFF until `tally.enabled` is true and `tally.url`
is an https URL; the bearer token is read from `tally.token_file` at send
time and never shown or logged. Each record and each completed routine is
one POST (`{"v": 1, "kind": "checklist_record" | "checklist_routine",
"sent_at": ..., "record"|"routine": {...}}`, JSON). Undelivered payloads
wait on disk (`<logs>/checklists/outbox/`) and are retried with backoff;
the page shows queued, sent and the last error.

## Threat analysis: what could fire flames unintentionally, or fail to stop

| # | Threat | Where it is stopped |
|---|---|---|
| T1 | A purge frame goes out while a show cue is playing (two sources of fire values) | `FlameLink.values_now`: purge is read only when `live is not True`; the engine refuses `purge-select` and `purge-fire` while the scheduler is in SHOW or PAUSED, while an Abort is latched, or while the show clock plays a cue; the watcher clears the selection when a show goes live |
| T2 | The page dies with Space down (crash, sleep, cable) | the heartbeat stops; `FlameLink` drops the purge frame 0.25 s after the last `purge_set` on its own clock; flamesafe zeroes `fire_hold_ms` after ltcplay's last frame if the engine dies too |
| T3 | Key repeat, a stuck key, a second tab | the page sends heartbeats on its own 100 ms timer from one keydown, ignores repeats, stops on keyup, blur, visibilitychange and pagehide; the engine binds a fire press to a `fire_id` and the selection's device; a heartbeat from another device, or for an old `fire_id`, is refused |
| T4 | A machine selected for purge is left armed after the page is gone | the selection lives only while the page's `morning-open` heartbeat is under 2 s old; the deck disarms every purge group it armed the moment the engine stops listing it, and 0.3 s after the engine stops answering |
| T5 | A locked machine fires from a show cue or a purge | flamesafe zeroes locked slots on every packet (the only writer); the flame link zeroes them too; `purge-select` refuses a locked machine and a lock drops it from a live selection |
| T6 | The lockout file is deleted, corrupted or half-written | atomic replace by one writer; flamesafe keeps the last set on an unreadable file and journals it; the engine's view and flamesafe's `lockout` field are compared on the page (a banner says "not yet confirmed by flamesafe") |
| T7 | Abort during a purge | the engine's Abort clears the purge and sends `disarm_all` before anything else, as today; `disarm_all()` and `zero()` clear the purge frame; the deck's own Abort sends every group false on the arm link; the laser tests are turned off |
| T8 | The deck arms a purge group while latched or in its refractory window | the deck's purge pass re-uses `_do_arm_fire`'s guards: latched and refractory are re-checked at the moment of arming |
| T9 | A network device purges without anyone meaning it to | purge from the network needs `screen_arming` true (the existing switch, off by default); every press is journaled with the device address; the same proxy, Host and cross-site refusals as every other route |
| T10 | A laser test lights during a show, or on a locked laser | the engine refuses the toggle while a show is live or latched; the watcher turns tests off when a show goes live; the sender refuses a locked laser; only configured addresses with no OSC wildcards, never BEYOND's BlackOut or MasterPause |
| T11 | The deck stays re-mapped after the page closes (FIRE where START was) | the deck's morning mode follows the engine's `morning_open` flag, which the engine clears 2 s after the last page heartbeat; a stale `deck-input` (0.3 s) also ends it |
| T12 | A Waver fires before its head is up | a fire press on a Waver sends pan and tilt first and the fire value only after `head_settle_s` of continuous heartbeats; the head values go in the same frame as the fire value thereafter |
| T13 | The purge frame reaches a fire slot flamesafe has not armed | flamesafe sends a fire value only while that group's safety slot carries the arm value on the same packet (unchanged) |
| T14 | A checklist record is altered after the fact | every record carries the hash of the one before it; `verify` fails on any edit, insertion or deletion; the per-day PDF prints the verification result |

Tier A guarantees that do not change: Abort wins over everything, flamesafe
is the only writer of the flame universe, the link key and sender lock,
`frame_stale_ms` disarms, one arm sender (the deck), consent needs a
genuine off-then-on from that sender, no re-arm without a deliberate
action. Hold-to-arm is set aside for the purge page only, by Jeff's ruling,
and only for the purge selection: the show's arm buttons on the show screen
still hold.

## Today's rules, investigated

- **Firing during a show or a Hold.** Today ltcplay sends non-zero flame
  values only while a show cue is live and its timecode is moving
  (`flamelink.values_now`). A Hold is not live, so flames are zero through
  a Hold; the conductor also zeroes the cues at Hold. The purge keeps that:
  it is refused and cleared whenever the scheduler is in SHOW or PAUSED, an
  Abort is latched, or the show clock plays a cue. A purge between shows
  (PRESHOW, INTERMISSION, STANDBY) is allowed, which is when Andy needs it.
- **Arming from a screen.** Off by default (`screen_arming`, review of PR
  #43). The purge's arming from the Rack screen itself is on by default
  (`purge_arming`, this page's purpose); from a network device it also
  needs `screen_arming`.

## What Jeff still has to decide or supply

- The laser mechanism and each laser's addresses or cues, and the low
  power level (`lasers` in `ltcplay_morning.json`).
- The real machine list and channel map (Andy), the Waver head-up pan and
  tilt values and settle time, the Fireball values (`machines`, `purge`).
- Whether a purge should be allowed from an iPad on the show network
  (today: only with `screen_arming` true).
- Andy's checklist steps and the list of names (`checklists`, `names`).
- The tally server's address and token (`tally`), and whether the payload
  below fits it.
