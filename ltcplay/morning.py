"""The Morning and Shutdown page's engine side (Jeff, 2026-10-17): the
machine list and layout, the purge, the lockout and the laser tests. The
checklists are in checklist.py. MORNING.md is the design note and threat
analysis; read it first.

Everything here is settings and state in the machine's settings folder:

  ltcplay_morning.json          machines, groups, purge values, lasers,
                                checklist steps, names, the tally server
  ltcplay_morning_layout.json   where each machine sits on the page
  ltcplay_lockout.json          what is locked out (this engine writes it,
                                flamesafe reads it: ONE writer)

The purge never touches flamesafe's arm link: a click on a machine adds it
to a selection the engine publishes on deck-input, the Stream Deck process
(the one arm sender) sets the group's wanted bit, flamesafe decides. Fire
values go out on the flame link's purge frame (flamelink.purge_set), which
the link itself drops 0.25 s after the last heartbeat and never sends while
a show is live.
"""
import json
import os
import re
import threading
import time

from . import laserarm
from . import lasertest
from .users import purge_fire_wanted

CONFIG_FILE = "ltcplay_morning.json"
LAYOUT_FILE = "ltcplay_morning_layout.json"
LOCKOUT_FILE = "ltcplay_lockout.json"
LOCKOUT_FORMAT = 1
LAYOUT_FORMAT = 1
# Jeff's group names, exactly (RULINGS 2026-10-17). A group button is a
# fast multi-select; the machines in each come from the config.
GROUP_NAMES = ("Ground G-Flames", "Scaffold G-Flames", "Scaffold Wavers",
               "Fireball")
KINDS = ("gflame", "waver", "fireball")
UNIVERSE_SIZE = 512
# The page's status must be this fresh for a purge click or fire heartbeat
# (the same as arming from a screen), flamesafe's status too.
FRESH_S = 1.0
# A fire press with no heartbeat for this long is over; the flame link
# drops the frame on its own clock after the same time (PURGE_HOLD_S).
BEAT_STALE_S = 0.25
# The page says it is open every 500 ms; this long without it and the purge
# selection is cleared and the deck is told the page is closed.
OPEN_STALE_S = 2.0
# After this engine writes the lockout file, flamesafe's report is given
# this long to catch up (it polls the file every 0.5 s) before a difference
# counts as a problem.
LOCK_SETTLE_S = 1.5
NAME_MAX = 48
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")

# "fuel" is the Fuel page's own block (fuel.py), checked there: a mistake
# in it shows on the Fuel page and never stops this page.
TOP_KEYS = frozenset(("machines", "groups", "purge", "lasers", "checklists",
                      "names", "tally", "notes", "laser_groups", "fuel",
                      "fireball_groups", "gflame_groups"))
MACHINE_KEYS = frozenset(("id", "name", "kind", "group", "fire", "pan",
                          "tilt", "notes", "safety"))
PURGE_KEYS = frozenset(("gflame_fire", "waver", "fireball"))
WAVER_KEYS = frozenset(("pan_up", "tilt_up", "head_settle_s", "fire"))
FIREBALL_KEYS = frozenset(("fire",))
# PLACEHOLDERS (Jeff, 2026-10-17): a G-Flame fires at 229 and above; the
# Waver head-up pan and tilt and its settle time, and the Fireball value,
# are guesses until Andy says. All in ltcplay_morning.json, "purge".
PURGE_DEFAULTS = {"gflame_fire": 255,
                  "waver": {"pan_up": 128, "tilt_up": 255,
                            "head_settle_s": 1.5, "fire": 255},
                  "fireball": {"fire": 255}}
HEAD_SETTLE_MAX_S = 10.0
# The shortest wait a config may ask for. 0 is allowed (Jeff, 2026-10-26):
# the head values and the fire value then go in the same frame, the first
# heartbeat on which the Waver is selected, unlocked and in an ARMED group.
# Never below 0 (review of PR #68, P3-11 asked for 0.5; Jeff's call).
HEAD_SETTLE_MIN_S = 0.0


class MorningConfigError(ValueError):
    """ltcplay_morning.json is wrong, in a sentence."""


def _write_json(path, doc):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_flamesafe_groups(path):
    """[{"name", "safety", "safeties": [...], "fire": [...]}] from
    flamesafe's config, read as plain JSON (ltcplay never imports
    flamesafe). ValueError on anything missing. `safety` is what the file
    says (one slot, or a list for a multi-safety group, 2026-10-26);
    `safeties` is always the list."""
    with open(path, encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    groups = doc.get("groups") if isinstance(doc, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"{path} has no groups.")
    out = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str):
            raise ValueError(f"{path}: a group has no name.")
        fire = g.get("fire")
        if not isinstance(fire, list):
            raise ValueError(f"{path}: {g['name']} has no fire slots.")
        saf = g.get("safety")
        saf_list = saf if isinstance(saf, list) else \
            ([] if saf is None else [saf])
        out.append({"name": g["name"], "safety": saf,
                    "safeties": [int(x) for x in saf_list],
                    "fire": [int(f) for f in fire]})
    return out


def _slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s or "group"


def _kind_for(group_name):
    n = group_name.lower()
    if "waver" in n or "wave" in n:
        return "waver"
    if "fireball" in n or "ball" in n:
        return "fireball"
    return "gflame"


def _bank_for(group_name, kind):
    n = group_name.lower()
    if kind == "fireball":
        return "Fireball"
    if kind == "waver":
        return "Scaffold Wavers"
    if "scaffold" in n or "cat" in n or "upper" in n:
        return "Scaffold G-Flames"
    return "Ground G-Flames"


def fixture_pairing(group):
    """{safety slot: [its fixture's fire slots]} for a flamesafe group with
    a list of safety slots, worked out from the slot layout and never from
    list order: a fixture's fire slots are the run of the group's fire
    slots directly below its safety slot (the wave flamer: channels 1 to 5
    fire, channel 6 safety). None when that does not account for every
    fire slot exactly once with at least one per safety slot: the pairing
    is then not known, and is never guessed."""
    saf = list(group.get("safeties") or [])
    fire = set(group.get("fire") or [])
    if len(saf) < 2:
        return None
    out, seen = {}, set()
    for s in saf:
        run, x = [], s - 1
        while x in fire:
            run.append(x)
            x -= 1
        if not run:
            return None
        out[s] = sorted(run)
        seen.update(run)
    return out if seen == fire else None


# A waver with no pan or no tilt slot (a default machine built from
# flamesafe's groups, such as Jeff's 6-channel wave flamers whose control
# channels are not mapped yet) is never purged: writing the fire value to
# every control slot would be a guess. It stays a machine, so locking and
# the lamps still work. The reason, word for word, is on the page.
NO_PURGE_MAP = ("no pan, tilt and fire map yet (set it in "
                "ltcplay_morning.json machines)")
# A fireball (Jeff, 2026-10-26: fuel charge, air charge, valve open and
# ignite) is never purged and never armed for one: its channels are a
# sequence, and writing one purge value to all of them at once is not one
# (it could open the valve and ignite on a fuel charge nobody asked for).
# No config can turn this on: a fireball purge needs its own design and
# review first (MORNING.md). purge.fireball in the config is not used.
NO_FIREBALL_PURGE = "a fireball has no purge sequence yet"
# A flamesafe group with several fire slots and one safety that nothing
# says is a row of G-Flames (review of PR #120, P2-1: "FB", "Boom" or
# "Poofer" with a 4 slot fire list could be a fireball by another name).
# With no machines list its machines are built, so locking and the lamps
# work, but none is purged or armed for a purge until the group is
# declared: fireball_groups (never purged) or gflame_groups (a row of
# G-Flames, purged one by one). A name that says row, cat, walk, scaffold,
# ground or g-flame is a row (GFLAME_ROW_WORDS: Jeff's front row and
# cat-walk), as a name that says fireball or ball is a fireball.
NO_UNKNOWN_PURGE = ("unknown multi-channel group, declare it in "
                    "fireball_groups (or gflame_groups for a row of "
                    "G-Flames) in ltcplay_morning.json")
GFLAME_ROW_WORDS = ("row", "cat", "walk", "scaffold", "ground", "gflame",
                    "g-flame", "g flame")
# The refusals that keep a machine out of the purge selection altogether,
# so its group is never armed for a purge.
NEVER_SELECTED = (NO_FIREBALL_PURGE, NO_UNKNOWN_PURGE)


class Machine:
    __slots__ = ("id", "name", "kind", "group", "fire", "pan", "tilt",
                 "bank", "notes", "safety", "safeties")

    def __init__(self, id, name, kind, group, fire, pan=None, tilt=None,
                 bank=None, notes="", safety=None, safeties=None):
        self.id = id
        self.name = name
        self.kind = kind
        self.group = group
        self.fire = list(fire)
        self.pan = pan
        self.tilt = tilt
        self.bank = bank
        self.notes = notes
        # Its own safety slot, for a fixture in a flamesafe multi-safety
        # group (2026-10-26): locking the machine puts it in the lockout
        # file, and flamesafe holds that safety at zero and arms the
        # group's other fixtures without it. None for every other machine.
        # `safeties` is every safety slot it locks: its own, or for the
        # one machine standing for a whole group whose fixtures cannot be
        # told apart, every one of the group's.
        self.safety = safety
        self.safeties = tuple(safeties) if safeties is not None else \
            ((safety,) if safety is not None else ())

    def purge_refusal(self):
        """Why this machine is never purged, or "": a waver needs its pan
        and tilt slots (the head goes straight up first) and a fire map; a
        fireball is never purged (NO_FIREBALL_PURGE)."""
        if self.kind == "fireball":
            return NO_FIREBALL_PURGE
        if self.kind == "waver" and (self.pan is None or self.tilt is None
                                     or not self.fire):
            return NO_PURGE_MAP
        return ""

    def channels(self):
        """Every slot this machine owns: fire, a Waver's pan and tilt, and
        its own safety slot in a multi-safety group."""
        out = set(self.fire) | set(self.safeties)
        for c in (self.pan, self.tilt):
            if c is not None:
                out.add(c)
        return out

    def view(self):
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "group": self.group, "fire": list(self.fire),
                "pan": self.pan, "tilt": self.tilt, "bank": self.bank,
                "notes": self.notes, "safety": self.safety,
                "safeties": list(self.safeties)}


