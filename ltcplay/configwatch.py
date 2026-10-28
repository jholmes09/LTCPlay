"""Settings changed on disk since a program read them (Jeff's field report,
2026-10-28, item 11).

On the show PC the settings files are read once, when each program starts.
At 01:54 flamesafe.json was being fixed: the supervisor restarted flamesafe
five times and flamesafe picked up the final file at 01:55:54, but the
engine and the Stream Deck program had read it at 01:54:49 and kept that
older copy. The wave flamer's heads stayed parked, and nothing on the rack
screen said the engine's settings were out of date. The same happened with
ltcplay_announce.json: the rack screen went on showing the old value.

ConfigWatch notes each settings file this engine reads at start (its path,
its modified time and size, and a hash of what matters in it) BEFORE the
engine reads it, so an edit that lands while the engine is starting is
counted as changed, never missed. Every CHECK_EVERY_S, on a thread of its
own and never on the show's path, it looks at each file again:

  the engine     is behind when the file's hash differs from the one taken
                 at start (a file saved again unchanged, or changed back,
                 is not a change)
  the deck,      are behind when the file was modified after they started
  flamesafe      (their start times come from the Windows supervisor's
                 programs.json; off the show PC there are none)
  LTC Player     (the supervisor) is behind when showpc.json differs, in a
                 setting other than show_folder, from what it read at its
                 start (it says so in programs.json; the show folder it
                 follows by itself)

What it finds is display only: a notice on the rack screen and the status
(/api/remote/status, "config_changed"), and one journal line each time the
picture changes. It is not an alert: it never holds a show, plays no
announcement, sounds no booth note and can never overwrite or clear a
flames alert. It never restarts anything; an operator decides.

One file may be read again live: a file given a reload function (the
announcements, ltcplay_announce.json) is read again by the engine once the
function says it is safe (nothing playing, no show running), and the
notice for it then clears by itself. Nothing that touches flames, arming or
flamesafe's link is ever read again while the engine runs.
"""
import hashlib
import json
import os
import threading
import time

CHECK_EVERY_S = 2.0

ENGINE, DECK, FLAMESAFE, SUPERVISOR = "engine", "deck", "flamesafe", \
    "supervisor"
# The order the programs are named in, and their names in a sentence.
PROGRAMS = (ENGINE, DECK, FLAMESAFE, SUPERVISOR)
WHO = {ENGINE: "the engine", DECK: "the Stream Deck program",
       FLAMESAFE: "flamesafe", SUPERVISOR: "LTC Player itself"}

RESTART = ("Restart LTC Player to use them (Start menu, Stop LTC Player "
           "then Start LTC Player).")
RUNNING_OLD = "Until then it runs with the older copy."


