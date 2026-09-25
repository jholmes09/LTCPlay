# The flamesafe link contract

Contract version 1. 2026-09-25, build step 7a.

This is the only thing ltcplay and flamesafe share. They share it as a
document, not as code: ltcplay implements its side from this page, and
neither program imports the other (a test fails the build if one does).

Both directions are UDP datagrams on loopback. Each datagram is one JSON
object, UTF-8, no framing. A datagram that is not exactly as written here is
rejected with a reason and changes nothing. flamesafe never raises on input.

## Ports and addresses

From the flamesafe config (`flamesafe.example.json`):

| Direction | Address | Config key |
|---|---|---|
| ltcplay to flamesafe, flame frames | 127.0.0.1, `link.listen_port` (example 5571) | flamesafe binds it |
| flamesafe to ltcplay, status frames | 127.0.0.1, `link.status_port` (example 5572) | ltcplay binds it |
| flamesafe to the flame node, sACN | `destination.ip`, `destination.port` (5568) | unicast, priority 200 |

The link addresses must be loopback; the config refuses anything else.

## Flame frame, ltcplay to flamesafe

One per output frame, at ltcplay's frame rate, whether or not a show is
running. When ltcplay is idle it still sends frames (all zeros), because a
missing frame means "unknown" to flamesafe, and unknown is zero.

```json
{"v": 1, "t": "flame", "seq": 1234, "tc": "00:01:02:03", "mono": 812.4471,
 "universe": 1, "values": [0, 0, 0, ...]}
```

| Field | Type | Rule |
|---|---|---|
| `v` | integer | must be 1 |
| `t` | string | must be `"flame"` |
| `seq` | integer, 0 or more | increases by one per frame; must be greater than the last accepted seq while the link is live |
| `tc` | string or null | `HH:MM:SS:FF`, or `HH:MM:SS;FF` for drop frame, or null when there is no timecode |
| `mono` | number | the sender's own `time.perf_counter()` at the frame, seconds; must not go backwards while the link is live |
| `universe` | integer | must equal flamesafe's configured flame universe |
| `values` | list of exactly 512 integers, each 0 to 255 | the whole flame universe, slot 1 first |

Rejected, with the reason in the next status frame's `frames.last_reject`:
not JSON, not an object, longer than 16384 bytes, wrong `v`, wrong `t`, any
field missing or of the wrong type, a `seq` at or below the last accepted
one, a `mono` below the last accepted one, a `values` list that is not
exactly 512 integers 0 to 255, a `universe` that is not the flame universe.

Live and stale: the link is live while the last accepted frame is younger
than `frame_stale_ms` (example 500 ms). Once it is stale, any `seq` is
accepted, so a restarted ltcplay re-syncs by itself. While the link is
stale, every fire slot is sent as zero. Arming is not affected by ltcplay
going quiet: the safety slot keeps the arm value, the fire slots are zero.

Clocks: `mono` is compared only with earlier `mono` values from the same
sender. flamesafe never compares it with its own clock.

## Status frame, flamesafe to ltcplay

One per flamesafe tick (`tick_hz`, example 40 Hz), whether or not ltcplay is
sending. The heartbeat counts ticks; a heartbeat that stops means flamesafe
has stopped. The screen never estimates anything from this frame; it shows
what is in it.

```json
{"v": 1, "t": "status", "heartbeat": 88123, "tick_ms": 25.0,
 "universe": 1, "priority": 200, "arm_value": 78, "confirmed": false,
 "fault": "", "fault_age_ms": null,
 "arm_input": {"state": "live", "seq": 4410, "age_ms": 12},
 "frames": {"state": "fresh", "seq": 1234, "timecode": "00:01:02:03",
            "age_ms": 8, "accepted": 1234, "rejected": 0, "last_reject": ""},
 "sacn": {"sent": 88123, "errors": 0},
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
| `heartbeat` | flamesafe's tick counter. The corner flame and the deck marquee step on this |
| `tick_ms` | the tick period |
| `universe`, `priority`, `arm_value` | what flamesafe is configured to send. `priority` is always 200 |
| `confirmed` | false until the config's numbers are confirmed by Andy. Show it |
| `fault` | empty, or one sentence: an overrun or a compose fault. `fault_age_ms` says how long ago |
| `arm_input.state` | `never`, `live` or `stale`. Stale means every group is disarmed |
| `frames.state` | `never`, `fresh` or `stale`. Stale means every fire slot is zero |
| `frames.last_reject` | why the last rejected datagram was rejected |

Per group, the two lamps of section 8 panel 5:

| Field | Meaning |
|---|---|
| `wanted` | what the arm input is asking for |
| `armed` | the ARMED lamp: `disarmed` (dim blue), `armed` (green: the safety slot carries the arm value), `held` (amber: arm asked for and refused) |
| `reason` | why held, in words: `cycle the arm`, `dirty edge`, `re-arm dwell`, `chatter`, `arm input stale`, `arm input has never asserted`, `safety program fault` |
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

## What the wire carries

flamesafe sends the whole flame universe as one ANSI E1.31 data packet per
tick, priority 200, to the configured unicast destination. Every channel that
belongs to no group is always zero. A group's safety slot is 0 or the arm
value. A group's fire slots carry ltcplay's values only while that group's
safety slot carries the arm value on the same packet and the edge-quiet
window (3 ticks from the rise) has passed; otherwise zero.

On a clean stop flamesafe sends three all-zero packets, then three all-zero
packets with the stream-terminated bit set.

## Timing knobs, all in the flamesafe config

| Key | Example | Meaning |
|---|---|---|
| `tick_hz` | 40 | sACN and status rate |
| `arm_stale_ms` | 500 | no fresh arm assertion for this long: every group disarms and needs a cycle |
| `frame_stale_ms` | 500 | no accepted flame frame for this long: every fire slot is zero |
| `overrun_ms` | 250 | a tick later than this: that tick is all zeros, every group needs a cycle |
| `min_arm_dwell_ms` | 1000 | after a disarm, the group is not raised again for this long |

## Versioning

`v` is the contract version. A change to any field's meaning, type or rule
is a new version. flamesafe rejects any other version outright; there is no
negotiation, because the two programs are installed together.
