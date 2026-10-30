"""Containing the third-party show programs (bench, 2026-10-05; opt in,
showpc.json "contain_third_party", off unless Jeff turns it on).

Show PC, block 2 of 184548e: MadMapper's cold start (six 1080p videos
decoded on the CPU, 600 to 645 %) held LTC Player's engine up for 270 to
310 ms at a time even with the engine at High. With containment on:

  - MadMapper and BEYOND run at Below normal priority, so every LTC Player
    thread, at any of its priorities, comes before theirs;
  - MadMapper may use every logical CPU but two, which are left to LTC
    Player alone (two hardware threads of one fast core when Windows says
    which cores are fast; never CPU 0, which takes most of the system's
    interrupts).

LTC Player's own programs are not pinned anywhere: they may use every
CPU, and the two kept clear are theirs whenever they need them.

Applied to each MadMapper and BEYOND process as it appears (a program
started after LTC Player too), checked again every few seconds, and set
back to normal when containment ends. Every change is logged. Nothing
here ever raises: a step Windows refuses is logged and the rest goes on.
"""
import fnmatch
import os
import sys

WINDOWS = sys.platform == "win32"

BELOW_NORMAL = 0x4000
NORMAL = 0x20
KEEP_FOR_LTC = 2
PROGRAMS = (("MadMapper", ("madmapper*.exe",), True),
            ("BEYOND", ("beyond*.exe",), False))     # (name, globs, pin)
PRIORITY_NAMES = {0x40: "Idle", 0x4000: "Below normal", 0x20: "Normal",
                  0x8000: "Above normal", 0x80: "High", 0x100: "Realtime"}


# ------------------------------------------------------- which CPUs ---
def cpu_sets():
    """[{"cpu": logical index, "core": core index, "eff": efficiency class
    (higher is faster)}] from Windows' CPU set list, or one entry per
    logical CPU (each its own core, all alike) when that cannot be read."""
    if WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            f = k.GetSystemCpuSetInformation
            f.argtypes = (ctypes.c_void_p, wintypes.ULONG,
                          ctypes.POINTER(wintypes.ULONG), wintypes.HANDLE,
                          wintypes.ULONG)
            need = wintypes.ULONG(0)
            f(None, 0, ctypes.byref(need), None, 0)
            buf = ctypes.create_string_buffer(need.value)
            if need.value and f(buf, need, ctypes.byref(need), None, 0):
                return parse_cpu_sets(buf.raw[:need.value])
        except Exception:
            pass
    n = os.cpu_count() or 1
    return [{"cpu": i, "core": i, "eff": 0, "group": 0} for i in range(n)]


def parse_cpu_sets(raw):
    """SYSTEM_CPU_SET_INFORMATION records: Size, Type, then Id (4 bytes),
    Group (2), LogicalProcessorIndex, CoreIndex, LastLevelCacheIndex,
    NumaNodeIndex, EfficiencyClass (1 byte each)."""
    import struct
    out, o = [], 0
    while o + 19 <= len(raw):
        size, typ = struct.unpack_from("<II", raw, o)
        if size <= 0:
            break
        if typ == 0:
            group, = struct.unpack_from("<H", raw, o + 12)
            lp, core, _llc, _numa, eff = struct.unpack_from("<5B", raw, o + 14)
            out.append({"cpu": lp, "core": core, "eff": eff, "group": group})
        o += size
    return out


def choose_kept(cpus, n=KEEP_FOR_LTC):
    """The logical CPUs to keep clear of MadMapper: `n` of the fastest
    class, whole cores (both hardware threads) first, from the top core
    down, never CPU 0 or its core. [] when there are too few CPUs to spare
    any (MadMapper keeps at least two)."""
    cpus = [c for c in cpus if c.get("group", 0) == 0]
    if len(cpus) < n + 2:
        return []
    top = max(c["eff"] for c in cpus)
    core0 = {c["core"] for c in cpus if c["cpu"] == 0}
    fast = [c for c in cpus if c["eff"] == top and c["core"] not in core0]
    if len(fast) < n:
        fast = [c for c in cpus if c["cpu"] != 0]
    by_core = {}
    for c in fast:
        by_core.setdefault(c["core"], []).append(c["cpu"])
    kept = []
    # Whole cores, the highest first; the most hardware threads first so
    # two threads of one core are kept together.
    for core in sorted(by_core, key=lambda k: (-len(by_core[k]), -k)):
        for cpu in sorted(by_core[core], reverse=True):
            if len(kept) < n:
                kept.append(cpu)
    return sorted(kept)


def mask_without(cpus, kept):
    m = 0
    for c in cpus:
        if c.get("group", 0) == 0 and c["cpu"] not in kept and c["cpu"] < 64:
            m |= 1 << c["cpu"]
    return m