def file_hash(path):
    """sha256 of the file's bytes, or None when it is not there or cannot
    be read."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def keys_hash(keys):
    """A hash function for a file only some of whose settings are read at
    start (ltcplay_remote.json): the hash of those keys alone, so a save of
    a setting the engine reads live is not a change. A file that is not
    JSON hashes as its bytes, so a broken edit still counts as changed."""
    keys = tuple(keys)

    def h(path):
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return hashlib.sha256(raw).hexdigest()
        if not isinstance(doc, dict):
            return hashlib.sha256(raw).hexdigest()
        part = {k: doc.get(k) for k in keys}
        return hashlib.sha256(json.dumps(part, sort_keys=True).encode(
            "utf-8")).hexdigest()
    return h


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _hms(t):
    try:
        return time.strftime("%H:%M:%S", time.localtime(t))
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


def _words(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class _Entry:
    def __init__(self, name, path, engine, others, digest, wall):
        self.name = name
        self.path = path
        self.engine = engine          # this engine read it at start
        self.others = tuple(others)   # programs the supervisor runs that do
        self.digest = digest
        self.reload = None
        self.waiting = ""             # why a reload has not happened yet
        self.snap(wall)

    def snap(self, wall):
        """What the engine has now: taken at start, and again once the
        engine has read the file afresh (a reload, its own save)."""
        self.stamp = _stamp(self.path)
        self.value = self.digest(self.path)
        self.loaded_at = wall
        self.seen = (self.stamp, self.value)


class ConfigWatch:
    """See the module docstring. `journal(text)` gets one line each time
    the notice changes. `wall` is time.time, injectable for the tests. Every
    method is safe to call from any thread and never raises."""

    def __init__(self, journal=None, wall=None):
        self._journal = journal
        self.wall = wall or time.time
        self._lock = threading.Lock()
        self._entries = []
        self._items = []
        self._said = None
        self._thread = None
        self._stop = threading.Event()

    # -- what this engine reads -----------------------------------------
    def add(self, name, path, engine=True, others=(), digest=None):
        """Note `path` (shown as `name`) as read by this engine at start
        (engine=False: only by `others`, programs this engine does not
        read it for, such as the Stream Deck's ltcplay_deck.json). Call it
        BEFORE the engine reads the file. A path already noted is not
        noted twice; its readers are joined."""
        if not path:
            return
        path = os.path.abspath(os.path.expanduser(str(path)))
        with self._lock:
            for e in self._entries:
                if os.path.normcase(e.path) == os.path.normcase(path):
                    e.engine = e.engine or engine
                    e.others = tuple(dict.fromkeys(e.others + tuple(others)))
                    return
            try:
                self._entries.append(_Entry(name, path, engine, others,
                                            digest or file_hash, self.wall()))
            except Exception:                    # noqa: BLE001
                pass

    def paths(self):
        with self._lock:
            return [e.path for e in self._entries]

    def set_reload(self, path, fn):
        """`fn()` -> (done, why): reads the file at `path` again if that is
        safe now. Called from check() while the engine's copy is behind;
        once it says done the engine's copy is the file on disk."""
        path = os.path.abspath(str(path))
        with self._lock:
            for e in self._entries:
                if os.path.normcase(e.path) == os.path.normcase(path):
                    e.reload = fn

    def refresh(self, path):
        """The engine has just read `path` afresh (its own save and reload,
        the Programming screen's schedule): what is on disk is its copy."""
        if not path:
            return
        path = os.path.abspath(str(path))
        with self._lock:
            for e in self._entries:
                if os.path.normcase(e.path) == os.path.normcase(path):
                    try:
                        e.snap(self.wall())
                        e.waiting = ""
                    except Exception:            # noqa: BLE001
                        pass

    # -- the check ----------------------------------------------------------
    def _engine_behind(self, e):
        st = _stamp(e.path)
        if st == e.stamp:
            return False
        if st != e.seen[0]:
            e.seen = (st, e.digest(e.path))
        if e.seen[1] == e.value:
            # Saved again with the same settings, or changed back.
            e.stamp = st
            return False
        return True

    @staticmethod
    def _started(programs, who):
        """When the supervisor started `who`, while it runs; else None."""
        p = (programs or {}).get(who)
        if not isinstance(p, dict) or not p.get("running"):
            return None
        t = p.get("started")
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            return None
        return float(t)

    def check(self, programs=None):
        """Look at every file now. `programs` is the supervisor's
        programs.json ({} or None off the show PC). Returns the notice's
        items, one per file some program is behind on."""
        items = []
        reloads = []
        with self._lock:
            entries = list(self._entries)
        for e in entries:
            try:
                behind = []
                if e.engine and self._engine_behind(e):
                    behind.append(ENGINE)
                st = _stamp(e.path)
                for who in e.others:
                    t = self._started(programs, who)
                    if t is not None and st is not None and \
                            st[0] / 1e9 > t:
                        behind.append(who)
                if not behind:
                    e.waiting = ""
                    continue
                if behind == [ENGINE] and e.reload is not None:
                    reloads.append(e)
                items.append(self._item(e, behind, st))
            except Exception:                    # noqa: BLE001
                continue
        for e in reloads:
            done, why = self._try_reload(e)
            for it in items:
                if it["path"] == e.path:
                    if done:
                        items.remove(it)
                    else:
                        it["waiting"] = why
                    break
        sup = (programs or {}).get(SUPERVISOR)
        if isinstance(sup, dict) and sup.get("showpc_changed"):
            keys = [str(k) for k in sup.get("showpc_changed") or []]
            path = str(sup.get("showpc") or "showpc.json")
            items.append({"file": os.path.basename(path), "path": path,
                          "changed_at": str(sup.get("changed_at") or ""),
                          "behind": [SUPERVISOR], "keys": keys,
                          "waiting": ""})
        with self._lock:
            self._items = items
        self._say(items)
        return items

    def _item(self, e, behind, st):
        return {"file": os.path.basename(e.path), "path": e.path,
                "name": e.name,
                "changed_at": _hms(st[0] / 1e9) if st else "",
                "gone": st is None,
                "loaded_at": _hms(e.loaded_at),
                "behind": [p for p in PROGRAMS if p in behind],
                "waiting": ""}

    def _try_reload(self, e):
        try:
            done, why = e.reload()
        except Exception as ex:                  # noqa: BLE001
            done, why = False, f"{type(ex).__name__}: {ex}"
        if done:
            e.snap(self.wall())
            e.waiting = ""
            self._line(f"Settings: {os.path.basename(e.path)} changed on "
                       f"disk and the engine read it again ({e.path}).")
            return True, ""
        e.waiting = str(why or "")
        return False, e.waiting

    def _line(self, text):
        if self._journal is None:
            return
        try:
            self._journal(text)
        except Exception:                        # noqa: BLE001
            pass

    def _say(self, items):
        """One journal line each time the notice's sentence changes."""
        text = notice_text(items)
        if text == self._said:
            return
        was, self._said = self._said, text
        if text:
            self._line(text)
        elif was:
            self._line("Settings: every program runs with the settings "
                       "files as they are on disk again.")

    # -- what the screens read ------------------------------------------
    def view(self):
        """{"files": items, "text": the notice or "", "engine_behind"}."""
        with self._lock:
            items = [dict(i) for i in self._items]
        return {"files": items, "text": notice_text(items),
                "engine_behind": any(ENGINE in i["behind"] for i in items)}

    # -- the thread ------------------------------------------------------
    def start(self, programs_get=None, every_s=CHECK_EVERY_S):
        """check() every `every_s` on a daemon thread of its own;
        `programs_get()` gives programs.json each time."""
        def run():
            while not self._stop.wait(every_s):
                try:
                    progs = programs_get() if programs_get else None
                except Exception:                # noqa: BLE001
                    progs = None
                try:
                    self.check(progs)
                except Exception:                # noqa: BLE001
                    pass
        self._thread = threading.Thread(target=run, daemon=True,
                                        name="ltcplay-configwatch")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()


def notice_text(items):
    """The rack screen's notice for `items` (check()), or ""."""
    if not items:
        return ""
    parts, restart = [], False
    for it in items:
        behind = it.get("behind") or []
        who = _words(WHO.get(p, p) for p in behind)
        when = (f"changed {it['changed_at']}" if it.get("changed_at")
                else "changed")
        if it.get("gone"):
            when = "removed"
        if it.get("keys"):
            when += f": {', '.join(it['keys'])}"
        if it.get("waiting") and behind == [ENGINE]:
            parts.append(f"{it['file']} ({when}; the engine reads it again "
                         f"by itself once {it['waiting']})")
            continue
        restart = True
        parts.append(f"{it['file']} ({when}; older copy in {who})")
    text = "Settings changed on disk: " + "; ".join(parts) + "."
    if restart:
        text += " " + RESTART + " " + RUNNING_OLD
    return text
