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
   collection can be armed. A group click arms every member that can take
   it and names each one it skipped and why (locked out, never purged, any
   other rule); it never stops at the first one refused. The button reads
   in words: green only when every member is selected and flamesafe
   reports the group armed; amber for part of it ("5 of 6 selected;
   flamesafe armed; front-row-4 locked out") or while flamesafe has not
   armed it yet, or while flamesafe still reports it armed after it was
   taken off here ("flamesafe still armed front row"); grey when none is
   selected and flamesafe holds none of it. The words sit on one line
   under the name, cut short with the whole in the tooltip, so they never
   move the field or the message line. While any member is
   selected, the next click takes the whole group off the purge, and
   nothing holds that back: not a locked or refused member, not Arrange
   mode, not purge arming switched off (Jeff, 2026-10-28, show PC: with
   front-row-4 locked out the group armed 1 to 3, never tried 5 and 6,
   stayed grey, and a second click armed again instead of disarming).
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
   to say it is confirmed, and the tap is that confirm. The engine refuses
   an unlock of a machine while its flame group is armed or asked for, or
   while flamesafe's state is not known ("Disarm <group> first, then
   unlock <machine>"), from every surface; a lock is never held back.
6. **Stream Deck**: while this page is open the deck re-maps itself (the
   group keys show PURGE ARMED / OFF / LOCKED, the START / STOP key becomes
   the FIRE key) and restores when the page is left or the engine stops
   saying it is open.
7. **Checklists**: a Morning and a Shutdown list, steps editable in
   `ltcplay_morning.json` (`checklists`), ticks reset each night, every tick
   recorded for audit (below).
8. **Wave flamer cleaning** (Shutdown; Andy's design, Jeff's ruling
   2026-10-28): a hold starts phase 1 (no fire, on its own), a second
   hold the purge bursts, Stop ends it. Below, and OPERATOR.md.

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

**A group nothing says is G-Flames is never purged** (review of PR #120,
P2-1, and #126, P2-3). With no `machines` list, any flamesafe group could
be a fireball under another name ("FB", "Boom", "Poofer", "Crowd
Poofers", "Arrow"). Only Jeff's own rows are G-Flames by name, and only by
their whole name in any case: front row, cat-walk (or cat walk, catwalk).
A name that says fireball or ball is a fireball and one that says wave is
a waver. A group with more than one safety slot (several fixtures) is
purged only as a waver or when `gflame_groups` lists it, never by a row
name alone (third review of #126). Every other group's machines are built (so locking and the lamps
work) but none is purged or armed for a purge until the group is
declared. The tile says "Never purged: unknown flame group, declare it in
fireball_groups (or gflame_groups for G-Flames) in ltcplay_morning.json",
and the journal says it once at start. Declare it either way:

```
"fireball_groups": ["FB"]      never purged, one machine for the group
"gflame_groups": ["Boom"]      G-Flames, purged one by one
```

A `machines` list is Andy's own word on what each machine is and is taken
as written (a fireball group's machines must still be kind fireball).

**A fireball is never purged from this page.** (Its channels, the Purge
channel among them, are worked one by one on the Fireball page: OPERATOR.md,
"The Fireball page", and fireball.py. That page sends on this page's purge
frame and by its rules, one source at a time: a purge selection here waits
while a Fireball channel is on, and the Fireball page waits for a purge
selection.) Its channels are a sequence (charge, then
open, then ignite), and the purge writes one value to every slot at once,
which is not a valid sequence and could be dangerous. The tile says "Never
purged: a fireball has no purge sequence yet"; a click on any machine of a
fireball group is refused and arms nothing; the group is never asked of
the Stream Deck; a fire press never writes a value to its slots. No setting turns
this on: a fireball purge needs its own design and safety review first.
`purge.fireball` in `ltcplay_morning.json` is still accepted, so an older
file loads, and it is not used.

The Stream Deck (a Mini: six keys; Jeff, 2026-10-26: START / STOP, four
arm keys and a spare, no Hold or Abort key) has four arm keys. Which four
groups, in which order, and what each key says come from
`ltcplay_deck.json` in the settings folder (optional):

```json
{"groups": [{"group": "front row", "label": "Floor G"}, {"group": "cat-walk", "label": "Cat-Walk G"}, {"group": "wave flamer", "label": "Blue Flames"}, {"group": "fireball", "label": "Fireball"}]}
```

With no file, front row, cat-walk, wave flamer and fireball go on keys 1
to 4 by their names (any case), wherever flamesafe's config lists them,
labelled Floor G, Cat-Walk G, Blue Flames and Fireball; every other group
follows in flamesafe's order. The file lists only the groups it wants to
place, in key order; every group it does not list follows in that default
order. A label is up to 12 letters, and a group with none says its own
name. A group past the fourth has no key: it
is armed only by holding its Hold to arm button on the Rack screen
(Arming from a screen on): a purge click never arms it, and its tile says
so. It is disarmed there, by Disarm all flame groups, by a Stop on the
deck or by any Abort. At start the deck journals its key map and which
group has no key, and the Rack screen's flame card says "(no Stream Deck
key)" by its name. If anything in `ltcplay_deck.json` is wrong (a listed
group flamesafe does not have, an entry with no group name, a group twice,
a label over 12 letters, or a label that reads like another group's name
or label) the whole file is set aside for the default layout, so a typo
never moves a key; the journal says why and the Rack screen's Deck chip
reads "default keys". A group the file does not list is never wrong. It
never refuses to start over the layout.

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

### The fireball's fuel pump (Jeff, 2026-10-30)

The fuel pump is not on this page and is never purged. It is one DMX
channel on the flame universe (Jeff: channel 100, on at 255), a
passthrough "switch" in `flamesafe.json`, not a flame group: it has no
safety slot and is never armed. Andy turns it on by hand, from the
engine's fuel pump route (its button on Interface 2.0's SHOW tab comes in
its own update), for a run time he sets first (5:00 unless he sets
another, ten minutes at most), and it turns off by itself at the end of
it. Stop, Abort and the sign-in that started it signing out turn it off;
Hold does not. OPERATOR.md, "The fireball's
fuel pump", has the rules and the one line to add to `flamesafe.json`.

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
`purge.waver.head_settle_s` may be 0 (Jeff, 2026-10-26): then the head
going up and the fire go out together, in the same frame, on that first
beat. It still fires only a selected, unlocked Waver whose group flamesafe
reports ARMED, and every other rule here stays the same.
A heartbeat fires only when its body carries `"on": true` (the JSON word
true, nothing else). Every other `purge-fire` body (`"on"` false, missing,
null, 0, `"false"`) is a release: the engine turns it into the same stop
as `purge-stop` (same owner rule), it never reaches the fire path, it needs
no purge arming switch and no sign-in, and it can never start a press.
While this engine has any machine locked out, no purge goes until
flamesafe's status reports exactly the slots asked for. A stop that lands
while a beat's frame is being worked out wins: that beat sets nothing.

### The timed purge (Jeff, 2026-10-29; morning.py `Purge.timed_beat`)

Jeff's request: "In addition to momentary purges, we would like a
designated time length purge option. This is to allow for longer spurts of
testing in case there is a DMX issue." It is the momentary purge's own
selection and its own purge frame on the flame link, held up by the
engine's timer for a chosen number of seconds instead of by a held key.
Nothing new reaches flamesafe and the wire does not change: flamesafe is
still the only writer of the flame universe, arms nothing by itself, and
fires a slot only for a group it reports ARMED.

- **What it fires.** Exactly what the momentary purge fires for the same
  selection (`Purge.frame`, a Waver's head first). It starts only when
  every selected machine's group already reads ARMED at flamesafe; it never
  waits for one.
- **How long.** 2, 5, 10, 20 or 30 s, or Other (a whole number). The
  longest is `purge_timed_max_s` in `ltcplay_remote.json`: 30 s unless an
  admin sets it, never more than 120 s, never unlimited. A longer request
  is refused in full (the page also keeps Other within it). The last
  duration started on a show PC is its default (`purge_timed_s`).
  The longest is read when a purge starts: lowering `purge_timed_max_s`
  while one runs does not shorten it (it still ends at its own time, or
  at any stop).
- **How it starts.** A hold, timed by the engine from the page's beats
  (every 100 ms, `TIMED_HOLD_S`, the Hold to arm buttons' 1 s): a tap, a
  release, a gap of 0.25 s, a changed duration, a stale page (`seen`) or any
  refusal fires nothing. Each hold names itself; a hold id that has ended
  or started a purge, or a beat number not past every one that screen sent
  before, never starts one (a replayed or late press). The button's words
  name the machines and the seconds. Same permission as the purge (the arm
  row), the same `purge_arming` and `screen_arming` switches, the same
  Stream Deck must be reading, only the selection's own screen.
- **Three ways it stops on time.** (1) The engine's timer sends every
  100 ms and checks the deadline and every ending cause before each send,
  and again under the lock just before it sets the frame. (2) The flame
  link is given the hard end at the start (`FlameLink.purge_limit`, on its
  own clock): past it every frame it sends carries zeros and `purge_set`
  is refused until `purge_clear`, whatever keeps calling it. (3) The link
  drops the frame 0.25 s after the last send (`PURGE_HOLD_S`), so a timer
  that hangs or dies is a purge that stops; the watcher (10 a second) then
  ends it in the engine too, and also checks the deadline itself. If the
  whole engine stops, flamesafe zeroes by its own `frame_stale_ms`.
  A stall never relights it: a send more than 0.25 s after the last one,
  judged on a clock read just before the frame is set, ends the purge, and
  the link itself refuses a timed frame once it has dropped one as stale,
  or once a zero (Stop, Hold, Abort) landed after the count was read. The
  hard end is given as the time left to the deadline, so a pause before it
  never loosens it. A purge press while one runs ends it even from a
  screen whose sign-in may not arm (it is refused for anything else), and
  a Stream Deck FIRE the engine refuses posts Stop purge on its way out.
  A group seen disarmed at flamesafe ends the purge on the next send, up
  to 0.1 s later; meanwhile flamesafe itself sends nothing for a disarmed
  group, so the link's value for it reaches no flame.
- **What ends it at once.** Stop purge (from any screen, signed in or
  not), Stop, Abort, Hold (and a Hold standing), Start now, Run (refused or
  not), a show going live or the show clock playing, an Abort standing,
  flamesafe's status over 1 s old, the flame link not sending or alarmed or
  zeroed, a selected machine locked out or taken off, a disarm of one of
  its groups (pressed, or seen at flamesafe), the starter signing out, any
  screen in Arrange mode, the lock state not trusted, and any second purge
  press (Space, the deck's FIRE, another hold from any screen). Each is a
  journal line with why and the seconds it actually ran; the start line
  says the machines, the seconds, who and where.
- **One at a time.** No timed purge while Space or FIRE fires, and a
  momentary press while one runs ends it. The Fireball page, the wave
  flamer cleaning and live flame programming already refuse while a purge
  is selected or firing, and a timed purge refuses while either of the
  first two has the frame.
- **The page can close.** It runs on the engine's clock: closing or
  leaving the page, or an iPad going to sleep, does not end it (and does
  not keep it going past its time). Meanwhile the engine keeps telling the
  Stream Deck the group is wanted; once it has ended, the closed page's
  selection is cleared and the deck lets go of the group as before.
- **The countdown** is the engine's (`left_s` in the status and on
  deck-input): the rack screen's Startup page, a banner on every 1.0
  screen, 2.0's pinned line and pill, and the Stream Deck's key 0
  (PURGING and the seconds).

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

### Calibrate all lasers, Calibration off, Open BEYOND (Jeff, 2026-10-26)

Three buttons under the lasers on the Startup page:

- **Calibrate all lasers** turns the calibration test on for every laser
  that is ARMED and not locked out, one after another in the order of
  `lasers` in `ltcplay_morning.json`. Each one goes through the same rules
  as that laser's own button: a locked-out laser is skipped and named, a
  laser that is not armed is skipped and named, a laser whose cue is not
  set up says what to put in the settings and sends nothing. It is refused
  while a show is live, while an Abort stands, and while this page's
  settings or lockout cannot be trusted, and the engine asks again before
  each laser, so a show going live part way stops it there. From the show
  network it needs `screen_arming`, and Arrange mode holds it back, as a
  single test ON does.
- **Calibration off** turns every laser's test off. Like any test off it
  always goes, from every surface, signed in or not.
- **Open BEYOND** brings the BEYOND window to the front of the show PC, so
  the grid can be checked in BEYOND itself (`windowfront.py`). It only
  moves a window: it never sends anything to BEYOND, presses no key and
  never starts BEYOND. It works on the show PC itself only (the button is
  not shown elsewhere), signed in or not, any time no show is live: it is
  refused only while a show is live, on Hold or between shows (STANDBY),
  while an Abort stands, and while a hold to arm or a purge is going on
  (the rack screen stays in front for those). The calibration test can be
  running: bringing BEYOND forward to adjust it is what it is for.
  Calibration off stays on the rack screen; click the rack screen (or
  its taskbar button) to come back to it. It finds BEYOND's window by its program
  (`beyond*.exe`, the same names the BEYOND watch uses), restores it if
  it was minimized and puts it in front, never waiting on BEYOND; if
  BEYOND is not responding, or nothing has come back within 3 seconds, it
  says "BEYOND is not responding" and a second press waits for the first.
  When Windows will not let it come to the front, BEYOND's taskbar button
  flashes and the page says to click it. It says so plainly when this is
  not Windows, when BEYOND is not running, and when BEYOND is set up on
  another machine (it looks on the machine named by the `beyond` block's
  host).

### Is BEYOND (or MadMapper) on this PC?

ltcplay reads BEYOND as on this PC when the `host` of the `beyond` block
in `ltcplay_fire_ice.json` is `localhost`, any 127.x.x.x loopback address
(127.0.0.2 on the show PC, bench B8.0), or any other address of this PC
itself: a network card's address, or the address of a Windows loopback
adapter such as "Microsoft KM-TEST Loopback Adapter" (often 10.x,
169.254.x or 192.168.x). MadMapper's `madmapper` block is read the same
way (`thispc.py`, one rule for both). Any address that is not this PC's is
another machine, and `0.0.0.0` is never a host. On Windows this PC's
addresses are every adapter's, the list `ipconfig` prints; they are read
again at most every 30 seconds, and if they cannot be read, only
127.x.x.x and localhost count. Only
then does Open BEYOND bring its window forward, the BEYOND and MadMapper
watches look for the program in Windows' process list, and the pre-roll's
Lasers check look for BEYOND (the Network check pings only a BEYOND or
MadMapper that is NOT on this PC).

To see which address ltcplay uses: it is the `host` in the `beyond` (or
`madmapper`) block of `ltcplay_fire_ice.json`. When ltcplay reads it as
another machine, it names both that address and this PC's own addresses:
in the Open BEYOND refusal, in the pre-roll's Lasers line, and in the
Video chip's tooltip on the rack screen. If BEYOND is on this PC and the
address is not in that list, set the host to one that is (127.0.0.2, or
the loopback adapter's address as `ipconfig` shows it), make BEYOND
receive there, and restart the engine.

### How to set up the laser calibration cue

BEYOND's commands for the calibration cue are not known yet (coming
soon). Until they are, every laser says on the Startup page, armed or
not, "Calibration cue not set up yet", what is needed (BEYOND's OSC
messages that put that laser's grid up and take it down) and where (the
`lasers` block of `ltcplay_morning.json`), and nothing is sent. When Jeff
or Andy knows them, each laser in `ltcplay_morning.json` gets them under
`lasers`: `"mechanism": "osc"`, BEYOND's address
(`host` and `port`; left out, the `beyond` block of `ltcplay_fire_ice.json`
is used), and two lists of OSC messages, each one an address and a value:
`on` puts the grid up, `off` takes it down. For example (the addresses and
numbers here are only an example, not BEYOND's real ones):

```
"lasers": [
  {"id": "laser-1", "name": "Laser 1", "mechanism": "osc",
   "host": "127.0.0.2", "port": 8100,
   "on":  [["/beyond/general/startcue", 101]],
   "off": [["/beyond/general/stopcue", 101]]}
]
```

Each message is sent three times. BEYOND's BlackOut, MasterPause,
brightness and laser output switch can never be used here. Restart the
engine after changing the file. The laser still has to be armed for its
test to go on.

**How to find the address, step by step.** No bench or document in this
repo has found or verified BEYOND's OSC messages for starting or stopping
a grid cue, so this program names none. What the repo does establish
(the bench report of 2026-09-25, B8): BEYOND receives OSC only once
**Settings > OSC > OSC Settings** has "Enable receiving OSC messages" on
(default off) and an Incoming port (8100 on the bench; 8000, the default,
clashes with MadMapper); and **Settings > OSC > OSC Monitor** lists every
OSC message BEYOND receives. The bench used the OSC Monitor to confirm
each message ltcplay sent; BEYOND itself never sent anything back.

1. In BEYOND, open Settings > OSC > OSC Settings. Check that "Enable
   receiving OSC messages" is on and note the Incoming port. That port is
   the laser's `port` (or the `beyond` block's).
2. The `host` is the address BEYOND receives on, as ltcplay reads it
   ("Is BEYOND (or MadMapper) on this PC?", above): 127.0.0.2 on the show
   PC, as the bench used.
3. Find the candidate address and value in Pangolin's own OSC command
   list (wiki.pangolin.com, beyond:osc_commands; the list the bench
   used). Which command starts and stops one cue, and what value names
   the grid cue, is NOT verified here: Andy, who built the cue, is the one
   to say which cue it is. The `/beyond/general/startcue` lines in the
   example above are only an example.
4. Open Settings > OSC > OSC Monitor in BEYOND and leave it open.
5. Put the candidate `on` and `off` messages in `ltcplay_morning.json`
   for one laser, with `"mechanism": "osc"`, and restart the engine.
6. With Andy there and the laser armed, press that laser's test on the
   Startup page. In the OSC Monitor, check that the message arrived (the
   address and the value as written), and in BEYOND (or on the wall)
   that the grid came up at low power. Press it again and check the `off`
   message arrived and the grid went down.
7. If nothing arrived in the OSC Monitor, the host or port is wrong (step
   1 and 2). If it arrived but the grid did not change, the address or
   value is wrong (step 3). Whether BEYOND's OSC Monitor also shows a
   message when a cue is clicked in BEYOND itself is not known (the bench
   never tried it); do not rely on it.

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

### Wave flamer cleaning (Andy's design, Jeff's ruling 2026-10-28; cleaning.py)

The Shutdown part of the page has **Wave flamer cleaning**: one button,
**Hold to start the cleaning**, and a big **Stop**. It pressurizes and
depressurizes the pump, then fires the leftover fuel to clear it.

1. **Start hold.** One deliberate hold of the button, the same hold as a
   group's Hold to arm. A release before it completes starts nothing.
2. **Phase 1, no fire, on its own** (the walk-away Jeff approved for
   phase 1 only, `WALK_AWAY_PHASE1`): `cycles` (3) times armed `armed_s`
   (3 s) and disarmed `disarmed_s` (3 s), each counted from flamesafe's
   own report. No frame is set at all: only the wave flamers' safety
   slots move.
3. **Pause.** It ends disarmed and waits. Only a second full hold (a tap
   does nothing) goes on; with nobody holding it goes back to idle after
   `pause_max_min` (10).
4. **Phase 2.** That second hold is the go-ahead and is itself the arm.
   Once flamesafe reports ARMED the angle channels (the aim slots) go to
   `aim` (128, straight up), the heads get `aim_settle_s` (1 s) from the
   first frame actually written with them up, then the ignition (channel 3
   of each Showven) 255 for `burst_on_s` (1 s) and 0 for `burst_off_s`
   (1 s) until Stop or Abort, at most `max_burst_min` (10). Nobody has to
   hold. Speed, duration and preset stay 0.

**Arming: no new way to arm.** Every arm is the rack screen's own hold to
arm (`Remote.arm_hold`). The start hold and the go-ahead hold are the
person's own: each beat of theirs calls it inside their request, with
their device, window, sign-in and page status time. Phase 1's second and
third arms are the walk-away: the engine's watcher calls the same handler
for the person whose completed hold started it (their request's context,
the engine's own time), every 50 ms until the deck takes it; never in
phase 2, never while paused, never before a completed start hold. Each
time the Stream Deck sees an ordinary screen hold on deck-input and arms
only after 1 s of it, with its re-arm refractory window, the latch and the
operator gate; flamesafe applies its re-arm dwell, the clean rising edge
(nothing is set while arming) and the edge-quiet window. Each arm waits
for flamesafe to report ARMED (`arm_wait_s`, 8 s, then it stops and says
flamesafe's reason). The disarms are the screen's per-group Disarm
(`Remote.group_disarm`); a stop's disarm that flamesafe has not confirmed
within 1 s gets Disarm all flame groups.

**Values: the purge frame, by flamesafe.** Phase 2 only, after the
go-ahead, once flamesafe reports the group ARMED: this page's purge frame
(`FlameLink.purge_set`), written by the watcher every 50 ms; the link
drops it by itself 0.25 s after the last write, and flamesafe zeroes it
whenever the group is not armed. Each fixture's angle and ignition come
from flamesafe's config: the group named wave flamer, with a
`safety_profile` and `aim`, each safety slot s one 6 channel block (angle
s-5, speed s-4, ignition s-3, duration s-2, preset s-1, the rule
flamesafe's config itself enforces). Anything else: the page says it does
not match and nothing runs. One source of purge frames at a time: the
purge and the Fireball page wait for it, and it for them.

**Stops** (ignition 0 first, the angle left where it is, then the
disarm; a stopped run never carries on), in every phase and while paused:
Stop (the page's or the banner's on any screen, the rack screen's, the
Stream Deck's: the deck reads the cleaning as running, so its key is
Stop), Abort (which never waits on the cleaning's lock), Hold, any disarm
anywhere (the engine's disarm count moves), the flame link zeroed, the
group not reading armed when it should, flamesafe's status older than
0.5 s, a wave flamer locked or unlocked, a show live, Run pressed, live
flame programming, a purge, the Fireball page, Arrange mode, another
flame group armed or being held, an open alert of kind flames, link or
deck, every fixture locked out, screen arming switched off, the Stream
Deck not reading the engine, the starter (phase 1) or the go-ahead
(phase 2) signing out, any refusal, `max_burst_min`, `pause_max_min`.

**Settings** (`ltcplay_morning.json`, `cleaning`; leave a key out for the
default): `cycles` 3 (1 to 10), `armed_s` 3 (1 to 30), `disarmed_s` 3
(2.5 to 30: it must cover the deck's 2 s re-arm window and flamesafe's 1 s
dwell), `arm_wait_s` 8 (3 to 20), `disarm_wait_s` 2 (1 to 5), `aim` 128
(118 to 138: never far off straight up), `aim_settle_s` 1 (1 to 5,
counted from the first frame written with the heads up), `burst_on_s` 1
(0.2 to 5), `burst_off_s` 1 (0.5 to 10), `max_burst_min` 10 (1 to 30),
`pause_max_min` 10 (1 to 60). A value outside its limits: the page says
which and nothing runs.

**Fuel.** Its bursts are flame from the show tank: the Fuel page counts
them as burn ("other", like scrubbing), once, and never as a Showven purge.

## Threat analysis: what could fire flames unintentionally, or fail to stop

| # | Threat | Where it is stopped |
|---|---|---|
| T1 | A purge frame goes out while a show cue is playing (two sources of fire values) | `FlameLink.values_now`: purge is read only when `live is not True`; the engine refuses `purge-select` and `purge-fire` while the scheduler is in SHOW or PAUSED, while an Abort is latched, or while the show clock plays a cue; the watcher clears the selection when a show goes live |
| T2 | The page dies with Space down (crash, sleep, cable) | the heartbeat stops; `FlameLink` drops the purge frame 0.25 s after the last `purge_set` on its own clock; flamesafe zeroes `fire_hold_ms` after ltcplay's last frame if the engine dies too |
| T3 | Key repeat, a lost keyup, a second tab or device | the page sends heartbeats on its own 100 ms timer from one keydown and stops on keyup, blur, visibilitychange and pagehide; the heartbeats are not the key: the browser's key repeat events are the evidence the key is still down, and a press older than 2.5 s with no repeat event for 1 s ends (`fireKeyLapsed`; so the OS key repeat has to be on; the page says so). A physically stuck key still repeats, so the PAGE is not a dead-man switch; the Stream Deck's FIRE key is (its heartbeats stop the instant the key reads up). The engine binds a fire press to a `fire_id` and the device that owns the selection; a heartbeat from another device, or for an old `fire_id`, is refused and does not end the owner's press; the deck process is known by its `screen` over loopback, never by an operator name; a machine never joins a press that is already firing |
| T4 | A machine selected for purge is left armed after the page is gone | the selection lives only while the OWNER device's `morning-open` beat (every 500 ms) is under 2 s old (another device's open page does not keep it); leaving the page posts `purge-clear` at once; the deck disarms every purge group it armed the moment the engine stops listing it, and 0.3 s after the engine stops answering |
| T5 | A locked machine fires from a show cue or a purge | flamesafe zeroes locked slots on every packet (the only writer); the flame link zeroes them too; `purge-select` refuses a locked machine and a lock drops it from a live selection; while this engine has any machine locked, NO purge goes until flamesafe's status reports exactly the slots this engine asked for (an old flamesafe, one without a lockout file, or a stale status all refuse) |
| T6 | The lockout file is deleted, corrupted or half-written; the lock table and the config disagree | atomic replace by one writer; flamesafe keeps the last set on an unreadable file, and on a file that held locks and disappeared (the engine never deletes it), and journals both; `lockout_file` defaults to the file beside flamesafe's own config, so the show PC enforces it with nothing set up; on the engine's side a lockout file that could not be read, or one naming a machine `ltcplay_morning.json` no longer has, is never written over (every lock and unlock is refused, the purge and the laser tests too) and is said on every screen until fixed; the engine's view and flamesafe's `lockout` field are compared both ways on the page and in the journal: slots flamesafe holds that this engine cannot account for (the file deleted and kept by flamesafe, an engine restarted without it), a config edited under a lock, flamesafe with no lockout file, or one reading a different path than the engine writes (said at start, both paths named) all refuse every lock, unlock, purge and laser test; the one way out is a confirmed "Rewrite the lockout file" on the show machine itself, journaled as a fault, which writes exactly this engine's table |
| T7 | Abort during a purge | the engine's Abort clears the purge and sends `disarm_all` before anything else, as today; `disarm_all()` and `zero()` clear the purge frame; the deck has no Abort key since 2026-10-26: it disarms every purge group it armed when it sees the Abort latch, and a Stop (the deck's or the Rack screen's) sends every group false on the arm link and the flame link's `disarm_all`, without the latch; the laser tests are turned off |
| T8 | The deck arms a purge group while latched, in its refractory window, or with no operator chosen | the deck's purge pass arms through `_do_arm_fire` itself, the code a completed hold goes through: the latch, the re-arm refractory window and the operator gate are checked at the moment of arming; only the hold is waived (Jeff). A latched rig also reads as no groups wanted at all (`purge_wanted`). The test holds that no arm value goes out on the arm link even for one pass |
| T9 | A network device purges, unlocks or lights a laser without anyone meaning it to | purge from the network, an unlock and a laser test ON all need `screen_arming` true (the existing switch, off by default); a lock, which only lowers risk, is open to every device; every press is journaled with the device address; the same proxy, Host and cross-site refusals as every other route |
| T10 | A laser test lights during a show, or on a locked laser | the engine refuses the toggle while a show is live or latched; the watcher turns tests off when a show goes live; the sender refuses a locked laser; only configured addresses with no OSC wildcards, never BEYOND's BlackOut or MasterPause |
| T11 | The deck stays re-mapped after the page closes (FIRE where START was) | the deck's morning mode follows the engine's `morning_open` flag, which the engine clears 2 s after the last page beat from a device that may purge (a view-only device's beats do not count); a stale `deck-input` (0.3 s) also ends it |
| T12 | A Waver fires before its head is up | a fire press on a Waver sends pan and tilt first and the fire value only after `head_settle_s` (0 to 10 s; with 0 the head values and the fire value go in the same frame, the first beat on which it is selected, unlocked and armed) of continuous heartbeats, counted per Waver from the first beat on which it is both selected and armed, and counted again from nothing whenever its group drops out of ARMED mid press (the head has fallen); the head values go in the same frame as the fire value thereafter; a machine cannot join a press that is firing |
| T13 | The purge frame reaches a fire slot flamesafe has not armed | flamesafe sends a fire value only while that group's safety slot carries the arm value on the same packet (unchanged) |
| T16 | A purge writes one value to every channel of a fireball (fuel, air, valve and ignite at once) | the rule is by flamesafe group (`MorningConfig.purge_refusal`, `fireball_groups`): a machine of any kind in a fireball group is refused by `purge-select` and left out of `groups_wanted` and the frame, and `ltcplay_morning.json` cannot put a machine of another kind in one; the Stream Deck refuses a purge arm for a group named fireball or ball and for every group the page lists as never purged (`purge.never_groups`), key or no key, a second barrier (review of #126, P2-4), and never arms a group with no key for a purge click |
| T17 | The fireball's fuel pump runs on after it was meant to stop | flamesafe writes its slot (its own `on` value) only while the flame frame names it, the link is fresh and no `disarm_all` has come since it was last named OFF; the engine names it only before the run's hard end and with its timer beating (the timed purge's machinery: engine timer, the flame link's own end and beat on its clock, flamesafe's `frame_stale_ms` if the engine dies); Stop and Abort clear it at the link before the first `disarm_all` copy; it is not a fire slot, so it never arms or fires a group |
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
- Wave flamer cleaning: the timings, if Andy wants others than 3 s, 3 s,
  three times, 1 s on, 1 s off.
- The tally server's address and token (`tally`), and whether the payload
  below fits it.