def cpu_list(mask):
    return ",".join(str(i) for i in range(64) if mask >> i & 1)


# ------------------------------------------------------- processes ---
def list_processes():
    """{pid: exe file name} of every process this user can see."""
    out = {}
    if not WINDOWS:
        return out
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")
        arr = (wintypes.DWORD * 8192)()
        got = wintypes.DWORD()
        if not psapi.EnumProcesses(arr, ctypes.sizeof(arr),
                                   ctypes.byref(got)):
            return out
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                  wintypes.DWORD)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        for pid in arr[:got.value // ctypes.sizeof(wintypes.DWORD)]:
            h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                n = wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    out[pid] = os.path.basename(buf.value)
            finally:
                k.CloseHandle(h)
    except Exception:
        pass
    return out


def which(exe):
    low = (exe or "").lower()
    for name, globs, pin in PROGRAMS:
        if any(fnmatch.fnmatch(low, g) for g in globs):
            return name, pin
    return None, False


class WinProc:
    """The few Windows calls on another process, each failing soft."""

    def _k(self):
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                  wintypes.DWORD)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.GetPriorityClass.argtypes = (wintypes.HANDLE,)
        k.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.SetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.c_size_t)
        k.GetProcessAffinityMask.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t))
        return k, ctypes

    def _open(self, pid):
        k, ctypes = self._k()
        h = k.OpenProcess(0x0200 | 0x1000, False, pid)  # SET_INFO | QUERY_LTD
        return k, ctypes, h

    def priority(self, pid):
        k, _c, h = self._open(pid)
        if not h:
            return None
        try:
            return k.GetPriorityClass(h) or None
        finally:
            k.CloseHandle(h)

    def set_priority(self, pid, cls):
        k, ctypes, h = self._open(pid)
        if not h:
            return f"could not open it (error {ctypes.get_last_error()})"
        try:
            if k.SetPriorityClass(h, cls):
                return ""
            return f"Windows refused (error {ctypes.get_last_error()})"
        finally:
            k.CloseHandle(h)

    def affinity(self, pid):
        k, ctypes, h = self._open(pid)
        if not h:
            return None, None
        try:
            pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
            if k.GetProcessAffinityMask(h, ctypes.byref(pm),
                                        ctypes.byref(sm)):
                return pm.value, sm.value
            return None, None
        finally:
            k.CloseHandle(h)

    def set_affinity(self, pid, mask):
        k, ctypes, h = self._open(pid)
        if not h:
            return f"could not open it (error {ctypes.get_last_error()})"
        try:
            if k.SetProcessAffinityMask(h, mask):
                return ""
            return f"Windows refused (error {ctypes.get_last_error()})"
        finally:
            k.CloseHandle(h)


class Containment:
    """tick() every few seconds while containment is on; release() when it
    ends. `log` gets one sentence per change."""

    def __init__(self, log=print, procs=list_processes, win=None,
                 cpus=None):
        self.log = log
        self._procs = procs
        self.win = win or WinProc()
        self.cpus = cpus if cpus is not None else cpu_sets()
        self.kept = choose_kept(self.cpus)
        self.mask = mask_without(self.cpus, self.kept) if self.kept else 0
        self.done = {}          # pid -> (name, exe)
        self.applied = []       # sentences, for a report
        self._said = set()

    def describe(self):
        if not self.kept:
            return (f"{len(self.cpus)} logical CPU(s): too few to keep any "
                    f"clear of MadMapper, so only the priorities are set")
        return (f"MadMapper may use CPUs {cpu_list(self.mask)}; CPUs "
                f"{', '.join(map(str, self.kept))} are kept for LTC Player "
                f"alone ({len(self.cpus)} logical CPUs)")

    def _say(self, text):
        self.applied.append(text)
        try:
            self.log(text)
        except Exception:
            pass

    def tick(self):
        try:
            procs = self._procs()
        except Exception:
            procs = {}
        for pid in list(self.done):
            if pid not in procs:
                name, exe = self.done.pop(pid)
                self._say(f"containment: {name} ({exe}, pid {pid}) has "
                          f"closed")
        for pid, exe in procs.items():
            name, pin = which(exe)
            if not name:
                continue
            try:
                self._contain(pid, name, exe, pin)
            except Exception as e:
                key = (pid, "error")
                if key not in self._said:
                    self._said.add(key)
                    self._say(f"containment: {name} (pid {pid}) not "
                              f"contained ({type(e).__name__}: {e})")

    def _contain(self, pid, name, exe, pin):
        first = pid not in self.done
        cur = self.win.priority(pid)
        parts = []
        if cur != BELOW_NORMAL:
            why = self.win.set_priority(pid, BELOW_NORMAL)
            was = PRIORITY_NAMES.get(cur, "unknown") if cur else "unknown"
            parts.append(f"priority Below normal (was {was})" if not why
                         else f"priority NOT lowered: {why}")
        if pin and self.mask:
            have, _sys = self.win.affinity(pid)
            if have != self.mask:
                why = self.win.set_affinity(pid, self.mask)
                parts.append(f"CPUs {cpu_list(self.mask)}" if not why else
                             f"CPUs NOT limited: {why}")
        self.done[pid] = (name, exe)
        if parts:
            lead = ("" if first else "set again (it had changed): ")
            self._say(f"containment: {name} ({exe}, pid {pid}): {lead}"
                      + ", ".join(parts))

    def release(self):
        """Back to Normal priority and every CPU, for each process this
        contained that is still running."""
        try:
            procs = self._procs()
        except Exception:
            procs = {}
        for pid, (name, exe) in list(self.done.items()):
            if pid not in procs:
                continue
            parts = []
            why = self.win.set_priority(pid, NORMAL)
            parts.append("priority Normal" if not why else
                         f"priority NOT set back: {why}")
            if dict(PROGRAMS_PIN).get(name):
                _have, sysm = self.win.affinity(pid)
                if sysm:
                    why = self.win.set_affinity(pid, sysm)
                    parts.append("every CPU" if not why else
                                 f"CPUs NOT set back: {why}")
            self._say(f"containment ended: {name} (pid {pid}): "
                      + ", ".join(parts))
        self.done.clear()


