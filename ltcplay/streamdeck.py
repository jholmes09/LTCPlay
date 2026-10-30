"""The real Stream Deck wiring: the keys from tools/streamdeck_design_demo.py
(approved look, bench demo only -- "connected to nothing else: no show, no
flames, no lasers") driving REAL flamesafe arm/disarm, and real Start Now /
Stop through ltc serve where one is connected.

THE KEYS (Jeff, 2026-10-26; a Stream Deck Mini, 3 by 2)
=======================================================

  key 0  START / STOP   key 1  Floor G      key 2  Cat-Walk G
  key 3  Blue Flames    key 4  Fireball     key 5  ANNOUNCE (page 2)

PAGE 2, ANNOUNCEMENTS (Jeff, 2026-10-26; announce.py), one press of key 5
(only while the engine says announcements are running; otherwise key 5 is
blank and does nothing):

  key 0  START / STOP   key 1  DELAYED      key 2  CANCEL
  key 3  CANNOT CONT.   key 4  STOP ANNC.   key 5  BACK

PAGE 3, THE HAZERS (Jeff, 2026-10-29; hazers.py): page 1 has no free key,
so the hazers have a page of their own, opened from page 1's key 5: a tap
while no announcements are set up; with announcements set up the key reads
ANNOUNCE with HOLD: HAZERS under it, a tap opens page 2 as before (on the
release) and a hold of HAZER_HOLD_S (0.7 s, its ring fills) opens page 3.
Page 2 is unchanged: its key 5 is BACK in every configuration.

  key 0  START / STOP   key 1  HAZER 1      key 2  HAZER 2
  key 3  HAZER 3        key 4  HAZERS OFF   key 5  BACK

A plain tap arms or disarms a hazer (ARMED in green, DISARMED); arming
needs what arming a group needs at the deck, disarming never. Its Haze and
Fan levels are set on the screens, not here. It gives way to page 1
exactly as page 2 does. Nothing on it arms, disarms or fires a flame.

One press plays (no double tap), through the engine's
/api/remote/announce-play, on this process's press thread. Between shows
the engine Holds the schedule first; during a show the announcement plays
over it and pauses nothing. STOP ANNC. ends only the announcement's sound.
A recording not on the PC yet reads NO FILE and its press is refused by
the engine. Nothing on page 2 arms, disarms, stops or latches anything.

KEYS RAN OUT, said honestly: the Mini has 6 keys and keys 1 to 4 are the
four arm keys, so page 2 has to cover them. The arm keys are hidden ONLY
while page 2 is up, and page 2 gives way to page 1 by itself: BACK, no key
pressed for PAGE2_IDLE_S, an Abort latch, an open alert, the Morning page
opening, a spoof alarm, or a deck reconnect. It cannot even be opened
while any of those is true or any other key is down or an arm hold is
filling. After page 2 gives way BY ITSELF (idle, an alert, the Morning
page) the group keys take no press for PAGE1_GUARD_S and until page 1 has
been drawn, so a press meant for an announcement never lands on an arm
key (review of PR #129). Key 0 is the same START / STOP on both pages, so Stop (every
group disarmed, then the run stopped) is never more than one hold away,
and an Abort on any screen still disarms everything. A key that is down
when the page changes does nothing until it is let go.

Keys 1 to 4 arm and disarm the first four flame groups in the deck's key
order (ltcplay_deck.json, deck_layout: by default flamesafe's own order,
labelled Floor G, Cat-Walk G, Blue Flames and Fireball for front row,
cat-walk, wave flamer and fireball). There is NO Hold key and NO Abort
key on the deck any more: Hold, Resume, Abort and Reset are the Rack
screen's (and the hardware e-stop is the e-stop). An Abort from any screen
still latches the deck: its border goes solid red, every key with nothing
to do greys, and a group key still disarms its group.

WHAT IS REAL AND WHAT IS NOT, read this before wiring it into a show
====================================================================

REAL, unconditionally, the moment this runs against the real hardware:
  - The four arm keys arm and disarm real flamesafe groups, over a
    keyed loopback link (CONTRACT.md, flamesafe/arminput.SocketArmInput)
    that this module is the ltcplay-side half of. flamesafe's own consent,
    dwell, chatter and edge-quiet rules are untouched; this module only
    says what is WANTED, exactly like the test driver.
  - STOP (a STOP_HOLD_S hold, 0.5 s, the old Abort key's) while a run is
    going or a scheduled show is live sends every group's wanted state to
    false on that SAME link, at once, before anything else
    (_disarm_all_groups: the arm link half the deck's Abort key had), then
    posts the Rack screen's Stop route (POST /api/stop) with "halt": a
    complete halt (Jeff, 2026-10-26). In the engine it ends live flame
    programming, sends the flame link's disarm_all (cues zeroed, a zero
    frame, the keyed disarm and its repeats: the flame half of a screen's
    Abort), disarms every laser, ends a live scheduled show (the
    scheduler's Stop, faded like an Abort) and stops playback. Nothing
    sets the Abort latch.
  - Disarm (pressing an armed or held group key) is INSTANT: this module
    never adds a hold-down or confirm step to a disarm press (Jeff,
    2026-10-01), and never waits on anything else either -- not the
    operator lookup, not flamesafe, nothing (safety review of PR #31, item
    5). Arming a group REQUIRES a hold (ARM_HOLD_S, item 8, Jeff,
    2026-10-01): pressing an OFF/SHOW LOST group key starts a timer with
    its own fill feedback on the key; let go early and nothing is sent. A
    key that just disarmed refuses to even START a new hold for
    REARM_REFRACTORY_S, so pressing it again "to be sure" during a panic
    cannot quietly re-arm it. None of this changes what flamesafe does
    with `wanted` once it is sent: this module still only reports
    flamesafe's own confirm/wait shape (dwell, dirty-edge, chatter), it
    does not add or remove any of that.
  - The operator gate: START NOW and arming a group are refused, loudly,
    while no operator is selected (see OPERATOR GATE below). STOP and
    disarming a group are NEVER gated on this: a press that only reduces
    risk must never wait on a picker.

THE START/STOP KEY (Jeff, 2026-10-26): key 0 is Start Now while nothing is
running (unchanged: it journals a refusal, there is no start entry point)
and STOP RUN while Run has been pressed and playback is going and the
scheduler is known to have no show live; during a scheduled show it says
what the show is doing (NOW PLAYING, PAUSED, AUDIO LOST) and is Stop too.
Stop is a HOLD of STOP_HOLD_S,
never a tap (the Rack screen asks for a confirm); letting go early sends
nothing, and the key ring fills while it is held. It disarms every flame
group on the arm link (above), then posts /api/stop with "halt" (live
flame programming ends, the flame link's disarm_all goes out, every laser
is disarmed, a live scheduled show ends with no latch, playback stops,
the outputs go dark), never behind the operator gate. Stop is a complete
halt of everything, during a scheduled show too, and the show ends (Jeff,
2026-10-26); to disarm one group mid-show, tap its key, or use the Rack
screen. While the scheduler cannot be read (the engine down) the key reads
STOP ALL and a hold is Stop too; a hold that completes always disarms the
arm link first, then asks for the halt (review of #130, 1). An open alert
is acknowledged by the press, which still starts the Stop hold while a
show or run is going, the state is unknown, or a group is armed (review of
#130, 9). It is reachable while an Abort is latched, as on the Rack
screen, and never touches the latch. See Controller.start_key_mode.

NOT REAL unless this module is given a `conductor` (EngineConductor, or
anything shaped like it) by whoever starts it:
  - START NOW does nothing but journal a refusal (there is no scheduler
    route that starts a show on demand), and STOP's /api/stop goes
    nowhere without an engine (the flame disarm above is real regardless).

THE OPERATOR GATE (Jeff, 2026-10-01)
=====================================
Jeff wants a named operator picked before the deck's actions unlock, for
accountability, with NO password or PIN (Andy's own physical key is the
real-world accountability control; a software password is redundant).
There is no existing "who is operating the rig right now" picker anywhere
in ltcplay (checked: nothing in web/index.html, nothing in web.py, nothing
in schedule_service.py before this change). The simplest correct thing,
per Jeff, is built here: schedule_service.py now keeps a
`current_operator` (ltcplay_current_operator.json, validated against the
existing operator list, ltcplay_operators.json) and serves it at
GET/POST /api/schedule/operator. This module READS that over the SAME
local web server ltcplay already runs for the Rack screen and Phone
(loopback traffic skips the token, see web.py's _authorised); it never
writes it. Picking the operator is the Rack screen's job (or any other
client of that route); the deck's spare key is not spent on a picker
and the approved layout is not to be changed.

FAILURE MODES, assumptions made here (flag these in the safety review):

  1. Deck HARDWARE unplugged mid-show, while armed (Jeff, 2026-10-07 and
     ruling 4 of 2026-10-12: a Stream Deck dying must NOT disarm the
     flames by itself; "audible booth note, then Andy confirms"). The
     Stream Deck's own read/write failing (DeckDisconnected) ends the
     main loop, and from then until a deck is back this process keeps
     sending its CURRENT wanted vector on the SAME arm socket at
     ARM_SEND_HZ with the counter still advancing (run_forever's
     _hold_link_steady): flamesafe sees an input that is alive and
     unchanged, so nothing disarms. The purpose reset is why: the
     software flame layer exists so Andy can go into the field without
     hitting the e-stop; the e-stop is the emergency layer, and a deck
     cable is not an emergency. The first failure of an outage raises the
     "deck" alert on the engine (alerts.py: booth note, rack-screen banner
     with Acknowledge and Disarm all, no timer); a hold in progress at the
     unplug is dropped, never finished off the wall clock. Before this
     (round 4, item A) the outage sent every group OFF; the sender-lock
     reason for staying on the air is unchanged, the OFF is not. Since
     round 5 (item 2) ANY exception out of the deck, the deck's open, or
     one main-loop pass is handled exactly like an unplug. Only a
     deliberate stop (Ctrl-C, SystemExit) ends the loop.
     RESIDUAL, accepted (Jeff, 2026-10-12): if this whole PROCESS dies,
     silence is what flamesafe sees, arm_stale_ms (500 ms) disarms every
     group, the supervisor restarts the process in about 2 s and it
     begins at counter 0 with every group OFF (CONTRACT rule 3: the input
     restarted, and the process is the input). That is a crash, not an
     unplug. The alternative, an arm handover to the engine with a grace
     window, needs a flamesafe change and is not built.
  2. Deck HARDWARE reconnected while armed (ruling 4, 2026-10-12: unplug
     and replug KEEPS the arm state). A reconnect inside a living process
     keeps `wanted` and the counter: nothing is sent that flamesafe has
     not been seeing all along, so nothing changes on the rig. Only the
     holds in progress and the key snapshot are dropped
     (Controller.reset_on_reconnect), so a key held at the unplug cannot
     fire from time that passed during the gap. The "deck" alert is
     updated to say the deck is back and still waits for its Acknowledge
     (any deck key, or the rack screen). Before this every reconnect
     restarted wanted and seq (ArmSocket.restart), which disarmed every
     group; ArmSocket.restart stays for a process that starts over.
  3. A key press while flamesafe itself is in fault, or its own status
     link has gone quiet for more than a second (CONTRACT.md: "ltcplay
     shows red for the safety program"). Arm/disarm presses are still SENT
     (sending costs nothing and a disarm must never be swallowed); whether
     a group actually (dis)arms is, as always, flamesafe's call, and its
     own status will read it back (e.g. "held: safety program fault").
     This module adds no special-case logic for a fault: there already is
     one, in rules.py, and duplicating it here would be a second place to
     get it right or wrong.
  4. Two people reaching for the deck "at once". Deck.keys_down() returns
     EVERY key-state snapshot the hardware reported since the last read,
     in order (fixed 2026-10-01, safety review of PR #31, item 3: it used
     to return only the latest one, so a quick tap-and-release between two
     read cycles could vanish with no trace); run_once() is called once
     per snapshot and processes each one's transitions in a fixed key
     order, so two simultaneous presses are both delivered, in that order,
     same as two presses a moment apart by the same person. There is no
     separate lock-out for a second presser: the operator picker names who
     to hold accountable in the journal, not who is allowed to press,
     matching how conductor.py's own generation counter and
     composer.assert_arm already resolve "two requests at once" for every
     other input this show has.
  5. More than 4 flamesafe groups configured. The deck (a Stream Deck
     Mini, 6 keys) has 4 arm keys (keys 1 to 4; Jeff, 2026-10-26). A
     group past the fourth in the deck's key order gets NO key and shares
     none: it is a SCREEN-ONLY group. It is armed only by a screen hold
     (the rack screen or a listed device, through this process:
     screen_pass, with every rule a key has: the hold, the re-arm
     refractory window, the latch, the operator gate), never by a Morning
     page purge click (purge_pass refuses it, review of PR #120, P1), and
     disarmed by a screen disarm, Disarm all flame groups, Stop on the
     deck, or an Abort on any screen. With screen arming off (the
     default) it cannot be armed at all. Its state is on the screens,
     never on the deck; a spoof mismatch on it still flashes ALARM on
     every deck group key. No page or shift key hides a group. Which
     group sits on which key, and the key's label, come from
     ltcplay_deck.json (deck_layout); with no file, flamesafe's own order
     (review of PR #120, P2-5: an install's order does not move), labelled
     Floor G, Cat-Walk G, Blue Flames and Fireball where the names match.
     Every group past the fourth is screen-only, said plainly at start,
     never refused.
  6. A second local process spoofing arm frames. arminput.SocketArmInput
     now locks onto the first sender it accepts, exactly like the
     flame-frame link, so a rogue frame is rejected once this deck is
     locked in (safety review of PR #31, item 1). This module adds a
     second line of defence on top of that: it compares flamesafe's
     reported arm counter and per-group `wanted` against what it itself
     last sent, and raises a visible, journaled alarm (every group key
     flashes "ALARM") the moment they disagree for longer than a normal
     send/receive lag -- see Controller._spoof_reason. It never tries to
     fix anything by itself, only to make the disagreement impossible to
     miss.
  7. The operator lookup and show-running lookup (LocalSchedule) run on
     their OWN background thread, never the main loop (safety review of
     PR #31, item 5): a hung or slow `ltc serve` used to be able to delay
     a key read or an arm-frame send by up to its own 1 s HTTP timeout.
     Since 2026-10-21 the arm frame is sent by a thread of its own
     (ArmPacer), and the key images are written to the deck by another
     (DisplayWorker): on the show PC a busy machine made one frame of USB
     writes take seconds, the single loop sent nothing meanwhile, and
     flamesafe disarmed mid-show 107 times in five hours. The fail-safe
     coupling is kept where it belongs: the pacer sends only while the
     loop that READS the keys (holds, Abort, disarm) has run within
     INPUT_STALE_S, so a frozen deck program still goes silent and
     flamesafe's own arm_stale_ms still disarms it. Only a slow screen no
     longer does, and a slow or stuck key image write never drops an arm
     hold or a held FIRE either (DECK_STALL_RULING, with what still ends
     one and the residual risk).
  8. Every arm, disarm, Abort and refusal this module journals also
     reaches ltc serve's own real, persistent night journal over HTTP
     (DeckJournal, POST /api/schedule/deck-event -- safety review of PR
     #31, item 9), on ITS OWN background thread, in addition to this
     process's console. A server that cannot be reached (not --schedule,
     not running, down) only means the line stays console-only; nothing
     here ever blocks or raises for it.
"""
from __future__ import annotations

import io
import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Visual constants and geometry, copied from tools/streamdeck_design_demo.py
# (the approved look) and left as close to it as code reuse allows. This
# file does not import that one: it is a frozen bench tool, kept standalone
# on purpose (see its own docstring), and this is the production build of
# the same artwork driven by real data instead of a local fake.
# --------------------------------------------------------------------------
VID, PID = 0x0FD9, 0x0063
K = 80
COLS, ROWS = 3, 2
W, H = COLS * K, ROWS * K

BLACK = (7, 6, 5)
GOLD = (212, 168, 74)
CHAMPAGNE = (246, 227, 174)
BULB_OFF = (52, 41, 20)
RED = (230, 30, 24)
DIM_TEXT = (72, 66, 58)
GREEN = (40, 190, 90)
AMBER = (245, 160, 30)
BRONZE = (44, 36, 24)         # #2C2418, off and idle (Jeff: "The blue feels
BRONZE_EDGE = (120, 96, 50)   # off brand"); its 1 px edge, as the demo draws
AMBER_DIM = (70, 44, 8)       # a flashing amber key's off phase (CYCLE ARM)
# The Start key during a show (Jeff, 2026-09-27, the approved demo): NOW
# PLAYING flashing green; PAUSED, steady, while the show is held. A show
# whose audio is lost reads AUDIO LOST, flashing amber, on that one key
# only: Hold and Abort stay live and are never covered during a show (Jeff,
# 2026-09-27 18:22, "Lock them in").
PLAYING_ON = ((50, 205, 95), (4, 28, 10))      # (bg, text), the lit phase
PLAYING_OFF = ((8, 40, 16), (60, 200, 100))    # the dark phase
PAUSED_BG = BRONZE
AUDIO_LOST_ON = (AMBER, (40, 20, 0))
AUDIO_LOST_OFF = (AMBER_DIM, AMBER)
# An alert is open on the engine (alerts.py; Jeff's ruling 2, 2026-10-12:
# the key that acknowledges must visibly change while an alert is pending,
# so nobody is confused, and return to normal after). The Start key reads
# ACK / ALERT, white on blue, flashing to a dark blue: a look nothing else
# on the deck uses, so it is never mistaken for Abort (red), an armed
# group (green) or a hold (amber). Any key acknowledges; this one does
# only that.
ACK_ON = ((36, 110, 230), (245, 248, 255))     # (bg, text), the lit phase
ACK_OFF = ((10, 32, 76), (150, 190, 255))      # the dark phase
# No link to flamesafe: the arm keys read NO / FLAME / LINK, one word per
# key, red (Jeff, 2026-09-27, locked wording). Steady: with no status
# frames there are no ticks to flash on. The arm link carries on exactly
# as before; only the drawing changed.
NO_FLAME_LINK = ("NO", "FLAME", "LINK")

STOP_HOLD_S = 0.5           # Stop's hold: the deck's old Abort key's, from
                            # the demo (Jeff, 2026-10-26). Disarm itself (a
                            # group key) has NO hold; see module doc.
# The Start/Stop key's modes in which a hold is Stop: a run going, a
# scheduled show live (Jeff, 2026-10-26: Stop is a complete halt of
# everything, and the show ends), and the engine or the scheduler not known
# (review of #130, 1: Stop must work when the engine cannot be reached; it
# always disarms the arm link first).
STOP_MODES = ("stop", "show", "unknown")
# How long after this deck disarmed a group flamesafe's status may still
# report it armed without that counting as "a group armed" for Stop (review
# of #132, 3: a purge machine deselected while FIRE is held).
DISARM_SETTLE_S = 1.5
# How long the Start/Stop key has to be down in a Stop hold before its face
# says HOLD TO STOP over anything else (an open alert's ACK, a fault).
STOP_FACE_AFTER_S = 0.15
ARM_SEND_HZ = 20.0          # >= the arm link's 10 Hz floor (CONTRACT.md)
STATUS_STALE_S = 1.0        # CONTRACT.md: "ltcplay shows red for the safety
                            # program" past this
GROUP_KEY_LIMIT = 4         # keys 1 to 4. See module docstring 5.

# Item 8 (Jeff, 2026-10-01, safety review of PR #31): arming is now a
# hold, like Abort; disarm stays an instant tap. ARM_HOLD_S is a little
# longer than Abort's own 0.5 s -- arming is the escalation, Abort and
# disarm are the de-escalations, and those have to stay fast -- but short
# enough that it still reads as "hold this key", not "this key is broken".
# A SAFETY CONSTANT since PR #34 (fix round 2, item 4): flamesafe refuses a
# hold begun before a screen Abort only if it completes inside the
# post-Abort window (min_arm_dwell_ms, never under 1 s, after the last
# Abort copy). A 2.0 s hold armed at +1.9 s after an Abort in review.
# selftest's test_the_deck_arm_hold_fits_inside_the_post_abort_window
# fails if this plus 0.3 s of deck lateness no longer fits.
ARM_HOLD_S = 0.6
# After a group's disarm, its key refuses to even START a new arm-hold for
# this long: a panicked "press it again to be sure" right after Abort or a
# disarm must never quietly turn into a re-arm. Long enough to cover a
# flinch-press, short enough that a deliberate re-arm a few seconds later
# is never mistaken for one.
REARM_REFRACTORY_S = 2.0
# The keys are drawn as the approved demo draws them: once per marquee step
# (CHASE_STEP_S), and every FRAME_S while something on them moves (a key
# held, a screen hold, a hold ring filling); at least every DRAW_IDLE_S for
# the 2 Hz blink when the marquee is still. Only keys whose image changed go
# to the deck. Jeff, 2026-10-05: drawing only every 0.2 s (a CPU saving of
# 2026-10-04, when all six keys at 20 Hz took 14% of a core) made the
# animation look slow and low frame rate. The arm link, key reads and holds
# still run once per pass.
DRAW_IDLE_S = 0.2
FRAME_S = 0.02
# Latched (an Abort from any screen; Jeff's approved design, decisions of
# 2026-09-26, without the deck's own Abort key since 2026-10-26): the deck
# border is solid red and every key with nothing to do is greyed. Meaning
# wins over grey: a group flamesafe still reports armed (or a fault, or no
# link) shows its real state, and a tap on it disarms it; Stop stays
# reachable while a run is going. Reset is the Rack screen's.
LATCHED_GREY = (26, 24, 21)   # a key with nothing to do while latched

# Item 1's second line of defence (Controller._spoof_reason): a mismatch
# between what this deck sent and what flamesafe reports back must PERSIST
# this long before it is trusted as a spoof, not a one-tick send/receive
# lag. RECONNECT_GRACE_S is the separate grace window right after this
# deck's own ArmSocket.open() resets its seq counter (a reconnect), since
# flamesafe can still be describing the sender from before the gap for up
# to its own arm_stale_ms.
SPOOF_GRACE_S = 0.5
RECONNECT_GRACE_S = 1.0

# The keys (Jeff, 2026-10-26): key 0 is Start/Stop; keys 1 to 4 arm and
# disarm the first four flame groups in the deck's key order; key 5 opens
# the announcements page while the engine runs announcements, and is blank
# and does nothing otherwise. The deck has no Hold and no Abort key any more:
# Hold and Abort are the Rack screen's (and the hardware e-stop's).
TOP_START = 0
GROUP_KEYS = (1, 2, 3, 4)
SPARE_KEY = 5
# Page 2, the announcements (module docstring). Key 5 opens it and is BACK
# on it; keys 1 to 3 play, key 4 stops the sound.
ANNOUNCE_KEY = SPARE_KEY
PAGE2_PLAY = {1: "delayed", 2: "cancellation", 3: "cannot_continue"}
PAGE2_FACE = {"delayed": ["DELAYED"], "cancellation": ["CANCEL"],
              "cannot_continue": ["CANNOT", "CONTINUE"]}
PAGE2_STOP = 4
PAGE2_BACK = SPARE_KEY
PAGE2_IDLE_S = 10.0     # no key pressed this long on page 2: back to page 1
# Page 3, the hazers (Jeff, 2026-10-29; hazers.py). Page 1 has no free key
# (keys 1 to 4 arm, key 5 opens the announcements), so the hazers get a
# page of their own with page 2's rules (it gives way by itself, exactly as
# page 2 does). It opens from page 1's key 5: a plain tap when no
# announcements are set up; with announcements, a tap still opens page 2
# and a HOLD of HAZER_HOLD_S (its ring fills; let go early and it is the
# tap) opens page 3. Page 2's key 5 is BACK in every configuration (Jeff,
# 2026-10-29: one press back, under stress). Keys 1 to 3 toggle the
# hazers in flamesafe's order (a plain tap: a hazer is not a flame; ON
# needs what arming needs at the deck, OFF nothing), key 4 is HAZERS OFF,
# key 5 BACK. Key 0 stays START / STOP.
HAZER_PAGE = 3
HAZER_HOLD_S = 0.7
PAGE3_HAZER = {1: 0, 2: 1, 3: 2}
PAGE3_OFF = 4
PAGE3_BACK = SPARE_KEY
# ... but only on a fresh answer that nothing plays. With no fresh answer
# at all for this long, page 2 gives way anyway: the arm keys come back.
PAGE2_STALE_CLOSE_S = 60.0
# After page 2 gives way BY ITSELF (idle, an alert, the Morning page), a
# press meant for an announcement key must not ARM the group now under the
# finger (review of PR #129): the group keys start no arm for
# PAGE1_GUARD_S AND until page 1's faces have actually been written to the
# keys (the display worker's word, not merely composed: a stalled USB
# write keeps the guard on). A tap that DISARMS an armed group always goes
# through (Jeff: a disarm is never gated). Not after BACK (the operator
# chose it), an Abort latch or an arm link alarm. Page 2 never closes for
# idle while an announcement plays (STOP ANNC. stays reachable).
PAGE1_GUARD_S = 1.5
# A deck refusal of an announcement press (the engine's sentence) shows on
# the pressed key this long (page 2's faces).
ANNOUNCE_SAID_S = 4.0

# Which flame group sits on which key, and what the key calls it (Jeff,
# 2026-10-26): ltcplay_deck.json in the settings folder, optional.
#   {"groups": [{"group": "front row", "label": "Floor G"}, ...]}
# With no file the keys are Jeff's, by NAME (review of #126, P1): front
# row, cat-walk, wave flamer and fireball on keys 1 to 4 in that order,
# wherever flamesafe's file lists them, then every other group in the
# file's order. The file lists only the groups it wants to place, in key
# order (keys 1 to 4 left to right, top row first); every group it does
# not list follows in the default order. `label` is what the key says (the
# group's own name when left out and not one of DECK_LABELS). A group past
# the fourth key is screen-only (module docstring 5). Nothing here changes
# what is sent: the arm frame keeps flamesafe's names in flamesafe's order.
DECK_CONFIG_FILE = "ltcplay_deck.json"
DECK_ORDER = ("front row", "cat-walk", "wave flamer", "fireball")
DECK_LABELS = {"front row": "Floor G", "cat-walk": "Cat-Walk G",
               "wave flamer": "Blue Flames", "fireball": "Fireball"}
LABEL_MAX = 12          # the key's name bar fits this many letters


# Other spellings of Jeff's rows that the Morning page also takes as the
# same row (morning.GFLAME_ROW_NAMES), so the deck and the Morning page
# never disagree on which group is the cat-walk (third review of #126,
# P2-3).
DECK_ALIASES = {"cat walk": "cat-walk", "catwalk": "cat-walk"}


def _known(name):
    """A flamesafe group name as DECK_ORDER and DECK_LABELS spell it:
    any case, spaces around it ignored, "cat walk" and "catwalk" read as
    "cat-walk"."""
    n = str(name).strip().lower()
    return DECK_ALIASES.get(n, n)


def _case_twins(names):
    """The flamesafe group names that read the same as an earlier one to
    the deck (_known: "front row" and "Front Row"). flamesafe tells them
    apart by case, so they are two groups; only the first gets Jeff's
    label and its key by name, the other keeps its own name."""
    seen, twins = set(), []
    for n in names:
        k = _known(n)
        if k in seen:
            twins.append(n)
        seen.add(k)
    return twins


def _default_layout(names):
    """(order, labels) with no ltcplay_deck.json: Jeff's four groups by
    NAME first, in DECK_ORDER (front row, cat-walk, wave flamer, fireball:
    Floor G, Cat-Walk G, Blue Flames, Fireball on keys 1 to 4, wherever
    flamesafe's file lists them; review of #126, P1), then every other
    group in the file's own order (an install with none of those names
    keeps its order). DECK_LABELS' words for a name that matches one,
    whatever its case or spaces (and cat walk or catwalk for cat-walk),
    else the group's own name. Two groups that differ only by case: the
    first in the file gets the label, the other keeps its own name, so
    two keys never read the same (third review of #126, P2-3)."""
    known = [_known(n) for n in names]
    order = [known.index(k) for k in DECK_ORDER if k in known]
    order += [i for i in range(len(names)) if i not in order]
    return order, [DECK_LABELS.get(k, n) if known.index(k) == i else n
                   for i, (n, k) in enumerate(zip(names, known))]


def _keys_text(order, labels, names):
    """The key map in words: "key 1 Floor G (front row), ...; no key:
    Fireball (fireball)"."""
    keyed = [f"key {GROUP_KEYS[j]} {labels[g]} ({names[g]})"
             for j, g in enumerate(order[:GROUP_KEY_LIMIT])]
    rest = [f"{labels[g]} ({names[g]})" for g in order[GROUP_KEY_LIMIT:]]
    return ", ".join(keyed) + (f"; no key: {', '.join(rest)}" if rest else "")


def deck_layout(names, doc=None, where=DECK_CONFIG_FILE):
    """(order, labels, notes, ok) for flamesafe's group `names` (its file
    order). `order`: group indices in key order, every group once; the
    first GROUP_KEY_LIMIT get keys 1 to 4, the rest are screen-only.
    `labels`: one per group index. `notes`: plain sentences for the
    journal. `ok`: False when `doc` (ltcplay_deck.json, parsed; None for no
    file) had anything wrong with it: then the WHOLE default layout is used,
    never a partly applied file, so no key moves because of a typo (review
    of PR #120, P2-1), and the notes say why. What is wrong: a listed group
    flamesafe does not have, an entry with no group name, a group listed
    twice, a label that is not words of up to LABEL_MAX letters, and a
    label that is another group's name or label (P2-2: a key must never
    read like another group's). A group the file does not list is never
    wrong: it follows the listed ones in the default order (review of
    #126, P1). Never raises: the deck still starts (Jeff, 2026-10-26)."""
    names = list(names)
    d_order, d_labels = _default_layout(names)
    if doc is None:
        # No file: Jeff's groups by name, then the rest in file order;
        # deck_layout_at_start journals the key map.
        return d_order, d_labels, [], True
    problems = []
    lst = doc.get("groups") if isinstance(doc, dict) else None
    order, labels = [], list(d_labels)
    if not isinstance(lst, list):
        problems.append(f"{where} has no \"groups\" list.")
        lst = []
    for e in lst:
        g = e.get("group") if isinstance(e, dict) else None
        if not isinstance(g, str) or not g.strip():
            problems.append(f"{where} has an entry with no \"group\" name.")
            continue
        known = [_known(n) for n in names]
        if _known(g) not in known:
            problems.append(f"{where} lists {g!r}, which is not a flame "
                            f"group in flamesafe's config "
                            f"({', '.join(names)}).")
            continue
        stripped = [str(n).strip() for n in names]
        if g.strip() in stripped:
            i = stripped.index(g.strip())
        elif known.count(_known(g)) > 1:
            problems.append(f"{where} lists {g!r}, and flamesafe has more "
                            f"than one group by that name in another case. "
                            f"Write it exactly as flamesafe does.")
            continue
        else:
            i = known.index(_known(g))
        if i in order:
            problems.append(f"{where} lists {g!r} twice.")
            continue
        order.append(i)
        lab = e.get("label")
        if lab is None:
            continue
        if not isinstance(lab, str) or not lab.strip() or \
                len(lab.strip()) > LABEL_MAX:
            problems.append(f"{where}: the label for {g!r} has to be words, "
                            f"up to {LABEL_MAX} letters.")
            continue
        labels[i] = lab.strip()
    # Every group the file does not list follows, in the default order.
    order += [i for i in d_order if i not in order]
    for i in order:
        mine = labels[i].strip().lower()
        for j, other in enumerate(names):
            if j != i and _known(other) == _known(names[i]) and \
                    labels[i].strip() == str(names[i]).strip():
                # Two flamesafe groups that differ only by case: a key
                # with its own group's name is the truth, not a look-alike
                # (third review of #126, P2-3).
                continue
            if j != i and mine in (other.lower(), d_labels[j].lower(),
                                   labels[j].strip().lower()):
                problems.append(f"{where}: the label {labels[i]!r} for "
                                f"{names[i]!r} reads like {other!r}'s. A "
                                f"key must never read like another group.")
                break
    if problems:
        return d_order, d_labels, problems + [
            f"So the deck ignores {where} and uses its default layout: "
            f"{_keys_text(d_order, d_labels, names)}."], False
    return order, labels, [], True


