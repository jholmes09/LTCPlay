"""BENCH BUILD ONLY (branch bench-build, never merged): the engine's stall
probe.

Show PC, 2026-10-05, block 2 of 184548e: with the engine at High and its
flame link sender at Highest, MadMapper's cold start (six 1080p videos
decoded on the CPU, 600 % and more) still cost the engine 270 to 310 ms at
a time, while flamesafe (also High, its tick at Highest) kept time. What
held the engine up decides the fix, so this probe tells the candidates
apart from inside the engine:

  - a thread of the engine's own holding Python's lock (the GIL) and
    running: that thread's CPU time across the stall is about the stall;
  - the engine paged: its page faults across the stall jump, or its
    working set shrinks (Windows trimmed it for MadMapper's memory);
  - the engine not given the CPU at all (another program, or the system's
    own interrupt and DPC time): the engine used almost no CPU across the
    stall and faulted little. The soak's 1 s system line (interrupt and
    DPC time) tells those two apart.

A thread named "bench-stall-probe" wakes every 10 ms (at Highest with the
scheduling protection, the same as the flame link's sender). Every 100 ms
it reads every engine thread's CPU time, the process's CPU time, page
faults and working set. A wake 40 ms or more late is a stall: it reads them
again at once, and the difference says what happened in the stall. Every
5 s it writes the last 300 stalls and each minute's CPU per thread to
LTCPLAY_BENCH_STALLS. The cost: about 1 ms of work every 100 ms.

Imported only when LTCPLAY_BENCH_STALLS is set (the soak sets it); the
installed LTC Player never sets it.
"""
import collections
import json
import os
import sys
import threading
import time

WINDOWS = sys.platform == "win32"

PERIOD_S = 0.010
SNAP_EVERY_S = 0.100
LATE_MS = 40.0
KEEP_STALLS = 300
KEEP_MINUTES = 180
WRITE_EVERY_S = 5.0

# How a stall is read (see classify()).
HOG_SHARE = 0.6          # one thread busy this share of the stall: it held on
BUSY_SHARE = 0.6         # the engine as a whole busy this share: it was busy
FAULTS_PAGING = 2000     # page faults across one stall: it paged
WSET_DROP = 0.10         # the working set shrank by this share: it was trimmed


# ------------------------------------------------------------ readers ---
def _win_k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenThread.restype = wintypes.HANDLE
    k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetThreadTimes.argtypes = (wintypes.HANDLE,) + (
        ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetThreadPriority.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    return k32


_K32 = []


def _k32():
    if not _K32:
        _K32.append(_win_k32())
    return _K32[0]


def _ft(ft):
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def thread_cpu(native_id):
    """(user s, kernel s, priority or None) of one thread of this process,
    or None when it cannot be read."""
    if WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            k = _k32()
            h = k.OpenThread(0x0800, False, native_id)  # QUERY_LIMITED
            if not h:
                return None
            try:
                c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
                if not k.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e),
                                        ctypes.byref(kt), ctypes.byref(ut)):
                    return None
                pr = k.GetThreadPriority(h)
                return (_ft(ut), _ft(kt),
                        None if pr == 0x7FFFFFFF else pr)
            finally:
                k.CloseHandle(h)
        except Exception:
            return None
    try:
        with open(f"/proc/self/task/{native_id}/stat", "rb") as fh:
            f = fh.read().rsplit(b")", 1)[1].split()
        hz = os.sysconf("SC_CLK_TCK")
        return int(f[11]) / hz, int(f[12]) / hz, None
    except (OSError, ValueError, IndexError):
        return None


def process_mem():
    """(page faults so far, working set bytes), or (None, None)."""
    if WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            k = _k32()
            m = PMC()
            m.cb = ctypes.sizeof(m)
            f = k.K32GetProcessMemoryInfo
            f.argtypes = (wintypes.HANDLE, ctypes.POINTER(PMC),
                          wintypes.DWORD)
            if f(wintypes.HANDLE(k.GetCurrentProcess()), ctypes.byref(m),
                 m.cb):
                return m.PageFaultCount, m.WorkingSetSize
        except Exception:
            pass
        return None, None
    try:
        with open("/proc/self/stat", "rb") as fh:
            f = fh.read().rsplit(b")", 1)[1].split()
        with open("/proc/self/statm", "rb") as fh:
            rss = int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        return int(f[7]) + int(f[9]), rss
    except (OSError, ValueError, IndexError):
        return None, None


def process_cpu():
    t = os.times()
    return t.user, t.system


