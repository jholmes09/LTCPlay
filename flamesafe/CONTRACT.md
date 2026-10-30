# The flamesafe link contract

Contract version 2. 2026-09-25, build step 7a, after the safety review.

This is the only thing ltcplay and flamesafe share. They share it as a
document, not as code: ltcplay implements its side from this page, and
neither program imports the other (a test fails the build if one does).

Both directions are UDP datagrams on loopback. Each datagram is one JSON
object, UTF-8, no framing. A datagram that is not exactly as written here is
rejected with a reason and changes nothing. flamesafe never raises on input.

## Version 2: the shared key

Any local process can write a datagram to a loopback port. In version 1 a
rogue frame with a large sequence number was accepted and then locked the
real ltcplay out until the link went stale (found in review). Version 2 adds
two guards:

1. **The key.** flamesafe's config carries `link.key` (16 to 128 printable
   characters, no spaces). Every flame frame and every status frame carries
   it as `"k"`. A flame frame without the right key is rejected before any
   other field is looked at. ltcplay reads the same value from its own
   config (the same string, typed once into each), sends it in every flame
   frame, and MUST reject any status frame whose `k` is not its own.
2. **The sender lock.** While the link is live, flamesafe accepts flame
   frames only from the (ip, port) of the first accepted sender; a frame
   from anywhere else is rejected with the reason `another sender`. Once the
   link is stale (`frame_stale_ms` without an accepted frame) the lock is
   released, so a restarted ltcplay on a new port takes it. Since
   2026-10-03 a second keyed sender on the link, or the lock changing
   hands, also disarms every group and stops any arm cycle counting for a
   while (see "Second copies" below).

Neither is secrecy in the cryptographic sense; the key lives in two config
files on one machine. Together they mean that firing a head from this
machine needs the key and the socket ltcplay already holds, not one
datagram.

What ltcplay must do for the lock to mean anything:

- **Send from ONE socket for its whole lifetime.** The lock is on
  (ip, port); a new socket per frame would be "another sender" every time.
- **Alarm when the lock is not its own.** The status frame's `frames.seq`
  is the seq of the last accepted frame. If it is not ltcplay's own seq for
  more than 1 s while ltcplay is sending, something else holds the lock:
  show red for the safety program and write it to the journal. The rogue
  is being fed the right key by something; that is a person's problem, not
  a program's.
- A config marked `confirmed` is refused while `link.key` is still the
  example key from the repo.

## Second copies: what was decided and built (Jeff, 2026-10-03)

PR #34's open question 10 (review probe p2 s5): while ltcplay restarted, a
second sender with the key took the flame link's sender lock; the real
ltcplay came back and was refused as "another sender"; the operator cycled
the arm on the Stream Deck and the groups armed, with the other sender
supplying the fire values and the screen Abort refused.

**Decision.** The show machines are dedicated hardware with nothing else
installed, and every link binds 127.0.0.1 only. So the realistic second
sender is a second copy of ltcplay or of `ltc deck` itself: a launcher
double-clicked twice, autostart plus a manual launch, an old copy still
running after a restart. Protecting the key (a separate Windows user, a
config only that user can read) was considered and is **not** built. Two
cheap guards are:

