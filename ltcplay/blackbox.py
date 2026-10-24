"""The black box: what the show PC was doing, every few seconds, in a file
that survives a hard reset (Jeff, 2026-10-24).

The Pico show PC froze hard overnight on 2026-10-24 between 00:24 and
00:33: no Windows crash dump, a WHEA fatal firmware error at the next
boot, and earlier freezes and NVMe stalls on 09-25, 10-17 and 10-19.
Nothing recorded the heat, the load or the disk while it happened, so
whether it was heat, the SSD's firmware or the platform is unproven. This
records it from now on.

One JSON line every EVERY_S (5 s; "black_box": {"every_s": ...} in
ltcplay_remote.json), in <settings>/logs/blackbox/blackbox.jsonl, beside
the audit log:

    cpu_pct, engine_cpu_pct   the whole PC's CPU and this engine's own
    mem_load_pct, mem_avail_mb, engine_rss_mb
    disk_free_mb              on the drive holding this folder
    disk_probe_ms             a small timed write and fsync of probe.bin
    last_write_ms             how long the previous line's write and fsync
                              took (a stalling drive shows here first)
    late_ms                   how late this sample woke: the PC starving
                              its threads shows here
    temps                     the ACPI thermal zones, the NVMe drive's own
                              temperature, Windows' "% Performance Limit"
                              (under 100: the CPU is being held back) and
                              "% Processor Performance", read on a slower
                              beat (SLOW_EVERY_S) on a thread of their own;
                              each is a number or "unavailable", and its
                              age_s says how old the reading is
    engine                    Run or idle, the flame groups flamesafe says
                              are armed, the schedule's state and the next
                              show's start
    dropped                   samples not written because the writer was
                              still busy with an earlier one

A "start" line opens every run (with when Windows booted and when the
previous run's last line was written, so a freeze is the gap between the
two), and a "stop" line closes a clean one: a run with no stop line ended
in a crash, a kill or a hard reset.

SURVIVES A HARD RESET. Every line is one write() of the whole line to a
file opened for appending, then fsync(), before the next is taken. A
reset can cut off at most the line being written; read_last() and
`python -m ltcplay.blackbox` skip a torn last line, and the next start
ends it with a newline first so the file stays one line per record.
Rotation by size: MAX_BYTES (20 MB) a file, FILES (10) files kept.

NEVER IN THE WAY OF THE SHOW. Three daemon threads of its own: the sampler
(cheap reads only, on a fixed beat), the writer (the disk: the probe, the
write and the fsync), and the slow probes (the temperatures, which call
into ACPI, the drive and the performance counters, any of which can hang
on a sick PC). The sampler hands each sample to the writer through a
queue of QUEUE_MAX; a full queue drops the sample and counts it, so a
stalled disk never stalls the sampler, and nothing here waits on the
engine's threads. The engine's state is read without taking any of its
locks. Nothing here imports or writes to the flame link, flamesafe, sACN,
Art-Net or anything else that reaches the rig, and nothing raises into a
caller. Off with "black_box": {"off": true}.
"""
import datetime
import json
import os
import queue
import struct
import sys
import threading
import time

FOLDER = os.path.join("logs", "blackbox")
FILENAME = "blackbox.jsonl"
PROBE_FILE = "probe.bin"
PROBE_BYTES = 4096
EVERY_S = 5.0
MIN_EVERY_S = 1.0
MAX_EVERY_S = 60.0
SLOW_EVERY_S = 30.0
MAX_BYTES = 20 * 1024 * 1024
FILES = 10
QUEUE_MAX = 4
UNAVAILABLE = "unavailable"
WINDOWS = sys.platform == "win32"


def default_folder(settings_dir):
    return os.path.join(settings_dir, FOLDER)


def stopped_after_s(every_s):
    """No sample written for this long: the black box has stopped."""
    return max(3.0 * float(every_s), 15.0)


