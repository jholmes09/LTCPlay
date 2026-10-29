"""LTC Player.exe: starts the three show programs, keeps them running, and
stops them the safe way.

The three programs are separate processes so that a crash in one never takes
another with it: flamesafe.exe (the flame safety program), ltcplay.exe (the
show engine and its web page) and ltcplay-deck.exe (the Stream Deck).

HOW THEY ARE STOPPED, AND WHY IT MATTERS
flamesafe sends its safe zeros (three all-zero packets, then three with the
stream-terminated flag) only on a clean stop: Ctrl-C, Ctrl-Break or its stop
event. Windows' End task is a hard kill and sends nothing, leaving the last
packet on the wire. So every program is started in its own windowless
console and process group, and stopped with Ctrl-Break sent to that group.
This program never hard-kills a show program. If one will not stop, it says
so and leaves it running.

The order is deck, engine, flamesafe: flamesafe goes last so it is still
holding the flame universe at zero while the others close.

A stop is refused while the engine says a show is running or starting.

Modes (one per run):
  (none), --start   start everything if it is not running, open the page
                    as an Edge app window maximized on the show monitor
                    (Minimize, Maximize and Close in its title bar, or the
                    page's i button closes it)
  --page            the same page in an ordinary browser window
  --stop            stop everything, unless a show is running
  --task            what the scheduled task runs at sign-in, every minute
  --run             the supervisor loop itself (started by --start)
  --install-task    register the sign-in task for this Windows account
  --remove-task     remove it
  --rollback        run the previous version's installer
  --update          check GitHub Releases for a newer LTC Player; download
                    it, check its SHA-256 and run its installer
                    (--check-only: say only; exit 0 same, 10 newer, 20 no
                    network, 21 no release)
  --diagnostics     write the diagnostics bundle (a zip of logs, settings,
                    journals and versions, no media or keys) to the Desktop
  --open-settings   open the settings and logs folder
  --prune-installers  keep only the newest three saved installers
  --version, --self-check
  --quiet           no message boxes (for the installer and tests)

Files:
  %LOCALAPPDATA%\\ltcplay\\showpc.json   show folder, flamesafe config, port,
                                       show mode and the schedule file

FIRST RUN (Jeff, 2026-10-17): a fresh install had none of flamesafe.json,
ltcplay_schedule.json or ltcplay_fire_ice.json, so flamesafe and the
Stream Deck program were never started and the rack screen was a shell.
In fire_ice mode each missing one is written once from the shipped
examples in its safe form (first_run_defaults): flamesafe sends to this PC
only, the scheduler is a dry run, the show times are the example's. The
rack screen's Getting started card says so and where to change them.
  %LOCALAPPDATA%\\ltcplay\\first-run.json  what was written, and when
  %LOCALAPPDATA%\\ltcplay\\programs.json   each program: running, or why not
  %LOCALAPPDATA%\\ltcplay\\close-rack-screen, restart-engine
                                       requests from the page (Requests)

SHOW MODE (showpc.json "show_mode")
  "fire_ice" (the default, the Fire & Ice show PC): the engine runs as
      `ltc serve --schedule <schedule>`, so the scheduler, the show
      conductor, the flame link and the show runner all run, with
      ltcplay_fire_ice.json beside the schedule file (flamesafe_config,
      flame_controller). The Stream Deck program is started with the
      engine's own address, so its Abort, Hold, Resume and Reset reach the
      show conductor.
  "plain": the engine alone, no scheduler. The Stream Deck program is NOT
      started: with no show conductor its Abort could only disarm flames.
  %LOCALAPPDATA%\\ltcplay\\logs\\        one log per program, and this one's
  %PROGRAMDATA%\\LTC Player\\stop        present: stay stopped (see --task)
  %PROGRAMDATA%\\LTC Player\\stop-refused  why the last stop was refused
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import diagnostics
import housekeeping
import ltcwin
import updater

try:
    # ltcplay/showwindows.py, carried as a module of its own like audit.py
    # (this program does not bundle the ltcplay package).
    import showwindows
except ImportError:
    try:
        # The soak program imports this file and carries the package.
        from ltcplay import showwindows
    except ImportError:
        sys.path.append(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "ltcplay"))
        import showwindows

# Global\, not the session's own namespace (locked decision 21): a second
# supervisor started in another Windows session (a second sign-in, Remote
# Desktop, a task running as another user) must see the first one too, or two
# would start two sets of show programs.
MUTEX_NAME = "Global\\LTCPlayerSupervisor"
ERROR_ACCESS_DENIED = 5
TASK_NAME = "LTC Player"
DEFAULT_PORT = 7878
STOP_WAIT_S = 20.0
POLL_S = 0.5

# start order; the stop order is the reverse
PROGRAMS = ("flamesafe", "engine", "deck")
EXE = {"flamesafe": "flamesafe.exe", "engine": "ltcplay.exe",
       "deck": "ltcplay-deck.exe"}


# ------------------------------------------------------------ locations ---
def appdata_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    d = os.path.join(base, "ltcplay")
    os.makedirs(d, exist_ok=True)
    return d


def control_dir():
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    d = os.path.join(base, ltcwin.APP)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def stop_file():
    return os.path.join(control_dir(), "stop")


def refused_file():
    return os.path.join(control_dir(), "stop-refused")


def log_dir():
    d = os.path.join(appdata_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def settings_path():
    return os.path.join(appdata_dir(), "showpc.json")


def load_settings():
    """showpc.json, written with the defaults the first time."""
    defaults = {
        "show_folder": os.path.join(os.path.expanduser("~"), "Documents",
                                    "LTC Shows"),
        "flamesafe_config": os.path.join(appdata_dir(), "flamesafe.json"),
        "port": DEFAULT_PORT,
        "run_flamesafe": True,
        "run_deck": True,
        "open_page_at_sign_in": True,
        # The scheduling protection (show PC, 2026-10-04): the engine and
        # flamesafe at High, their show threads at Highest, the deck at
        # Above normal. false turns it off (A/B tests).
        "priority_boost": True,
        # BENCH BUILD, opt in (bench 2026-10-05): MadMapper and BEYOND at
        # Below normal, and MadMapper kept off two logical CPUs that are
        # left to LTC Player alone (contain.py). Off unless Jeff says.
        "contain_third_party": False,
        # MadMapper's own "Increase MadMapper Process Priority" preference
        # turns itself back on at every MadMapper restart and starts it at
        # High, which stalled the engine in the soak (2026-10-07). "normal"
        # puts it back to Normal whenever it is found at High or above;
        # "leave" leaves MadMapper's priority alone.
        "madmapper_priority": "normal",
        # The rack screen page (/remote), maximized on this monitor:
        # 1 is the main display, 2 the next one Windows lists, and so on.
        "page_monitor": 1,
        # Extra room under the page's Abort bar, in the page's own pixels,
        # for a window that still reaches under the taskbar (0: none; the
        # page also measures it by itself, 2026-10-26).
        "page_bottom_pad": 0,
        # Jeff's field report, 2026-10-29 (showwindows.py): after a reboot
        # BEYOND came up over the rack screen. The rack screen is raised
        # again when a BEYOND or MadMapper window covers it (false: never).
        # MadMapper's output watch reads its switches from
        # ltcplay_fire_ice.json (output_settings), the engine's own file,
        # so the two never disagree.
        "rack_screen_keep_in_front": True,
        "show_mode": "fire_ice",
        "schedule": os.path.join(appdata_dir(), "ltcplay_schedule.json"),
    }
    p = settings_path()
    doc = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            log(f"{p} could not be read ({e}); using the defaults")
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
    else:
        try:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(defaults, fh, indent=2)
                fh.write("\n")
        except OSError:
            pass
    out = dict(defaults)
    out.update({k: v for k, v in doc.items() if k in defaults})
    try:
        out["port"] = int(out["port"])
    except (TypeError, ValueError):
        out["port"] = DEFAULT_PORT
    return out


# ------------------------------------------------------------- logging ---
_LOG = None
_AUDIT = []


def audit_event(action, target="", result="accepted", reason=""):
    """One line in the supervisor's audit day file (ltcplay/audit.py, which
    the app carries as a module of its own: this program does not bundle
    the ltcplay package). Started on first use; never raises, never waits,
    and a log that cannot be written changes nothing here."""
    try:
        if not _AUDIT:
            try:
                import audit as audit_mod
            except ImportError:
                sys.path.append(os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__)))), "ltcplay"))
                import audit as audit_mod
            _AUDIT.append(audit_mod.AuditLog(
                audit_mod.default_folder(appdata_dir()),
                "supervisor").start())
        _AUDIT[0].event(user="system", surface="supervisor", action=action,
                        target=target, result=result, reason=reason,
                        source="LTC Player.exe")
    except Exception:                            # noqa: BLE001
        pass


def audit_flush():
    try:
        if _AUDIT:
            _AUDIT[0].flush(1.0)
    except Exception:                            # noqa: BLE001
        pass


def log(msg):
    global _LOG
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        if _LOG is None:
            path = os.path.join(log_dir(), "supervisor.log")
            housekeeping.rotate(path)
            _LOG = open(path, "a", encoding="utf-8")
        _LOG.write(line + "\n")
        _LOG.flush()
    except OSError:
        pass


def rotate_own_log():
    """The supervisor's own log, rotated by generations while it runs
    (it holds the handle, so only it can). Never raises."""
    global _LOG
    path = os.path.join(log_dir(), "supervisor.log")
    try:
        if os.path.getsize(path) <= housekeeping.LOG_LIMIT:
            return
        if _LOG is not None:
            _LOG.close()
            _LOG = None
        housekeeping.rotate(path)
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


def rotate(path, limit=housekeeping.LOG_LIMIT):
    """A program's log at its start: generations .1, .2, .3
    (housekeeping.rotate), so the newest few are always there for the
    diagnostics bundle and nothing grows without end."""
    housekeeping.rotate(path, limit)


QUIET = False


def tell(text, title=ltcwin.APP, error=False):
    log(text.replace("\n", " "))
    if QUIET or not ltcwin.WINDOWS:
        return
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, text, title,
                                     0x10 if error else 0x40)


def ask(text, title=ltcwin.APP):
    if QUIET or not ltcwin.WINDOWS:
        return True
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, text, title, 0x24) == 6


# --------------------------------------------------------- Windows bits ---
def _k32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


_MUTEX = None


LOCAL_MUTEX_NAME = "Local\\LTCPlayerSupervisor"
ERROR_FILE_NOT_FOUND = 2


def _mutex_there(k, name):
    """True when a mutex called `name` exists, even one this account may
    not open (another user's): only "not found" means it is not."""
    import ctypes
    k.OpenMutexW.restype = ctypes.c_void_p
    h = k.OpenMutexW(0x00100000, False, name)          # SYNCHRONIZE
    if h:
        k.CloseHandle(ctypes.c_void_p(h))
        return True
    return ctypes.get_last_error() != ERROR_FILE_NOT_FOUND


def take_mutex():
    """True if this is now the only supervisor on this machine. Global\
    first; "access denied" there means another user's supervisor holds it
    only when that mutex is really there. An account that may not make
    Global\ names at all falls back to Local\ and logs it loudly."""
    global _MUTEX
    if not ltcwin.WINDOWS:
        return True
    import ctypes
    k = _k32()
    k.CreateMutexW.restype = ctypes.c_void_p
    for name in (MUTEX_NAME, LOCAL_MUTEX_NAME):
        h = k.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if h:
            if err == 183:          # ERROR_ALREADY_EXISTS
                k.CloseHandle(ctypes.c_void_p(h))
                return False
            _MUTEX = h
            if name == LOCAL_MUTEX_NAME:
                log(f"WARNING: the supervisor's lock could not be made "
                    f"machine wide ({MUTEX_NAME}), so it is {name}: a "
                    f"supervisor in another Windows session would NOT be "
                    f"seen. Run only one LTC Player on this machine.")
            return True
        if err == ERROR_ACCESS_DENIED and _mutex_there(k, name):
            return False            # another user's supervisor
        log(f"the supervisor's lock {name} could not be made (error {err})")
    log("not starting a second supervisor on a guess: neither lock could "
        "be made")
    return False


def supervisor_running():
    if not ltcwin.WINDOWS:
        return False
    k = _k32()
    return any(_mutex_there(k, n) for n in (MUTEX_NAME, LOCAL_MUTEX_NAME))


_HANDLER = []


def ignore_console_events():
    """While attached to a program's console to send it Ctrl-Break, this
    process must not be stopped by that same event."""
    if not ltcwin.WINDOWS:
        return
    import ctypes
    from ctypes import wintypes
    H = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    h = H(lambda ev: True)
    _HANDLER.append(h)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(h, True)


def send_ctrl_break(pid):
    """Ctrl-Break to the process group `pid` leads, in its own console.
    Returns '' when sent, or why not."""
    if not ltcwin.WINDOWS:
        import signal
        try:
            os.kill(pid, signal.SIGINT)
            return ""
        except OSError as e:
            return str(e)
    import ctypes
    k = _k32()
    k.FreeConsole()
    if not k.AttachConsole(pid):
        return f"could not reach its console (error {ctypes.get_last_error()})"
    try:
        if not k.GenerateConsoleCtrlEvent(ltcwin.CTRL_BREAK_EVENT, pid):
            return f"Ctrl-Break was not sent (error {ctypes.get_last_error()})"
        return ""
    finally:
        time.sleep(0.1)
        k.FreeConsole()


def keep_awake(on=True):
    """No sleep and no screen-off while the show programs run."""
    if not ltcwin.WINDOWS:
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM, ES_DISPLAY = 0x80000000, 0x1, 0x2
    flags = ES_CONTINUOUS | (ES_SYSTEM | ES_DISPLAY if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def boot_time():
    if not ltcwin.WINDOWS:
        return 0.0
    import ctypes
    k = ctypes.windll.kernel32
    k.GetTickCount64.restype = ctypes.c_ulonglong
    return time.time() - k.GetTickCount64() / 1000.0


def running_pids(exe_names):
    """{exe name: [pid, ...]} for processes started from this app folder."""
    out = {n: [] for n in exe_names}
    if not ltcwin.WINDOWS:
        return out
    import ctypes
    from ctypes import wintypes
    k = _k32()
    psapi = ctypes.WinDLL("psapi")
    arr = (wintypes.DWORD * 8192)()
    got = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(got)):
        return out
    here = os.path.normcase(ltcwin.app_dir())
    k.OpenProcess.restype = ctypes.c_void_p
    for pid in arr[:got.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf,
                                            ctypes.byref(n)):
                path = os.path.normcase(buf.value)
                name = os.path.basename(buf.value)
                if os.path.dirname(path) == here and name in out:
                    out[name].append(pid)
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    return out


def pid_alive(pid):
    if not ltcwin.WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = _k32()
    k.OpenProcess.restype = ctypes.c_void_p
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
        return code.value == 259    # STILL_ACTIVE
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


# ------------------------------------------------------- the show state ---
def show_running(port):
    """(True, why) while the engine says a show is running or starting.
    An engine that does not answer has no show running."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=3) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False, ""
    if isinstance(doc, dict) and (doc.get("running") or doc.get("starting")):
        return True, ("A show is running. Press Stop on the show page "
                      "first, then try again.")
    return False, ""


def engine_restart_refusal(port):
    """Why the engine may not be restarted now, in the engine's own words
    (its restart-check: a show running or starting, an announcement
    playing, a flame group armed or asked for, flamesafe's state not seen,
    live flame programming or fire flames while scrubbing on, a purge, the
    Fireball, a laser armed or in test), or "" when it may. Fails closed:
    an engine that does not answer, or answers with anything else, may not
    be restarted (2026-10-28). The second check, right before the stop, so
    nothing that started since the page's press is cut."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/remote/restart-check",
                timeout=3) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                      # noqa: BLE001
        return (f"the engine did not say whether it may be restarted "
                f"({type(e).__name__}), so it is left running")
    if not isinstance(doc, dict) or not isinstance(doc.get("refusal"), str):
        return ("the engine's answer could not be read, so it is left "
                "running")
    return doc["refusal"]