def default_machines(groups, fireball_groups=(), unknown_groups=()):
    """One machine per fire slot of flamesafe's groups: a sensible default
    until Andy's own list is written into ltcplay_morning.json. A waver
    built here has no pan and no tilt slot, so it is never purged
    (Machine.purge_refusal) and its notes say so on the page. A group in
    `fireball_groups` is one fireball machine, whatever its name."""
    out = []
    for g in groups:
        kind = "fireball" if g["name"] in fireball_groups else \
            _kind_for(g["name"])
        bank = _bank_for(g["name"], kind)
        notes = f"Never purged: {NO_PURGE_MAP}." if kind == "waver" else \
            f"Never purged: {NO_FIREBALL_PURGE}." if kind == "fireball" \
            else f"Never purged: {NO_UNKNOWN_PURGE}." \
            if g["name"] in unknown_groups else ""
        saf = g.get("safeties") or []
        if len(saf) > 1:
            # A multi-safety group (2026-10-26): one machine per fixture,
            # each with its own fire slots and its own safety slot, paired
            # by the slot layout (fixture_pairing), never by list order,
            # so locking one locks that fixture's safety with its fire.
            pairs = fixture_pairing(g)
            if pairs is None:
                # The fixtures cannot be told apart: one machine for the
                # whole group, and locking it locks every slot of it.
                out.append(Machine(_slug(g["name"]), g["name"], kind,
                                   g["name"], list(g["fire"]), bank=bank,
                                   notes=notes, safeties=saf))
                continue
            order = sorted(pairs, key=lambda s: pairs[s][0])
            for n, s in enumerate(order, 1):
                out.append(Machine(f"{_slug(g['name'])}-{n}",
                                   f"{g['name']} {n}", kind, g["name"],
                                   pairs[s], bank=bank, notes=notes,
                                   safety=s))
            continue
        if kind == "fireball":
            # One fireball is one machine: its fire slots (fuel, air,
            # valve, ignite) are one fixture, so locking it locks them all.
            out.append(Machine(_slug(g["name"]), g["name"], kind,
                               g["name"], list(g["fire"]), bank=bank,
                               notes=notes))
            continue
        for n, slot in enumerate(g["fire"], 1):
            many = len(g["fire"]) > 1
            out.append(Machine(f"{_slug(g['name'])}-{n}" if many
                               else _slug(g["name"]),
                               f"{g['name']} {n}" if many else g["name"],
                               kind, g["name"], [slot], bank=bank,
                               notes=notes))
    return out


def _slot(v, what):
    if isinstance(v, bool) or not isinstance(v, int) or \
            not 1 <= v <= UNIVERSE_SIZE:
        raise MorningConfigError(f"{what} has to be a slot 1 to "
                                 f"{UNIVERSE_SIZE}, not {v!r}.")
    return v


def _value(v, what):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
        raise MorningConfigError(f"{what} has to be 0 to 255, not {v!r}.")
    return v


def _only(d, keys, where):
    unknown = sorted(k for k in d if k not in keys)
    if unknown:
        raise MorningConfigError(
            f"{where} has no setting {', '.join(repr(k) for k in unknown)}; "
            f"it takes {', '.join(sorted(keys))}.")


def parse_purge(doc, where):
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise MorningConfigError(f"{where}: purge has to be an object.")
    _only(doc, PURGE_KEYS, f"{where}: purge")
    out = json.loads(json.dumps(PURGE_DEFAULTS))
    if "gflame_fire" in doc:
        out["gflame_fire"] = _value(doc["gflame_fire"],
                                    f"{where}: purge.gflame_fire")
    w = doc.get("waver", {})
    if not isinstance(w, dict):
        raise MorningConfigError(f"{where}: purge.waver has to be an object.")
    _only(w, WAVER_KEYS, f"{where}: purge.waver")
    for k in ("pan_up", "tilt_up", "fire"):
        if k in w:
            out["waver"][k] = _value(w[k], f"{where}: purge.waver.{k}")
    if "head_settle_s" in w:
        s = w["head_settle_s"]
        if isinstance(s, bool) or not isinstance(s, (int, float)) or \
                not HEAD_SETTLE_MIN_S <= s <= HEAD_SETTLE_MAX_S:
            raise MorningConfigError(f"{where}: purge.waver.head_settle_s "
                                     f"has to be {HEAD_SETTLE_MIN_S:g} to "
                                     f"{HEAD_SETTLE_MAX_S:g} seconds (0 sends "
                                     f"the head up and fires in the same "
                                     f"frame).")
        out["waver"]["head_settle_s"] = float(s)
    fb = doc.get("fireball", {})
    if not isinstance(fb, dict):
        raise MorningConfigError(f"{where}: purge.fireball has to be an "
                                 f"object.")
    _only(fb, FIREBALL_KEYS, f"{where}: purge.fireball")
    if "fire" in fb:
        out["fireball"]["fire"] = _value(fb["fire"],
                                         f"{where}: purge.fireball.fire")
    return out


def parse_machines(doc, where, groups, fireball_groups=()):
    """The "machines" list, checked against flamesafe's groups when they
    are known: every slot a machine owns must be one of its group's fire
    slots, or flamesafe would zero it on every packet. Every machine in a
    fireball group (`fireball_groups`, or any group one machine says is a
    fireball) has to say kind "fireball": a fireball is never purged, and
    a machine of another kind there would be (review of PR #120, P1)."""
    if not isinstance(doc, list) or not doc:
        raise MorningConfigError(f"{where}: machines has to be a list with "
                                 f"at least one machine, or be left out.")
    fireball_groups = set(fireball_groups) | {
        d.get("group") for d in doc if isinstance(d, dict) and
        d.get("kind") == "fireball" and isinstance(d.get("group"), str)}
    by_group = {g["name"]: set(g["fire"]) for g in (groups or [])}
    saf_of = {g["name"]: list(g.get("safeties") or [])
              for g in (groups or [])}
    pair_of = {g["name"]: fixture_pairing(g) for g in (groups or [])}
    out, ids, owned = [], set(), {}
    for i, d in enumerate(doc):
        w = f"{where}: machines[{i + 1}]"
        if not isinstance(d, dict):
            raise MorningConfigError(f"{w} is not an object.")
        _only(d, MACHINE_KEYS, w)
        mid = d.get("id")
        if not isinstance(mid, str) or not ID_RE.match(mid) or mid in ids:
            raise MorningConfigError(f"{w} needs an id of its own: letters, "
                                     f"digits, dot, dash or underscore, up "
                                     f"to 48.")
        ids.add(mid)
        name = d.get("name", mid)
        if not isinstance(name, str) or not name.strip() or \
                len(name) > NAME_MAX:
            raise MorningConfigError(f"{w}: name has to be words, up to "
                                     f"{NAME_MAX} characters.")
        kind = d.get("kind", "gflame")
        if kind not in KINDS:
            raise MorningConfigError(f"{w}: kind has to be one of "
                                     f"{', '.join(repr(k) for k in KINDS)}.")
        group = d.get("group")
        if not isinstance(group, str) or not group.strip():
            raise MorningConfigError(f"{w}: group is the flamesafe group "
                                     f"this machine is armed with.")
        if groups and group not in by_group:
            raise MorningConfigError(
                f"{w}: group {group!r} is not a flamesafe group "
                f"({', '.join(sorted(by_group))}).")
        fire = d.get("fire")
        if not isinstance(fire, list) or not fire:
            raise MorningConfigError(f"{w}: fire has to be a list of the "
                                     f"machine's fire slots.")
        fire = [_slot(f, f"{w}: fire slot") for f in fire]
        pan = tilt = None
        if kind == "waver":
            if "pan" not in d or "tilt" not in d:
                raise MorningConfigError(f"{w}: a waver needs pan and tilt "
                                         f"slots (the head goes straight up "
                                         f"before a purge).")
            pan = _slot(d["pan"], f"{w}: pan")
            tilt = _slot(d["tilt"], f"{w}: tilt")
            if pan == tilt or pan in fire or tilt in fire:
                raise MorningConfigError(f"{w}: pan, tilt and the fire slots "
                                         f"have to be different slots.")
        elif "pan" in d or "tilt" in d:
            raise MorningConfigError(f"{w}: only a waver has pan and tilt.")
        if group in fireball_groups and kind != "fireball":
            raise MorningConfigError(
                f"{w}: {group!r} is a fireball group, so this machine has to "
                f"say \"kind\": \"fireball\" (a fireball is never purged). "
                f"It says {kind!r}{'' if 'kind' in d else ' (left out)'}.")
        notes = d.get("notes", "")
        if not isinstance(notes, str):
            raise MorningConfigError(f"{w}: notes has to be text.")
        safety = None
        if groups and len(saf_of[group]) > 1 and "safety" not in d:
            raise MorningConfigError(
                f"{w}: {group!r} has a safety slot for each fixture in "
                f"flamesafe's config, so this machine has to name its own "
                f"(safety, one of "
                f"{', '.join(str(x) for x in saf_of[group])}). Locking it "
                f"then locks that safety slot with its fire slots.")
        if "safety" in d:
            safety = _slot(d["safety"], f"{w}: safety")
            if groups and (len(saf_of[group]) < 2 or
                           safety not in saf_of[group]):
                raise MorningConfigError(
                    f"{w}: safety {safety} is not one of {group!r}'s safety "
                    f"slots in flamesafe's config. Only a machine in a group "
                    f"with a list of safety slots names its own.")
            pairs = pair_of.get(group) if groups else None
            if pairs is not None:
                mine = set(pairs[safety])
                other = sorted(c for c in fire + [pan, tilt]
                               if c is not None and c not in mine)
                if other:
                    owner = next((x for x, f in pairs.items()
                                  if other[0] in f), None)
                    raise MorningConfigError(
                        f"{w}: slot {other[0]} is on the fixture whose "
                        f"safety slot is {owner}, not {safety}. A machine "
                        f"names its own fixture's safety slot.")
        m = Machine(mid, name.strip(), kind, group, fire, pan, tilt,
                    notes=notes, safety=safety)
        for c in m.channels():
            if groups and c != safety and c not in by_group[group]:
                raise MorningConfigError(
                    f"{w}: slot {c} is not one of {group!r}'s fire slots in "
                    f"flamesafe's config, so flamesafe would hold it at "
                    f"zero. List it there first.")
            if c in owned:
                raise MorningConfigError(f"{w}: slot {c} is also "
                                         f"{owned[c]}'s.")
            owned[c] = mid
        out.append(m)
    return out


def parse_banks(doc, where, machines):
    """The "groups" object: Jeff's four names, each a list of machine ids.
    A name left out is an empty group; every machine not placed keeps its
    default group."""
    ids = {m.id: m for m in machines}
    placed = set()
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise MorningConfigError(f"{where}: groups has to be an object of "
                                 f"the four group names.")
    for k in doc:
        if k not in GROUP_NAMES:
            raise MorningConfigError(
                f"{where}: groups has {k!r}; the group names are exactly "
                f"{', '.join(GROUP_NAMES)}.")
        lst = doc[k]
        if not isinstance(lst, list):
            raise MorningConfigError(f"{where}: groups[{k!r}] has to be a "
                                     f"list of machine ids.")
        for mid in lst:
            if mid not in ids:
                raise MorningConfigError(f"{where}: groups[{k!r}] names "
                                         f"{mid!r}, which is not a machine.")
            if mid in placed:
                raise MorningConfigError(f"{where}: {mid!r} is in two "
                                         f"groups.")
            placed.add(mid)
            ids[mid].bank = k
    banks = {k: [] for k in GROUP_NAMES}
    for m in machines:
        if m.bank is None:
            m.bank = _bank_for(m.group, m.kind)
        banks[m.bank].append(m.id)
    return banks


