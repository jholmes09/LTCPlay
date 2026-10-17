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

from . import lasertest

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
# The page says it is open every 250 ms; this long without it and the purge
# selection is cleared and the deck is told the page is closed.
OPEN_STALE_S = 2.0
NAME_MAX = 48
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")

TOP_KEYS = frozenset(("machines", "groups", "purge", "lasers", "checklists",
                      "names", "tally", "notes"))
MACHINE_KEYS = frozenset(("id", "name", "kind", "group", "fire", "pan",
                          "tilt", "notes"))
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
    """[{"name", "safety", "fire": [...]}] from flamesafe's config, read as
    plain JSON (ltcplay never imports flamesafe). ValueError on anything
    missing."""
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
        out.append({"name": g["name"], "safety": g.get("safety"),
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


class Machine:
    __slots__ = ("id", "name", "kind", "group", "fire", "pan", "tilt",
                 "bank", "notes")

    def __init__(self, id, name, kind, group, fire, pan=None, tilt=None,
                 bank=None, notes=""):
        self.id = id
        self.name = name
        self.kind = kind
        self.group = group
        self.fire = list(fire)
        self.pan = pan
        self.tilt = tilt
        self.bank = bank
        self.notes = notes

    def channels(self):
        """Every slot this machine owns: fire, and a Waver's pan and tilt."""
        out = set(self.fire)
        for c in (self.pan, self.tilt):
            if c is not None:
                out.add(c)
        return out

    def view(self):
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "group": self.group, "fire": list(self.fire),
                "pan": self.pan, "tilt": self.tilt, "bank": self.bank,
                "notes": self.notes}


def default_machines(groups):
    """One machine per fire slot of flamesafe's groups: a sensible default
    until Andy's own list is written into ltcplay_morning.json."""
    out = []
    for g in groups:
        kind = _kind_for(g["name"])
        bank = _bank_for(g["name"], kind)
        for n, slot in enumerate(g["fire"], 1):
            many = len(g["fire"]) > 1
            out.append(Machine(f"{_slug(g['name'])}-{n}" if many
                               else _slug(g["name"]),
                               f"{g['name']} {n}" if many else g["name"],
                               kind, g["name"], [slot], bank=bank))
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
                not 0 <= s <= HEAD_SETTLE_MAX_S:
            raise MorningConfigError(f"{where}: purge.waver.head_settle_s "
                                     f"has to be 0 to {HEAD_SETTLE_MAX_S:g} "
                                     f"seconds.")
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


def parse_machines(doc, where, groups):
    """The "machines" list, checked against flamesafe's groups when they
    are known: every slot a machine owns must be one of its group's fire
    slots, or flamesafe would zero it on every packet."""
    if not isinstance(doc, list) or not doc:
        raise MorningConfigError(f"{where}: machines has to be a list with "
                                 f"at least one machine, or be left out.")
    by_group = {g["name"]: set(g["fire"]) for g in (groups or [])}
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
        notes = d.get("notes", "")
        if not isinstance(notes, str):
            raise MorningConfigError(f"{w}: notes has to be text.")
        m = Machine(mid, name.strip(), kind, group, fire, pan, tilt,
                    notes=notes)
        for c in m.channels():
            if groups and c not in by_group[group]:
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


class MorningConfig:
    """ltcplay_morning.json, validated. Everything is optional: with no
    file at all the page shows one machine per flamesafe fire slot, Jeff's
    four groups, placeholder purge values, four undecided lasers, two empty
    checklists, no names and the tally off."""

    def __init__(self, machines, banks, purge, lasers, checklists, names,
                 tally, path=None, notes=""):
        self.machines = machines
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
        if "machines" in doc:
            machines = parse_machines(doc["machines"], where, groups)
        else:
            machines = default_machines(groups or [])
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
        return cls(machines, banks, purge, lasers, checklists, names, tally,
                   path=where if where != CONFIG_FILE else None, notes=notes)

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
        return {"machines": [m.view() for m in self.machines],
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

    def is_locked(self, kind, uid):
        return uid in (self.machines if kind == "machine" else self.lasers)

    def set(self, cfg, kind, uid, on, who, device, reason="",
            confirmed=False):
        """Lock or unlock one unit. (ok, sentence). Unlocking needs
        confirmed True: one deliberate confirm (Jeff). A lock never does."""
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
        return {"machines": {k: dict(v) for k, v in self.machines.items()},
                "lasers": {k: dict(v) for k, v in self.lasers.items()},
                "flame_channels": sorted(self.channels(cfg)),
                "file": self.path, "load_error": self.load_error}


# -------------------------------------------------------------- the purge --

class Purge:
    """The purge selection and the fire heartbeat. Pure apart from the
    providers it is built with; the Morning facade owns the clock."""

    def __init__(self, cfg_get, lockout, link_get, flames_get, live_check,
                 group_names, journal=None, clock=time.monotonic):
        self._cfg = cfg_get              # () -> MorningConfig
        self._lockout = lockout
        self._link = link_get            # () -> FlameLink or None
        self._flames = flames_get        # () -> flame status view or None
        self._live = live_check          # () -> refusal sentence or ""
        self._names = group_names        # () -> flamesafe group names
        self._journal = journal
        self._clock = clock
        self._lock = threading.Lock()
        self.selected = []               # machine ids, in click order
        self.owner = None                # device that owns the selection
        self.fire = None                 # {"id", "device", "start", "beat",
        #                                  "head_since"} while a key is held
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
        return ""

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
        why = self._refusal(device, seen_ok)
        if why:
            return 409, {"error": why}
        if self._lockout.is_locked("machine", machine_id):
            return 409, {"error": f"{m.name} is locked out, so it cannot "
                                  f"be armed for a purge. Unlock it first."}
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
        A locked machine, or one in a fully locked group, never counts."""
        cfg = self._cfg()
        names = list(self._names() or [])
        want = set()
        with self._lock:
            sel = list(self.selected)
        for mid in sel:
            m = cfg.by_id.get(mid)
            if m is None or self._lockout.is_locked("machine", mid):
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
        head_settle_s of continuous heartbeats). Also the machines left out
        and why, for the page."""
        cfg = self._cfg()
        vals = bytearray(UNIVERSE_SIZE)
        armed = self._armed_groups()
        firing, waiting = [], []
        with self._lock:
            sel = list(self.selected)
            head_since = self.fire["head_since"] if self.fire else now
        for mid in sel:
            m = cfg.by_id.get(mid)
            if m is None:
                continue
            if self._lockout.is_locked("machine", mid):
                waiting.append(f"{m.name}: locked out")
                continue
            if m.group not in armed:
                waiting.append(f"{m.name}: group {m.group} is not armed")
                continue
            p = cfg.purge
            if m.kind == "waver":
                w = p["waver"]
                vals[m.pan - 1] = w["pan_up"]
                vals[m.tilt - 1] = w["tilt_up"]
                if now - head_since < w["head_settle_s"]:
                    waiting.append(f"{m.name}: head going up "
                                   f"({w['head_settle_s']:g} s)")
                    continue
                fire = w["fire"]
            elif m.kind == "fireball":
                fire = p["fireball"]["fire"]
            else:
                fire = p["gflame_fire"]
            for f in m.fire:
                vals[f - 1] = fire
            firing.append(m.name)
        return bytes(vals), firing, waiting

    def fire_beat(self, body, device, seen_ok, deck=False):
        """One fire heartbeat (every 100 ms while the key is down). The
        first call (no fire_id) starts a press; later ones carry its id.
        Any refusal ends the press."""
        why = self._refusal(device, seen_ok)
        with self._lock:
            sel = list(self.selected)
            owner = self.owner
        if not why and not sel:
            why = "Nothing is armed for a purge. Click a machine first."
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
                                 "start": now, "beat": now,
                                 "head_since": now}
                self.fires += 1
        vals, firing, waiting = self.frame(now)
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
                 threaded=True, transport=None):
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
        self.layout = Layout(os.path.join(folder, LAYOUT_FILE))
        self.lockout = Lockout(os.path.join(folder, LOCKOUT_FILE),
                               journal=journal, wall=wall)
        self._link_get = link_get
        self._flames_get = flames_get
        self._live_check = live_check
        self.purge = Purge(lambda: self.cfg, self.lockout, link_get,
                           flames_get, self._live_or_broken, self._group_names,
                           journal=journal, clock=clock)
        self.lasers = lasertest.LaserTests(
            self.cfg.lasers, locked=lambda lid: self.lockout.is_locked(
                "laser", lid), journal=journal, socket_factory=socket_factory,
            clock=clock)
        logs_dir = logs_dir or os.path.join(folder, "logs")
        self.checklist = checklist_mod.ChecklistService(
            self.cfg, os.path.join(logs_dir, "checklists"), night=night,
            journal=journal, wall=wall, clock=clock, transport=transport,
            threaded=threaded)
        self._open_at = None         # clock() of the page's last open beat
        self._open_devices = {}      # device -> clock of its last beat
        self._was_live = False       # the watcher's last word on live_check
        self._stop = threading.Event()
        self._thread = None
        self._lock_channels_to_link()
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
        try:
            return self._live_check() or ""
        except Exception as e:
            return f"the show state could not be read ({type(e).__name__})."

    def _lock_channels_to_link(self):
        link = self._link_get()
        if link is not None and hasattr(link, "set_locked_channels"):
            try:
                link.set_locked_channels(self.lockout.channels(self.cfg))
            except Exception:
                pass

    def page_open(self):
        at = self._open_at
        return at is not None and self._clock() - at <= OPEN_STALE_S

    def open_beat(self, device):
        """The page says it is open (every 250 ms while it is)."""
        now = self._clock()
        self._open_at = now
        self._open_devices[device] = now
        for d in [d for d, t in self._open_devices.items()
                  if now - t > OPEN_STALE_S]:
            del self._open_devices[d]

    def tick(self, now=None):
        """The watcher's pass, 10 a second: purge expiry, laser tests off
        when a show goes live or an Abort stands, the link's lock set."""
        now = self._clock() if now is None else now
        try:
            self.purge.tick(now, self.page_open())
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
        live = bool(self._live_or_broken())
        if live and not self._was_live:
            # A show going live (or an Abort landing) while a laser is
            # locked out: its lock messages go again (MORNING.md: "at every
            # show start while locked"), so BEYOND hears the lock on the
            # night it matters, not only on the morning it was set.
            for lid in list(self.lockout.lasers):
                self.lasers.send_lock(lid, "the show is going live")
        self._was_live = live
        self._lock_channels_to_link()
        try:
            self.checklist.tick(now)
        except Exception:
            pass

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
        lm = [self.cfg.by_id[m].name for m in self.lockout.machines
              if m in self.cfg.by_id]
        ll = [l.name for l in self.cfg.lasers if l.id in self.lockout.lasers]
        fl = self._flames_get() or {}
        fs_slots = (fl.get("lockout") or {}).get("slots")
        want = sorted(self.lockout.channels(self.cfg))
        confirmed = (fs_slots == want) if isinstance(fs_slots, list) else None
        return {"locked_machines": lm, "locked_lasers": ll,
                "flamesafe_confirmed": confirmed,
                "purge": self.purge.view()["firing"] or bool(
                    self.purge.selected),
                "laser_tests_on": sorted(self.lasers.on),
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
        v["checklists"] = self.checklist.view()
        v["config_error"] = self.config_error
        v["flamesafe_groups"] = list(self._group_names() or [])
        v["live_refusal"] = self._live_or_broken()
        v["open"] = self.page_open()
        v["me"] = ctx_device
        return v

    def post(self, name, body, device, who, seen_ok, local):
        """(code, body) for the page's POST routes."""
        body = body if isinstance(body, dict) else {}
        if name == "morning-open":
            self.open_beat(device)
            return 200, {"ok": True, "open": True}
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
            deck = local and who == "Stream Deck"
            return self.purge.fire_beat(body, device if not deck else
                                        "Stream Deck", seen_ok or deck,
                                        deck=deck)
        if name == "purge-stop":
            self.purge.stop(f"{device} let go", device=device)
            return 200, {"ok": True}
        if name == "purge-clear":
            self.purge.clear(f"{device} cleared the selection")
            return 200, {"ok": True, "text": "Purge selection cleared."}
        if name == "laser-test":
            on = body.get("on") is True
            if on:
                why = self._live_or_broken()
                if why:
                    return 409, {"error": f"Laser tests are not allowed now: "
                                          f"{why}"}
            ok, text = self.lasers.set(str(body.get("laser")), on,
                                       who=who, device=device)
            return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                          else {"error": text})
        if name == "lock":
            kind = body.get("kind")
            if kind not in ("machine", "laser"):
                return 400, {"error": "kind has to be machine or laser."}
            uid = str(body.get("id") or "")
            on = body.get("on") is not False
            ok, text = self.lockout.set(self.cfg, kind, uid, on, who, device,
                                        reason=body.get("reason", ""),
                                        confirmed=body.get("confirmed"))
            if ok and on:
                if kind == "machine" and uid in self.purge.selected:
                    self.purge.select(uid, False, device, True)
                if kind == "laser":
                    self.lasers.send_lock(uid, "locked out")
            self._lock_channels_to_link()
            return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                          else {"error": text})
        if name == "layout":
            try:
                pos = self.layout.save(self.cfg, body.get("positions"))
            except (ValueError, OSError) as e:
                return 400, {"error": str(e)}
            self._note(f"{device} saved the machine layout.", action="layout",
                       outcome="saved", screen=device)
            return 200, {"ok": True, "text": "Layout saved.", "layout": pos}
        if name.startswith("checklist-"):
            return self.checklist.post(name, body, device, who, local)
        return 404, {"error": "no such thing here"}