def load_config(doc):
    """The "black_box" block of ltcplay_remote.json, checked:
    ({"off", "every_s", "max_mb", "files"}, problem sentence or "").
    Anything wrong falls back to the default for that value and is said;
    a bad block never stops the engine."""
    out = {"off": False, "every_s": EVERY_S, "max_mb": MAX_BYTES // 2 ** 20,
           "files": FILES}
    if doc is None:
        return out, ""
    if not isinstance(doc, dict):
        return out, ("black_box has to be an object like "
                     '{"every_s": 5}; the defaults are used.')
    bad = []
    if "off" in doc:
        if doc["off"] is True or doc["off"] is False:
            out["off"] = doc["off"]
        else:
            bad.append("off has to be true or false")
    for key, lo, hi in (("every_s", MIN_EVERY_S, MAX_EVERY_S),
                        ("max_mb", 1, 500), ("files", 2, 100)):
        if key not in doc:
            continue
        v = doc[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                not lo <= v <= hi:
            bad.append(f"{key} has to be a number from {lo:g} to {hi:g}")
            continue
        out[key] = float(v) if key == "every_s" else int(v)
    return out, ("black_box: " + "; ".join(bad) + ". The defaults are "
                 "used for those." if bad else "")


def _iso_utc(t):
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _iso_local(t):
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(
        timespec="milliseconds")


# ------------------------------------------------------------ the probes --
# Each probe returns a number, or UNAVAILABLE, and never raises. Windows
# reads go through ctypes into the system's own DLLs: nothing installed,
# no administrator, no PowerShell started every few seconds.

class _CpuMeter:
    """The whole PC's CPU busy percent between two calls, and this
    process's own (os.times, so it is the engine, every thread)."""

    def __init__(self, system_times=None, process_times=None,
                 clock=time.monotonic):
        self._sys = system_times or _system_cpu_times
        self._proc = process_times or (lambda: sum(os.times()[:2]))
        self._clock = clock
        self._last_sys = None
        self._last_proc = None

    def read(self):
        cpu, mine = UNAVAILABLE, UNAVAILABLE
        try:
            cur = self._sys()
        except Exception:                        # noqa: BLE001
            cur = None
        if cur is not None and self._last_sys is not None:
            busy = cur[0] - self._last_sys[0]
            total = cur[1] - self._last_sys[1]
            if total > 0:
                cpu = round(max(0.0, min(100.0, 100.0 * busy / total)), 1)
        self._last_sys = cur
        try:
            now = (self._proc(), self._clock())
        except Exception:                        # noqa: BLE001
            now = None
        if now is not None and self._last_proc is not None:
            dt = now[1] - self._last_proc[1]
            if dt > 0:
                ncpu = os.cpu_count() or 1
                mine = round(max(0.0, 100.0 * (now[0] - self._last_proc[0])
                                 / dt / ncpu), 1)
        self._last_proc = now
        return cpu, mine


def _system_cpu_times():
    """(busy, total) in any unit, or None."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        class FT(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]
        idle, kern, user = FT(), FT(), FT()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None
        v = [(f.hi << 32) | f.lo for f in (idle, kern, user)]
        total = v[1] + v[2]               # kernel time includes idle
        return total - v[0], total
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            parts = fh.readline().split()
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums[:8])
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


def memory():
    """(load percent, available MB, this process's resident MB), each a
    number or UNAVAILABLE."""
    load, avail, rss = UNAVAILABLE, UNAVAILABLE, UNAVAILABLE
    try:
        if WINDOWS:
            import ctypes
            from ctypes import wintypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD),
                            ("dwMemoryLoad", wintypes.DWORD)] + [
                    (n, ctypes.c_ulonglong) for n in (
                        "total", "avail", "ptotal", "pavail", "vtotal",
                        "vavail", "xavail")]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                load = int(ms.dwMemoryLoad)
                avail = round(ms.avail / 2 ** 20, 1)

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("faults", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in (
                        "peak_ws", "ws", "qpp", "qp", "qpnp", "qnp", "pf",
                        "peak_pf")]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.K32GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
            if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                           ctypes.byref(pmc), pmc.cb):
                rss = round(pmc.ws / 2 ** 20, 1)
            return load, avail, rss
        info = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for ln in fh:
                k, _, v = ln.partition(":")
                info[k] = int(v.split()[0])
        total, av = info.get("MemTotal"), info.get("MemAvailable")
        if total and av is not None:
            load = round(100.0 * (total - av) / total)
            avail = round(av / 1024.0, 1)
        with open("/proc/self/statm", encoding="ascii") as fh:
            rss = round(int(fh.read().split()[1]) * os.sysconf(
                "SC_PAGE_SIZE") / 2 ** 20, 1)
    except Exception:                            # noqa: BLE001
        pass
    return load, avail, rss


def disk_free_mb(folder):
    try:
        import shutil
        return round(shutil.disk_usage(folder).free / 2 ** 20)
    except Exception:                            # noqa: BLE001
        return UNAVAILABLE


def boot_utc(wall=time.time):
    """When the PC last booted (UTC text), or UNAVAILABLE."""
    try:
        if WINDOWS:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            up = k32.GetTickCount64() / 1000.0
        else:
            with open("/proc/uptime", encoding="ascii") as fh:
                up = float(fh.read().split()[0])
        return _iso_utc(wall() - up)
    except Exception:                            # noqa: BLE001
        return UNAVAILABLE


def disk_probe_ms(path):
    """A PROBE_BYTES write and fsync of `path`, timed, in ms, or
    UNAVAILABLE. Rewrites the same bytes in place: the file never grows."""
    t = time.perf_counter()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT |
                     getattr(os, "O_BINARY", 0), 0o644)
        try:
            os.write(fd, b"\0" * PROBE_BYTES)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        return UNAVAILABLE
    return round((time.perf_counter() - t) * 1000.0, 1)


# The slow probes: temperatures and throttling.

PDH_COUNTERS = {
    "acpi_c": r"\Thermal Zone Information(*)\Temperature",
    "perf_limit_pct": r"\Processor Information(_Total)\% Performance Limit",
    "proc_perf_pct":
        r"\Processor Information(_Total)\% Processor Performance",
}
STORAGE_DEVICE_TEMPERATURE_PROPERTY = 52
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400


def parse_storage_temperature(buf):
    """STORAGE_TEMPERATURE_DATA_DESCRIPTOR bytes -> the temperatures it
    lists, in C (the first is the drive's composite), or []."""
    try:
        _ver, size, _crit, _warn, count = struct.unpack_from("<IIhhH", buf, 0)
    except struct.error:
        return []
    out = []
    for i in range(min(count, 16)):
        off = 24 + 16 * i
        if off + 16 > len(buf) or (size and off + 16 > size):
            break
        _idx, temp = struct.unpack_from("<Hh", buf, off)
        if -40 < temp < 150:
            out.append(int(temp))
    return out


def thermal_kelvin_to_c(values):
    """The thermal zones' Kelvin readings in C, dropping zeros."""
    return [round(float(v) - 273.15, 1) for v in values
            if isinstance(v, (int, float)) and v > 0]


class _WindowsSlow:
    """The PDH counters and the NVMe drives' own temperature. Each read
    returns {name: value or UNAVAILABLE} and a {name: why} for the ones
    that cannot be read here."""

    def __init__(self):
        self.why = {}
        self._q = None
        self._counters = {}
        self._drives = None
        try:
            self._open_pdh()
        except Exception as e:                   # noqa: BLE001
            for k in PDH_COUNTERS:
                self.why.setdefault(k, f"performance counters: "
                                       f"{type(e).__name__}: {e}")

    def _open_pdh(self):
        import ctypes
        from ctypes import wintypes
        pdh = ctypes.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhOpenQueryW.restype = ctypes.c_uint32
        pdh.PdhAddEnglishCounterW.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.restype = ctypes.c_uint32
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhCollectQueryData.restype = ctypes.c_uint32
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_uint32
        self._pdh = pdh
        q = ctypes.c_void_p()
        st = pdh.PdhOpenQueryW(None, 0, ctypes.byref(q))
        if st != 0:
            raise OSError(f"PdhOpenQuery 0x{st:08X}")
        self._q = q
        for name, path in PDH_COUNTERS.items():
            c = ctypes.c_void_p()
            st = pdh.PdhAddEnglishCounterW(q, path, 0, ctypes.byref(c))
            if st == 0:
                self._counters[name] = c
            else:
                self.why[name] = f"{path} is not on this PC (0x{st:08X})"
        pdh.PdhCollectQueryData(q)        # rate counters need two reads

    def _pdh_values(self, counter):
        import ctypes
        from ctypes import wintypes

        class VAL(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD),
                        ("doubleValue", ctypes.c_double)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", VAL)]
        fmt = 0x00000200 | 0x00008000      # PDH_FMT_DOUBLE | NOCAP100
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        st = self._pdh.PdhGetFormattedCounterArrayW(
            counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if st != 0x800007D2 or not size.value:          # PDH_MORE_DATA
            return []
        buf = ctypes.create_string_buffer(size.value)
        st = self._pdh.PdhGetFormattedCounterArrayW(
            counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if st != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(ITEM))
        return [items[i].FmtValue.doubleValue for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]

    def _nvme(self):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p]
        k32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        invalid = ctypes.c_void_p(-1).value
        temps, errs = [], []
        for n in range(4):
            # Access 0: a query, no read or write rights, no administrator.
            h = k32.CreateFileW(f"\\\\.\\PhysicalDrive{n}", 0, 3, None, 3, 0,
                                None)
            if h is None or h == invalid:
                continue
            try:
                q = struct.pack("<II4x", STORAGE_DEVICE_TEMPERATURE_PROPERTY,
                                0)
                qb = ctypes.create_string_buffer(q, len(q))
                out = ctypes.create_string_buffer(512)
                got = wintypes.DWORD(0)
                if k32.DeviceIoControl(h, IOCTL_STORAGE_QUERY_PROPERTY, qb,
                                       len(q), out, 512, ctypes.byref(got),
                                       None):
                    t = parse_storage_temperature(out.raw[:got.value])
                    if t:
                        temps.append(t[0])
                else:
                    errs.append(f"drive {n}: error "
                                f"{ctypes.GetLastError()}")
            finally:
                k32.CloseHandle(h)
        return temps, errs

    def read(self):
        out = {k: UNAVAILABLE for k in PDH_COUNTERS}
        if self._q is not None:
            try:
                self._pdh.PdhCollectQueryData(self._q)
                for name, c in self._counters.items():
                    vals = self._pdh_values(c)
                    if name == "acpi_c":
                        vals = thermal_kelvin_to_c(vals)
                        if vals:
                            out[name] = vals
                        else:
                            self.why[name] = ("the thermal zone counter "
                                              "gave no reading")
                    elif vals:
                        out[name] = round(vals[0], 1)
            except Exception as e:               # noqa: BLE001
                self.why["pdh"] = f"{type(e).__name__}: {e}"
        try:
            temps, errs = self._nvme()
            if temps:
                out["nvme_c"] = temps
                self.why.pop("nvme_c", None)
            else:
                out["nvme_c"] = UNAVAILABLE
                self.why["nvme_c"] = ("Windows gave no drive temperature"
                                      + (f" ({'; '.join(errs)})" if errs
                                         else ""))
        except Exception as e:                   # noqa: BLE001
            out["nvme_c"] = UNAVAILABLE
            self.why["nvme_c"] = f"{type(e).__name__}: {e}"
        return out


class _UnixSlow:
    """Linux's thermal zones and an NVMe hwmon sensor, for a bench; a Mac
    has neither without tools, so it says unavailable."""

    def __init__(self):
        self.why = {}

    def read(self):
        import glob
        out = {"acpi_c": UNAVAILABLE, "nvme_c": UNAVAILABLE,
               "perf_limit_pct": UNAVAILABLE, "proc_perf_pct": UNAVAILABLE}
        self.why["perf_limit_pct"] = self.why["proc_perf_pct"] = (
            "a Windows performance counter")
        zones = []
        for p in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            try:
                zones.append(round(int(open(p).read()) / 1000.0, 1))
            except (OSError, ValueError):
                pass
        if zones:
            out["acpi_c"] = zones
        else:
            self.why["acpi_c"] = "no thermal zone is readable here"
        nv = []
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                if open(os.path.join(d, "name")).read().strip() != "nvme":
                    continue
                nv.append(round(int(open(os.path.join(
                    d, "temp1_input")).read()) / 1000.0, 1))
            except (OSError, ValueError):
                pass
        if nv:
            out["nvme_c"] = nv
        else:
            self.why["nvme_c"] = "no NVMe sensor is readable here"
        return out


def slow_reader():
    try:
        return _WindowsSlow() if WINDOWS else _UnixSlow()
    except Exception:                            # noqa: BLE001
        return _UnixSlow()


# ------------------------------------------------------------- reading --

def read_last(folder):
    """The last whole record in the current file, or None. A torn last
    line (a reset in the middle of a write) is skipped."""
    path = os.path.join(folder, FILENAME)
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read()
    except OSError:
        return None
    for ln in reversed(tail.split(b"\n")):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            return rec
    return None


def files_oldest_first(folder):
    names = [f"blackbox.{i}.jsonl" for i in range(FILES * 10, 0, -1)]
    names.append(FILENAME)
    return [os.path.join(folder, n) for n in names
            if os.path.exists(os.path.join(folder, n))]


def read_all(folder):
    """Every whole record in every file, oldest first; torn lines skipped."""
    out = []
    for p in files_oldest_first(folder):
        try:
            with open(p, "rb") as fh:
                lines = fh.read().split(b"\n")
        except OSError:
            continue
        for ln in lines:
            try:
                rec = json.loads(ln.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ------------------------------------------------------------ recorder --

class Recorder:
    """`engine_state()` -> a small dict, read without the engine's locks
    (web.py builds it). `probes` and the clocks are injected by the tests.
    """

    def __init__(self, folder, every_s=EVERY_S, max_bytes=MAX_BYTES,
                 files=FILES, engine_state=None, slow=None,
                 slow_every_s=SLOW_EVERY_S, clock=time.monotonic,
                 wall=time.time, cpu=None, mem=None, probe=None):
        self.folder = folder
        self.path = os.path.join(folder, FILENAME)
        self.every_s = float(every_s)
        self.max_bytes = int(max_bytes)
        self.files = int(files)
        self._engine_state = engine_state or (lambda: {})
        self._slow = slow
        self.slow_every_s = float(slow_every_s)
        self._clock = clock
        self._wall = wall
        self._cpu = cpu or _CpuMeter()
        self._mem = mem or memory
        self._probe = probe or disk_probe_ms
        self._q = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self._threads = []
        self._fd = None
        self._seq = 0
        self.dropped = 0
        self.written = 0
        self.last_write_at = None        # clock() of the last line written
        self.last_sample_at = None       # and of the last sample written
        self.last_write_ms = None
        self.fault = ""
        self.started_at = None
        self._temps = {}
        self._temps_at = None
        self._temps_why = {}
        self._why_logged = None

    # -- the file --------------------------------------------------------
    def _open(self):
        os.makedirs(self.folder, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                     getattr(os, "O_BINARY", 0), 0o644)
        try:
            if os.fstat(fd).st_size:
                with open(self.path, "rb") as fh:
                    fh.seek(-1, 2)
                    if fh.read(1) != b"\n":
                        # A torn last line from a reset: end it, so the
                        # next record starts on a line of its own.
                        os.write(fd, b"\n")
        except OSError:
            pass
        self._fd = fd

    def _close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _rotate(self):
        self._close()
        oldest = os.path.join(self.folder, f"blackbox.{self.files - 1}.jsonl")
        try:
            if os.path.exists(oldest):
                os.remove(oldest)
        except OSError:
            pass
        for i in range(self.files - 2, 0, -1):
            src = os.path.join(self.folder, f"blackbox.{i}.jsonl")
            if os.path.exists(src):
                try:
                    os.replace(src, os.path.join(self.folder,
                                                 f"blackbox.{i + 1}.jsonl"))
                except OSError:
                    pass
        try:
            os.replace(self.path, os.path.join(self.folder,
                                               "blackbox.1.jsonl"))
        except OSError:
            pass
        self._open()

    def write_record(self, rec):
        """One record, one line: written, then fsync'd. On the writer's
        thread (and the tests'). Never raises; a failure is self.fault."""
        t = time.perf_counter()
        try:
            data = (json.dumps(rec, separators=(",", ":"), default=str)
                    + "\n").encode("utf-8")
            if self._fd is None:
                self._open()
            try:
                size = os.fstat(self._fd).st_size
            except OSError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                self._rotate()
            os.write(self._fd, data)
            os.fsync(self._fd)
        except Exception as e:                   # noqa: BLE001
            self.fault = f"{type(e).__name__}: {e}"
            self._close()
            return False
        self.last_write_ms = round((time.perf_counter() - t) * 1000.0, 1)
        self.last_write_at = self._clock()
        self.written += 1
        self.fault = ""
        return True

    # -- the records -----------------------------------------------------
    def _stamp(self, kind):
        now = self._wall()
        self._seq += 1
        return {"t": _iso_utc(now), "local": _iso_local(now), "kind": kind,
                "seq": self._seq}

    def start_record(self):
        prev = read_last(self.folder)
        rec = self._stamp("start")
        rec.update({"pid": os.getpid(), "platform": sys.platform,
                    "python": sys.version.split()[0],
                    "boot_utc": boot_utc(self._wall),
                    "every_s": self.every_s,
                    "previous_last_t": prev.get("t") if prev else None,
                    "previous_last_kind": prev.get("kind") if prev else None})
        if prev is not None and prev.get("kind") != "stop":
            rec["previous_ended"] = ("without a stop line: a crash, a kill "
                                     "or a hard reset")
        try:
            from . import version as version_mod
            rec["build"] = version_mod.status()
        except Exception:                        # noqa: BLE001
            rec["build"] = UNAVAILABLE
        return rec

    def sample(self, late_ms=None):
        """One sample of the cheap readings (the sampler's thread). The
        disk probe and the write happen on the writer's thread."""
        rec = self._stamp("sample")
        cpu, mine = self._cpu.read()
        load, avail, rss = self._mem()
        rec.update({"cpu_pct": cpu, "engine_cpu_pct": mine,
                    "mem_load_pct": load, "mem_avail_mb": avail,
                    "engine_rss_mb": rss,
                    "disk_free_mb": disk_free_mb(self.folder),
                    "late_ms": late_ms, "dropped": self.dropped})
        temps = dict(self._temps) if self._temps else {
            k: UNAVAILABLE for k in ("acpi_c", "nvme_c", "perf_limit_pct",
                                     "proc_perf_pct")}
        temps["age_s"] = (None if self._temps_at is None else
                          round(self._clock() - self._temps_at, 1))
        why = dict(self._temps_why)
        if why and why != self._why_logged:
            # Why a reading is unavailable, written when it changes (the
            # first sample after the slow probes first ran, and then only
            # when the reason moves), not on every line.
            temps["why"] = why
            self._why_logged = why
        rec["temps"] = temps
        try:
            eng = self._engine_state()
            rec["engine"] = dict(eng) if isinstance(eng, dict) else {}
        except Exception as e:                   # noqa: BLE001
            rec["engine"] = {"error": f"{type(e).__name__}: {e}"}
        return rec

    def offer(self, rec):
        """Hand a sample to the writer, or drop it (counted) when the
        writer is still busy. Never waits."""
        try:
            self._q.put_nowait(rec)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def write_sample(self, rec):
        """The writer's half: the disk probe, then the line."""
        if rec.get("kind") == "sample":
            rec["disk_probe_ms"] = self._probe(
                os.path.join(self.folder, PROBE_FILE))
            rec["last_write_ms"] = self.last_write_ms
        ok = self.write_record(rec)
        if ok and rec.get("kind") == "sample":
            self.last_sample_at = self._clock()
        return ok

    def slow_once(self):
        if self._slow is None:
            self._slow = slow_reader()
        try:
            got = self._slow.read()
        except Exception as e:                   # noqa: BLE001
            self._temps_why["slow"] = f"{type(e).__name__}: {e}"
            return
        self._temps = dict(got)
        self._temps_at = self._clock()
        self._temps_why = dict(getattr(self._slow, "why", {}) or {})

    # -- the threads -----------------------------------------------------
    def _sampler(self):
        nxt = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            late = round(max(0.0, now - nxt) * 1000.0, 1)
            try:
                self.offer(self.sample(late_ms=late))
            except Exception as e:               # noqa: BLE001
                self.fault = f"sampler: {type(e).__name__}: {e}"
            nxt += self.every_s
            now = self._clock()
            if nxt < now:
                # Beats missed while this thread could not run: counted,
                # never caught up in a burst.
                missed = int((now - nxt) // self.every_s) + 1
                self.dropped += missed
                nxt += missed * self.every_s
            self._stop.wait(max(0.0, nxt - now))

    def _writer(self):
        while True:
            try:
                rec = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if rec is None:
                return
            self.write_sample(rec)

    def _slow_loop(self):
        while not self._stop.is_set():
            self.slow_once()
            self._stop.wait(self.slow_every_s)

    def start(self):
        if self._threads:
            return self
        self.started_at = self._clock()
        try:
            self._open()
        except OSError as e:
            self.fault = f"{type(e).__name__}: {e}"
        self.write_record(self.start_record())
        for target, name in ((self._writer, "ltcplay-blackbox-writer"),
                             (self._sampler, "ltcplay-blackbox"),
                             (self._slow_loop, "ltcplay-blackbox-slow")):
            th = threading.Thread(target=target, daemon=True, name=name)
            th.start()
            self._threads.append(th)
        return self

    def stop(self, timeout=2.0):
        """A clean stop: the "stop" line, then the file closed. Waits at
        most `timeout` for the writer; never raises."""
        self._stop.set()
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            pass
        for th in self._threads:
            if th.name == "ltcplay-blackbox-writer":
                th.join(timeout)
        rec = self._stamp("stop")
        rec["written"] = self.written
        rec["dropped"] = self.dropped
        self.write_record(rec)
        self._close()

    def status(self):
        """{"state": "recording" | "starting" | "stopped", "age_s",
        "every_s", "written", "dropped", "fault", "folder", "why"}: what
        the rack screen and the heartbeat say. Never raises."""
        now = self._clock()
        at = self.last_sample_at
        age = None if at is None else round(now - at, 1)
        limit = stopped_after_s(self.every_s)
        if self._stop.is_set() or self.started_at is None:
            state = "stopped"
        elif at is None:
            state = ("starting" if now - self.started_at <= limit
                     else "stopped")
        else:
            state = "recording" if now - at <= limit else "stopped"
        return {"state": state, "age_s": age, "every_s": self.every_s,
                "written": self.written, "dropped": self.dropped,
                "fault": self.fault, "folder": self.folder,
                "temps_why": dict(self._temps_why)}


def start(settings_dir, engine_state=None, config=None, log=print):
    """The Recorder for the show engine, started, or None when the config
    turns it off or it could not start (said once through `log`)."""
    cfg, problem = load_config(config)
    if problem:
        log(problem)
    if cfg["off"]:
        log("Black box: off (black_box off is true in ltcplay_remote.json).")
        return None
    try:
        r = Recorder(default_folder(settings_dir), every_s=cfg["every_s"],
                     max_bytes=cfg["max_mb"] * 2 ** 20, files=cfg["files"],
                     engine_state=engine_state).start()
    except Exception as e:                       # noqa: BLE001
        log(f"Black box: could not start: {type(e).__name__}: {e}")
        return None
    log(f"Black box: recording every {cfg['every_s']:g} s to {r.path}")
    return r


# --------------------------------------------------------- the command --

def summarize(records, every_s=EVERY_S):
    """Plain lines for a person: each run, and every gap longer than
    stopped_after_s(every_s) between two records."""
    out = []
    prev = None
    for rec in records:
        if rec.get("kind") == "start":
            out.append(f"{rec.get('local') or rec.get('t')}  START pid "
                       f"{rec.get('pid')}, PC booted {rec.get('boot_utc')}"
                       + (f"; the run before ended {rec['previous_ended']}"
                          if rec.get("previous_ended") else ""))
            every_s = float(rec.get("every_s") or every_s)
        elif rec.get("kind") == "stop":
            out.append(f"{rec.get('local') or rec.get('t')}  STOP, clean")
        if prev is not None:
            try:
                a = datetime.datetime.fromisoformat(
                    str(prev["t"]).replace("Z", "+00:00"))
                b = datetime.datetime.fromisoformat(
                    str(rec["t"]).replace("Z", "+00:00"))
                gap = (b - a).total_seconds()
                if gap > stopped_after_s(every_s):
                    out.append(f"  GAP of {gap:.0f} s after "
                               f"{prev.get('local') or prev.get('t')}")
            except (KeyError, ValueError):
                pass
        prev = rec
    if prev is not None:
        out.append(f"last record: {json.dumps(prev, sort_keys=True)}")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("-h", "--help"):
        folder = argv[0]
    elif argv:
        print("python -m ltcplay.blackbox [folder]: each run, every gap, "
              "and the last record the black box wrote.")
        return 0
    else:
        from . import remote as remote_mod
        folder = default_folder(remote_mod.settings_folder())
    recs = read_all(folder)
    if not recs:
        print(f"No black box records in {folder}.")
        return 1
    for ln in summarize(recs):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
