"""What the engine sees happen by itself, for the audit log (audit.py):
a show starting and ending, Hold and Abort taking effect, a show file
Run and stopped, flame groups arming and disarming, machines locked out.
Presses are logged where they are pressed (web.py, streamdeck.py); this
logs what came of them and what the scheduler did on its own, as user
"system" on surface "engine".

Read only: it looks at the same views the rack screen polls, once a
second, on a thread of its own. Nothing here can change the show, and an
error in here only skips that second.
"""
import threading

from . import audit as audit_mod

WATCH_S = 1.0


def snapshot(remote):
    """{key: value} of what is worth a line when it changes. Never raises;
    a part that cannot be read is left out (and so never "changes")."""
    out = {}
    svc = getattr(remote, "schedule", None)
    if svc is not None:
        try:
            st = svc.state_view()
            out["show state"] = str(st.get("state"))
        except Exception:                        # noqa: BLE001
            pass
        try:
            m = getattr(svc, "machine", None)
            cond = getattr(svc, "conductor", None)
            out["abort latch"] = ("latched" if (
                getattr(m, "abort_latched", False) or
                getattr(cond, "latched", False)) else "clear")
        except Exception:                        # noqa: BLE001
            pass
    try:
        cs = remote.control.state() or {}
        out["show file"] = (f"running {cs.get('timeline') or cs.get('show') or ''}"
                            .strip() if cs.get("running") else "stopped")
    except Exception:                            # noqa: BLE001
        pass
    fs = getattr(remote, "flame_status", None)
    if fs is not None:
        try:
            v = fs.view()
            if not v.get("stale"):
                for g in v.get("groups") or []:
                    out[f"flame group {g.get('name')}"] = str(g.get("armed"))
                lk = v.get("lockout")
                if isinstance(lk, dict):
                    out["locked out slots"] = ",".join(
                        str(s) for s in sorted(lk.get("slots") or [])) or "none"
        except Exception:                        # noqa: BLE001
            pass
    return out


def changes(before, after):
    """[(key, old, new)] for every value that changed. A key that went
    missing (a view that could not be read) is not a change."""
    return [(k, before.get(k), v) for k, v in sorted(after.items())
            if k in before and before[k] != v]


class EngineWatch:
    def __init__(self, remote, log, every_s=WATCH_S):
        self.remote = remote
        self.log = log
        self.every_s = every_s
        self._last = None
        self._stop = threading.Event()
        self._thread = None

    def step(self):
        """One look: a line per change since the last look."""
        now = snapshot(self.remote)
        if self._last is not None:
            for k, old, new in changes(self._last, now):
                self.log.event(user=audit_mod.SYSTEM, surface="engine",
                               action=k, target=f"{old} -> {new}",
                               result="accepted")
        # Keys that could not be read this time keep their last value.
        merged = dict(self._last or {})
        merged.update(now)
        self._last = merged

    def _loop(self):
        while not self._stop.wait(self.every_s):
            try:
                self.step()
            except Exception:                    # noqa: BLE001
                pass

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ltcplay-audit-watch")
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