def parse_gflame_groups(doc, where, groups, fireball_groups):
    """The "gflame_groups" list: flamesafe group names that are rows of
    G-Flames, one G-Flame per fire slot (review of PR #120, P2-1), for a
    name GFLAME_ROW_WORDS does not already say. Never a fireball group."""
    out = set()
    if doc is None:
        return frozenset()
    if not isinstance(doc, list) or \
            not all(isinstance(x, str) and x.strip() for x in doc):
        raise MorningConfigError(f"{where}: gflame_groups has to be a list "
                                 f"of flamesafe group names.")
    known = {g["name"] for g in (groups or [])}
    for x in doc:
        if groups and x not in known:
            raise MorningConfigError(
                f"{where}: gflame_groups names {x!r}, which is not a "
                f"flamesafe group ({', '.join(sorted(known))}).")
        if x in fireball_groups:
            raise MorningConfigError(
                f"{where}: {x!r} is a fireball group, so it cannot also be a "
                f"row of G-Flames (gflame_groups).")
        out.add(x)
    return frozenset(out)


def unknown_multi_groups(groups, fireball_groups, gflame_groups):
    """The flamesafe groups whose default machines are never purged:
    several fire slots, one safety slot (or none), not a waver, not
    declared (fireball_groups, gflame_groups) and not a row by name
    (GFLAME_ROW_WORDS). A multi-safety group is fixtures paired by slot
    layout, so it is not one of these."""
    out = set()
    for g in groups or []:
        name = g["name"]
        if len(g.get("fire") or []) < 2 or len(g.get("safeties") or []) > 1:
            continue
        if name in fireball_groups or name in gflame_groups:
            continue
        if _kind_for(name) != "gflame":
            continue
        if any(w in name.lower() for w in GFLAME_ROW_WORDS):
            continue
        out.add(name)
    return frozenset(out)


def parse_fireball_groups(doc, where, groups):
    """The "fireball_groups" list: flamesafe group names that are fireballs
    (Jeff, 2026-10-26), plus every flamesafe group whose name says fireball
    or ball (_kind_for). A fireball group is never purged; every machine in
    it has to be kind "fireball"."""
    out = set()
    if doc is not None:
        if not isinstance(doc, list) or \
                not all(isinstance(x, str) and x.strip() for x in doc):
            raise MorningConfigError(f"{where}: fireball_groups has to be a "
                                     f"list of flamesafe group names.")
        known = {g["name"] for g in (groups or [])}
        for x in doc:
            if groups and x not in known:
                raise MorningConfigError(
                    f"{where}: fireball_groups names {x!r}, which is not a "
                    f"flamesafe group ({', '.join(sorted(known))}).")
            out.add(x)
    for g in groups or []:
        if _kind_for(g["name"]) == "fireball":
            out.add(g["name"])
    return frozenset(out)


class MorningConfig:
    """ltcplay_morning.json, validated. Everything is optional: with no
    file at all the page shows one machine per flamesafe fire slot, Jeff's
    four groups, placeholder purge values, four undecided lasers, two empty
    checklists, no names and the tally off."""

    def __init__(self, machines, banks, purge, lasers, checklists, names,
                 tally, path=None, notes="", laser_groups=None,
                 fireball_groups=None, unknown_groups=None):
        self.machines = machines
        # Groups whose default machines are never purged until declared
        # (unknown_multi_groups; review of PR #120, P2-1).
        self.unknown_groups = frozenset(unknown_groups or ())
        # The flamesafe groups that are fireballs: never purged, never
        # armed for one, whatever any machine in them says (review of PR
        # #120, P1). Declared ones, and every group with a fireball machine.
        self.fireball_groups = frozenset(fireball_groups or ()) | \
            frozenset(m.group for m in machines if m.kind == "fireball")
        # The laser groups for arming (Jeff, 2026-10-22; laserarm.py): a
        # fast multi-select, nothing saved. PLACEHOLDER until Jeff and Andy
        # name them: left out, one group, "All lasers".
        self.laser_groups = laser_groups if laser_groups is not None else \
            laserarm.parse_groups(None, "", [l.id for l in lasers])
        self.by_id = {m.id: m for m in machines}
        self.banks = banks
        self.purge = purge
        self.lasers = lasers
        self.checklists = checklists
        self.names = names
        self.tally = tally
        self.path = path
        self.notes = notes

    @classmethod
    def parse(cls, doc, where=CONFIG_FILE, groups=None, laser_host=None,
              laser_port=None):
        from . import checklist as checklist_mod
        if not isinstance(doc, dict):
            raise MorningConfigError(f"{where}: it has to be one JSON "
                                     f"object.")
        _only(doc, TOP_KEYS, f"{where}")
        fireballs = parse_fireball_groups(doc.get("fireball_groups"), where,
                                          groups)
        rows = parse_gflame_groups(doc.get("gflame_groups"), where, groups,
                                   fireballs)
        unknown = frozenset()
        if "machines" in doc:
            # Andy's own list says what each machine is: trusted as written
            # (a fireball group still has to be all kind fireball).
            machines = parse_machines(doc["machines"], where, groups,
                                      fireballs)
        else:
            unknown = unknown_multi_groups(groups, fireballs, rows)
            machines = default_machines(groups or [], fireballs, unknown)
        banks = parse_banks(doc.get("groups"), where, machines)
        purge = parse_purge(doc.get("purge"), where)
        if "lasers" in doc:
            try:
                lasers = lasertest.parse_lasers(doc["lasers"], where,
                                                laser_host, laser_port)
            except lasertest.LaserConfigError as e:
                raise MorningConfigError(str(e)) from None
        else:
            lasers = [lasertest.Laser(f"laser-{i}", f"Laser {i}",
                                      host=laser_host, port=laser_port)
                      for i in range(1, 5)]
        try:
            checklists = checklist_mod.parse_checklists(
                doc.get("checklists"), where)
            names = checklist_mod.parse_names(doc.get("names"), where)
            tally = checklist_mod.parse_tally(doc.get("tally"), where)
        except checklist_mod.ChecklistConfigError as e:
            raise MorningConfigError(str(e)) from None
        notes = doc.get("notes", "")
        if not isinstance(notes, str):
            raise MorningConfigError(f"{where}: notes has to be text.")
        try:
            laser_groups = laserarm.parse_groups(
                doc.get("laser_groups"), where, [l.id for l in lasers])
        except laserarm.LaserArmConfigError as e:
            raise MorningConfigError(str(e)) from None
        return cls(machines, banks, purge, lasers, checklists, names, tally,
                   path=where if where != CONFIG_FILE else None, notes=notes,
                   laser_groups=laser_groups, fireball_groups=fireballs,
                   unknown_groups=unknown)

    def purge_refusal(self, m):
        """Why machine `m` is never purged, or "": by its flamesafe group
        first (a fireball group, whatever the machine's kind), then by the
        machine itself (Machine.purge_refusal)."""
        if m.group in self.fireball_groups or m.kind == "fireball":
            return NO_FIREBALL_PURGE
        if m.group in self.unknown_groups:
            return NO_UNKNOWN_PURGE
        return m.purge_refusal()

    def journal_lines(self):
        """What this config says that a person should read once, in the
        journal: each unknown multi-channel group (review of PR #120,
        P2-1)."""
        return [f"Morning page: the flame group {g!r} has several fire "
                f"slots and one safety, and nothing says what it is, so its "
                f"machines are never purged: {NO_UNKNOWN_PURGE}."
                for g in sorted(self.unknown_groups)]

    @classmethod
    def load(cls, path, groups=None, laser_host=None, laser_port=None):
        if not os.path.exists(path):
            return cls.parse({}, path, groups, laser_host, laser_port)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise MorningConfigError(f"{path} could not be read: {e}")
        return cls.parse(doc, path, groups, laser_host, laser_port)

    def view(self):
        # never_purged: the engine's own reason a machine is never purged,
        # or "", for the Startup page's tile (review of PR #120, P2-1).
        return {"machines": [dict(m.view(),
                                  never_purged=self.purge_refusal(m))
                             for m in self.machines],
                "groups": {k: list(v) for k, v in self.banks.items()},
                "purge": self.purge,
                "checklist_names": list(self.names),
                "file": self.path}


# ------------------------------------------------------------- the layout --

def default_layout(cfg):
    """Each group on its own row, top down the way the rig stands: Fireball
    at the back, the Scaffold rows, the Ground row along the front. x and
    y are percent of the view."""
    rows = {"Fireball": 12, "Scaffold Wavers": 34, "Scaffold G-Flames": 56,
            "Ground G-Flames": 82}
    out = {}
    for bank, ids in cfg.banks.items():
        n = len(ids)
        for i, mid in enumerate(ids):
            x = 50.0 if n == 1 else 8.0 + 84.0 * i / (n - 1)
            out[mid] = {"x": round(x, 1), "y": float(rows.get(bank, 50))}
    return out