1. **Flame-link checks in flamesafe** (fix round 1 of PR #40 made the
   first one a disarm, on Jeff's direction):
   - **A second sender disarms every group.** When a well-formed, keyed
     flame frame or disarm_all arrives from any (ip, port) other than the
     live, locked sender, it is refused as `another sender` AND every
     group's latch and pending down edge is cleared on the tick it arrives
     in: every armed group comes off the wire. With one copy of ltcplay per
     machine (point 2), two keyed senders at once means something is wrong.
     This can only ever take arm away. The sender counts as present for
     `frame_stale_ms` after its LAST datagram (`frames.foreign_senders`),
     and while it does no arm cycle counts. The journal gets one plain line
     per episode (`second-sender`), naming both senders and the groups it
     disarmed.
   - **A change of hands disarms and waits.** When the lock is taken by a
     sender other than the one that held it before, every latch is cleared
     and no arm cycle counts for `frame_stale_ms` (`frames.new_sender`).
     Every normal ltcplay restart does this (a new socket is a new port),
     after link loss has already disarmed every group, so after every
     restart the Stream Deck's group keys read OTHER SENDER for about half
     a second and a cycle in that half second does not count. The first
     sender flamesafe ever sees, and the same sender coming back, are not a
     change.
   - **A flood blocks consent.** More than 50 datagrams, or more than
     256 KiB, waiting on the flame link in one tick, keyed or not, is a
     flood (`frames.flooded`); no arm cycle counts for `frame_stale_ms`
     after the last one, and it is journaled once per episode. It does not
     disarm by itself. This is the arm link's flood rule; an honest ltcplay
     sends about one 2 KB frame per tick.

   While any of these holds, the lamp of a group the deck asks for is held,
   steady amber: `Another sender is on the show program link, or it just
   changed hands: every group is disarmed and a cycle cannot arm until that
   has settled. Cycle the arm again once it has.` (the deck key reads OTHER
   SENDER). Afterwards nothing re-arms by itself: a group needs a genuine
   off-then-on from the Stream Deck, as after the arm link's veto.

   Measured with the review probes (real flamesafe, real ltcplay
   `FlameLink`, real Stream Deck controller, the actual universe read off
   the wire):
   - p2 s5 (a second sender takes the flame link in an ltcplay restart gap,
     ltcplay comes back and is refused, the operator cycles): before PR
     #40 groups 0 and 1 armed and fired; now nothing arms and nothing
     fires.
   - Review s8 (a group armed while the second sender was the only one on
     the link, then ltcplay comes back): before fix round 1 it stayed armed
     and the second sender's fire reached the wire; now it is disarmed the
     moment ltcplay's first frame arrives.
   - Review s7 (a keyed second sender holds the link by flooding, with junk
     floods from other ports crowding ltcplay's frames out): nothing armed
     in any run, and the flood flag kept the check on in every status
     sample.

   **What the flame-link checks do not do.** They do not stop a second
   sender TAKING the flame link. A flood, or an ltcplay restart gap, still
   hands the lock to whoever has the key and sends next; ltcplay's own
   frames, fire values and screen Abort are then refused for as long as the
   other sender keeps the lock. So the show's flames stop (nothing is armed,
   nothing fires) rather than going wrong.
2. **One copy per machine** (ltcplay side, `ltcplay/onlyone.py`). The show
   program (`ltc run` and `ltc serve`, one lock between them) and `ltc
   deck` each take an exclusive lock on a file in ltcplay's state folder
   for their whole life (`ltcplay_show.lock`, `ltcplay_deck.lock`, beside
   `ltcplay_output.lock`) and refuse to start while another copy holds it,
   printing what the running copy says about itself (its process id, the
   command, the show folder or config, when it started) and how to stop
   it. The lock is a flock on a Mac and a byte-range lock on Windows; the
   operating system drops it when the holder dies however it dies, so a
   crashed or killed copy never blocks a restart. A rehearsal (`ltc run
   --no-output`, Rehearse in the menu) is a copy too: it is refused while
   the app, the autostart engine or a Run or Web window is running.

**What remains, stated plainly.** A program that is NOT ltcplay or `ltc
deck`, running on the show machine and holding the key, can still:
- take the arm link while the deck process is down and arm a group with an
  off-then-on of its own (the deck residual under "The arm link" below);
- take the flame link while ltcplay is down, and once it has been the only
  sender for `frame_stale_ms`, supply the fire values to any group the
  operator then cycles on the Stream Deck. That lasts until ltcplay comes
  back, which disarms every group;
- hold the flame link against a running ltcplay (by a flood, or by getting
  in during a restart gap), so that no group can be armed and the show has
  no flames until it stops.
Jeff accepts that: on a dedicated show machine there is no such program,
and a second copy of ltcplay or `ltc deck` refuses to start.

## Ports and addresses

From the flamesafe config (`flamesafe.example.json`):

| Direction | Address | Config key |
|---|---|---|
| ltcplay to flamesafe, flame frames | 127.0.0.1, `link.listen_port` (example 5571) | flamesafe binds it |
| flamesafe to ltcplay, status frames | 127.0.0.1, `link.status_port` (example 5572) | ltcplay binds it |
| flamesafe to ltcplay's engine, a copy of every status frame | `link.status_ip`, `link.status_mirror_port` (optional, absent by default) | ltcplay's engine binds it, for the remote page |

`link.status_mirror_port` (added 2026-10-03 for the iPad remote): when set,
every status frame is sent a second time, byte for byte, to this port as
well. The Stream Deck process binds `status_port`, so the engine needs its
own copy to show flamesafe's real armed state on the remote page. It is
display only, exactly like `status_port`: nothing is ever read from it by
flamesafe, and a failed send there is counted (`mirror_errors` on the
service) but is never a fault, because neither the wire nor the deck's own
status is affected. It must be a port of its own; the config refuses one
that equals `listen_port`, `status_port`, `arm_port` or a loopback
destination port.
| flamesafe to the flame node, sACN | `destination.ip`, `destination.port` (5568) | unicast, priority 200 |

The link addresses must be loopback; the config refuses anything else, and
refuses a loopback destination on either link port.

## Flame frame, ltcplay to flamesafe

One per output frame, at ltcplay's frame rate, whether or not a show is
running. When ltcplay is idle it still sends frames (all zeros), because a
missing frame means "unknown" to flamesafe, and unknown is zero.

**Send-rate floor: 20 Hz or faster, idle included.** ltcplay must never
let more than 50 ms pass between link frames, whatever it is doing, so
that `frame_stale_ms` (500 ms) means ten or more missed frames in a row
before the link is declared lost and every group disarms, never one late
frame. A sender that idles slower than this will disarm the show for no
reason.

```json
{"v": 2, "k": "<link.key>", "t": "flame", "seq": 1234, "tc": "00:01:02:03",
 "mono": 812.4471, "universe": 1, "values": [0, 0, 0, ...]}
```

| Field | Type | Rule |
|---|---|---|
| `v` | integer | must be 2 |
| `k` | string | must equal flamesafe's `link.key` |
| `t` | string | must be `"flame"` |
| `seq` | integer, 0 or more | increases by one per frame; must be greater than the last accepted seq while the link is live |
| `tc` | string or null | `HH:MM:SS:FF`, or `HH:MM:SS;FF` for drop frame, or null when there is no timecode |
| `mono` | number | the sender's own `time.perf_counter()` at the frame, seconds; must not go backwards while the link is live |
| `universe` | integer | must equal flamesafe's configured flame universe |
| `values` | list of exactly 512 integers, each 0 to 255 | the whole flame universe, slot 1 first |
| `hazers_on` | optional list of strings | the passthrough entries (the hazers) the show program has switched ON, by name: at most 16, distinct, each 1 to 64 characters. Left out is the empty list: every hazer off. ltcplay sends it (`[]` for none ON) only while flamesafe's config lists hazers; with none, its frame is exactly the frame before this field existed. Added 2026-10-29; see "Passthrough: the hazers" |

**ltcplay's side: when values may be non-zero.** ltcplay sends non-zero
values only while a show is released, live, and its timecode is moving
(`ltcplay/flamelink.py`). Design note (PR #34, fix rounds 1 and 2):
"moving" means the timecode CHANGED within the last 0.1 s, not that it
advanced. A source stuck bouncing between two frames, or stepping
backwards, still counts as moving (review probe p9b). The rule catches a
clock that has stopped, not one that is wrong.

Rejected, with the reason in the next status frame's `frames.last_reject`:
not JSON, not an object, longer than 16384 bytes, wrong `v`, wrong or
missing `k`, wrong `t`, any field missing or of the wrong type, a `seq` at
or below the last accepted one, a `mono` below the last accepted one, a
`values` list that is not exactly 512 integers 0 to 255, a `universe` that
is not the flame universe, a `hazers_on` that is present and not a list of
at most 16 distinct names of 1 to 64 characters (checked after the key, so a
wrong key is still "wrong key"), a sender other than the locked one.

**Rejections are journaled once per episode (2026-10-02).** Before this
date a rejected flame frame was counted and shown in `last_reject` but
never written to the journal. Now they are journaled with the arm link's
own throttle (round 4 of #31's review, item C, `arminput._RejectJournal`):
one line per reason when an episode starts, naming the sender's address;
one closing line with the count and how many distinct source addresses
once that reason has been quiet for 5 s (only if there was more than one);
and never more than 4 lines per reason in any 60 s. The reason is one of a
fixed list (`composer._FLAME_REASONS`: "wrong key", "another sender",
"wrong contract version", and so on), never the raw message, because the
message can carry text and numbers the sender chose; a refused disarm_all
has its own `disarm_all: <reason>` keys. The sender's text in a line is cut
at 200 characters. This covers flame frames and disarm_all (below) alike.

Two windows, both on flamesafe's clock from the last accepted frame:

- **`fire_hold_ms`** (example 100 ms, config; floor two ticks, ceiling
  below `frame_stale_ms`): how long the last frame's fire values stay on
  the wire after ltcplay stops sending or its sequence sticks. After it,
  every fire slot is zero. Jeff and Andy can adjust it: shorter cuts a
  flame sooner when ltcplay dies, longer rides through a hiccup. Rev 5 had
  no hold at all; 100 ms is four ticks.
- **`frame_stale_ms`** (example 500 ms): after it the link is stale, the
  sender lock is released, and any `seq` is accepted, so a restarted
  ltcplay re-syncs by itself.

**Losing the link disarms (Jeff, 2026-09-26).** When the link goes stale
while the safety program is running, every group is disarmed exactly as
for a stale arm input: the latches are cleared, the arm value comes off
every safety slot, the journal gets one sentence, and the ARMED lamp reads
steady amber with "Show program stopped answering: disarmed. Cycle the arm
to re-arm once it is back." When ltcplay comes back nothing re-arms by
itself; the operator cycles the arm (the lamp then reads "cycle the arm",
flashing). The journal gets one more sentence when the link is back, with
the length of the outage, and nothing in between: the latches are cleared
on every stale tick, but only the first one is written and counted. A
cycle made while the link is still down does not count. At
startup, before ltcplay has answered at all, no group can arm: the lamp
reads "Show program has not answered yet: disarmed. Cycle the arm once it
is running." This replaces the earlier design in which the arm value stayed
on the safety slot with only the fire slots zeroed; handoff section 15 is
updated to match.

Clocks: `mono` is compared only with earlier `mono` values from the same
sender. flamesafe never compares it with its own clock.

## Status frame, flamesafe to ltcplay

One per flamesafe tick (`tick_hz`, example 40 Hz), whether or not ltcplay is
sending. The heartbeat counts ticks; a heartbeat that stops means flamesafe
has stopped. The screen never estimates anything from this frame; it shows
what is in it.

```json
{"v": 2, "k": "<link.key>", "t": "status", "heartbeat": 88123,
 "tick_ms": 25.0, "universe": 1, "priority": 200, "arm_value": 78,
 "confirmed": false, "fault": "", "fault_age_ms": null,
 "arm_input": {"state": "live", "seq": 4410, "age_ms": 12},
 "frames": {"state": "fresh", "fire": "passing", "seq": 1234,
            "timecode": "00:01:02:03", "age_ms": 8, "accepted": 1234,
            "rejected": 0, "last_reject": ""},
 "sacn": {"sent": 88123, "errors": 0, "status_errors": 0},
 "stats": {"...": "counters, for the journal"},
 "groups": [
   {"name": "front row", "safety_slot": 401,
    "safety_slots": [401], "safeties_up": [true],
    "safeties_locked": [false],
    "fire_slots": [411, 412, 413, 414, 415], "aim_slots": [],
    "wanted": true, "armed": "armed", "reason": "", "amber": "",
    "dwell_s": 0, "sent_safety": 78,
    "sent_fire": [0, 0, 0, 0, 0], "commanded_fire": [0, 0, 0, 0, 0]}
 ]}
```

Top level:

| Field | Meaning |
|---|---|
| `k` | the key. ltcplay rejects a status frame without its own key |
| `heartbeat` | flamesafe's tick counter. The corner flame and the deck marquee step on this |
| `tick_ms` | the tick period |
| `universe`, `priority`, `arm_value` | what flamesafe is configured to send. `priority` is always 200. `arm_value` here is the FILE's arm value, the default for every group without a `safety_profile`; a group with one arms at its own value, and the per-group `arm_value` is the truth for that group |
| `confirmed` | false until the config's numbers are confirmed by Andy. Show it, quietly: ltcplay journals it (the Stream Deck once, when it first sees it) and carries it in the engine's status for the rack screen's Info popup. It is never drawn on a Stream Deck key (the amber corner mark went 2026-10-19) and never changes what any key reads |
| `fault` | empty, or one sentence: an overrun, a compose fault, a failed sACN send, a failed status send. `fault_age_ms` says how long ago. **A non-empty fault is red for ltcplay**: an armed group is not "fine" while the wire is not being written. A fault clears itself after 5 s of clean ticks and clean sends (the journal records both the fault and its clearing), so one failed send is not red all night; the cumulative counts (`sacn.errors`, `sacn.status_errors`, `stats.overruns`, `stats.compose_faults`, `stats.faults_noted`, `stats.faults_cleared`, `stats.journal_dropped`) never reset, and ltcplay shows them in health |
| `arm_input.state` | `never`, `live` or `stale`. Stale means every group is disarmed |
| `arm_input.foreign_senders` | how many OTHER senders are currently sending on the arm link besides the locked one (added round 3 of the safety review, 2026-10-02). Zero almost always. A non-zero count means a second local process is talking to this port right now. Its `wanted` bits can only ever CLEAR a group's bit, never set one, while it is not the locked sender. It CAN become the locked sender: the lock goes to whoever sends next once the locked sender has been quiet for `arm_stale_ms`, and a flood that crowds the real deck's frames out of the receive buffer makes the real deck look quiet (the round-4 review did this). Once that has happened the real deck, still sending, is the one counted here. That is why, as of round 4, no consent edge counts while this is non-zero (see the arm link section): whoever holds the lock, nobody can arm a group while anyone else is on the link. ltcplay must alarm on this alone; the clearing can leave `wanted` looking exactly like what the deck itself expects, with nothing else to notice |
| `arm_input.flooded` | true while flamesafe has seen a flood on the arm port (more than 50 datagrams, or more than 64 KiB, waiting in one poll, keyed or not; the byte limit added round 5) inside the last `arm_stale_ms` (added round 4 of the safety review, 2026-10-02). No consent edge counts while it is true |
| `frames.state` | `never`, `fresh` or `stale` (by `frame_stale_ms`). `stale` or `never` means every group is disarmed and needs a cycle once the link is back |
| `frames.fire` | `passing` while the last frame is younger than `fire_hold_ms`, else `zeroed`: every fire slot is zero |
| `frames.last_reject` | why the last rejected datagram was rejected (a refused disarm_all's reason starts `disarm_all:`) |
| `frames.foreign_senders` | how many OTHER senders had a keyed, well-formed flame frame or disarm_all refused as `another sender` inside the last `frame_stale_ms` (added 2026-10-03, the second-copy guard). Zero almost always. The first such datagram of an episode disarms every group; while non-zero, no consent edge counts (see "Second copies" above) |
| `frames.new_sender` | true for `frame_stale_ms` after the flame link's lock passed to a different sender (added 2026-10-03). The change itself disarms every group; no consent edge counts while it is true. True for about half a second after every normal ltcplay restart |
| `frames.flooded` | true for `frame_stale_ms` after a tick that found more than 50 datagrams, or more than 256 KiB, waiting on the flame link (added 2026-10-03, fix round 1 of PR #40). No consent edge counts while it is true |
| `passthrough` | the hazers (added 2026-10-29): one entry per config `passthrough` entry, `[]` for none. See "Passthrough: the hazers" |
| `disarm_all` | what the show program's Abort did: `accepted` (disarm_all datagrams taken, cumulative), `last_id`, `last_reason`, `age_ms` (null before the first). Added 2026-10-02, see "Disarm every group" below |
| `sacn.sent`, `sacn.errors`, `sacn.status_errors` | packets sent to the node, sends that failed, status sends that failed. Cumulative, never reset; shown in health. A failed send also sets `fault` for 5 s, which is the red |
| `stats.journal_dropped` | journal lines lost while the console was blocked, cumulative. When the backlog drains the journal writes how many were lost |

Per group, the two lamps of section 8 panel 5:

| Field | Meaning |
|---|---|
| `wanted` | what the arm input is asking for |
| `armed` | the ARMED lamp: `disarmed` (dim blue), `armed` (green: the safety slot carries the arm value), `held` (amber: arm asked for and refused) |
| `reason` | why held, in words: `cycle the arm`, `dirty edge`, `re-arm dwell`, `chatter`, `arm input stale`, `arm input has never asserted`, `safety program fault`, `Show program stopped answering: disarmed. Cycle the arm to re-arm once it is back.`, `Show program has not answered yet: disarmed. Cycle the arm once it is running.`, `Another sender is on the arm link: a cycle cannot arm until it stops. Cycle the arm again once it has gone.` (round 4: shown instead of `cycle the arm` while `arm_input.foreign_senders` is non-zero or `arm_input.flooded` is true, and also instead of the Abort sentence), `Another sender is on the show program link, or it just changed hands: every group is disarmed and a cycle cannot arm until that has settled. Cycle the arm again once it has.` (2026-10-03, the second-copy guard: shown the same way while `frames.foreign_senders` is non-zero, `frames.new_sender` is true or `frames.flooded` is true; the arm link's sentence wins when both apply), `Disarmed by the show's Abort. Cycle the arm to re-arm.` (2026-10-02, disarm_all) |
| `amber` | `flashing` when cycling the arm is the fix (`cycle the arm`, `dirty edge`, the Abort sentence); `steady` when cycling would only restart the wait or fix nothing (`re-arm dwell`, `chatter`, and every veto, the flame link's included). Empty unless held |
| `dwell_s` | whole seconds left in the re-arm dwell, 1 or more while held for it, else 0. The lamp shows this number; the screen never counts down on its own |
| `arm_value` | the value this group raises its safety slots to: the file's `arm_value`, or its `safety_profile`'s (added 2026-10-26). The rack screen shows it as the group name's tooltip, "Arms at N on its safety slots" |
| `sent_safety` | the SENT safety value this tick: 0 or the group's `arm_value` |
| `safety_slot` | the group's safety slot; for a multi-safety group, the first of `safety_slots` |
| `safety_slots` | every safety slot of the group, in config order (added 2026-10-26, multi-safety). One entry for a group whose config `safety` is one number |
| `safeties_up` | one per safety slot: true when that slot carries the arm value in this frame (added 2026-10-26). `armed` stays the one state of the whole group; this list is for the rack screen's "2 of 3 safeties up" line |
| `safeties_locked` | one per safety slot: true when that slot is in the lockout file and so held at zero and left out of the arm (added 2026-10-26). Always false for a one-safety group |
| `aim_slots` | the group's aim slots as this program read them at start, sorted; `[]` for a group without `aim` (added 2026-10-28). Display only: ltcplay compares them with its own list (a display only "aim" alert while they differ) and the rack screen's aim line shows them with `commanded_fire` and `sent_fire` on those slots. An older ltcplay ignores it |
| `sent_fire` | the SENT fire values this tick, one per fire slot |
| `commanded_fire` | what ltcplay asked for on those slots this tick (COMMANDED). Not a lamp; for the journal and the amber row flag (armed, commanded fire, sent none, for more than 3 frames) |

### Multi-safety groups (added 2026-10-26)

A group's `safety` in the config is one slot (as before, behaviour
unchanged) or a list of distinct slots: one arm group driving several
fixtures, each with a safety channel of its own (the wave flamer: three
6-channel fixtures, channels 1 to 5 fire slots and channel 6 that fixture's
safety). A list of one is the same group as that one number. Every slot
rule applies to every safety slot: 1 to 512, never one of any group's fire
slots, never shared with another group. Such a group is still one arm
group: one Stream Deck key, one rack screen card, one hold to arm, one
disarm, one `armed` state.

- Arming raises every unlocked safety slot of the group in the same frame.
  Disarming, an Abort, a lost link or any other disarm lowers all of them
  in the same frame.
- A safety slot named in the lockout file stays at zero under any command
  and is left out of "every safety up": the group arms on its other
  safeties. With every safety slot locked the group is held (steady amber,
  "Every safety slot of this group is locked out: it cannot arm until one
  is unlocked."). Fire slots follow the lockout file exactly as before.
- After composing each frame flamesafe confirms that every unlocked safety
  slot of every raised group carries its arm value. If one does not (it
  dropped, was overwritten, or was unlocked while the group was up, so its
  rising edge was never checked), the whole group comes down in that same
  frame: every safety slot and every fire slot zero, the latch cleared, a
  journal line, `stats.safety_unconfirmed` counted, and the lamp reads
  "Not every unlocked safety slot of this group was up: it was disarmed.
  Cycle the arm to re-arm." (flashing amber). A valid config cannot trip
  this; it is the fail safe.
- Nothing from the show program's frame (show data, xLights live, the
  purge) ever reaches a safety slot, exactly as for a one-safety group:
  flamesafe writes safety slots from its own arm state only.
- A safety slot in the lockout file of a ONE-safety group is ignored, as
  it always was (only that group's fire slots are zeroed by the lockout).

### A group's own arm value: safety_profile (added 2026-10-26)

The wave flamer is not a G-Flame, so the G-Flame and Showven rules that fix
the file's `arm_value` (78 validated for 30-50%, 157 for 60-80% with
`accept_unsourced_risk`, everything else refused) do not describe it. Jeff
tunes its arm value in the config, as often as the fixtures need:

```json
{"name": "wave flamer", "safety": [6, 12, 18],
 "safety_profile": {"arm_value": 178},
 "fire": [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17]}
```

A profile takes four keys; any other key is refused. Every number is Jeff's,
and the optional keys are guards that compare only the numbers written in
the profile, never the G-Flame or Showven ones:

| Key | Rule |
|---|---|
| `arm_value` | required. A whole number 1 to 255. 0 is the disarm value and is refused; 255 is allowed |
| `enable_lo`, `enable_hi` | optional, both or neither (null is the same as left out). `enable_lo` is at least 1, so the disarm value 0 reads as not enabled, and `arm_value` lies inside the window |
| `fire_at` | optional (null is the same as left out): the lowest safety value that fires the fixture. `arm_value` is below it, and no single-bit flip of `arm_value` reaches it (178 with `fire_at` 242 is refused, because 178 with bit 6 flipped is 242; 243 loads). Left out, there is no fire guard: the config loads and flamesafe writes one line at start saying that group has no fire guard |

- A profile replaces the G-Flame and Showven checks for that group only. It
  needs no `accept_unsourced_risk`: the profile is the deliberate
  statement. Every group without a profile, and the file's own
  `arm_value`, is checked exactly as before (178 there is still refused).
- A group `arm_value` beside a profile that says a different number is
  refused; the same number is allowed.
- Arming raises every safety slot of the group (one or a list) to the
  profile's value; any disarm writes 0 on all of them. Every other rule
  (the sole writer, the Abort latch, the link key and lock, hold to arm,
  `arm_stale_ms`, one copy per machine, the lockout, the edge gate, the
  dwell) is unchanged.
- The config is read at start, as before: a changed value takes effect on
  the next restart of flamesafe.

### Aim slots: a head's angle is not a flame (added 2026-10-28)

The wave flamer's channel 1 points the nozzle (0 is -105 degrees, 128 is
straight up, 255 is +105 degrees). It lights nothing, and xLights rests it
at 128. Two rules written for flame channels held it at zero: this
program's rising-edge check (every fire slot below 15) and ltcplay's
zero-first hold (a slot waits to be seen at zero after a landing). The
nozzle sat at its end stop for the whole show while the preview swept it
(Jeff, 2026-10-28). A group may list such slots as `aim`:

```json
{"name": "wave flamer", "safety": [6, 12, 18],
 "safety_profile": {"arm_value": 178},
 "fire": [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17],
 "aim": [1, 7, 13]}
```

The wave flamer's channels, per fixture (6 channels: slots 1 to 6, 7 to
12 and 13 to 18 in the example). Source: Jeff, from the Showven xLights
model (2026-10-28); there is no Showven DMX chart in this repo.

| Channel | Role | Values | Written by |
| --- | --- | --- | --- |
| 1 | Angle (the only `aim` slot) | 0 = -105 deg, 128 = 0 deg (straight up), 255 = +105 deg; 15 steps of 15 deg, e.g. 54 = -60 deg | xLights |
| 2 | Speed | 0 and 255 = max speed; 1 to 254 slower to faster; xLights sends 255 while moving | xLights |
| 3 | Ignition | 0 to 253 off, 254 to 255 on | xLights |
| 4 | Duration | 0 and 255 = fires while ch3 is on (10 s limit); 1 to 254 = value x 10 ms; xLights sends 0 | xLights |
| 5 | Preset | 0 to 2 none; 3 to 255 = preset N, value = 2 + N x 2.55 (a preset can run a programmed sequence: fire-capable) | xLights |
| 6 | Pressure / arm (safety slot) | 0 to 49 relief (e-stop), 50 to 200 armed, 201 to 255 relief | flamesafe only (arms at 178) |

Only channel 1 is aim. Channel 2 (speed) lights nothing but stays a fire
slot under every rule: xLights sends 255 on it while the head moves, so a
group armed mid-movement is held by the rising-edge check, as before.

- Optional. Each aim slot must be one of the group's own fire slots, listed
  once, and at least one fire slot must stay out of `aim`; anything else is
  refused at start.
- `aim` is only for a group with a `safety_profile` (the wave flamer); in
  a G-Flame or Showven group, or a wave flamer whose profile was deleted,
  it is refused at start.
- A typo must not free a fuel channel, so `aim` is also refused at start
  unless: each fixture has at most one aim slot (a fixture's slots are the
  group's fire slots after the previous safety slot, up to its own; a group
  with one safety slot is one fixture holding every fire slot), in a group
  with several safety slots no aim slot lies after the last one, and each
  aim slot is channel 1 of its fixture: 5 below that fixture's safety slot
  (channel 6), with the fixture's fire slots exactly that slot and the 4
  after it. `[1, 7, 13]` is taken; `[1, 7, 14]`, `[2, 7, 13]`,
  `[2, 8, 14]` and `[2, 3, 4, 5]` are refused, by flamesafe (it does not
  start) and by ltcplay's reader (`ltc serve` does not start).
- What this cannot catch: a whole layout moved consistently (safety, fire
  and aim all shifted by the same amount). Channel 1 is measured from the
  safety slot the operator writes; only the patch Andy confirms says where
  each fixture really starts.
- Restart BOTH flamesafe and ltcplay after editing `aim`: each reads the
  list once, at start. Deploy and restart them together, from the same
  release. A flamesafe from before #146 refuses the `aim` key as unknown
  and does not start (fail closed); the one in #146 accepts `aim` without
  the per fixture rules above.
- On every disarm, abort, stale input or lost link the angle goes to 0,
  like every fire slot, so the head slams to its -105 degree end stop. On
  a re-arm the flame slots are free after the 3 tick edge-quiet hold while
  the head may still be swinging back from that end stop.
- An aim slot is still a fire slot everywhere else: it goes out only while
  the group is armed, it is zero when the group is disarmed, locked out,
  aborted or stale, and the lockout file zeros it like any fire slot.
- It is left out of the rising-edge check (a nozzle at 128 does not hold
  the group disarmed) and is not held at zero through the edge-quiet
  window (zero would swing the head to its end stop for those ticks).
  Every other fire slot of the group keeps both rules exactly as before.
- ltcplay reads the same lists from this file and leaves aim slots out of
  its zero-first hold, in show playback (Programming playback included)
  and live flame programming.
- Seeing them (2026-10-28). flamesafe journals one start line, `aim slots:
  wave flamer 1, 7, 13 (...), read from <full path of its config>` (or
  `aim slots: none in <path>`), and every status frame carries each
  group's `aim_slots`. ltcplay journals its own list and the file it read
  when the flame link starts (`Flame link: aim slots 1, 7, 13 ... read
  from <path>`). While the two lists differ (ltcplay started before the
  edit, or the two read different files) the rack screen raises an alert
  of its own kind, "aim", saying both lists, the file ltcplay read and what
  to do; it clears itself once they agree. Display only: it never holds a
  show at showtime, plays no announcement, sounds no booth note and
  changes nothing on the Stream Deck, it
  never blocks Stop, Hold, Abort or a disarm, and nothing in it reaches a
  flame frame or the arm link.

What ltcplay does with it: display, journal, the amber row flag. There is no
path from a status frame to flame output, because ltcplay never writes the
flame universe.

If no status frame arrives for 1 s, ltcplay shows red for the safety
program and keeps running the rest of the show. It never takes over the
flame universe. There is no fallback path, deliberately.

## Passthrough: the hazers (added 2026-10-29)

Jeff, 2026-10-29: three haze machines join the flame universe. "Abort
turns off the hazers. Hazers don't have an arm/disarm channel." And: "I
want haze control to be in LTC Play, not the FSEQ. I want Andy to be able
to set the show levels of both Haze and Fan, in addition to arming which
ones run during the show." Before this every slot in no group was zero. A
haze machine is not a flame: it gets no safety slot, no edge check and no
dwell. It gets an arm that only the show program can set, and that
everything uncertain clears; the show program writes its levels.

The config's optional top-level `passthrough`, one entry per hazer, its
haze channel and its fan channel by role (swap the two numbers if a
fixture is the other way round):

```json
"passthrough": [
  {"name": "Hazer 1", "haze": 80, "fan": 81},
  {"name": "Hazer 2", "haze": 82, "fan": 83},
  {"name": "Hazer 3", "haze": 84, "fan": 85}
]
```

Refused at start (flamesafe does not start) unless: a list of at most 16
objects with exactly `name`, `haze` and `fan` (or, since 2026-10-30, a
switch with exactly `name`, `switch` and `on`: see "A switch" below); each name 1 to 64
characters and used by no other entry and no group; `haze` and `fan` each
a whole number 1 to 512, two different slots; no slot listed by another
entry; and no slot that is any group's safety, fire or aim slot. Left out,
there is none and every slot in no group is zero, exactly as before. An
entry's two slots pass and zero together; flamesafe never looks at the
roles, which are for the show program and the screens.

**The rule.** An entry's slots carry the values of the last accepted flame
frame only while ALL of these hold; otherwise they are zero:

1. the flame link is fresh (`frame_stale_ms`; stale or never is zero). The
   hold is `frame_stale_ms`, not `fire_hold_ms`: haze is not a flame;
2. the last accepted frame from the locked sender lists the entry in
   `hazers_on`;
3. since the last thing that took it away, an accepted frame from the
   locked sender has listed it OFF (left it out of `hazers_on`). What takes
   it away: flamesafe starting, the link going stale, the link's lock
   changing hands, a panic tick (an overrun or a compose fault, after
   which every group needs a fresh cycle too), and an accepted
   `disarm_all` (the show's Abort). So an
   Abort zeroes every hazer on the tick it arrives in, and a hazer stays
   zero, whatever later frames ask, until the show program has sent it
   OFF and then ON again. A frame still asking ON after an Abort (an
   engine that did not switch its hazers off) is not enough;
4. the slot is not in the lockout file (zero whatever else holds, the
   safe side; the engine's lockout names flame machines, so this is only
   ever a guard).

Only the locked flame-link sender can arm one: `hazers_on` rides on
the flame frame, after the key, the sender lock, the sequence and the
sender's clock. A foreign keyed frame is refused as `another sender` before
its `hazers_on` is used (and disarms every group as before). A refused
`disarm_all` (another sender, no live link) changes no hazer, by design:
only the locked sender's own frames and its accepted `disarm_all` decide
them; an older
ltcplay that sends no `hazers_on` switches every hazer off. A name this
config does not have is ignored.

**What it never does.** It never reads or writes any group's state: no
latch, no consent edge, no dwell, no safety or fire slot, no confirmation
step. Its slots are written before the groups', and the config refuses any
overlap, so a group's slot can never hold a hazer value. Arming never
depends on it and it never depends on arming. A panic tick (overrun,
compose fault) is all zeros, hazers included. Every other slot in no group
is still zero.

**Seeing it.** The status frame's top-level `passthrough` lists every
entry: `name`, `slots` (haze then fan), `haze`, `fan`, `asked` (in the last accepted frame's
`hazers_on`), `enabled` (switched on here: asked and not taken away),
`passing` (its slots carry the show's values on this tick), `locked` (one
per slot: true for a slot in the lockout file, which is zero whatever
else holds, and then `why` says so), `sent` (the
values on its slots this tick) and `why` (empty while passing, else one of:
`switched off in the show program`, `the show program link is not fresh`,
`not yet seen switched off by the show program since this program started:
switch it off and on again`, `zeroed by the show's Abort: switch it off
and on again in the show program`, `the show program link changed hands:
switch it off and on again in the show program`, `the safety program
faulted (an overrun or a compose fault): ...`, `a slot of it is in the
lockout file: held at zero`). A Composer also refuses, when it is built,
a passthrough slot that is any group's slot, however its Config was made.
flamesafe journals one
start line, `passthrough: Hazer 1 80, 81; ... read from <path>` (or
`passthrough: none in <path>`), and one line when an Abort switches off a
hazer that was on.

**A switch: the fireball's fuel pump (added 2026-10-30).** Jeff: "we
need a toggle for a fuel pump. This is something for Andy to manually
control as needed. It does not need to be armed or disarmed but it should
be affected by the stop button." A second entry kind, one slot that is
either 0 or the entry's own `on` value:

```json
"passthrough": [
  {"name": "Hazer 1", "haze": 80, "fan": 81},
  {"name": "Fireball Fuel Pump", "switch": 100, "on": 255}
]
```

An entry that has `switch` is a switch and must have exactly `name`,
`switch` (a whole number 1 to 512) and `on` (a whole number 1 to 255; not
a decimal, not true or false). Every other passthrough rule above applies
to it unchanged: the name is its own, its slot is no group's safety, fire
or aim slot and no other entry's slot, it counts towards the 16, and it is
switched on and off by name in the same `hazers_on` list, with the same
take-aways (start, stale link, change of sender, panic tick, `disarm_all`)
and the same OFF-then-ON rule. The one difference: while it is switched on
its slot carries **the entry's own `on` value, written by flamesafe**,
never the value the frame carries on that slot. So no frame, whatever it
holds there, can put any other level on the pump; the frame only says ON
or OFF. A slot in the lockout file is zero. A Composer refuses, when it is
built, a switch whose `on` is not 1 to 255 and two entries that share a
slot, however its Config was made. The status frame's entry for a switch
has `kind` `"switch"`, `slots` (its one slot), `switch` and `on` in place
of `haze` and `fan`; a hazer's entry gains `kind` `"hazer"` and is
otherwise unchanged. The start line names it as `Fireball Fuel Pump switch
100 at 255`.

**Deploy order for a switch.** A flamesafe or an LTC Player from before
this date refuses a config with a switch entry (an unknown key in a
passthrough entry; LTC Player: an entry without `haze` and `fan`) and does
not start. Update BOTH programs first, then add the entry to
`flamesafe.json`, then restart both.

**ltcplay's side** (`ltcplay/hazers.py`, `ltcplay/flamelink.py`). The
engine reads the same `passthrough` list from this file and holds, per
hazer, ARMED or DISARMED and a Haze and a Fan level (0 to 255, set in
percent on the rack screen, kept in `ltcplay_hazers.json` in its settings
folder). The hazer slots never carry show data: the engine zeroes them in
whatever the FSEQ, xLights or the Programming transport carries, and
writes an ARMED hazer's two levels there only while a show is playing (the
show audio playing a cue and the scheduled show in SHOW; not on Hold, not
between shows, not in Programming). Every flame frame carries `hazers_on`,
the names ARMED, whenever this file lists hazers. DISARMED at start, after
Abort, after Stop (both send `disarm_all`, which disarms them at the link
before its first copy), when live flame programming ends (it sends
`disarm_all` too; arming a hazer is refused while it is on), and while it
has no fresh status from flamesafe. While the engine's list and the
status frame's differ, the rack screen raises a display only alert of
kind "hazers".

## The arm link (build step 7b, landed 2026-10-01)

A fourth loopback socket, `link.arm_port` on `link.arm_ip` (default: the
same address as `link.listen_ip`), optional: a config without it runs with
`NullArmInput`, exactly as before this build step, and every group stays
disarmed. The Stream Deck (`ltcplay/streamdeck.py`, run as its own
process by `ltc deck`, separate from both the ltcplay show program that
sends the flame frames and flamesafe) sends one arm frame at 10 Hz or faster,
always carrying every group's current wanted state, whether or not
anything changed:

```json
{"v": 2, "k": "<link.key>", "t": "arm", "seq": 4410,
 "wanted": [true, false, false], "names": ["front row", "cat-walk", "wave flamer"]}
```

| Field | Type | Rule |
|---|---|---|
| `v` | integer | must be 2 |
| `k` | string | must equal flamesafe's `link.key` |
| `t` | string | must be `"arm"` |
| `seq` | integer, 0 or more | the deck's own liveness counter; see the arm input rules below |
| `wanted` | list of exactly N booleans | N is flamesafe's configured group count; positional, in config order |
| `names` | list of exactly N strings | the group names, in config order; required on this frame (unlike `arminput.ArmAssertion.names`, which is optional for the test driver) |

Rejected, with the reason journaled: not JSON, not an object, longer than
16384 bytes, wrong `v`, wrong or missing `k`, wrong `t`, `seq` missing or
negative, `wanted` not exactly N booleans, `names` not exactly N strings, a
sender other than the locked one (below), group names that do not match
this config's (checked one layer up, in the arm input rules below, and
also journaled). A rejected datagram changes nothing, exactly as for a
rejected flame frame.

**The sender lock (added 2026-10-01, after a second safety review).** This
section used to end here: "Unlike the flame-frame link there is no sender
lock: there is exactly one Stream Deck in this show, and the key already
keeps out anything that has not read flamesafe's config... the cost of
being wrong here is a DISARM, never a fire." Running the actual code
proved that false. `wanted` can ask for EITHER state, so a second local
process that has read flamesafe's config -- the key is not a secret in the
cryptographic sense, it lives in a file -- can send `wanted=True` exactly
as easily as `wanted=False`. Worse, because the service keeps only the
LAST arm datagram it could decode each tick, a rogue sender racing the
real Stream Deck can win a tick outright: if the operator's own Abort
sends `wanted` all false and a rogue frame lands after it in the same
tick, or the rogue simply keeps re-asserting `True` faster than anyone
notices, the composer sees only the rogue's `True` that tick, and a group
can read ARMED again a moment after the operator just told it not to be --
an Abort visibly undone by a datagram the operator never sent, on the
screen they are watching. That is not "only ever a disarm"; it is a way
to MASK an Abort.

So the arm link now has the same sender lock the flame-frame link always
had: while a datagram has been accepted from one (ip, port) inside
`arm_stale_ms` of another, a datagram from anywhere else is rejected as
`another sender` and journaled, changing nothing. Once nothing has been
accepted for `arm_stale_ms` the lock releases, so a restarted deck on a
new port still takes over. **This does NOT bound how long a rogue can
hold the lock once it is in.** An earlier draft of this section claimed
"the window in which a genuinely different sender could slip in is never
wider than the window in which a disconnect would have disarmed every
group anyway" -- that is false, and a third safety review (round 2) found
the gap it was hiding: the lock only ever decides whether a SECOND sender
is accepted while a FIRST one is already live; it says nothing about how
that first one got there. If the real deck is silent for `arm_stale_ms`
(a reconnect, a restart, ordinary boot ordering before the real deck has
sent its first frame), a rogue racing it can become the locked sender
itself, for real, and arm groups the operator never asked for -- and it
then holds that lock for as long as it keeps re-asserting faster than
`arm_stale_ms`, which is indefinite, not bounded by anything. Worse, the
real deck's own Abort, once it reconnects, is then rejected as "another
sender" and does nothing: an Abort that visibly fails to disarm what the
rogue armed.

The actual fix (round 2) is in `SocketArmInput.poll()`: a datagram from
any sender OTHER than the currently-locked one can still never ARM
anything, but it can always DISARM. `poll()` tracks each foreign sender's
last-reported `wanted` vector (while it keeps re-asserting inside its own
`arm_stale_ms`) and ANDs every tracked foreign vector, bit for bit, into
whatever assertion it returns: a foreign sender saying a group is
`wanted=false` forces that group's bit false in the result no matter what
the locked sender is asking for. This is what actually restores "a
foreign frame can only ever disarm, never arm" even while a rogue holds
the nominal lock -- the lock by itself only ever answered "is this the
sender I already trust", never "should an ARM from a rogue be trusted",
and never claimed to. This mechanism lives entirely in
`flamesafe/arminput.py`'s `SocketArmInput`, not in the composer: every
consent, dwell, chatter and edge-quiet rule is unchanged by this fix, and
the lock holder still has to prove consent (rule 6 below) all over again.
On the Stream Deck's own side (`ltcplay/streamdeck.py`), this is a second
line of defence, not the only one: the deck compares flamesafe's reported
arm counter and per-group `wanted` states against what it itself last
sent, and raises a visible alarm the moment they diverge, because it
should never see state on the wire that it did not set and does not
expect -- and, as of round 2, that alarm (and any group flamesafe is
still actually reporting armed) stays visible on the deck's screen even
after an Abort has latched it: a latched screen must never paint a flat
OFF over a group that is still really armed. As of round 3 the deck also
raises this alarm on `arm_input.foreign_senders` alone (above), since the
AND can leave `wanted` looking exactly like what the deck expects with
nothing else to notice.

**The forced-edge fix (round 3, after a fourth safety review).** The AND
above closes "a rogue can mask an Abort", but a fourth review found it
opened a different hole: a foreign sender saying a group is `wanted=false`
for a while, then `wanted=true` again, puts a false-then-true sequence in
front of the composer's consent rule (rule 6 below) -- and that rule reads
ANY false-then-true sequence on a live counter as the operator cycling the
arm, with no way to tell a bit that went low because the AND forced it
from one the LOCKED sender genuinely reported low. The locked sender's own
report never has to change at all for this to arm a group. Fixed by
`SocketArmInput.poll()` also reporting, per group, which False bits in the
result it just forced (`ArmAssertion.forced`); `Composer.assert_arm` never
lets a forced low set up a future consent edge, so a forced low-then-high
sequence can never read as the operator cycling the arm -- only a fresh,
UN-forced low-to-high transition from the locked sender itself can. This
is additive to the AND above, not a replacement for it: a foreign sender
can still only ever clear a bit, never set one.

**Round 4 (after the round-4 independent review): nobody arms anything
while anyone else is on the link.** The sentence in the status table that
a foreign sender "never becomes the locked sender itself" was false. The
lock goes to whoever sends next once the locked sender has been quiet for
`arm_stale_ms`, and two attacks proved a rogue can get there:

- *The deck gap.* The Stream Deck used to close its arm socket and send
  nothing while it was unplugged. The lock lapsed, a rogue took it, sent
  its own low then high, and every group armed on the wire with no deck
  plugged in. Fixed on the deck side (`ltcplay/streamdeck.py`): while no
  deck is connected the deck process keeps sending every group OFF at its
  normal rate on the SAME socket, and a reconnect restarts `wanted` and
  `seq` on that socket instead of opening a new one, so the lock does not
  lapse while the deck process is running, whether or not a deck is
  plugged in. Since round 5 any error from the deck hardware or its main
  loop is handled the same way, not only a clean unplug; only a deliberate
  stop ends the deck process. **Residual, stated plainly: the deck process
  is its own process (`ltc deck`), separate from the show program that
  sends the flame frames. If the deck process alone dies, is killed or
  freezes, the show link stays up, every group disarms within
  `arm_stale_ms`, and the arm lock lapses with it. A local process holding
  the key can then take the lock with nothing on the link to compete
  against, and the "no consent while anyone else is on the link" rule
  below has nobody else to see.** It still has to earn consent the normal
  way, a genuine low then high from the lock holder on a live counter
  (rule 6), and nothing it does can arm a group before that. This is a
  residual, not something the link prevents: the key lives in a file that
  any process running as the same user can read. Only when the show
  program itself dies does the show link go too, and link loss disarms
  every group (section above). **Decided 2026-10-03 (Jeff):** the show
  machines are dedicated and run nothing else, so the realistic process
  "holding the key" is a second copy of `ltc deck` or ltcplay. A second
  `ltc deck` now refuses to start while one is running, and a crashed one
  never blocks a restart (see "Second copies" above). Protecting the key
  with a separate user is not built. The residual stands only for a program
  that is neither, which a dedicated show machine does not have.
- *The flood.* About 300,000 valid frames in 2 s crowd the real deck's
  frames out of the receive buffer; the deck looks quiet for
  `arm_stale_ms`, the arm input goes stale (every group disarms), and the
  rogue becomes the locked sender. It then forged a low then a high on a
  group the deck still wanted, and that group re-armed with no operator
  action.

The fix for both is in `Composer.assert_arm`: a down edge only counts as
consent while `arm_input.foreign_senders` is zero AND `arm_input.flooded`
is false, and while either is not, every pending down edge is cleared too,
so nothing seen before the other sender turned up can be finished while it
is there. After a takeover the real deck is the foreign sender, and it
keeps sending (20 Hz, OFF frames included, unplugged or not), so a rogue
that holds the lock can never collect consent for as long as the deck
process (`ltc deck`) is alive; if it is not, see the residual above. Also: when the lock changes hands, the composer clears every latch
and pending edge exactly as for an input restart, so a consent edge can
never be half proved by one sender and finished by another
(`ArmAssertion.sender`). The cost, accepted: a genuine cycle made while
another sender is on the link does not count, and the operator cycles
again once it has gone. Whether the lock itself should refuse to change
hands during a flood was considered and left alone: a rule that pins the
lock cannot tell "the real deck is crowded out" from "a rogue that grabbed
the lock went quiet", and the second must hand the lock back to the real
deck. Blocking consent instead is safe whichever one holds it.

A flood is more than 50 datagrams, or more than 64 KiB, waiting in one
poll (round 5: the kernel's receive buffer fills by bytes, and 12
maximum-size frames filled the default one without ever reaching 50
datagrams). The arm socket also asks the kernel for a 4 MiB receive
buffer.

Every rejection on the arm link (wrong key, wrong shape, garbage, another
sender, a flood) is journaled once per reason per episode with a count on
the closing line; an episode ends only after 5 s with none of that reason,
no reason writes more than 4 lines a minute however the datagrams are
spaced, and all the reasons together write no more than 8 a minute (round
5). A flood of undecodable datagrams used to write one line per datagram
and push real events out of the bounded journal queue. On top of that the
journal never lets arm-link lines take more than half its queue, so the
other half is always free for "show program stopped answering", arm and
disarm lines and faults behind a blocked console.

**This frame says only what the deck wants.** It decides nothing: every
rule below (consent, the dirty-edge gate, the re-arm dwell, chatter,
`arm_stale_ms`) runs in the composer exactly as it does for the test
driver. The Stream Deck's bottom-row keys set `wanted` for one group each;
they never encode "armed", "cycling" or "waiting" on the wire; those are
read back from the status frame's per-group `armed`, `reason`, `amber` and
`dwell_s` and drawn on the key. **Arming is a hold, disarming is a tap**
(Jeff, 2026-10-01, safety review of PR #31, item 8): pressing an OFF (or
SHOW LOST) group key starts a short hold-to-arm timer on the DECK side
only (the same shape as the Abort key's own hold, with the same kind of
fill feedback); only once it completes does the deck send `wanted=True`
for that group, and letting go early sends nothing. Pressing an
armed-or-held key sends `wanted=False` at once, no hold, same as always.
A group key also refuses to even START a hold for a short refractory
window right after that same key's last disarm, so pressing it again "to
be sure" during a panic can never quietly re-arm it. None of this changes
what is on the wire or what the composer does with it; `wanted` is still
a plain boolean vector, asserted continuously, exactly as this whole
section already describes -- only the deck's own button feel changed.

**The Stream Deck's Abort disarms over this same link.** Pressing and
holding the Stream Deck's ABORT key sends `wanted` all false at once, on
the same socket, before anything else happens. An Abort from the Rack
screen or Phone (the show conductor's own Abort, `ltcplay/conductor.py`)
disarms with `disarm_all` on the flame link instead: see "Disarm every
group: the show program's Abort" below for the message and for why it is
not "all false" on this link. (This paragraph used to say the screen's
Abort had no such path and could only zero the cues; `disarm_all`, added
2026-10-02 on Jeff's rule of 2026-09-27, "on Abort, flame cues zero and
every group disarms instantly", is that path.)

**Abort must respond whenever there is anything to abort** (Jeff,
2026-10-01, safety review of PR #31, item 2): the deck's own hold-to-fire
gate on the Abort key used to ask ltcplay's scheduler whether a show was
PLAYING or HELD, and refused to even start the hold otherwise. That left
Abort dead exactly when it still mattered: a group armed before a show
has started, a group left armed between two shows, or any group at all
while ltcplay's own web server (and so the scheduler's state) could not
be reached. The gate is now whether any flame group is wanted or armed,
as the deck itself knows it, never a scheduler's opinion; a scheduler
that says a show is running is an ADDITIONAL reason to light the key (for
a wired conductor's own lasers/video/pixels/music cascade), never the
only one. Pressing Abort with nothing armed or wanted is refused and
journaled, so it is never a silent no-op.

**Losing the Stream Deck disarms every group at once.** Since round 4
the deck process keeps sending, while no deck is connected, every group
`wanted` false on the same socket, so every group disarms on the next
frame and the sender lock stays with the deck process. If the deck
PROCESS dies or freezes (it runs on its own, as `ltc deck`), `poll()`
returns None once nothing has arrived for the input's own bookkeeping to
call fresh, and the EXISTING `arm_stale_ms` rule below does the rest --
every group disarms within `arm_stale_ms` of the last accepted frame, the
same as a crashed or frozen test driver: silence is silence. The show link
stays up in that case, and the arm lock lapses after `arm_stale_ms`, so a
local process holding the key can then take the lock with nothing to
compete against; it still needs a genuine low then high of its own to arm
anything (the residual stated under round 4 above, and what was decided
about it on 2026-10-03). **Reconnecting never re-arms anything by
itself** (rule 6, consent): the deck's own `seq` restarts at 0 on every
connect and reconnect (rule 3 below), so even a deck that remembered its
last button states and resent them immediately would fail consent, which
needs the counter proven to advance while already fresh -- the first
assertion after any gap proves nothing. The Stream Deck driver additionally
never tries to remember pre-disconnect state: on open (first connect, or a
reconnect after the hardware was lost) it starts every group `wanted`
false, so the operator sees every key read OFF and re-arms by pressing it,
matching what the lamp already says ("cycle the arm").

## Arming from a screen (added 2026-10-03)

Jeff approved arming from the rack touch screen and from an iPad on the
same consent rules as the Stream Deck. Since 2026-10-07 there are no PINs or
sign-ins: screen arming needs only a listed address plus the hold. **Nothing in flamesafe changed for it, and no rule was
loosened.** This section says why, and how.

**The design: a screen hold is a remote press of the deck's own key.** The
page never sends anything to flamesafe. A screen's "hold to
arm" goes to ltcplay's engine (`ltcplay/remote.py`, `arm-hold`, repeated
every 100 ms while the finger is down). The Stream Deck process
(`ltcplay/streamdeck.py`, `ScreenKeys`, read 20 times a second from the
engine's `deck-input`, loopback only) treats a live screen hold exactly as
a finger on that group's key: the deck's own hold timer, re-arm
refractory window, latched refusal and operator gate all apply, and when
the hold completes the deck sets that group's `wanted` bit on its own arm
socket, the same as for its own key. A per-group Disarm on the page is an
instant tap of that key. The engine answers it only once it is known
(2026-10-29, the same rule as Disarm all flame groups): "confirmed" when
flamesafe's own status, received after the deck read the Disarm, reports
the group disarmed and not wanted within 1 s; when the deck has not read
the engine in the last 0.5 s, flamesafe's status is not reaching the
engine, or that word does not come, the engine sends the flame link's
`disarm_all` (below; there is no one-group disarm on the link, so every
group needs a fresh cycle, every hazer goes off and the show's flames stay
at zero until Hold then Resume or a new run), raises a flames alert and
says "forced" and exactly that. Nothing new reaches flamesafe: the plain disarm is still the
deck's tap, and the forced one is the Abort's `disarm_all`. So:

- There is still exactly **one sender on the arm link**, the deck process.
  The sender lock, the round-2 AND, the forced-edge rule, the round-4 veto
  (no consent while `foreign_senders` is non-zero or `flooded`), the
  post-Abort window after a `disarm_all`, the second-copy guard and
  `arm_stale_ms` all see the same single stream they always have and work
  unchanged. flamesafe cannot tell, and does not need to tell, whether a
  bit was set by a finger on the deck or on a screen.
- There is **one `wanted` latch per group**, the deck's. The deck's own
  Abort (all false) disarms a group the screen armed, the deck's tap on
  that group's key disarms it, and the deck's keys draw it, because it is
  the deck's own bit.

**Why not a second sender on the arm link.** Three reasons, each enough on
its own:

1. The round-4 veto refuses every consent edge while another sender is on
   the link, and the deck process sends at 20 Hz all the time, unplugged or
   not. A second arm sender could never arm anything while the deck runs,
   and lifting the veto for it would reopen exactly the takeover the
   round-4 review proved.
2. Two senders would mean two `wanted` latches. A group armed from the
   screen would be one the deck's own bit says is off: the deck's Abort
   (all false) would leave it armed, and the deck's key could not disarm
   it without the deck changing anyway.
3. A second port with its own lock and merge rules would be a new consent
   path inside flamesafe to review and get right; this design adds none.

**What the screen hold must prove, at the engine** (all re-checked on
every heartbeat; any failure lets the hold go and is journaled):

- a listed address: the engine listens only on the one listed
  show-network address and on 127.0.0.1, and refuses anything proxied,
  cross-site or misaddressed (no PIN or sign-in since 2026-10-07);
- `screen_arming` on in `ltcplay_remote.json`: **off by default** since the
  review of PR #43 (2026-10-05, P0-4), until PR #41's own safety review
  decides; only the JSON word `true` turns it on, and a broken file reads as
  off. It is set on the show machine itself (2026-10-22: the rack screen's
  Programming, "Arming from a screen"; route `screen-arming`, loopback only,
  the role matrix's Settings to turn it on, anyone to turn it off). While it
  is off the engine refuses every `arm-hold` before looking at anything
  else, and says how to turn it on, refuses a screen's per-group Disarm
  (Disarm every flame group and the Stream Deck still work), and `ltc deck`
  takes no screen hold (the engine answers `enabled` false);
- no Abort standing, the Stream Deck program heard within 2 s, this device
  not in Arrange mode on the Morning page, and not every machine of the
  group locked out (2026-10-22): each is refused by the deck or flamesafe
  anyway, and the engine says it by name before a hold fills its bar. What
  only the deck knows (its re-arm wait, its latch, its operator gate) it
  reports back on its next `deck-input` read (`refused=<hold id>.<code>`),
  and the hold's next heartbeat says it. A hold is credited with arming
  a group only when the deck reports it took THAT hold (`taken=<hold id>`).
  Reporting only: nothing the deck or flamesafe decides changed;
- the page's own status no older than 1 s, and flamesafe's status (the
  `status_mirror_port` copy) no older than 1 s, so the page is showing
  flamesafe's real state, not a cached one;
- the group not already armed or wanted;
- one hold per group at a time: a second browser holding the same group is
  refused; a Disarm from anyone cancels every hold on that group, and an
  Abort or Disarm every flame group cancels every hold.

**Connection loss never completes an arm.** The engine lets a hold go 0.25
s after its last heartbeat, and a heartbeat for a hold that was let go is
refused ("lift your finger and hold again"): an interrupted hold never
carries on. The deck lets a screen hold go the moment the engine stops
reporting it fresh, or the moment it has not heard from the engine for 0.3
s. And the deck fires a screen hold only when BOTH its own `ARM_HOLD_S`
has run since it saw the press AND the engine reports `held_s` of at least
1 s, where `held_s` is measured to the last heartbeat the engine actually
received. So a hold whose page drops before 1 s of heartbeats has arrived
can never complete, whatever the timing of the deck's polls.

**What it cannot do.** The physical key switch on Andy's flame system sits
outside all of this: with it off nothing fires, whatever any software
says. With the deck unplugged, the deck process holds every group off
(round 4) and does not run screen holds, so nothing arms from a screen
either. If the deck process is not running, nothing reads the screen
holds and nothing arms.

## Disarm every group: the show program's Abort (added 2026-10-02)

Jeff, 2026-09-27: "on Abort, flame cues zero and every group disarms
instantly". The Stream Deck's Abort already does this on the arm link. The
show conductor's Abort (the rack screen, the phone) does it with one more
message on the FLAME link, ltcplay to flamesafe, `link.listen_port`, from
the same socket as the flame frames:

```json
{"v": 2, "k": "<link.key>", "t": "disarm_all", "seq": 1235,
 "mono": 812.4721, "id": 1, "reason": "Abort from the rack screen"}
```

| Field | Type | Rule |
|---|---|---|
| `v` | integer | must be 2 |
| `k` | string | must equal flamesafe's `link.key`; checked before any other field |
| `t` | string | must be `"disarm_all"` |
| `seq` | integer, 0 or more | the SAME sequence as the flame frames: ltcplay takes the next number for every datagram it sends on this link, flame or disarm_all. Must be greater than the last accepted one. ltcplay starts it at a random large number each run, so a sender counting up from 1 is never mistaken for it |
| `mono` | number | the sender's `time.perf_counter()`, finite; must not go backwards (same rule as the flame frame) |
| `id` | integer, 1 or more | which Abort this is. ltcplay starts its ids at a random large number each run, so no other run's Abort has the same id. The sender sends three copies at once and then one more after every flame frame for at least 0.75 s, and always 0.25 s past `frame_stale_ms`, in case datagrams are lost: losing every copy then means losing enough flame frames for the link itself to be lost, which disarms every group anyway. Every copy is applied; only the first of each (`id`, sender) is journaled |
| `reason` | string, 1 to 200 characters | for the journal, in words |

No other field is allowed: a disarm_all carrying anything else is refused.

**Accepted only from the live, locked flame-link sender.** The key and the
shape are checked first (a wrong key is "wrong key" whatever else is in
it); then, exactly as for a flame frame, the sender must be the (ip, port)
that holds the flame link's sender lock, the `seq` must be above the last
accepted one and `mono` must not go backwards. With the flame link not
live (`never` or `stale`) it is refused as `no live flame link to accept it
from`: there is no locked sender to take it from, and nothing is armed
anyway, because link loss already disarmed every group. A refused
disarm_all is never taken as the show's Abort (it does not set the Abort
words or `disarm_all.last_id`), is counted (`stats.disarm_all_rejected`), sets
`frames.last_reject` (prefixed `disarm_all:`) and is journaled once per
episode (above). It does not take the sender lock, refresh the link's
liveness or carry any fire values: only flame frames do those. One refused
as `another sender` is a second keyed sender on the flame link, and since
fix round 1 of PR #40 that disarms every group ("Second copies" above);
any other refusal changes nothing.

**What it does, and all it does.** On the tick it arrives in (the service
drains the flame link before it composes): every group's latch is cleared,
every pending consent edge is cleared, and the safety slot of every group
is zero on that tick's packet. It never sets a latch, never sets a consent
edge and never touches `wanted`. The re-arm dwell needs no help from it:
the low half of the fresh cycle below is a True-to-False report for any
group that was armed, and that starts the dwell, so no safety slot rises
again within `min_arm_dwell_ms` of the Abort. So:

- **It cannot arm anything.** There is no code path from it to a latch.
- **Each group needs a fresh, genuine arm cycle from the Stream Deck
  afterwards.** A consent edge seen before the Abort is forgotten by it, so
  an arm press that completes after the Abort does not count; the deck's
  key has to report the group down AFTER the Abort (the operator taps the
  held key, which sends `wanted=false`) and then up again (hold to arm),
  and the re-arm dwell must have passed. A forced low (round 3, above) is
  still never a cycle.
- **A low that was already going on at the Abort is not consent for
  `min_arm_dwell_ms` after it** (fix round 1 of PR #34). The Stream Deck
  reports a group `wanted=false` all through an arm-HOLD, so a hold the
  operator began before a screen Abort kept re-proving its low on every
  frame after the Abort and armed the group when the hold completed, about
  0.35 s after the Abort, with nothing more done. Now, until
  `min_arm_dwell_ms` has passed since the last accepted disarm_all, a low
  that began before it does not count; a low that begins after it (a
  `true`-to-`false` report) counts as before. The deck's hold is 0.6 s and
  the dwell is never under 1 s, so a hold begun before the Abort normally
  completes inside this window and is refused: the group reads `held`
  with the Abort sentence until it is cycled. Every copy of an Abort
  restarts the window, so after a screen Abort an arm-hold has to complete
  at least `max(0.75 s, frame_stale_ms + 0.25 s)` (the copies) plus
  `min_arm_dwell_ms` after it: about 1.75 s with `frame_stale_ms` 500 and
  a 1 s dwell, 3.75 s with `frame_stale_ms` 2500.

  **The edge** (fix round 2, review probe p12). That guarantee assumes the
  window is measured from copies that arrive. In the worst case only the
  three immediate copies arrive (every repeat lost, with the link itself
  still up) AND the deck delivers nothing for about 0.45 s right after the
  Abort (a stall shorter than `arm_stale_ms`, so not itself a disarm),
  then finishes a 0.6 s hold timed from when it got the press: the hold's
  `true` then lands just past the 1 s window and arms (measured: a stall of
  0.40 s was refused, 0.45 s armed at +1.05 s). With the repeat copies
  arriving, the same 0.45 s stall is refused. The deck's 0.6 s hold is
  therefore a safety constant: selftest fails if it plus 0.3 s of deck
  lateness no longer fits inside the shortest dwell flamesafe allows (a
  2.0 s hold armed at +1.9 s after an Abort).

  A group the deck keeps asking for reads `held`,
  flashing amber, reason `Disarmed by the show's Abort. Cycle the arm to
  re-arm.` until it is cycled; the deck shows it as ABORTED.
- The status frame's top-level `disarm_all` says what flamesafe took:
  `{"accepted": count, "last_id": id or null, "last_reason": "...",
  "age_ms": ms or null}`. ltcplay compares `last_id` with the Abort it sent
  (equal, never "greater or equal": another run's id says nothing about
  this one), says the Abort was sent but not confirmed until it matches,
  and journals a fault if it is not confirmed within 1 s.

**Why a new message on the flame link, and not ltcplay sending `wanted`
all false on the arm link.** The arm link is sender-locked to the Stream
Deck's own socket. ltcplay's conductor sending there would be a second
sender: its frames would be "another sender", journaled as a rogue, and
counted in `arm_input.foreign_senders`, which raises the deck's spoof
alarm. Every screen Abort would then light the one alarm that means
"something else is on the arm link", and an operator who sees it every
Abort learns to ignore it. It would also make ltcplay's conductor a second
voice saying what is WANTED, which only the operator's deck may say. The
flame link is the link ltcplay already owns, keyed and sender-locked, and a
message on it that can only ever take arm away keeps the arm link meaning
exactly one thing.

## The arm input (for build step 7b)

Not on this link; an in-process interface (`flamesafe/arminput.py`), but
its rules belong here because they are what the consent rule rests on:

- `poll()` returns None while the deck is not connected. It never
  synthesises a report (no "all down" on boot or unplug). Silence disarms
  in `arm_stale_ms`.
- `wanted` is positional, one bool per group in config order, and the
  assertion carries the group `names` in the same order; the composer
  rejects an assertion whose names do not match its config exactly.
- `seq` is a plain int, starts at 0 on every connect and reconnect, and
  goes up by one per assertion. A counter that restarts at 0 is how the
  composer knows the input restarted. Consent (a down edge that lets a
  group arm) needs the counter to have advanced now AND to have been fresh
  before this assertion, so the first assertion after a boot, a restart, a
  gap, or a counter frozen for longer than `arm_stale_ms` proves nothing
  whatever its value. **The one case that rule cannot close:** a counter
  that jumps UP across a reboot with no gap longer than `arm_stale_ms`,
  which to the composer looks like an input that never stopped. That is
  why the driver restarts at 0, and why it must never use a time-based
  counter.
- Assert at 10 Hz or faster (`arm_stale_ms` default 500 ms).
- On Windows only Ctrl-C and Ctrl-Break reach the stop handler, so the
  deck must offer an in-band stop that sets the service's stop event.

## What the wire carries

flamesafe sends the whole flame universe as one ANSI E1.31 data packet per
tick, priority 200, to the configured unicast destination, with its own
CID (a uuid5 of `flamesafe.jeffholmespresents`) and source name
`flamesafe`. Every channel that belongs to no group is always zero, except
a passthrough entry's slots while it is switched on (2026-10-29, above). A
group's safety slot is 0 or the arm value. A group's fire slots carry
ltcplay's values only while that group's safety slot carries the arm value
on the same packet and the edge-quiet window (3 ticks from the rise) has
passed; otherwise zero.

On a clean stop (Ctrl-C, SIGTERM, the stop event) flamesafe sends three
all-zero packets, then three all-zero packets with the stream-terminated
bit set.

**A hard kill sends no zeros.** End task, an interpreter crash, or a power
cut leaves the last packet standing until the node's own sACN-loss timeout,
and a flame that was on stays on until the head's Max. Flame Duration ends
it. Those two settings are the bounds. Bench items, before 2026-10-14:

1. Set the PixLite Aux port's sACN-loss behaviour to zero the outputs, and
   measure the timeout.
2. Set Max. Flame Duration on every G-Flame to the longest cue plus margin
   (never `----`), and measure it with gas off.
3. Confirm the PixLite Aux port honours sACN priority: a source at 100 must
   lose to flamesafe at 200.

**On equal priority.** Two sources at the same priority are merged HTP
(highest takes precedence) by many nodes, so a second source at 200 would
not lose, it would add. Priority 200 defends against a source at the
default 100, nothing more. The keyed-off rule stays the real defence: while
other computers are on the network, the flame node is keyed off.

## Timing knobs, all in the flamesafe config

| Key | Example | Meaning |
|---|---|---|
| `tick_hz` | 40 | sACN and status rate |
| `arm_stale_ms` | 500 | no fresh arm assertion for this long: every group disarms and needs a cycle |
| `fire_hold_ms` | 100 | no accepted flame frame for this long: every fire slot is zero |
| `frame_stale_ms` | 500 | no accepted flame frame for this long: the link is lost, every group disarms and needs a cycle once it is back, the sender lock is released, any seq is accepted |
| `overrun_ms` | 250 | a tick later than this: that tick is all zeros, every group needs a cycle |
| `min_arm_dwell_ms` | 1000 | after a disarm, the group is not raised again for this long. The file loader floors it at 1000 |

The config refuses any key it does not know (top level, `destination`,
`link`, and each group), so a misspelt optional key cannot be ignored in
silence.

## Versioning

`v` is the contract version. A change to any field's meaning, type or rule
is a new version. flamesafe rejects any other version outright; there is no
negotiation, because the two programs are installed together.

Version 1 (2026-09-25, superseded the same day): no `k`, no sender lock, no
`fire_hold_ms`, no `frames.fire`.

Version 2, 2026-09-26: link loss disarms every group (no field changed;
two new `reason` sentences).

Version 2, 2026-10-01 (build step 7b): the arm input becomes a real,
optional link -- a new `"t": "arm"` frame on `link.arm_port`/`link.arm_ip`,
keyed exactly like the flame and status frames. No existing frame's field,
meaning or type changed; a config without `link.arm_port` runs exactly as
before this step.

Version 2, 2026-10-01 (safety review follow-up, same day): the arm link
gets the flame-frame link's own sender lock (a datagram from a second
sender is now rejected as `another sender`, journaled), and a group-name
mismatch the composer rejects is now journaled too. No field's name, type
or wire meaning changed; a deck and a flamesafe that already spoke build
step 7b's arm frame correctly see no difference at all.

Version 2, 2026-10-02 (third safety review, round 2 of PR #31): the
sender lock above closed one hole and quietly left another -- a rogue
that becomes the locked sender while the real deck is briefly quiet holds
that lock indefinitely, and the real deck's own Abort is then rejected as
"another sender" once it reconnects. A rejected foreign datagram's
`wanted` is now tracked and ANDed (bits cleared, never set) into whatever
`SocketArmInput.poll()` returns, so a foreign disarm always takes effect;
no field's name, type or wire meaning changed. Also fixed the same round:
an Abort and an in-progress arm-hold completing in the same Stream Deck
main-loop pass could re-arm a group right after the Abort that was meant
to clear it (streamdeck.py only, no wire change); a latched Stream Deck
screen no longer paints a flat OFF over a group flamesafe is still
actually reporting armed, nor hides the spoof/divergence alarm, while
latched; a `confirmed: false` config now shows the real per-group state
instead of blanking it (CONTRACT.md's own "show it" was never "show it
instead of the real state"). The small amber corner mark this round added
to each key for it was removed again on 2026-10-19 (below).

Version 2, 2026-10-02 (round-4 independent review of PR #31): the
status frame gains `arm_input.flooded` and one new `reason` sentence
(another sender on the arm link); no existing field's name, type or wire
meaning changed. No consent edge counts while another sender is on the arm
link or a flood has been seen on it, a change of locked sender clears
every latch and pending edge, the Stream Deck keeps the arm link held OFF
on one socket while unplugged, and every arm-link rejection is journaled
once per reason per episode with a per-minute cap.

Version 2, 2026-10-02 (the flame link's sender, `ltcplay/flamelink.py`,
and the show's Abort): a new message on the flame link, `"t":
"disarm_all"`, keyed, strict, sender-locked and in sequence with the flame
frames; it can only clear arm state (see "Disarm every group" above). The
status frame gains a top-level `disarm_all` object and one new `reason`
sentence. Flame-link rejections are now journaled once per episode. No
existing field's name, type or meaning changed, so, following the 7b
precedent, this stays version 2: a flamesafe from before this date refuses
a disarm_all as `wrong message type 'disarm_all'` (counted, never acted
on), and the two programs are installed together.

Version 2, 2026-10-02 (fix round 1 of PR #34, from an independent safety
review): a low already going on at a disarm_all is not consent for
`min_arm_dwell_ms` after it; a disarm_all is journaled once per (`id`,
sender) rather than per `id`; the `tc` and the key are matched whole (a
trailing newline is refused, digits are ASCII only). On ltcplay's side,
one Abort is repeated after every flame frame past `frame_stale_ms`, seq
and ids start at a random large number, and only an equal `last_id`
confirms an Abort. No field's name, type or meaning changed.

Version 2, 2026-10-03 (fix round 2 of PR #34): documentation of the
post-Abort window's real length and its edge, and of what "timecode
moving" means; on ltcplay's side, its copy of `frame_stale_ms` is
required (read from flamesafe's own config) and a stuck sender reports
itself. No wire change.

Version 2, 2026-10-03 (the second-copy guard, PR #34's open question 10):
no consent edge counts while another keyed sender is on the flame link or
the flame link's lock has just changed hands (see "Second copies" above).
The status frame gains `frames.foreign_senders` and `frames.new_sender`
and one new `reason` sentence; no existing field's name, type or meaning
changed. On ltcplay's side the show program and `ltc deck` each refuse to
start while another copy is running.

Version 2, 2026-10-03 (fix round 1 of PR #40, Jeff's direction): a second
keyed sender on the flame link while the lock holder is live now DISARMS
every group instead of only blocking new arming, and a flood on the flame
link blocks consent. The status frame gains `frames.flooded`; the held
reason's words say every group is disarmed. No existing field's name or
type changed.

Version 2, 2026-10-17 (the Morning and Shutdown page, MORNING.md): the
config gains `lockout_file`, the path of a JSON file written by ltcplay's
engine (the one writer) naming `flame_channels` to hold at zero. Left out,
it is `ltcplay_lockout.json` beside the config file (where the show PC
keeps both); `false` switches it off on purpose; a relative path is beside
the config file. flamesafe polls it twice a second: a locked fire slot is
zero on every packet whatever is armed or commanded, and a group with every
fire slot locked is held (steady amber, "Every machine in this group is
locked out"). An unreadable file keeps the last lock set; a file that held
locks and disappears keeps them too (`missing_kept`, the engine never
deletes the file, it rewrites it) until the engine writes it again; a file
that never held locks and is missing locks nothing. The status frame gains
a top-level `lockout` object (`slots`, `state`: none, read, missing,
missing_kept or unreadable) and per group `locked_fire`; no existing
field's name, type or meaning changed. ltcplay's side also sends a PURGE frame on the flame link
(flamelink.purge_set): an ordinary flame frame, carrying fire values only
while no show cue is playing and for 0.25 s after the page's last
heartbeat, which flamesafe treats exactly like any cue frame (fire values
pass only for an armed group). Nothing on this link's wire changed for it.

Version 2, 2026-10-19 (Jeff: "Kill the little orange dot"): the Stream Deck
no longer draws the amber corner mark on its group keys for `confirmed:
false`; a key reads the group's real state and nothing else, exactly as it
does for a confirmed config. The unconfirmed config is still said, where it
can be read at leisure and never in the way: flamesafe's own journal at
startup (unchanged), one line in the Stream Deck's journal the first time a
fresh status frame says `confirmed: false` (and one more if it later says
true), `flames.confirmed` in the engine's `/api/remote/status`, and one line
in the rack screen's Info popup. Display only: `confirmed` never changed
what the deck sends, and no field on any wire changed.

Version 2, 2026-10-26 (multi-safety, the wave flamer): a group's `safety`
may be a list of distinct slots, one per fixture, armed as one group (see
"Multi-safety groups" under the status frame). The status frame gains per
group `safety_slots`, `safeties_up` and `safeties_locked`, and `stats`
gains `safety_lock_blocks` and `safety_unconfirmed`. A group whose
`safety` is one number behaves exactly as before; no existing field's name,
type or meaning changed (`safety_slot` is the first of the list for a
multi-safety group).

Version 2, 2026-10-26 (safety_profile, the wave flamer at 178): a group may
carry a `safety_profile` with its own arm value (see "A group's own arm
value" under the status frame). The status frame gains a per group
`arm_value`. The top-level `arm_value` keeps its name and meaning, the
file's arm value; no existing field's name, type or meaning changed.

Version 2, 2026-10-28 (aim slots): a group may carry `aim`, the fire slots
that only point a head (see "Aim slots" under the status frame). No field
on any wire changed.

Version 2, 2026-10-29 (passthrough, the hazers): the config gains
`passthrough` (name, haze, fan); the flame frame gains an optional
`hazers_on`; the status frame gains a top-level `passthrough`. No existing field's name, type or
meaning changed: a frame without `hazers_on` is read as before (every
hazer off), and a flamesafe from before this date ignores the field, so its
hazers stay zero (fail closed). A flamesafe from before this date refuses a
config with `passthrough` as an unknown key and does not start. Deploy and
restart flamesafe and ltcplay together.

Version 2, 2026-10-30 (passthrough switch, the fireball's fuel pump): a
passthrough entry may be a switch, `{name, switch, on}` (see "A switch"
under "Passthrough: the hazers"); the status frame's passthrough entries
gain `kind`, and a switch's carries `switch` and `on` in place of `haze`
and `fan`. No field on the flame link changed: a switch is switched by
name in the existing `hazers_on`. A config without a switch runs exactly
as before. A flamesafe or LTC Player from before this date refuses a
config with a switch and does not start: update both, then add the entry,
then restart both.

Version 2, 2026-10-28 (aim slots seen): the status frame gains a per group
`aim_slots`, and flamesafe journals one `aim slots:` line at start naming
its config file. No existing field's name, type or meaning changed, the
key, the lock and every rule are unchanged, and an older ltcplay reads the
frame as before.