def engine_restart_blocked(port):
    """(True, why) when the engine may not be restarted now: a show
    running or starting (show_running), else the engine's own
    restart-check (engine_restart_refusal, failing closed). Asked for
    every restart-engine request, right before the stop."""
    busy, why = show_running(port)
    if busy:
        return True, why
    why = engine_restart_refusal(port)
    return bool(why), why


def config_hash(path):
    """sha256 of a settings file's bytes as a program reads them (a UTF-8
    byte order mark and Windows line ends do not count), or None when it
    is not there. The same as ltcplay/configwatch.py's file_hash: the
    engine compares the two to tell which program runs an older copy."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


DECK_LAYOUT_FILE = "ltcplay_deck.json"


def files_read(name, settings):
    """The settings files program `name` reads at its start, beyond its
    command line: flamesafe and the deck read flamesafe's config, the deck
    its key layout too. The engine notes its own (configwatch.py)."""
    fs = (settings or {}).get("flamesafe_config")
    out = []
    if name in ("flamesafe", "deck") and fs:
        out.append(os.path.abspath(fs))
    if name == "deck":
        out.append(os.path.join(appdata_dir(), DECK_LAYOUT_FILE))
    return out


def engine_answers(port):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ------------------------------------------------------ the supervisor ---
class Program:
    def __init__(self, name):
        self.name = name
        self.exe = os.path.join(ltcwin.app_dir(), EXE[name])
        self.proc = None
        self.pid = None         # also set for a program adopted at start
        self.started = 0.0
        self.started_wall = 0.0
        self.read = {}          # {settings file: config_hash} at its start
        self.backoff = 2.0
        self.next_try = 0.0
        self.said_why_not = ""
        self.out = None

    def alive(self):
        if self.proc is not None:
            return self.proc.poll() is None
        return self.pid is not None and pid_alive(self.pid)

    def start(self, args, settings=None):
        path = os.path.join(log_dir(), f"{self.name}.log")
        rotate(path)
        self.out = open(path, "a", encoding="utf-8")
        self.out.write(time.strftime("\n===== %Y-%m-%d %H:%M:%S started by "
                                     "LTC Player =====\n"))
        self.out.flush()
        flags = 0
        if ltcwin.WINDOWS:
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW)
        # No bench test hook ever reaches a show program (safety audit of
        # bench-build, P1): the soak starts its own programs and sets them
        # there; the supervisor never passes one on, whatever is left in
        # this account's environment.
        env = ltcwin.without_bench_hooks(os.environ)
        env.pop(ltcwin.PRIORITY_ENV, None)
        if priority_boost_on():
            env[ltcwin.PRIORITY_ENV] = "high"
        # What it is about to read, hashed BEFORE it starts, so an edit
        # while it starts counts as newer than its copy (2026-10-28).
        self.read = {f: config_hash(f) for f in files_read(self.name,
                                                            settings)}
        env.pop(ltcwin.FLAMESAFE_ENV, None)
        if settings is not None and settings.get("flamesafe_config"):
            # One flamesafe config for all three programs (P1-4): the same
            # path flamesafe and the deck get on their command lines.
            env[ltcwin.FLAMESAFE_ENV] = os.path.abspath(
                settings["flamesafe_config"])
        self.proc = subprocess.Popen([self.exe] + args,
                                     stdin=subprocess.DEVNULL,
                                     stdout=self.out,
                                     stderr=subprocess.STDOUT,
                                     cwd=appdata_dir(),
                                     creationflags=flags, env=env)
        self.pid = self.proc.pid
        self.started = time.monotonic()
        self.started_wall = time.time()
        log(f"started {EXE[self.name]} (pid {self.pid}): {' '.join(args)}")

    def exited(self):
        code = self.proc.returncode if self.proc is not None else "?"
        ran = time.monotonic() - self.started
        if ran > 60:
            self.backoff = 2.0
        log(f"{EXE[self.name]} stopped by itself (exit code {code}) after "
            f"{ran:.0f} s; starting it again in {self.backoff:.0f} s")
        audit_event("program-exited", EXE[self.name], "error",
                    f"exit code {code} after {ran:.0f} s")
        self.next_try = time.monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, 60.0)
        self._forget()

    def _forget(self):
        self.proc = None
        self.pid = None
        if self.out is not None:
            try:
                self.out.close()
            except OSError:
                pass
            self.out = None

    def stop(self):
        """Ctrl-Break, then wait. Returns '' if it stopped, or why not."""
        if not self.alive():
            self._forget()
            return ""
        log(f"stopping {EXE[self.name]} (pid {self.pid}) with Ctrl-Break")
        why = send_ctrl_break(self.pid)
        if why:
            log(f"{EXE[self.name]}: {why}")
        deadline = time.monotonic() + STOP_WAIT_S
        while time.monotonic() < deadline:
            if not self.alive():
                log(f"{EXE[self.name]} stopped cleanly")
                self._forget()
                return ""
            time.sleep(0.1)
        return (f"{EXE[self.name]} did not stop within {STOP_WAIT_S:.0f} s "
                f"and is still running ({why or 'Ctrl-Break was sent'}). It "
                f"was NOT forced to quit.")


def priority_boost_on(settings=None):
    try:
        settings = settings or load_settings()
        return settings.get("priority_boost", True) is not False
    except Exception:
        return True


def contain_on(settings=None):
    try:
        if settings is None:
            settings = load_settings()
        return settings.get("contain_third_party", False) is True
    except Exception:
        return False


CONTAIN_EVERY_S = 3.0


def start_containment():
    """A contain.Containment when showpc.json turns it on, else None.
    Never raises."""
    if not contain_on():
        log("third-party containment OFF (showpc.json "
            "\"contain_third_party\": false)")
        return None
    try:
        import contain
        c = contain.Containment(log=log)
        log("third-party containment ON: MadMapper and BEYOND at Below "
            "normal as each appears; " + c.describe())
        return c
    except Exception as e:
        log(f"third-party containment could not start "
            f"({type(e).__name__}: {e}); nothing was changed")
        return None


MADMAPPER_EVERY_S = 5.0
HIGH_OR_ABOVE = (0x80, 0x100)           # HIGH, REALTIME priority classes


class MadMapperPriority:
    """Every MADMAPPER_EVERY_S: each MadMapper process found at High or
    above is set to Normal through SetPriorityClass, with one log line each
    time. A new MadMapper process (a restart) is found and set the same
    way. Only processes contain.which() names MadMapper are ever touched:
    never the engine, the deck or flamesafe. A refusal (access denied) is
    said once per process and never raised."""

    def __init__(self, log=None, procs=None, win=None):
        import contain
        self.contain = contain
        self.log = log or (lambda t: None)
        self.procs = procs or contain.list_processes
        self.win = win or contain.WinProc()
        self.refused = set()

    def tick(self):
        c = self.contain
        seen = set()
        for pid, exe in sorted(self.procs().items()):
            if c.which(exe)[0] != "MadMapper":
                continue
            seen.add(pid)
            cls = self.win.priority(pid)
            if cls not in HIGH_OR_ABOVE:
                continue
            was = c.PRIORITY_NAMES.get(cls, hex(cls))
            why = self.win.set_priority(pid, c.NORMAL)
            if not why:
                self.refused.discard(pid)
                self.log(f"note: MadMapper ({exe}, pid {pid}) was running "
                         f"at {was} priority; set to Normal so it cannot "
                         f"hold up the show engine (showpc.json "
                         f"\"madmapper_priority\": \"normal\")")
            elif pid not in self.refused:
                self.refused.add(pid)
                self.log(f"note: MadMapper ({exe}, pid {pid}) is running at "
                         f"{was} priority and could not be set to Normal: "
                         f"{why}. Left as it is; untick Increase MadMapper "
                         f"Process Priority in MadMapper's preferences")
        self.refused &= seen


def start_madmapper_priority(settings):
    """A MadMapperPriority unless showpc.json says "leave". Never raises."""
    want = str(settings.get("madmapper_priority", "normal")).strip().lower()
    if want == "leave":
        log("MadMapper priority: left alone (showpc.json "
            "\"madmapper_priority\": \"leave\")")
        return None
    if want != "normal":
        log(f"MadMapper priority: showpc.json \"madmapper_priority\" is "
            f"{want!r}, which is not \"normal\" or \"leave\"; using "
            f"\"normal\"")
    try:
        m = MadMapperPriority(log=log)
    except Exception as e:
        log(f"MadMapper priority could not be watched ({type(e).__name__}: "
            f"{e}); nothing was changed")
        return None
    log("MadMapper priority: set back to Normal whenever MadMapper is found "
        "at High or above (checked every 5 s)")
    return m


def wanted_args(settings):
    port = settings["port"]
    fs = os.path.abspath(settings["flamesafe_config"])
    fs_ok = bool(settings["run_flamesafe"]) and os.path.isfile(fs)
    out = {}
    out["flamesafe"] = ([fs], "") if fs_ok else (
        None, f"flamesafe is not started: no config at {fs}" if
        settings["run_flamesafe"] else "flamesafe is switched off in "
        "showpc.json")
    folder = settings["show_folder"]
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    mode = settings.get("show_mode") or "fire_ice"
    if mode not in SHOW_MODES:
        # A typo must not quietly drop the scheduler and the conductor.
        out["engine"] = (None, f"show_mode {mode!r} in showpc.json is not "
                         f"one of {', '.join(SHOW_MODES)}, so the engine is "
                         f"not started. Fix it in showpc.json.")
        out["deck"] = (None, "the Stream Deck program is not started: the "
                       "engine is not running")
        return out
    # The show network (Jeff, 2026-10-28: the page never opened from his
    # Mac): the address saved on the rack screen's Show network card, on
    # the show PC, when there is one; else this PC only. Never every
    # interface. The installer's firewall rule lets this PC's own subnet in.
    engine = ["serve", "--folder", folder, "--port", str(port),
              "--no-browser", "--network-if-saved"]
    if mode == "fire_ice":
        engine += ["--schedule", settings["schedule"]]
        # Operator announcements (announce.py, Jeff 2026-10-26), always on
        # in fire_ice mode, from ltcplay_announce.json beside the schedule.
        # Safe with nothing set up: a missing file, no output device or a
        # missing recording each refuse every press with a sentence on the
        # rack screen, and nothing plays to the system default, ever.
        engine += ["--announce", announce_path(settings)]
    out["engine"] = (engine, "")
    url = f"http://127.0.0.1:{port}"
    if mode != "fire_ice":
        out["deck"] = (None, "the Stream Deck program is not started: its "
                       "Abort, Hold, Resume and Reset need the engine's show "
                       "conductor, which runs only with show_mode "
                       "\"fire_ice\" in showpc.json")
    elif not settings["run_deck"]:
        out["deck"] = (None, "the Stream Deck program is switched off in "
                       "showpc.json")
    elif not fs_ok:
        out["deck"] = (None, "the Stream Deck program is not started: it "
                       "needs flamesafe's config")
    else:
        out["deck"] = (["--flamesafe-config", fs, "--ltcplay-url", url], "")
    return out


SHOW_MODES = ("fire_ice", "plain")


class SettingsWatch:
    """showpc.json, followed while the supervisor runs (soak 2026-10-21:
    show_folder was changed by hand, and the engine went on serving
    Documents\\LTC Shows, because this file was read only when the
    supervisor started). poll() is the settings read again when the file
    changed since the last look, else None. Never raises."""

    def __init__(self, path=None, read=None):
        self.path = path or settings_path()
        self._read = read or load_settings
        self._stamp = self._look()

    def _look(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def poll(self):
        st = self._look()
        if st == self._stamp:
            return None
        self._stamp = st
        try:
            return self._read()
        except Exception as e:                 # noqa: BLE001
            log(f"{self.path} changed but could not be read ({e}); the "
                f"settings in use stay as they were")
            return None


def show_folder_change(old, new):
    """The sentence when showpc.json now names another show folder than
    the one the engine was started with, else "". Only the show folder
    is followed while running; every other setting still takes a restart
    of LTC Player, as before."""
    a = os.path.normcase(os.path.abspath(str(old.get("show_folder") or "")))
    b = os.path.normcase(os.path.abspath(str(new.get("show_folder") or "")))
    if not new.get("show_folder") or a == b:
        return ""
    return f"showpc.json now names the show folder {new['show_folder']}"


RESTART_BUSY_EVERY_S = 5.0


FIRST_RUN_FILE = "first-run.json"     # what this wrote, for the rack screen
PROGRAMS_FILE = "programs.json"       # each program: running, or why not
SCREEN_CLOSE_FILE = "close-rack-screen"   # the engine asks: close Edge
RESTART_ENGINE_FILE = "restart-engine"    # the engine asks: restart it
STATUS_MIRROR_PORT = 5574             # flamesafe's status copy for the engine


def example_flamesafe_path():
    """flamesafe.example.json as the installer leaves it beside the
    programs (the repo's flamesafe/ folder from source)."""
    beside = os.path.join(ltcwin.app_dir(), "flamesafe.example.json")
    if os.path.isfile(beside):
        return beside
    root = os.path.dirname(os.path.dirname(ltcwin.app_dir()))
    return os.path.join(root, "flamesafe", "flamesafe.example.json")


def example_schedule_path():
    beside = os.path.join(ltcwin.app_dir(), "ltcplay_schedule.example.json")
    if os.path.isfile(beside):
        return beside
    root = os.path.dirname(os.path.dirname(ltcwin.app_dir()))
    return os.path.join(root, "ltcplay", "ltcplay_schedule.example.json")


ANNOUNCE_FILE = "ltcplay_announce.json"
ANNOUNCE_DIR = "Announcements"
ANNOUNCE_IDS = ("delayed", "cancellation", "cannot_continue")
REMOTE_FILE = "ltcplay_remote.json"


def announce_path(settings):
    """ltcplay_announce.json, beside the schedule file (where the Fire &
    Ice file is too)."""
    return os.path.join(os.path.dirname(os.path.abspath(
        settings["schedule"])), ANNOUNCE_FILE)


def first_run_announce_doc():
    """The first-run announcements file: NO output device yet (it has to
    be the output's exact name, and the system default is never used), and
    the three recordings in the Announcements folder beside it, which the
    rack screen reads as Recording missing until they are copied there."""
    return {"device": "",
            "files": {i: f"{ANNOUNCE_DIR}/{i}.wav" for i in ANNOUNCE_IDS}}


def first_run_booth_doc():
    """The first-run rack screen settings, written only when there are none
    yet: the booth speaker block (boothaudio.py), off until its device is
    set by its exact name."""
    return {"booth_alert": {"device": "", "channels": [1, 2], "files": {}}}


def safe_flamesafe_config(example_doc):
    """The first-run flamesafe config: the shipped example, sending to
    this PC only (127.0.0.1, which the example already does), with every
    one of its groups (the Stream Deck keys four and arms the rest from a
    screen, 2026-10-26), and the status copy the engine's flame lamps
    read. Unconfirmed, so flamesafe runs it as the
    bench fixture it is; nothing here can reach a flame controller."""
    doc = dict(example_doc)
    doc["confirmed"] = False
    doc["destination"] = {"ip": "127.0.0.1",
                          "port": int((example_doc.get("destination") or {})
                                      .get("port", 5568))}
    link = dict(doc.get("link") or {})
    link.setdefault("status_mirror_port", STATUS_MIRROR_PORT)
    doc["link"] = link
    doc["groups"] = list(doc.get("groups") or [])
    doc["note"] = ("FIRST-RUN DEFAULT, written by LTC Player so flamesafe "
                   "and the Stream Deck start on a fresh PC. It sends to "
                   "this PC only (destination 127.0.0.1), never to a flame "
                   "controller. Before any flame hardware is connected: set "
                   "the real destination, universe, groups and link.key, "
                   "and set confirmed to true. " + str(doc.get("note") or ""))
    return doc


def first_run_defaults(settings, write=True):
    """A fresh install has no flamesafe.json, no schedule and no Fire & Ice
    settings, so flamesafe and the Stream Deck were never started and the
    rack screen was a shell (Jeff, 2026-10-17). Each missing file is
    written once, from the shipped examples, in its safe form: flamesafe
    to loopback, the scheduler a dry run (scheduler_performs false), the
    example's show times. What was written is recorded in first-run.json,
    which the rack screen reads to say "the EXAMPLE times". A file that is
    already there is never touched. Returns the names written."""
    written = {}
    fs = os.path.abspath(settings["flamesafe_config"])
    sched = os.path.abspath(settings["schedule"])
    fi = os.path.join(os.path.dirname(sched), "ltcplay_fire_ice.json")
    if not os.path.isfile(fs):
        try:
            with open(example_flamesafe_path(), encoding="utf-8-sig") as fh:
                example = json.load(fh)
            doc = safe_flamesafe_config(example)
            if write:
                _write_json(fs, doc)
            written["flamesafe"] = fs
        except (OSError, ValueError) as e:
            log(f"first run: flamesafe.json could not be written from the "
                f"example ({e}); flamesafe and the Stream Deck stay off")
    if not os.path.isfile(sched):
        try:
            with open(example_schedule_path(), encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if write:
                _write_json(sched, doc)
            written["schedule"] = sched
        except (OSError, ValueError) as e:
            log(f"first run: the example schedule could not be written "
                f"({e}); no show is scheduled")
    if not os.path.isfile(fi) and (os.path.isfile(fs) or
                                   "flamesafe" in written):
        doc = {"scheduler_performs": False,
               "flamesafe_config": fs,
               "notes": "FIRST-RUN DEFAULT, written by LTC Player. "
                        "scheduler_performs false: the scheduler decides "
                        "but starts nothing. Set it to true, and add the "
                        "madmapper and beyond blocks, when the rig is "
                        "connected (INSTALL.md)."}
        try:
            if write:
                _write_json(fi, doc)
            written["fire_ice"] = fi
        except OSError as e:
            log(f"first run: ltcplay_fire_ice.json could not be written "
                f"({e})")
    ann = announce_path(settings)
    if not os.path.isfile(ann):
        try:
            if write:
                _write_json(ann, first_run_announce_doc())
                os.makedirs(os.path.join(os.path.dirname(ann), ANNOUNCE_DIR),
                            exist_ok=True)
            written["announce"] = ann
        except OSError as e:
            log(f"first run: {ANNOUNCE_FILE} could not be written ({e}); "
                f"the rack screen says announcements are not set up")
    booth = os.path.join(appdata_dir(), REMOTE_FILE)
    if not os.path.isfile(booth):
        try:
            if write:
                _write_json(booth, first_run_booth_doc())
            written["booth"] = booth
        except OSError as e:
            log(f"first run: {REMOTE_FILE} could not be written ({e})")
    if written and write:
        for name, path in written.items():
            log(f"first run: wrote a safe default {name} file: {path}")
        try:
            _write_json(os.path.join(appdata_dir(), FIRST_RUN_FILE),
                        {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "written": written})
        except OSError:
            pass
    return written


def _write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


class ProgramsFile:
    """programs.json in the settings folder: for each program, whether it
    is running and, when it is not started, the sentence saying why
    (wanted_args). Written only when something changed, so the rack screen
    can say "the Stream Deck program is not running: it needs flamesafe's
    config" instead of "not heard". Never raises."""

    def __init__(self, path):
        self.path = path
        self.last = None

    def update(self, progs, want, supervisor=None):
        """`supervisor`: this process's own block (supervisor_block), for
        the engine's settings notice (ltcplay/configwatch.py)."""
        doc = {}
        for n in PROGRAMS:
            args, why = want.get(n, (None, ""))
            p = progs[n]
            alive = bool(p.alive())
            # When this supervisor started it (time.time()), so the engine
            # can tell a settings file changed after this program read it;
            # None for one it adopted, whose start it does not know.
            started = getattr(p, "started_wall", 0.0)
            doc[n] = {"running": alive,
                      "why": "" if args is not None else why}
            if alive and getattr(p, "proc", None) is not None and started:
                doc[n]["started"] = started
                read = getattr(p, "read", None)
                if read:
                    doc[n]["read"] = dict(read)
        if supervisor is not None:
            doc["supervisor"] = supervisor
        if doc == self.last:
            return
        self.last = doc
        try:
            _write_json(self.path, doc)
        except OSError:
            pass


def showpc_changed(loaded, now):
    """The showpc.json settings that differ between what this supervisor
    read at its start (`loaded`) and the file now (`now`), sorted, leaving
    out show_folder, which it follows by itself (SettingsWatch). Each takes
    a restart of LTC Player (2026-10-28, the rack screen says so)."""
    keys = set(loaded or {}) | set(now or {})
    return sorted(k for k in keys if k != "show_folder" and
                  (loaded or {}).get(k) != (now or {}).get(k))


def supervisor_block(started, showpc, changed, changed_at=""):
    """programs.json's "supervisor" block: when this process started, its
    showpc.json, and the settings in it changed since (showpc_changed)."""
    return {"started": started, "showpc": showpc,
            "showpc_changed": list(changed or []),
            "changed_at": changed_at if changed else ""}


def restart_note(name, progs, flamesafe_config, clock_hms=None):
    """The log line when `name` has just been started while other programs
    run (2026-10-28: at 01:54 flamesafe was restarted five times as its
    file was fixed, and the engine and the deck kept the copy they read at
    01:54:49). Names each running program that started before flamesafe's
    config last changed, so it runs with an older copy; "" when there is
    none. Every program reads flamesafe's config."""
    hms = clock_hms or (lambda t: time.strftime("%H:%M:%S",
                                                 time.localtime(t)))
    try:
        changed = os.path.getmtime(flamesafe_config)
    except (OSError, TypeError, ValueError):
        return ""
    me = progs[name]
    older = [f"{EXE[n]} (started {hms(progs[n].started_wall)})"
             for n in PROGRAMS
             if n != name and progs[n].alive() and progs[n].started_wall and
             progs[n].started_wall < changed]
    if not older:
        return ""
    return (f"{EXE[name]} started at {hms(me.started_wall)} with "
            f"{os.path.basename(flamesafe_config)} as changed at "
            f"{hms(changed)}; {' and '.join(older)} still run with the older "
            f"copy they read when they started. The rack screen says so "
            f"until LTC Player is stopped and started again.")


class Requests:
    """The engine's two requests, as files in the settings folder (the
    engine serves the rack screen; this process owns the windows and the
    programs): close-rack-screen closes the Edge window this process
    opened, restart-engine restarts ltcplay.exe between shows (a new show
    folder). A request older than this process is ignored."""

    def __init__(self, folder, since):
        self.folder = folder
        self.handled = {SCREEN_CLOSE_FILE: since, RESTART_ENGINE_FILE: since}

    def pending(self, name):
        path = os.path.join(self.folder, name)
        try:
            m = os.path.getmtime(path)
        except OSError:
            return False
        if m <= self.handled[name]:
            return False
        self.handled[name] = m
        try:
            os.remove(path)
        except OSError:
            pass
        return True


RACK_WINDOW = [None]          # the Popen of the Edge window this started


def close_rack_window():
    """The Edge window this process opened, closed. Edge is a view, not a
    show program, so ending it is safe; the engine and the show carry on.
    Returns a sentence for the log."""
    p = RACK_WINDOW[0]
    if p is None:
        return "no rack screen window was opened by this LTC Player"
    try:
        if p.poll() is not None:
            RACK_WINDOW[0] = None
            return "the rack screen window had already closed"
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            pass
    except OSError as e:
        return f"the rack screen window could not be closed: {e}"
    RACK_WINDOW[0] = None
    return "closed the rack screen window at the page's request"


def fire_ice_files(settings):
    """Sentences about what fire_ice mode needs and does not have yet, for
    the log (the engine still starts: its page says the same)."""
    if (settings.get("show_mode") or "fire_ice") != "fire_ice":
        return []
    out = []
    sched = settings["schedule"]
    if not os.path.isfile(sched):
        out.append(f"no schedule file at {sched}: the engine starts, but no "
                   f"show is scheduled")
    fi = os.path.join(os.path.dirname(sched), "ltcplay_fire_ice.json")
    if not os.path.isfile(fi):
        out.append(f"no ltcplay_fire_ice.json beside the schedule ({fi}): "
                   f"the scheduler stays a dry run, with no flame link")
    return out


# Set once Windows is shutting down or signing out: nothing is started or
# restarted after it.
import threading as _threading
ENDING = _threading.Event()


def end_session_stop(progs, wait_s=ltcwin.END_SESSION_WAIT_S,
                     breaker=None, clock=time.monotonic):
    """A Windows shutdown or sign-out (review of PR #38, P1-2): every show
    program gets its clean stop at once, the deck and the engine first and
    flamesafe last, so flamesafe's safe zeros go out before Windows ends
    it. No show-running refusal here: Windows is ending the session either
    way, and a clean stop is the only kind that sends zeros. Returns the
    names of the programs still running after `wait_s`."""
    ENDING.set()
    breaker = breaker or send_ctrl_break
    for name in reversed(PROGRAMS):
        p = progs[name]
        if p.alive():
            log(f"Windows is ending the session: stopping {EXE[name]} "
                f"(pid {p.pid}) with Ctrl-Break")
            why = breaker(p.pid)
            if why:
                log(f"{EXE[name]}: {why}")
    end = clock() + wait_s
    while clock() < end:
        if not any(progs[n].alive() for n in PROGRAMS):
            log("every show program stopped cleanly for the end of the "
                "session")
            return []
        time.sleep(0.05)
    left = [EXE[n] for n in PROGRAMS if progs[n].alive()]
    log(f"still running when Windows ended the session: {', '.join(left)}")
    return left


def _watch_end_session(progs):
    """The supervisor's own hidden window for WM_QUERYENDSESSION and
    WM_ENDSESSION. This process has no console, so no console event ever
    reaches it; without this a shutdown stopped nothing cleanly."""
    if not ltcwin.WINDOWS:
        return None
    done = _threading.Event()

    def stop():
        _threading.Thread(target=lambda: (end_session_stop(progs),
                                          done.set()),
                          daemon=True, name="ltcplay-end-session").start()
    handler = ltcwin.EndSession(stop, done, log=log)
    th = ltcwin.end_session_window(handler, "LTC Player end session")
    if th is None or not th.hwnd:
        log("could not make the shutdown window: a Windows shutdown or "
            "sign-out may not stop the show programs cleanly")
        return None
    return handler


def leave_at_end_of_session(ending, wait_s=ltcwin.END_SESSION_WAIT_S + 1.0):
    """Before the supervisor ends for a shutdown: the clean stops are done
    (or the wait is over), AND the window procedure's TRUE has reached
    Windows (re-review of #47, P2-e: a process that ends first answers 0,
    which Windows reads as a refusal)."""
    if ending is None:
        return
    ending.done.wait(wait_s)
    ending.let_windows_have_its_answer()


REFUSED_FILE = "ltcplay_engine_refused.txt"   # ltcplay/cli.py writes it


def engine_refusal(since):
    """The engine's own sentence for refusing to start (re-review of #46
    and #47, P2-g), when it wrote one at or after `since` (time.time()),
    else ""."""
    path = os.path.join(appdata_dir(), REFUSED_FILE)
    try:
        if os.path.getmtime(path) < since - 1.0:
            return ""
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    return " ".join(l.strip() for l in lines[1:] if l.strip())


def refusal_page(text):
    """A page for the rack screen saying why the engine did not start (the
    engine, which serves the rack screen, is not running to say it)."""
    import html
    path = os.path.join(appdata_dir(), "engine-refused.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><meta charset=utf-8><title>LTC Player did "
                 "not start</title><body style=\"background:#200;color:#fff;"
                 "font:28px sans-serif;padding:40px\"><h1>The show engine did "
                 "not start</h1><p>" + html.escape(text) + "</p><p>Fix it, "
                 "then start LTC Player again. Nothing was sent to the "
                 "rig.</p></body>")
    return path


def say_engine_refused(text, said, show=None):
    """Logged, and put on the rack screen, once per distinct sentence.
    `show` opens the page (show_refusal_page, which starts a browser);
    the selftest passes its own so no browser is started on a runner."""
    if not text or text == said[0]:
        return False
    said[0] = text
    log(f"the engine refused to start: {text}")
    try:
        page = refusal_page(text)
        (show or show_refusal_page)(page)
    except Exception as e:
        log(f"could not show the engine's refusal on the rack screen: {e}")
    return True


def show_refusal_page(page):
    """The refusal page as an app window on the rack monitor, or in the
    default browser."""
    if not ltcwin.WINDOWS:
        return
    cmd = rack_page_command(0, load_settings().get("page_monitor", 1),
                            monitors(), edge_exe(),
                            os.path.join(appdata_dir(), "rack-screen"),
                            url="file:///" + page.replace("\\", "/"))
    if cmd:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    else:
        os.startfile(page)


WINDOWS_EVERY_S = 2.0


def post_alert(port, kind, text=None):
    """The engine's alert of `kind` raised with `text`, or cleared when
    `text` is None (its alert API, from this machine). True once the engine
    took it (a clear with nothing open counts as done); a failure is
    logged."""
    body = {"raise": kind, "text": text} if text is not None else \
        {"clear": kind}
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/remote/alert",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if text is None and e.code == 409:
            return True                # nothing open to clear
        log(f"the engine refused the {kind} alert "
            f"{'raise' if text is not None else 'clear'} (HTTP {e.code})")
        return False
    except urllib.error.URLError as e:
        # Not connected (refused, or the connect itself timed out): it
        # never reached the engine, so it is sent again later.
        log(f"the {kind} alert {'raise' if text is not None else 'clear'} "
            f"did not reach the engine ({e.reason})")
        return False
    except TimeoutError:
        # Sent, and no answer in time (review B of #176): the engine may
        # have taken it, so it is never sent again and an Acknowledge is
        # never undone. Said in the log.
        log(f"the {kind} alert {'raise' if text is not None else 'clear'} "
            f"was sent but got no answer within 3 s; it is not sent again")
        return True
    except Exception as e:
        log(f"the {kind} alert {'raise' if text is not None else 'clear'} "
            f"did not reach the engine ({type(e).__name__}: {e})")
        return False


def output_settings(settings):
    """(check, monitor) for MadMapper's output watch, from
    ltcplay_fire_ice.json beside the schedule (madmapper_output_check,
    madmapper_output_monitor): the same file and keys the engine's rig
    check reads. Missing or wrong: (True, 0), said in the log."""
    try:
        fi = os.path.join(os.path.dirname(settings["schedule"]),
                          "ltcplay_fire_ice.json")
        with open(fi, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except Exception:
        return True, 0
    if not isinstance(doc, dict):
        return True, 0
    # Only for a show whose MadMapper runs on this PC, as the engine's
    # rig check (review B round 2 of #176): a stray MadMapper here never
    # raises an alert nothing would clear.
    mm = doc.get("madmapper")
    if not isinstance(mm, dict):
        log("MadMapper output watch: the show has no MadMapper "
            "(no madmapper block in ltcplay_fire_ice.json)")
        return False, 0
    if not showwindows.on_this_pc(mm.get("host", "127.0.0.1")):
        log(f"MadMapper output watch: the show's MadMapper is on another "
            f"machine ({mm.get('host')}), so its output is not watched here")
        return False, 0
    chk = doc.get("madmapper_output_check", True)
    mon = doc.get("madmapper_output_monitor", 0)
    if chk is not True and chk is not False:
        log(f"ltcplay_fire_ice.json madmapper_output_check is {chk!r}, not "
            f"true or false: the output is watched")
        chk = True
    if isinstance(mon, bool) or not isinstance(mon, int) or \
            not 0 <= mon <= 8:
        log(f"ltcplay_fire_ice.json madmapper_output_monitor is {mon!r}, "
            f"not 0 to 8: any display but the rack screen's counts")
        mon = 0
    return chk, mon


def soak_kit(settings):
    """True when ltcplay_fire_ice.json beside the schedule is a soak kit's
    (soak.py writes "soak_only": true, with the rig check off): the bench
    soak's shows are never held by the MadMapper output alert."""
    try:
        fi = os.path.join(os.path.dirname(settings["schedule"]),
                          "ltcplay_fire_ice.json")
        with open(fi, encoding="utf-8-sig") as fh:
            return json.load(fh).get("soak_only") is True
    except Exception:
        return False


def output_alert(settings, port, text):
    """OutputWatch's alert: the engine's video alert, except under a soak
    kit (said in the log instead). True once done."""
    if soak_kit(settings):
        log("MadMapper output: OFF, no alert raised (a soak kit's "
            "ltcplay_fire_ice.json is in use)")
        return True
    return post_alert(port, "output", text)


def start_window_watch(settings, port, api=None):
    """[True, RackKeeper or None, OutputWatch or None] (showwindows.py),
    as showpc.json says, each said in the log. Windows only; never
    raises."""
    out = [True, None, None]
    if not ltcwin.WINDOWS and api is None:
        return out
    try:
        api = api or showwindows.Win32()
    except Exception as e:
        log(f"the show PC's windows cannot be read ({type(e).__name__}: "
            f"{e}): the rack screen keeper and the MadMapper output watch "
            f"are off")
        return out
    if settings.get("rack_screen_keep_in_front", True) is not False:
        out[1] = showwindows.RackKeeper(
            api, monitor=settings.get("page_monitor", 1), log=log,
            opened=lambda: showwindows.opened_at(appdata_dir()))
        log(f"rack screen keeper: ON (display "
            f"{settings.get('page_monitor', 1)}): the rack screen is raised "
            f"again when a BEYOND or MadMapper window covers it (any, for "
            f"{showwindows.BOOT_SETTLE_S:g} s after start; then only one "
            f"that appeared in the last {showwindows.APP_SETTLE_S:g} s), "
            f"never over MadMapper's output or a window a person brought "
            f"forward (showpc.json \"rack_screen_keep_in_front\": false "
            f"turns it off)")
    else:
        log("rack screen keeper: OFF (showpc.json "
            "\"rack_screen_keep_in_front\": false)")
    chk, mon = output_settings(settings)
    if chk:
        page = settings.get("page_monitor", 1)
        out[2] = showwindows.OutputWatch(
            api, monitor=mon, log=log, rack=page if isinstance(page, int)
            else 1,
            alert=lambda text: output_alert(settings, port, text),
            clear=lambda: post_alert(port, "output"),
            epoch=lambda: ENGINE_STARTS[0])
        try:
            n = len(api.monitors())
        except Exception:
            n = 0
        if mon and n and mon > n:
            log(f"MadMapper output watch: madmapper_output_monitor is {mon} "
                f"but this PC has {n} display(s); the output reads as not "
                f"known until it is set right (ltcplay_fire_ice.json)")
        log(f"MadMapper output watch: ON, "
            + (f"display {mon}" if mon else "any display but the rack "
               "screen's") + " (ltcplay_fire_ice.json "
            "\"madmapper_output_check\": false turns it off). Read from "
            "the window list only; nothing is sent to MadMapper.")
    else:
        log("MadMapper output watch: OFF (ltcplay_fire_ice.json "
            "\"madmapper_output_check\": false)")
    return out


# How many times this supervisor started the engine: a new engine has no
# alerts, so the MadMapper output watch raises its alert again.
ENGINE_STARTS = [0]


def start_window_thread(settings, port):
    """start_window_watch, ticked every WINDOWS_EVERY_S on a daemon thread
    of its own until Windows ends the session. Never raises."""
    def loop():
        try:
            ws = start_window_watch(settings, port)
        except Exception as e:
            log(f"the window watches could not start ({type(e).__name__}: "
                f"{e})")
            return
        while not ENDING.is_set():
            for w in ws[1:]:
                if w is None:
                    continue
                try:
                    if isinstance(w, showwindows.RackKeeper) and \
                            RACK_WINDOW[0] is not None:
                        w.rack_pid = getattr(RACK_WINDOW[0], "pid", None)
                    w.tick()
                except Exception as e:
                    log(f"window watch: {type(e).__name__}: {e}")
            ENDING.wait(WINDOWS_EVERY_S)
    try:
        import threading
        t = threading.Thread(target=loop, daemon=True,
                             name="ltcplay-window-watch")
        t.start()
        return t
    except Exception as e:
        log(f"the window watches could not start ({type(e).__name__}: {e})")
        return False


def run_loop(open_page=False):
    if not take_mutex():
        log("another LTC Player supervisor is already running; leaving it")
        return 0
    ignore_console_events()
    settings = load_settings()
    port = settings["port"]
    log(f"supervisor starting: {ltcwin.version_line(ltcwin.APP)}, "
        f"settings {settings_path()}, show mode "
        f"{settings.get('show_mode') or 'fire_ice'}")
    if (settings.get("show_mode") or "fire_ice") == "fire_ice":
        first_run_defaults(settings)
    for why in fire_ice_files(settings):
        log(why)
    log(ltcwin.settings_folder_line())
    audit_event("supervisor-start", ltcwin.version_line(ltcwin.APP))
    log("scheduling protection " + (
        "ON: the engine and flamesafe run at High priority, their show "
        "threads at Highest, the Stream Deck program at Above normal; each "
        "program's log says what Windows agreed to" if priority_boost_on()
        else "OFF (showpc.json \"priority_boost\": false)"))
    keep_awake(True)
    contained = start_containment()
    next_contain = 0.0
    mm_priority = start_madmapper_priority(settings)
    next_mm = 0.0
    windows = None
    mm_failed = ""
    me_started = time.time()
    progs = {n: Program(n) for n in PROGRAMS}
    # Programs left running by an earlier supervisor (one that was ended
    # from outside) are adopted, never started twice.
    for name, pids in running_pids([EXE[n] for n in PROGRAMS]).items():
        for n in PROGRAMS:
            if EXE[n] == name and pids:
                progs[n].pid = pids[0]
                log(f"{name} was already running (pid {pids[0]}); adopted")
    handled_stop = 0.0
    refused_said = [""]
    page_opened = not open_page
    ending = _watch_end_session(progs)
    keeper = Housekeeper(settings)
    programs_file = ProgramsFile(os.path.join(appdata_dir(), PROGRAMS_FILE))
    requests = Requests(appdata_dir(), me_started)
    watch = SettingsWatch()
    # showpc.json as this process read it at its start: the rack screen's
    # settings notice names the settings changed since (2026-10-28).
    loaded_settings = dict(settings)
    sup_block = supervisor_block(me_started, settings_path(), [])
    folder_change = ""          # a hand edit of show_folder, not yet used
    folder_said = ""
    folder_next = 0.0
    while True:
        if ENDING.is_set():
            # The clean stops go out on the end-session thread; this
            # process must not end before they have.
            leave_at_end_of_session(ending)
            log("Windows is ending the session; supervisor exiting")
            keep_awake(False)
            return 0
        # A stop request newer than anything handled so far.
        try:
            m = os.path.getmtime(stop_file())
        except OSError:
            m = 0.0
        if m and m > handled_stop and m >= me_started - 1:
            handled_stop = m
            busy, why = show_running(port)
            if busy:
                log(f"stop refused: {why}")
                audit_event("stop", "every program", "refused", why)
                _write_refusal(why)
            else:
                audit_event("stop", "every program")
                audit_flush()
                failures = [w for w in (progs[n].stop()
                                        for n in reversed(PROGRAMS)) if w]
                if failures:
                    _write_refusal(" ".join(failures))
                    log("supervisor exiting with programs it could not stop")
                else:
                    log("everything stopped cleanly; supervisor exiting")
                keep_awake(False)
                if contained is not None:
                    try:
                        contained.release()
                    except Exception as e:
                        log(f"containment not ended cleanly: {e}")
                return 1 if failures else 0
        if requests.pending(SCREEN_CLOSE_FILE):
            log(close_rack_window())
        if requests.pending(RESTART_ENGINE_FILE):
            # A new show folder from the Programming screen: the engine
            # alone is restarted, between shows only, with the settings
            # read again. flamesafe and the deck are not touched.
            busy, why = engine_restart_blocked(port)
            if busy:
                log(f"the page asked for an engine restart; refused: {why}")
                audit_event("restart-engine", "engine", "refused", why)
            else:
                audit_event("restart-engine", "engine")
                settings = load_settings()
                why = progs["engine"].stop()
                said = why or ("the engine stopped cleanly and is started "
                               "again with " + str(settings["show_folder"]))
                log(f"the page asked for an engine restart (a new show "
                    f"folder): {said}")
        # showpc.json edited by hand: a new show folder reaches the engine
        # by the same restart as the page's, once no show is running.
        changed = watch.poll()
        if changed is not None:
            keys = showpc_changed(loaded_settings, changed)
            if keys != sup_block["showpc_changed"]:
                if keys:
                    log(f"showpc.json changed ({', '.join(keys)}); LTC "
                        f"Player uses it once it is stopped and started "
                        f"again")
                sup_block = supervisor_block(
                    me_started, settings_path(), keys,
                    time.strftime("%H:%M:%S"))
            why = show_folder_change(settings, changed)
            if why:
                folder_change = why
                settings = dict(settings, show_folder=changed["show_folder"])
                folder_next = 0.0
        if folder_change and time.monotonic() >= folder_next:
            folder_next = time.monotonic() + RESTART_BUSY_EVERY_S
            busy, why = (engine_restart_blocked(port)
                         if progs["engine"].alive() else (False, ""))
            if busy:
                if folder_said != folder_change:
                    folder_said = folder_change
                    log(f"{folder_change}; the engine is restarted with it "
                        f"once no show is running ({why})")
            else:
                audit_event("restart-engine", "engine", "done",
                            folder_change)
                why = progs["engine"].stop()
                log(f"{folder_change}: " + (why or "the engine stopped "
                    "cleanly and is started again with it"))
                folder_change = folder_said = ""
        want = wanted_args(settings)
        now = time.monotonic()
        if contained is not None and now >= next_contain:
            next_contain = now + CONTAIN_EVERY_S
            try:
                contained.tick()
            except Exception as e:
                log(f"containment check failed: {e}")
        if mm_priority is not None and now >= next_mm:
            next_mm = now + MADMAPPER_EVERY_S
            try:
                mm_priority.tick()
            except Exception as e:
                why = f"{type(e).__name__}: {e}"
                if why != mm_failed:
                    mm_failed = why
                    log(f"note: MadMapper priority check failed ({why}); "
                        f"carrying on")
        keeper.tick(now)
        for n in PROGRAMS:
            p = progs[n]
            args, why_not = want[n]
            if p.proc is not None and p.proc.poll() is not None:
                if n == "engine":
                    say_engine_refused(engine_refusal(p.started_wall),
                                       refused_said)
                p.exited()
            elif p.proc is None and p.pid is not None and not p.alive():
                log(f"{EXE[n]} (adopted, pid {p.pid}) has stopped")
                p._forget()
            if p.alive():
                continue
            if args is None:
                if why_not != p.said_why_not:
                    log(why_not)
                    p.said_why_not = why_not
                continue
            p.said_why_not = ""
            if now >= p.next_try and not ENDING.is_set():
                try:
                    p.start(args, settings)
                    if n == "engine":
                        ENGINE_STARTS[0] += 1
                    audit_event("program-start", EXE[n])
                    note = restart_note(n, progs,
                                        settings.get("flamesafe_config"))
                    if note:
                        log(note)
                except OSError as e:
                    log(f"could not start {EXE[n]}: {e}")
                    audit_event("program-start", EXE[n], "error", str(e))
                    p.next_try = now + p.backoff
                    p.backoff = min(p.backoff * 2, 60.0)
        programs_file.update(progs, want, sup_block)
        if not page_opened and engine_answers(port):
            page_opened = True
            open_page_now(port)
        if windows is None:
            # On a thread of its own (review A of #176): a window call or
            # an alert POST never holds up this loop.
            windows = start_window_thread(settings, port)
        time.sleep(POLL_S)


HOUSEKEEP_EVERY_S = 300.0


class Housekeeper:
    """Every HOUSEKEEP_EVERY_S while the supervisor runs (and once at its
    start): the supervisor's own log rotated, old soaks, journals and spare
    log generations pruned, a live program log that has grown past
    LIVE_LOG_WARN said once (it is rotated at that program's next start:
    the program holds it open, and no program is ever restarted for a
    log), and the free disk checked, said in the log on each change and
    written to the settings folder for the rack screen. Never raises."""

    def __init__(self, settings, every_s=HOUSEKEEP_EVERY_S):
        self.every_s = every_s
        self.next_at = 0.0
        self.disk_state = "ok"
        self.said_big = set()
        self.show_folder = settings.get("show_folder") or appdata_dir()

    def tick(self, now):
        if now < self.next_at:
            return
        self.next_at = now + self.every_s
        try:
            self.run()
        except Exception as e:                 # noqa: BLE001
            log(f"housekeeping failed ({type(e).__name__}: {e}); carrying on")

    def run(self):
        rotate_own_log()
        for line in housekeeping.prune(appdata_dir(), log_dir()):
            log("housekeeping: " + line)
        for name, size in housekeeping.oversize_logs(log_dir()):
            if name not in self.said_big:
                self.said_big.add(name)
                log(f"note: {name} is {size / 1024 ** 2:.0f} MB; it is "
                    f"rotated when its program next starts (no program is "
                    f"restarted for a log)")
        worst = ("ok", None, "")
        for path in (appdata_dir(), self.show_folder):
            st = housekeeping.disk_state(path)
            if st[0] == "critical" or (st[0] == "low" and worst[0] == "ok"):
                worst = st
        if worst[0] != self.disk_state:
            self.disk_state = worst[0]
            log(("DISK LOW: " if worst[0] != "ok" else "disk: ") +
                (worst[2] or "enough free space again"))
        housekeeping.write_disk_file(appdata_dir(), worst[0], worst[2])


def _write_refusal(why):
    try:
        token = open(stop_file(), encoding="utf-8").read().strip()
    except OSError:
        token = ""
    try:
        with open(refused_file(), "w", encoding="utf-8") as fh:
            fh.write(f"{token}\n{why}\n")
    except OSError as e:
        log(f"could not write {refused_file()}: {e}")


RACK_PAGE = "/remote"     # the rack screen page ("Rack screen")
# The most room the page keeps under its Abort bar (CSS px), whatever
# showpc.json page_bottom_pad says; the page caps it the same (PAD_MAX).
PAGE_BOTTOM_PAD_MAX = 160


def dip_rect(rect, dpi):
    """(left, top, right, bottom) in physical pixels at `dpi` -> the same
    in Edge's own (96 dpi) pixels, which --window-size and
    --window-position take: at Windows' 200 percent (192 dpi) a 2560x1504
    work area is 1280x752. Rounded inward, so the window is never larger
    than the work area. A missing or odd dpi is 96."""
    import math
    try:
        d = float(dpi)
    except (TypeError, ValueError):
        d = 96.0
    if not 48 <= d <= 960:
        d = 96.0
    k = 96.0 / d
    left, top, right, bottom = rect[:4]
    return (int(math.ceil(left * k)), int(math.ceil(top * k)),
            int(math.floor(right * k)), int(math.floor(bottom * k)))


def work_area(monitor_rect, work_rect):
    """The window's rectangle on one display: its WORK AREA (rcWork, the
    display less the taskbar and any docked bars), never the whole display
    (Jeff, 2026-10-26: sized to the monitor, the window ran 96 px under the
    always-on-top taskbar and cut the ABORT bar in half). A work area that
    is empty or not inside the display is the display."""
    ml, mt, mr, mb = monitor_rect[:4]
    wl, wt, wr, wb = work_rect[:4]
    if wr > wl and wb > wt and wl >= ml and wt >= mt and wr <= mr and \
            wb <= mb:
        return (wl, wt, wr, wb)
    return (ml, mt, mr, mb)


def monitors():
    """[(left, top, right, bottom, dpi)] of every display's WORK AREA (the
    display less the taskbar), in physical pixels, and its dpi, the main
    one first (Windows). Read per-monitor DPI aware, so the numbers are the
    real pixels whatever this process's own setting; rack_page_command
    turns them into Edge's 96 dpi pixels. [] when they cannot be read."""
    if not ltcwin.WINDOWS:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        found = []
        u32 = ctypes.WinDLL("user32")
        old_ctx = None
        try:
            # PER_MONITOR_AWARE_V2 (-4), for this thread only, put back
            # below: the rects and the dpi then agree (real pixels).
            u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            u32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            old_ctx = u32.SetThreadDpiAwarenessContext(
                ctypes.c_void_p(-4)) or None
        except (AttributeError, OSError):
            old_ctx = None          # Windows before 10 1703: not aware, 96
        try:
            shcore = ctypes.WinDLL("shcore")
            shcore.GetDpiForMonitor.argtypes = (
                wintypes.HMONITOR, ctypes.c_int,
                ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT))
        except (OSError, AttributeError):
            shcore = None

        def dpi_of(hmon):
            if old_ctx is None or shcore is None:
                return 96
            x, y = wintypes.UINT(), wintypes.UINT()
            try:
                if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x),
                                           ctypes.byref(y)) == 0 and x.value:
                    return int(x.value)
            except OSError:
                pass
            return 96

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        u32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR,
                                        ctypes.POINTER(MONITORINFO))
        u32.GetMonitorInfoW.restype = wintypes.BOOL
        PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                  wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                  wintypes.LPARAM)

        def cb(hmon, _hdc, _rect, _data):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r, w = mi.rcMonitor, mi.rcWork
                found.append((work_area(
                    (r.left, r.top, r.right, r.bottom),
                    (w.left, w.top, w.right, w.bottom)) + (dpi_of(hmon),),
                    bool(mi.dwFlags & 1)))
            return True
        try:
            u32.EnumDisplayMonitors(None, None, PROC(cb), 0)
        finally:
            if old_ctx is not None:
                try:
                    u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))
                except OSError:
                    pass
        found.sort(key=lambda m: (not m[1], m[0][0], m[0][1]))
        return [r for r, _main in found]
    except Exception as e:
        log(f"could not list the displays: {e}")
        return []