class Layout:
    """ltcplay_morning_layout.json: {id: {x, y}} in percent. Written by the
    page's Arrange mode (Save), read on every view."""

    def __init__(self, path):
        self.path = path

    def load(self, cfg):
        pos = default_layout(cfg)
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return pos, False
        got = doc.get("positions") if isinstance(doc, dict) else None
        if doc.get("layout_format") != LAYOUT_FORMAT if isinstance(doc, dict) \
                else True:
            return pos, False
        if isinstance(got, dict):
            for mid, p in got.items():
                if mid in pos and isinstance(p, dict):
                    try:
                        x, y = float(p["x"]), float(p["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 <= x <= 100 and 0 <= y <= 100:
                        pos[mid] = {"x": x, "y": y}
        return pos, True

    def save(self, cfg, positions):
        if not isinstance(positions, dict):
            raise ValueError("positions has to be an object of machine ids.")
        out = {}
        for mid, p in positions.items():
            if mid not in cfg.by_id:
                raise ValueError(f"{mid!r} is not a machine.")
            try:
                x, y = float(p["x"]), float(p["y"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"{mid}: x and y have to be numbers.")
            if not (0 <= x <= 100 and 0 <= y <= 100):
                raise ValueError(f"{mid}: x and y have to be 0 to 100.")
            out[mid] = {"x": round(x, 1), "y": round(y, 1)}
        _write_json(self.path, {"layout_format": LAYOUT_FORMAT,
                                "positions": out})
        return out

    def reset(self):
        """Back to the default (the page's Reset layout): the file is
        removed, so load() lays the groups out by row again."""
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------ the lockout --

class Lockout:
    """What is locked out, and the one file flamesafe reads for it. This
    engine is the ONLY writer. Survives restarts: read at start, written
    atomically on every change."""

    def __init__(self, path, journal=None, wall=time.time):
        self.path = path
        self._journal = journal
        self._wall = wall
        self._lock = threading.Lock()
        self.machines = {}      # id -> {"since", "who", "device", "reason"}
        self.lasers = {}
        self.load_error = ""
        # The flame_channels the FILE names (what flamesafe enforces), kept
        # apart from what the config resolves them to, so a config edited
        # under a lock is noticed (review of PR #68, P3-10). None: no file.
        self.file_channels = None
        # True when the last set() or rewrite() actually wrote the file (an
        # "already locked" answer, or a save that failed, did not): only a
        # real write earns flamesafe's settle time (second review, P3-1).
        self.wrote = False
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self.load_error = f"{self.path} could not be read: {e}"
            return
        if not isinstance(doc, dict) or \
                doc.get("lockout_format") != LOCKOUT_FORMAT:
            self.load_error = f"{self.path} is not a lockout file."
            return
        chans = doc.get("flame_channels")
        if not isinstance(chans, list):
            # flamesafe refuses such a file too (not the format): say so
            # rather than read it as "no slots" (review of PR #68, P3-13).
            self.load_error = (f"{self.path} has no flame_channels list, so "
                               f"it is not a lockout file flamesafe can read")
            return
        self.file_channels = sorted(c for c in chans if isinstance(c, int)
                                    and not isinstance(c, bool))
        for kind in ("machines", "lasers"):
            got = doc.get(kind)
            if isinstance(got, dict):
                getattr(self, kind).update(
                    {str(k): (v if isinstance(v, dict) else {})
                     for k, v in got.items()})

    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    def channels(self, cfg):
        """Every flame slot a locked machine owns: what flamesafe zeroes."""
        out = set()
        for mid in self.machines:
            m = cfg.by_id.get(mid)
            if m is not None:
                out |= m.channels()
        return out

    def save(self, cfg):
        """Write the file flamesafe reads. Raises OSError when it cannot be
        written (the caller says so; the lock stays in memory)."""
        doc = {"lockout_format": LOCKOUT_FORMAT,
               "note": "Written by LTC Player (the show engine) from the "
                       "Morning and Shutdown page. flamesafe reads "
                       "flame_channels and holds them at zero. Do not edit "
                       "by hand: lock and unlock on the page.",
               "written": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(self._wall())),
               "flame_channels": sorted(self.channels(cfg)),
               "machines": self.machines, "lasers": self.lasers}
        _write_json(self.path, doc)
        self.file_channels = doc["flame_channels"]

    def is_locked(self, kind, uid):
        return uid in (self.machines if kind == "machine" else self.lasers)

    def unknown(self, cfg):
        """Locked ids the config no longer names (a renamed machine, a
        config that could not be read and fell back to the defaults): their
        slots cannot be worked out, so the file must not be rewritten from
        this table until they are resolved (review of PR #68, P1-3)."""
        lasers = {l.id for l in cfg.lasers}
        return (sorted(m for m in self.machines if m not in cfg.by_id),
                sorted(l for l in self.lasers if l not in lasers))

    def problem(self, cfg):
        """Why the lock table cannot be trusted or written right now, or ""."""
        if self.load_error:
            return (f"the lockout file could not be read ({self.load_error}), "
                    f"so what is locked out is not known; fix or remove the "
                    f"file, then restart the engine")
        um, ul = self.unknown(cfg)
        if um or ul:
            return (f"the lockout file names "
                    f"{', '.join(um + ul)}, which ltcplay_morning.json does "
                    f"not (renamed, or the file could not be read); fix the "
                    f"config, then restart the engine")
        if self.file_channels is not None and \
                set(self.file_channels) != self.channels(cfg):
            return (f"the lockout file holds flame slots "
                    f"{self.file_channels} but ltcplay_morning.json now gives "
                    f"the locked machines {sorted(self.channels(cfg))}: the "
                    f"config changed under a lock; check it, then use "
                    f"'Rewrite the lockout file' on the show machine")
        return ""

    def rewrite(self, cfg, who, device):
        """The one deliberate way out of a problem() (review of PR #68,
        P1-5, P3-10): write the file again from THIS table, dropping ids
        the config does not know, so flamesafe follows what this engine
        can account for. Local, confirmed, and journaled as a fault. (ok,
        sentence)."""
        um, ul = self.unknown(cfg)
        self.wrote = False
        with self._lock:
            for m in um:
                self.machines.pop(m, None)
            for l in ul:
                self.lasers.pop(l, None)
            try:
                self.save(cfg)
            except OSError as e:
                return False, (f"The lockout file could NOT be written ({e}). "
                               f"Nothing changed.")
            self.wrote = True
            self.load_error = ""
        self._note(f"{who or device} REWROTE the lockout file from this "
                   f"engine's table: flame slots {self.file_channels}"
                   f"{', dropping ' + ', '.join(um + ul) if um or ul else ''}"
                   f". Whatever flamesafe held beyond that is lifted.",
                   action="lockout", outcome="rewritten", fault=True,
                   who=who, screen=device)
        return True, (f"The lockout file was written again: flame slots "
                      f"{self.file_channels} are locked out, nothing else.")

    def set(self, cfg, kind, uid, on, who, device, reason="",
            confirmed=False):
        """Lock or unlock one unit. (ok, sentence). Unlocking needs
        confirmed True: one deliberate confirm (Jeff). A lock never does.
        Nothing is written while the table cannot be trusted (problem):
        a write from a half known table would lift flamesafe's locks."""
        self.wrote = False
        why = self.problem(cfg)
        if why:
            return False, (f"Nothing was locked or unlocked: {why}. The "
                           f"lockout file was not touched, so flamesafe keeps "
                           f"enforcing what it holds.")
        table = self.machines if kind == "machine" else self.lasers
        if kind == "machine":
            unit = cfg.by_id.get(uid)
            label = unit.name if unit is not None else None
        else:
            unit = next((l for l in cfg.lasers if l.id == uid), None)
            label = unit.name if unit is not None else None
        if unit is None:
            return False, f"There is no {kind} called {uid!r}."
        reason = " ".join(str(reason or "").split())[:200]
        with self._lock:
            if on:
                if uid in table:
                    return True, f"{label} is already locked out."
                table[uid] = {"since": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self._wall())),
                    "who": who, "device": device, "reason": reason}
            else:
                if uid not in table:
                    return True, f"{label} is not locked out."
                if confirmed is not True:
                    return False, (f"Unlocking {label} needs a confirm on "
                                   f"the page. Nothing was changed.")
                del table[uid]
            try:
                self.save(cfg)
                saved = ""
                self.wrote = True
            except OSError as e:
                saved = (f" The lockout file could NOT be written ({e}): "
                         f"flamesafe will not see this change. The lock is "
                         f"held in this program until it is.")
        what = "LOCKED OUT" if on else "UNLOCKED"
        self._note(f"{label} ({kind}) {what} by {who or device}"
                   f"{' on the ' + device if device and who else ''}"
                   f"{': ' + reason if reason else ''}.{saved}",
                   action="lockout", outcome="locked" if on else "unlocked",
                   fault=bool(saved), who=who, screen=device)
        return True, (f"{label} is locked out until it is unlocked here. It "
                      f"cannot be armed or fired from anywhere; show cues "
                      f"skip it.{saved}" if on else
                      f"{label} is unlocked.{saved}")

    def view(self, cfg):
        um, ul = self.unknown(cfg)
        return {"machines": {k: dict(v) for k, v in self.machines.items()},
                "lasers": {k: dict(v) for k, v in self.lasers.items()},
                "flame_channels": sorted(self.channels(cfg)),
                "file": self.path, "load_error": self.load_error,
                "file_channels": self.file_channels,
                "unknown_machines": um, "unknown_lasers": ul,
                "problem": self.problem(cfg)}


# -------------------------------------------------------------- the purge --

