"""flamesafe config: one JSON file, validated with every check rev 5 made.

Anything invalid refuses to start with one plain sentence.  ConfigError is
raised at load only; nothing in the composing path raises it.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from . import rules
from .link import EXAMPLE_KEY, KEY_MAX, KEY_MIN, valid_key

CONFIG_FORMAT = 1

# Every key a config may carry.  An unknown key is refused: a misspelt
# optional key (listen_ip, log_dir, a group's arm_value) would otherwise be
# ignored in silence and the program would run on a default the operator
# thought they had changed.
TOP_KEYS = {"flamesafe_config", "confirmed", "note", "universe",
            "destination", "link", "gflame_range", "arm_value",
            "accept_unsourced_risk", "min_arm_dwell_ms", "arm_stale_ms",
            "frame_stale_ms", "fire_hold_ms", "tick_hz", "overrun_ms",
            "log_dir", "groups"}
DESTINATION_KEYS = {"ip", "port"}
LINK_KEYS = {"listen_ip", "listen_port", "status_ip", "status_port", "key"}
GROUP_KEYS = {"name", "safety", "fire", "arm_value"}


def _only_known(d, allowed, where):
    unknown = sorted(k for k in d if k not in allowed)
    if unknown:
        raise ConfigError(f"{where} has a key this program does not know: "
                          f"{', '.join(unknown)}. Check the spelling against "
                          f"flamesafe.example.json.")

# Bounds on the timing knobs.  The lower bounds stop a config from making the
# program deaf to its own liveness; the upper bounds stop one from letting a
# dead input hold an arm for longer than a person would notice.
TICK_HZ_MIN, TICK_HZ_MAX = 10, 50
STALE_MS_MIN, STALE_MS_MAX = 100, 2500
# The re-arm dwell is 1 second (flame panel spec 7a).  The loader cannot set
# it shorter; tests that need a shorter one set Config.min_arm_dwell_ms
# directly, through a clearly named test-only path, never through a file.
DWELL_MS_MIN, DWELL_MS_MAX = 1000, 10000
OVERRUN_MS_MIN, OVERRUN_MS_MAX = 50, 2500
FIRE_HOLD_MS_MAX = 2500
PORT_MIN, PORT_MAX = 1, 65535
NAME_MAX = 64


class ConfigError(Exception):
    """Raised at load only.  Its text is the sentence the operator reads."""


class Group:
    """One arm group: a safety slot and the fire slots it governs.

    There is no offset.  Every slot is stated explicitly because rev 1
    invented one and it was wrong for every real unit.  Slots are 1..512
    inside the flame universe.
    """

    __slots__ = ("name", "safety", "fire", "arm_value")

    def __init__(self, name, safety, fire, arm_value):
        self.name = str(name)
        self.safety = int(safety)
        self.fire = [int(f) for f in fire]
        self.arm_value = int(arm_value)

    def __repr__(self):
        return (f"<Group {self.name!r} safety={self.safety} fire={self.fire} "
                f"arm={self.arm_value}>")


class Config:
    """Validated settings.  Construct through load() or from_dict()."""

    __slots__ = ("universe", "destination_ip", "destination_port",
                 "link_listen_ip", "link_listen_port", "link_status_ip",
                 "link_status_port", "link_key", "gflame_range", "arm_value",
                 "accept_unsourced_risk", "min_arm_dwell_ms", "arm_stale_ms",
                 "frame_stale_ms", "fire_hold_ms", "tick_hz", "overrun_ms",
                 "groups", "confirmed", "note", "log_dir",
                 "lights_warning_led")

    def test_only_override_dwell_ms(self, ms):
        """FOR TESTS ONLY.  The file loader floors the dwell at 1000 ms; the
        chatter and property tests need a shorter one to exercise the
        rules.  Nothing in the program calls this."""
        self.min_arm_dwell_ms = int(ms)

    @property
    def n(self):
        return len(self.groups)

    @property
    def tick_period_s(self):
        return 1.0 / self.tick_hz

    def all_slots(self):
        return ([g.safety for g in self.groups]
                + [f for g in self.groups for f in g.fire])


def _int(d, key, lo, hi, what=None):
    what = what or key
    if key not in d:
        raise ConfigError(f"{what} is missing from the config.")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{what} must be a whole number, not {v!r}.")
    if not (lo <= v <= hi):
        raise ConfigError(f"{what} is {v}; it must be between {lo} and {hi}.")
    return v


def _bool(d, key, default=None):
    if key not in d:
        if default is None:
            raise ConfigError(f"{key} is missing from the config.")
        return default
    v = d[key]
    if not isinstance(v, bool):
        raise ConfigError(f"{key} must be true or false, not {v!r}.")
    return v


def _ip(text, what, loopback_only=False):
    try:
        ip = ipaddress.ip_address(str(text))
    except ValueError:
        raise ConfigError(f"{what} {text!r} is not an IP address.") from None
    if loopback_only and not ip.is_loopback:
        raise ConfigError(f"{what} must be a loopback address (127.x.x.x); "
                          f"the link to ltcplay never leaves this machine.")
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved \
            or str(ip) == "255.255.255.255":
        raise ConfigError(f"{what} {text!r} is not a unicast address. The "
                          f"flame universe goes to one node, by unicast.")
    return str(ip)


def _validate_arm_value(arm_value, gflame_range, accept_unsourced_risk, who):
    """Every constraint rev 5 put on the arm value, for one group."""
    if gflame_range not in rules.GFLAME_SAFETY_RANGES:
        raise ConfigError(f"gflame_range {gflame_range!r} is not one of the "
                          f"five ranges in the G-Flame manual: "
                          f"{sorted(rules.GFLAME_SAFETY_RANGES)}.")
    if gflame_range not in rules.ARM_OPTIONS:
        raise ConfigError(
            f"there is no validated arm value for the G-Flame range "
            f"{gflame_range!r}; the validated options are "
            f"{sorted(rules.ARM_OPTIONS)}. Adding a range means re-deriving "
            f"the value, not guessing one.")
    derived, above_unsourced = rules.ARM_OPTIONS[gflame_range]
    lo, hi = rules.GFLAME_SAFETY_RANGES[gflame_range]
    if not (lo <= arm_value <= hi):
        raise ConfigError(
            f"{who}: arm value {arm_value} is outside the G-Flame "
            f"{gflame_range} window {lo}-{hi}. In this configuration no "
            f"G-Flame would ever arm.")
    if not (rules.SHOWVEN_ENABLE_LO <= arm_value <= rules.SHOWVEN_ENABLE_HI):
        raise ConfigError(f"{who}: arm value {arm_value} is not inside the "
                          f"Showven enable window; a Showven would read it "
                          f"as Firing Disable / Emergency STOP.")
    if arm_value >= rules.GFLAME_FIRE_AT:
        raise ConfigError(f"{who}: arm value {arm_value} would fire a G-Flame.")
    if arm_value >= rules.SHOWVEN_CF2_FIRE_AT:
        raise ConfigError(f"{who}: arm value {arm_value} would fire a "
                          f"Circle Flamer II.")
    for bit in range(8):
        neighbour = arm_value ^ (1 << bit)
        if neighbour >= rules.GFLAME_FIRE_AT:
            raise ConfigError(
                f"{who}: a single-bit flip of the arm value ({arm_value} -> "
                f"{neighbour}, bit {bit}) reaches the G-Flame fire threshold "
                f"{rules.GFLAME_FIRE_AT}.")
    if (arm_value >= rules.SHOWVEN_ASSUMED_LOWEST_FIRE_AT) != above_unsourced:
        raise ConfigError(
            f"ARM_OPTIONS says {gflame_range} is "
            f"{'above' if above_unsourced else 'below'} the unsourced "
            f"threshold and the value {arm_value} says otherwise.")
    if above_unsourced and not accept_unsourced_risk:
        raise ConfigError(
            f"{who}: arm value {arm_value} is at or above the UNSOURCED "
            f"Showven fire threshold {rules.SHOWVEN_ASSUMED_LOWEST_FIRE_AT}. "
            f"That may be the right call (it is what lights the G-Flame "
            f"warning LED); set accept_unsourced_risk to true to say so "
            f"deliberately.")
    # Last, because every check above is a physical fact about the value and
    # this one is bookkeeping: the value must be the one the table derives.
    if arm_value != derived:
        raise ConfigError(
            f"{who}: arm value {arm_value} is not the validated value "
            f"{derived} for the G-Flame range {gflame_range}.")


def from_dict(d, source="config"):
    """Validate a parsed config.  Raises ConfigError with one sentence."""
    if not isinstance(d, dict):
        raise ConfigError(f"{source} is not a JSON object.")
    fmt = d.get("flamesafe_config")
    if fmt != CONFIG_FORMAT:
        raise ConfigError(f"{source}: flamesafe_config must be "
                          f"{CONFIG_FORMAT}, found {fmt!r}.")

    _only_known(d, TOP_KEYS, "the config")
    c = Config()
    c.universe = _int(d, "universe", 1, 63999)
    dest = d.get("destination")
    if not isinstance(dest, dict):
        raise ConfigError("destination must be an object with ip and port.")
    _only_known(dest, DESTINATION_KEYS, "destination")
    c.destination_ip = _ip(dest.get("ip"), "destination ip")
    c.destination_port = _int(dest, "port", PORT_MIN, PORT_MAX,
                              "destination port")

    link = d.get("link")
    if not isinstance(link, dict):
        raise ConfigError("link must be an object with listen_port and "
                          "status_port.")
    _only_known(link, LINK_KEYS, "link")
    c.link_listen_ip = _ip(link.get("listen_ip", "127.0.0.1"),
                           "link listen_ip", loopback_only=True)
    c.link_listen_port = _int(link, "listen_port", PORT_MIN, PORT_MAX,
                              "link listen_port")
    c.link_status_ip = _ip(link.get("status_ip", "127.0.0.1"),
                           "link status_ip", loopback_only=True)
    c.link_status_port = _int(link, "status_port", PORT_MIN, PORT_MAX,
                              "link status_port")
    if c.link_status_port == c.link_listen_port \
            and c.link_status_ip == c.link_listen_ip:
        raise ConfigError("link listen_port and status_port are the same; "
                          "flamesafe would be talking to itself.")
    if ipaddress.ip_address(c.destination_ip).is_loopback and \
            c.destination_port in (c.link_listen_port, c.link_status_port):
        raise ConfigError(f"destination port {c.destination_port} is one of "
                          f"the link ports; the flame universe would land on "
                          f"the link.")
    c.link_key = link.get("key")
    if not valid_key(c.link_key):
        raise ConfigError(f"link key is missing or not {KEY_MIN} to "
                          f"{KEY_MAX} printable characters without spaces; "
                          f"ltcplay carries the same value in its config.")

    c.gflame_range = d.get("gflame_range")
    if not isinstance(c.gflame_range, str):
        raise ConfigError("gflame_range is missing; it is one of the five "
                          "G-Flame menu options, for example \"30-50%\".")
    c.accept_unsourced_risk = _bool(d, "accept_unsourced_risk", False)
    c.arm_value = _int(d, "arm_value", 0, 255)
    _validate_arm_value(c.arm_value, c.gflame_range, c.accept_unsourced_risk,
                        "arm_value")
    c.lights_warning_led = (rules.GFLAME_WARNING_LED_LO <= c.arm_value
                            <= rules.GFLAME_WARNING_LED_HI)

    c.min_arm_dwell_ms = _int(d, "min_arm_dwell_ms", DWELL_MS_MIN,
                              DWELL_MS_MAX)
    c.arm_stale_ms = _int(d, "arm_stale_ms", STALE_MS_MIN, STALE_MS_MAX)
    c.frame_stale_ms = _int(d, "frame_stale_ms", STALE_MS_MIN, STALE_MS_MAX)
    c.tick_hz = _int(d, "tick_hz", TICK_HZ_MIN, TICK_HZ_MAX)
    c.overrun_ms = _int(d, "overrun_ms", OVERRUN_MS_MIN, OVERRUN_MS_MAX)
    period_ms = 1000.0 / c.tick_hz
    if c.overrun_ms < 2 * period_ms:
        raise ConfigError(f"overrun_ms {c.overrun_ms} is less than two ticks "
                          f"at {c.tick_hz} Hz; every tick would count as an "
                          f"overrun.")
    # How long a fire value from ltcplay's last frame is kept on the wire
    # after ltcplay stops sending.  Shorter than frame_stale_ms, which only
    # governs when a restarted ltcplay's sequence is accepted.
    c.fire_hold_ms = _int(d, "fire_hold_ms", 1, FIRE_HOLD_MS_MAX)
    if c.fire_hold_ms < 2 * period_ms:
        raise ConfigError(f"fire_hold_ms {c.fire_hold_ms} is less than two "
                          f"ticks at {c.tick_hz} Hz; a fire cue could never "
                          f"reach the wire.")
    if c.fire_hold_ms >= c.frame_stale_ms:
        raise ConfigError(f"fire_hold_ms {c.fire_hold_ms} is not below "
                          f"frame_stale_ms {c.frame_stale_ms}.")
    c.confirmed = _bool(d, "confirmed", False)
    if c.confirmed and c.link_key == EXAMPLE_KEY:
        raise ConfigError("the config is marked confirmed but link.key is "
                          "still the example key from the repo; set a key "
                          "of your own and put the same one in ltcplay's "
                          "config.")
    c.note = str(d.get("note", ""))
    log_dir = d.get("log_dir")
    c.log_dir = str(log_dir) if log_dir else None

    groups = d.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigError("groups is missing or empty; nothing to arm.")
    c.groups = []
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            raise ConfigError(f"group {i + 1} is not an object.")
        _only_known(g, GROUP_KEYS, f"group {i + 1}")
        name = g.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"group {i + 1} has no name.")
        if len(name) > NAME_MAX:
            raise ConfigError(f"group {i + 1}: the name is longer than "
                              f"{NAME_MAX} characters.")
        safety = _int(g, "safety", 1, rules.UNIVERSE_SIZE,
                      f"{name}: safety slot")
        fire = g.get("fire")
        if not isinstance(fire, list):
            raise ConfigError(f"{name}: fire must be a list of slots.")
        if not fire:
            raise ConfigError(f"{name}: no fire slots listed. An empty list "
                              f"would silently disable the rising-edge gate "
                              f"for this group.")
        for f in fire:
            if isinstance(f, bool) or not isinstance(f, int) \
                    or not (1 <= f <= rules.UNIVERSE_SIZE):
                raise ConfigError(f"{name}: fire slot {f!r} is not a slot "
                                  f"between 1 and {rules.UNIVERSE_SIZE}.")
            if f == safety:
                raise ConfigError(
                    f"{name}: fire slot {f} is also its safety slot. The "
                    f"G-Flame manual forbids this outright ('Identical DMX "
                    f"Channels!').")
        if len(set(fire)) != len(fire):
            raise ConfigError(f"{name}: a fire slot is listed twice.")
        arm_value = c.arm_value
        if "arm_value" in g:
            arm_value = _int(g, "arm_value", 0, 255, f"{name}: arm_value")
            _validate_arm_value(arm_value, c.gflame_range,
                                c.accept_unsourced_risk, name)
        c.groups.append(Group(name, safety, fire, arm_value))

    names = {}
    seen = {}
    for g in c.groups:
        if g.name in names:
            raise ConfigError(f"two groups are both named {g.name!r}.")
        names[g.name] = True
        if g.safety in seen:
            raise ConfigError(f"{g.name} and {seen[g.safety]} share safety "
                              f"slot {g.safety}.")
        seen[g.safety] = g.name

    # Two groups must not share a fire slot.  This program zeros a group's
    # fire slots through its arming edge, so a shared slot means one group
    # arming can mute another group's live cue.
    fire_owner = {}
    for g in c.groups:
        for f in g.fire:
            if f in fire_owner:
                raise ConfigError(
                    f"{g.name} and {fire_owner[f]} share fire slot {f}. "
                    f"Arming either one would zero that slot for "
                    f"{rules.EDGE_QUIET_FRAMES} frames under the other.")
            fire_owner[f] = g.name

    # A fire slot of one group must not be the safety slot of another, or
    # arming group A would be gated on group B's content and vice versa.
    safeties = {g.safety for g in c.groups}
    for g in c.groups:
        clash = (safeties - {g.safety}).intersection(g.fire)
        if clash:
            raise ConfigError(f"{g.name}: fire slot(s) {sorted(clash)} are "
                              f"safety slots of another group.")
    return c


def load(path):
    """Read and validate the config file at `path`."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"the config file {p} cannot be read: "
                          f"{e.strerror or e}.") from None
    try:
        d = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"the config file {p} is not valid JSON: {e}.") \
            from None
    return from_dict(d, source=str(p))