# ------------------------------------------------------------- reading ---
def classify(gap_ms, threads, proc_ms, faults, wset_before, wset_after,
             me="bench-stall-probe"):
    """One sentence on what a stall of `gap_ms` was, from what happened
    across it: `threads` [(name, cpu ms)], `proc_ms` the whole engine's CPU
    ms, `faults` its page faults, the working set before and after."""
    others = [(n, ms) for n, ms in threads if n != me]
    top = max(others, key=lambda x: x[1]) if others else ("", 0.0)
    if gap_ms > 0 and top[1] >= HOG_SHARE * gap_ms:
        return (f"held by its own thread {top[0]} (busy {top[1]:.0f} ms of "
                f"the {gap_ms:.0f} ms)")
    if gap_ms > 0 and proc_ms >= BUSY_SHARE * gap_ms:
        return (f"the engine busy across several threads ({proc_ms:.0f} ms "
                f"of CPU in {gap_ms:.0f} ms)")
    trimmed = (wset_before and wset_after is not None and
               wset_after < wset_before * (1 - WSET_DROP))
    if (faults or 0) >= FAULTS_PAGING or trimmed:
        return ("the engine paged ("
                + (f"{faults} page faults" if faults is not None else
                   "page faults not read")
                + (f", working set {wset_before / 1e6:.0f} to "
                   f"{wset_after / 1e6:.0f} MB" if trimmed else "") + ")")
    return (f"the engine not given the CPU ({proc_ms:.0f} ms of CPU in "
            f"{gap_ms:.0f} ms, {faults if faults is not None else '?'} page "
            f"faults): another program or the system's interrupt and DPC "
            f"time; see the soak's system line")


KINDS = (("held by its own thread", "own thread"),
         ("the engine busy", "engine busy"),
         ("the engine paged", "paged"),
         ("the engine not given the CPU", "not given the CPU"))


def kind_of(sentence):
    for head, short in KINDS:
        if sentence.startswith(head):
            return short
    return "other"