PROGRAMS_PIN = [(n, pin) for n, _g, pin in PROGRAMS]


def self_test():
    """Plain checks with made-up CPUs and processes."""
    # An Intel hybrid: 8 P-cores with 2 threads (CPUs 0-15, class 1), 8
    # E-cores (CPUs 16-23, class 0). Two threads of the top P-core.
    hy = [{"cpu": i, "core": i // 2, "eff": 1} for i in range(16)] + \
        [{"cpu": 16 + i, "core": 8 + i, "eff": 0} for i in range(8)]
    assert choose_kept(hy) == [14, 15], choose_kept(hy)
    # 8 cores, no SMT, all alike: the top two, never CPU 0.
    flat = [{"cpu": i, "core": i, "eff": 0} for i in range(8)]
    assert choose_kept(flat) == [6, 7], choose_kept(flat)
    assert mask_without(flat, [6, 7]) == 0x3F
    # Too few to spare: nothing kept.
    assert choose_kept(flat[:3]) == []
    # Windows' records, 32 bytes each: Size, Type, Id, Group, then the
    # logical index, core, cache, NUMA node and efficiency class.
    import struct
    raw = b"".join(struct.pack("<IIIH5B", 32, 0, 256 + i, 0, i, i // 2, 0, 0,
                               1 if i < 4 else 0).ljust(32, b"\0")
                   for i in range(6))
    got = parse_cpu_sets(raw)
    assert [(c["cpu"], c["core"], c["eff"]) for c in got] == \
        [(0, 0, 1), (1, 0, 1), (2, 1, 1), (3, 1, 1), (4, 2, 0), (5, 2, 0)], got
    assert choose_kept(got) == [2, 3], choose_kept(got)
    yield ("containment keeps two hardware threads of a fast core for LTC "
           "Player (never CPU 0), and none on a PC too small to spare them")

    class FakeWin:
        def __init__(self):
            self.pri, self.aff = {}, {}

        def priority(self, pid):
            return self.pri.get(pid, NORMAL)

        def set_priority(self, pid, cls):
            self.pri[pid] = cls
            return ""

        def affinity(self, pid):
            return self.aff.get(pid, 0xFF), 0xFF

        def set_affinity(self, pid, mask):
            self.aff[pid] = mask
            return ""
    w = FakeWin()
    said = []
    procs = {10: "MadMapperDemo.exe", 11: "BEYOND.exe", 12: "ltcplay.exe"}
    c = Containment(log=said.append, procs=lambda: procs, win=w, cpus=flat)
    c.tick()
    assert w.pri == {10: BELOW_NORMAL, 11: BELOW_NORMAL}, w.pri
    assert w.aff == {10: 0x3F}, w.aff
    n = len(said)
    c.tick()
    assert len(said) == n, said          # nothing changed, nothing said
    w.pri[10] = NORMAL                   # MadMapper set itself back
    c.tick()
    assert w.pri[10] == BELOW_NORMAL and "set again" in said[-1], said
    c.release()
    assert w.pri == {10: NORMAL, 11: NORMAL} and w.aff[10] == 0xFF, (w.pri,
                                                                     w.aff)
    yield ("containment lowers MadMapper and BEYOND, limits MadMapper's "
           "CPUs, never touches LTC Player, sets them again if they change, "
           "and sets them back when it ends")
