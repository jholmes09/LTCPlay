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
   tilt values, a placeholder settle time). A Fireball is never purged
   (below). Every value is in `ltcplay_morning.json` (`purge`).
3. **Groups**, with Jeff's exact names: Ground G-Flames, Scaffold G-Flames,
   Scaffold Wavers, Fireball. A group button is a fast multi-select
   (nothing saved); every machine can also be clicked on its own; any
   collection can be armed.
4. **Layout**: the machines drawn labelled in a top-down positional view.
   The machine list comes from `ltcplay_morning.json` (or, when it names
   none, one machine per fire slot of flamesafe's own config), positions
   from `ltcplay_morning_layout.json`, editable in the page (Arrange, drag,
   Save). A sensible default is laid out when there is no layout file.
   While any screen or window is in Arrange mode, nothing fires from any
   surface, the Stream Deck's FIRE key included, and a fire press already
   going stops at once; the refusal names the screen that is arranging. A
   page that closed without saving lets go by itself within 2 s. Abort,
   Hold, Stop, disarm, letting go of a fire key, a lockout, a laser test
   going off and Acknowledge are never held back by Arrange mode.
5. **Lockout**: any single flame machine or laser can be locked out until
   it is unlocked, across restarts. A locked unit cannot be armed or fired
   from any surface: this page, the deck, show cues, the remote. Enforced
   at flamesafe for flames (it is the only writer of the flame universe)
   and in the laser output path for lasers. A lock icon on the machine, a
   banner on every screen, a journal line on lock and unlock. On the rack
   screen one tap on the L button locks or unlocks, with no pop-up (Jeff,
   2026-10-26), and an Undo for 5 s; the engine still requires an unlock
   to say it is confirmed, and the tap is that confirm.
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
| flamesafe's lockout file | `flamesafe.json` | `lockout_file` (left out: `ltcplay_lockout.json` beside flamesafe.json, which on the show PC is the engine's file; `false` switches it off) |

### The fireball (Jeff, 2026-10-26)

A fireball has four channels in universe 300: Fuel Charge 55, Air Charge
56, Valve Open 57, Ignite 58, and its safety on 99. In flamesafe it is one
more group, with one safety slot like a G-Flame group; its arm value comes
from a `safety_profile` Jeff gives it:

```json
{"name": "fireball", "safety": 99, "fire": [55, 56, 57, 58]}
```

On this page it is one machine of kind `fireball` in that group. It names
no `safety` (the group has one safety slot, so locking the machine locks
its four fire slots and flamesafe holds the group disarmed):

```json
{"id": "fireball", "name": "Fireball", "kind": "fireball", "group": "fireball", "fire": [55, 56, 57, 58]}
```

With no `machines` list the page builds exactly that one machine from
flamesafe's group (one machine per fireball, never one per channel).

**The rule is by flamesafe group, not by machine.** A fireball group is
one named in `fireball_groups` in `ltcplay_morning.json` (for a group
whose name does not say so: `"fireball_groups": ["Big Boom"]`), one whose
name says fireball or ball, or one with any machine of kind `fireball`.
Every machine in a fireball group has to say `"kind": "fireball"`: a
machine there with the kind left out (a G-Flame by default) or of another
kind is refused when the file is read, in a sentence.

**A fireball is never purged.** Its channels are a sequence (charge, then
open, then ignite), and the purge writes one value to every slot at once,
which is not a valid sequence and could be dangerous. The tile says "Never
purged: a fireball has no purge sequence yet"; a click on any machine of a
fireball group is refused and arms nothing; the group is never asked of
the Stream Deck; a fire press never writes a value to its slots. No setting turns
this on: a fireball purge needs its own design and safety review first.
`purge.fireball` in `ltcplay_morning.json` is still accepted, so an older
file loads, and it is not used.

The Stream Deck (a Mini: six keys, Start, Hold and Abort on top, three
arm keys below) has no free key, so only three flame groups have a key.
Which three, in which order, and what each key says come from
`ltcplay_deck.json` in the settings folder (optional):

