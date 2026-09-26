"""flamesafe rules: the wire constants and the safety rules, in one place.

Ported from flamesafe.py rev 5 (flamepanel_rev6), which was written for the
rev 6 toggle panel and audited over six rounds.  Rev 9 dropped the toggles,
the ESP32 panel and the wire reader; the Stream Deck (build step 7b) becomes
the arm input.  Every rule the rev 5 header enforces still applies, at the
same strictness.  The rules, and the reason each exists:

  1. THE ARM VALUE IS DERIVED, NOT CHOSEN.  It must sit inside the configured
     G-Flame safety window, inside the Showven enable window, below every
     sourced fire threshold, and every single-bit neighbour of it must also
     be below the G-Flame fire threshold.  A value above the UNSOURCED
     Showven threshold is refused unless the risk is acknowledged in the
     config.  ANDY DECIDES the range; this module only refuses a wrong pair.

  2. THE RISING EDGE MUST BE CLEAN.  G-Flame 4.7.2: the safety condition is
     fulfilled only if the flame channel is below 6% during the rising edge
     of the safety channel.  Arming while a flame channel is high does not
     merely fail to fire, IT FAILS TO ARM, and nothing on the wire says so.
     So the safety slot is never raised while any of that group's fire
     slots is at or above GFLAME_EDGE_BELOW, and the refusal is reported.

  3. THE EDGE IS HELD QUIET FOR EDGE_QUIET_FRAMES.  The node clocks DMX out
     of its own buffer at its own rate, so the frame it emits during the
     transition may not be the frame we checked.  The group's fire slots are
     held at zero for EDGE_QUIET_FRAMES ticks from the rise.

  4. THE RE-ARM DWELL.  A Showven reads every departure from its 50-200
     enable window as an emergency stop and depressurises.  After an
     operator disarm, that group is not raised again for min_arm_dwell_ms.
     Lowering is never delayed.  Cycling the arm during the dwell restarts it.

  5. CHATTER DETECTION.  A single transient stays cheap.  A slot that would
     rise more than CHATTER_RISES times inside CHATTER_WINDOW_MS has that
     rise refused, is held for the dwell, and says so, whatever caused the
     chatter.  (Rev 5 let the chattering rise out and only delayed the next
     one; refusing it is stricter.)  The hold ends by itself once the window
     has slid past: only rises that went out are counted.

  6. CONSENT.  A group arms only after the arm input has been seen, while
     PROVEN ALIVE, asking for that group to be disarmed, and then asking for
     it to be armed.  An input that boots up already asking for arm is not
     consent, and neither is one that was asking before an interruption.

  7. INTERRUPTIONS CLEAR THE LATCHES.  A gap in the arm input longer than
     arm_stale_ms, a liveness counter that stalls for longer than that, a
     counter that goes backwards (an input reboot), and this program's own
     tick overrunning all clear every latch, so nothing re-arms on its own
     when the input comes back.  The operator cycles the arm.

  8. NO TWO GROUPS SHARE A FIRE SLOT, no fire slot is a safety slot, no
     group is without fire slots, and every slot is inside one universe.

  9. ONLY THE WRITER.  This program alone writes the flame universe, at sACN
     priority 200.  Every channel that belongs to no group is always zero.
     A group's fire slots carry ltcplay's values only while that group's
     safety slot carries the arm value on the same frame and the edge-quiet
     window has passed.  A disarmed group never emits a fire value; a fire
     value commanded on a disarmed group is refused and logged as a fault.

 10. ZERO ON ANYTHING UNCERTAIN.  Startup, a stale frame from ltcplay, a
     stale or stalled arm input, a config error, this program's own overrun
     and any exception inside compose all produce zeros.  compose() never
     raises.

NOT AN INTERLOCK.  The certified E-stop and the gas valve are the interlocks.
This raises the floor under an operator's confidence.  Galaxis themselves
write that DMX is considered unsafe because it carries no checksum and that
the user bears the risk.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# WIRE CONSTANTS.  Facts read out of vendor manuals, not preferences.
# Changing any of these without re-reading both manuals is a safety change.
# ---------------------------------------------------------------------------

# Galaxis G-Flame V2.43, section 17.2.2.  FIVE fixed options, nothing between.
GFLAME_SAFETY_RANGES = {
    "30-50%": (76, 120),
    "40-60%": (102, 153),
    "50-70%": (127, 178),
    "60-80%": (153, 204),
    "70-90%": (178, 229),
}

# G-Flame, section 4.7.2.  SOURCED.
GFLAME_FIRE_AT = 229          # "at least 90% (at least decimal 229)"

# The manual gives two different numbers for the same 6%, eight lines apart:
#   edge precondition  "below 6% (below decimal 15)"
#   re-trigger         "below 6% (below decimal 16)"
# We enforce the edge precondition, so we take the stricter of the two.  A
# flame slot at exactly 15 blocks an arm.  Being one count too strict costs
# nothing; being one count too permissive produces a green lamp over a head
# that silently refused to arm, which is the exact failure this module exists
# to prevent.
GFLAME_EDGE_BELOW = 15        # strict: flame slot must be < 15 on the edge
GFLAME_REARM_BELOW = 16       # observed head behaviour, not a gate we enforce

# Showven.  SOURCED, from Circle Flamer II (2024-11-21):
#   CH6 "0~49 and 201~255: Firing Disable (Emergency STOP) / 50~200: Firing
#   Enable"   -> the enable window
#   CH3 "0-253: Firing OFF / 254-255: Firing ON"   -> this model's fire value
SHOWVEN_ENABLE_LO = 50
SHOWVEN_ENABLE_HI = 200
SHOWVEN_CF2_FIRE_AT = 254

# Circle Flamer II CH5: "0-2 no preset sequence, 3-255 preset sequence".
# There is no usable arm value below 3, so NO ARM VALUE IS INERT ON THIS
# CHANNEL.  The defence against a misaddressed head is the fixture table,
# the exclusive-arm walk and the content lock.  It is not the value.
SHOWVEN_CF2_SEQUENCE_FIRE_AT = 3

# G-Flame section 15, the DMX512 row: the 'Attention armed' warning LED lights
# "As soon as the safety channel is received with values between 60% and 80%."
GFLAME_WARNING_LED_LO = 153       # 60% of 255
GFLAME_WARNING_LED_HI = 204       # 80% of 255

# ASSUMED, NOT SOURCED.  Rev 2 cited "uFlamer 2CH-P fires at 111".  That figure
# is in NEITHER supplied manual.  It is kept because it is the conservative
# direction.  DO NOT quote this as a manual fact.
SHOWVEN_ASSUMED_LOWEST_FIRE_AT = 111

# The G-Flame range the design has carried since rev 2.  ANDY DECIDES; see
# ARM_OPTIONS.  A config may name the other validated option and must then
# acknowledge the unsourced risk in writing (accept_unsourced_risk).
DEFAULT_GFLAME_RANGE = "30-50%"

# One DMX512 universe.  This program owns exactly one, and every slot in the
# group table is numbered 1..512 inside it.
UNIVERSE_SIZE = 512

# The two defensible arm values, one per validated range.  The derivation is
# in flamesafe.py rev 5 and in the flame panel spec section 2b: 78 is optimal
# under both the sourced constraints and the unsourced one; 157 lights the
# G-Flame warning LED and sits above the unsourced Showven threshold.
# (arm value, does this option sit above the unsourced Showven threshold)
ARM_OPTIONS = {
    "30-50%": (78, False),
    "60-80%": (157, True),
}
DISARM_VALUE = 0

# How many ticks to hold a group's fire slots at zero from the rise of its
# safety slot.  See rule 3.
EDGE_QUIET_FRAMES = 3

# Chatter detection.  See rule 5.
CHATTER_RISES = 3
CHATTER_WINDOW_MS = 2000

# sACN (ANSI E1.31) priority.  200 is the maximum.  A stray source at the
# default 100 loses at the PixLite.  This is prevention, not detection: the
# wire reader is gone, so nothing in the system can see a foreign value.
SACN_PRIORITY = 200
SACN_PORT = 5568


def required_head_settings(gflame_range=None):
    """The per-unit menu settings that MUST be made at load-in, with the
    manual line that requires each.  Not preferences: every one is a vendor
    default that is wrong for this installation, or a vendor requirement.
    The commissioning card is generated from here so the document and the
    code cannot drift apart."""
    gflame_range = gflame_range or DEFAULT_GFLAME_RANGE
    return {
        "G-Flame": [
            ("Safety channel range", gflame_range,
             "17.2.2. Any other range puts the arm value outside ITS window. "
             "Under 30-50% no other range arms at all; under 60-80% a head "
             "mis-set to 50-70% would also arm, which the exclusive-arm walk "
             "is what catches."),
            ("Flame monitoring", "On",
             "17.1.7. 'For the reasons of safety, you should generally enable "
             "the ionization measurement.' Closes the valves if no flame is "
             "detected for more than one second."),
            ("Max. Flame Duration", "set to the longest cue plus margin, never ----",
             "17.3. The factory setting is '----', which the manual defines as "
             "'no time limitation will take place'. This is the ONLY bound on "
             "a flame if DMX stops while the flame slot is high."),
            ("Number of Allowed Misfirings", "set, not unlimited",
             "17.1.9. Locks the unit out after N misfires rather than venting "
             "unburned fuel repeatedly."),
            ("Warning LED 'Attention armed'", "On",
             "Section 15. The LED lights only 'providing that the warning LED "
             "has been activated in the menu', and only for a safety value "
             "between 60% and 80%."),
            ("RDM on this universe", "off at the source",
             "4.7.3. 'Operation of the device via DMX is not possible' while "
             "RDM packets are present."),
            ("Universe", "exclusive to flame effects, zeros on unused channels",
             "Galaxis require this in writing."),
        ],
        "Showven": [
            ("Flame Monitor", "ON",
             "Advanced menu. THE FACTORY DEFAULT IS OFF."),
            ("Safety key switch", "USER MODE",
             "TEST MODE raises 'E0 Test Mode' and the unit will not pressurise."),
            ("External Trigger", "OFF",
             "Advanced menu, default OFF. ON accepts a 9-60V pyro signal that "
             "bypasses everything in this design."),
            ("ARM State", "ON",
             "Advanced menu. Leaves the front ARM indicator working, which is "
             "what the commissioning walk reads."),
        ],
    }


REQUIRED_HEAD_SETTINGS = required_head_settings()