def load_deck_layout(names, folder):
    """deck_layout() for ltcplay_deck.json in `folder`. A missing file is
    the defaults; one that cannot be read is said, and the defaults used."""
    path = os.path.join(folder, DECK_CONFIG_FILE) if folder else None
    doc = None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            order, labels = _default_layout(list(names))
            return order, labels, [
                f"{path} could not be read ({e}), so the deck uses its "
                f"default layout: {_keys_text(order, labels, list(names))}."
            ], False
    return deck_layout(names, doc, path or DECK_CONFIG_FILE)


def _first_font(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _import_hid():
    try:
        import hid
        return hid
    except Exception as e:
        raise SystemExit(
            "The hidapi package is not installed, so the Stream Deck "
            "cannot be opened.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install hidapi")


def _import_pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except Exception as e:
        raise SystemExit(
            "The Pillow package is not installed, so the Stream Deck's key "
            "artwork cannot be drawn.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install pillow")


class DeckDisconnected(Exception):
    """The Stream Deck stopped answering (unplugged, USB fault, driver
    error). The caller must stop sending arm frames at once; see module
    docstring, failure mode 1."""


# Elgato's USB product ids, for the sentence that says what IS plugged in
# when the Mini (VID:PID above) cannot be opened (Jeff, 2026-10-17: "the
# Stream Deck never connected", and nothing said what was looked for).
ELGATO_PRODUCTS = {0x0060: "Stream Deck (original)", 0x006D: "Stream Deck V2",
                   0x0063: "Stream Deck Mini", 0x0090: "Stream Deck Mini MK.2",
                   0x006C: "Stream Deck XL", 0x008F: "Stream Deck XL V2",
                   0x0080: "Stream Deck MK.2", 0x0084: "Stream Deck Plus",
                   0x009A: "Stream Deck Neo", 0x0086: "Stream Deck Pedal"}


def describe_hid(hid):
    """One sentence: what this program looks for, and which Elgato USB
    devices the machine can see right now. Never raises (it is only ever
    said while something already failed)."""
    looked = (f"looked for USB vendor {VID:04X} product {PID:04X} "
              f"({ELGATO_PRODUCTS.get(PID, 'Stream Deck')})")
    try:
        found = []
        for d in hid.enumerate(VID, 0):
            pid = int(d.get("product_id", 0))
            name = ELGATO_PRODUCTS.get(pid) or str(d.get("product_string")
                                                 or "unknown Elgato device")
            item = f"product {pid:04X} ({name})"
            if item not in found:
                found.append(item)
    except Exception as e:                       # noqa: BLE001
        return (f"{looked}; the USB HID list could not be read "
                f"({type(e).__name__}: {e})")
    if not found:
        return (f"{looked}; no Elgato USB device is plugged into this "
                f"machine")
    return f"{looked}; Elgato USB devices present: {', '.join(found)}"


# --------------------------------------------------------------------------
# Pure protocol: the arm frame this module sends, and the status frame it
# reads. Hand-written from flamesafe/CONTRACT.md, exactly as ltcplay's own
# flame-frame encoder would be: this module never imports flamesafe (the
# wall test, selftest.py's test_the_wall_between_ltcplay_and_flamesafe,
# fails the build if it does).
# --------------------------------------------------------------------------
CONTRACT_VERSION = 2


def encode_arm_frame(seq, wanted, names, key):
    return json.dumps({
        "v": CONTRACT_VERSION, "k": key, "t": "arm", "seq": int(seq),
        "wanted": [bool(w) for w in wanted],
        "names": [str(n) for n in names],
    }, separators=(",", ":")).encode("utf-8")


def decode_status_frame(data, key):
    """The fields this module actually uses off a status frame, or None if
    it is not a status frame with our key. Never raises: a malformed or
    foreign datagram is simply not a status frame, and the deck keeps
    showing whatever it last knew (then goes stale after STATUS_STALE_S,
    same as CONTRACT.md's own rule for ltcplay)."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != CONTRACT_VERSION:
        return None
    if obj.get("k") != key or obj.get("t") != "status":
        return None
    groups = obj.get("groups")
    if not isinstance(groups, list):
        return None
    return obj


# --------------------------------------------------------------------------
# ArmSocket: the one socket this module sends arm frames from, for its
# whole lifetime (CONTRACT.md's own lesson from the flame-frame link,
# applied here too). Resets wanted and seq to a clean slate on every open,
# never on its own initiative otherwise.
# --------------------------------------------------------------------------
class ArmSocket:

    def __init__(self, ip, port, key, n, journal=None):
        self.ip = ip
        self.port = port
        self.key = key
        self.n = n
        self._sock = None
        self.wanted = [False] * n
        self.seq = 0
        self._journal = journal or (lambda text, **kw: None)
        self._send_failing = False   # item 10: log once per episode, not
                                     # once per dropped frame
        # time.monotonic() of the last frame the socket took, or None: the
        # rack screen's Deck chip (sent_age_ms). Never read by arming.
        self.sent_at = None
        self._now = time.monotonic   # its clock (a test gives its own)
        # Two threads send on this socket since the arm link got a pacer
        # of its own (ArmPacer, 2026-10-21): the pacer's steady assertion
        # and the deck's own instant ones (a disarm, Abort, a completed
        # hold). One lock keeps `wanted`, `seq` and the datagram together,
        # so seq leaves this process strictly in order and no frame ever
        # carries a half-changed vector.
        self.lock = threading.RLock()

    def open(self):
        with self.lock:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.wanted = [False] * self.n
            self.seq = 0

    @property
    def is_open(self):
        return self._sock is not None

    def restart(self):
        """A reconnect on the SAME socket (round 4 of the safety review,
        item A): exactly what open() does to `wanted` and `seq` -- every
        group OFF, the counter back to 0 so flamesafe sees an input that
        restarted -- but keeping the socket, and so the source port, that
        flamesafe's arm-link sender lock already belongs to. Closing and
        reopening here is what used to hand that lock to whoever sent
        next."""
        with self.lock:
            if self._sock is None:
                self.open()
                return
            self.wanted = [False] * self.n
            self.seq = 0

    def set_group(self, i, on):
        with self.lock:
            self.wanted[i] = bool(on)

    def set_all(self, on):
        with self.lock:
            self.wanted = [bool(on)] * self.n

    def send(self, names):
        """Send the current `wanted` vector with an advancing seq. Called
        at ARM_SEND_HZ or faster, whether or not anything changed (the arm
        link's own 10 Hz-or-faster rule, CONTRACT.md). Safe from any
        thread: see `lock`."""
        with self.lock:
            note = self._send_locked(names)
        # The journal (a console print, then a queue) is written with the
        # lock released (review of PR #79, P3-d): a blocked console must
        # never hold up an Abort's set_all and send on another thread.
        if note is not None:
            try:
                self._journal(note[0], **note[1])
            except Exception:
                pass

    def _send_locked(self, names):
        """One frame; returns (text, kw) for the journal, or None."""
        if self._sock is None:
            return None
        self.seq += 1
        try:
            self._sock.sendto(encode_arm_frame(self.seq, self.wanted, names,
                                               self.key), (self.ip, self.port))
            self.sent_at = self._now()
            if self._send_failing:
                self._send_failing = False
                return ("Stream Deck: arm frame sends are working again.",
                        {"action": "arm-link"})
        except OSError as e:
            # Item 10 (safety review of PR #31): a dropped UDP send used to
            # vanish completely -- the next one, 50 ms away, tries again,
            # which is still the right thing to DO, but a persistent
            # failure (not just one lost packet) must leave a trace. Logged
            # once per episode, not once per frame: at 20 Hz that would be
            # a flood for exactly the moment the journal matters most.
            if not self._send_failing:
                self._send_failing = True
                return (f"Stream Deck: an arm frame could not be sent "
                        f"({type(e).__name__}: {e}). Retrying at "
                        f"{ARM_SEND_HZ:g} Hz; this line will not repeat "
                        f"until it recovers.",
                        {"fault": True, "action": "arm-link"})
        return None

    def sent_age_ms(self):
        """ms since the socket last took an arm frame, or None before the
        first: carried to the engine for the Deck chip. Never raises."""
        at = self.sent_at
        if at is None:
            return None
        return max(0, int((self._now() - at) * 1000))

    def close(self):
        with self.lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class StatusSocket:
    """Reads flamesafe's status frames for display only. See CONTRACT.md:
    "There is no path from a status frame to flame output" -- this module
    holds to that just as strictly: nothing read here is ever sent back as
    an arm or fire command, only drawn on a key."""

    def __init__(self, ip, port, key):
        self.ip = ip
        self.port = port
        self.key = key
        self._sock = None
        self.last = None
        self.last_at = None

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((self.ip, self.port))
        sock.setblocking(False)
        self._sock = sock

    def poll(self, clock=time.monotonic):
        sock = self._sock
        if sock is None:
            return
        for _ in range(200):
            try:
                data, _addr = sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError:
                return
            obj = decode_status_frame(data, self.key)
            if obj is not None:
                self.last = obj
                self.last_at = clock()

    def stale(self, clock=time.monotonic):
        return self.last_at is None or (clock() - self.last_at) > STATUS_STALE_S

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Pure logic: visual state from real data. No socket, no hid, fully
# unit-testable (see selftest.py).
# --------------------------------------------------------------------------
def group_look(group_status, fault="", slot=0):
    """(line1, line2_or_None, bg, text, flashing) for one bottom-row key
    from the status frame's own per-group dict (CONTRACT.md section on the
    status frame), or from None (no status ever received / stale): drawn
    red, as its word of NO / FLAME / LINK (`slot` is which of the three
    keys this is), matching CONTRACT.md's "ltcplay shows red for the safety
    program" rule -- the deck never claims a group is armed, disarmed or
    anything else when it cannot actually see flamesafe's answer.

    `fault` is flamesafe's own TOP-LEVEL status field (CONTRACT.md), never
    per-group; the caller reads it off the same status frame
    `group_status` came from (see Controller.draw). Before this fix
    (safety review of PR #31, item 4) this function did not read it, so a
    group flamesafe itself was NOT actually honouring -- a fault means the
    wire is not being written at all -- could still draw ARMED in green.
    A non-empty `fault` now wins over every other look, including a group
    flamesafe still reports as armed, matching CONTRACT.md's own rule: "a
    non-empty fault is red for ltcplay: an armed group is not fine while
    the wire is not being written."

    flamesafe's top-level `confirmed` (false while the config's own
    numbers are not confirmed by Andy; flamesafe.example.json SHIPS this
    way) is NOT read here and changes nothing on a key. It used to blank
    every group to "NOT CONFIRMED" (item 5, round 2 of the safety review
    put the real state back, under a small amber corner mark), and the
    corner mark went too (Jeff, 2026-10-19: pointless on a key). The keys
    show the real state, nothing else; the unconfirmed config is still
    said where it can be read at leisure: this deck's journal, once, when
    it first sees it (Controller._note_config_confirmed), the engine's
    status (flames.confirmed, remote.py) and the rack screen's Info
    popup. Display only, then and now: `confirmed` never changed what the
    deck sends.

    TODO (item F, safety review of PR #31): if a show conductor's own
    Abort-latch state is ever wired in here (PR #29/#30 are heading that
    way), THIS is the first place that has to check it -- a latched
    conductor must never let a status frame that predates the latch make
    this function draw a group as armed or fine. No conductor is wired
    into this PR; this is a marker for whoever does that wiring next, not
    an implementation of it."""
    if group_status is None:
        if slot == 3:
            # The fourth arm key (2026-10-26) under LINK: NO LINK again.
            return "NO", "LINK", RED, CHAMPAGNE, False
        return NO_FLAME_LINK[slot % 3], None, RED, CHAMPAGNE, False
    if fault:
        return "FAULT", None, RED, CHAMPAGNE, False
    armed = group_status.get("armed")
    if armed == "armed":
        look = ("ARMED", None, GREEN, (6, 30, 12), False)
    elif armed == "disarmed":
        look = ("OFF", None, BRONZE, CHAMPAGNE, False)
    else:
        # held: dwell_s counts down (re-arm dwell, chatter); otherwise the
        # reason is shown, flashing exactly when CONTRACT.md's own `amber`
        # field says cycling the arm is the fix.
        flashing = group_status.get("amber") == "flashing"
        dwell = group_status.get("dwell_s") or 0
        if dwell > 0:
            look = (str(int(dwell)), None, AMBER, (40, 20, 0), flashing)
        else:
            reason = group_status.get("reason") or "held"
            short = _SHORT_REASON.get(reason, "HELD")
            line1, line2 = (short.split(" ", 1) if " " in short
                           else (short, None))
            if short == "SHOW LOST":
                # The approved look draws SHOW LOST like OFF (bronze), not
                # amber: the group is disarmed until the arm is cycled.
                look = (line1, line2, BRONZE, CHAMPAGNE, flashing)
            else:
                look = (line1, line2, AMBER, (40, 20, 0), flashing)
    return look


# flamesafe's own reason sentences (CONTRACT.md), to a label that fits a
# 56x30 px key face. Anything not in this table still shows (dim "HELD"),
# it is just less specific; nothing here hides a real reason, it only
# abbreviates the ones already catalogued in CONTRACT.md.
_SHORT_REASON = {
    "cycle the arm": "ARM AGAIN",   # plain words (Jeff, 2026-10-23)
    "dirty edge": "DIRTY EDGE",
    "re-arm dwell": "WAIT",
    "chatter": "CHATTER",
    "arm input stale": "NO SIGNAL",
    "arm input has never asserted": "NEVER ARMED",
    "safety program fault": "FAULT",
    "Show program stopped answering: disarmed. Cycle the arm to re-arm "
    "once it is back.": "SHOW LOST",
    "Show program has not answered yet: disarmed. Cycle the arm once it "
    "is running.": "SHOW LOST",
    # Round 4 of the safety review, item B (flamesafe/composer.py's
    # OTHER_SENDER): a cycle is refused while another sender is on the arm
    # link. Hand-copied: this module never imports flamesafe.
    "Another sender is on the arm link: a cycle cannot arm until it "
    "stops. Cycle the arm again once it has gone.": "OTHER SENDER",
    # Second-copy guard (2026-10-03, flamesafe/composer.py's
    # FLAME_OTHER_SENDER): a second sender on the show program's flame link
    # (every group disarmed), that link just changing hands (which every
    # normal ltcplay restart does, so this shows for about half a second
    # after one), or a flood on it.
    "Another sender is on the show program link, or it just changed "
    "hands: every group is disarmed and a cycle cannot arm until that has "
    "settled. Cycle the arm again once it has.": "OTHER SENDER",
    # The show's Abort from the rack screen or phone (flamesafe's
    # disarm_all, CONTRACT.md 2026-10-02): flashing, cycle the arm.
    "Disarmed by the show's Abort. Cycle the arm to re-arm.": "ABORTED",
}


def role_gate(user, action):
    """None if the signed-in user's role (users.py, through the engine) may
    do `action` from the deck, else a refusal sentence. Only "arm" is
    asked here: the deck's other presses that a role can restrict go to
    the engine, which checks them itself. ABORT, Hold and every disarm are
    NEVER passed through this function (users.py, the safety rule), and
    `user` None (the engine not heard from) never refuses: the deck's own
    rules then decide alone, as before users existed."""
    if action in ("abort", "disarm", "hold") or not isinstance(user, dict):
        return None
    if user.get("stale"):
        return (f"the Stream Deck's {action} key was pressed, but the deck "
                f"has not heard from the engine, so who is signed in on the "
                f"Rack screen is not known. Nothing was done; it works again "
                f"once the engine answers.")
    may = user.get("may") if isinstance(user.get("may"), dict) else {}
    if may.get(action) is not False:
        return None
    who = user.get("name") or "nobody signed in"
    return (f"the Stream Deck's {action} key was pressed, but the role "
            f"matrix does not let {who} ({user.get('role') or 'signed out'})"
            f" {action}. Sign in on the Rack screen as someone who may, or "
            f"change the matrix on the Users screen. Nothing was done.")


def announce_refusal_word(text):
    """The engine's refusal of an announcement press, in a word or two for
    the pressed key (page 2). Display only."""
    t = str(text or "").lower()
    for needle, word in (("recording missing", "NO FILE"),
                         ("already playing", "ONE PLAYING"),
                         ("show ended", "SHOW ENDED"),
                         ("resumed while", "RESUMED"),
                         ("status", "PAGE STALE"),
                         ("output device", "NO OUTPUT"),
                         ("not attached", "NO OUTPUT"),
                         ("not running", "NOT SET UP"),
                         ("could not be reached", "NO ENGINE")):
        if needle in t:
            return word
    return "REFUSED"


def deck_key_name(k, names, latched=False, morning=False, ack=False,
                  page=1):
    """What the audit log calls a deck key (audit.py). The Start key keeps
    its old name, "Start now", whatever it reads (review of PR #120, P2-6:
    the name an audit search finds stays the same); what a Stop hold did
    is its own audit line (action "stop")."""
    if k == TOP_START:
        return "ACK" if ack else ("FIRE" if morning else "Start now")
    if page == 2:
        if k in PAGE2_PLAY:
            return f"announce {PAGE2_PLAY[k]}"
        if k == PAGE2_STOP:
            return "announce stop"
        if k == PAGE2_BACK:
            return "announce back"
    if page == HAZER_PAGE:
        if k in PAGE3_HAZER:
            return f"hazer key {PAGE3_HAZER[k] + 1}"
        if k == PAGE3_OFF:
            return "hazers off"
        if k == PAGE3_BACK:
            return "hazers back"
    if k in GROUP_KEYS and k - GROUP_KEYS[0] < len(names):
        return f"arm key {names[k - GROUP_KEYS[0]]}"
    if k == SPARE_KEY:
        return "announce key"
    return f"key {k}"


USER_TOAST_S = 3.0      # the Start key names a new sign-in this long
# Who is signed in, as the engine last said, is trusted this long; past it
# the deck's arm keys wait (ScreenKeys.user, role_gate). Longer than the
# screen holds' SCREEN_STALE_S so a slow engine answer does not refuse an
# arm, short enough that a sign-out reaches the keys at once.
USER_STALE_S = 2.0
# What ScreenKeys.user() answers while the engine is not heard from.
USER_UNKNOWN = {"name": None, "role": None, "active": None, "stale": True,
                "may": {"arm": False, "show": False}}
# Who the deck's journal names when users are on and nobody is signed in
# on this machine (users.UNSIGNED; this module never imports users.py).
UNSIGNED = "unsigned"


def operator_gate(current_operator, action):
    """None if `action` may proceed, else a refusal sentence. ABORT and a
    DISARM press (action="disarm") are never gated: see module docstring.
    Everything else (arm, start, hold, resume) needs a chosen operator.

    This split is Jeff's own policy decision (safety review of PR #31,
    item 7), blessed explicitly, not an oversight: a press that only
    REDUCES risk must never wait on a picker, so Abort and disarm are
    never routed through this function with those two actions, anywhere
    in this module. Do not "fix" that by adding a gate call at an Abort
    or disarm call site; that would be undoing a deliberate decision."""
    if action in ("abort", "disarm"):
        return None
    if current_operator == UNSIGNED:
        # Users are on and nobody is signed in on this machine (2026-10-28):
        # the deck is view only, like the rack screen.
        return (f"the Stream Deck's {action} key was pressed but nobody is "
                f"signed in on the Rack screen. Sign in there first. Nothing "
                f"was done.")
    if current_operator:
        return None
    return (f"the Stream Deck's {action} key was pressed but no operator is "
            f"chosen. Pick an operator on the Rack screen first. Nothing "
            f"was done.")


class AbortHold:
    """A hold-to-fire timer: held down continuously for `hold_s` fires
    once; let go early and it resets to nothing. Pure (a clock function is
    passed in), so the exact behaviour is unit-tested without hardware.

    Despite the name this is generic, not Abort-specific: Controller also
    builds one per flame group, at ARM_HOLD_S, for arming (item 8, Jeff,
    2026-10-01) -- the same shape of hold, the same kind of fill feedback,
    just a different duration and a different key. STOP_HOLD_S (0.5 s,
    the deck's old Abort key's, from the demo) is this class's own
    default; Stop uses it, groups use ARM_HOLD_S explicitly."""

    def __init__(self, hold_s=STOP_HOLD_S):
        self.hold_s = hold_s
        self._down_at = None

    def press(self, now):
        self._down_at = now

    def release(self):
        self._down_at = None

    def fraction(self, now):
        """0..1 while held, 0 once released. 1.0 means "fires now"."""
        if self._down_at is None:
            return 0.0
        return min(1.0, (now - self._down_at) / self.hold_s)

    def fired(self, now):
        """True exactly once, the instant the hold reaches 1.0 (consumes
        the press: calling it again before the next press returns False,
        matching the demo's own abort_down_at = None on fire)."""
        if self._down_at is not None and (now - self._down_at) >= self.hold_s:
            self._down_at = None
            return True
        return False


def edges(prev, cur):
    """Which key indices went from up to down between two reads, in a
    fixed, deterministic order (see module docstring, failure mode 4)."""
    return [k for k in range(len(cur)) if cur[k] and not prev[k]]


def releases(prev, cur):
    return [k for k in range(len(cur)) if prev[k] and not cur[k]]


# --------------------------------------------------------------------------
# A plain local HTTP read of ltcplay's own web server: loopback traffic
# skips the token (web.py's _authorised), so this is an ordinary GET/POST,
# the same kind the Rack screen's own page already makes. Never imports
# web.py or schedule_service.py: this module may run as its own process,
# entirely separate from `ltc serve`.
# --------------------------------------------------------------------------
_OPENER = []


def _opener():
    """One urllib opener for the whole process, built on first use (no
    proxies: the engine is on this machine). Building one per request costs
    about 20 ms of CPU on Python 3.12, which makes a fresh TLS context and
    loads the system certificates every time, and on Windows the memory
    grew steadily with it (the deck program, show PC and CI, 2026-10-04:
    about 14 to 190 MB an hour at the 4 Hz engine poll)."""
    if not _OPENER:
        _OPENER.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({})))
    return _OPENER[0]


POLL_HZ = 4.0            # how often the BACKGROUND thread refreshes the
                         # cache; the main loop never waits on this
FETCH_TIMEOUT_S = 1.0    # per HTTP GET, same as before -- it just no
                         # longer matters to the main loop's own timing


# Polls in a row that must fail before LocalSchedule says the run or the
# scheduler is not known (review of #130, second pass, 6): one dropped poll
# never flips the Start/Stop key's label.
STATE_MISSES = 2