class Purge:
    """The purge selection and the fire heartbeat. Pure apart from the
    providers it is built with; the Morning facade owns the clock."""

    def __init__(self, cfg_get, lockout, link_get, flames_get, live_check,
                 group_names, journal=None, clock=time.monotonic,
                 locks_ok=lambda: ""):
        self._cfg = cfg_get              # () -> MorningConfig
        self._lockout = lockout
        self._link = link_get            # () -> FlameLink or None
        self._flames = flames_get        # () -> flame status view or None
        self._live = live_check          # () -> refusal sentence or ""
        self._names = group_names        # () -> flamesafe group names
        self._locks_ok = locks_ok        # () -> refusal while flamesafe has
        #                                  not confirmed the engine's locks
        self._journal = journal
        self._clock = clock
        self._lock = threading.Lock()
        self.selected = []               # machine ids, in click order
        self.owner = None                # device that owns the selection
        self.fire = None                 # {"id", "device", "start", "beat",
        #                                  "heads": {waver id: clock when its
        #                                  head started up}} while a key is
        #                                  held
        self._fire_ids = 0
        self.fires = 0
        self.last_text = ""

    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    # -- rules ---------------------------------------------------------------
    def _refusal(self, device, seen_ok, need_flames=True):
        """Why nothing may be selected or fired right now, or ""."""
        why = self._live()
        if why:
            return why
        if not seen_ok:
            return ("The page's status is more than 1 s old. Wait for it "
                    "to be live, then try again.")
        if need_flames:
            fl = self._flames()
            if not fl or fl.get("stale") or fl.get("age_ms") is None or \
                    fl["age_ms"] > FRESH_S * 1000:
                return ("flamesafe has not reported in the last second, so "
                        "its real armed state is not known. Nothing can be "
                        "purged until it does.")
        if self._link() is None:
            return ("No flame link is connected in this engine, so a purge "
                    "could reach nothing.")
        try:
            why = self._locks_ok() or ""
        except Exception as e:                           # noqa: BLE001
            why = f"the lock state could not be read ({type(e).__name__})."
        if why:
            return why
        return ""

    def press_live(self, now=None):
        """True while a fire press is going (a heartbeat within
        BEAT_STALE_S)."""
        now = self._clock() if now is None else now
        with self._lock:
            f = self.fire
            return f is not None and now - f["beat"] <= BEAT_STALE_S

    def select(self, machine_id, on, device, seen_ok):
        """A click on a machine (or a group button's click on each). (code,
        body). The first click of a session owns it; another device's click
        is refused while this one is live."""
        cfg = self._cfg()
        m = cfg.by_id.get(machine_id)
        if m is None:
            return 400, {"error": f"There is no machine called "
                                  f"{machine_id!r}."}
        if not on:
            with self._lock:
                if machine_id in self.selected:
                    self.selected.remove(machine_id)
                if not self.selected:
                    self.owner = None
                    self.fire = None
            self._note(f"{device} took {m.name} out of the purge selection.",
                       action="purge", outcome="deselected", screen=device)
            return 200, {"ok": True, "text": f"{m.name} off the purge.",
                         "selected": list(self.selected)}
        never = cfg.purge_refusal(m)
        if never in NEVER_SELECTED:
            # Never armed for a purge at all: its group is not asked for.
            return 409, {"error": f"{m.name} is never purged: {never}. "
                                  f"Nothing was armed."}
        why = self._refusal(device, seen_ok)
        if why:
            return 409, {"error": why}
        if self._lockout.is_locked("machine", machine_id):
            return 409, {"error": f"{m.name} is locked out, so it cannot "
                                  f"be armed for a purge. Unlock it first."}
        if self.press_live():
            # A machine never joins a press that is already firing (review
            # of PR #68, P1-1): a Waver's head-up time is counted from the
            # moment it is selected AND armed, and a G-Flame joining a live
            # press would fire on the next beat with nobody having pressed
            # for it. Let go, click, press again.
            return 409, {"error": f"A purge is firing right now. Let go of "
                                  f"the key, then click {m.name}."}
        with self._lock:
            if self.owner is not None and self.owner != device and \
                    self.selected:
                return 409, {"error": f"{self.owner} has the purge "
                                      f"selection. One device at a time."}
            if machine_id not in self.selected:
                self.selected.append(machine_id)
            self.owner = device
        self._note(f"{device} armed {m.name} for a purge (group {m.group}). "
                   f"The Stream Deck process asks flamesafe to arm the "
                   f"group; it fires only while Space or FIRE is held.",
                   action="purge", outcome="selected", screen=device)
        return 200, {"ok": True, "text": f"{m.name} armed for the purge. "
                                         f"Hold Space (or the deck's FIRE "
                                         f"key) to fire.",
                     "selected": list(self.selected)}

    def groups_wanted(self):
        """flamesafe group indices the selection needs armed, for the deck.
        A locked machine, or one in a fully locked group, never counts,
        and neither does a fireball (never purged, never armed for one)."""
        cfg = self._cfg()
        names = list(self._names() or [])
        want = set()
        with self._lock:
            sel = list(self.selected)
        for mid in sel:
            m = cfg.by_id.get(mid)
            if m is None or self._lockout.is_locked("machine", mid):
                continue
            if m.group in cfg.fireball_groups or \
                    cfg.purge_refusal(m) in NEVER_SELECTED:
                continue
            if m.group in names:
                want.add(names.index(m.group))
        return sorted(want)

    def _armed_groups(self):
        fl = self._flames() or {}
        if fl.get("stale") or not fl.get("connected"):
            return set()
        return {g["name"] for g in fl.get("groups", [])
                if g.get("armed") == "armed"}

    def frame(self, now):
        """The 512 channel purge frame for this heartbeat: fire values for
        selected, unlocked machines whose group flamesafe reports ARMED; a
        Waver's head up first (pan and tilt at once, fire only after
        head_settle_s of continuous heartbeats counted PER WAVER from the
        first beat on which it is both selected and armed, never from the
        press's start: a group that arms late in a press waits its full
        settle time, review of PR #68, P1-1). A settle time of 0 fires on
        that same first beat, head and fire in one frame. Also the machines
        left out and why, for the page."""
        cfg = self._cfg()
        vals = bytearray(UNIVERSE_SIZE)
        armed = self._armed_groups()
        firing, waiting = [], []
        with self._lock:
            sel = list(self.selected)
            f = self.fire
            heads = f["heads"] if f is not None else {}
            for mid in sel:
                m = cfg.by_id.get(mid)
                if m is None:
                    continue
                no_map = cfg.purge_refusal(m)
                if no_map:
                    # Never purged and never raises: nothing at all is
                    # written for it, and the page says why.
                    heads.pop(mid, None)
                    waiting.append(f"{m.name}: {no_map}")
                    continue
                if self._lockout.is_locked("machine", mid):
                    heads.pop(mid, None)
                    waiting.append(f"{m.name}: locked out")
                    continue
                if m.group not in armed:
                    # Its head clock starts over: a group that drops out of
                    # ARMED mid press (a deck tap, flamesafe holding it)
                    # lets the head fall, so the next arm waits the full
                    # settle time again (review of PR #68, P2-10).
                    heads.pop(mid, None)
                    waiting.append(f"{m.name}: group {m.group} is not armed")
                    continue
                p = cfg.purge
                if m.kind == "waver":
                    w = p["waver"]
                    vals[m.pan - 1] = w["pan_up"]
                    vals[m.tilt - 1] = w["tilt_up"]
                    since = heads.setdefault(m.id, now)
                    if now - since < w["head_settle_s"]:
                        waiting.append(f"{m.name}: head going up "
                                       f"({w['head_settle_s']:g} s)")
                        continue
                    fire = w["fire"]
                elif m.kind == "fireball":
                    # Belt (purge_refusal above is the rule): one value on
                    # every slot of a fireball is never written.
                    waiting.append(f"{m.name}: {NO_FIREBALL_PURGE}")
                    continue
                else:
                    fire = p["gflame_fire"]
                for slot in m.fire:
                    vals[slot - 1] = fire
                firing.append(m.name)
        return bytes(vals), firing, waiting

    def fire_beat(self, body, device, seen_ok, deck=False):
        """One fire heartbeat (every 100 ms while the key is down). The
        first call (no fire_id) starts a press; later ones carry its id.
        Any refusal ends the press. Only a body purge_fire_wanted() says
        asks to fire gets past the first line: a release can only stop."""
        if not purge_fire_wanted(body):
            stopped = self.stop("a release reached the fire heartbeat",
                                device=device)
            return 409, {"error": "That press asked to let go, not to "
                                  "fire. Nothing was fired"
                                  f"{' and the press was stopped' if stopped else ''}.",
                         "let_go": True}
        why = self._refusal(device, seen_ok)
        with self._lock:
            sel = list(self.selected)
            owner = self.owner
        if not why and not sel:
            why = "Nothing is armed for a purge. Click a machine first."
        if not why:
            # Every machine selected is one that is never purged (a wave
            # flamer with no pan, tilt and fire map): a clean refusal.
            cfg = self._cfg()
            ms = [cfg.by_id.get(mid) for mid in sel]
            ms = [m for m in ms if m is not None]
            if ms and all(cfg.purge_refusal(m) for m in ms):
                why = ("Nothing selected can be purged: " +
                       "; ".join(f"{m.name}: {cfg.purge_refusal(m)}"
                                 for m in ms) + ".")
        if not why and not deck and owner != device:
            why = (f"{owner} has the purge selection; only that device or "
                   f"the Stream Deck fires it.")
        link = self._link()
        if why or link is None:
            # A refusal ends the press it belongs to (the owner's, or the
            # deck's); another device's refused heartbeat changes nothing.
            with self._lock:
                f = self.fire
                mine = f is None or f["device"] == (
                    "Stream Deck" if deck else device)
            if mine:
                self.stop(f"refused: {why}")
            return 409, {"error": why or "No flame link.", "let_go": True}
        now = self._clock()
        fid = body.get("fire_id")
        with self._lock:
            f = self.fire
            if fid is not None:
                if f is not None and f["device"] != device:
                    # Another device's heartbeat never carries this press
                    # on, and never ends it either: refused, nothing else.
                    return 409, {"error": f"{f['device']} is firing; only "
                                          f"that device carries it on.",
                                 "let_go": True}
                live = (f is not None and f["id"] == fid and
                        now - f["beat"] <= BEAT_STALE_S)
                if not live:
                    self.fire = None
                    link.purge_clear()
                    return 409, {"error": "The fire press was interrupted. "
                                          "Release the key and press again.",
                                 "let_go": True}
                f["beat"] = now
            else:
                if f is not None and now - f["beat"] <= BEAT_STALE_S and \
                        f["device"] != device:
                    return 409, {"error": f"{f['device']} is already firing.",
                                 "let_go": True}
                self._fire_ids += 1
                f = self.fire = {"id": self._fire_ids, "device": device,
                                 "start": now, "beat": now, "heads": {}}
                self.fires += 1
        vals, firing, waiting = self.frame(now)
        with self._lock:
            if self.fire is not f:
                # A stop (the key came up, a watcher pass, an Abort) landed
                # while the frame was being worked out: it wins, and this
                # beat sets nothing (review of PR #68, P2-7).
                link.purge_clear()
                return 409, {"error": "The fire press ended. Release the "
                                      "key and press again.",
                             "let_go": True}
            if any(vals):
                link.purge_set(vals)
            else:
                link.purge_clear()
        if fid is None:
            self._note(f"{device} started a purge fire press: "
                       f"{', '.join(firing) or 'nothing firing yet'}"
                       f"{'; waiting: ' + '; '.join(waiting) if waiting else ''}.",
                       action="purge", outcome="fire", screen=device)
        return 200, {"ok": True, "fire_id": f["id"], "firing": firing,
                     "waiting": waiting,
                     "held_s": round(now - f["start"], 2)}

    def stop(self, why="", device=None):
        """The key came up (or anything else ended the press): the flame
        link's purge frame is cleared now, not at its own expiry."""
        with self._lock:
            f, self.fire = self.fire, None
        link = self._link()
        if link is not None:
            link.purge_clear()
        if f is not None:
            held = self._clock() - f["start"]
            self._note(f"Purge fire ended after {held:.1f} s"
                       f"{': ' + why if why else ''}.", action="purge",
                       outcome="fire_end", screen=device or f["device"])
        return f is not None

    def clear(self, why):
        """Everything off: the selection and any fire. The deck sees no
        groups wanted on its next read and disarms them."""
        self.stop(why)
        with self._lock:
            had = list(self.selected)
            self.selected = []
            self.owner = None
        if had:
            self._note(f"Purge selection cleared ({why}): "
                       f"{', '.join(had)}.", action="purge",
                       outcome="cleared")
        return had

    def tick(self, now, page_open):
        """Every 100 ms from the watcher (or the test): a fire press with no
        heartbeat for BEAT_STALE_S is over; a selection with no open page,
        a live show or an Abort standing is cleared."""
        with self._lock:
            f = self.fire
            stale = f is not None and now - f["beat"] > BEAT_STALE_S
        if stale:
            self.stop("no heartbeat for 0.25 s")
        if self.selected:
            why = self._live()
            if why:
                self.clear(why)
            elif not page_open:
                self.clear("the Morning and Shutdown page is no longer open")

    def view(self):
        wanted = self.groups_wanted()        # takes the lock itself
        with self._lock:
            f = self.fire
            return {"selected": list(self.selected), "owner": self.owner,
                    "firing": f is not None, "fire_device":
                        f["device"] if f else None,
                    "groups_wanted": wanted, "fires": self.fires}


# ------------------------------------------------------------- the facade --