def edge_exe(env=None, isfile=os.path.isfile):
    env = os.environ if env is None else env
    for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"),
                 env.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application",
                             "msedge.exe")
            if isfile(p):
                return p
    return None


def rack_page_command(port, monitor, screens, edge, profile, url=None,
                      bottom_pad=0):
    """The command that opens the rack screen page as an ordinary Edge app
    window (its own profile, so it never asks first-run questions): a title
    bar with Minimize, Maximize and Close, maximized on display number
    `monitor` (1 = main) of `screens`, or None without Edge. An unknown
    display number means the main one. NOT a kiosk (Jeff, 2026-10-19: the
    kiosk window could not be minimized, and the Explorer windows the page
    opened came up behind it with no taskbar to find them on).
    `screens` are monitors()'s work areas, (left, top, right, bottom[,
    dpi]) in real pixels: the window's size and place are that WORK AREA
    in Edge's 96 dpi pixels (Jeff, 2026-10-26: sized to the whole 2560x1600
    display at 200 percent, it ran under the taskbar). `bottom_pad` (CSS
    px, showpc.json page_bottom_pad) is passed to the page as pad_bottom."""
    if not edge:
        return None
    url = url or f"http://127.0.0.1:{port}{RACK_PAGE}"
    try:
        pad = int(bottom_pad)
    except (TypeError, ValueError):
        pad = 0
    pad = min(pad, PAGE_BOTTOM_PAD_MAX)      # review of #115, P2-2
    if pad > 0 and url.startswith("http"):
        url += ("&" if "?" in url else "?") + f"pad_bottom={pad}"
    cmd = [edge, f"--app={url}", "--start-maximized", "--no-first-run",
           f"--user-data-dir={profile}"]
    if screens:
        i = monitor - 1 if isinstance(monitor, int) and \
            1 <= monitor <= len(screens) else 0
        dpi = screens[i][4] if len(screens[i]) > 4 else 96
        left, top, right, bottom = screens[i][:4]
        # The size: the work area's own width and height in Edge's pixels.
        _l, _t, w, h = dip_rect((0, 0, right - left, bottom - top), dpi)
        if w > 0 and h > 0:
            cmd.append(f"--window-size={w},{h}")
        # The PLACE is the work area's own corner in real pixels, from
        # MONITORINFO, not scaled (review of #115, P2-3): how Edge lays
        # displays of different scaling side by side in its own pixels is
        # not something this can prove, and a scaled corner could land on
        # the wrong display. The real corner is the display's own (0,0 on
        # the main one, the same either way); --start-maximized then fills
        # the work area of whichever display the window is on.
        cmd.append(f"--window-position={screens[i][0]},{screens[i][1]}")
    return cmd


