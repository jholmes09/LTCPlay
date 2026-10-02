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

Rejected, with the reason in the next status frame's `frames.last_reject`:
not JSON, not an object, longer than 16384 bytes, wrong `v`, wrong or
missing `k`, wrong `t`, any field missing or of the wrong type, a `seq` at
or below the last accepted one, a `mono` below the last accepted one, a
`values` list that is not exactly 512 integers 0 to 255, a `universe` that
is not the flame universe, a sender other than the locked one.

**Rejections are journaled once per episode (2026-10-02).** Before this
date a rejected flame frame was counted and shown in `last_reject` but
never written to the journal. Now the first rejection of each kind of
reason writes one line, and once none of that kind has arrived for
`frame_stale_ms` a closing line gives the count (only if there was more
than one). The kind is the reason with its numbers and quoted text blanked
and cut to four words, so a sender cannot open unbounded kinds by varying
them; at most 16 kinds are tracked at once and any further kind is counted
as `other`. This covers flame frames and disarm_all (below) alike.

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
| `arm_input.foreign_senders` | how many OTHER senders are currently sending on the arm link besides the locked one (added round 3 of the safety review, 2026-10-02). Zero almost always. A non-zero count means a second local process is talking to this port right now. Its `wanted` bits can only ever CLEAR a group's bit, never set one, while it is not the locked sender. It CAN become the locked sender: the lock goes to whoever sends next once the locked sender has been quiet for `arm_stale_ms`, and a flood that crowds the real deck's frames out of the receive buffer makes the real deck look quiet (the round-4 review did this). Once that has happened the real deck, still sending, is the one counted here. That is why, as of round 4, no consent edge counts while this is non-zero (see the arm link section): whoever holds the lock, nobody can arm a group while anyone else is on the link. ltcplay must alarm on this alone; the clearing can leave `wanted` looking exactly like what the deck itself expects, with nothing else to notice |
| `arm_input.flooded` | true while flamesafe has seen a flood on the arm port (more datagrams waiting in one poll than an honest deck could send, keyed or not) inside the last `arm_stale_ms` (added round 4 of the safety review, 2026-10-02). No consent edge counts while it is true |
| `frames.state` | `never`, `fresh` or `stale` (by `frame_stale_ms`). `stale` or `never` means every group is disarmed and needs a cycle once the link is back |
| `frames.fire` | `passing` while the last frame is younger than `fire_hold_ms`, else `zeroed`: every fire slot is zero |
| `frames.last_reject` | why the last rejected datagram was rejected (a refused disarm_all's reason starts `disarm_all:`) |
| `disarm_all` | what the show program's Abort did: `accepted` (disarm_all datagrams taken, cumulative), `last_id`, `last_reason`, `age_ms` (null before the first). Added 2026-10-02, see "Disarm every group" below |
| `sacn.sent`, `sacn.errors`, `sacn.status_errors` | packets sent to the node, sends that failed, status sends that failed. Cumulative, never reset; shown in health. A failed send also sets `fault` for 5 s, which is the red |
| `stats.journal_dropped` | journal lines lost while the console was blocked, cumulative. When the backlog drains the journal writes how many were lost |

Per group, the two lamps of section 8 panel 5:

| Field | Meaning |
|---|---|
| `wanted` | what the arm input is asking for |
| `armed` | the ARMED lamp: `disarmed` (dim blue), `armed` (green: the safety slot carries the arm value), `held` (amber: arm asked for and refused) |
| `reason` | why held, in words: `cycle the arm`, `dirty edge`, `re-arm dwell`, `chatter`, `arm input stale`, `arm input has never asserted`, `safety program fault`, `Show program stopped answering: disarmed. Cycle the arm to re-arm once it is back.`, `Show program has not answered yet: disarmed. Cycle the arm once it is running.`, `Another sender is on the arm link: a cycle cannot arm until it stops. Cycle the arm again once it has gone.` (round 4: shown instead of `cycle the arm` while `arm_input.foreign_senders` is non-zero or `arm_input.flooded` is true, and also instead of the Abort sentence), `Disarmed by the show's Abort. Cycle the arm to re-arm.` (2026-10-02, disarm_all) |
| `amber` | `flashing` when cycling the arm is the fix (`cycle the arm`, `dirty edge`, the Abort sentence); `steady` when cycling would only restart the wait or fix nothing (`re-arm dwell`, `chatter`, and every veto). Empty unless held |
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

## The arm link (build step 7b, landed 2026-10-01)

A fourth loopback socket, `link.arm_port` on `link.arm_ip` (default: the
same address as `link.listen_ip`), optional: a config without it runs with
`NullArmInput`, exactly as before this build step, and every group stays
disarmed. The Stream Deck (part of ltcplay's own process, `ltcplay/
streamdeck.py`, never flamesafe's) sends one arm frame at 10 Hz or faster,
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
  `seq` on that socket instead of opening a new one, so the lock never
  lapses while ltcplay is running. If ltcplay itself dies, the show link
  goes too, and link loss disarms every group (section above).
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
that holds the lock can never collect consent for as long as ltcplay is
alive. Also: when the lock changes hands, the composer clears every latch
and pending edge exactly as for an input restart, so a consent edge can
never be half proved by one sender and finished by another
(`ArmAssertion.sender`). The cost, accepted: a genuine cycle made while
another sender is on the link does not count, and the operator cycles
again once it has gone. Whether the lock itself should refuse to change
hands during a flood was considered and left alone: a rule that pins the
lock cannot tell "the real deck is crowded out" from "a rogue that grabbed
the lock went quiet", and the second must hand the lock back to the real
deck. Blocking consent instead is safe whichever one holds it.

Every rejection on the arm link (wrong key, wrong shape, garbage, another
sender, a flood) is journaled once per reason per episode with a count on
the closing line; an episode ends only after 5 s with none of that reason,
and no reason writes more than 4 lines a minute however the datagrams are
spaced. A flood of undecodable datagrams used to write one line per
datagram and push real events out of the bounded journal queue.

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
frame and the sender lock stays with ltcplay. If the deck PROCESS dies or
freezes, `poll()` returns None once nothing has arrived for the input's own
bookkeeping to call fresh, and the EXISTING `arm_stale_ms` rule below does
the rest -- every group disarms within `arm_stale_ms` of the last accepted
frame, the same as a crashed or frozen test driver: silence is silence. **Reconnecting never re-arms anything by
itself** (rule 6, consent): the deck's own `seq` restarts at 0 on every
connect and reconnect (rule 3 below), so even a deck that remembered its
last button states and resent them immediately would fail consent, which
needs the counter proven to advance while already fresh -- the first
assertion after any gap proves nothing. The Stream Deck driver additionally
never tries to remember pre-disconnect state: on open (first connect, or a
reconnect after the hardware was lost) it starts every group `wanted`
false, so the operator sees every key read OFF and re-arms by pressing it,
matching what the lamp already says ("cycle the arm").

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
| `seq` | integer, 0 or more | the SAME sequence as the flame frames: ltcplay takes the next number for every datagram it sends on this link, flame or disarm_all. Must be greater than the last accepted one |
| `mono` | number | the sender's `time.perf_counter()`, finite; must not go backwards (same rule as the flame frame) |
| `id` | integer, 1 or more | which Abort this is. The sender repeats one Abort a few times (three copies) in case a datagram is lost; every copy is applied, only the first of each `id` is journaled |
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
disarm_all changes nothing, is counted (`stats.disarm_all_rejected`), sets
`frames.last_reject` (prefixed `disarm_all:`) and is journaled once per
episode (above). It does not take the sender lock, refresh the link's
liveness or carry any fire values: only flame frames do those.

**What it does, and all it does.** On the tick it arrives in (the service
drains the flame link before it composes): every group's latch is cleared,
every pending consent edge is cleared, every group that was up or latched
gets the re-arm dwell from now, and the safety slot of every group is zero
on that tick's packet. It never sets a latch, never sets a consent edge
and never touches `wanted`. So:

- **It cannot arm anything.** There is no code path from it to a latch.
- **Each group needs a fresh, genuine arm cycle from the Stream Deck
  afterwards.** A consent edge seen before the Abort is forgotten by it, so
  an arm press that completes after the Abort does not count; the deck's
  key has to report the group down AFTER the Abort (the operator taps the
  held key, which sends `wanted=false`) and then up again (hold to arm),
  and the re-arm dwell must have passed. A forced low (round 3, above) is
  still never a cycle. A group the deck keeps asking for reads `held`,
  flashing amber, reason `Disarmed by the show's Abort. Cycle the arm to
  re-arm.` until it is cycled; the deck shows it as ABORTED.
- The status frame's top-level `disarm_all` says what flamesafe took:
  `{"accepted": count, "last_id": id or null, "last_reason": "...",
  "age_ms": ms or null}`. ltcplay compares `last_id` with the Abort it sent
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
with an added caveat instead of blanking it (CONTRACT.md's own "show it"
was always about an overlay, never a replacement).

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