class Morning:
    """Everything the page's routes need, built by web.serve for Fire & Ice
    and by the selftest with fakes. `flames_get` returns the remote's
    FlameStatus view, `link_get` the flame link, `live_check` a sentence
    while a show is live or an Abort stands (else ""), `group_names` the
    flamesafe group names in config order."""

    def __init__(self, folder, flamesafe_config=None, link_get=lambda: None,
                 flames_get=lambda: None, live_check=lambda: "",
                 group_names=None, journal=None, night=None,
                 clock=time.monotonic, wall=time.time, laser_host=None,
                 laser_port=None, logs_dir=None, socket_factory=None,
                 threaded=True, transport=None, laser_output=lambda: None,
                 laser_blockers=lambda: "", laser_blank=None,
                 night_state=lambda: None, audit=None):
        from . import checklist as checklist_mod
        self.folder = folder
        self._journal = journal
        self._clock = clock
        self._wall = wall
        self.flamesafe_config = flamesafe_config
        groups = None
        self.config_error = ""
        if flamesafe_config:
            try:
                groups = load_flamesafe_groups(flamesafe_config)
            except (OSError, ValueError) as e:
                self.config_error = str(e)
        self.groups = groups or []
        self._group_names = group_names or (lambda: [g["name"] for g in
                                                     self.groups])
        self.config_path = os.path.join(folder, CONFIG_FILE)
        try:
            self.cfg = MorningConfig.load(self.config_path, groups,
                                          laser_host, laser_port)
        except MorningConfigError as e:
            # A broken file: the page says so and runs on the defaults,
            # with the purge and laser tests refused (nothing fires on a
            # config that could not be read).
            self.config_error = str(e)
            self.cfg = MorningConfig.parse({}, CONFIG_FILE, groups,
                                           laser_host, laser_port)
        for line in self.cfg.journal_lines():
            if journal is not None:
                try:
                    journal(line, action="config", outcome="never-purged")
                except Exception:                # noqa: BLE001
                    pass
        self.layout = Layout(os.path.join(folder, LAYOUT_FILE))
        self.lockout = Lockout(os.path.join(folder, LOCKOUT_FILE),
                               journal=journal, wall=wall)
        self._link_get = link_get
        self._flames_get = flames_get
        self._live_check = live_check
        self.purge = Purge(lambda: self.cfg, self.lockout, link_get,
                           flames_get, self._live_or_broken, self._group_names,
                           journal=journal, clock=clock,
                           locks_ok=self._locks_confirmed)
        self._fs_lock_said = False   # the "flamesafe has no lockout_file" line
        self.lasers = lasertest.LaserTests(
            self.cfg.lasers, locked=lambda lid: self.lockout.is_locked(
                "laser", lid), journal=journal, socket_factory=socket_factory,
            clock=clock, armed=lambda lid: self.laser_arm.is_armed(lid))
        # The laser arm (Jeff, 2026-10-22; laserarm.py): every laser starts
        # disarmed, and BEYOND's laser output is switched off at once.
        self._night_state = night_state
        self._laser_blockers = laser_blockers
        self.laser_arm = laserarm.LaserArm(
            [(l.id, l.name) for l in self.cfg.lasers],
            groups=self.cfg.laser_groups,
            locked=lambda lid: self.lockout.is_locked("laser", lid),
            output=laser_output, blockers=self._arm_blockers,
            blank_now=laser_blank, per_laser=self.lasers, journal=journal,
            audit=audit, threaded=threaded, clock=clock, wall=wall)
        logs_dir = logs_dir or os.path.join(folder, "logs")
        self.checklist = checklist_mod.ChecklistService(
            self.cfg, os.path.join(logs_dir, "checklists"), night=night,
            journal=journal, wall=wall, clock=clock, transport=transport,
            threaded=threaded)
        self._open_at = None         # clock() of the page's last open beat
        self._open_devices = {}      # device -> clock of its last beat
        self._arranging = {}         # device -> {page instance: clock of
        #                              its last beat saying Arrange mode}
        self._beat_seq = {}          # (device, instance) -> (last beat
        #                              seq, clock it came)
        self._was_live = False       # the watcher's last word on live_check
        self._stop = threading.Event()
        self._thread = None
        self._fs_lockout_path = None
        self._lock_written_at = None   # clock() of this engine's last write
        self._lock_channels_to_link()
        self._check_lockout_paths()
        if threaded:
            self._thread = threading.Thread(target=self._watch, daemon=True,
                                            name="ltcplay-morning-watch")
            self._thread.start()

    # -- plumbing ------------------------------------------------------------
    def _note(self, text, **f):
        if self._journal is None:
            return
        try:
            self._journal(text, **f)
        except Exception:
            pass

    def _live_or_broken(self):
        if self.config_error:
            return (f"ltcplay_morning.json could not be read "
                    f"({self.config_error}), so nothing fires from this page "
                    f"until it is fixed.")
        lock_problem = self.lock_problem()
        if lock_problem:
            # What is locked out is not known for certain (review of PR
            # #68, P1-3, P1-5): nothing fires and no laser test lights
            # until it is.
            return f"{lock_problem}; nothing fires from this page until then."
        return self._live_sentence()

    def _arm_blockers(self):
        """Why no laser may be armed, nor BEYOND's laser output switched on,
        right now (laserarm.LaserArm's `blockers`): this page's settings or
        lockout cannot be trusted, then the engine's own (an Abort latch,
        a Hold, the night closing). "" when nothing blocks."""
        if self.config_error:
            return (f"ltcplay_morning.json could not be read "
                    f"({self.config_error})")
        why = self.lock_problem()
        if why:
            return why
        state, err = laserarm._ask(self._night_state)
        if err is None and state in laserarm.CLOSING_STATES:
            return "the night is closing"
        return self._laser_blockers() or ""

    def lasers_disarm_now(self, why, who="", screen=""):
        """Every laser disarmed on the caller's thread with nothing else
        done there (Abort, Hold): the blank and DisableLaserOutput go on the
        laser arm's sender."""
        return self.laser_arm.disarm_all(why, who, screen, defer=True)

    def _live_sentence(self):
        """live_check alone: a show live or an Abort standing, or ""."""
        try:
            return self._live_check() or ""
        except Exception as e:
            return f"the show state could not be read ({type(e).__name__})."

    def _flamesafe_extra(self):
        """Flame slots flamesafe reports locked that this engine cannot
        account for (review of PR #68, P1-5): the lockout file was deleted
        or replaced and flamesafe kept the last set (missing_kept), or this
        engine started with no file while flamesafe still holds one.
        Writing the file from this table would lift them silently, so
        until it is resolved nothing is written and nothing fires."""
        if self._lock_written_at is not None and \
                self._clock() - self._lock_written_at < LOCK_SETTLE_S:
            # This engine just wrote the file: flamesafe reads it within
            # LOCKOUT_POLL_S, and until then it still reports the old set.
            # Not a problem for that long (review of PR #68, P3-12), or
            # every unlock would clear a selection and end a laser test.
            return []
        fl = self._flames_get() or {}
        lk = fl.get("lockout") if isinstance(fl.get("lockout"), dict) else None
        if lk is None or fl.get("stale"):
            return []
        try:
            slots = {int(s) for s in lk.get("slots") or []}
        except (TypeError, ValueError):
            return []
        return sorted(slots - self.lockout.channels(self.cfg))

    def lock_problem(self):
        """Why the lock state cannot be trusted right now, or "": the
        table's own problems (an unreadable file, ids the config lacks, a
        config changed under a lock) and flamesafe holding slots this engine
        does not know about."""
        why = self.lockout.problem(self.cfg)
        if why:
            return why
        extra = self._flamesafe_extra()
        if extra:
            return (f"flamesafe holds flame slots {extra} locked that this "
                    f"engine does not know about (its lockout file was "
                    f"deleted or replaced, or the engine was restarted "
                    f"without it); restore the file, or on the show machine "
                    f"use 'Rewrite the lockout file' to lift them "
                    f"deliberately")
        return ""

    def _locks_confirmed(self):
        """"" while flamesafe reports the very slots this engine has locked
        (or this engine has locked no machine); else why a purge must wait.
        flamesafe is the enforcement; a purge never goes while it has not
        taken the lock (review of PR #68, P2-2)."""
        want = sorted(self.lockout.channels(self.cfg))
        if not want:
            return ""
        fl = self._flames_get() or {}
        lk = fl.get("lockout") if isinstance(fl.get("lockout"), dict) else None
        if lk is None or fl.get("stale"):
            names = [self.cfg.by_id[m].name for m in self.lockout.machines
                     if m in self.cfg.by_id]
            return ("flamesafe has not said which flame slots it holds "
                    "locked (no lockout_file in its config, an old flamesafe, "
                    "or its status is stale), so the lock on "
                    f"{', '.join(names)} is not confirmed; nothing fires from "
                    "this page until it is.")
        if sorted(lk.get("slots") or []) != want:
            return (f"flamesafe holds slots {sorted(lk.get('slots') or [])} "
                    f"locked but this engine asked for {want}; nothing fires "
                    f"from this page until they agree.")
        return ""

    def device_open(self, device):
        """True while THAT device's page has beaten within OPEN_STALE_S (the
        purge selection lives only while its owner's page is open, review of
        PR #68, P2-1). No device named: any open page."""
        if device is None:
            return self.page_open()
        at = self._open_devices.get(device)
        return at is not None and self._clock() - at <= OPEN_STALE_S

    def _lock_channels_to_link(self):
        """The flame link's belt: every slot the table resolves to, AND
        every slot the file itself names (so a machine the config no longer
        knows is still zeroed here while the problem stands)."""
        link = self._link_get()
        if link is not None and hasattr(link, "set_locked_channels"):
            try:
                link.set_locked_channels(
                    self.lockout.channels(self.cfg)
                    | set(self.lockout.file_channels or ()))
            except Exception:
                pass

    def page_open(self):
        at = self._open_at
        return at is not None and self._clock() - at <= OPEN_STALE_S

    def open_beat(self, device, arranging=False, inst="", seq=None,
                  mark_open=True):
        """The page says it is open (every 500 ms while it is), and whether
        it is in Arrange mode (Jeff, 2026-10-19). A screen that is
        arranging has nothing armed: its purge selection is cleared here,
        and arrange_refusal() holds its arming routes back until a beat
        says it has left Arrange mode (or its beats stop).

        Every page load is its own instance (`inst`, made once by the page):
        the show PC's windows are all "Rack screen", so one window's
        ordinary beat must never clear another window's Arrange mode
        (adversarial review of PR #73, finding 1). The device is held back
        while ANY of its instances is arranging. `seq` numbers an
        instance's beats: one that arrives after a later one (separate
        connections) says nothing about Arrange mode (finding 6).
        `mark_open` False: a device that may not purge reads the page but
        does not hold it open; its Arrange mode still counts (finding 2)."""
        now = self._clock()
        if mark_open:
            self._open_at = now
            self._open_devices[device] = now
            for d in [d for d, t in self._open_devices.items()
                      if now - t > OPEN_STALE_S]:
                del self._open_devices[d]
        for d in list(self._arranging):
            live = {i: t for i, t in self._arranging[d].items()
                    if now - t <= OPEN_STALE_S}
            if live:
                self._arranging[d] = live
            else:
                del self._arranging[d]
        for k in [k for k, (_q, t) in self._beat_seq.items()
                  if now - t > 60.0]:
            del self._beat_seq[k]        # page loads long gone
        inst = str(inst or "")[:64]
        key = (device, inst)
        if isinstance(seq, int) and not isinstance(seq, bool):
            last = self._beat_seq.get(key)
            if last is not None and seq <= last[0]:
                return                   # an older beat, overtaken
            self._beat_seq[key] = (seq, now)
        if arranging:
            first = device not in self._arranging
            self._arranging.setdefault(device, {})[inst] = now
            if self.purge.owner == device and (self.purge.selected or
                                               self.purge.fire is not None):
                self.purge.clear(f"{device} went into Arrange mode")
            if self.purge.fire is not None:
                # Whoever is firing, from whatever surface (the deck's FIRE
                # key included): the press ends now, the same stop as the
                # key coming up (Jeff, 2026-10-21: "If it's being arranged
                # in any capacity, firing is disabled"). Its next heartbeat
                # meets fire_arrange_refusal() and is told why.
                self.purge.stop(f"{device} went into Arrange mode, and "
                                f"nothing fires while any screen is "
                                f"arranging")
            if first:
                self._note(f"{device} is arranging the machine layout: "
                           f"nothing arms or unlocks from it, and nothing "
                           f"fires from any surface, until it saves or "
                           f"cancels.", action="layout",
                           outcome="arranging", screen=device)
        else:
            mine = self._arranging.get(device)
            if mine is not None:
                mine.pop(inst, None)
                if not mine:
                    del self._arranging[device]

    def group_locked_out(self, name):
        """True when every fire slot of flamesafe's group `name` is locked
        out (flamesafe then holds it disarmed, composer.LOCKED_OUT): a
        screen hold on it is refused with that sentence before it fills.
        False when the group or its slots are not known, or the lock table
        cannot be trusted (flamesafe still holds a locked group)."""
        if self.lockout.problem(self.cfg):
            return False
        for g in self.groups:
            if g["name"] == name:
                fire = set(g["fire"])
                locked = self.lockout.channels(self.cfg)
                # A multi-safety group (2026-10-26) with every safety slot
                # locked is held too (composer.SAFETIES_LOCKED).
                saf = set(g.get("safeties") or [])
                return (bool(fire) and fire <= locked) or \
                    (len(saf) > 1 and saf <= locked)
        return False

    def device_arranging(self, device):
        """True while ANY instance of THAT device said, in its last open
        beat within OPEN_STALE_S, that the page is in Arrange mode."""
        now = self._clock()
        return any(now - t <= OPEN_STALE_S
                   for t in (self._arranging.get(device) or {}).values())

    def arranging_devices(self):
        """Every device with ANY instance whose last open beat within
        OPEN_STALE_S said it is in Arrange mode, sorted. An entry whose
        beats stopped (a crashed browser, a closed window) drops out after
        OPEN_STALE_S by the clock alone, with no further beat needed."""
        now = self._clock()
        return sorted(d for d, insts in list(self._arranging.items())
                      if any(now - t <= OPEN_STALE_S
                             for t in list(insts.values())))

    def fire_arrange_refusal(self):
        """The sentence that refuses ANY purge fire heartbeat while any
        screen or window anywhere is in Arrange mode, or "" (Jeff,
        2026-10-21: "If it's being arranged in any capacity, firing is
        disabled"). Every surface meets it, the Stream Deck's FIRE key
        included, whoever owns the selection. If the Arrange state cannot
        be read, nothing fires."""
        try:
            who = self.arranging_devices()
        except Exception as e:                           # noqa: BLE001
            return (f"Firing is off: whether a screen is in Arrange mode "
                    f"could not be read ({type(e).__name__}). Nothing was "
                    f"fired.")
        if not who:
            return ""
        names = " and ".join(who)
        return (f"Firing is off: {names} {'is' if len(who) == 1 else 'are'} "
                f"in Arrange mode on the Startup page, and "
                f"nothing fires while any screen is arranging. Press Save "
                f"layout or Cancel there (a page that has closed lets go "
                f"by itself within {OPEN_STALE_S:g} s). Nothing was fired.")

    def arrange_refusal(self, name, body, device):
        """The sentence that refuses `name` from a device in Arrange mode,
        or "". Arming a machine, a laser test going ON, arming a laser, an
        unlock and a rewrite of the lockout file are held back from the
        arranging device; every fire heartbeat is held back from EVERY
        device while any screen arranges (fire_arrange_refusal). What only
        takes risk away is never held back by Arrange mode, from any
        device: a select or laser test going OFF, a laser disarm, a
        lockout, a stop and a clear."""
        if name == "purge-fire":
            return self.fire_arrange_refusal()
        off = body.get("on") is False
        gated = (name == "lock" and (off or body.get("kind") == "rewrite")) \
            or (name in ("purge-select", "laser-test") and not off) \
            or (name == "laser-arm" and body.get("on") is True)
        if not gated or not self.device_arranging(device):
            return ""
        return (f"{device} is in Arrange mode on the Startup "
                f"page: nothing can be armed, fired or unlocked from it "
                f"until Save layout or Cancel. Nothing was done.")

    def tick(self, now=None):
        """The watcher's pass, 10 a second: purge expiry, laser tests off
        when a show goes live or an Abort stands, the link's lock set."""
        now = self._clock() if now is None else now
        try:
            # The laser arm FIRST, so nothing below that raises can skip
            # it (review of PR #87, round 2, P3-6): the night closing
            # disarms every laser (ruling 4), as does a blocker appearing.
            state, err = laserarm._ask(self._night_state)
            self.laser_arm.tick(None if err else state)
            if not self.laser_arm._threaded:
                self.laser_arm.run_pending()
        except Exception:
            pass
        if self.purge.fire is not None:
            # The belt under open_beat's stop: a press that began in the
            # same instant a screen went into Arrange mode ends here.
            held = self.fire_arrange_refusal()
            if held:
                try:
                    self.purge.stop(held)
                except Exception:
                    pass
        try:
            self.purge.tick(now, self.device_open(self.purge.owner))
        except Exception as e:
            self._note(f"The purge watcher raised {type(e).__name__}: {e}; "
                       f"the purge was cleared.", fault=True, action="purge",
                       outcome="error")
            try:
                self.purge.clear("watcher error")
            except Exception:
                pass
        if self.lasers.on:
            why = self._live_or_broken()
            if why:
                self.lasers.all_off(why)
        live = bool(self._live_sentence())
        if live and not self._was_live:
            # A show going live (or an Abort landing) while a laser is
            # locked out: its lock messages go again (MORNING.md: "at every
            # show start while locked"), so BEYOND hears the lock on the
            # night it matters, not only on the morning it was set.
            for lid in list(self.lockout.lasers):
                self.lasers.send_lock(lid, "the show is going live")
        self._was_live = live
        self._lock_channels_to_link()
        self._check_flamesafe_lockout()
        try:
            self.checklist.tick(now)
        except Exception:
            pass

    def _check_flamesafe_lockout(self):
        """Said once (a fault line in the journal): this engine has a machine
        locked out but flamesafe reports no lockout file at all, so the lock
        is not enforced where the wire is written (review of PR #68, P2-2).
        The purge is refused meanwhile (_locks_confirmed)."""
        if self._fs_lock_said:
            return
        fl = self._flames_get() or {}
        lk = fl.get("lockout") if isinstance(fl.get("lockout"), dict) else None
        if lk is None or fl.get("stale"):
            return
        state = lk.get("state")
        if state == "none" and self.lockout.machines:
            self._fs_lock_said = True
            self._note("flamesafe has NO lockout_file in its config, so the "
                       "machines locked out here are not held at zero by "
                       "flamesafe. Set lockout_file in flamesafe.json to this "
                       f"engine's {self.lockout.path} (the default when both "
                       "files sit in the same folder) and restart flamesafe. "
                       "Nothing fires from the Startup page until it "
                       "confirms the lock.", fault=True, action="lockout",
                       outcome="unconfirmed")
        elif state == "missing" and self.lockout.machines:
            self._fs_lock_said = True
            self._note("flamesafe reads a lockout file that does not exist, "
                       f"while this engine writes {self.lockout.path}: its "
                       f"lockout_file points elsewhere"
                       f"{' (' + self._fs_lockout_path + ')' if self._fs_lockout_path else ''}"
                       ". The machines locked out here are not held at zero "
                       "by flamesafe. Nothing fires from the Startup page "
                       "until it confirms the lock.", fault=True,
                       action="lockout", outcome="unconfirmed")
        elif state == "missing_kept":
            self._fs_lock_said = True
            self._note(f"flamesafe is holding flame slots "
                       f"{sorted(lk.get('slots') or [])} locked from a lockout "
                       f"file that has disappeared; this engine's table knows "
                       f"{sorted(self.lockout.channels(self.cfg))}. Restore "
                       f"the file, or rewrite it deliberately on the show "
                       f"machine.", fault=True, action="lockout",
                       outcome="unconfirmed")

    def _flamesafe_lockout_path(self):
        """The lockout file flamesafe will read, worked out from its config
        by the same rule flamesafe uses (left out: beside the config; false:
        none), or None when it cannot be told. Read as plain JSON; ltcplay
        never imports flamesafe."""
        if not self.flamesafe_config:
            return None
        try:
            with open(self.flamesafe_config, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        got = doc.get("lockout_file") if isinstance(doc, dict) else None
        if got is False:
            return ""
        if got is None:
            got = LOCKOUT_FILE
        if not isinstance(got, str) or not got.strip():
            return None
        got = got.strip()
        if not os.path.isabs(got):
            got = os.path.join(os.path.dirname(os.path.realpath(
                self.flamesafe_config)), got)
        return got

    def _check_lockout_paths(self):
        """Said once at start (review of PR #68, P2-12): flamesafe will read
        another file than the one this engine writes, so no lock set here
        can ever be confirmed there."""
        self._fs_lockout_path = self._flamesafe_lockout_path()
        p = self._fs_lockout_path
        if p is None:
            return
        mine = os.path.realpath(self.lockout.path)
        if p == "":
            self._note("flamesafe's config switches its lockout_file off "
                       "(false): nothing locked out on the Startup page is "
                       "held at zero by flamesafe, and no purge can go while "
                       "anything is locked.", fault=True, action="lockout",
                       outcome="unconfirmed")
        elif os.path.realpath(p) != mine:
            self._note(f"flamesafe reads its lockout file at {p} but this "
                       f"engine writes {mine}: a lock set here is never seen "
                       f"there. Set lockout_file in flamesafe.json to the "
                       f"engine's path (or keep both files in one folder).",
                       fault=True, action="lockout", outcome="unconfirmed")

    def _watch(self):
        while not self._stop.wait(0.1):
            try:
                self.tick()
            except Exception:
                pass

    def on_abort(self, who="", screen=""):
        """Abort wins over everything: the purge and the laser tests end."""
        self.purge.clear(f"Abort pressed by {who or screen or 'someone'}")
        self.lasers.all_off("Abort pressed")

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        try:
            self.purge.clear("the engine is stopping")
        finally:
            try:
                self.laser_arm.close()
            finally:
                self.lasers.close()
                self.checklist.close()

    # -- what deck-input and status carry -----------------------------------
    def deck_view(self):
        open_ = self.page_open()
        names = list(self._group_names() or [])
        fully = []
        for i, n in enumerate(names):
            ms = [m for m in self.cfg.machines if m.group == n]
            if ms and all(self.lockout.is_locked("machine", m.id)
                          for m in ms):
                fully.append(i)
        pv = self.purge.view()
        return {"open": open_,
                "purge": {"wanted_groups": pv["groups_wanted"] if open_
                          else [], "selected": len(pv["selected"]),
                          "firing": pv["firing"]},
                "locks": {"groups_fully_locked": fully,
                          "machines": len(self.lockout.machines),
                          "lasers": len(self.lockout.lasers)}}

    def status_view(self):
        """The compact part every screen shows: the lock banner and whether
        a purge or a laser test is going."""
        um, ul = self.lockout.unknown(self.cfg)
        lm = [self.cfg.by_id[m].name for m in self.lockout.machines
              if m in self.cfg.by_id] + [f"{m} (not in ltcplay_morning.json)"
                                        for m in um]
        ll = [l.name for l in self.cfg.lasers if l.id in self.lockout.lasers] \
            + [f"{l} (not in ltcplay_morning.json)" for l in ul]
        fl = self._flames_get() or {}
        fs_slots = (fl.get("lockout") or {}).get("slots")
        want = sorted(self.lockout.channels(self.cfg))
        confirmed = (fs_slots == want) if isinstance(fs_slots, list) else None
        return {"locked_machines": lm, "locked_lasers": ll,
                "flamesafe_confirmed": confirmed,
                "lock_error": self.lock_problem(),
                "flamesafe_extra": self._flamesafe_extra(),
                "purge": self.purge.view()["firing"] or bool(
                    self.purge.selected),
                "laser_tests_on": sorted(self.lasers.on),
                "laser_arm": self.laser_arm.status_view(),
                "open": self.page_open(), "config_error": self.config_error}

    # -- the routes ---------------------------------------------------------
    def full_view(self, ctx_device):
        pos, saved = self.layout.load(self.cfg)
        v = self.cfg.view()
        v["layout"] = pos
        v["layout_saved"] = saved
        v["lockout"] = self.lockout.view(self.cfg)
        v["purge_state"] = self.purge.view()
        v["lasers"] = self.lasers.view()
        v["laser_arm"] = self.laser_arm.view()
        v["checklists"] = self.checklist.view()
        v["config_error"] = self.config_error
        v["flamesafe_groups"] = list(self._group_names() or [])
        v["live_refusal"] = self._live_or_broken()
        v["open"] = self.page_open()
        v["me"] = ctx_device
        return v

    def post(self, name, body, device, who, screen, seen_ok, local):
        """(code, body) for the page's POST routes. `screen` is the surface
        the engine gave the press (Rack screen, iPad at ..., or "Stream
        Deck" for the deck process over loopback, which the purge fire
        trusts as the deck whatever operator name it carries)."""
        raw = body
        body = body if isinstance(body, dict) else {}
        if name == "purge-fire" and not purge_fire_wanted(raw):
            # A release ("on" false, missing, null, 0, "false", anything but
            # the JSON word true): it can only STOP a press, never start
            # one, so it never reaches fire_beat. It is the same stop as
            # purge-stop, with the same owner rule (found on PR #82: a body
            # with "on" false fired 240 on slot 411 with purge arming off).
            name = "purge-stop"
        if name == "morning-open":
            self.open_beat(device, arranging=body.get("arranging") is True,
                           inst=body.get("inst"), seq=body.get("seq"),
                           mark_open=body.get("leaving") is not True)
            return 200, {"ok": True, "open": True,
                         "arranging": self.device_arranging(device)}
        # A screen in Arrange mode (Jeff, 2026-10-19): the engine's own
        # refusal, whatever the page let through. A fire heartbeat is
        # refused while ANY screen or window arranges, from every surface,
        # the Stream Deck's FIRE key included (Jeff, 2026-10-21: "If it's
        # being arranged in any capacity, firing is disabled"), and a press
        # still going ends with it.
        why = self.arrange_refusal(name, body, device)
        if why:
            if name == "purge-fire":
                self.purge.stop(f"refused: {why}")
            return 409, {"error": why, "let_go": True}
        if name == "purge-select":
            on = body.get("on") is not False
            if isinstance(body.get("machines"), list):
                out, last = [], (200, {"ok": True})
                for mid in body["machines"]:
                    last = self.purge.select(str(mid), on, device, seen_ok)
                    out.append(last)
                    if last[0] != 200:
                        break
                return last[0], dict(last[1], selected=list(
                    self.purge.selected))
            return self.purge.select(str(body.get("machine")), on, device,
                                     seen_ok)
        if name == "purge-fire":
            # The deck process names itself in `screen` (streamdeck.PurgeFire
            # sends screen "Stream Deck" and the operator's name in `who`),
            # and only over loopback (review of PR #68, P1-2).
            deck = local and screen == "Stream Deck"
            return self.purge.fire_beat(body, device if not deck else
                                        "Stream Deck", seen_ok, deck=deck)
        if name in ("purge-stop", "purge-clear"):
            # Only the owner's device, or the show machine itself, ends the
            # owner's purge (review of PR #68, P2-11): another device's page
            # leaving the view must not read as a fault at the rack. Abort
            # stays the one path everyone has.
            pv = self.purge.view()
            mine = local or device == pv["owner"] or \
                (name == "purge-stop" and device == pv["fire_device"])
            if not mine and (pv["selected"] or pv["firing"]):
                return 409, {"error": f"{pv['owner']} has the purge "
                                      f"selection; only that device or the "
                                      f"show machine itself can "
                                      f"{'stop' if name == 'purge-stop' else 'clear'} "
                                      f"it (or Abort)."}
            if name == "purge-stop":
                self.purge.stop(f"{device} let go", device=device)
                return 200, {"ok": True}
            self.purge.clear(f"{device} cleared the selection")
            return 200, {"ok": True, "text": "Purge selection cleared."}
        if name == "laser-test":
            on = body.get("on") is True
            if on:
                why = self._live_or_broken()
                if why:
                    return 409, {"error": f"Laser tests are not allowed now: "
                                          f"{why}"}
            if body.get("all") is True:
                # Calibrate all lasers / Calibration off (Jeff,
                # 2026-10-26): every armed, unlocked laser in turn, each
                # through the one-laser rules; the show state is asked
                # again before each one.
                ok, text = self.lasers.set_all(
                    on, who=who, device=device,
                    still_ok=self._live_or_broken if on else None)
                return (200 if ok else 409), ({"ok": True, "text": text}
                                              if ok else {"error": text})
            ok, text = self.lasers.set(str(body.get("laser")), on,
                                       who=who, device=device)
            return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                          else {"error": text})
        if name == "laser-arm":
            return self._laser_arm_post(body, who, device)
        if name == "lock":
            kind = body.get("kind")
            if kind == "rewrite":
                # The one deliberate way out of a lock problem (review of
                # PR #68, P1-5): the show machine itself, confirmed.
                if not local:
                    return 403, {"error": "Rewriting the lockout file is "
                                          "done on the show machine itself."}
                if body.get("confirmed") is not True:
                    return 409, {"error": "Rewriting the lockout file needs a "
                                          "confirm. Nothing was changed."}
                if self.config_error:
                    return 409, {"error": f"Not while ltcplay_morning.json "
                                          f"cannot be read "
                                          f"({self.config_error})."}
                ok, text = self.lockout.rewrite(self.cfg, who, device)
                if ok and self.lockout.wrote:
                    self._lock_written_at = self._clock()
                self._lock_channels_to_link()
                return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                              else {"error": text})
            if kind not in ("machine", "laser"):
                return 400, {"error": "kind has to be machine, laser or "
                                      "rewrite."}
            uid = str(body.get("id") or "")
            on = body.get("on") is not False
            why = self.lock_problem()
            if why:
                return 409, {"error": f"Nothing was locked or unlocked: "
                                      f"{why}."}
            if self.config_error:
                # The machine list on this page is the default one, not
                # Andy's: a lock written against it could name the wrong
                # slots (review of PR #68, P1-3).
                return 409, {"error": f"Nothing was locked or unlocked: "
                                      f"ltcplay_morning.json could not be "
                                      f"read ({self.config_error}). Fix it "
                                      f"and restart the engine."}
            ok, text = self.lockout.set(self.cfg, kind, uid, on, who, device,
                                        reason=body.get("reason", ""),
                                        confirmed=body.get("confirmed"))
            if ok and self.lockout.wrote:
                # Only a real write of the file earns flamesafe's settle time
                # (LOCK_SETTLE_S): an "already locked" answer, or a save that
                # failed, must not keep hiding slots flamesafe holds beyond
                # this table (second review, P3-1).
                self._lock_written_at = self._clock()
            if ok and on:
                if kind == "machine" and uid in self.purge.selected:
                    self.purge.select(uid, False, device, True)
                if kind == "laser":
                    self.laser_arm.on_lock(uid, who, device)
                    self.lasers.send_lock(uid, "locked out")
            self._lock_channels_to_link()
            return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                          else {"error": text})
        if name == "layout":
            if body.get("reset") is True:
                # The page's Reset layout: the file goes, the default
                # (each group on its own row) comes back.
                try:
                    self.layout.reset()
                except OSError as e:
                    return 400, {"error": f"Could not reset the layout: {e}"}
                pos, _saved = self.layout.load(self.cfg)
                self._note(f"{device} reset the machine layout to the "
                           f"default.", action="layout", outcome="reset",
                           screen=device)
                return 200, {"ok": True, "text": "Layout reset to the "
                             "default.", "layout": pos}
            try:
                pos = self.layout.save(self.cfg, body.get("positions"))
            except (ValueError, OSError) as e:
                return 400, {"error": str(e)}
            self._note(f"{device} saved the machine layout.", action="layout",
                       outcome="saved", screen=device)
            return 200, {"ok": True, "text": "Layout saved.", "layout": pos}
        if name.startswith("checklist-"):
            return self.checklist.post(name, body, device, screen, local)
        return 404, {"error": "no such thing here"}

    def _laser_arm_post(self, body, who, device):
        """POST laser-arm: {"on": true, "confirmed": true} with "laser",
        "lasers" or "group" arms (all or none); ANY other body disarms what
        it names, and with "all" (or naming nothing) every laser: a request
        that means disarm must never be able to arm (users.classify reads
        it the same way)."""
        arm = self.laser_arm
        ids = []
        if isinstance(body.get("lasers"), list):
            ids = [str(i) for i in body["lasers"]]
        elif body.get("laser") is not None:
            ids = [str(body.get("laser"))]
        elif body.get("group") is not None:
            g = arm.groups.get(str(body.get("group")))
            if g is None:
                if body.get("on") is True:
                    return 409, {"error": f"There is no laser group called "
                                          f"{body.get('group')!r}. Nothing "
                                          f"was armed."}
                ids = list(arm.order)
            else:
                ids = list(g)
        if body.get("on") is True:
            ok, text = arm.arm(ids, who=who, screen=device,
                               confirmed=body.get("confirmed"))
            return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                          else {"error": text})
        if body.get("all") is True or not ids:
            ids = list(arm.order)
        ok, text = arm.disarm(ids, "", who=who, screen=device)
        if not arm._threaded:
            arm.run_pending()
        return 200, {"ok": True, "text": text}