def open_page_now(port):
    """The rack screen page (/remote), an app window maximized on the show
    monitor (showpc.json "page_monitor"); without Edge, in the default
    browser."""
    url = f"http://127.0.0.1:{port}{RACK_PAGE}"
    try:
        if ltcwin.WINDOWS:
            cfg = load_settings()
            cmd = rack_page_command(
                port, cfg.get("page_monitor", 1), monitors(),
                edge_exe(), os.path.join(appdata_dir(), "rack-screen"),
                bottom_pad=cfg.get("page_bottom_pad", 0))
            if cmd:
                RACK_WINDOW[0] = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
                log(f"opened the rack screen as an app window, maximized "
                    f"on the show monitor's work area (above the taskbar): "
                    f"{cmd[1]} ({' '.join(cmd[-2:])}). Its title "
                    f"bar has Minimize, Maximize and Close; the page's i "
                    f"button also closes it; the show keeps running.")
                return
            os.startfile(url)
        else:
            import webbrowser
            webbrowser.open(url)
    except OSError as e:
        log(f"could not open the page: {e}")


# ------------------------------------------------------------ the verbs ---
def cmd_start():
    try:
        os.remove(stop_file())
    except OSError:
        pass
    settings = load_settings()
    if not supervisor_running():
        args = [supervisor_exe()] if ltcwin.frozen() else [
            sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(args + ["--run"], cwd=appdata_dir(),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        log("started the supervisor")
    if QUIET:
        return 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if engine_answers(settings["port"]):
            open_page_now(settings["port"])
            return 0
        time.sleep(0.5)
    tell("LTC Player is starting, but the show page did not answer within "
         "30 seconds. Try 'LTC Player' again in a minute. The logs are in "
         + log_dir(), error=True)
    return 1


def cmd_page():
    """Start menu, "Rack screen in a normal window": the same page in the
    default browser's ordinary window (no kiosk), for a person who wants
    the taskbar and the close button. Starts LTC Player if it is not
    running."""
    settings = load_settings()
    try:
        os.remove(stop_file())
    except OSError:
        pass
    if not supervisor_running():
        args = [supervisor_exe()] if ltcwin.frozen() else [
            sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(args + ["--run"], cwd=appdata_dir(),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        log("started the supervisor (for the normal window)")
    url = f"http://127.0.0.1:{settings['port']}{RACK_PAGE}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if engine_answers(settings["port"]):
            break
        time.sleep(0.5)
    if ltcwin.WINDOWS:
        os.startfile(url)
    else:
        import webbrowser
        webbrowser.open(url)
    log(f"opened the rack screen in a normal browser window: {url}")
    return 0


def cmd_stop():
    settings = load_settings()
    busy, why = show_running(settings["port"])
    if busy:
        tell(why, error=True)
        return 3
    token = f"stop {time.time():.3f} {os.getpid()}"
    with open(stop_file(), "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    if not supervisor_running():
        left = {k: v for k, v in running_pids(EXE.values()).items() if v}
        if left:
            tell("These LTC Player programs are running on their own, not "
                 "under LTC Player: " + ", ".join(left) + ". Stop each one "
                 "with Ctrl+C in its window. Never use End task: it stops "
                 "flamesafe without its safe zeros.", error=True)
            return 4
        tell("LTC Player was not running. It stays stopped until you start "
             "it or restart the computer.")
        return 0
    deadline = time.monotonic() + 3 * STOP_WAIT_S + 15
    while time.monotonic() < deadline:
        if not supervisor_running():
            break
        try:
            lines = open(refused_file(), encoding="utf-8").read().splitlines()
            if lines and lines[0].strip() == token:
                tell("LTC Player did not stop. " + " ".join(lines[1:]),
                     error=True)
                return 3
        except OSError:
            pass
        time.sleep(0.5)
    else:
        tell("LTC Player did not finish stopping within a minute. Look in "
             + log_dir(), error=True)
        return 5
    try:
        lines = open(refused_file(), encoding="utf-8").read().splitlines()
        if lines and lines[0].strip() == token:
            tell("LTC Player stopped, but: " + " ".join(lines[1:]),
                 error=True)
            return 3
    except OSError:
        pass
    tell("LTC Player has stopped, and flamesafe sent its safe zeros. It "
         "stays stopped until you start it or restart the computer.")
    return 0


def cmd_task():
    """The sign-in task. It fires at sign-in and then every minute, so a
    supervisor that has died is back within a minute. A stop made since
    this computer last started is respected; an older one is not, so a
    restart always brings the show programs back."""
    try:
        m = os.path.getmtime(stop_file())
    except OSError:
        m = 0.0
    if m:
        if m >= boot_time():
            return 0
        try:
            os.remove(stop_file())
        except OSError:
            pass
    if supervisor_running():
        return 0
    return run_loop(open_page=load_settings()["open_page_at_sign_in"])


def _xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def supervisor_exe():
    """LTC Player.exe itself: this module also runs inside the soak test's
    own exe, so sys.executable is not always it."""
    return os.path.join(ltcwin.app_dir(), "LTC Player.exe")


def task_xml(user):
    exe = supervisor_exe() if ltcwin.frozen() else os.path.abspath(__file__)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts LTC Player (the show engine, flamesafe and the Stream Deck program) when {_xml(user)} signs in, and starts it again within a minute if it stops. Installed by LTC Player Setup.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <UserId>{_xml(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>"{_xml(exe)}"</Command>
      <Arguments>--task</Arguments>
      <WorkingDirectory>{_xml(os.path.dirname(exe))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def cmd_install_task():
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    if dom:
        user = f"{dom}\\{user}"
    path = os.path.join(appdata_dir(), "autostart-task.xml")
    with open(path, "w", encoding="utf-16") as fh:
        fh.write(task_xml(user))
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML",
                        path, "/F"], capture_output=True, text=True)
    log(f"schtasks /Create for {user}: exit {r.returncode} "
        f"{(r.stdout + r.stderr).strip()}")
    if r.returncode != 0:
        tell("The sign-in task could not be set up: "
             + (r.stdout + r.stderr).strip(), error=True)
    return r.returncode


def cmd_remove_task():
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True)
    log(f"schtasks /Delete: exit {r.returncode} "
        f"{(r.stdout + r.stderr).strip()}")
    return 0


def installers_dir():
    return os.path.join(ltcwin.app_dir(), "Installers")


def _installer_key(path):
    """Sorts saved installers by the version in their name, then by date."""
    name = os.path.basename(path)[:-4]
    ver = name.rsplit(" ", 1)[-1]
    parts = ver.replace("gpl-", "").split(".")
    try:
        nums = tuple(int(x) for x in parts)
    except ValueError:
        nums = ()
    return (nums, os.path.getmtime(path))


def saved_installers():
    d = installers_dir()
    try:
        names = [os.path.join(d, n) for n in os.listdir(d)
                 if n.lower().endswith(".exe")]
    except OSError:
        return []
    return sorted(names, key=_installer_key)


def cmd_rollback():
    current = ltcwin.release().get("release") or ""
    older = [p for p in saved_installers()
             if not (current and os.path.basename(p)[:-4].endswith(
                 " " + current))]
    if current:
        cur_key = _installer_key_for_version(current)
        if cur_key:
            older = [p for p in older if _installer_key(p)[0] < cur_key] \
                or older
    if not older:
        tell("No earlier version of LTC Player is kept on this PC yet. "
             "Download the version you want from the GitHub Releases page "
             "and run it.", error=True)
        return 1
    target = older[-1]
    if not ask(f"Go back from {current or 'this version'} to "
               f"{os.path.basename(target)[:-4].replace('LTC Player Setup ', '')}?"
               "\n\nThe installer stops LTC Player first, and refuses if a "
               "show is running. Your settings and show files are not "
               "touched."):
        return 0
    os.startfile(target)
    return 0


def _installer_key_for_version(ver):
    try:
        return tuple(int(x) for x in ver.replace("gpl-", "").split("."))
    except ValueError:
        return ()


def cmd_diagnostics():
    """The diagnostics bundle on the Desktop, then shown in Explorer."""
    settings = load_settings()
    out_dir = diagnostics.desktop_dir()
    programs = [os.path.join(ltcwin.app_dir(), EXE[n]) for n in PROGRAMS]
    programs.append(supervisor_exe())
    try:
        path, noted = diagnostics.make_bundle(
            out_dir, ltcwin.app_dir(), appdata_dir(), log_dir(),
            control_dir(), settings.get("show_folder") or "", programs,
            task_name=TASK_NAME)
    except OSError as e:
        tell(f"The diagnostics bundle could not be written to {out_dir}: "
             f"{e}", error=True)
        return 1
    log(f"diagnostics bundle written: {path} ({'; '.join(noted)})")
    if not QUIET and ltcwin.WINDOWS:
        try:
            subprocess.Popen(["explorer", "/select,", path], close_fds=True)
        except OSError:
            pass
    tell(f"Saved {os.path.basename(path)} on the Desktop. Send that file "
         f"to Jeff. It holds the logs, settings, journals and versions, and "
         f"no show files, media or keys.")
    return 0


def cmd_update(check_only=False):
    """Check GitHub Releases; with a newer release, ask, download, check
    the SHA-256 and run the installer (which stops LTC Player the safe way
    and refuses during a show). Exit codes as updater says."""
    installed = ltcwin.release().get("release") or ""
    code, text, rel = updater.check(installed)
    log(f"update check: {text} (code {code})")
    if check_only or code != updater.NEWER_AVAILABLE:
        tell(text, error=code in (updater.NO_NETWORK, updater.NO_RELEASES))
        return code
    busy, why = show_running(load_settings()["port"])
    if busy:
        tell(f"{text} {why}", error=True)
        return 3
    if not ask(f"{text}\n\nDownload and install it now? The installer "
               f"stops LTC Player the safe way first (it refuses while a "
               f"show is running), installs, and starts it again. Your "
               f"settings and show files are not touched. To go back later: "
               f"Start menu, Roll back to the previous version."):
        return code
    downloads = os.path.join(appdata_dir(), "downloads")
    try:
        path = updater.fetch_installer(rel, downloads, log=log)
    except OSError as e:
        tell(str(e), error=True)
        return 1
    log(f"starting the installer {path}")
    os.startfile(path)
    return 0


def cmd_prune_installers(keep=2):
    """The saved installers: this version's and the one before it (what
    "Roll back to the previous version" runs). Older ones were clutter on
    the show PC (2026-10-18: three setup exes in Installers)."""
    for p in saved_installers()[:-keep]:
        try:
            os.remove(p)
            log(f"removed an old saved installer: {p}")
        except OSError:
            pass
    return 0


def self_check():
    yield f"app folder: {ltcwin.app_dir()}"
    for n in PROGRAMS:
        p = os.path.join(ltcwin.app_dir(), EXE[n])
        if ltcwin.frozen() and not os.path.isfile(p):
            raise RuntimeError(f"{EXE[n]} is missing from {ltcwin.app_dir()}")
    yield "all three programs are beside it"
    yield f"settings: {settings_path()}"
    # Any file that exists stands in for flamesafe.json (frozen, __file__
    # is not on disk).
    probe = {"port": DEFAULT_PORT, "flamesafe_config": sys.executable,
             "run_flamesafe": True, "run_deck": True,
             "show_folder": control_dir(),
             "show_mode": "fire_ice", "schedule": r"C:\x\ltcplay_schedule.json"}
    want = wanted_args(probe)
    if "--schedule" not in (want["engine"][0] or []) or \
            "--announce" not in (want["engine"][0] or []) or \
            f"http://127.0.0.1:{DEFAULT_PORT}" not in (want["deck"][0] or []):
        raise RuntimeError(f"fire_ice mode does not run the scheduler and the "
                           f"deck on the engine's address: {want}")
    probe["show_mode"] = "plain"
    want = wanted_args(probe)
    if want["deck"][0] is not None or "--schedule" in want["engine"][0] \
            or "--announce" in want["engine"][0]:
        raise RuntimeError(f"plain mode would start the deck or the "
                           f"scheduler: {want}")
    yield "fire_ice mode runs the scheduler and the deck; plain runs neither"
    yield f"control folder: {control_dir()}"
    task_xml("EXAMPLE\\user")
    yield "the sign-in task can be written"
    if not (priority_boost_on({"priority_boost": True}) and
            priority_boost_on({}) and
            not priority_boost_on({"priority_boost": False})):
        raise RuntimeError("the priority_boost setting is read wrongly")
    got = []
    import threading
    stop = threading.Event()
    th = threading.Thread(target=stop.wait, args=(5,),
                          name="ltcplay-flame-link")
    th.start()
    ltcwin.boost_threads(("ltcplay-flame-link",), log=got.append,
                         every_s=0.02, setter=lambda n, lv: lv == 2,
                         stop=stop)
    deadline = time.monotonic() + 2
    while not got and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    th.join(1)
    if not got or "Highest" not in got[0]:
        raise RuntimeError(f"the show thread is not raised: {got}")
    yield ("scheduling protection: on unless showpc.json says "
           "\"priority_boost\": false; a show thread is raised to Highest")
    if contain_on({}) or contain_on({"contain_third_party": "yes"}) or \
            not contain_on({"contain_third_party": True}):
        raise RuntimeError("the contain_third_party setting is read wrongly")
    import contain
    for line in contain.self_test():
        yield line
    yield ("third-party containment: off unless showpc.json says "
           "\"contain_third_party\": true")
    cmd = rack_page_command(7878, 2, [(0, 0, 1920, 1080),
                                      (1920, 0, 3840, 1080)],
                            r"C:\Edge\msedge.exe", r"C:\p")
    if not (cmd and cmd[1] == "--app=http://127.0.0.1:7878/remote"
            and "--start-maximized" in cmd
            and "--window-size=1920,1080" in cmd
            and cmd[-1] == "--window-position=1920,0"
            and not any(a.startswith("--kiosk") or "kiosk" in a
                        or a == "--start-fullscreen" for a in cmd)):
        raise RuntimeError(f"the rack screen command is wrong (it must be "
                           f"an ordinary app window with Minimize, Maximize "
                           f"and Close, never a kiosk): {cmd}")
    if rack_page_command(7878, 9, [(0, 0, 1, 1)], "e", "p")[-1] != \
            "--window-position=0,0" or \
            rack_page_command(7878, 1, [], None, "p") is not None or \
            rack_page_command(0, 1, [], "e", "p", url="file:///C:/r.html")[1] \
            != "--app=file:///C:/r.html":
        raise RuntimeError("the rack screen's fallbacks are wrong")
    yield ("the page opens as the rack screen (/remote), an ordinary app "
           "window maximized on the show monitor, never a kiosk")
    for line in showwindows.self_test():
        yield line
    for line in housekeeping.self_test():
        yield line
    for line in diagnostics.self_test():
        yield line
    for line in updater.self_test():
        yield line
    if not os.path.isdir(diagnostics.desktop_dir()):
        raise RuntimeError("no Desktop or home folder for the diagnostics "
                           "bundle")
    yield "the diagnostics bundle goes on the Desktop; --update checks " \
          "GitHub Releases and runs only an installer whose SHA-256 matches"


def main(argv=None):
    global QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--quiet" in argv:
        QUIET = True
        argv.remove("--quiet")
    rc = ltcwin.common_flags(ltcwin.APP, argv, self_check, emit=log)
    if rc is not None:
        return rc
    pkg = ltcwin.package_name()
    if pkg:
        tell(ltcwin.CONTAINER_REFUSAL.format(pkg=pkg), error=True)
        return 4
    check_only = "--check-only" in argv
    if check_only:
        argv.remove("--check-only")
    verb = argv[0] if argv else "--start"
    verbs = {"--start": cmd_start, "--stop": cmd_stop, "--task": cmd_task,
             "--page": cmd_page,
             "--run": run_loop, "--install-task": cmd_install_task,
             "--remove-task": cmd_remove_task, "--rollback": cmd_rollback,
             "--prune-installers": cmd_prune_installers,
             "--diagnostics": cmd_diagnostics,
             "--update": lambda: cmd_update(check_only),
             "--open-settings": lambda: os.startfile(appdata_dir()) or 0}
    fn = verbs.get(verb)
    if fn is None:
        tell(f"Unknown option {verb}. Known: {', '.join(sorted(verbs))}",
             error=True)
        return 2
    try:
        return fn() or 0
    except Exception as e:
        tell(f"LTC Player hit a problem: {type(e).__name__}: {e}", error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