```json
{"groups": [{"group": "front row", "label": "Floor G"}, {"group": "cat-walk", "label": "Cat-Walk G"}, {"group": "wave flamer", "label": "Blue Flames"}, {"group": "fireball", "label": "Fireball"}]}
```

That is also the default with no file: Floor G, Cat-Walk G and Blue Flames
on the keys left to right, the fireball after them. The order is this
list's, never flamesafe's file order; a label is up to 12 letters, and a
group with none says its own name. A group past the third has no key: it
is armed only by holding its Hold to arm button on the Rack screen
(Arming from a screen on): a purge click never arms it, and its tile says
so. It is disarmed there, by Disarm all flame groups, or by any Abort, the
deck's included. At start the deck journals its key map and which group
has no key. If anything in `ltcplay_deck.json` is wrong (a misspelt or
missing group, a group twice, a label over 12 letters, or a label that
reads like another group's name or label) the whole file is set aside for
the default layout, so a typo never moves a key; the journal says why and
the Rack screen's Deck chip reads "default keys". With no file, and
flamesafe's groups in another order than the default, the journal says
the keys follow the default order. It never refuses to start over the
layout.

**Show cues and the safety slot.** The show's flame values reach flamesafe
on the flame link exactly as for the other groups: the flame controller's
channels 1 to 512 from the FSEQ are slots 1 to 512, so 55 to 58 are carried
when the flame controller in `xlights_networks.xml` has at least 58
channels. flamesafe writes slot 99 itself, from its arm state only: a value
the FSEQ or xLights has on channel 99 is never sent (CONTRACT.md, "What the
wire carries"). The flame controller's span must still reach 99: `ltc serve`
refuses at start a group whose safety or fire slot is past it.

A flame group with a safety slot for each fixture (flamesafe multi-safety, the wave flamer) has one machine per fixture, and locking one locks that fixture's safety slot with its fire slots. With no `machines` list the pairing comes from the slot layout, never the list order: a fixture's fire slots are the ones directly below its safety slot (channels 1 to 5 fire, channel 6 safety). If the layout does not say, the whole group is one machine and locking it locks every slot. In a `machines` list each machine of such a group must name its own `safety`, and its slots must be on that fixture.

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
heartbeat must belong to the device that made the selection (or to the
Stream Deck process, which names itself in `screen` over loopback and may
fire any device's selection), and a gap of more than 0.25 s ends that
press (the next heartbeat is refused: release and press again). Only
machines whose group flamesafe reports ARMED get a value; locked machines
never do; a Waver's head-up time counts from the first beat on which it is
both selected and armed, so a group that arms late in a press waits its
full settle time; a machine cannot be added to a press that is firing.
A heartbeat fires only when its body carries `"on": true` (the JSON word
true, nothing else). Every other `purge-fire` body (`"on"` false, missing,
null, 0, `"false"`) is a release: the engine turns it into the same stop
as `purge-stop` (same owner rule), it never reaches the fire path, it needs
no purge arming switch and no sign-in, and it can never start a press.
While this engine has any machine locked out, no purge goes until
flamesafe's status reports exactly the slots asked for. A stop that lands
while a beat's frame is being worked out wins: that beat sets nothing.

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

### Lasers: arm and disarm (Jeff, 2026-10-22)

`laserarm.py` holds each laser's ARMED or DISARMED state, in memory only:
every engine start and restart is DISARMED, and BEYOND's
`/beyond/general/DisableLaserOutput` is sent at once.

- **Arm**: the Arm button on a laser, or a laser group's button (a fast
  multi-select, nothing saved; `laser_groups` in `ltcplay_morning.json`,
  a PLACEHOLDER of one group "All lasers" until Jeff and Andy name them),
  then Arm in the confirm box. All or none. Refused, with the reason, when
  the lasers' blanking is not set up ("Lasers: blanking not set up": no
  `beyond` block with its host and port), when the laser is locked out,
  while the Abort latch stands, while the show is on Hold, while the night
  is closing, and while this page's settings or lockout cannot be
  trusted. It is the role matrix's "arm" row (as for arming flames), and
  from the show network it needs `screen_arming`, as a laser test ON does.
  Held back from a screen in Arrange mode.
- **Disarm**: the Disarm button on a laser or group, or **Disarm all
  lasers**. Never refused, never confirmed, from every surface, signed in
  or not. Every disarm sends the blank (brightness 0) and
  DisableLaserOutput.
- **BEYOND's output switch is global.** The only laser output commands
  this repo has verified are `/beyond/general/EnableLaserOutput` and
  `/beyond/general/DisableLaserOutput` (no argument; bench B8.3: both
  accepted by BEYOND 5.5, from Pangolin's OSC list; no laser hardware was
  attached, so what they do to a projector was not seen). They switch every
  projector at once. So EnableLaserOutput, and the show's unblank, go out
  only when EVERY laser is armed and none is locked out. With one laser
  disarmed and another armed, LTC Player keeps every laser dark: the
  global command cannot leave one projector off and light another. A
  laser's own calibration test needs that laser armed.
- **When EnableLaserOutput is sent**: once when the last laser needed is
  armed (output on, still blanked), and again at every show start and
  Resume right before the unblank (through the conductor's laser gate).
- **Abort and Hold** disarm every laser at the press, on every surface,
  before anything else is asked; the blank and DisableLaserOutput go on
  the arm's own sender thread, retried, so the press never waits on
  BEYOND. Abort's own blank at the press stays the fast path. Resume and
  Reset never re-arm: the lasers stay dark until someone arms them again,
  and arming during a show that is already up lights them through the
  laser gate.
- **Announcements** disarm every laser the way a Hold does (blank,
  DisableLaserOutput, arm again by hand afterwards). Jeff has not ruled on
  this; it is the safe default and can be reverted.
- **Arming never races a disarm**: an arm whose checks were overtaken by a
  disarm (an Abort, a Hold, a disarm press) writes nothing, and while an
  Abort or Hold press is being handled no laser arms at all.
- **Closing**: no laser stays armed while the scheduler is CLOSING, and the
  night being over (OFF) disarms on the way in, even when a dry-run
  scheduler went from CLOSING to OFF in one step; arming in OFF (the next
  morning) works again afterwards. The engine stopping disarms too, and so
  does a blocker that appears while every laser is armed (a lockout or
  settings problem, the blanking lost).
- **Startup**: `ltc serve` blanks the lasers and sends DisableLaserOutput
  itself at start, before the Morning page is built, so a page that fails
  to build cannot leave BEYOND's output on.
- **Order**: a disarm's blank and DisableLaserOutput go first, before any
  journal line, audit event, test-off or placeholder message. A laser's own
  OSC lists (`on`, `off`, `lock`, `arm`, `disarm`) can never name BEYOND's
  global brightness or output switch.
- **Per laser OSC**: each laser may carry `arm` and `disarm` message lists,
  PLACEHOLDERS, empty by default (nothing is sent). No per-projector or
  per-zone BEYOND address is verified anywhere; when Andy finds one that
  works on the rig, it goes there, and it never loosens the global rule.
- **The record**: every arm, refusal and disarm is a journal line with who
  and which surface, and an audit event; every Enable and Disable sent is
  an audit event (system) and a journal line from beyond.py, a failed one a
  fault.

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
| T3 | Key repeat, a lost keyup, a second tab or device | the page sends heartbeats on its own 100 ms timer from one keydown and stops on keyup, blur, visibilitychange and pagehide; the heartbeats are not the key: the browser's key repeat events are the evidence the key is still down, and a press older than 2.5 s with no repeat event for 1 s ends (`fireKeyLapsed`; so the OS key repeat has to be on; the page says so). A physically stuck key still repeats, so the PAGE is not a dead-man switch; the Stream Deck's FIRE key is (its heartbeats stop the instant the key reads up). The engine binds a fire press to a `fire_id` and the device that owns the selection; a heartbeat from another device, or for an old `fire_id`, is refused and does not end the owner's press; the deck process is known by its `screen` over loopback, never by an operator name; a machine never joins a press that is already firing |
| T4 | A machine selected for purge is left armed after the page is gone | the selection lives only while the OWNER device's `morning-open` beat (every 500 ms) is under 2 s old (another device's open page does not keep it); leaving the page posts `purge-clear` at once; the deck disarms every purge group it armed the moment the engine stops listing it, and 0.3 s after the engine stops answering |
| T5 | A locked machine fires from a show cue or a purge | flamesafe zeroes locked slots on every packet (the only writer); the flame link zeroes them too; `purge-select` refuses a locked machine and a lock drops it from a live selection; while this engine has any machine locked, NO purge goes until flamesafe's status reports exactly the slots this engine asked for (an old flamesafe, one without a lockout file, or a stale status all refuse) |
| T6 | The lockout file is deleted, corrupted or half-written; the lock table and the config disagree | atomic replace by one writer; flamesafe keeps the last set on an unreadable file, and on a file that held locks and disappeared (the engine never deletes it), and journals both; `lockout_file` defaults to the file beside flamesafe's own config, so the show PC enforces it with nothing set up; on the engine's side a lockout file that could not be read, or one naming a machine `ltcplay_morning.json` no longer has, is never written over (every lock and unlock is refused, the purge and the laser tests too) and is said on every screen until fixed; the engine's view and flamesafe's `lockout` field are compared both ways on the page and in the journal: slots flamesafe holds that this engine cannot account for (the file deleted and kept by flamesafe, an engine restarted without it), a config edited under a lock, flamesafe with no lockout file, or one reading a different path than the engine writes (said at start, both paths named) all refuse every lock, unlock, purge and laser test; the one way out is a confirmed "Rewrite the lockout file" on the show machine itself, journaled as a fault, which writes exactly this engine's table |
| T7 | Abort during a purge | the engine's Abort clears the purge and sends `disarm_all` before anything else, as today; `disarm_all()` and `zero()` clear the purge frame; the deck's own Abort sends every group false on the arm link; the laser tests are turned off |
| T8 | The deck arms a purge group while latched, in its refractory window, or with no operator chosen | the deck's purge pass arms through `_do_arm_fire` itself, the code a completed hold goes through: the latch, the re-arm refractory window and the operator gate are checked at the moment of arming; only the hold is waived (Jeff). A latched rig also reads as no groups wanted at all (`purge_wanted`). The test holds that no arm value goes out on the arm link even for one pass |
| T9 | A network device purges, unlocks or lights a laser without anyone meaning it to | purge from the network, an unlock and a laser test ON all need `screen_arming` true (the existing switch, off by default); a lock, which only lowers risk, is open to every device; every press is journaled with the device address; the same proxy, Host and cross-site refusals as every other route |
| T10 | A laser test lights during a show, or on a locked laser | the engine refuses the toggle while a show is live or latched; the watcher turns tests off when a show goes live; the sender refuses a locked laser; only configured addresses with no OSC wildcards, never BEYOND's BlackOut or MasterPause |
| T11 | The deck stays re-mapped after the page closes (FIRE where START was) | the deck's morning mode follows the engine's `morning_open` flag, which the engine clears 2 s after the last page beat from a device that may purge (a view-only device's beats do not count); a stale `deck-input` (0.3 s) also ends it |
| T12 | A Waver fires before its head is up | a fire press on a Waver sends pan and tilt first and the fire value only after `head_settle_s` (0.5 s at least) of continuous heartbeats, counted per Waver from the first beat on which it is both selected and armed, and counted again from nothing whenever its group drops out of ARMED mid press (the head has fallen); the head values go in the same frame as the fire value thereafter; a machine cannot join a press that is firing |
| T13 | The purge frame reaches a fire slot flamesafe has not armed | flamesafe sends a fire value only while that group's safety slot carries the arm value on the same packet (unchanged) |
| T16 | A purge writes one value to every channel of a fireball (fuel, air, valve and ignite at once) | the rule is by flamesafe group (`MorningConfig.purge_refusal`, `fireball_groups`): a machine of any kind in a fireball group is refused by `purge-select` and left out of `groups_wanted` and the frame, and `ltcplay_morning.json` cannot put a machine of another kind in one; the Stream Deck never arms a group with no key for a purge click |
| T14 | A checklist record is altered after the fact | every record carries the hash of the one before it; `verify` fails on any edit, insertion or deletion; the per-day PDF prints the verification result |
| T15 | A laser lights while disarmed or locked out, or after an Abort or Hold | default disarmed at every start (DisableLaserOutput sent); EnableLaserOutput and the unblank only with every laser armed and none locked out (the laser gate, and again before every unblank packet); Abort and Hold disarm at the press on every surface and the conductor; Resume and Reset never re-arm; the night closing and the engine stopping disarm; a disarm cuts an enable or a restore short; the allow-list in beyond.py holds exactly brightness 0/100 and the two output switches with no argument |

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

## How it is tested, and what the tests do not prove

- **The engine's rules** (purge, lockout, laser tests, checklists) are
  tested end to end over the real HTTP routes in `selftest.py`
  (`test_purge_rules_at_the_engine`, `test_morning_lockout_trust_link_and_close`
  and the other Morning tests), flamesafe's lockout in its own suite
  (`flamesafe/test_flamesafe.py`), and one mutation per threat in the table
  above in `mutate.py`. `morning.py` and `lasertest.py` are safety trigger
  files: every flamesafe, conductor and flame link mutation runs when they
  change.
- **The page's JavaScript.** Its pure helpers (`machineLook`,
  `fireKeyLapsed`, between the page's LOGIC markers) run under node in the
  selftest, so their decisions are tested as code; a CI runner without
  node fails that check rather than passing by reading, and `mutate.py`
  has an entry that makes `fireKeyLapsed` never end a press (T3). The event wiring (one
  keydown starts the heartbeats; keyup, blur, visibilitychange, pagehide and
  leaving the page stop them; `purge-clear` goes when the page is left) is
  checked by STRING PRESENCE in `remote.html` only: the selftest has no
  browser, so that wiring is proven present, not proven to behave. The
  engine and the flame link do not rely on it: both let go 0.25 s after the
  last heartbeat on their own clocks, and flamesafe's `fire_hold_ms` stands
  behind them.
- **A physically stuck Space bar** keeps repeating, so the page cannot tell
  it from a held one. The page is NOT a dead-man switch; the Stream Deck's
  FIRE key is (its heartbeats stop the instant the key reads up), and the
  deck's purge arming is tested on the arm link's wire.
- **The Windows installer.** `pdfmini` (the checklist PDF writer),
  `checklist`, `morning` and `lasertest` are plain modules of the `ltcplay`
  package with no library beyond the standard one, so PyInstaller's
  `collect_submodules("ltcplay")` in `packaging/windows/ltcplay.spec`
  bundles them with everything else. `ltcplay.exe --self-check` imports
  them and writes a one page PDF, and the installer smoke test
  (`packaging/windows/ci_smoke.py`, run on every bench-build push) checks
  that line from the installed program.

## What Jeff still has to decide or supply

- The laser mechanism and each laser's addresses or cues, and the low
  power level (`lasers` in `ltcplay_morning.json`).
- The real machine list and channel map (Andy), the Waver head-up pan and
  tilt values and settle time (`machines`, `purge`).
- The fireball's arm value (its `safety_profile` in flamesafe's config),
  and whether a fireball should ever be purged, and if so its sequence.
- Whether a purge should be allowed from an iPad on the show network
  (today: only with `screen_arming` true).
- Andy's checklist steps and the list of names (`checklists`, `names`).
- The tally server's address and token (`tally`), and whether the payload
  below fits it.