class StallProbe:
    def __init__(self, path=None, threads=threading.enumerate,
                 thread_cpu=thread_cpu, mem=process_mem, cpu=process_cpu,
                 clock=time.perf_counter, wall=time.time, sleep=time.sleep):
        self.path = path
        self._threads = threads
        self._thread_cpu = thread_cpu
        self._mem = mem
        self._cpu = cpu
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self.stalls = collections.deque(maxlen=KEEP_STALLS)
        self.by_minute = {}       # minute -> {thread name: cpu ms}
        self.worst = {}           # minute -> worst late wake ms
        self.priorities = {}      # thread name -> Windows priority
        self._snap = None
        self._last_wake = None
        self._last_write = None
        self.stop = threading.Event()

    def snapshot(self):
        names, times = {}, {}
        for th in self._threads():
            nid = getattr(th, "native_id", None)
            if not nid:
                continue
            got = self._thread_cpu(nid)
            if got is None:
                continue
            names[nid] = th.name
            times[nid] = got[0] + got[1]
            if got[2] is not None:
                self.priorities[th.name] = got[2]
        u, k = self._cpu()
        faults, wset = self._mem()
        return {"at": self._clock(), "names": names, "times": times,
                "cpu": u + k, "faults": faults, "wset": wset}

    @staticmethod
    def diff(a, b):
        per = collections.Counter()
        for nid, t in b["times"].items():
            if nid in a["times"]:
                per[b["names"][nid]] += max(0.0, t - a["times"][nid]) * 1000
        faults = (b["faults"] - a["faults"]
                  if a["faults"] is not None and b["faults"] is not None
                  else None)
        return per, (b["cpu"] - a["cpu"]) * 1000, faults

    def _account(self, a, b):
        per, _cpu, _f = self.diff(a, b)
        m = int(self._wall() // 60)
        d = self.by_minute.setdefault(m, {})
        for n, ms in per.items():
            d[n] = d.get(n, 0.0) + ms
        while len(self.by_minute) > KEEP_MINUTES:
            del self.by_minute[min(self.by_minute)]

    def step(self):
        """One wake of the probe; returns the stall it recorded, or None."""
        now = self._clock()
        out = None
        if self._last_wake is not None:
            late = (now - self._last_wake - PERIOD_S) * 1000.0
            m = int(self._wall() // 60)
            if late > self.worst.get(m, 0.0):
                self.worst[m] = late
                while len(self.worst) > KEEP_MINUTES:
                    del self.worst[min(self.worst)]
            if late >= LATE_MS and self._snap is not None:
                b = self.snapshot()
                a = self._snap
                per, proc_ms, faults = self.diff(a, b)
                window = (b["at"] - a["at"]) * 1000.0
                gap = late + PERIOD_S * 1000.0
                top = [(n, round(ms, 1)) for n, ms in per.most_common(4)]
                why = classify(gap, per.items(), proc_ms, faults, a["wset"],
                               b["wset"])
                out = {"at": round(self._wall(), 3), "late_ms": round(late, 1),
                       "window_ms": round(window, 1),
                       "engine_cpu_ms": round(proc_ms, 1),
                       "page_faults": faults,
                       "wset_mb": [None if a["wset"] is None else
                                   round(a["wset"] / 1e6, 1),
                                   None if b["wset"] is None else
                                   round(b["wset"] / 1e6, 1)],
                       "threads": top, "why": why, "kind": kind_of(why)}
                self.stalls.append(out)
                self._account(a, b)
                self._snap = b
        self._last_wake = now
        if self._snap is None or now - self._snap["at"] >= SNAP_EVERY_S:
            b = self.snapshot()
            if self._snap is not None:
                self._account(self._snap, b)
            self._snap = b
        if self.path and (self._last_write is None or
                          now - self._last_write >= WRITE_EVERY_S):
            self._last_write = now
            self.write()
        return out

    def doc(self):
        return {"stalls": list(self.stalls),
                "worst_by_minute": {str(m): round(v, 1)
                                    for m, v in self.worst.items()},
                "threads_by_minute": {
                    str(m): {n: round(ms, 1) for n, ms in sorted(
                        d.items(), key=lambda x: -x[1])[:10]}
                    for m, d in self.by_minute.items()},
                "priorities": dict(self.priorities),
                "late_ms": LATE_MS, "period_ms": PERIOD_S * 1000}

    def write(self):
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(self.doc(), fh)
            os.replace(self.path + ".tmp", self.path)
        except Exception:
            pass

    def run(self, raise_to=None):
        if raise_to is not None:
            try:
                raise_to(threading.get_native_id())
            except Exception:
                pass
        while not self.stop.is_set():
            try:
                self.step()
            except Exception:
                pass
            self._sleep(PERIOD_S)

    def start(self, raise_to=None):
        th = threading.Thread(target=self.run, args=(raise_to,), daemon=True,
                              name="bench-stall-probe")
        th.start()
        return th


def self_test():
    """Plain checks with made-up readings: each kind of stall is read as
    that kind."""
    k = classify(300, [("ltcplay-output", 290), ("bench-stall-probe", 5)],
                 300, 10, 100e6, 100e6)
    assert kind_of(k) == "own thread" and "ltcplay-output" in k, k
    k = classify(300, [("a", 100), ("b", 100)], 200, 10, 100e6, 100e6)
    assert kind_of(k) == "engine busy", k
    k = classify(300, [("a", 5)], 5, 5000, 100e6, 100e6)
    assert kind_of(k) == "paged", k
    k = classify(300, [("a", 5)], 5, 10, 100e6, 50e6)
    assert kind_of(k) == "paged", k
    k = classify(300, [("a", 5)], 5, 10, 100e6, 99e6)
    assert kind_of(k) == "not given the CPU", k
    yield "the stall probe reads each kind of stall as that kind"
    # A fake engine: one thread that burns 280 ms while the probe sleeps.
    clock = [0.0]
    cpu = {"hog": 0.0, "bench-stall-probe": 0.0}

    class T:
        def __init__(self, name, nid):
            self.name, self.native_id = name, nid
    ths = [T("hog", 1), T("bench-stall-probe", 2)]
    nid_name = {1: "hog", 2: "bench-stall-probe"}
    p = StallProbe(threads=lambda: ths,
                   thread_cpu=lambda n: (cpu[nid_name[n]], 0.0, None),
                   mem=lambda: (100, 100e6),
                   cpu=lambda: (sum(cpu.values()), 0.0),
                   clock=lambda: clock[0], wall=lambda: 6000.0 + clock[0])
    for _ in range(20):
        p.step()
        clock[0] += 0.010
    cpu["hog"] += 0.280
    clock[0] += 0.290
    st = p.step()
    assert st and st["kind"] == "own thread" and "hog" in st["why"], st
    assert p.worst[100] >= 280, p.worst
    yield "a 290 ms stall with one busy engine thread is pinned on that thread"
