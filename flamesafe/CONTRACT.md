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
   released, so a restarted ltcplay on a new port takes it.

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

## Ports and addresses

From the flamesafe config (`flamesafe.example.json`):

| Direction | Address | Config key |
|---|---|---|
| ltcplay to flamesafe, flame frames | 127.0.0.1, `link.listen_port` (example 5571) | flamesafe binds it |
| flamesafe to ltcplay, status frames | 127.0.0.1, `link.status_port` (example 5572) | ltcplay binds it |
| flamesafe to the flame node, sACN | `destination.ip`, `destination.port` (5568) | unicast, priority 200 |

The link addresses must be loopback; the config refuses anything else, and
refuses a loopback destination on either link port.

## Flame frame, ltcplay to flamesafe

One per output frame, at ltcplay's frame rate, whether or not a show is
running. When ltcplay is idle it still sends frames (all zeros), because a
missing frame means "unknown" to flamesafe, and unknown is zero.

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

Rejected, with the reason in the next status frame's `frames.last_reject`:
not JSON, not an object, longer than 16384 bytes, wrong `v`, wrong or
missing `k`, wrong `t`, any field missing or of the wrong type, a `seq` at
or below the last accepted one, a `mono` below the last accepted one, a
`values` list that is not exactly 512 integers 0 to 255, a `universe` that
is not the flame universe, a sender other than the locked one.

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
flashing). A cycle made while the link is still down does not count. At
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
    "fire_slots": [411, 412, 413, 414, 415],
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
| `universe`, `priority`, `arm_value` | what flamesafe is configured to send. `priority` is always 200 |
| `confirmed` | false until the config's numbers are confirmed by Andy. Show it |
| `fault` | empty, or one sentence: an overrun, a compose fault, a failed sACN send, a failed status send. `fault_age_ms` says how long ago. **A non-empty fault is red for ltcplay**: an armed group is not "fine" while the wire is not being written. A fault clears itself after 5 s of clean ticks and clean sends (the journal records both the fault and its clearing), so one failed send is not red all night; the cumulative counts (`sacn.errors`, `sacn.status_errors`, `stats.overruns`, `stats.compose_faults`, `stats.faults_noted`, `stats.faults_cleared`, `stats.journal_dropped`) never reset, and ltcplay shows them in health |
| `arm_input.state` | `never`, `live` or `stale`. Stale means every group is disarmed |
| `frames.state` | `never`, `fresh` or `stale` (by `frame_stale_ms`). `stale` or `never` means every group is disarmed and needs a cycle once the link is back |
| `frames.fire` | `passing` while the last frame is younger than `fire_hold_ms`, else `zeroed`: every fire slot is zero |
| `frames.last_reject` | why the last rejected datagram was rejected |
| `sacn.sent`, `sacn.errors`, `sacn.status_errors` | packets sent to the node, sends that failed, status sends that failed. Cumulative, never reset; shown in health. A failed send also sets `fault` for 5 s, which is the red |
| `stats.journal_dropped` | journal lines lost while the console was blocked, cumulative. When the backlog drains the journal writes how many were lost |

Per group, the two lamps of section 8 panel 5:

| Field | Meaning |
|---|---|
| `wanted` | what the arm input is asking for |
| `armed` | the ARMED lamp: `disarmed` (dim blue), `armed` (green: the safety slot carries the arm value), `held` (amber: arm asked for and refused) |
| `reason` | why held, in words: `cycle the arm`, `dirty edge`, `re-arm dwell`, `chatter`, `arm input stale`, `arm input has never asserted`, `safety program fault`, `Show program stopped answering: disarmed. Cycle the arm to re-arm once it is back.`, `Show program has not answered yet: disarmed. Cycle the arm once it is running.` |
| `amber` | `flashing` when cycling the arm is the fix (`cycle the arm`, `dirty edge`); `steady` when cycling would only restart the wait or fix nothing (`re-arm dwell`, `chatter`, and every veto). Empty unless held |
| `dwell_s` | whole seconds left in the re-arm dwell, 1 or more while held for it, else 0. The lamp shows this number; the screen never counts down on its own |
| `sent_safety` | the SENT safety value this tick: 0 or the arm value |
| `sent_fire` | the SENT fire values this tick, one per fire slot |
| `commanded_fire` | what ltcplay asked for on those slots this tick (COMMANDED). Not a lamp; for the journal and the amber row flag (armed, commanded fire, sent none, for more than 3 frames) |

What ltcplay does with it: display, journal, the amber row flag. There is no
path from a status frame to flame output, because ltcplay never writes the
flame universe.

If no status frame arrives for 1 s, ltcplay shows red for the safety
program and keeps running the rest of the show. It never takes over the
flame universe. There is no fallback path, deliberately.

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
`flamesafe`. Every channel that belongs to no group is always zero. A
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
