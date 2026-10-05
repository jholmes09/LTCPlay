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
  --stop            stop everything, unless a show is running
  --task            what the scheduled task runs at sign-in, every minute
  --run             the supervisor loop itself (started by --start)
  --install-task    register the sign-in task for this Windows account
  --remove-task     remove it
  --rollback        run the previous version's installer
  --open-settings   open the settings and logs folder
  --prune-installers  keep only the newest three saved installers
  --version, --self-check
  --quiet           no message boxes (for the installer and tests)

Files:
  %LOCALAPPDATA%\\ltcplay\\showpc.json   show folder, flamesafe config, port,
                                       show mode and the schedule file

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

import ltcwin

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
        # The rack screen page (/remote), full screen on this monitor:
        # 1 is the main display, 2 the next one Windows lists, and so on.
        "page_monitor": 1,
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


def log(msg):
    global _LOG
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        if _LOG is None:
            _LOG = open(os.path.join(log_dir(), "supervisor.log"), "a",
                        encoding="utf-8")
        _LOG.write(line + "\n")
        _LOG.flush()
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass


def rotate(path, limit=20 * 1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except OSError:
        pass


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
        env = dict(os.environ)
        env.pop(ltcwin.PRIORITY_ENV, None)
        if priority_boost_on():
            env[ltcwin.PRIORITY_ENV] = "high"
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
        log(f"started {EXE[self.name]} (pid {self.pid}): {' '.join(args)}")

    def exited(self):
        code = self.proc.returncode if self.proc is not None else "?"
        ran = time.monotonic() - self.started
        if ran > 60:
            self.backoff = 2.0
        log(f"{EXE[self.name]} stopped by itself (exit code {code}) after "
            f"{ran:.0f} s; starting it again in {self.backoff:.0f} s")
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
    engine = ["serve", "--folder", folder, "--port", str(port),
              "--no-browser"]
    if mode == "fire_ice":
        engine += ["--schedule", settings["schedule"]]
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
    for why in fire_ice_files(settings):
        log(why)
    log(ltcwin.settings_folder_line())
    log("scheduling protection " + (
        "ON: the engine and flamesafe run at High priority, their show "
        "threads at Highest, the Stream Deck program at Above normal; each "
        "program's log says what Windows agreed to" if priority_boost_on()
        else "OFF (showpc.json \"priority_boost\": false)"))
    keep_awake(True)
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
    page_opened = not open_page
    ending = _watch_end_session(progs)
    while True:
        if ENDING.is_set():
            # The clean stops go out on the end-session thread; this
            # process must not end before they have.
            if ending is not None:
                ending.done.wait(ltcwin.END_SESSION_WAIT_S + 1.0)
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
                _write_refusal(why)
            else:
                failures = [w for w in (progs[n].stop()
                                        for n in reversed(PROGRAMS)) if w]
                if failures:
                    _write_refusal(" ".join(failures))
                    log("supervisor exiting with programs it could not stop")
                else:
                    log("everything stopped cleanly; supervisor exiting")
                keep_awake(False)
                return 1 if failures else 0
        want = wanted_args(settings)
        now = time.monotonic()
        for n in PROGRAMS:
            p = progs[n]
            args, why_not = want[n]
            if p.proc is not None and p.proc.poll() is not None:
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
                except OSError as e:
                    log(f"could not start {EXE[n]}: {e}")
                    p.next_try = now + p.backoff
                    p.backoff = min(p.backoff * 2, 60.0)
        if not page_opened and engine_answers(port):
            page_opened = True
            open_page_now(port)
        time.sleep(POLL_S)


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


def monitors():
    """[(left, top, right, bottom)] of every display, the main one first
    (Windows). [] when they cannot be read."""
    if not ltcwin.WINDOWS:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        found = []

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        u32 = ctypes.WinDLL("user32")
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
                r = mi.rcMonitor
                found.append(((r.left, r.top, r.right, r.bottom),
                              bool(mi.dwFlags & 1)))
            return True
        u32.EnumDisplayMonitors(None, None, PROC(cb), 0)
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


def rack_page_command(port, monitor, screens, edge, profile):
    """The command that opens the rack screen page full screen (Edge kiosk
    mode, its own profile so it never asks first-run questions) on display
    number `monitor` (1 = main) of `screens`, or None without Edge. An
    unknown display number means the main one."""
    if not edge:
        return None
    url = f"http://127.0.0.1:{port}{RACK_PAGE}"
    cmd = [edge, "--kiosk", url, "--edge-kiosk-type=fullscreen",
           "--no-first-run", f"--user-data-dir={profile}"]
    if screens:
        i = monitor - 1 if isinstance(monitor, int) and \
            1 <= monitor <= len(screens) else 0
        left, top = screens[i][0], screens[i][1]
        cmd.append(f"--window-position={left},{top}")
    return cmd


def open_page_now(port):
    """The rack screen page (/remote), full screen on the show monitor
    (showpc.json "page_monitor"); without Edge, in the default browser."""
    url = f"http://127.0.0.1:{port}{RACK_PAGE}"
    try:
        if ltcwin.WINDOWS:
            cmd = rack_page_command(
                port, load_settings().get("page_monitor", 1), monitors(),
                edge_exe(), os.path.join(appdata_dir(), "rack-screen"))
            if cmd:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
                log(f"opened the rack screen full screen: {cmd[2]} "
                    f"({cmd[-1]})")
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


def cmd_prune_installers(keep=3):
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
            f"http://127.0.0.1:{DEFAULT_PORT}" not in (want["deck"][0] or []):
        raise RuntimeError(f"fire_ice mode does not run the scheduler and the "
                           f"deck on the engine's address: {want}")
    probe["show_mode"] = "plain"
    want = wanted_args(probe)
    if want["deck"][0] is not None or "--schedule" in want["engine"][0]:
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
    cmd = rack_page_command(7878, 2, [(0, 0, 1920, 1080),
                                      (1920, 0, 3840, 1080)],
                            r"C:\Edge\msedge.exe", r"C:\p")
    if not (cmd and cmd[1:3] == ["--kiosk", "http://127.0.0.1:7878/remote"]
            and cmd[-1] == "--window-position=1920,0"):
        raise RuntimeError(f"the rack screen command is wrong: {cmd}")
    if rack_page_command(7878, 9, [(0, 0, 1, 1)], "e", "p")[-1] != \
            "--window-position=0,0" or \
            rack_page_command(7878, 1, [], None, "p") is not None:
        raise RuntimeError("the rack screen's fallbacks are wrong")
    yield ("the page opens as the rack screen (/remote), full screen on the "
           "show monitor")


def main(argv=None):
    global QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--quiet" in argv:
        QUIET = True
        argv.remove("--quiet")
    rc = ltcwin.common_flags(ltcwin.APP, argv, self_check, emit=log)
    if rc is not None:
        return rc
    verb = argv[0] if argv else "--start"
    pkg = ltcwin.package_name()
    if pkg:
        tell(ltcwin.CONTAINER_REFUSAL.format(pkg=pkg), error=True)
        return 4
    verbs = {"--start": cmd_start, "--stop": cmd_stop, "--task": cmd_task,
             "--run": run_loop, "--install-task": cmd_install_task,
             "--remove-task": cmd_remove_task, "--rollback": cmd_rollback,
             "--prune-installers": cmd_prune_installers,
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