class LocalSchedule:
    """Reads the operator and the scheduler's state from a running `ltc
    serve --schedule` on this machine, OFF the main loop (safety review of
    PR #31, item 5): a BACKGROUND thread does the actual HTTP GETs, each
    with its own FETCH_TIMEOUT_S timeout, and current_operator() /
    show_running() only ever hand back the last answer that thread cached,
    synchronously, in memory -- never a socket call, never a wait. The old
    design cached per-call but still did the GET on whichever thread asked
    (the main loop, every frame once the cache aged out), so a web server
    that hung for its own 1 s timeout stalled key reads and arm-frame
    sends for that same second; that is exactly the failure the review
    found by actually running it. start() begins polling; without it,
    both methods simply return their safe defaults ("" / None) forever,
    the same answers a server that cannot be reached would give, so a
    caller that forgets to start() the poller fails exactly as safe as a
    caller whose server is down."""

    def __init__(self, base_url, poll_hz=POLL_HZ, fetcher=None):
        self.base_url = base_url.rstrip("/")
        self._period = 1.0 / poll_hz
        self._fetch = fetcher or self._http_fetch
        self._lock = threading.Lock()
        self._operator = ""
        self._show_running = None
        self._held = None
        self._run_live = None
        # Polls in a row that could not read the scheduler, or the run:
        # one failed poll keeps the last answer (review of #130, second
        # pass, 6), STATE_MISSES of them make it unknown.
        self._sched_misses = 0
        self._play_misses = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Begin the background poll. Safe to call more than once."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-deck-schedule-poll")
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    def _loop(self):
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self._period)

    def _poll_once(self):
        op = self._fetch("/api/schedule/operator")
        running = self._fetch("/api/schedule/state")
        # Whether Run has been pressed and playback is going (the page's
        # own /api/state "running"), for the Start/Stop key (2026-10-26).
        play = self._fetch("/api/state")
        def answered(doc):
            return isinstance(doc, dict) and "_http_status" not in doc
        with self._lock:
            if answered(play):
                self._run_live = bool(play.get("running"))
                self._play_misses = 0
            else:
                self._play_misses += 1
                if self._play_misses >= STATE_MISSES:
                    self._run_live = None
            if isinstance(op, dict):
                self._operator = str(op.get("current_operator") or "")
            else:
                # Item 8 (round 2 of the safety review): a regression left
                # this branch doing nothing, so the LAST successfully
                # fetched operator name was kept forever once ltc serve
                # became unreachable -- a group could then be armed under a
                # name no longer actually confirmed present at the rig.
                # Revert to "" on every failed poll, matching
                # current_operator()'s own documented default and what the
                # pre-fix-round code actually did.
                self._operator = ""
            if answered(running) and running.get("ok"):
                self._show_running = running.get("state") in ("SHOW",
                                                               "PAUSED")
                self._held = running.get("state") in ("HOLD", "PAUSED")
                self._sched_misses = 0
            elif isinstance(running, dict) and \
                    running.get("_http_status") == 404 and answered(play):
                # The engine answers but runs no scheduler (ltc serve
                # without --schedule): no show can be live, so the key is
                # never stuck on unknown (review of #130, second pass, 6).
                self._show_running = False
                self._held = False
                self._sched_misses = 0
            else:
                self._sched_misses += 1
                if self._sched_misses >= STATE_MISSES:
                    self._show_running = None
                    self._held = None

    def _http_fetch(self, path):
        """One blocking GET. Only ever called from the background thread
        (_loop); never from current_operator() or show_running()."""
        try:
            with _opener().open(self.base_url + path,
                                timeout=FETCH_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # The engine answered, with an HTTP error: said apart from no
            # answer at all (a 404 is an engine with no scheduler).
            return {"_http_status": e.code}
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def current_operator(self):
        """The last operator the background poll read, or "" before the
        first successful poll, or forever if start() was never called.
        Never touches the network: see the class docstring."""
        with self._lock:
            return self._operator

    def run_live(self):
        """True while Run has been pressed and playback is going (ltc
        serve's /api/state "running"), False when stopped, None if
        unreachable or before the first poll. Never touches the network."""
        with self._lock:
            return self._run_live

    def show_running(self):
        """True while the scheduler's own state says a show is playing or
        held (schedule.py's SHOW/PAUSED -- the same two names conductor.py
        reuses for LASER_STATES), False otherwise, None if unreachable (or
        before the first successful poll). Never touches the network."""
        with self._lock:
            return self._show_running

    def held(self):
        """True while the scheduler is on HOLD (between shows) or PAUSED (a
        show held), False otherwise, None if unreachable: the Hold key's
        Hold or Resume (Controller._held_hint). Never touches the
        network."""
        with self._lock:
            return self._held


ENGINE_POST_TIMEOUT_S = 3.0
# Stop's halt (/api/stop) waits for a live show's fade to finish before
# playback stops (web.HALT_FADE_WAIT_S, twice at most, 6 s, plus the
# scheduler's lock and the stop itself), so its post waits longer than a
# press (review of #130, 5). A Stop that times out reads CHECK RACK, never
# ENGINE FAULT: the engine took it and may still be working on it.
STOP_POST_TIMEOUT_S = 15.0
ENGINE_QUEUE_MAX = 50


class EngineResult:
    """What a press handed to EngineConductor answers at once: Controller
    journals `sentence`, as a fault when `ok` is False."""

    def __init__(self, ok, sentence):
        self.ok = ok
        self.sentence = sentence


class EngineConductor:
    """The engine (`ltc serve`), for Controller, reached over this
    machine's loopback (PR #43 review, finding 8).

    What the deck asks of it since its Hold and Abort keys went
    (2026-10-26): stop_run() (POST /api/stop, the Rack screen's Stop, which
    also sends the flame link's disarm_all and ends live flame programming,
    review of #126, P2-1), acknowledge() and alert(); snapshot() for the
    latch, fault and cues_fault for the Start key's face. hold(), resume(),
    abort() and reset() are still here, duck-typed like
    conductor.Conductor, for any caller; the deck no longer calls them.

    Each press is POSTed on a BACKGROUND thread, never the main loop (the
    same rule as LocalSchedule and DeckJournal: a slow or hung web server
    must not delay a key read or an arm frame), so a press returns at once
    saying it was sent; the engine's answer is journaled when it comes, a
    refusal or no answer as a fault. The deck's Stop has already sent every
    group's wanted state false on the arm link before this is asked.

    snapshot() never touches the network and never raises: a background
    poll caches GET /api/conductor. "latched" is the newer of an abort() or
    reset() asked through here and the engine's last answer (an Abort or
    Reset from a screen reaches the deck as the engine's answer), and
    stays as last answered while the engine cannot be reached."""

    def __init__(self, base_url, journal=None, poll_hz=POLL_HZ,
                 clock=time.monotonic, wall=time.time, poster=None,
                 fetcher=None):
        self.base_url = base_url.rstrip("/")
        self._journal = journal or (lambda text, **kw: None)
        self._period = 1.0 / poll_hz
        self._clock = clock
        self._wall = wall
        self._poster = poster or self._http_post
        self._fetch = fetcher or self._http_get
        self._lock = threading.Lock()
        self._engine = None        # (snapshot dict, monotonic time it came)
        self._local = (False, None)   # (latched, monotonic time of press)
        self._q = queue.Queue(maxsize=ENGINE_QUEUE_MAX)
        # Announcement presses have a queue and a thread of their own: a
        # Play can take a while at the engine (it reads the file), and it
        # must never delay or crowd out Stop, an Acknowledge or anything
        # else on the main queue.
        self._aq = queue.Queue(maxsize=ENGINE_QUEUE_MAX)
        self._stop = threading.Event()
        self._threads = []
        self.dropped = 0
        self.alert_delivered = None   # the last alert raise: None, False, True
        # The last press the engine did not take (or could not be reached
        # for), for the deck's own keys to show, not only its console; ""
        # once a press is taken again (fix round 2, E).
        self.fault = ""
        # The words on key 0 for that fault: ENGINE FAULT, or DISARM FAILED
        # when a Stop stopped playback but the engine's flame disarm failed
        # (third review of #126, P2-2).
        self.fault_face = ("ENGINE", "FAULT")
        # The engine's flame cue fault from /api/conductor (review of PR
        # #43, P1-3), "" when none: shown on the deck's own keys.
        self.cues_fault = ""
        self._last_ok = None      # when the engine last answered a poll
        # The engine's answer to the last announcement press: (ok,
        # sentence, clock()), or None. Page 2's own faces show it.
        self.announce_said = None

    def start(self):
        if self._threads:
            return
        self._started_at = self._clock()
        self._stop.clear()
        for target, name in ((self._post_loop, "ltcplay-deck-engine-press"),
                             (self._announce_loop,
                              "ltcplay-deck-engine-announce"),
                             (self._poll_loop, "ltcplay-deck-engine-poll")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for q in (self._q, self._aq):
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    # -- presses ------------------------------------------------------------
    def hold(self, who="", screen="Stream Deck"):
        return self._press("hold", who, screen)

    def resume(self, who="", screen="Stream Deck"):
        return self._press("resume", who, screen)

    def abort(self, who="", screen="Stream Deck"):
        with self._lock:
            self._local = (True, self._clock())
        return self._press("abort", who, screen)

    def acknowledge(self, who="", screen="Stream Deck"):
        """A deck key pressed while an alert is open (alerts.py): one
        Acknowledge to the engine's alert route, clearing every open
        alert and changing nothing on the rig. On the queue, like Hold."""
        return self._press("alert", who, screen, action="acknowledge")

    def alert(self, body):
        """This process's own alerts: the deck hardware lost or back
        ({"raise": "deck", "text": ..., "options": [...]}), posted to the
        same route from the show machine. On the queue, never the main
        loop. alert_delivered reads False from here until the engine
        answers 200 to it (_post_one), so the caller can post it again."""
        self.alert_delivered = False
        return self._press("alert", "", "Stream Deck", **body)

    def reset(self, who="", screen="Stream Deck"):
        with self._lock:
            self._local = (False, self._clock())
        return self._press("reset", who, screen)

    def stop_run(self, who="", screen="Stream Deck"):
        """The Start/Stop key's Stop (Jeff, 2026-10-26): POST /api/stop on
        ltc serve, the Rack screen's Stop route, with "halt": the deck's
        Stop is a complete halt of everything. The engine disarms every
        flame group and every laser, ends a scheduled show that is live
        (the scheduler's Stop: faded to dark like an Abort, never the Abort
        latch), stops playback and blacks the outputs out; no show starts
        until Run is pressed again. Taken while aborted, never behind a
        role ("safety", users.WEB_ROUTES). On the queue, like Hold."""
        return self._press("stop", who, screen, path="/api/stop", halt=True)

    def announce(self, ann_id, who="", screen="Stream Deck"):
        """Page 2's play key: one press, POST /api/remote/announce-play on
        the press thread. The engine decides (a recording missing, no
        output device, one already playing) and journals it; a refusal is
        never an ENGINE FAULT on the Start key."""
        return self._press("announce-play", who, screen, id=str(ann_id))

    def announce_stop(self, who="", screen="Stream Deck"):
        """Page 2's STOP ANNC.: the announcement's sound only."""
        return self._press("announce-stop", who, screen)

    def hazer(self, name, on, who="", screen="Stream Deck"):
        """Page 3's hazer key: POST /api/remote/hazer on the press thread.
        The engine decides (the role matrix for ON, an Abort standing,
        flamesafe's status) and journals it."""
        return self._press("hazer", who, screen, hazer=str(name),
                           on=on is True)

    def hazers_off(self, who="", screen="Stream Deck"):
        """Page 3's HAZERS OFF: every hazer OFF, never refused."""
        return self._press("hazers-off", who, screen)

    def _press(self, name, who, screen, path=None, **extra):
        body = {"who": who, "screen": screen or "Stream Deck",
                "seen": int(self._wall() * 1000)}
        body.update(extra)
        if name == "abort":
            body["confirmed"] = True
            # Abort never waits behind another press (fix round 2, D: a deck
            # Abort waited 2.6 s behind a slow Hold): a thread of its own,
            # at once, like the scheduler's own Abort bypass.
            threading.Thread(target=self._post_one, args=(name, body),
                             daemon=True,
                             name="ltcplay-deck-engine-abort").start()
            return EngineResult(True, "abort sent to the engine's show "
                                      "conductor at once; the engine's "
                                      "journal says what it did.")
        if name == "stop":
            # Stop never waits behind another press either (review of
            # #130, 7): a thread of its own, at once.
            threading.Thread(target=self._post_one,
                             args=(name, body, path), daemon=True,
                             name="ltcplay-deck-engine-stop").start()
            return EngineResult(True, "Stop sent to the engine at once; "
                                      "the engine's journal says what it "
                                      "did.")
        q = self._aq if name.startswith("announce-") else self._q
        try:
            q.put_nowait((name, body) if path is None
                         else (name, body, path))
        except queue.Full:
            self.dropped += 1
            return EngineResult(False, f"{name} could NOT be sent to the "
                                       f"engine's show conductor: too many "
                                       f"presses are waiting for it.")
        return EngineResult(True, f"{name} sent to the engine's show "
                                  f"conductor; the engine's journal says "
                                  f"what it did.")

    def _post_loop(self):
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                return
            self._post_one(*item)

    def _announce_loop(self):
        while not self._stop.is_set():
            item = self._aq.get()
            if item is None:
                return
            self._post_one(*item)

    def _post_one(self, name, body, path=None):
        """One press to the engine; its answer journaled, and kept as
        the deck's engine fault when the engine did not take it. `path`:
        a route of its own (Stop's /api/stop), else /api/remote/<name>."""
        doc = {}
        try:
            got = self._poster(path or "/api/remote/" + name, body)
            ok, text = got[0], got[1]
            if len(got) > 2 and isinstance(got[2], dict):
                doc = got[2]
        except Exception as e:
            ok, text = False, f"{type(e).__name__}: {e}"
        # Stop (/api/stop) is a complete halt (review of #126, P2-1; Jeff,
        # 2026-10-26). Every step of it that failed is said, all of them,
        # never only the first (review of #130, 3): the flame disarm, the
        # laser disarm, a live show the scheduler did not end. A fault on
        # the deck's own key, never a quiet "done"; DISARM FAILED when a
        # disarm failed. The deck's own arm link disarm went first anyway.
        fails = []
        # The engine answered Stop with an HTTP error (a 500 after the halt
        # ran): its own failed steps are still read and said, never ENGINE
        # FAULT (third review of #130, 4).
        errored = name == "stop" and not ok and isinstance(doc, dict) and \
            doc.get("_http_status") is not None
        if name == "stop" and (ok or errored) and isinstance(doc, dict):
            flames = doc.get("flames")
            lasers = doc.get("lasers")
            show = doc.get("show")
            if isinstance(flames, dict) and flames.get("disarmed") is False:
                fails.append(("disarm",
                              f"the flame disarm FAILED ("
                              f"{flames.get('text') or 'no reason given'}): "
                              f"use Disarm all flame groups on the Rack "
                              f"screen or the hardware e-stop"))
            if isinstance(lasers, dict) and lasers.get("disarmed") is False:
                fails.append(("disarm",
                              f"the laser disarm FAILED ("
                              f"{lasers.get('text') or 'no reason given'}): "
                              f"use Disarm all lasers on the Rack screen"))
            if isinstance(show, dict) and show.get("ok") is False:
                fails.append(("show",
                              f"the scheduler did NOT end the show ("
                              f"{show.get('text') or 'no reason given'}): "
                              f"use Abort on the Rack screen"))
            blank = doc.get("lasers_blank")
            if isinstance(blank, dict) and blank.get("ok") is False:
                fails.append(("disarm",
                              f"the laser blank FAILED ("
                              f"{blank.get('text') or 'no reason given'}): "
                              f"use Disarm all lasers on the Rack screen"))
            # Anything else the engine says failed (the halt itself
            # raising, review of #130, second pass, 5), said too.
            told = {"flames", "lasers", "show", "laser blank",
                    "playback stop"}
            if "playback stop" in (doc.get("failed") or []):
                # Only playback did not stop: the flames and lasers were
                # disarmed (said apart, review of #130, low items, 1).
                fails.append(("playback",
                              f"playback did NOT stop ("
                              f"{doc.get('playback_error') or doc.get('error') or 'no reason given'}"
                              f"): press Stop on the Rack screen"))
            for step in doc.get("failed") or []:
                if step not in told:
                    fails.append(("disarm",
                                  f"the engine's {step} step FAILED ("
                                  f"{doc.get('error') or 'no reason given'}"
                                  f"): use Abort on the Rack screen or the "
                                  f"hardware e-stop"))
        disarm_failed = any(k == "disarm" for k, _t in fails)
        playback_only = bool(fails) and all(k == "playback" for k, _t in fails)
        if errored and playback_only:
            # The halt worked; only the playback stop did not: say what
            # was disarmed, apart from a disarm that failed (review of
            # #130, low items, 1).
            done = [w for k, w in (("flames", "every flame group"),
                                   ("lasers", "every laser"))
                    if isinstance(doc.get(k), dict) and
                    doc[k].get("disarmed") is True]
            text = ((f"Stop disarmed {' and '.join(done)}, but "
                     if done else "") + fails[0][1])
        elif errored:
            text = (f"the engine answered Stop with an error ({text})"
                    + ("; and " + "; and ".join(t for _k, t in fails)
                       if fails else "") + ": check the Rack screen")
        elif fails:
            ok = False
            text = "playback stopped, but " + "; and ".join(
                t for _k, t in fails)
        if name == "abort" and doc.get("stopped") is False:
            # The engine disarmed every flame group but had no show to stop
            # (review of PR #43, P0-1), so nothing is latched: the Abort key
            # must not read RESET for a latch the engine does not have.
            with self._lock:
                if self._local[0]:
                    self._local = (False, self._clock())
        # The engine answered, and what the press asked for is already so
        # (schedule_service.operator_press, "nothing_to_do": a Hold while
        # the schedule is already on hold or a show already paused, or the
        # night over; a Resume with nothing held). The engine is fine and
        # the rig is as asked: journaled, never ENGINE FAULT (2026-10-21,
        # the show PC: Hold showed ENGINE FAULT while the Hold had worked).
        noop = (not ok and name in ("hold", "resume") and
                doc.get("nothing_to_do") is True)
        # A Stop the engine took but did not answer in STOP_POST_TIMEOUT_S
        # is not an engine that cannot be reached (review of #130, second
        # pass, 5): CHECK RACK, the halt may still be under way.
        slow = name == "stop" and not ok and isinstance(doc, dict) and (
            doc.get("timeout") is True or errored)
        if name in ("announce-play", "announce-stop"):
            # An announcement is not a press on the rig: a refusal (a
            # recording missing, no output device) is journaled and shown
            # on page 2, never as ENGINE FAULT on the Start key.
            key = (str(body.get("id") or "") if name == "announce-play"
                   else "stop")
            self.announce_said = (bool(ok), str(text), self._clock(), key)
        elif name != "alert":
            # An alert post is not a press on the rig: a refused or late
            # Acknowledge (two keys inside one poll) is journaled below,
            # never shown as ENGINE FAULT on the keys.
            self.fault = "" if (ok or noop) else f"{name.title()}: {text}"
            show_live = any(k == "show" for k, _t in fails)
            self.fault_face = ("DISARM", "FAILED") if disarm_failed else \
                ("SHOW NOT", "STOPPED") if show_live else \
                ("PLAYBACK", "FAILED") if playback_only else \
                ("CHECK", "RACK") if slow else ("ENGINE", "FAULT")
        elif "raise" in body:
            self.alert_delivered = bool(ok)
        if noop:
            line = (f"Stream Deck {name.title()}: nothing to do, the engine "
                    f"says: {text}")
        elif ok:
            line = f"Stream Deck {name.title()}: the engine says: {text}"
        elif slow or (name == "stop" and not ok and
                      doc.get("stopped") is not None):
            line = f"Stream Deck Stop: {text}."
        else:
            line = (f"Stream Deck {name.title()}: the engine's show "
                    f"conductor did NOT take it: {text}")
        try:
            self._journal(line, fault=not (ok or noop), action=name,
                          who=body.get("who") or "",
                          screen=body.get("screen") or "Stream Deck")
        except Exception:
            pass

    def _http_post(self, path, body):
        """(ok, sentence, answer) for one blocking POST, from the press
        thread; answer is the engine's JSON object, or {}."""
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        opener = _opener()
        wait = STOP_POST_TIMEOUT_S if path == "/api/stop" else \
            ENGINE_POST_TIMEOUT_S
        try:
            with opener.open(req, timeout=wait) as r:
                doc = json.loads(r.read().decode("utf-8") or "{}")
            if not isinstance(doc, dict):
                doc = {}
            return (bool(doc.get("ok", True)),
                    str(doc.get("text") or "done"), doc)
        except urllib.error.HTTPError as e:
            doc = {}
            try:
                doc = json.loads(e.read().decode("utf-8") or "{}")
                why = doc.get("text") or doc.get("error") or str(e)
            except Exception:
                why = str(e)
            doc = doc if isinstance(doc, dict) else {}
            # The engine answered, with an error: said apart from an engine
            # that cannot be reached (third review of #130, 4).
            doc.setdefault("_http_status", e.code)
            return False, str(why), doc
        except urllib.error.URLError as e:
            # urllib wraps everything up to sending the request (connecting,
            # a connect timeout, a refusal) in URLError: the engine could
            # not be reached (third review of #130, 3).
            return False, (f"ltc serve could not be reached at "
                           f"{self.base_url} ({e})")
        except TimeoutError:
            # Only a timeout AFTER the request was sent (waiting for the
            # answer): the engine took it and has not answered yet (review
            # of #130, second pass, 5). If it has stopped answering its
            # polls too, the key says ENGINE FAULT all the same.
            return False, (f"ltc serve took the request but did not "
                           f"answer within {wait:g} s; it may still be "
                           f"working on it. Check the Rack screen"), \
                {"timeout": True}
        except (OSError, ValueError) as e:
            return False, (f"ltc serve could not be reached at "
                           f"{self.base_url} ({e})")

    # -- what the engine says -----------------------------------------------
    def _poll_loop(self):
        while not self._stop.is_set():
            got = self._fetch("/api/conductor")
            snap = got.get("conductor") if isinstance(got, dict) else None
            cues = got.get("flame_cues") if isinstance(got, dict) else None
            self.cues_fault = str((cues or {}).get("fault") or "") \
                if isinstance(cues, dict) else ""
            if isinstance(snap, dict):
                # The answer's own read-only audio field rides along with
                # the conductor's state (audio_lost reads it).
                snap = dict(snap, audio=got.get("audio"))
            with self._lock:
                self._engine = ((snap, self._clock())
                                if isinstance(snap, dict) else None)
                if self._engine is not None:
                    self._last_ok = self._clock()
            self._stop.wait(self._period)

    def _http_get(self, path):
        opener = _opener()
        try:
            with opener.open(self.base_url + path,
                             timeout=FETCH_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def unreachable(self):
        """True once the engine has not answered a poll for 2 s (or never
        has, after the first 2 s): for the deck's keys to show."""
        with self._lock:
            last = self._last_ok
        if last is None:
            return bool(self._threads) and self._clock() - \
                self._started_at > 2.0
        return self._clock() - last > 2.0

    def answered_at(self):
        """This process's clock time of the engine's latest answer to the
        /api/conductor poll, or None while it is not answering. A new
        value is a fresh answer: the deck's top-row keys flash on these,
        so a frozen engine freezes them (Controller.engine_blink)."""
        with self._lock:
            return None if self._engine is None else self._engine[1]

    def audio_lost(self):
        """True while the engine's latest answer says the show audio is
        lost mid-show (/api/conductor's read-only "audio" field: a cue is
        playing and the show clock has stopped following the audio).
        False while the engine is not answering: the deck then shows the
        engine's silence (ENGINE FAULT), never a remembered loss."""
        with self._lock:
            engine = self._engine
        if engine is None:
            return False
        audio = engine[0].get("audio")
        return bool(isinstance(audio, dict) and audio.get("lost"))

    def snapshot(self):
        """{"latched", "look"}: never the network, never raises."""
        with self._lock:
            engine, (local, pressed_at) = self._engine, self._local
        if engine is not None and (pressed_at is None or
                                   engine[1] > pressed_at):
            snap = engine[0]
            return {"latched": bool(snap.get("latched")),
                    "look": snap.get("look") or ""}
        return {"latched": local, "look": ""}


# --------------------------------------------------------------------------
# Screen and browser arming (Jeff, 2026-10-03): a signed-in operator's
# "hold to arm" on the remote page (ltcplay/remote.py) reaches flamesafe
# THROUGH THIS PROCESS, as a remote press of the same group key. There is
# still exactly one sender on flamesafe's arm link (this deck process), so
# every flamesafe rule (consent, dwell, chatter, edge quiet, the round-4
# veto, the post-Abort window, the second-copy guard) applies unchanged,
# and so does every deck rule (the hold, the re-arm refractory window, the
# latched refusal, the operator gate). See flamesafe/CONTRACT.md, "Arming
# from a screen".
#
# A screen hold counts only while the engine says it is fresh, and only
# while this process has heard from the engine within SCREEN_STALE_S: a
# dropped page, a dead engine or an unreachable one all read as "let go".
# It fires only once BOTH the deck's own ARM_HOLD_S has run since this
# process saw the press AND the engine has seen the page's heartbeats
# carry on for SCREEN_HOLD_S (held_s, measured from beats the engine
# actually received), so a page that drops before then can never finish
# an arm.
# --------------------------------------------------------------------------
SCREEN_POLL_HZ = 20.0
SCREEN_STALE_S = 0.3
SCREEN_HOLD_S = 1.0       # must equal remote.SCREEN_HOLD_S (selftest pins it)
# A screen hold this deck would not take is reported back to the engine on
# its deck-input reads for this long (2026-10-22: the page says why, never
# a bar that fills and arms nothing). Reporting only: what the deck does
# with the hold is decided exactly as before.
SCREEN_REFUSED_KEEP_S = 1.0


class ScreenKeys:
    """The remote page's arm holds and per-group disarms, read from the
    engine's /api/remote/deck-input on a BACKGROUND thread, like
    LocalSchedule: holds() and new_disarms() never touch the network."""

    def __init__(self, base_url, poll_hz=SCREEN_POLL_HZ, fetcher=None,
                 clock=time.monotonic, report=None):
        self.base_url = base_url.rstrip("/")
        self._period = 1.0 / poll_hz
        self._fetch = fetcher or self._http_fetch
        self._clock = clock
        # () -> the deck's worst USB frame write in ms (UsbMeter), or None:
        # carried to the engine on every deck-input read for the rack
        # screen's Deck chip (2026-10-21). Display only.
        self.report = report
        # () -> key image writes the deck did not take in the last minute
        # (UsbMeter.failed_count): usb_fail, for the same chip (review of
        # PR #83, P2-2). Display only.
        self.report_failed = None
        # () -> whether the USB deck is here (Controller.deck_present):
        # hw=1 or hw=0; and () -> ms since the last arm frame went out
        # (ArmSocket.sent_age_ms): arm_ms. The Deck chip's live truth
        # (2026-10-26: green with the deck unplugged). Display only.
        self.report_present = None
        self.report_arm = None
        # () -> Controller.rearm_report(): rearm (ms left in each group's
        # re-arm refractory window) and prefused (a purge arm refused for
        # it), for the rack screen's words (2026-10-26). Display only.
        self.report_rearm = None
        # () -> True when ltcplay_deck.json could not be used and the keys
        # are in the default layout: said as layout=default (Deck chip).
        self.report_layout_default = None
        # () -> the group indices with no key (screen-only), said as
        # nokey=4,5 for the Rack screen's flame card (review of #126, P1).
        self.report_nokey = None
        self._lock = threading.Lock()
        self._last = None          # (answer, clock) of the last good fetch
        # Whether the engine last said users are on (deck-input "user"):
        # None never heard, False no users at all, True on. Kept through an
        # unreachable engine, so a deck on a machine with no users decides
        # alone as before while the engine is quiet (review of PR #143, 4).
        self._users_on = None
        # Screen holds this deck would not take: hold id -> (code, clock),
        # carried on the next deck-input reads (remote.DECK_REFUSALS).
        self._refused = {}
        self._seen_disarm = None   # highest disarm id already handed out
        self._pending = []
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-deck-screen-poll")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)

    def _loop(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._period)

    def note_refused(self, hold_id, code):
        """The controller would not take screen hold `hold_id` (`code`, one
        of remote.DECK_REFUSALS): said to the engine on the next reads, so
        the page names the reason. Changes nothing the deck does."""
        code = str(code)
        if not isinstance(hold_id, int) or isinstance(hold_id, bool) or \
                not (code.isascii() and code.isalpha() and code.islower()
                     and len(code) <= 16):
            return
        with self._lock:
            self._refused[hold_id] = (code, self._clock())

    def note_taken(self, hold_id):
        """The controller armed a group from screen hold `hold_id`: said
        to the engine on the next reads (`taken`), so the page credits the
        hold only when this deck says it was the cause (review of PR #90,
        P3-1). Changes nothing the deck does."""
        self.note_refused(hold_id, "taken")

    def _codes(self, taken):
        now = self._clock()
        with self._lock:
            for k in [k for k, (_c, t) in self._refused.items()
                      if now - t > SCREEN_REFUSED_KEEP_S]:
                del self._refused[k]
            items = sorted((k, c) for k, (c, _t) in self._refused.items()
                           if (c == "taken") == taken)[-8:]
        return items

    def _refused_param(self):
        return ",".join(f"{k}.{c}" for k, c in self._codes(False))

    def _taken_param(self):
        return ",".join(str(k) for k, _c in self._codes(True))

    def _path(self):
        path = "/api/remote/deck-input"
        try:
            ms = self.report() if self.report is not None else None
        except Exception:                        # noqa: BLE001
            ms = None
        if isinstance(ms, int) and not isinstance(ms, bool) and ms >= 0:
            path += f"?usb_ms={ms}"
            try:
                n = self.report_failed() \
                    if self.report_failed is not None else 0
            except Exception:                    # noqa: BLE001
                n = 0
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                path += f"&usb_fail={n}"
        try:
            present = self.report_present() \
                if self.report_present is not None else None
        except Exception:                        # noqa: BLE001
            present = None
        if isinstance(present, bool):
            path += "&" if "?" in path else "?"
            path += "hw=1" if present else "hw=0"
        try:
            age = self.report_arm() if self.report_arm is not None else None
        except Exception:                        # noqa: BLE001
            age = None
        if isinstance(age, int) and not isinstance(age, bool) and age >= 0:
            path += "&" if "?" in path else "?"
            path += f"arm_ms={age}"
        try:
            got = self.report_rearm() \
                if self.report_rearm is not None else ({}, {})
        except Exception:                        # noqa: BLE001
            got = ({}, {})
        # Each copied on its own (review of #115, P2-1): one that fails
        # never drops the other.
        try:
            rearm = ",".join(f"{i}.{int(ms)}"
                             for i, ms in sorted(dict(got[0]).items())
                             if isinstance(i, int) and int(ms) > 0)
        except Exception:                        # noqa: BLE001
            rearm = ""
        try:
            pr = ",".join(f"{i}.{c}" for i, c in sorted(dict(got[1]).items())
                          if isinstance(i, int) and str(c).isalpha())
        except Exception:                        # noqa: BLE001
            pr = ""
        try:
            dflt = bool(self.report_layout_default()) \
                if self.report_layout_default is not None else False
        except Exception:                        # noqa: BLE001
            dflt = False
        if dflt:
            path += ("&" if "?" in path else "?") + "layout=default"
        try:
            nokey = ",".join(str(int(i)) for i in (
                self.report_nokey() if self.report_nokey is not None
                else ()))
        except Exception:                        # noqa: BLE001
            nokey = ""
        if nokey:
            path += ("&" if "?" in path else "?") + f"nokey={nokey}"
        if rearm:
            path += ("&" if "?" in path else "?") + f"rearm={rearm}"
        if pr:
            path += ("&" if "?" in path else "?") + f"prefused={pr}"
        try:
            ref = self._refused_param()
        except Exception:                        # noqa: BLE001
            ref = ""
        if ref:
            path += ("&" if "?" in path else "?") + f"refused={ref}"
        try:
            took = self._taken_param()
        except Exception:                        # noqa: BLE001
            took = ""
        if took:
            path += ("&" if "?" in path else "?") + f"taken={took}"
        return path

    def poll_once(self):
        ans = self._fetch(self._path())
        with self._lock:
            if not isinstance(ans, dict):
                self._last = None          # unreachable: every hold let go
                return
            self._last = (ans, self._clock())
            u = ans.get("user")
            self._users_on = isinstance(u, dict) and \
                u.get("active") is not False
            evs = [e for e in (ans.get("disarms") or [])
                   if isinstance(e, dict) and isinstance(e.get("id"), int)]
            if self._seen_disarm is None:
                # Disarms from before this process started are history.
                self._seen_disarm = max([e["id"] for e in evs] + [0])
                return
            for e in sorted(evs, key=lambda e: e["id"]):
                if e["id"] > self._seen_disarm:
                    self._pending.append(e)
                    self._seen_disarm = e["id"]

    def _http_fetch(self, path):
        try:
            with _opener().open(self.base_url + path,
                                timeout=FETCH_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def holds(self):
        """{group index: hold} for every fresh screen hold, or {} when the
        engine has not answered within SCREEN_STALE_S, says screen arming
        is off, or has none."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return {}
        ans = last[0]
        if ans.get("enabled") is not True:
            return {}
        out = {}
        for h in ans.get("holds") or []:
            if isinstance(h, dict) and h.get("fresh") is True and \
                    isinstance(h.get("group"), int):
                out[h["group"]] = h
        return out

    def new_disarms(self):
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def morning(self):
        """The engine's word on the Morning and Shutdown page (morning.py,
        deck-input "morning"): whether it is open, the purge selection's
        groups, the locks. None when the engine has not answered within
        SCREEN_STALE_S or carries none: a stale answer must never keep
        this deck re-mapped or a purge group armed."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return None
        m = last[0].get("morning")
        return m if isinstance(m, dict) else None

    def flames_live(self):
        """True while the engine's last fresh answer says live flame
        programming is on, or Programming playback with the show's flame
        cues following it (deck-input "flames_live"), False when it says
        neither, None when the engine has not answered within
        SCREEN_STALE_S."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return None
        v = last[0].get("flames_live")
        return v if isinstance(v, bool) else None

    def alerts(self):
        """The engine's open alerts (alerts.py), as its last fresh answer
        listed them; () when none, or when the engine has not answered
        within SCREEN_STALE_S (a stale list must not keep a key reading
        ACK, or acknowledge something the engine no longer shows).
        A display only kind (alerts.DISPLAY_ONLY, the aim slots,
        2026-10-28) is left out: it changes nothing on the deck, no ACK
        face, no swallowed first press, no page 2 closing."""
        from .alerts import DISPLAY_ONLY
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return ()
        return tuple(a for a in (last[0].get("alerts") or [])
                     if isinstance(a, dict)
                     and a.get("kind") not in DISPLAY_ONLY)

    def preroll(self):
        """True while the engine checks the rig before a show (preroll.py),
        as its last fresh deck-input answer said. Display only."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return False
        return last[0].get("preroll") is True

    def announce(self):
        """The engine's announcements, as its last fresh deck-input answer
        carried them ({"configured", "device_ok", "ready", "missing",
        "playing"}), or None when stale or not there. Display only: page
        2's faces. The engine decides every press."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return None
        a = last[0].get("announce")
        return a if isinstance(a, dict) else None

    def hazers(self):
        """The engine's hazers, as its last fresh deck-input answer carried
        them ({"names", "on", "down"}), or None when stale or not there.
        Display only: page 3's faces. The engine decides every press."""
        with self._lock:
            last = self._last
        if last is None or self._clock() - last[1] > SCREEN_STALE_S:
            return None
        h = last[0].get("hazers")
        return h if isinstance(h, dict) and h.get("names") else None

    def user(self):
        """Who is signed in on this machine, as the engine's last answer
        said (users.py, deck-input "user"): {"name": str or None, "role",
        "active", "may": {"arm": bool, ...}}. None when the engine answers
        with no users at all: then the deck's own rules decide alone, as
        before users existed. When the engine has not answered within
        USER_STALE_S, who is signed in is not known, and that is never
        taken as permission (Jeff's field report, 2026-10-28): USER_UNKNOWN,
        which may not arm. Abort, Stop, Hold and every disarm never ask."""
        with self._lock:
            last, users_on = self._last, self._users_on
        if last is None or self._clock() - last[1] > USER_STALE_S:
            # Not heard lately. With no users at all (the engine's last
            # word), the deck's own rules decide alone, as before users
            # existed; otherwise, never heard or users on, not known.
            # Never heard includes a deck started before the engine on a
            # machine with no users: its arm keys wait until it has heard
            # the engine once (fail safe, re-review of PR #143, 4; its
            # operator gate needs the engine then anyway).
            return None if users_on is False else dict(USER_UNKNOWN)
        u = last[0].get("user")
        return u if isinstance(u, dict) else None


JOURNAL_QUEUE_MAX = 1000
JOURNAL_POST_TIMEOUT_S = 2.0

# The deck's FIRE key for a purge (Jeff, 2026-10-17): while it is held this
# process posts a purge-fire heartbeat to the engine every PURGE_BEAT_S, on
# a thread of its own (never the main loop: the arm link's send must not
# wait on an HTTP call). The engine, and the flame link under it, drop the
# purge frame 0.25 s after the last heartbeat whatever this process does.
PURGE_BEAT_S = 0.1
PURGE_POST_TIMEOUT_S = 0.5


class PurgeFire:
    """One fire press from the deck's FIRE key: start() begins the
    heartbeats, stop() ends them and posts purge-stop. `poster(path, body)
    -> (ok, text, doc)` is EngineConductor's own POST, injectable."""

    def __init__(self, poster, journal=None, wall=time.time,
                 sleep=time.sleep):
        self._poster = poster
        self._journal = journal or (lambda text, **kw: None)
        self._wall = wall
        self._sleep = sleep
        self._lock = threading.Lock()
        self._press = None        # the newest press (_FirePress)
        self._thread = None
        self.fire_id = None
        self.firing = False
        self.last_error = ""
        self.beats = 0
        # () -> False once the deck's input loop has not run for
        # PURGE_INPUT_STALE_S (run_forever's threaded mode sets it): the
        # heartbeats stop, so a FIRE held on a deck program that froze
        # never keeps firing. None: no input loop to ask (tests).
        self.alive = None

    def start(self, who=""):
        """Begin a press. Never waits: a press still finishing its last
        POST (after a stop) is joined by the NEW press's own thread before
        its first heartbeat, so the old press's purge-stop always lands
        before the new press's first beat."""
        with self._lock:
            if self.firing:
                return False
            prev = self._thread
            press = _FirePress(who)
            self._press = press
            self.fire_id = None
            self.firing = True
            self.last_error = ""
            self._thread = threading.Thread(target=self._run,
                                            args=(press, prev),
                                            daemon=True,
                                            name="ltcplay-deck-purge-fire")
            self._thread.start()
        return True

    def stop(self, who=""):
        """End the press. Never waits on the heartbeat thread (review of
        PR #83, P3-1): it is called from the deck's input loop, and that
        thread may be inside a POST of up to PURGE_POST_TIMEOUT_S, longer
        than INPUT_STALE_S. purge-stop is posted exactly once per press,
        after its last heartbeat: by the press's own thread when it is
        still running (it sees `stop_who` on its way out), else here, on
        a thread of its own. The choice is made under _lock."""
        with self._lock:
            was = self.firing
            self.firing = False
            press = self._press
            post_here = False
            if press is not None:
                press.stop.set()
                if was:
                    if press.exited:
                        post_here = True
                    else:
                        press.stop_who = who
        if post_here:
            threading.Thread(target=self._post_stop, args=(who,), daemon=True,
                             name="ltcplay-deck-purge-stop").start()
        return was

    def join(self, timeout=1.0):
        """Tests and shutdown only: wait for the heartbeat thread."""
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _body(self, who):
        return {"who": who or "", "screen": "Stream Deck",
                "seen": int(self._wall() * 1000)}

    def _post_stop(self, who):
        try:
            self._poster("/api/remote/purge-stop", self._body(who))
        except Exception:
            pass

    def _loop_alive(self):
        f = self.alive
        if f is None:
            return True
        try:
            return bool(f())
        except Exception:                        # noqa: BLE001
            return False

    def _run(self, press, prev=None):
        if prev is not None and prev is not threading.current_thread():
            # The press before this one finishes (its last POST and its
            # purge-stop) before this press's first heartbeat.
            prev.join(timeout=2.0)
        try:
            self._beat_loop(press)
        finally:
            with self._lock:
                press.exited = True
                who_stop, press.stop_who = press.stop_who, None
            if who_stop is not None:
                self._post_stop(who_stop)

    def _end_press(self, press):
        """This press ends itself (loop not alive, engine refusal): True
        if it was still the firing one."""
        with self._lock:
            if self._press is press and self.firing:
                self.firing = False
                press.stop.set()
                return True
            return False

    def _beat_loop(self, press):
        who = press.who
        while not press.stop.is_set():
            # The press itself is the input loop's word that it ran at
            # that moment (review of PR #83, P3-2: the pacer has no beat
            # before the loop's first pass ends), so it counts as a beat.
            fresh = time.monotonic() - press.started <= PURGE_INPUT_STALE_S
            if not fresh and not self._loop_alive():
                # The loop that reads the FIRE key has stopped: nothing
                # can read its release, so the press is over. A fresh
                # press is needed once the loop runs again.
                if self._end_press(press):
                    self._journal(
                        f"Stream Deck FIRE released: the deck's key loop "
                        f"has not run for {PURGE_INPUT_STALE_S:g} s, so the "
                        f"FIRE key's release could not be read.", fault=True,
                        action="purge", who=who, screen="Stream Deck")
                    with self._lock:
                        press.stop_who = who
                return
            body = self._body(who)
            # The JSON word true asks the engine to fire; any other body
            # (purge-stop's included) can only stop a press there.
            body["on"] = True
            if press.fire_id is not None:
                body["fire_id"] = press.fire_id
            try:
                got = self._poster("/api/remote/purge-fire", body)
                ok, text = got[0], got[1]
                doc = got[2] if len(got) > 2 and isinstance(got[2], dict) \
                    else {}
            except Exception as e:
                ok, text, doc = False, f"{type(e).__name__}: {e}", {}
            if not ok:
                if press.stop.is_set():
                    return
                # Any refusal ends the press: the operator lets go and
                # presses again. Said once per press.
                self.last_error = text
                self._journal(f"Stream Deck FIRE: the engine refused the "
                              f"purge fire: {text}", fault=True,
                              action="purge", who=who, screen="Stream Deck")
                # And purge-stop goes on the way out (_run): a refusal of
                # any kind, a sign-in one included, still stops a purge
                # (re-review of #172, B2: a timed purge kept firing).
                with self._lock:
                    if press.stop_who is None:
                        press.stop_who = who
                self._end_press(press)
                return
            self.beats += 1
            if press.fire_id is None:
                press.fire_id = doc.get("fire_id")
                with self._lock:
                    if self._press is press:
                        self.fire_id = press.fire_id
                self._journal(f"Stream Deck FIRE held: purge firing "
                              f"({', '.join(doc.get('firing') or []) or 'nothing armed yet'}).",
                              action="purge", who=who, screen="Stream Deck")
            press.stop.wait(PURGE_BEAT_S)


class _FirePress:
    """One FIRE press's own state (PurgeFire): its stop signal, and the
    purge-stop its thread still owes on the way out."""

    def __init__(self, who):
        self.who = who or ""
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.fire_id = None
        self.exited = False
        self.stop_who = None


class DeckJournal:
    """Sends every arm, disarm, Abort and refusal line to ltc serve's own
    real, persistent night journal (schedule_service.py's Logbook, over
    POST /api/schedule/deck-event), as well as printing it locally (safety
    review of PR #31, item 9: before this, the deck's own journal lines
    went to this process's console ONLY, never into the same record as
    every other operator action).

    The HTTP POST happens on a BACKGROUND thread with its own queue, never
    on the main loop -- the same principle as item 5's operator lookup: a
    hung or slow web server must not be able to delay a key read or an
    arm-frame send. A line that cannot be delivered (the server is not
    running --schedule, is down, returns a non-2xx, or anything else) is
    still printed locally and counted in `dropped`; nothing here ever
    raises into the caller.

    Item 7 (round 2 of the safety review): `dropped` used to exist in name
    only -- it was incremented on a queue-full drop, but a POST that
    actually failed (unreachable, timed out, or a non-2xx reply such as a
    404 because the server was not started with --schedule) silently
    vanished, counted nowhere and logged nowhere beyond this process's own
    console, which nobody watches once the deck is wired into a real show.
    Every failed POST now increments `dropped`, and one clear FAULT line is
    printed per OUTAGE (the first failure after a success, or after
    startup), not one per event -- matching the pattern ArmSocket already
    uses for a persistent send failure (added round 1) -- with a matching
    recovery line once posting works again."""

    def __init__(self, base_url, poster=None, echo=print):
        self.base_url = base_url.rstrip("/")
        self._poster = poster or self._post
        self._echo = echo
        self.dropped = 0
        self._post_failing = False   # item 7: log once per OUTAGE
        self._q = queue.Queue(maxsize=JOURNAL_QUEUE_MAX)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-deck-journal")
        self._thread.start()

    def __call__(self, text, **kw):
        """The journal callable Controller._log() calls: prints at once,
        queues the HTTP POST for the background thread."""
        try:
            self._echo(("FAULT " if kw.get("fault") else "") + text)
        except Exception:
            pass
        try:
            self._q.put_nowait((text, kw))
        except queue.Full:
            self.dropped += 1

    def _loop(self):
        while True:
            text, kw = self._q.get()
            try:
                ok = self._poster(text, kw)
            except Exception:
                ok = False
            if ok:
                if self._post_failing:
                    self._post_failing = False
                    self._echo_safe(
                        "Stream Deck: the night journal (ltc serve) is "
                        "reachable again; deck-event posts are landing "
                        "there once more.")
            else:
                self.dropped += 1
                if not self._post_failing:
                    self._post_failing = True
                    self._echo_safe(
                        "FAULT Stream Deck: deck-event posts to the night "
                        "journal (ltc serve) are failing (unreachable, "
                        "timed out, or a non-2xx reply -- e.g. the server "
                        "was not started with --schedule). Every line is "
                        "still printed to this console; this line will "
                        "not repeat until posting recovers.")

    def _echo_safe(self, text):
        try:
            self._echo(text)
        except Exception:
            pass

    def _post(self, text, kw):
        """One blocking POST. Returns True on a 2xx reply, False on
        anything else (unreachable, timed out, a non-2xx status) -- never
        raises; the caller (_loop) turns a False into the dropped count and
        the once-per-outage FAULT line (item 7)."""
        body = json.dumps({
            "text": text, "fault": bool(kw.get("fault")),
            "action": kw.get("action") or "", "who": kw.get("who") or "",
            "screen": kw.get("screen") or "Stream Deck",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/schedule/deck-event", data=body,
            method="POST", headers={"Content-Type": "application/json"})
        try:
            _opener().open(req, timeout=JOURNAL_POST_TIMEOUT_S).read()
            return True
        except (OSError, urllib.error.URLError):
            # Not running --schedule, not reachable, timed out, or a
            # non-2xx reply (urllib raises HTTPError, a URLError subclass,
            # for those): the local print above already has the line, so
            # the night is not blind, only without the SAME record every
            # other action lands in. _loop counts and logs this.
            return False


# --------------------------------------------------------------------------
# Drawing: geometry and helpers ported from tools/streamdeck_design_demo.py
# (the approved look), kept as close to it as a second file reasonably can.
# Duplicated rather than imported on purpose: the demo stays a frozen,
# standalone bench tool (its own docstring: "connected to nothing else"),
# and this file must import neither it nor flamesafe.
# --------------------------------------------------------------------------
def _spaced_width(d, text, f, sp):
    return sum(d.textlength(c, font=f) for c in text) + sp * max(0, len(text) - 1)


class Fonts:
    """Lazily built, once Pillow is known to be importable."""

    def __init__(self):
        Image, ImageDraw, ImageFont = _import_pil()
        self.ImageDraw = ImageDraw
        serif = _first_font([
            "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
            r"C:\Windows\Fonts\georgiab.ttf",
        ]) or _first_font(["/System/Library/Fonts/Helvetica.ttc"])
        self._serif_path = serif
        self._cache = {}
        self._fit = {}
        self.ImageFont = ImageFont
        self.Image = Image

    def _sans_bold(self, size):
        for path in ("/System/Library/Fonts/Avenir Next Condensed.ttc",
                     "/System/Library/Fonts/HelveticaNeue.ttc"):
            if not os.path.exists(path):
                continue
            for idx in range(12):
                try:
                    f = self.ImageFont.truetype(path, size, index=idx)
                except Exception:
                    break
                name = " ".join(f.getname()).lower()
                if "bold" in name and "italic" not in name and "ultra" not in name:
                    return f
        # Windows has no Avenir Next Condensed: the closest condensed bold
        # it has. Bahnschrift's Bold Condensed instance first; a Pillow or
        # FreeType that cannot pick a variable font's instance would draw
        # Bahnschrift Regular, which is neither, so it then falls through
        # to Arial Narrow Bold.
        path = r"C:\Windows\Fonts\bahnschrift.ttf"
        if os.path.exists(path):
            try:
                f = self.ImageFont.truetype(path, size)
                f.set_variation_by_name("Bold Condensed")
                return f
            except Exception:
                pass
        for path in (r"C:\Windows\Fonts\arialnb.ttf",
                     r"C:\Windows\Fonts\arialbd.ttf"):
            if os.path.exists(path):
                return self.ImageFont.truetype(path, size)
        return self.ImageFont.truetype(self._serif_path, size)

    def get(self, kind, size):
        if (kind, size) not in self._cache:
            self._cache[(kind, size)] = (
                self.ImageFont.truetype(self._serif_path, size)
                if kind == "serif" else self._sans_bold(size))
        return self._cache[(kind, size)]

    def text_block(self, d, box, lines, kind, fill, max_size, sp_ratio=0.06):
        """One or two lines, as large as fits, centred on the letters' own
        ink (cap height and actual left/right edges), not on the font's
        line box. The size found for a face is remembered: the deck redraws
        the same words many times a second."""
        x0, y0, x1, y1 = box
        fit = (kind, tuple(lines), x1 - x0, y1 - y0, max_size, sp_ratio)
        size = self._fit.get(fit)
        if size is None:
            size = max_size
            while size > 8:
                f = self.get(kind, size)
                sp = size * sp_ratio
                cap = f.getbbox("H")
                cap_h = cap[3] - cap[1]
                pitch = cap_h * 1.4
                total_h = cap_h + pitch * (len(lines) - 1)
                if all(_spaced_width(d, ln, f, sp) <= (x1 - x0) - 2
                       for ln in lines) and total_h <= (y1 - y0) - 4:
                    break
                size -= 1
            self._fit[fit] = size
        f = self.get(kind, size)
        sp = size * sp_ratio
        cap = f.getbbox("H")
        cap_h = cap[3] - cap[1]
        pitch = cap_h * 1.4
        total_h = cap_h + pitch * (len(lines) - 1)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for i, ln in enumerate(lines):
            w = _spaced_width(d, ln, f, sp)
            lead = f.getbbox(ln[0])[0]
            trail = d.textlength(ln[-1], font=f) - f.getbbox(ln[-1])[2]
            ink_w = w - lead - trail
            x = cx - ink_w / 2 - lead
            cap_top = cy - total_h / 2 + pitch * i
            y = cap_top - cap[1]
            for c in ln:
                d.text((x, y), c, font=f, fill=fill)
                x += d.textlength(c, font=f) + sp

    def show_key(self, d, box, lines, text, bg=None, kind="serif", max_size=24):
        if bg:
            d.rounded_rectangle(box, radius=5, fill=bg)
        self.text_block(d, (box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2),
                        lines, kind, text, max_size)


def key_origin(k):
    return (k % COLS) * K, (k // COLS) * K


# The face boxes sit inside each key's ring of dots (look A's 12 px margin).
FACE_MARGIN = 12


def face_box(k, margin=FACE_MARGIN):
    ox, oy = key_origin(k)
    return (ox + margin, oy + margin, ox + K - margin, oy + K - margin)


# The approved look ("look a" in the demo; Jeff, 2026-09-27: "I prefer A.
# Give it two opposing marquee snakes", then "Your two marquees should be
# chasing the same direction, just on opposite ends of each other"): every
# key has its own ring of marquee dots and no solid outline. Two snakes run
# the same way round the outside dots of the whole deck, half a lap apart,
# each RUN_FRACTION of the lap long, a champagne head on a gold body.
DOT_INSET, DOT_SPACING, DOT_R = 6, 11.0, 2.6
RUN_FRACTION = 0.16     # share of the outside dots lit by each snake
# The deck's motion is a liveness indicator (Jeff, 2026-10-05; the demo's
# "freeze": when the safety program stops, nothing moves). It never runs on
# the deck's own clock:
# - the snakes step once per CHASE_STEP_S of flamesafe's OWN ticks, read
#   off its status frames (`heartbeat` x `tick_ms`, CONTRACT.md: "The
#   corner flame and the deck marquee step on this"). At the example 40 Hz
#   (25 ms ticks) that is one step per 2.4 ticks, the demo's one dot per
#   0.06 s. A re-read of the same frame, or no frame, is no step.
# - a key flamesafe's status says to flash (CYCLE ARM, ABORTED, the spoof
#   alarm) changes phase every BLINK_HALF_S of flamesafe's ticks;
# - a top-row key that flashes (RESUME, RESET, ENGINE FAULT) changes phase
#   every ENGINE_ANSWERS_PER_BLINK fresh answers from the engine's
#   /api/conductor (polled at POLL_HZ, 4 Hz: a phase every 0.5 s, the
#   demo's 2 Hz blink). No conductor wired: flamesafe's ticks instead.
CHASE_STEP_S = 0.06
BLINK_HALF_S = 0.5
ENGINE_ANSWERS_PER_BLINK = 2
DEFAULT_TICK_MS = 25.0  # CONTRACT.md's example tick_hz 40, if tick_ms is absent


def key_ring(k):
    """Key k's dots, clockwise from its top-left corner."""
    ox, oy = key_origin(k)
    x0, y0 = ox + DOT_INSET, oy + DOT_INSET
    x1, y1 = ox + K - DOT_INSET, oy + K - DOT_INSET
    side = x1 - x0
    n = max(1, round(side / DOT_SPACING))
    pts = [(x0 + side * i / n, y0) for i in range(n)]
    pts += [(x1, y0 + side * i / n) for i in range(n)]
    pts += [(x1 - side * i / n, y1) for i in range(n)]
    pts += [(x0, y1 - side * i / n) for i in range(n)]
    return pts


def _outer_dots():
    """The dots on the outside of the whole deck, clockwise from its
    top-left corner."""
    dots = set()
    for k in range(COLS * ROWS):
        for (x, y) in key_ring(k):
            if (abs(y - DOT_INSET) < 0.5 or abs(y - (H - DOT_INSET)) < 0.5
                    or abs(x - DOT_INSET) < 0.5
                    or abs(x - (W - DOT_INSET)) < 0.5):
                dots.add((x, y))

    def along(p):
        x, y = p
        if abs(y - DOT_INSET) < 0.5 and x < W - DOT_INSET - 0.5:
            return x
        if abs(x - (W - DOT_INSET)) < 0.5 and y < H - DOT_INSET - 0.5:
            return W + y
        if abs(y - (H - DOT_INSET)) < 0.5 and x > DOT_INSET + 0.5:
            return W + H + (W - x)
        return 2 * W + H + (H - y)
    return sorted(dots, key=along)


OUTER_DOTS = _outer_dots()
ALL_DOTS = sorted({p for k in range(COLS * ROWS) for p in key_ring(k)})


def marquee_colours(chase, abort_frac, arm_fills=None):
    """{dot: colour} for every dot on the deck. Purely decorative; no
    safety meaning (the holds' own timing, AbortHold.fraction, is what is
    tested as behaviour).

    Abort held (abort_frac > 0) or latched (1.0): every key's own ring
    fills red at once, each going round clockwise, and nothing chases
    (Jeff: "have the marquee fill fully in red, all dots, not the edges").
    Otherwise the two snakes, and `arm_fills` ({key: fraction}) fills that
    one key's ring in gold, clockwise, while its arm-hold runs; the snakes
    do not cross a ring that is filling."""
    lit = {}
    if abort_frac > 0:
        for k in range(COLS * ROWS):
            ring = key_ring(k)
            for p in ring[:round(abort_frac * len(ring))]:
                lit[p] = RED
    else:
        n = len(OUTER_DOTS)
        run = max(1, round(n * RUN_FRACTION))
        for head in (chase, chase + n // 2):
            for j in range(run):
                lit[OUTER_DOTS[(head - j) % n]] = CHAMPAGNE if j == 0 else GOLD
        for k, frac in (arm_fills or {}).items():
            frac = max(0.0, min(1.0, frac))
            ring = key_ring(k)
            for p in ring:
                lit.pop(p, None)
            for p in ring[:round(frac * len(ring))]:
                lit[p] = GOLD
    return {p: lit.get(p, BULB_OFF) for p in ALL_DOTS}


def draw_marquee(d, chase, abort_frac, arm_fills=None):
    for (x, y), c in marquee_colours(chase, abort_frac, arm_fills).items():
        d.ellipse((x - DOT_R, y - DOT_R, x + DOT_R, y + DOT_R), fill=c)


def _off_phase(bg):
    """(bg, text) for a flashing key's dark half: amber goes to dark amber
    with amber letters (the demo's CYCLE ARM); anything else to dim grey."""
    if bg == AMBER:
        return AMBER_DIM, AMBER
    return (26, 24, 21), DIM_TEXT


def _body_size(lines):
    """The largest size a bottom-row key's state may use, as the demo
    sizes them: a countdown digit 36, ARMED 22, two lines 20, else 24."""
    if len(lines) > 1:
        return 20
    if lines[0].isdigit():
        return 36
    if lines[0] == "ARMED":
        return 22
    return 24


def arm_key_image(fonts, d, box, name, look, blink_on):
    """One bottom-row key: the real group name on top, the real status
    below it (group_look's 5-tuple, or a caller's own, the ALARM look for
    instance), flashing when told to. Nothing else is ever drawn on a
    group key: the amber corner mark an unconfirmed config used to add
    here is gone (Jeff, 2026-10-19; see group_look). A look on
    LATCHED_GREY is a key greyed by the Abort latch: its name greys too."""
    line1, line2, bg, text, flashing = look[:5]
    x0, y0, x1, y1 = box
    bar = y0 + 17
    fonts.text_block(d, (x0, y0 - 1, x1, bar), [_fit_name(name)], "sans",
                     DIM_TEXT if bg == LATCHED_GREY else CHAMPAGNE, 16)
    body = (x0, bar + 1, x1, y1)
    lines = [line1] + ([line2] if line2 else [])
    if flashing and not blink_on:
        bg, text = _off_phase(bg)
    if bg == BRONZE:
        d.rounded_rectangle(body, radius=4, fill=bg, outline=BRONZE_EDGE,
                            width=1)
    else:
        d.rounded_rectangle(body, radius=4, fill=bg)
    fonts.text_block(d, body, lines, "sans", text, _body_size(lines))


def _fit_name(name):
    """A real flamesafe group name, shortened to fit a ~56px-wide label.
    Never silently truncates to nothing: a name this short still reads,
    just not in full -- the full name is always in the journal and the
    status frame, never only on the key."""
    name = name.upper()
    return name if len(name) <= LABEL_MAX else name[:LABEL_MAX - 1] + "…"


def draw_group_hold(fonts, d, box, name, frac):
    """One bottom-row key's face while its arm-hold is in progress but has
    not fired yet (item 8, Jeff, 2026-10-01). The progress itself is the
    key's own ring of dots filling in gold (marquee_colours' arm_fills),
    the same shape as Abort's red fill, so holding to arm reads the way
    holding to Abort does; the face says HOLD. Purely decorative; the
    hold's own timing (AbortHold.fraction) is what is actually tested."""
    x0, y0, x1, y1 = box
    bar = y0 + 17
    fonts.text_block(d, (x0, y0 - 1, x1, bar), [_fit_name(name)], "sans",
                     CHAMPAGNE, 16)
    body = (x0, bar + 1, x1, y1)
    d.rounded_rectangle(body, radius=4, fill=(26, 24, 21))
    fonts.text_block(d, body, ["HOLD"], "sans", CHAMPAGNE, 16)


def to_native(Image, img):
    img = img.rotate(90).transpose(Image.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        img.save(buf, "BMP")
        return buf.getvalue()


class Deck:
    """The physical Stream Deck Mini. Any read or write failure is raised
    as DeckDisconnected: the caller must stop sending what the keys asked
    for the instant this happens and send every group OFF instead (module
    docstring, failure mode 1), so this class never swallows an I/O error
    to "keep going"."""

    def __init__(self):
        hid = _import_hid()
        # Round 5 of the safety review, item 2: EVERYTHING after the import
        # is one try. set_nonblocking and the two feature reports used to
        # sit outside it, so a plain OSError from hidapi mid-plug escaped
        # as itself, not DeckDisconnected, and ended run_forever (and with
        # it the all-OFF hold that keeps flamesafe's sender lock).
        h = None
        try:
            h = hid.device()
            h.open(VID, PID)
            h.set_nonblocking(1)
            h.send_feature_report([0x0B, 0x63] + [0] * 15)
            h.send_feature_report([0x05, 0x55, 0xAA, 0xD1, 0x01, 80]
                                  + [0] * 11)
        except Exception as e:
            if h is not None:
                try:
                    h.close()     # a half-opened handle must not keep the
                except Exception:  # device busy for the next attempt
                    pass
            raise DeckDisconnected(
                f"could not open the Stream Deck: {e} "
                f"({describe_hid(hid)})") from e
        self.h = h
        self.last = {}

    def set_key(self, Image, key, img):
        """True once the key shows `img`, False when a write did not go
        through (review of PR #83, P2-2: hidapi's write returns -1, it does
        not raise, when Windows gives up on a report after 1 s). A key
        whose write did not go through is not remembered as written, so
        the same face is sent again; the caller does not count the frame
        as shown. A write that raises is an unplug, as before."""
        # The raw pixels are compared first, so an unchanged key costs no
        # rotate or BMP encode at all.
        raw = img.tobytes()
        if self.last.get(key) == raw:
            return True
        data = to_native(Image, img)
        self.last.pop(key, None)
        step = 1024 - 16
        page = sent = 0
        try:
            while sent < len(data):
                chunk = data[sent:sent + step]
                last = 1 if sent + len(chunk) >= len(data) else 0
                pkt = (bytes([0x02, 0x01, page, 0, last, key + 1] + [0] * 10)
                      + chunk)
                n = self.h.write(pkt + bytes(1024 - len(pkt)))
                if isinstance(n, int) and not isinstance(n, bool) and n <= 0:
                    return False
                sent += len(chunk)
                page += 1
        except Exception as e:
            raise DeckDisconnected(f"could not write a key image: {e}") from e
        self.last[key] = raw
        return True

    def keys_down(self):
        """EVERY 6-key snapshot the device has reported since the last
        call, oldest first, as a list (possibly empty). Before this fix
        (item 3, safety review of PR #31) this returned only the LAST
        snapshot, so a key pressed and released between two read cycles
        (the main loop is 50 ms or slower, worse under load) could leave
        no trace at all: down then up could both arrive before a single
        call here, and keeping only the last of them shows neither edge
        ever happening. Returning the whole run in order means the caller
        (Controller.run_once, called once per element) sees every down and
        every up exactly once, in the order the hardware reported them,
        whatever the main loop's own pace is. Never None once open: a USB
        read failure is a disconnect, raised, not a silent "nothing new"."""
        out = []
        try:
            while True:
                r = self.h.read(7)
                if not r:
                    return out
                out.append([bool(v) for v in r[1:7]])
        except Exception as e:
            raise DeckDisconnected(f"could not read the keys: {e}") from e

    def close(self):
        try:
            Image, _ImageDraw, _ImageFont = _import_pil()
            black = Image.new("RGB", (K, K), (0, 0, 0))
            self.last.clear()
            for k in range(6):
                self.set_key(Image, k, black)
        except Exception:
            pass
        try:
            self.h.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# The controller: wires a Deck, an ArmSocket, a StatusSocket and (optionally)
# a conductor and a LocalSchedule together. Every collaborator is injected,
# so the loop itself (run_once) is unit-testable with fakes; only main()
# constructs the real hardware and real sockets.
# --------------------------------------------------------------------------
class Controller:
    """One Stream Deck. `group_names`: flamesafe's group names, in its own
    config order (the arm frame's order). `key_order`: group indices in
    key order (deck_layout; None: flamesafe's order); the first
    GROUP_KEY_LIMIT get keys 3, 4 and 5 and any after them are screen-only
    (module docstring, failure mode 5). `labels`: what each group's key
    says, per group index (None: the group names).

    `conductor` is optional and duck-typed: .hold(who, screen),
    .resume(who, screen), .abort(who, screen), .snapshot() (for `latched`).
    None (the default) means no show control is connected; see module
    docstring for exactly what still works without one.

    `operator_provider` () -> current operator name or "". Defaults to
    always "" (operator gate never opens) if not given, which is the safe
    default: a deck that cannot ask who is operating must never ACT as if
    someone had been chosen.

    `show_running_provider` () -> True/False/None: whether a scheduled
    show is live, for the Start/Stop key (start_key_mode). None
    (unreachable) never shows STOP RUN.

    `held_provider` () -> True/False/None: whether the scheduler itself is
    on hold or paused (LocalSchedule.held), for the Start key's PAUSED
    face (_held_hint)."""

    def __init__(self, arm_socket, status_socket, group_names, deck_factory=Deck,
                operator_provider=lambda: "", show_running_provider=lambda: None,
                conductor=None, journal=None, clock=time.monotonic,
                sleep=time.sleep, screen=None, user_provider=None,
                audit=None, held_provider=None, key_order=None,
                labels=None, run_live_provider=None):
        if not group_names:
            raise ValueError("the Stream Deck was given no flamesafe group "
                             "names, so it has nothing to arm.")
        self.arm = arm_socket
        self.status = status_socket
        self.names = list(group_names)
        n = len(self.names)
        self.key_order = list(range(n)) if key_order is None else \
            list(key_order)
        if sorted(self.key_order) != list(range(n)):
            raise ValueError(f"key_order {self.key_order!r} is not every one "
                             f"of the {n} groups once.")
        self.labels = list(group_names) if labels is None else \
            [str(x) for x in labels]
        if len(self.labels) != n:
            raise ValueError("one label per flamesafe group, please.")
        # Key -> group index, and back, for the groups that have a key.
        self._key_group = {GROUP_KEYS[j]: g for j, g in
                           enumerate(self.key_order[:len(GROUP_KEYS)])}
        self._group_key = {g: k for k, g in self._key_group.items()}
        self.deck_factory = deck_factory
        self.operator_provider = operator_provider
        self.show_running_provider = show_running_provider
        self.conductor = conductor
        self._journal = journal or (lambda text, **kw: None)
        self._clock = clock
        self._sleep = sleep
        # The Start/Stop key's Stop is a HOLD of STOP_HOLD_S (Jeff,
        # 2026-10-26: the Rack screen asks for a confirm, and a stray tap
        # must never end a run); letting go early sends nothing.
        self._stop_hold = AbortHold(hold_s=STOP_HOLD_S)
        # Page 1's key 5 held (announcements and hazers both set up): the
        # hazers page once it completes; let go early is the tap (page 2).
        self._key5_hold = AbortHold(hold_s=HAZER_HOLD_S)
        # The latch as tick() last saw it (an Abort or Reset from a screen
        # starts every group's re-arm refractory window).
        self._was_latched = False
        # The frames draw() composes, numbered, and the newest one the deck
        # has been fully written (frame_shown; DisplayWorker, run_forever's
        # threaded mode). Display bookkeeping only since the deck's Hold and
        # Abort keys went (2026-10-26): no press depends on a shown face.
        self.display_confirms = False
        self.frame_id = 0
        self.last_shown = None
        # The deck's USB write timing (UsbMeter), for the rack screen and
        # the journal; given by run_forever. Never read by arming.
        self.usb = None
        # Whether the USB deck is here right now: True from each connect
        # until the unplug, False before the first and while it is away
        # (run_forever). Carried to the engine for the rack screen's Deck
        # chip (ScreenKeys, hw=). Display only: never read by arming.
        self.deck_present = False
        self.held_provider = held_provider or (lambda: None)
        # () -> True while Run has been pressed and playback is going
        # (LocalSchedule.run_live): the Start key is then Stop (Jeff,
        # 2026-10-26). None: not known, the key stays Start.
        self.run_live_provider = run_live_provider or (lambda: None)
        self._latched = False      # fallback when no conductor is wired
        self._prev_keys = [False] * 6
        # Page 2, the announcements (module docstring): 1 or 2, when page 2
        # last saw a key pressed, and the keys that were down at the last
        # page change, which do nothing until they are let go.
        self.page = 1
        self._page2_at = None
        self._masked = set()
        self._ann_pressed = None     # the id page 2 last asked to play
        self._ann_fresh_at = None    # clock of the last fresh engine answer
        # The arm guard after an automatic return (PAGE1_GUARD_S): the
        # clock of the return while it stands, else None; it ends
        # PAGE1_GUARD_S after page 1 was written to the keys
        # (_page1_written_at). It never holds back a disarm.
        self._guard = None
        self._guard_frame = None     # first page 1 frame after the return
        self._guard_frame_at = None
        # Item 8 (Jeff, 2026-10-01): one hold-to-arm timer and one
        # disarm-refractory clock per group key.
        self._arm_holds = [AbortHold(hold_s=ARM_HOLD_S)
                          for _ in self.names]
        self._disarmed_at = [None] * len(self.names)
        # Key 0 went down as the Morning page's FIRE and has not come up
        # (review of #132, 1): the press turns into a Stop hold the moment
        # Stop wins, whether or not PurgeFire is still firing (the engine
        # refuses beats the moment a show is live, which ends the press on
        # its own before this deck's poll sees the show), and whether or
        # not the page closed in the same pass.
        self._key0_fire = False
        # The groups purge_pass itself disarmed on a deselect, and when:
        # the only disarms the settle window (DISARM_SETTLE_S) forgives,
        # and only while key 0 is down as FIRE (delta review of #132, A).
        self._purge_deselected = {}
        # Screen arming (2026-10-03): the remote page's holds, as remote
        # presses of the group keys. _vholds: group -> the engine's hold id
        # this deck has seen; _vpressed: the ones whose arm-hold timer it
        # actually started; _vwho: (operator, device); _vheld: the engine's
        # beat-evidenced held_s. See ScreenKeys and screen_pass.
        self.screen = screen
        self._vholds = {}
        self._vpressed = set()
        self._vwho = {}
        self._vheld = {}
        # The Morning and Shutdown page's purge (Jeff, 2026-10-17): the
        # groups THIS deck armed for it (disarmed again the moment the
        # engine stops listing them), the groups the engine listed last
        # pass (a group is armed only when it ENTERS the list, so a deck
        # tap that disarms it is never undone), and the FIRE key's press.
        self._purge_armed = set()
        self._purge_seen = set()
        self._purge_refused_said = set()
        # Groups whose purge arm this deck refused because the re-arm
        # refractory window was still running: group index -> code, while
        # the group stays in the engine's purge list. Reported to the
        # engine (ScreenKeys.report_rearm) so the Startup page says so
        # instead of "Arming..." forever (Jeff, 2026-10-26). Reporting
        # only: whether and when a group arms is decided exactly as before.
        self._purge_refused = {}
        self.purge_fire = None        # PurgeFire, given by _main
        # Item 1's second line of defence: what THIS deck itself last sent,
        # so a status frame that disagrees can be caught even if a rogue
        # sender briefly won the arm link's own lock (arminput.py).
        self._spoof_alarm = ""
        self._spoof_category = ""  # round 3, item 5: the STABLE dedup key
        self._spoof_since = None
        # flamesafe's top-level `confirmed` as this deck last journaled it.
        # Starts True: a confirmed config is nothing to say; the first
        # fresh frame saying false earns one line. See _note_config_confirmed.
        self._confirmed_noted = True
        # None until the first check: a deck PROCESS that just started is
        # a reconnect too (round 4, item E). A restart quicker than
        # flamesafe's arm_stale_ms finds the old process's lock still
        # held, so for that long this new one is "another sender" and its
        # seq is behind; without this, that read as a spoof.
        self._spoof_last_seq = None
        self._spoof_grace_until = 0.0
        # Users (users.py, 2026-10-20): the deck acts as whoever is signed
        # in on this machine's rack screen. `user_provider` () -> the
        # engine's {"name", "role", "may"} or None (ScreenKeys.user);
        # `audit` an audit.AuditLog's event(), or None. Neither is ever
        # asked before an Abort or a disarm goes out, and neither can stop
        # one: both only ever add a refusal to ARMING, or a line to a log.
        if user_provider is None:
            user_provider = getattr(screen, "user", None) or (lambda: None)
        self.user_provider = user_provider
        self._audit = audit
        self._user_seen = ("?",)
        self._user_toast_until = 0.0
        # Module docstring 5: a group past the bottom row's keys is
        # screen-only. Said once at start, so nobody looks for its key.
        extra = [self.labels[g] for g in self.key_order[GROUP_KEY_LIMIT:]]
        if extra:
            keyed = [self.labels[g] for g in self.key_order[:GROUP_KEY_LIMIT]]
            self._log(f"Stream Deck: {', '.join(extra)} "
                      f"{'has' if len(extra) == 1 else 'have'} no key on the "
                      f"deck (it has {GROUP_KEY_LIMIT} arm keys, for "
                      f"{', '.join(keyed)}). Arm and disarm "
                      f"{'it' if len(extra) == 1 else 'them'} from the Rack "
                      f"screen (Arming from a screen must be on). Stop on "
                      f"the deck, or an Abort on any screen, disarms every "
                      f"group.", action="config",
                      outcome="screen-only")

    def _key_down(self, i):
        """Whether group `i`'s own deck key is down. Always False for a
        screen-only group (module docstring 5): it has no key."""
        if self.page != 1 or self._group_key.get(i) in self._masked:
            return False    # page 2 is up, or the key is masked till let go
        k = self._group_key.get(i)
        return k is not None and bool(self._prev_keys[k])

    def _user(self):
        try:
            u = self.user_provider()
        except Exception:
            return None
        return u if isinstance(u, dict) else None

    def _who(self):
        """Who a deck press is from. With users on at the engine (the
        deck-input "user" says active): whoever is signed in on this
        machine's rack screen, or UNSIGNED, never the chosen operator, so
        the journal names a real sign-in (Jeff's field report, 2026-10-28:
        "By Jeff" with nobody signed in) and operator_gate refuses an arm
        or a start for nobody. Without users: the chosen operator, as
        before."""
        u = self._user()
        if u is not None and (u.get("active") is True or u.get("stale")):
            # Signed in, or users on and who is signed in not known: never
            # the chosen operator's name, which nobody signed in to.
            return str(u.get("name") or "") or UNSIGNED
        try:
            return self.operator_provider() or ""
        except Exception:
            return ""

    def _audit_event(self, action, target="", result="accepted", reason=""):
        """One line in this process's audit file (audit.py). Never blocks
        and never raises: the event goes on a queue, or is counted lost."""
        a = self._audit
        if a is None:
            return
        try:
            u = self._user() or {}
            a(user=u.get("name") or "unsigned", surface="deck",
              action=action, target=target, result=result, reason=reason,
              source="Stream Deck")
        except Exception:
            pass

    def _audit_keys(self, down, latched, pending):
        """Every key that went down or came up since the last snapshot,
        holds and their releases included (the release of FIRE ends a
        purge, of Abort cancels it)."""
        if self._audit is None:
            return
        morning = self.morning_open()
        for k, what in ([(k, "key-press") for k in
                         edges(self._prev_keys, down)] +
                        [(k, "key-release") for k in
                         releases(self._prev_keys, down)]):
            self._audit_event(what, deck_key_name(
                k, [self.names[g] for g in
                    self.key_order[:len(GROUP_KEYS)]], latched=latched,
                morning=morning, ack=bool(pending), page=self.page))

    def _log(self, text, **kw):
        try:
            self._journal(text, **kw)
        except Exception:
            pass
        if self._audit is not None and kw.get("action"):
            low = text.lower()
            result = ("error" if kw.get("fault") else
                      "refused" if ("refused" in low or "nothing was" in low)
                      else "accepted")
            self._audit_event(str(kw.get("action")), target=text,
                              result=result,
                              reason=text if result != "accepted" else "")

    def reset_on_reconnect(self):
        """Called by run_forever right after a (re)connect, once arm.open()
        has already reset ArmSocket's own `wanted`/seq to a clean slate
        (module docstring, failure mode 2). Clears every hold-start
        timestamp and the last-seen key snapshot this controller keeps
        BETWEEN main-loop passes, so none of it can survive the gap and be
        checked against wall-clock time that passed while the deck was
        unplugged.

        Round 3 of the safety review, item 2: tick() runs unconditionally
        on every main-loop pass (round 2's own fix) and checks hold
        completion with `self._prev_keys[gk] and self._arm_holds[i].fired
        (now)` -- against state that this method did not used to clear.  A
        key held down when the deck unplugged, then physically released
        WHILE it was unplugged, left `_prev_keys` still showing it down and
        `_arm_holds[i]._down_at` still the original press time once the
        deck reconnected.  If the hardware reports a key's state only on
        change (nothing to report, since nothing has changed since before
        the gap), run_once() may see no snapshot at all on the first pass
        after reconnecting, so tick() is the only thing that runs -- and it
        found a hold that had long since "reached" its own duration purely
        because real time kept passing during the outage, firing an arm (or
        an Abort) the operator never actually held down. A reconnect must
        read as a clean start for key state too, the same as it already
        does for `wanted`: every group starts OFF, every key starts
        released, and the operator re-presses for real."""
        self._prev_keys = [False] * 6
        for h in self._arm_holds:
            h.release()
        self._stop_hold.release()
        self._vholds.clear()
        self._key5_hold.release()
        self.page, self._page2_at = 1, None
        self._masked = set()
        self._guard = None
        self._vpressed.clear()
        self._vwho.clear()
        self._vheld.clear()
        self._key0_fire = False
        self._fire_release("the deck reconnected")

    def animating(self):
        """True while something on the keys moves every pass: a key held
        down (a hold ring filling) or a screen hold in progress."""
        return any(self._prev_keys) or bool(getattr(self, "_vpressed", ()))

    def _latched_now(self):
        if self.conductor is not None:
            try:
                return bool(self.conductor.snapshot().get("latched"))
            except Exception as e:
                self._log(f"could not read the show conductor's state "
                         f"({type(e).__name__}: {e}); treating the rig as "
                         f"not latched.", fault=True)
                return False
        return self._latched

    def _disarm_all_groups(self, why):
        """Every flame group's wanted goes false on the arm link at once,
        and every group's re-arm refractory window starts: the arm link
        half of what the deck's own Abort key did (it left the deck on
        2026-10-26), now Stop's first step. The flame link's disarm_all is
        the engine's (/api/stop). Never sets the Abort latch: that is a
        screen's Abort's alone."""
        self.arm.set_all(False)
        self.arm.send(self.names)
        now = self._clock()
        self._disarmed_at = [now] * len(self.names)
        self._purge_armed.clear()
        self._log(f"Stream Deck: {why}: every flame group's wanted state "
                  f"was sent false at once. Nothing was latched.",
                  action="disarm", who=self._who(),
                  screen="Stream Deck")

    def alert_pending(self):
        """The engine's open alerts, as this deck last read them on its
        deck-input poll (ScreenKeys); () without one, or with no fresh
        answer. While any is open the Start key reads ACK and every key
        press acknowledges (Jeff's ruling 2, 2026-10-12)."""
        s = self.screen
        if s is None or not hasattr(s, "alerts"):
            return ()
        try:
            return tuple(s.alerts())
        except Exception:
            return ()

    def _do_acknowledge(self, pending):
        """Any deck key while an alert is open: Acknowledge, which clears
        the alert on the engine and changes nothing on the rig. The key
        still does its own job (a group key still disarms) after this. Never
        behind the operator gate: seeing the banner is not an action on
        the rig."""
        who = self._who()
        kinds = ", ".join(str(a.get("kind") or "?") for a in pending)
        ack = getattr(self.conductor, "acknowledge", None)
        if ack is None:
            self._log(f"Stream Deck key pressed while an alert is open "
                      f"({kinds}): no show conductor is connected in this "
                      f"build, so the alert could not be acknowledged from "
                      f"the deck.", fault=True, action="alert", who=who,
                      screen="Stream Deck")
            return
        r = ack(who=who, screen="Stream Deck")
        self._log(f"Stream Deck key pressed while an alert is open "
                  f"({kinds}): Acknowledge. {r.sentence}", fault=not r.ok,
                  action="alert", who=who, screen="Stream Deck")

    def start_key_mode(self):
        """What the Start/Stop key is right now (Jeff, 2026-10-26: Start
        and Stop are one key): "show" while a scheduled show is live (the
        key keeps saying what the show is doing, and a Stop hold is a
        complete halt: the show ends, Jeff's ruling of 2026-10-26),
        "stop" while Run has been pressed and playback is going AND the
        scheduler is known to have no show live (review of PR #120, P2-4:
        an unknown scheduler never shows STOP RUN), else as below. Stop
        holds in "show" and "stop" (STOP_MODES), and in "unknown": the
        scheduler cannot be read (the engine down, or not heard from yet;
        LocalSchedule reads the run and the show in one poll). Then the
        key reads STOP ALL and a hold is Stop: the arm link is disarmed
        first whatever the engine does (review of #130, 1)."""
        try:
            running = self.show_running_provider()
        except Exception:                        # noqa: BLE001
            running = None
        if running is True:
            return "show"
        if running is None:
            return "unknown"
        try:
            if self.run_live_provider() is True:
                return "stop"
        except Exception:                        # noqa: BLE001
            pass
        return "start"

    def _do_start_or_stop(self, now):
        mode = self.start_key_mode()
        if mode == "start" and not self._idle_stop():
            self._do_start_now()
        else:
            # Nothing running but a group armed: the hold is Stop too, a
            # complete halt (review of #130, second pass, 6).
            # A hold, never a tap (Jeff, 2026-10-26): tick() sends Stop
            # once the key has been held STOP_HOLD_S. During a scheduled
            # show too: Stop is a complete halt, and the show ends.
            self._stop_hold.press(now)

    def _any_flame_armed(self, purge=True):
        """True while this deck wants any group armed or flamesafe reports
        one armed; with purge=False, not counting the groups armed for the
        Morning page's purge. Never raises."""
        try:
            skip = set() if purge else self._purge_own()
            if not purge and self._key0_fire:
                # A purge machine deselected while FIRE is held: its group,
                # disarmed by purge_pass, that flamesafe's status still
                # reports armed is not a reason for Stop to win, for
                # DISARM_SETTLE_S (review of #132, 3). Only those: a Stop's
                # or a tap's own disarm that has not shown yet still counts
                # as armed (delta review of #132, A).
                now = self._clock()
                skip |= {i for i, at in self._purge_deselected.items()
                         if not self.arm.wanted[i] and
                         now - at < DISARM_SETTLE_S}
            return any(w for i, w in enumerate(self.arm.wanted)
                       if i not in skip) or any(
                self._reported_armed(i) for i in range(len(self.names))
                if i not in skip)
        except Exception:                        # noqa: BLE001
            return True

    def _purge_own(self):
        """The groups that are the Morning page's purge's own: the ones the
        purge armed (_purge_armed), and the ones its selection wants armed
        right now (purge_wanted), even if the operator had already armed
        one by hand so purge_pass never took it over (third review of
        #130, 2). Never raises."""
        try:
            return set(self._purge_armed) | set(self.purge_wanted())
        except Exception:                        # noqa: BLE001
            return set(self._purge_armed)

    def _stop_wins(self):
        """Key 0 is Start/Stop, never the Morning page's FIRE, whenever
        anything is running (Jeff's rule): a show live, a run going ("stop",
        third review of #130, 1) or the state not known; and while a group
        is armed other than for the purge (review of #130, second pass,
        2). An open Morning page never swallows a Stop."""
        return self.start_key_mode() in STOP_MODES or self._idle_stop()

    def _flames_live(self):
        """True while the engine says live flame programming or flame-
        following Programming playback is on (deck-input "flames_live"),
        False when it says
        neither, None when not known: a stale or missing answer, or one
        that raised (review of #132, 4). A deck with no screen wired at
        all (no engine) has no live flames: False. Never raises."""
        s = self.screen
        fn = getattr(s, "flames_live", None) if s is not None else None
        if fn is None:
            return False
        try:
            v = fn()
        except Exception:                        # noqa: BLE001
            return None
        return v if isinstance(v, bool) else None

    def _idle_stop(self):
        """With no show or run going, key 0 is still Stop (STOP ALL) while
        a group is armed other than for the purge, or live flame
        programming or flame-following Programming playback is on: those
        count as running
        (review of #130, low items, 3). The purge still works when nothing
        at all is running."""
        # Live flames not known count as on (review of #132, 4): Stop
        # wins, as with the scheduler unknown.
        return self._any_flame_armed(purge=False) or \
            self._flames_live() is not False

    def _do_stop(self):
        """Stop (Jeff, 2026-10-26): first every flame group disarmed on the
        arm link at once (_disarm_all_groups; never the Abort latch), then
        the Rack screen's Stop button, POST /api/stop: the engine ends live
        flame programming, sends the flame link's
        disarm_all (cues zeroed, a zero frame, the keyed disarm and its
        repeats; review of #126, P2-1), then stops playback and blacks the
        outputs out. The lasers: exactly what the Rack screen's Stop does
        with them, since it is the same route; the deck adds nothing of its
        own. Never behind the operator gate or a role: it only takes risk
        away ("safety" in users.WEB_ROUTES)."""
        self._disarm_all_groups("Stop held")
        who = self._who()
        fn = getattr(self.conductor, "stop_run", None)
        if fn is None:
            self._log("Stream Deck Stop pressed: no engine is connected in "
                      "this build. Nothing was stopped.", fault=True,
                      action="stop", who=who, screen="Stream Deck")
            return
        r = fn(who=who, screen="Stream Deck")
        self._log(f"Stream Deck Stop: {r.sentence}", fault=not r.ok,
                  action="stop", who=who, screen="Stream Deck")

    def _do_start_now(self):
        who = self._who()
        refusal = operator_gate(who, "start")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="start")
            return
        self._log("Stream Deck Start Now pressed: there is no real 'start "
                  "the show now' entry point wired up yet in this build "
                  "(the scheduler decides when a show starts; the "
                  "conductor only reconciles the rig once a cue has "
                  "already begun). Nothing was started.", fault=True,
                  action="start", who=who, screen="Stream Deck")

    def nokey(self):
        """The group indices with no key on the deck (past key 4 in key
        order), for the Rack screen's flame card. Display only."""
        return list(self.key_order[GROUP_KEY_LIMIT:])

    def rearm_report(self):
        """({group index: ms left in its re-arm refractory window},
        {group index: purge refusal code}): what the rack screen needs to
        say "Re-arm in 1.5 s" and why a purge click did not arm (Jeff,
        2026-10-26). Only groups with something to say. Reporting only."""
        now = self._clock()
        left = {}
        try:
            for i in range(len(self.names)):
                ms = int(self._in_rearm_refractory(i, now) * 1000)
                if ms > 0:
                    left[i] = ms
        except Exception:                        # noqa: BLE001
            left = {}
        try:
            # Read from the deck-input thread while the main loop may
            # change it: a copy that fails says nothing, the waits still go.
            refused = dict(self._purge_refused)
        except Exception:                        # noqa: BLE001
            refused = {}
        return left, refused

    def _in_rearm_refractory(self, i, now):
        at = self._disarmed_at[i]
        if at is None:
            return 0.0
        left = REARM_REFRACTORY_S - (now - at)
        return left if left > 0 else 0.0

    def _do_disarm(self, i, who=None, screen="Stream Deck"):
        """Disarm stays an instant single tap, no hold, gate or no gate
        (Jeff, 2026-10-01; items 5 and 7: disarm is NEVER behind the
        operator gate, and NEVER waits on anything -- flip `wanted` and
        send BEFORE looking up who, so even a slow operator_provider()
        cannot delay the disarm itself). `who` and `screen` are given for a
        disarm pressed on the remote page (screen_pass)."""
        self.arm.set_group(i, False)
        self.arm.send(self.names)
        self._disarmed_at[i] = self._clock()
        self._purge_armed.discard(i)
        if who is None:
            who = self._who()
        where = "" if screen == "Stream Deck" else f" on the {screen}"
        self._log(f"Stream Deck: {self.names[i]} disarm pressed{where} by "
                  f"{who or 'an operator the deck could not name'}.",
                  action="disarm", who=who, screen=screen)

    def screen_pass(self):
        """Once per MAIN-LOOP pass, before tick(): the remote page's
        per-group disarms (instant, like a tap) and its arm holds (remote
        presses of the group key; see ScreenKeys). A screen hold goes
        through _on_group_press's own rules (refractory, operator gate);
        it never disarms, and it never starts while the group is already
        wanted. Anything the engine stops reporting (let go, a dropped
        page, an Abort, an unreachable engine) releases the hold at once."""
        if self.screen is None:
            return
        now = self._clock()
        for ev in self.screen.new_disarms():
            i = ev.get("group")
            if not isinstance(i, int) or not 0 <= i < len(self.names):
                continue
            self._screen_release(i)
            if self.arm.wanted[i]:
                self._do_disarm(i, who=str(ev.get("who") or ""),
                                screen=str(ev.get("device") or "screen"))
        latched = self._latched_now()
        holds = {} if latched else self.screen.holds()
        if latched:
            # Taken by nothing while latched, as before; the engine is told
            # why so the page can say it (reporting only).
            try:
                for h in self.screen.holds().values():
                    self._report_refused(h.get("id"), "latched")
            except Exception:                    # noqa: BLE001
                pass
        for i in list(self._vholds):
            h = holds.get(i)
            if h is None or h.get("id") != self._vholds[i]:
                self._screen_release(i)
        for i, h in holds.items():
            if not 0 <= i < len(self.names):
                continue
            if i in self._vholds:
                self._vheld[i] = float(h.get("held_s") or 0.0)
                continue
            self._vholds[i] = h.get("id")
            self._vheld[i] = float(h.get("held_s") or 0.0)
            who = str(h.get("who") or "")
            device = str(h.get("device") or "screen")
            self._vwho[i] = (who, device)
            if self.arm.wanted[i] or self._key_down(i):
                if not self.arm.wanted[i]:
                    self._report_refused(h.get("id"), "key")
                continue            # already wanted, or the key itself is down
            left = self._in_rearm_refractory(i, now)
            if left > 0:
                self._log(f"Stream Deck: {self.names[i]} arm hold on the "
                          f"{device} by {who or 'someone'} refused, "
                          f"{left:.1f} s left in the re-arm refractory "
                          f"window after its last disarm.",
                          action="arm-refused", who=who, screen=device)
                self._report_refused(h.get("id"), "refractory")
                continue
            refusal = operator_gate(who, "arm")
            if refusal:
                self._log(f"Stream Deck: {refusal}", action="arm")
                self._report_refused(h.get("id"), "operator")
                continue
            self._arm_holds[i].press(now)
            self._vpressed.add(i)
        self.purge_pass(now)

    # -- the Morning and Shutdown page's purge (morning.py, MORNING.md) ------
    def morning_view(self):
        """The engine's fresh word on the Morning page, or None."""
        s = self.screen
        if s is None or not hasattr(s, "morning"):
            return None
        try:
            return s.morning()
        except Exception:
            return None

    def morning_open(self):
        """True while the engine says the Morning and Shutdown page is open
        (fresh within SCREEN_STALE_S): the deck is re-mapped meanwhile."""
        m = self.morning_view()
        return bool(m and m.get("open") is True)

    def purge_wanted(self):
        """The flamesafe group indices the engine's purge selection needs
        armed right now: only while the page is open, purge arming is on
        at the engine, the engine is fresh and the rig is not latched."""
        m = self.morning_view()
        if not m or m.get("open") is not True or \
                m.get("purge_enabled") is not True or self._latched_now():
            return set()
        p = m.get("purge") if isinstance(m.get("purge"), dict) else {}
        out = set()
        for i in p.get("wanted_groups") or []:
            if isinstance(i, int) and not isinstance(i, bool) and \
                    0 <= i < len(self.names):
                out.add(i)
        return out

    def purge_timed_left(self):
        """The seconds a timed purge has left on the engine's clock, while
        the engine says one runs (deck-input "morning"), else None.
        Display only."""
        m = self.morning_view()
        p = m.get("purge") if isinstance(m, dict) and \
            isinstance(m.get("purge"), dict) else {}
        v = p.get("timed_left_s")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
            return None
        return max(0.0, float(v))

    def purge_pass(self, now):
        """Once per main-loop pass. A group ENTERING the engine's purge
        list is armed (Jeff's exception to hold-to-arm: a click on the
        page), with the refractory window and the latch still checked; a
        group LEAVING it that this deck armed for the purge is disarmed at
        once, as is every one when the page closes, the engine goes quiet
        or the rig latches. A group the operator armed themselves is left
        alone, and a group they tap off is not re-armed (it is only armed
        when it enters the list)."""
        wanted = self.purge_wanted()
        entered = wanted - self._purge_seen
        self._purge_seen = wanted
        for i in sorted(entered):
            if self.arm.wanted[i]:
                continue             # already armed by the operator
            if self._never_purged(i):
                # A fireball group, or one the Morning page never purges:
                # never armed for a purge on the deck either, a second
                # barrier behind the page's own refusal (review of #126,
                # P2-4; the Fireball has a key since 2026-10-26).
                self._purge_refused[i] = "neverpurged"  # reporting only
                self._log(f"Stream Deck: {self.labels[i]} ({self.names[i]}) "
                          f"was not armed for a purge: it is never purged.",
                          action="arm-refused", screen="Morning page")
                continue
            if i not in self._group_key:
                # A screen-only group (module docstring 5) is armed by a
                # screen HOLD only, never by a purge click (review of PR
                # #120, P1): no key, so no click may stand in for one.
                self._purge_refused[i] = "screenonly"   # reporting only
                self._log(f"Stream Deck: {self.labels[i]} ({self.names[i]}) "
                          f"was not armed for a purge: it has no key on the "
                          f"deck, so it is armed only by holding its Hold to "
                          f"arm button on the Rack screen.",
                          action="arm-refused", screen="Morning page")
                continue
            # The same guards as a completed hold, in the same code
            # (_do_arm_fire, review of PR #68, P2-6): the latch and the
            # refractory window re-checked at the moment of arming, and the
            # operator gate (a purge needs a chosen operator on the deck,
            # like any arm). Only the hold itself is waived (Jeff).
            who = self._who()
            if self._in_rearm_refractory(i, now) > 0:
                self._purge_refused[i] = "refractory"   # reporting only
            else:
                self._purge_refused.pop(i, None)
            self._do_arm_fire(i, now, who=who, screen="Morning page")
            if not self.arm.wanted[i]:
                continue             # refused; _do_arm_fire said why
            self._purge_armed.add(i)
            self._log(f"Stream Deck: {self.names[i]} armed for a PURGE from "
                      f"the Morning and Shutdown page (a click, Jeff's "
                      f"exception to hold-to-arm). It is disarmed the "
                      f"moment the page stops asking for it.", action="arm",
                      who=who, screen="Morning page")
        for i in [i for i in self._purge_refused if i not in wanted]:
            del self._purge_refused[i]
        for i in sorted(self._purge_armed - wanted):
            self._purge_armed.discard(i)
            if self.arm.wanted[i]:
                self._do_disarm(i, who="the purge ended",
                                screen="Morning page")
                self._purge_deselected[i] = self._clock()
        if not self.morning_open():
            self._fire_release("the Morning page is not open")

    def _never_purged(self, i):
        """Whether group `i` is never armed for a purge: its name says
        fireball or ball (the Morning page's own rule), or the engine's
        Morning view lists it in purge.never_groups."""
        n = str(self.names[i]).lower()
        if "fireball" in n or "ball" in n:
            return True
        m = self.morning_view() or {}
        p = m.get("purge") if isinstance(m.get("purge"), dict) else {}
        never = p.get("never_groups") or []
        return isinstance(never, list) and i in never

    def _fire_press(self, now):
        """The FIRE key went down while the Morning page is open: the
        heartbeats start (PurgeFire) if anything is armed for the purge."""
        pf = self.purge_fire
        if pf is None:
            self._log("Stream Deck FIRE pressed: no purge sender is wired "
                      "in this build. Nothing was sent.", fault=True,
                      action="purge", screen="Stream Deck")
            return
        if not self.purge_wanted():
            self._log("Stream Deck FIRE pressed with nothing armed for a "
                      "purge on the Morning page; refused.", action="purge",
                      screen="Stream Deck")
            return
        pf.start(who=self._who())

    def _fire_release(self, why):
        pf = self.purge_fire
        if pf is not None and getattr(pf, "firing", False):
            pf.stop(who=self._who())
            self._log(f"Stream Deck FIRE released: {why}.", action="purge",
                      screen="Stream Deck")

    def _report_refused(self, hold_id, code):
        """Tell the engine (ScreenKeys.note_refused) that a screen hold was
        not taken, and why, so the page says it. Reporting only: never
        raises, never changes what was decided."""
        s = self.screen
        fn = getattr(s, "note_refused", None) if s is not None else None
        if fn is None or hold_id is None:
            return
        try:
            fn(hold_id, code)
        except Exception:                        # noqa: BLE001
            pass

    def _screen_release(self, i):
        self._vholds.pop(i, None)
        self._vwho.pop(i, None)
        self._vheld.pop(i, None)
        if i in self._vpressed:
            self._vpressed.discard(i)
            if not self._key_down(i):
                self._arm_holds[i].release()

    def _on_group_press(self, i, now):
        """One group key went down. Disarm fires at once; arming only
        starts a hold (item 8) -- see _arm_holds and run_once's own
        fired()-polling loop for where the hold actually completes."""
        if self.arm.wanted[i]:
            # Disarm is NEVER behind the operator gate (item 7, Jeff's
            # policy, blessed explicitly -- see operator_gate's own
            # docstring). Do not add a gate call here.
            self._do_disarm(i)
            return
        left = self._in_rearm_refractory(i, now)
        if left > 0:
            self._log(f"Stream Deck: {self.names[i]} arm press refused, "
                      f"{left:.1f} s left in the re-arm refractory window "
                      f"after its last disarm. Wait, then hold the key "
                      f"again.", action="arm-refused")
            return
        who = self._who()
        refusal = operator_gate(who, "arm")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="arm")
            return
        refusal = role_gate(self._user(), "arm")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="arm")
            return
        self._arm_holds[i].press(now)

    def _do_arm_fire(self, i, now, who=None, screen="Stream Deck"):
        """A group's arm-hold reached ARM_HOLD_S: send wanted=True, but
        only after two independent guards (item 3, round 2 of the safety
        review): a latched rig (post-Abort, pre-Reset) and the re-arm
        refractory window are both RE-CHECKED here, at fire time, not only
        at hold-START time.

        The reasoning that used to be here -- "the refractory window is
        not re-checked, since starting the hold already proved it was
        clear" -- was wrong: an Abort (or a disarm) can land at ANY point
        during an in-progress hold, including in the very same main-loop
        pass the hold completes in (see tick(), which returns immediately
        after _do_abort() for exactly this reason, so an Abort can never
        also let a same-pass arm-hold complete). Relying on that ordering
        alone, in one place, is a single point of failure; checking again
        here means a latched or refractory group can never be armed no
        matter how the two code paths interleave -- this is the SECOND,
        independent guard, not a substitute for the ordering fix. The
        operator gate is also re-checked (defensive: the operator could in
        principle have been cleared mid-hold).

        Returns None when it set the group, else the refusal's code
        ("latched", "refractory", "operator", "role"), which a screen
        hold reports back to the engine for the page (2026-10-22)."""
        if self._latched_now():
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "
                      f"but the rig is latched (an Abort landed during the "
                      f"hold); refused. Reset, then hold the key again.",
                      action="arm-refused")
            return "latched"
        left = self._in_rearm_refractory(i, now)
        if left > 0:
            self._log(f"Stream Deck: {self.names[i]} arm-hold completed "
                      f"but {left:.1f} s is still left in the re-arm "
                      f"refractory window (a disarm or Reset landed during "
                      f"the hold); refused.", action="arm-refused")
            return "refractory"
        if who is None:
            who = self._who()
        refusal = operator_gate(who, "arm")
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="arm")
            return "operator"
        # The deck's own key only: a screen's hold and a purge were checked
        # against the role matrix by the engine already.
        refusal = (role_gate(self._user(), "arm") if screen == "Stream Deck"
                   else None)
        if refusal:
            self._log(f"Stream Deck: {refusal}", action="arm")
            return "role"
        self.arm.set_group(i, True)
        self.arm.send(self.names)
        held = (f"held {ARM_HOLD_S:g} s" if screen == "Stream Deck" else
                "a click on the Morning page, for a purge" if
                screen == "Morning page" else
                f"held {SCREEN_HOLD_S:g} s on the {screen}")
        self._log(f"Stream Deck: {self.names[i]} arm pressed ({held}) by "
                  f"{who or 'an operator the deck could not name'}.",
                  action="arm", who=who, screen=screen)

    def run_once(self, down):
        """One pass given ONE of the deck's 6-key snapshots (item 3 of
        round 1: the caller passes every snapshot the hardware reported
        since the last call, in order, not just the latest one -- see
        Deck.keys_down()). Handles key TRANSITIONS only (presses and
        releases): starting a hold, cancelling one on an early release, an
        instant disarm, Start Now, Hold/Resume. Pure apart from the
        collaborators it was built with, so this is unit-testable with a
        fake deck snapshot.

        Hold-DURATION checks (did a hold reach its own time yet) are NOT
        here -- see tick() (item 2, round 2 of the safety review): they
        used to be at the end of this method, which only runs when
        keys_down() actually has a snapshot to hand it. If the real
        hardware reports a key's state only on CHANGE, nothing calls this
        method again between a press and a release, so a hold's own
        completion was never checked until the next unrelated key event or
        the release itself -- the hold could complete and sit there
        unnoticed, or never fire at all before release. tick() checks hold
        completion every MAIN-LOOP pass instead, unconditionally, against
        self._prev_keys (updated below) and wall-clock time, independently
        of whether this method ran this pass."""
        now = self._clock()
        latched = self._latched_now()
        # An open alert (alerts.py): the first press of ANY key
        # acknowledges it, and the key then does what it always does
        # below (a group key still disarms). The Start key, which reads
        # ACK meanwhile, does only the acknowledge.
        pending = self.alert_pending()
        self._audit_keys(down, latched, pending)
        if pending and edges(self._prev_keys, down):
            self._do_acknowledge(pending)
        self._masked -= set(releases(self._prev_keys, down))
        if self.page in (2, HAZER_PAGE):
            why = self._page2_must_close(latched, pending)
            if why:
                self._to_page1(why, down)
            elif self.page == HAZER_PAGE:
                self._page3_run(down, now)
                return
            else:
                self._page2_run(down, now)
                return
        for k in releases(self._prev_keys, down):
            if k == TOP_START:
                # The FIRE key (Morning page): letting go ends the purge
                # fire at once, whether or not the page is still open.
                self._key0_fire = False
                self._fire_release("the key came up")
                # Letting go of Stop before STOP_HOLD_S sends nothing.
                self._stop_hold.release()
            elif k in self._key_group:
                # Letting go before ARM_HOLD_S cancels the hold and sends
                # nothing.
                self._arm_holds[self._key_group[k]].release()
        if latched:
            # Aborted, from the Rack screen or any other screen (the deck
            # has no Abort key since 2026-10-26; Reset is the Rack
            # screen's): the deck's border is solid red and every key with
            # nothing to do is greyed.
            # - a group key disarms that group if this deck wants it or
            #   flamesafe still reports it armed: disarm is never behind
            #   any gate. Nothing can be armed.
            # - Stop stays reachable while a run is going, as on the Rack
            #   screen (Jeff, 2026-10-26): the same hold, and it never
            #   touches the latch.
            for k in edges(self._prev_keys, down):
                if k == TOP_START:
                    # An open alert is acknowledged above, and never
                    # swallows the Stop hold (review of #130, 9). Latched
                    # with nothing running but a group still reported armed
                    # or live flames on: Stop all the same (delta review of
                    # #132, D1).
                    if self.start_key_mode() in STOP_MODES or \
                            self._idle_stop():
                        self._stop_hold.press(now)
                elif k in self._key_group:
                    i = self._key_group[k]
                    if self.arm.wanted[i] or self._reported_armed(i):
                        self._do_disarm(i)
            for h in self._arm_holds:
                h.release()
            self._key5_hold.release()
            self._fire_release("the rig is aborted")
            self._prev_keys = list(down)
            return
        morning = self.morning_open()
        for k in edges(self._prev_keys, down):
            if k == TOP_START:
                if pending:
                    # The press acknowledged the alert above. While a show
                    # or a run is going, the state is not known, or a flame
                    # group is armed, it is ALSO the start of a Stop hold:
                    # an alert never swallows the first Stop (review of
                    # #130, 9). A tap only acknowledges; a full hold stops.
                    # The Morning page's FIRE keeps the key unless Stop
                    # wins (_stop_wins).
                    if (not morning or self._stop_wins()) and (
                            self.start_key_mode() in STOP_MODES or
                            self._idle_stop()):
                        self._stop_hold.press(now)
                    elif morning:
                        # An acknowledge on the Morning page that Stop does
                        # not win yet: if it starts to win during this same
                        # hold, the hold becomes Stop (delta review of #132,
                        # D2), as a held FIRE does.
                        self._key0_fire = True
                elif morning and not self._stop_wins():
                    # Re-mapped while the Morning page is open: FIRE.
                    self._key0_fire = True
                    self._fire_press(now)
                else:
                    # Start and Stop are one key (Jeff, 2026-10-26).
                    self._do_start_or_stop(now)
            elif k in self._key_group:
                if k in self._masked:
                    pass
                elif self._guard_on(now) and \
                        not self.arm.wanted[self._key_group[k]]:
                    # Only an ARM is held back; a tap on an armed group
                    # disarms it as always (a disarm is never gated).
                    self._log(f"Stream Deck: {self.labels[self._key_group[k]]}"
                              f" key pressed just after the announcements "
                              f"page closed by itself: not armed, so a "
                              f"press meant for an announcement never arms "
                              f"a group. Hold it again once the flame keys "
                              f"show.", action="arm-refused")
                else:
                    self._on_group_press(self._key_group[k], now)
            elif k == ANNOUNCE_KEY and not pending and \
                    (self.announce_view() or {}).get("configured"):
                # Only while the engine says announcements are running:
                # otherwise key 5 stays the blank key that does nothing.
                if self.hazers_view():
                    # Hazers too: a hold opens their page (tick), a tap
                    # (let go early) opens page 2, below.
                    self._key5_hold.press(now)
                else:
                    self._open_page2(down, now, latched, morning)
            elif k == ANNOUNCE_KEY and not pending and self.hazers_view():
                # No announcements, but hazers: key 5 opens their page.
                self._open_page2(down, now, latched, morning,
                                 page=HAZER_PAGE)
        if ANNOUNCE_KEY in releases(self._prev_keys, down) and \
                self._key5_hold._down_at is not None:
            # Let go before HAZER_HOLD_S: the tap, page 2, with page 2's
            # own checks at the release.
            self._key5_hold.release()
            if not pending:
                self._open_page2(down, now, latched, morning)
        self._prev_keys = list(down)

    # -- page 2, the announcements (module docstring) ------------------------
    def _page2_must_close(self, latched, pending, now=None):
        """Why page 2 has to give way to page 1 right now, or "". Every
        one of these needs the arm keys (or the Start key's own face)
        back in front of the operator."""
        if latched:
            return "an Abort is latched"
        if pending:
            return "an alert is open"
        if self._spoof_alarm:
            return "the arm link alarm is up"
        if self.morning_open():
            return "the Morning page opened"
        if now is None or self._page2_at is None:
            return ""
        if self.page == HAZER_PAGE:
            # The hazers page: idle is idle (nothing on it plays).
            if now - self._page2_at >= PAGE2_IDLE_S:
                return f"no key was pressed for {PAGE2_IDLE_S:g} s"
            return ""
        av = self.announce_view()
        if isinstance(av, dict) and not av.get("stale") and \
                av.get("ok") is not False:
            self._ann_fresh_at = now
        if now - self._page2_at < PAGE2_IDLE_S:
            return ""
        if self._fresh_and_quiet(av):
            return f"no key was pressed for {PAGE2_IDLE_S:g} s"
        seen = self._ann_fresh_at if self._ann_fresh_at is not None \
            else self._page2_at
        if now - max(seen, self._page2_at) >= PAGE2_STALE_CLOSE_S:
            return (f"the engine's announcement status has been "
                    f"unavailable for {PAGE2_STALE_CLOSE_S:g} s")
        return ""

    @staticmethod
    def _fresh_and_quiet(av):
        """True only on a FRESH engine answer that says nothing is playing
        (delta review of #129, 1): a stale or missing answer is "maybe
        playing", and page 2 (with STOP ANNC.) then stays."""
        return (isinstance(av, dict) and not av.get("stale") and
                av.get("ok") is not False and not av.get("playing"))

    def _open_page2(self, down, now, latched, morning, page=2):
        """Key 5 pressed on page 1. Opened only with nothing else going
        on: no latch, no alert, no Morning page, no spoof alarm, no other
        key down, no arm hold filling, no Stop hold filling. `page` is
        HAZER_PAGE for the hazers page (no announcements set up)."""
        why = ""
        if latched or morning or self._spoof_alarm:
            why = "the deck is busy with something more important"
        elif any(down[k] for k in range(len(down)) if k != ANNOUNCE_KEY):
            why = "another key is down"
        elif any(h.fraction(now) > 0 for h in self._arm_holds) or \
                self._stop_hold.fraction(now) > 0:
            why = "a hold is filling"
        what = "hazers" if page == HAZER_PAGE else "announcements"
        if why:
            self._log(f"Stream Deck: the {what} page was not "
                      f"opened: {why}.", action="page")
            return
        self.page, self._page2_at = page, now
        self._ann_fresh_at = None
        self._masked = {k for k in range(len(down)) if down[k]}
        self._log(f"Stream Deck: {what} page open. The arm keys are "
                  "back on BACK, or by themselves after "
                  f"{PAGE2_IDLE_S:g} s.", action="page")

    GUARDED_RETURNS = ("no key was pressed", "an alert is open",
                       "the Morning page opened",
                       "the engine's announcement status")

    def _page1_written_at(self):
        """When page 1 reached the keys after the automatic return, or
        None: with a display worker (display_confirms), the time it says
        it finished writing the first page 1 frame composed after the
        return; without one (the frame is written on the loop itself,
        right after it is composed), when it was composed."""
        f = self._guard_frame
        if f is None:
            return None
        if not self.display_confirms:
            return self._guard_frame_at
        shown = self.last_shown
        if shown and shown[0] >= f:
            return shown[1]
        return None

    def _guard_on(self, now=None):
        """True while a group key starts no ARM after page 2 gave way by
        itself: until PAGE1_GUARD_S after page 1 actually reached the keys
        (delta review of #129, 2). Never a disarm."""
        if self._guard is None:
            return False
        now = self._clock() if now is None else now
        at = self._page1_written_at()
        if at is None or now < at + PAGE1_GUARD_S:
            return True
        self._guard = None
        return False

    def _to_page1(self, why, down=None):
        if self.page != 1:
            self.page, self._page2_at = 1, None
            if why.startswith(self.GUARDED_RETURNS):
                now = self._clock()
                self._guard = now
                self._guard_frame = None
            cur = down if down is not None else self._prev_keys
            # A key down at the change does nothing until it is let go.
            self._masked = {k for k in range(len(cur)) if cur[k]}
            self._log(f"Stream Deck: back to the arm keys ({why}).",
                      action="page")

    def _page2_run(self, down, now):
        """run_once on page 2. Key 0 is the same START / STOP as page 1;
        keys 1 to 3 play, key 4 stops the sound, key 5 is BACK. No group
        key exists on this page: nothing here arms or disarms."""
        for k in releases(self._prev_keys, down):
            if k == TOP_START:
                self._fire_release("the key came up")
                self._stop_hold.release()
        for k in edges(self._prev_keys, down):
            if k in self._masked:
                continue
            self._page2_at = now
            if k == TOP_START:
                self._do_start_or_stop(now)
            elif k in PAGE2_PLAY:
                self._announce_press(PAGE2_PLAY[k])
            elif k == PAGE2_STOP:
                self._announce_stop()
            elif k == PAGE2_BACK:
                self._to_page1("BACK pressed", down)
        self._prev_keys = list(down)

    def _page3_run(self, down, now):
        """run_once on page 3, the hazers. Key 0 is START / STOP; keys 1
        to 3 toggle a hazer (a plain tap), key 4 is HAZERS OFF, key 5 BACK.
        No group key exists on this page: nothing here arms a flame."""
        for k in releases(self._prev_keys, down):
            if k == TOP_START:
                self._fire_release("the key came up")
                self._stop_hold.release()
        for k in edges(self._prev_keys, down):
            if k in self._masked:
                continue
            self._page2_at = now
            if k == TOP_START:
                self._do_start_or_stop(now)
            elif k in PAGE3_HAZER:
                self._hazer_press(PAGE3_HAZER[k])
            elif k == PAGE3_OFF:
                self._hazers_off_press()
            elif k == PAGE3_BACK:
                self._to_page1("BACK pressed", down)
        self._prev_keys = list(down)

    def hazers_view(self):
        """The engine's hazers for page 3 (ScreenKeys.hazers), or None."""
        s = self.screen
        fn = getattr(s, "hazers", None) if s is not None else None
        if fn is None:
            return None
        try:
            return fn()
        except Exception:
            return None

    def _hazer_press(self, i):
        """A hazer key: ARM if the engine says it is disarmed, else DISARM.
        Arming needs what arming a group needs at the deck (a chosen
        operator, the role matrix's arm row); a disarm is never gated. While the engine's word is
        not fresh the key reads NO STATUS and a press does nothing (HAZERS
        OFF, Stop and Abort still switch every hazer off)."""
        hv = self.hazers_view()
        names = list((hv or {}).get("names") or [])
        if i >= len(names):
            return
        name = names[i]
        on = hv is not None and name not in (hv.get("on") or [])
        who = self._who()
        if on:
            refusal = operator_gate(who, "arm") or role_gate(self._user(),
                                                            "arm")
            if refusal:
                self._log(f"Stream Deck: {name} arm refused: {refusal}",
                          action="hazer")
                return
        fn = getattr(self.conductor, "hazer", None)
        if fn is None:
            self._log(f"Stream Deck {name} pressed: no engine is connected "
                      f"in this build. Nothing was changed.", fault=True,
                      action="hazer")
            return
        r = fn(name, on, who=who, screen="Stream Deck")
        self._log(f"Stream Deck {name} {'arm' if on else 'disarm'}: "
                  f"{r.sentence}", fault=not r.ok, action="hazer", who=who,
                  screen="Stream Deck")

    def _hazers_off_press(self):
        who = self._who()
        fn = getattr(self.conductor, "hazers_off", None)
        if fn is None:
            self._log("Stream Deck HAZERS OFF pressed: no engine is "
                      "connected in this build.", fault=True, action="hazer")
            return
        r = fn(who=who, screen="Stream Deck")
        self._log(f"Stream Deck Hazers off: {r.sentence}", fault=not r.ok,
                  action="hazer", who=who, screen="Stream Deck")

    def _announce_press(self, ann_id):
        """One press, one announcement (no double tap, no hold). Not behind
        the operator gate: the engine names the deck's chosen operator, or
        the Stream Deck, and decides every rule itself."""
        who = self._who()
        fn = getattr(self.conductor, "announce", None)
        if fn is None:
            self._log("Stream Deck announcement pressed: no engine is "
                      "connected in this build. Nothing was played.",
                      fault=True, action="announce", who=who,
                      screen="Stream Deck")
            return
        self._ann_pressed = ann_id
        r = fn(ann_id, who=who, screen="Stream Deck")
        self._log(f"Stream Deck {ann_id} announcement: {r.sentence}",
                  fault=not r.ok, action="announce", who=who,
                  screen="Stream Deck")

    def _announce_stop(self):
        who = self._who()
        fn = getattr(self.conductor, "announce_stop", None)
        if fn is None:
            self._log("Stream Deck announcement Stop pressed: no engine is "
                      "connected in this build.", fault=True,
                      action="announce", who=who, screen="Stream Deck")
            return
        r = fn(who=who, screen="Stream Deck")
        self._log(f"Stream Deck announcement Stop: {r.sentence}",
                  fault=not r.ok, action="announce", who=who,
                  screen="Stream Deck")

    def _preroll_checking(self):
        """The engine is checking the rig before a show (ScreenKeys)."""
        s = self.screen
        fn = getattr(s, "preroll", None) if s is not None else None
        try:
            return fn() is True if fn is not None else False
        except Exception:
            return False

    def announce_view(self):
        """The engine's announcements for page 2's faces, or None."""
        s = self.screen
        fn = getattr(s, "announce", None) if s is not None else None
        if fn is None:
            return None
        try:
            return fn()
        except Exception:
            return None

    def tick(self):
        """Called once per MAIN-LOOP pass, UNCONDITIONALLY -- whether or
        not a new key snapshot arrived this pass (item 2, round 2 of the
        safety review). Advances the abort-hold and every group's arm-hold
        against self._prev_keys (the last snapshot run_once actually saw)
        and wall-clock time, exactly mirroring the approved bench demo's
        own abort_frac = min(1.0, (now - abort_down_at) / hold_s)
        pattern: recomputed every pass, never gated on a key event.
        Press/release detection stays in run_once, keyed off real
        transitions (unchanged, already correct); only the hold-DURATION
        math moves here, which is the part that used to only run inside
        run_once -- starved of calls for as long as a held key produced no
        further snapshots, if the real hardware reports a key's state only
        on CHANGE rather than continuously."""
        now = self._clock()
        latched = self._latched_now()
        if self.page in (2, HAZER_PAGE):
            why = self._page2_must_close(latched, self.alert_pending(), now)
            if why:
                self._to_page1(why)
        if self.page == 1 and self._prev_keys[ANNOUNCE_KEY] and \
                self._key5_hold.fired(now):
            # Page 1's key 5 held HAZER_HOLD_S: the hazers page, with every
            # check page 2 has when it opens (and no open alert).
            if self.alert_pending():
                self._log("Stream Deck: the hazers page was not opened: an "
                          "alert is open.", action="page")
            else:
                self._open_page2(self._prev_keys, now, latched,
                                 self.morning_open(), page=HAZER_PAGE)
        if latched != self._was_latched:
            # An Abort or a Reset from a screen (the deck has neither key
            # since 2026-10-26): every group's re-arm refractory window
            # starts, as the deck's own Abort and Reset started it (item 9,
            # round 2 of the safety review), so a reflexive "make sure"
            # press right after either cannot quietly re-arm.
            self._was_latched = latched
            self._disarmed_at = [now] * len(self.names)
        if self._prev_keys[TOP_START] and self._key0_fire and \
                self._stop_wins():
            # Key 0 went down as FIRE, is still held, and now a show or run
            # has started (or a group was armed, or live flames came on):
            # FIRE ends now and the same held key becomes a Stop hold at
            # once, with no second press (review of #130, low items, 4).
            # Read from the press itself, never from PurgeFire.firing,
            # which the engine's first refusal clears (review of #132, 1).
            self._key0_fire = False
            self._fire_release("Stop wins: something is running")
            self._stop_hold.press(now)
        if self._prev_keys[TOP_START] and self._stop_hold.fired(now):
            # A Stop hold that completes always stops (review of #130, 1):
            # every group disarmed on the arm link first, then the engine's
            # halt, whatever the engine or the run did during the hold (a
            # halt with nothing running still disarms everything). Checked
            # before the latch: Stop is reachable while aborted.
            self._do_stop()
            # Never ALSO complete an arm-hold in the SAME pass Stop just
            # disarmed every group in (the ordering rule Abort had on the
            # deck); _do_arm_fire's refractory check, set by the disarm, is
            # the second, independent backstop.
            return
        if latched:
            # While latched, run_once's own latched branch releases every
            # arm hold; nothing here arms.
            return
        # A slow or stuck key IMAGE write never drops a hold here (Jeff,
        # 2026-10-21: "If the USB is stalling but I'm holding an arm key,
        # it should work even if the animation isn't."). A hold ends on the
        # key's release as read, on a read or write that raises, or on the
        # input loop stopping; see DECK_STALL_RULING below.
        for i in range(len(self.names)):
            if self._key_down(i):
                if self._arm_holds[i].fired(now):
                    self._do_arm_fire(i, now)
            elif i in self._vpressed:
                # A screen hold: the deck's own ARM_HOLD_S AND the engine's
                # beat-evidenced SCREEN_HOLD_S, both, before anything goes
                # out. Checked before fired(), which consumes the press.
                if self._vheld.get(i, 0.0) < SCREEN_HOLD_S:
                    continue
                if self._arm_holds[i].fired(now):
                    self._vpressed.discard(i)
                    who, device = self._vwho.get(i, ("", "screen"))
                    why = self._do_arm_fire(i, now, who=who, screen=device)
                    if why:
                        self._report_refused(self._vholds.get(i), why)
                    else:
                        self._report_refused(self._vholds.get(i), "taken")

    def check_links(self):
        """Called once per MAIN-LOOP pass (after status.poll()), never
        once per key snapshot: updates the spoof alarm (item 1's second
        line of defence) independent of how many key transitions this
        pass processed, and journals an unconfirmed config the first time
        flamesafe's status says so."""
        category, reason = self._spoof_reason()
        if reason:
            self._raise_spoof_alarm(category, reason)
        else:
            self._clear_spoof_alarm()
        self._note_config_confirmed()

    def _note_config_confirmed(self):
        """One journal line when flamesafe's fresh status first says its
        config is not confirmed by Andy (`confirmed: false`, CONTRACT.md),
        and one more if it later says it is (nothing at all for a config
        that was confirmed all along). The keys no longer carry a
        mark for this (Jeff, 2026-10-19), so the journal is where the
        deck says it; the engine's status and the rack screen's Info
        popup say it too. Never repeats while the answer stands, never
        says anything while there is no fresh status, and never changes
        what the deck sends: this is a note about the config's numbers,
        not about any group's state."""
        if self.status.last is None or self.status.stale(self._clock):
            return
        confirmed = bool(self.status.last.get("confirmed", True))
        if confirmed == self._confirmed_noted:
            return
        self._confirmed_noted = confirmed
        if not confirmed:
            self._log("Stream Deck: flamesafe's config is not confirmed by "
                      "Andy (its status says confirmed false): the universe, "
                      "group map and arm value it sends are the config's own "
                      "numbers. The keys show each group's real state as "
                      "usual; this line will not repeat.",
                      action="config", outcome="unconfirmed")
        else:
            self._log("Stream Deck: flamesafe's config is confirmed.",
                      action="config", outcome="confirmed")

    def _spoof_reason(self):
        """(category, sentence): ("", "") normally; once flamesafe's
        reported arm state keeps diverging from what THIS deck actually
        sent (item 1, safety review of PR #31), `sentence` is the full,
        human-readable line, and `category` is a short, STABLE name for
        the KIND of problem -- it never includes a number that changes on
        its own.  The sender lock in arminput.py stops a forged frame from
        being ACCEPTED once this deck is locked in, but this is the second
        line of defence for the race at (re)connect or the lock's own
        staleness window -- this deck should never see state on the wire
        that it did not set and does not expect.

        `category` exists only so _raise_spoof_alarm can dedupe correctly
        (round 3 of the safety review, item 5: a PROVEN bug, flagged once
        before and never actually fixed -- the arm-seq-ahead sentence
        below embeds this deck's OWN seq, which ArmSocket advances on
        every single send, so the full sentence was a new string every
        tick and `self._spoof_alarm == reason` never once matched while
        the condition persisted, journaling the alarm every main-loop
        pass instead of once). `sentence` still carries the live numbers
        for the person reading it; only the dedup key leaves them out."""
        now = self._clock()
        if self._spoof_last_seq is None or \
                self.arm.seq < self._spoof_last_seq:
            # ArmSocket.open() reset our own counter (a reconnect):
            # flamesafe's status may still describe the sender from before
            # the gap for up to its own arm_stale_ms. Give it a moment
            # before trusting a seq mismatch, or every normal reconnect
            # would read as a spoof.
            self._spoof_grace_until = now + RECONNECT_GRACE_S
        self._spoof_last_seq = self.arm.seq
        if now < self._spoof_grace_until:
            return "", ""
        st = self.status.last
        if st is None or self.status.stale(self._clock):
            return "", ""
        arm_input = st.get("arm_input") or {}
        if arm_input.get("state") != "live":
            return "", ""
        seq = arm_input.get("seq")
        foreign = arm_input.get("foreign_senders")
        category = reason = ""
        if isinstance(seq, int) and not isinstance(seq, bool) \
                and seq > self.arm.seq:
            category = "arm-seq-ahead"
            reason = (f"flamesafe reports arm seq {seq}, ahead of the "
                      f"{self.arm.seq} this deck has sent")
        elif isinstance(foreign, int) and not isinstance(foreign, bool) \
                and foreign > 0:
            # Item 6, round 3 of the safety review: this deck could
            # previously go a whole rogue episode with NO alarm at all --
            # the foreign-disarm AND (arminput.py) can leave `wanted`
            # looking exactly like what this deck itself expects (the
            # rogue is only clearing bits this deck already has clear, or
            # the mismatch has not reached SPOOF_GRACE_S yet), and the seq
            # check above only fires if the rogue's own counter races ahead
            # of this deck's. flamesafe now says outright whether anyone
            # else is on the link; this deck trusts that over its own
            # necessarily incomplete view of the effect.
            category = "foreign-senders"
            reason = (f"flamesafe reports {foreign} other sender"
                      f"{'s' if foreign != 1 else ''} currently "
                      f"interacting with the arm link -- this deck is not "
                      f"the only one talking to it")
        else:
            by_name = {g.get("name"): g for g in (st.get("groups") or [])
                      if isinstance(g, dict)}
            for i, name in enumerate(self.names):
                g = by_name.get(name)
                if g is None:
                    continue
                want = g.get("wanted")
                if isinstance(want, bool) and want != self.arm.wanted[i]:
                    category = f"wanted-mismatch:{name}"
                    reason = (f"flamesafe reports {name} wanted={want}, "
                              f"but this deck last sent "
                              f"wanted={self.arm.wanted[i]}")
                    break
        if not reason:
            self._spoof_since = None
            return "", ""
        if self._spoof_since is None:
            self._spoof_since = now
        if (now - self._spoof_since) < SPOOF_GRACE_S:
            # A one-tick lag between this deck sending and flamesafe's
            # status catching up is normal, not a spoof; only a mismatch
            # that PERSISTS is.
            return "", ""
        return category, reason

    def _raise_spoof_alarm(self, category, reason):
        self._spoof_alarm = reason      # always the LATEST text, for draw()
        if self._spoof_category == category:
            return    # already alarming for this exact CATEGORY; no spam,
                      # even though `reason`'s own numbers keep moving
        self._spoof_category = category
        self._log(f"Stream Deck ALARM: {reason}. Flamesafe is honouring "
                  f"an arm signal this deck did not send; treat every "
                  f"group's displayed state as untrustworthy until this "
                  f"clears.", fault=True, action="spoof-alarm")

    def _clear_spoof_alarm(self):
        # NOTE: does not touch self._spoof_since. _spoof_reason() returns
        # "" both when there is genuinely nothing wrong (it resets
        # _spoof_since itself in that case) AND while a real mismatch is
        # still inside its own debounce window (SPOOF_GRACE_S) -- clearing
        # _spoof_since here too would reset that debounce clock on every
        # single check_links() call, so a persistent mismatch could never
        # outlast it and the alarm would never fire.
        if self._spoof_alarm:
            self._log("Stream Deck: the arm-link spoof alarm has cleared; "
                      "flamesafe's reported state matches what this deck "
                      "is sending again.", action="spoof-alarm")
        self._spoof_alarm = ""
        self._spoof_category = ""

    def status_for(self, name):
        """The real per-group status dict for `name`, or None if no status
        frame has ever arrived or the last one is stale (module docstring;
        CONTRACT.md's own "ltcplay shows red" rule, applied here too)."""
        if self.status.stale(self._clock) or self.status.last is None:
            return None
        for g in self.status.last.get("groups", []):
            if g.get("name") == name:
                return g
        return None

    def _top_fault_confirmed(self):
        """(fault, confirmed), flamesafe's own TOP-LEVEL status fields
        (item 4), or ("", True) while there is no fresh status at all --
        status_for already draws NO / FLAME / LINK in that case, which takes
        priority over fault/confirmed entirely, so the default here never
        has to mean anything on its own. Only `fault` reaches a key;
        `confirmed` is journaled (_note_config_confirmed), never drawn."""
        if self.status.stale(self._clock) or self.status.last is None:
            return "", True
        st = self.status.last
        return str(st.get("fault") or ""), bool(st.get("confirmed", True))

    def _flamesafe_ms(self):
        """How far flamesafe's own clock has run, in ms: its tick counter
        times its tick period, off the last status frame. None before the
        first frame. Unchanged while no new frame arrives, so everything
        driven by it stops dead when flamesafe does."""
        st = self.status.last
        if not isinstance(st, dict):
            return None
        hb, tick = st.get("heartbeat"), st.get("tick_ms")
        if not isinstance(hb, int) or isinstance(hb, bool):
            return None
        if not isinstance(tick, (int, float)) or isinstance(tick, bool) \
                or tick <= 0:
            tick = DEFAULT_TICK_MS
        return hb * tick

    def marquee_step(self):
        """The snakes' position: one step per CHASE_STEP_S of flamesafe's
        ticks (see CHASE_STEP_S). 0 before any status frame."""
        ms = self._flamesafe_ms()
        return 0 if ms is None else int(ms / (CHASE_STEP_S * 1000.0))

    def status_blink(self):
        """The flash phase of a key flamesafe's status says to flash: it
        changes every BLINK_HALF_S of flamesafe's ticks, and freezes with
        them."""
        ms = self._flamesafe_ms()
        return ms is None or int(ms / (BLINK_HALF_S * 1000.0)) % 2 == 0

    def engine_blink(self):
        """The flash phase of the top row: it changes every
        ENGINE_ANSWERS_PER_BLINK fresh answers from the engine. Called at
        least once per main-loop pass (20 Hz), several times faster than
        the engine is polled, so no answer goes uncounted. An engine that
        stops answering freezes it. No conductor wired: status_blink."""
        answered = getattr(self.conductor, "answered_at", None)
        if not callable(answered):
            return self.status_blink()
        at = answered()
        if at is not None and at != getattr(self, "_answer_seen", None):
            self._answer_seen = at
            self._answers = getattr(self, "_answers", 0) + 1
        n = getattr(self, "_answers", 0)
        return (n // ENGINE_ANSWERS_PER_BLINK) % 2 == 0

    def draw(self, fonts, blink_on, chase, group_blink=None):
        """One frame, as a PIL Image the size of the whole deck. Kept
        separate from run_once so the loop can draw at its own rate
        independently of how often new key presses arrive. `blink_on` is
        the top row's flash phase (engine_blink), `group_blink` the
        bottom row's (status_blink; None: the same as blink_on), `chase`
        the snakes' position (marquee_step)."""
        if group_blink is None:
            group_blink = blink_on
        Image, ImageDraw, _ImageFont = fonts.Image, fonts.ImageDraw, None
        canvas = Image.new("RGB", (W, H), BLACK)
        d = ImageDraw.Draw(canvas)
        now = self._clock()
        latched = self._latched_now()
        self._note_frame(latched, now)
        # An Abort from a screen: every ring on the deck solid red (Jeff's
        # approved latched look; the deck has no Abort key of its own).
        abort_frac = 1.0 if latched else 0.0
        arm_fills = {} if latched else {
            self._group_key[i]: f for i, f in
            enumerate(h.fraction(now) for h in self._arm_holds)
            if f > 0 and i in self._group_key}
        # Stop's hold fills its own ring, latched or not (Stop stays
        # reachable while aborted, as on the Rack screen).
        stop_frac = self._stop_hold.fraction(now)
        if self.page != 1:
            # No arm key on page 2 or 3: only Stop's ring can fill.
            arm_fills = {}
        elif self._key5_hold.fraction(now) > 0:
            arm_fills[ANNOUNCE_KEY] = self._key5_hold.fraction(now)
        if stop_frac > 0:
            arm_fills[TOP_START] = stop_frac
        draw_marquee(d, chase, abort_frac, arm_fills)
        b0 = face_box(TOP_START)
        engine_fault = (getattr(self.conductor, "fault", "") or
                        (self.conductor is not None and
                         hasattr(self.conductor, "unreachable") and
                         self.conductor.unreachable()))
        cues_fault = getattr(self.conductor, "cues_fault", "") or ""
        if stop_frac * STOP_HOLD_S >= STOP_FACE_AFTER_S:
            # A Stop hold under way for STOP_FACE_AFTER_S: HOLD TO STOP,
            # over every other face (an engine fault, an open alert's ACK,
            # FIRE), so the operator sees the Stop coming and can let go
            # to cancel it (review of #130, second pass, 1). A tap shorter
            # than this only shows what the key was.
            fonts.show_key(d, b0, ["HOLD TO", "STOP"], CHAMPAGNE,
                           bg=BRONZE, kind="sans", max_size=16)
        elif engine_fault:
            # Fix round 2, E: the engine did not take a press (or cannot be
            # reached): said on the deck itself, not only its console. A
            # fault face outranks the ACK face below (review of PR #59):
            # a refused press is never hidden behind a waiting alert.
            face = list(getattr(self.conductor, "fault_face", None) or
                        ("ENGINE", "FAULT")) if getattr(
                self.conductor, "fault", "") else ["ENGINE", "FAULT"]
            try:
                gone = (hasattr(self.conductor, "unreachable") and
                        self.conductor.unreachable())
            except Exception:                    # noqa: BLE001
                gone = True
            if gone and face == ["CHECK", "RACK"]:
                # The engine has stopped answering its polls too: a Stop
                # that timed out was not "taken", it is an engine fault
                # (third review of #130, 3).
                face = ["ENGINE", "FAULT"]
            fonts.show_key(d, b0, face,
                           BLACK if blink_on else RED,
                           bg=RED if blink_on else None, kind="sans",
                           max_size=16)
        elif cues_fault:
            # Review of PR #43, P1-3: the show's flame cues are refused (a
            # render that does not fit the layout, and the like): every
            # flame cue is zero, and that is a fault, not a quiet zero.
            fonts.show_key(d, b0, ["CUES", "FAULT"],
                           BLACK if blink_on else RED,
                           bg=RED if blink_on else None, kind="sans",
                           max_size=16)
        elif self.alert_pending():
            # Jeff's ruling 2 (2026-10-12): while an alert is open the key
            # that acknowledges it says so, in a look of its own (ACK_ON /
            # ACK_OFF), and reads START NOW again once it is cleared.
            bg, text = ACK_ON if blink_on else ACK_OFF
            fonts.show_key(d, b0, ["ACK", "ALERT"], text, bg=bg, kind="sans",
                           max_size=16)
        elif latched and stop_frac > 0:
            fonts.show_key(d, b0, ["HOLD TO", "STOP"], CHAMPAGNE,
                           bg=BRONZE, kind="sans", max_size=16)
        elif latched and (self.start_key_mode() in STOP_MODES or
                          self._idle_stop()):
            # Aborted with a run still going: Stop is reachable, as on the
            # Rack screen (Jeff, 2026-10-26). It never touches the latch.
            # Aborted with nothing running but a group armed or live
            # flames on: STOP ALL (delta review of #132, D1).
            fonts.show_key(d, b0, ["STOP", "RUN"]
                           if self.start_key_mode() in STOP_MODES
                           else ["STOP", "ALL"], CHAMPAGNE, bg=BRONZE,
                           kind="sans")
        elif latched:
            fonts.show_key(d, b0, ["START", "NOW"], DIM_TEXT)
        elif self.morning_open() and not self._stop_wins():
            # Re-mapped while the Morning and Shutdown page is open (Jeff,
            # 2026-10-17): this key is FIRE for the purge. Red on dark while
            # a purge group is armed, flashing while held, dim PURGE when
            # nothing is selected. Restores when the page closes.
            pf = self.purge_fire
            left = self.purge_timed_left()
            if left is not None:
                # A timed purge (morning.py, 2026-10-29): PURGING and the
                # engine's seconds left. Pressing FIRE now is a second
                # purge press, which ends it at the engine.
                fonts.show_key(d, b0, ["PURGING", f"{int(-(-left // 1))} S"],
                               BLACK if blink_on else RED,
                               bg=RED if blink_on else None, kind="sans",
                               max_size=16)
            elif pf is not None and getattr(pf, "firing", False):
                fonts.show_key(d, b0, ["FIRING"], BLACK if blink_on else RED,
                               bg=RED if blink_on else None, kind="sans")
            elif self.purge_wanted():
                fonts.show_key(d, b0, ["FIRE", "HOLD"], RED, kind="sans")
            else:
                fonts.show_key(d, b0, ["PURGE", "NONE"], DIM_TEXT, kind="sans",
                               max_size=16)
        else:
            running = self.show_running_provider() is True
            held = self._held_hint()
            op = self._who()
            if stop_frac > 0:
                # Stop being held: the ring fills; let go early, nothing.
                # During a scheduled show too (Jeff, 2026-10-26).
                fonts.show_key(d, b0, ["HOLD TO", "STOP"], CHAMPAGNE,
                               bg=BRONZE, kind="sans", max_size=16)
            elif running and held:
                # The approved demo's PAUSED: the operator's own Hold.
                fonts.show_key(d, b0, ["PAUSED"], CHAMPAGNE, bg=PAUSED_BG,
                               kind="sans")
            elif running and self._audio_lost():
                # Only this key says so.
                bg, text = AUDIO_LOST_ON if blink_on else AUDIO_LOST_OFF
                fonts.show_key(d, b0, ["AUDIO", "LOST"], text, bg=bg,
                               kind="sans")
            elif running:
                # The key says what the show is doing: NOW PLAYING,
                # flashing green on the engine's own answers. A hold is
                # still Stop, a complete halt (Jeff, 2026-10-26).
                bg, text = PLAYING_ON if blink_on else PLAYING_OFF
                fonts.show_key(d, b0, ["NOW", "PLAYING"], text, bg=bg,
                               kind="sans")
            elif self._preroll_checking():
                # The rig check before a show (preroll.py): said on the
                # key, in Stop's colours, because the key is still Stop
                # (a hold stops, and cancels the check).
                fonts.show_key(d, b0, ["CHECKING", "RIG"], CHAMPAGNE,
                               bg=BRONZE, kind="sans", max_size=14)
            elif self.start_key_mode() == "stop":
                # Run pressed and playback going: this key is Stop (Jeff,
                # 2026-10-26), held STOP_HOLD_S: every flame group
                # disarmed, then the Rack screen's Stop.
                fonts.show_key(d, b0, ["STOP", "RUN"], CHAMPAGNE, bg=BRONZE,
                               kind="sans")
            elif self.start_key_mode() == "unknown" or \
                    self._idle_stop():
                # The engine or the scheduler cannot be read, or nothing
                # is running but a group is armed or live flames are on:
                # the key is Stop, a complete halt (review of #130, 1;
                # second pass, 6; low items, 3).
                fonts.show_key(d, b0, ["STOP", "ALL"], CHAMPAGNE, bg=BRONZE,
                               kind="sans")
            elif held:
                # The demo's idle hold: START NOW dims while held.
                fonts.show_key(d, b0, ["START", "NOW"], DIM_TEXT)
            elif self.user_toast(now):
                # Who just signed in (or out) on the rack screen, for a few
                # seconds, only while nothing else needs this key.
                fonts.show_key(d, b0, self.user_toast(now), CHAMPAGNE,
                               kind="sans", max_size=16)
            elif op == UNSIGNED:
                # Users on and nobody signed in here (2026-10-28): view
                # only, like the rack screen.
                fonts.show_key(d, b0, ["SIGN", "IN"], DIM_TEXT, kind="sans",
                               max_size=16)
            elif operator_gate(op, "start"):
                fonts.show_key(d, b0, ["PICK", "OPERATOR"], DIM_TEXT, kind="sans",
                              max_size=16)
            else:
                fonts.show_key(d, b0, ["START", "NOW"], CHAMPAGNE)
        if self.page == 2:
            self._draw_page2(fonts, d, blink_on)
            return canvas
        if self.page == HAZER_PAGE:
            self._draw_page3(fonts, d)
            return canvas
        if self._guard is not None and self._guard_frame is None:
            self._guard_frame = self.frame_id
            self._guard_frame_at = now
        # Key 5 opens the announcements page (module docstring), drawn only
        # when the engine says announcements are running.
        av = self.announce_view()
        if av and av.get("configured") and not latched and \
                self.hazers_view():
            # Both: a tap is the announcements, a hold the hazers.
            fonts.show_key(d, face_box(ANNOUNCE_KEY),
                           ["ANNOUNCE", "HOLD:", "HAZERS"], CHAMPAGNE,
                           bg=BRONZE, kind="sans", max_size=12)
        elif av and av.get("configured") and not latched:
            fonts.show_key(d, face_box(ANNOUNCE_KEY), ["ANNOUNCE"],
                           CHAMPAGNE, bg=BRONZE, kind="sans", max_size=14)
        elif self.hazers_view() and not latched:
            fonts.show_key(d, face_box(ANNOUNCE_KEY), ["HAZERS"],
                           CHAMPAGNE, bg=BRONZE, kind="sans", max_size=14)
        fault, _confirmed = self._top_fault_confirmed()
        # Only the groups with a key are drawn, in key order, each under
        # its label: a screen-only group (module docstring 5) has none.
        # `slot` is the key's place in the row (NO / FLAME / LINK).
        for k in GROUP_KEYS:
            if k not in self._key_group:
                continue
            i, slot = self._key_group[k], k - GROUP_KEYS[0]
            name, label = self.names[i], self.labels[i]
            box = face_box(k)
            if self._spoof_alarm and group_blink:
                # Item 1's visible alarm: while it is active, every group
                # key shows it, flashing -- the deck itself no longer
                # trusts what flamesafe is reporting, so it must not keep
                # drawing a calm ARMED/OFF/HELD look underneath. This check
                # now runs even while latched (round 2 of the safety
                # review): a latch must never hide a real spoof/divergence
                # alarm either.
                arm_key_image(fonts, d, box, label,
                             ("ALARM", None, RED, CHAMPAGNE, False),
                             group_blink)
                continue
            if latched:
                # Item 1, round 2 of the safety review: a latched screen
                # used to paint a flat OFF over every group key here,
                # whatever flamesafe was actually reporting. That is worse
                # than no fix at all -- if a rogue sender armed a group
                # while the real deck was briefly quiet (see arminput.py's
                # FOREIGN DISARM fix) and Abort then latched the screen,
                # this blanked the one place an operator could see the
                # group was STILL actually armed. Show the REAL per-group
                # state, exactly as the un-latched path below does; only
                # the hold-in-progress key image is skipped, because
                # run_once already released every arm-hold the instant it
                # entered the latched branch, so there is no hold left to
                # draw.
                # Jeff's design greys every key but RESET while latched;
                # meaning wins: a group still reported armed, a fault or no
                # link keeps its real look. Only a group that is really off
                # (disarmed or held) greys out.
                st = self.status_for(name)
                look = group_look(st, fault=fault, slot=slot)
                if st is not None and not fault and \
                        st.get("armed") != "armed":
                    look = (look[0], look[1], LATCHED_GREY, DIM_TEXT, False)
                arm_key_image(fonts, d, box, label, look, group_blink)
                continue
            hold_frac = self._arm_holds[i].fraction(now)
            if hold_frac > 0:
                draw_group_hold(fonts, d, box, label, hold_frac)
                continue
            look = group_look(self.status_for(name), fault=fault, slot=slot)
            mv = self.morning_view()
            if mv and mv.get("open") is True:
                # The Morning page's re-map: a group every machine of which
                # is locked out reads LOCKED; one armed for the purge says
                # so under its name. flamesafe's real state is never hidden.
                locks = mv.get("locks") if isinstance(mv.get("locks"), dict) \
                    else {}
                if i in (locks.get("groups_fully_locked") or []):
                    look = ("LOCKED", None, LATCHED_GREY, DIM_TEXT, False)
                elif i in self._purge_armed and look[0] == "ARMED":
                    look = ("PURGE", "ARMED") + tuple(look[2:])
            arm_key_image(fonts, d, box, label, look, group_blink)
        if self.status.stale(self._clock) or self.status.last is None:
            # Fewer than four groups: the rest of NO / FLAME / LINK still
            # reads whole, on the keys that have no group.
            for i in range(len(self.names), len(GROUP_KEYS)):
                arm_key_image(fonts, d, face_box(GROUP_KEYS[i]), "",
                              group_look(None, slot=i), group_blink)
        return canvas

    def _draw_page2(self, fonts, d, blink_on):
        """Page 2's faces (keys 1 to 5; key 0 is drawn as on page 1): each
        announcement READY, NO FILE (the recording is not on the PC yet),
        NO OUTPUT, or PLAYING; STOP ANNC.; BACK. Display only."""
        av = self.announce_view() or {}
        # The engine's refusal of the last press, on the pressed key, for
        # ANNOUNCE_SAID_S (second review of #129, 6).
        refused = {}
        said = getattr(self.conductor, "announce_said", None)
        if said and not said[0] and len(said) > 3:
            clk = getattr(self.conductor, "_clock", None) or self._clock
            try:
                fresh = clk() - said[2] <= ANNOUNCE_SAID_S
            except Exception:
                fresh = False
            if fresh:
                # On the key whose press it answers (delta review, 5).
                refused[said[3]] = announce_refusal_word(said[1])
        ready = av.get("ready") if isinstance(av.get("ready"), dict) else {}
        missing = av.get("missing") if isinstance(av.get("missing"),
                                                  dict) else {}
        playing = av.get("playing")
        for k, aid in PAGE2_PLAY.items():
            box = face_box(k)
            lines = list(PAGE2_FACE[aid])
            if aid in refused:
                fonts.show_key(d, box, lines + [refused[aid]],
                               BLACK if blink_on else RED,
                               bg=RED if blink_on else None, kind="sans",
                               max_size=12)
            elif not av or av.get("stale"):
                fonts.show_key(d, box, lines + ["NO STATUS"], AMBER,
                               kind="sans", max_size=12)
            elif playing == aid:
                fonts.show_key(d, box, lines + ["PLAYING"],
                               BLACK if blink_on else AMBER,
                               bg=AMBER if blink_on else BRONZE,
                               kind="sans", max_size=14)
            elif not av.get("configured"):
                fonts.show_key(d, box, lines + ["NOT SET UP"], DIM_TEXT,
                               kind="sans", max_size=12)
            elif missing.get(aid):
                fonts.show_key(d, box, lines + ["NO FILE"], AMBER,
                               kind="sans", max_size=12)
            elif not ready.get(aid):
                fonts.show_key(d, box, lines + ["CANNOT PLAY"], AMBER,
                               kind="sans", max_size=12)
            elif not av.get("device_ok"):
                fonts.show_key(d, box, lines + ["NO OUTPUT"], AMBER,
                               kind="sans", max_size=12)
            else:
                fonts.show_key(d, box, lines, CHAMPAGNE, bg=BRONZE,
                               kind="sans", max_size=16)
        live = bool(playing) or bool(av.get("stale")) or not av
        if "stop" in refused:
            fonts.show_key(d, face_box(PAGE2_STOP),
                           ["STOP", refused["stop"]],
                           BLACK if blink_on else RED,
                           bg=RED if blink_on else None, kind="sans",
                           max_size=12)
        else:
            fonts.show_key(d, face_box(PAGE2_STOP), ["STOP", "ANNC."],
                           CHAMPAGNE if live else DIM_TEXT,
                           bg=BRONZE if live else None, kind="sans",
                           max_size=16)
        fonts.show_key(d, face_box(PAGE2_BACK), ["BACK"], CHAMPAGNE,
                       kind="sans", max_size=16)

    def _draw_page3(self, fonts, d):
        """Page 3's faces (keys 1 to 5; key 0 is drawn as on page 1): each
        hazer's name and ARMED (green; PAUSED on a Hold) or DISARMED, NO
        STATUS while the engine's word is stale; HAZERS OFF; BACK. Display
        only. The levels are set on the screens, never here."""
        hv = self.hazers_view()
        names = list((hv or {}).get("names") or [])
        on = set((hv or {}).get("on") or [])
        for k, i in PAGE3_HAZER.items():
            box = face_box(k)
            if i >= len(names):
                continue
            line = _fit_name(names[i]).upper()
            if hv is None:
                fonts.show_key(d, box, [line, "NO STATUS"], AMBER,
                               kind="sans", max_size=12)
            elif names[i] in on and hv.get("paused"):
                # A Hold: still armed, its haze stopped until Resume.
                fonts.show_key(d, box, [line, "ARMED", "PAUSED"], BLACK,
                               bg=GREEN, kind="sans", max_size=12)
            elif names[i] in on:
                fonts.show_key(d, box, [line, "ARMED"], BLACK, bg=GREEN,
                               kind="sans", max_size=14)
            else:
                fonts.show_key(d, box, [line, "DISARMED"], CHAMPAGNE,
                               bg=BRONZE, kind="sans", max_size=14)
        fonts.show_key(d, face_box(PAGE3_OFF), ["HAZERS", "OFF"], CHAMPAGNE,
                       kind="sans", max_size=16)
        fonts.show_key(d, face_box(PAGE3_BACK), ["BACK"], CHAMPAGNE,
                       kind="sans", max_size=16)

    def user_toast(self, now):
        """The Start key's lines naming who is signed in on this machine,
        for USER_TOAST_S after that changes; None otherwise. The first
        answer after the deck starts names them too."""
        u = self._user()
        if u is not None and not u.get("stale"):
            key = (u.get("name") or "",)
            if key != self._user_seen:
                self._user_seen = key
                self._user_toast_until = now + USER_TOAST_S
        if now >= self._user_toast_until or self._user_seen == ("?",):
            return None
        name = self._user_seen[0]
        return ["SIGNED IN", _fit_name(name).upper()] if name else \
            ["SIGNED", "OUT"]

    def _audio_lost(self):
        """Whether the engine says the show audio is lost (an
        EngineConductor's audio_lost); False with no conductor wired or
        one that cannot say."""
        lost = getattr(self.conductor, "audio_lost", None)
        if not callable(lost):
            return False
        try:
            return bool(lost())
        except Exception:
            return False

    def _reported_armed(self, i):
        st = self.status_for(self.names[i])
        return bool(st) and st.get("armed") == "armed"

    def _note_frame(self, latched, now):
        """Called by draw(), once per frame composed: numbers it."""
        self.frame_id += 1

    def frame_shown(self, shown):
        """`shown`: (frame id, clock time) of the newest frame the deck has
        been fully written (DisplayWorker.last_shown), or None. Kept for
        the display's bookkeeping; since the deck's Hold and Abort keys
        went (2026-10-26) no press depends on a face the deck showed."""
        if shown:
            self.last_shown = shown

    def _held_hint(self):
        """Whether the show looks HELD right now, for the Start key's
        PAUSED face (it was the deck's Hold key's own toggle, Hold vs
        Resume, until that key went on 2026-10-26). The scheduler's own HOLD or PAUSED first
        (held_provider): a Hold pressed between shows puts the scheduler on
        hold and, by design, asks the conductor nothing (no show is
        playing), so the conductor's look stays BETWEEN. Reading the look
        alone (before 2026-10-21) left this key on HOLD, and the next press
        asked the engine to hold a schedule already on hold: refused, and
        drawn as ENGINE FAULT. Then the conductor snapshot when one is
        wired; otherwise this defaults to "Hold" (the safer guess: asking
        to hold an already-held show is a no-op, per conductor.py)."""
        try:
            if self.held_provider() is True:
                return True
        except Exception:
            pass
        if self.conductor is not None:
            try:
                return self.conductor.snapshot().get("look") in ("HELD", "DARK")
            except Exception:
                return False
        return False


# --------------------------------------------------------------------------
# The arm link's own pace, and the deck's screen on a thread of its own
# (2026-10-21, the five hour soak on the show PC, build bench-2026.10.21-
# f4badcf). flamesafe journaled 107 "arm input stale ... latch-reset" events
# in a show: this process sent its arm assertion on the same loop as the
# key images it writes to the Stream Deck over USB, and on a busy machine
# one frame of key images took seconds (deck.log: worst pass 2684 ms and
# 6659 ms, "USB writes worst 6644 ms"), so flamesafe heard nothing for more
# than arm_stale_ms and disarmed. Fail safe, but it cycled the arm in a
# show. Now:
#   - ArmPacer sends the arm assertion at ARM_SEND_HZ on its own thread.
#     It never waits on a USB write. It sends only while the deck's INPUT
#     loop (key reads, holds, Abort, disarm) has run within INPUT_STALE_S:
#     a frozen input loop still goes silent on the arm link, and flamesafe
#     still disarms within arm_stale_ms of that (failure mode 7, kept).
#   - DisplayWorker writes key images on its own thread, one frame at a
#     time, newest first: a frame not yet started when a newer one comes is
#     dropped (latest wins), so the backlog is never more than one frame
#     and nothing ever waits for it. Key reads stay on the input loop, on
#     the same hidapi handle: hidapi 0.15 keeps a read's and a write's
#     overlapped I/O and error text apart, and its Python binding releases
#     the GIL for both, so a read is never queued behind a write.
#   - UsbMeter keeps the worst frame write of the last USB_WINDOW_S for the
#     rack screen (deck-input carries it to the engine) and journals one
#     fault line when a write passes USB_SLOW_S. Display only: nothing it
#     measures is ever read by arming, disarming or Abort.
# --------------------------------------------------------------------------
#
# DECK_STALL_RULING (Jeff, 2026-10-21): "If the USB is stalling but I'm
# holding an arm key, it should work even if the animation isn't." The soak
# saw frame writes of 2.6 to 6.6 s on a busy PC while the key reads kept
# working. So how long a key IMAGE write takes is never evidence about the
# keys: a slow or stuck image write does not drop an arm hold, release a
# purge FIRE, stop a hold completing, or retire the deck. It is shown (the
# rack screen's Deck chip, UsbMeter's journal line) and nothing more.
#
# What still ends a hold or a held purge FIRE, as before: the key's
# release, read; a key read or a key image write that raises (an unplug,
# the outage path: holds dropped, FIRE released, the deck retired, the
# "deck" alert; hidapi's write returns -1 on a Windows write timeout and
# does not raise, so a slow write is never one); the input loop stopping (the arm link goes silent after
# INPUT_STALE_S and flamesafe disarms in arm_stale_ms, and the FIRE
# heartbeats stop after PURGE_INPUT_STALE_S); Abort; the engine refusing.
#
# No liveness probe is used, because the pinned stack gives no reliable
# one (hidapi 0.15.0, the `hid` module; checked against its source):
#   - a read with nothing to say returns [] whether no key changed or the
#     deck's firmware has hung but is still enumerated. Only a device that
#     is gone (unplugged, removed, a driver error) makes the read fail, and
#     that already raises DeckDisconnected.
#   - a feature report (serial 0x03, firmware 0x04) has no timeout on
#     Windows (GetOverlappedResult waits forever) and none we can set on
#     macOS; on the same handle it races the display thread's hid_write
#     (both reset the handle's one error string, so a concurrent pair can
#     free it twice); a second handle is refused on macOS (hidapi opens it
#     exclusive); and nothing shows it answers during the very USB stalls
#     the soak saw, so it could retire a working deck mid-hold, against
#     the ruling.
#   - the Mini has no documented key-state poll (an input report by
#     GET_REPORT); Elgato's own library only reads the interrupt reports.
# The residual risk, accepted by the ruling: a deck whose firmware hangs
# while staying on the bus, with a key held, never reports that key's
# release. An arm hold on it completes after ARM_HOLD_S as if still held
# (it was held at least to the moment the deck hung), and a held purge
# FIRE keeps firing until the operator presses Abort, disarms from a
# screen, the cable is pulled (an unplug, above) or the deck program stops.
INPUT_STALE_S = 0.2       # the input loop's pass is 50 ms; four missed passes
PURGE_INPUT_STALE_S = 1.0  # a held FIRE's heartbeats stop when the input
                           # loop has not run for this long (PurgeFire.alive)
USB_SLOW_S = 1.0          # a frame of key images slower than this is a fault
RETRY_UNWRITTEN_S = 0.05  # a frame the deck did not take is written again
                          # after this (DisplayWorker.step)
USB_WINDOW_S = 60.0       # the rack screen's "worst" looks back this far
USB_LOG_EVERY_S = 600.0   # one console line with the worst, this often


class ArmPacer:
    """The arm assertion's own thread: arm.send(names) every 1/ARM_SEND_HZ
    while beat() was called within INPUT_STALE_S. Pure apart from the
    thread: step() is one decision and one send, with an injected clock,
    so the rule is unit-tested without time passing."""

    def __init__(self, arm, names, clock=time.monotonic,
                 period=1.0 / ARM_SEND_HZ, stale_s=INPUT_STALE_S):
        self.arm = arm
        self.names = names
        self._clock = clock
        self.period = period
        self.stale_s = stale_s
        self._beat_at = None
        self._last_sent = None
        self.sent = 0
        self.silent = 0          # steps that sent nothing: the input is stale
        self.worst_gap_s = 0.0   # the longest time between two of its sends
        self.error = ""
        self._stop = threading.Event()
        self._thread = None

    def beat(self):
        """The input loop ran a pass just now (run_forever, and
        _hold_link_steady while the deck hardware is away)."""
        self._beat_at = self._clock()

    def live(self, now=None, stale_s=None):
        at = self._beat_at
        if at is None:
            return False
        now = self._clock() if now is None else now
        return now - at <= (self.stale_s if stale_s is None else stale_s)

    def step(self):
        """One tick: send if the input loop is live, else say nothing (the
        arm link goes silent and flamesafe disarms in arm_stale_ms, as it
        always did for a frozen deck program). Never raises."""
        now = self._clock()
        if not self.live(now):
            self.silent += 1
            return False
        try:
            self.arm.send(self.names)
        except Exception as e:                   # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            return False
        if self._last_sent is not None:
            self.worst_gap_s = max(self.worst_gap_s, now - self._last_sent)
        self._last_sent = now
        self.sent += 1
        return True

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-deck-arm-pacer")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)

    def _loop(self):
        nxt = time.monotonic()
        while not self._stop.is_set():
            self.step()
            nxt += self.period
            left = nxt - time.monotonic()
            if left < 0:
                # Late (the machine was busy): carry on from now, never a
                # burst of catch-up frames.
                nxt = time.monotonic()
                left = 0.0
            self._stop.wait(left)


class UsbMeter:
    """How long writing a frame of key images to the deck takes: the worst
    of the last USB_WINDOW_S, and one fault line in the journal when a
    write passes USB_SLOW_S (and one when they have all been under it for a
    whole window again). Display and journal only: arming never reads it.
    Thread safe: the display worker records, the input loop checks."""

    def __init__(self, journal=None, clock=time.monotonic, echo=print):
        self._journal = journal or (lambda text, **kw: None)
        self._clock = clock
        self._echo = echo
        self._lock = threading.Lock()
        self._samples = []        # (clock at the end, seconds)
        self._inflight_since = None
        self.slow = False         # inside a slow episode, journaled once
        self._episode_worst = 0.0
        self._logged_at = None
        self.frames = 0
        self._fails = []          # (clock, keys the deck did not take)
        self.fail_total = 0
        self._fail_said = False   # inside a failing spell, journaled once

    def started(self, at=None):
        with self._lock:
            self._inflight_since = self._clock() if at is None else at

    def record(self, secs, at=None):
        at = self._clock() if at is None else at
        with self._lock:
            self._inflight_since = None
            self._samples.append((at, float(secs)))
            self.frames += 1
            self._prune(at)

    def _prune(self, now):
        cut = now - USB_WINDOW_S
        while self._samples and self._samples[0][0] < cut:
            self._samples.pop(0)
        while self._fails and self._fails[0][0] < cut:
            self._fails.pop(0)

    def failed(self, keys=1, at=None):
        """A frame with `keys` key writes the deck did not take (hidapi's
        write returned -1: review of PR #83, P2-2). Display and journal
        only."""
        at = self._clock() if at is None else at
        with self._lock:
            self._fails.append((at, int(keys)))
            self.fail_total += int(keys)
            self._prune(at)

    def failed_count(self, now=None):
        """Key writes the deck did not take in the last USB_WINDOW_S."""
        now = self._clock() if now is None else now
        with self._lock:
            self._prune(now)
            return sum(n for _a, n in self._fails)

    def worst_s(self, now=None):
        """The worst frame write of the last USB_WINDOW_S, or the write in
        progress if it has already run longer: a write that never finishes
        still shows. 0.0 before any."""
        now = self._clock() if now is None else now
        with self._lock:
            self._prune(now)
            worst = max([s for _a, s in self._samples] + [0.0])
            if self._inflight_since is not None:
                worst = max(worst, now - self._inflight_since)
        return worst

    def worst_ms(self, now=None):
        return int(round(self.worst_s(now) * 1000.0))

    def check(self, now=None):
        """Called by the input loop once a pass: the fault line at the
        start of a slow episode, the all-clear at its end, and a console
        line with the worst every USB_LOG_EVERY_S. Never raises."""
        now = self._clock() if now is None else now
        worst = self.worst_s(now)
        try:
            if worst >= USB_SLOW_S:
                self._episode_worst = max(self._episode_worst, worst)
                if not self.slow:
                    self.slow = True
                    self._journal(
                        f"Stream Deck: writing the key images to the deck "
                        f"over USB took {worst:.1f} s (over "
                        f"{USB_SLOW_S:g} s), so the deck's keys are behind "
                        f"while this lasts. Arming, disarming, Abort and a "
                        f"held FIRE do not wait on it and are not stopped "
                        f"by it, and the deck is not treated as unplugged "
                        f"for it: the arm link and the key reads have "
                        f"threads of their own. This line does not "
                        f"repeat until the writes have stayed under "
                        f"{USB_SLOW_S:g} s for {USB_WINDOW_S:g} s.",
                        fault=True, action="deck-usb")
            elif self.slow:
                self.slow = False
                self._journal(
                    f"Stream Deck: USB writes to the deck have stayed under "
                    f"{USB_SLOW_S:g} s for {USB_WINDOW_S:g} s again; the "
                    f"worst in that slow spell was "
                    f"{self._episode_worst:.1f} s.", action="deck-usb")
                self._episode_worst = 0.0
            fails = self.failed_count(now)
            if fails and not self._fail_said:
                self._fail_said = True
                self._journal(
                    f"Stream Deck: the deck did not take {fails} key image "
                    f"write(s) over USB (the USB driver gave up on them), "
                    f"so its keys may show an older face until a resend "
                    f"goes through. A key press acts on the face the deck "
                    f"last really showed. Arming, disarming and Abort do "
                    f"not wait on it. This line does not repeat until no "
                    f"write has failed for {USB_WINDOW_S:g} s.",
                    fault=True, action="deck-usb")
            elif not fails and self._fail_said:
                self._fail_said = False
                self._journal(
                    f"Stream Deck: no key image write has failed for "
                    f"{USB_WINDOW_S:g} s.", action="deck-usb")
            if self._logged_at is None:
                self._logged_at = now
            elif now - self._logged_at >= USB_LOG_EVERY_S:
                self._logged_at = now
                self._echo(f"Stream Deck: USB key image writes, worst "
                           f"{worst * 1000.0:.0f} ms per frame over the last "
                           f"{USB_WINDOW_S:g} s ({self.frames} frames "
                           f"written so far).")
        except Exception:                        # noqa: BLE001
            pass


class DisplayWorker:
    """Writes key images to the deck on a thread of its own, so a slow USB
    write never holds up a key read, a hold, Abort or the arm link.

    submit() hands over the newest frame and returns at once. Only one
    frame waits: a newer submit replaces a waiting one (latest wins,
    counted in `dropped`); the frame being written finishes first. A write
    error (an unplug) is kept and raised to the input loop by check(), so
    it is handled exactly like a key read that failed. retire() closes the
    deck on this thread once the write in hand is done, so the input loop
    never waits on it either. step() is one write, for tests and for a
    loop with no thread (run_forever(threaded=False))."""

    def __init__(self, deck, Image, meter=None, clock=time.monotonic):
        self.deck = deck
        self.Image = Image
        self.meter = meter
        self._clock = clock
        self._cv = threading.Condition()
        self._pending = None      # (frame id, image)
        self._retire = False
        self._stop = False
        self.error = None
        self.shown = None         # (frame id, clock when fully written)
        self.writing = None       # frame id being written now
        self._write_since = None  # clock when the write in hand began
        self.dropped = 0
        self.written = 0
        self.unwritten = 0        # frames a key of which the deck did not take
        self._thread = None

    def submit(self, frame_id, img):
        with self._cv:
            if self._pending is not None:
                self.dropped += 1
            self._pending = (frame_id, img)
            self._cv.notify()

    def last_shown(self):
        with self._cv:
            return self.shown

    def check(self):
        """Raise the worker's write error, if any, on the caller's thread."""
        e = self.error
        if e is not None:
            raise e

    def step(self):
        """Write the waiting frame, if there is one. True if one was."""
        with self._cv:
            item, self._pending = self._pending, None
            if item is None:
                return False
            self.writing = item[0]
            t0 = self._write_since = self._clock()
        frame_id, img = item
        if self.meter is not None:
            self.meter.started(t0)
        unwritten = 0
        try:
            for k in range(6):
                ox, oy = key_origin(k)
                if self.deck.set_key(self.Image, k,
                                     img.crop((ox, oy, ox + K, oy + K))) \
                        is False:
                    unwritten += 1
        except Exception as e:                   # noqa: BLE001
            with self._cv:
                self.writing = None
                self._write_since = None
                if self.error is None:
                    self.error = e
            return False
        done = self._clock()
        if self.meter is not None:
            self.meter.record(done - t0, done)
        if unwritten:
            # Review of PR #83, P2-2: a key the deck did not take. The
            # frame is NOT shown (frame_shown is the display's word on what
            # the deck really shows), and it is written again unless a
            # newer one waits.
            if self.meter is not None:
                self.meter.failed(unwritten, done)
            with self._cv:
                self.writing = None
                self._write_since = None
                self.unwritten += 1
                if self._pending is None:
                    self._pending = item
                # Not a spin on a deck that refuses at once.
                self._cv.wait(RETRY_UNWRITTEN_S)
            return False
        with self._cv:
            self.writing = None
            self._write_since = None
            self.written += 1
            self.shown = (frame_id, done)
        return True

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-deck-display")
        self._thread.start()
        return self

    def _loop(self):
        while True:
            with self._cv:
                while self._pending is None and not self._retire and \
                        not self._stop:
                    self._cv.wait(0.5)
                if self._stop:
                    return
                if self._retire:
                    break
                if self.error is not None:
                    # A deck that failed is not written again; the input
                    # loop raises the error (check) and retires this.
                    self._pending = None
                    continue
            self.step()
        _close_quietly(self.deck)

    def retire(self):
        """No more frames: close the deck once the write in hand is done,
        on this worker's thread (or now, with no thread). Never waits."""
        with self._cv:
            self._retire = True
            self._pending = None
            self._cv.notify()
        if self._thread is None:
            _close_quietly(self.deck)

    def stop(self, timeout=1.0):
        """Tests only: end the thread without closing the deck."""
        with self._cv:
            self._stop = True
            self._cv.notify()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)


# --------------------------------------------------------------------------
# The real hardware loop. Everything above this line is unit-testable
# without a Stream Deck plugged in; everything below it is the glue that
# only makes sense against the real device, and is exercised on the bench,
# not in selftest.py.
# --------------------------------------------------------------------------
def run_forever(controller, deck_factory=Deck, journal=None, sleep=time.sleep,
                clock=time.monotonic, threaded=False):
    """Opens the real Stream Deck and runs until interrupted (Ctrl-C) or
    told to stop. Reconnects on DeckDisconnected. Every (re)connect runs
    controller.reset_on_reconnect(), which clears every hold-start
    timestamp and the last-seen key snapshot (round 3 of the safety
    review, item 2: those used to survive a disconnect, so a hold in
    progress when the deck unplugged could read as already complete,
    purely from wall-clock time passing during the outage, the instant it
    reconnected); so does the unplug itself. The arm socket is opened ONCE,
    at the first connect or the first no-deck frame, whichever comes
    first: that is the process start, the one moment every group begins
    OFF at counter 0.

    Ruling 4 (Jeff, 2026-10-12; purpose reset 2026-10-07): a deck unplug
    and replug KEEPS the arm state. While there is no deck hardware
    (before the first connect, and between a disconnect and the next
    one), this process keeps the arm socket open and keeps sending its
    CURRENT wanted vector at ARM_SEND_HZ with the counter advancing
    (_hold_link_steady), and a reconnect keeps `wanted` and the counter
    too. Flamesafe sees an input that is alive and unchanged: nothing
    disarms. The engine is told (the "deck" alert, _DeckOutage: booth
    note, rack-screen banner, Acknowledge or Disarm all, no timer) and the
    operator decides. Round 4 of the safety review, item A, is why the
    link stays on the air at all: going silent released flamesafe's
    sender lock after arm_stale_ms, and the round-4 review had a second
    local process take the free lock while the deck was unplugged and arm
    every group on the wire. Flamesafe still disarms within arm_stale_ms
    if this whole PROCESS dies, and the restarted process begins at 0
    (module docstring, failure mode 1: the accepted residual).

    Round 5 of the safety review, item 2: ANY Exception from
    deck_factory() or from a main-loop pass is handled as a disconnect,
    not only DeckDisconnected. Only DeckDisconnected used to be caught, so
    a plain OSError from hidapi on a reconnect ended this function (and
    the process) after one hold-off, the link went silent, and the
    sender lock lapsed after all. Each kind of failure is journaled once
    per outage (_DeckOutage), not once per 2 s retry. Only a deliberate
    stop ends the loop: KeyboardInterrupt and SystemExit are not
    Exceptions and pass straight through.

    threaded=True (what `ltc deck` runs, 2026-10-21; see ArmPacer): the
    arm assertion goes out from ArmPacer's own thread at ARM_SEND_HZ while
    this loop keeps beating, and the key images go to the deck from
    DisplayWorker's own thread, newest frame first. This loop reads the
    keys, runs the holds, Abort and disarm, and composes each frame; it
    never waits on a USB write. A deck write error comes back here
    (DisplayWorker.check) and is an unplug like any other. threaded=False
    is the single loop as it was: one arm send and one synchronous frame
    write per pass, for tests that step it on a fake clock.
"""
    pacer = None
    if threaded:
        # The arm link's own thread, for the life of this loop, across
        # every reconnect: it sends only while the loop below beats.
        pacer = ArmPacer(controller.arm, controller.names).start()
        controller.display_confirms = True
        pf = getattr(controller, "purge_fire", None)
        if pf is not None:
            # A held FIRE's heartbeats need this loop alive too
            # (DECK_STALL_RULING): a frozen loop cannot read the release.
            pf.alive = (lambda p=pacer: p.live(
                stale_s=PURGE_INPUT_STALE_S))
    try:
        return _run_forever(controller, deck_factory, journal, sleep, clock,
                            pacer)
    finally:
        if pacer is not None:
            pacer.stop()


def _run_forever(controller, deck_factory, journal, sleep, clock, pacer):
    """run_forever's loop. `pacer` None: the single loop (threaded=False)."""
    journal = journal or (lambda text, **kw: None)
    fonts = Fonts()
    drawn = None
    period = 1.0 / ARM_SEND_HZ
    cond = controller.conductor
    outage = _DeckOutage(
        journal, alert=getattr(cond, "alert", None),
        delivered=(lambda: cond.alert_delivered)
        if hasattr(cond, "alert_delivered") else None)
    if getattr(controller, "usb", None) is None:
        controller.usb = UsbMeter(
            getattr(controller, "_journal", None) or journal, clock=clock,
            echo=print if pacer is not None else (lambda text: None))
    meter = controller.usb
    last_draw = None
    while True:
        deck = None
        display = None
        try:
            deck = deck_factory()
            # Ruling 4 (Jeff, 2026-10-12): a hardware (re)connect keeps
            # `wanted` and the counter. Only a PROCESS start begins at 0,
            # and that is ArmSocket.open's doing, once, here or in
            # _hold_link_steady, whichever comes first.
            if not controller.arm.is_open:
                controller.arm.open()
            controller.reset_on_reconnect()
        except Exception as e:          # round 5, item 2: not only
            outage.failed(e, "Retrying in 2 s; every flame group stays as "
                             "it was on the arm link meanwhile.")
            _close_quietly(deck)
            _hold_link_steady(controller, sleep, 2.0, outage, pacer)
            continue
        if pacer is not None:
            display = DisplayWorker(deck, fonts.Image, meter=meter,
                                    clock=clock).start()
        outage.connected()
        controller.deck_present = True   # the Deck chip (remote.py)
        connected_at = clock()
        try:
            while True:
                t0 = clock()
                if display is not None:
                    # A key image write that failed (an unplug) ends the
                    # pass here, exactly like a key read that did.
                    display.check()
                    # A write that is only slow or stuck is shown (the
                    # Deck chip, one journal line per slow spell) and is
                    # never an unplug: DECK_STALL_RULING.
                    meter.check(clock())
                # Item 3: every key-state snapshot the device reported
                # since the last read, in order, not just the latest one
                # -- a quick tap-and-release between read cycles must not
                # vanish.
                moved = False
                for down in deck.keys_down():
                    controller.run_once(down)
                    moved = True
                # The remote page's holds and disarms (2026-10-03), as
                # remote presses of the group keys, before tick() advances
                # the holds. _hold_link_steady runs the same two calls
                # while the hardware is away, so the screen still works.
                controller.screen_pass()
                # Item 2 (round 2 of the safety review): tick() runs every
                # pass, unconditionally -- even when deck.keys_down() above
                # returned nothing, which it will if the real hardware
                # reports a key's state only on CHANGE and a held key
                # produces no further snapshots until release.
                controller.tick()
                controller.status.poll(clock)
                controller.check_links()
                if pacer is not None:
                    # The arm link's own thread sends; this loop says it
                    # is alive (ArmPacer). A frozen loop stops the beat.
                    pacer.beat()
                else:
                    controller.arm.send(controller.names)
                if display is not None:
                    controller.frame_shown(display.last_shown())
                meter.check(clock())
                # What moves on the keys comes from evidence of life, not
                # this loop's clock (see CHASE_STEP_S): the snakes and the
                # bottom row's flashing from flamesafe's status frames,
                # the top row's flashing from the engine's answers.
                motion = (controller.marquee_step(), controller.engine_blink(),
                          controller.status_blink())
                if moved or controller.animating() or last_draw is None or \
                        t0 - last_draw >= DRAW_IDLE_S or motion != drawn:
                    last_draw, drawn = t0, motion
                    _paint(controller, deck, display, fonts, motion)
                outage.ran_clean(clock() - connected_at)
                # The rest of the pass: flamesafe's frames are read every
                # FRAME_S (display only), and the keys redrawn each time
                # something on them changed: a marquee step, a flash, or a
                # hold filling. A hold's ring follows this deck's own clock,
                # since it is the operator's own press. Key reads, holds and
                # the arm link's send (or beat) stay once per pass.
                while True:
                    left = period - (clock() - t0)
                    if left <= FRAME_S:
                        if left > 0:
                            sleep(left)
                        break
                    sleep(FRAME_S)
                    controller.status.poll(clock=clock)
                    motion = (controller.marquee_step(),
                              controller.engine_blink(),
                              controller.status_blink())
                    if motion != drawn or controller.animating():
                        last_draw, drawn = clock(), motion
                        _paint(controller, deck, display, fonts, motion)
        except Exception as e:          # round 5, item 2: not only
            outage.failed(e, "Every flame group stays as it was on the arm "
                             "link until it reconnects; the alert on the "
                             "rack screen waits for an Acknowledge.")
            controller.deck_present = False  # the Deck chip (remote.py)
            # NOT arm.close() (round 4, item A): see _hold_link_steady.
            # NOT arm.set_all(False) either (ruling 4, 2026-10-12): the
            # groups stay as they are. The holds in progress are dropped
            # now, so none can finish off the wall clock during the gap.
            controller.reset_on_reconnect()
            try:
                controller.arm.send(controller.names)
            except Exception:
                pass
            if display is not None:
                # Closed on the display's own thread once the write in
                # hand is done: a stalled USB write never holds up this
                # loop, so the outage loop below beats at once.
                display.retire()
            else:
                _close_quietly(deck)
            _hold_link_steady(controller, sleep, 1.0, outage, pacer)


def _paint(controller, deck, display, fonts, motion):
    """One frame: composed here, on the loop that reads the keys; written
    to the deck here (threaded=False) or handed to the display worker,
    which writes the newest frame on its own thread."""
    if display is None:
        _draw_deck(controller, deck, fonts, motion)
        return
    chase, blink_on, group_blink = motion
    img = controller.draw(fonts, blink_on, chase, group_blink)
    display.submit(controller.frame_id, img)


def _draw_deck(controller, deck, fonts, motion):
    """One frame onto the deck; Deck.set_key sends only the keys whose
    image changed. `motion` is (marquee step, top-row flash phase,
    bottom-row flash phase)."""
    chase, blink_on, group_blink = motion
    img = controller.draw(fonts, blink_on, chase, group_blink)
    meter = getattr(controller, "usb", None)
    t0 = time.monotonic()
    for k in range(6):
        ox, oy = key_origin(k)
        deck.set_key(fonts.Image, k, img.crop((ox, oy, ox + K, oy + K)))
    if meter is not None:
        meter.record(time.monotonic() - t0)


# A deck that has run this long without a failure ends an outage: the next
# failure after that is journaled again (_DeckOutage).
DECK_STABLE_S = 10.0
# The "deck" alert's sentences (alerts.py), as the rack screen shows them.
DECK_LOST_TEXT = ("The Stream Deck is unplugged or not answering. Every "
                  "flame group stays as it was. Press Acknowledge once you "
                  "have seen it, or Disarm all.")
DECK_BACK_TEXT = ("The Stream Deck is back. Every flame group stayed as it "
                  "was while it was away. Press Acknowledge once you have "
                  "seen it.")
# Before the FIRST connect of this process (Jeff, 2026-10-17: a deck that
# never connected has to say what it looked for and what it found): the
# first failure's own words, once per outage, never per 2 s retry.
DECK_NEVER_TEXT = ("No Stream Deck has answered since the Stream Deck "
                   "program started: {why}. Every flame group stays OFF. "
                   "Plug in the Stream Deck Mini, then press Acknowledge.")


class _DeckOutage:
    """run_forever's journal bookkeeping (round 5 of the safety review,
    item 2). An outage starts at the first failure (a disconnect, or any
    other exception from opening the deck or from a main-loop pass) and
    ends once a deck has run DECK_STABLE_S without one. Inside an outage
    each KIND of failure is journaled once, with fault=True, and only the
    first reconnect is journaled; the rest are counted and the total goes
    on one line when the outage ends. Before this a missing deck wrote a
    line every 2 s retry, and a deck that failed on every pass would have
    written two lines a second."""

    def __init__(self, journal, alert=None, delivered=None):
        self._journal = journal
        self._alert = alert          # EngineConductor.alert, or None
        # EngineConductor's alert_delivered: whether the engine answered
        # 200 to the last alert post. None: no way to know, so no retry.
        self._delivered = delivered
        self.active = False
        self._kinds = set()
        self._unlogged = 0
        self._reconnect_logged = False
        self._alert_text = None      # the sentence last posted
        self._ever_connected = False
        self._never_text = None      # the never-connected sentence, once

    def _alert_landed(self):
        if self._delivered is None:
            return True
        try:
            return self._delivered() is True
        except Exception:
            return True

    def _post_alert(self, text):
        """The "deck" alert (alerts.py): the sentence the engine should
        show. Posted when it changes, and posted AGAIN on every failed()
        while the engine has not answered 200 to it (the engine down, or
        the post queue full, when the deck process starts first: review
        of PR #59). Never raises."""
        if self._alert is None:
            return
        if text == self._alert_text and self._alert_landed():
            return
        self._alert_text = text
        try:
            self._alert({"raise": "deck", "text": text,
                         "options": ["Acknowledge", "Disarm all"]})
        except Exception:
            pass

    def screen_failed(self, e):
        """The no-deck loop's screen pass or tick raised: journaled once
        per outage, per kind of error; the arm send went out regardless
        (_hold_link_steady)."""
        kind = "screen:" + type(e).__name__
        if not self.active:
            self.active = True
            self._kinds = set()
            self._unlogged = 0
            self._reconnect_logged = False
        if kind in self._kinds:
            self._unlogged += 1
            return
        self._kinds.add(kind)
        _journal_quietly(self._journal,
                         f"Stream Deck: the screen's holds could not be read "
                         f"while the deck is away ({type(e).__name__}: {e}). "
                         f"The arm link carries on unchanged. This line does "
                         f"not repeat until the deck has run "
                         f"{DECK_STABLE_S:g} s without a failure.",
                         fault=True, action="deck")

    def failed(self, e, tail):
        if not self.active:
            self.active = True
            self._kinds = set()
            self._unlogged = 0
            self._reconnect_logged = False
        if self._ever_connected:
            self._post_alert(DECK_LOST_TEXT)
        else:
            if self._never_text is None:
                why = " ".join(str(e).split()) or type(e).__name__
                self._never_text = DECK_NEVER_TEXT.format(why=why[:400])
            self._post_alert(self._never_text)
        kind = ("unplugged" if isinstance(e, DeckDisconnected)
                else type(e).__name__)
        if kind in self._kinds:
            self._unlogged += 1
            return
        self._kinds.add(kind)
        if isinstance(e, DeckDisconnected):
            text = f"Stream Deck: {e}. {tail}"
        else:
            text = (f"Stream Deck: unexpected error ({type(e).__name__}: "
                    f"{e}), handled exactly like an unplugged deck. {tail}")
        _journal_quietly(self._journal,
                         f"{text} This line does not repeat until the deck "
                         f"has run {DECK_STABLE_S:g} s without a failure.",
                         fault=True, action="deck")

    def connected(self):
        self._ever_connected = True
        if self.active:
            if self._reconnect_logged:
                self._unlogged += 1
                return
            self._reconnect_logged = True
            self._post_alert(DECK_BACK_TEXT)
        _journal_quietly(self._journal,
                         "Stream Deck connected. Every flame group stays as "
                         "it was on the arm link (ruling 4, 2026-10-12); a "
                         "hold that was in progress is dropped and has to "
                         "be pressed again.", action="deck")

    def ran_clean(self, secs):
        if not self.active or secs < DECK_STABLE_S:
            return
        self.active = False
        if self._unlogged:
            _journal_quietly(
                self._journal,
                f"Stream Deck: running cleanly for {DECK_STABLE_S:g} s. "
                f"{self._unlogged} more failure(s) or reconnect(s) during "
                f"that outage were not logged one by one.", action="deck")


def _journal_quietly(journal, text, **kw):
    try:
        journal(text, **kw)
    except Exception:
        pass


def _close_quietly(deck):
    if deck is None:
        return
    try:
        deck.close()
    except Exception:
        pass


def _hold_link_steady(controller, sleep, secs, outage=None, pacer=None):
    """No deck hardware: keep sending the CURRENT wanted vector at
    ARM_SEND_HZ for about `secs`, on the arm socket this process already
    holds flamesafe's sender lock with (opening it the first time, which
    is the one moment every group starts OFF: a process start), so the
    lock never lapses while the deck is away (round 4 of the safety
    review, item A) and nothing on the rig changes (Jeff, 2026-10-07:
    "Stream Deck dying mid-show must NOT disarm flames by itself"; ruling
    4, 2026-10-12). Flamesafe sees an input that is alive and unchanged.
    The remote page's holds and disarms keep working meanwhile
    (screen_pass and tick, as in the main loop), so a group can still be
    armed or disarmed from the rack screen with the hardware away.
    Counted in sends, not wall-clock time, so a test's fake sleep cannot
    spin it forever. Round 5, item 2: never raises an Exception either; a
    socket that could not be opened is tried again on the next frame.
    With `pacer` (run_forever's threaded mode) this loop beats it instead
    of sending: the pacer's own thread sends, as while the deck is here."""
    for _ in range(max(1, int(round(secs * ARM_SEND_HZ)))):
        # Three steps, each in its own try: a persistent exception from the
        # screen pass or the tick must never skip the send below. If it
        # did, the link would go silent, flamesafe would disarm every
        # group within arm_stale_ms and the sender lock would lapse (the
        # round-4 item A hazard, found again in review of PR #59). The
        # exception is journaled once per outage (_DeckOutage), never
        # raised.
        try:
            if not controller.arm.is_open:
                controller.arm.open()
        except Exception:
            pass
        try:
            controller.screen_pass()
            controller.tick()
        except Exception as e:
            if outage is not None:
                outage.screen_failed(e)
        try:
            if pacer is not None:
                pacer.beat()
            else:
                controller.arm.send(controller.names)
        except Exception:
            pass
        sleep(1.0 / ARM_SEND_HZ)


def load_flamesafe_link(path):
    """The handful of flamesafe config fields this module needs, read as
    plain JSON (never flamesafe.config: this module must not import
    flamesafe). Returns (arm_ip, arm_port, status_ip, status_port, key,
    group_names). Raises ValueError with a plain sentence on anything
    wrong, including no groups at all or no arm link
    configured at all."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    link = doc.get("link") or {}
    if "arm_port" not in link:
        raise ValueError(f"{path} has no link.arm_port: this flamesafe "
                         f"config has not been set up for a real Stream "
                         f"Deck yet (see flamesafe/CONTRACT.md, the arm "
                         f"link).")
    names = [g.get("name") for g in (doc.get("groups") or [])]
    if not all(isinstance(n, str) for n in names):
        raise ValueError(f"{path}: every group needs a name.")
    if not names:
        raise ValueError(f"{path} configures no flamesafe groups, so the "
                         f"Stream Deck has nothing to arm.")
    return (link.get("arm_ip", link.get("listen_ip", "127.0.0.1")),
            int(link["arm_port"]), link.get("status_ip", "127.0.0.1"),
            int(link["status_port"]), link["key"], names)


def deck_layout_at_start(names, folder, journal, settings_folder=None):
    """(key_order, labels, ok) for `ltc deck` at start: ltcplay_deck.json in
    `folder` (None: the settings folder), and every note about it, the
    settings folder not found, and the key map itself, journaled (review
    of PR #120, P2). `ok` False: the default layout stands in for a file
    that could not be used, which the Deck chip says too."""
    notes = []
    if not folder:
        try:
            if settings_folder is None:
                from . import remote as remote_mod
                settings_folder = remote_mod.settings_folder
            folder = settings_folder()
        except Exception as e:                   # noqa: BLE001
            notes.append(f"the settings folder could not be found "
                         f"({type(e).__name__}: {e}), so {DECK_CONFIG_FILE} "
                         f"was not read and the deck uses its default "
                         f"layout.")
    order, labels, more, ok = load_deck_layout(names, folder)
    notes += more
    for t in _case_twins(list(names)):
        notes.append(f"flamesafe has a group {t!r} whose name differs from "
                     f"an earlier group's only by case. They are two "
                     f"groups: only the first gets the deck's own label "
                     f"and key by name, and {t!r} keeps its own name.")
    ok = ok and bool(folder)
    notes.append(f"the keys are key 0 Start/Stop, "
                 f"{_keys_text(order, labels, list(names))}; key 5 opens "
                 f"the announcements page. "
                 f"Unless {DECK_CONFIG_FILE} says otherwise, front row, "
                 f"cat-walk, wave flamer and fireball go on keys 1 to 4 by "
                 f"name and every other group follows in flamesafe's order "
                 f"on the keys left over (a key one of those four does not "
                 f"use goes to the next group; past key 4, Rack screen "
                 f"only). The deck has no Hold "
                 f"or Abort key (2026-10-26): Hold and Abort are on the "
                 f"Rack screen.")
    for note in notes:
        try:
            journal(f"Stream Deck: {note}", action="config",
                    outcome="layout" if ok else "layout-default")
        except Exception:                        # noqa: BLE001
            pass
    return order, labels, ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        prog="ltc deck", description="Run the real Stream Deck: arms and "
        "disarms real flamesafe groups, and reaches Start Now, Hold and "
        "Abort through a show conductor where one is connected.")
    ap.add_argument("--flamesafe-config", required=True,
                    help="the flamesafe config this deck talks to "
                    "(its link.arm_port, link.key and group names)")
    ap.add_argument("--ltcplay-url", default="http://127.0.0.1:7878",
                    help="ltcplay's own local web server (for the chosen "
                    "operator and the show's state); default "
                    "http://127.0.0.1:7878 (item 6, round 2 of the safety "
                    "review: this used to default to 8080, which is not "
                    "the port `ltc serve` or any launcher actually runs "
                    "on, so the operator gate and journal routing silently "
                    "never reached the real server by default)")
    ap.add_argument("--deck-folder", default=None,
                    help="where ltcplay_deck.json (which flame group is on "
                    "which key, and its label) is read from; default the "
                    "settings folder")
    ap.add_argument("--audit-folder", default=None,
                    help="where this deck's audit day files go (audit.py); "
                    "default logs/audit in the settings folder")
    args = ap.parse_args(argv)
    # Second-copy guard (2026-10-03, onlyone.py): one deck process per
    # machine. A second `ltc deck` would be a second sender on the arm link:
    # the round-4 veto would refuse every cycle while both ran, and the two
    # would fight over the hardware.
    from . import onlyone
    try:
        only = onlyone.only_copy(
            onlyone.DECK_LOCK,
            f"ltc deck for {os.path.abspath(args.flamesafe_config)}, "
            f"started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    except onlyone.AlreadyRunning as e:
        print(f"error: {onlyone.refusal(onlyone.DECK_LOCK, e.holder)}")
        return 2
    try:
        return _main(args)
    finally:
        only.release()


def _main(args):
    try:
        arm_ip, arm_port, status_ip, status_port, key, names = \
            load_flamesafe_link(args.flamesafe_config)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}")
        return 2
    # Item 5: the operator/show-running lookup runs on its OWN background
    # thread from here on; start() begins it before anything reads it.
    sched = LocalSchedule(args.ltcplay_url)
    sched.start()
    # Item 9: every arm/disarm/Abort/refusal line also reaches ltc serve's
    # real, persistent night journal (schedule_service.py's Logbook), on
    # ITS own background thread, in addition to this process's console.
    journal = DeckJournal(args.ltcplay_url)
    arm = ArmSocket(arm_ip, arm_port, key, len(names), journal=journal)
    status = StatusSocket(status_ip, status_port, key)
    status.open()

    # PR #43 review, finding 8: Abort, Hold, Resume and Reset reach the
    # engine's own show conductor over loopback, on background threads.
    engine = EngineConductor(args.ltcplay_url, journal=journal)
    engine.start()
    # The engine's deck input, read on its own background thread
    # (ScreenKeys), never on the main loop: the remote page's holds and
    # disarms, and the open alerts (alerts.py: the ACK key). Always made;
    # the holds inside it still count only while ltcplay_remote.json
    # switches screen arming on (the engine answers enabled false
    # otherwise, and ScreenKeys.holds() is empty), so an alert can reach
    # the keys with screen arming off.
    screen = ScreenKeys(args.ltcplay_url).start()
    # The audit log (audit.py): every key press and release, and every
    # action's outcome, in this process's own day files, written on a
    # thread of its own. A log that cannot be written loses lines, never
    # a press.
    audit_log = None
    try:
        from . import audit as audit_mod
        from . import remote as remote_mod
        audit_log = audit_mod.AuditLog(
            args.audit_folder or audit_mod.default_folder(
                remote_mod.settings_folder()), "deck").start()
    except Exception as e:                       # noqa: BLE001
        print(f"Stream Deck: the audit log is off: {type(e).__name__}: {e}")
    # Which group is on which key, and what each key says (Jeff,
    # 2026-10-26; ltcplay_deck.json, DECK_CONFIG_FILE). Whatever does not
    # fit is said here and in the journal; the deck still starts.
    key_order, labels, layout_ok = deck_layout_at_start(
        names, getattr(args, "deck_folder", None), journal)
    controller = Controller(arm, status, names, key_order=key_order,
                            labels=labels,
                            operator_provider=sched.current_operator,
                            show_running_provider=sched.show_running,
                            conductor=engine, journal=journal,
                            screen=screen,
                            audit=(audit_log.event if audit_log is not None
                                   else None),
                            held_provider=sched.held,
                            run_live_provider=sched.run_live)
    # The deck's worst USB key image write rides along on every deck-input
    # read, for the rack screen's Deck chip (2026-10-21). Display only.
    controller.usb = UsbMeter(journal)
    screen.report = controller.usb.worst_ms
    screen.report_failed = controller.usb.failed_count
    # And whether the USB deck is here, and how fresh the arm link is: the
    # Deck chip is green only on both (2026-10-26). Display only.
    screen.report_present = lambda: controller.deck_present
    screen.report_arm = arm.sent_age_ms
    # And each group's re-arm wait and any purge arm refused for it, so the
    # rack screen counts the wait down in words (2026-10-26). Display only.
    screen.report_rearm = controller.rearm_report
    # And whether ltcplay_deck.json was set aside for the default layout,
    # for the Deck chip (review of PR #120, P2-1). Display only.
    screen.report_layout_default = lambda: not layout_ok
    # And which groups have no key, for the Rack screen's flame card.
    screen.report_nokey = controller.nokey
    # The Morning page's purge (2026-10-17): the FIRE key's heartbeats go
    # to the engine on their own thread, through the same POST the presses
    # use. Nothing here touches the arm link for it.
    controller.purge_fire = PurgeFire(engine._http_post, journal=journal)
    print(f"Stream Deck: arming {', '.join(names)} over {arm_ip}:{arm_port}, "
         f"reading flamesafe's status on {status_ip}:{status_port}. Abort, "
         f"Hold, Resume and Reset go to the show conductor in ltc serve at "
         f"{args.ltcplay_url}; Start Now journals a refusal. "
         f"{'Screen arming is ON (ltcplay_remote.json).' if screen else 'Screen arming is off.'}"
         f" Ctrl-C to stop.")
    try:
        # threaded: the arm link and the key images each on a thread of
        # their own, never one waiting on the other (2026-10-21; ArmPacer).
        run_forever(controller, journal=journal, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        if controller.purge_fire is not None:
            controller.purge_fire.stop()
            controller.purge_fire.join(1.0)   # its purge-stop goes out
        arm.close()
        status.close()
        sched.stop()
        engine.stop()
        if screen is not None:
            screen.stop()
        if audit_log is not None:
            audit_log.flush(1.0)
            audit_log.stop(1.0)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
