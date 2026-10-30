"""The diagnostics bundle: one zip Jeff can send back when something is
wrong, made by "LTC Player.exe --diagnostics" (Start menu, "Diagnostics
bundle (send this to Jeff)").

What goes in: the programs' logs (live and rotated), the supervisor's log,
the settings (showpc.json, flamesafe.json, the schedule, the Fire & Ice
settings, the remote page's settings), tonight's files and the Abort latch,
the night journals of the last 14 nights, what each program says with
--version, the VERSION stamp, the sign-in task, the stop files, the newest
Setup logs, and a few lines about this PC (Windows version, disks, time
zone).

What never goes in: media (audio, video, renders, pictures), the show
folder's files, lock files, and any file whose name says pin, token, secret,
key or password. The filter is a plain function, checked by the supervisor's
self-check. Files over MAX_FILE bytes go in as their last TAIL bytes.
"""
import glob
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile

MEDIA = (".wav", ".aif", ".aiff", ".mp3", ".flac", ".ogg", ".m4a", ".mp4",
         ".mov", ".avi", ".mkv", ".fseq", ".xsq", ".png", ".jpg", ".jpeg",
         ".gif", ".bmp", ".psd")
SECRET_WORDS = ("pin", "token", "secret", "password", "passwd", "key")
SKIP_EXT = MEDIA + (".lock", ".exe", ".dll", ".pyd", ".zip")
MAX_FILE = 50 * 1024 * 1024
TAIL = 5 * 1024 * 1024
JOURNAL_NIGHTS = 14


# Never in the bundle whatever they are called: the users' password hashes
# and the audit upload's token (users.py, auditsend.py).
NEVER_FILES = ("ltcplay_users.json", "ltcplay_audit.json")


def wanted(name):
    """True when a file by this name may go in the bundle."""
    low = os.path.basename(name).lower()
    if low in NEVER_FILES:
        return False
    stem, ext = os.path.splitext(low)
    # A rotated log is "engine.log.2": its real extension is .log.
    if ext.lstrip(".").isdigit():
        stem, ext = os.path.splitext(stem)
    if ext in SKIP_EXT:
        return False
    words = stem.replace("-", "_").replace(" ", "_").split("_")
    if any(w in SECRET_WORDS for w in words) or "pins" in words:
        return False
    return True


def _add_file(z, path, arcname):
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE:
            with open(path, "rb") as fh:
                fh.seek(size - TAIL)
                data = fh.read()
            z.writestr(arcname + f".last-{TAIL // (1024 * 1024)}MB",
                       b"(the first part of this file was left out: it was "
                       + str(size).encode() + b" bytes)\n" + data)
        else:
            z.write(path, arcname)
        return True
    except OSError:
        return False


def _add_folder(z, folder, arc, pick=None, recurse=False):
    """Every wanted file in `folder` (a flat folder unless `recurse`)."""
    added = 0
    for d, dirs, names in os.walk(folder):
        if not recurse:
            dirs[:] = []
        rel = os.path.relpath(d, folder)
        for n in sorted(names):
            p = os.path.join(d, n)
            if not wanted(p):
                continue
            if pick is not None and not pick(n):
                continue
            arcname = arc + "/" + (n if rel == "." else rel.replace(
                os.sep, "/") + "/" + n)
            if _add_file(z, p, arcname):
                added += 1
    return added


def _run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
        return f"$ {subprocess.list2cmdline(cmd)}\nexit {r.returncode}\n" \
               f"{r.stdout}{r.stderr}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"$ {subprocess.list2cmdline(cmd)}\n{type(e).__name__}: {e}\n"


def about_this_pc(app_dir, settings_dir, show_folder):
    """A few lines about the machine, for the top of the bundle."""
    lines = [f"made {time.strftime('%Y-%m-%d %H:%M:%S')} "
             f"(UTC{time.strftime('%z')})",
             f"computer {platform.node()}, user "
             f"{os.environ.get('USERNAME') or os.environ.get('USER', '')}",
             f"{platform.platform()}",
             f"python {sys.version.split()[0]}, frozen "
             f"{bool(getattr(sys, 'frozen', False))}",
             f"app folder {app_dir}", f"settings folder {settings_dir}",
             f"show folder {show_folder}"]
    for label, path in (("settings disk", settings_dir),
                        ("show disk", show_folder)):
        try:
            u = shutil.disk_usage(path)
            lines.append(f"{label}: {u.free / 1024 ** 3:.1f} GB free of "
                         f"{u.total / 1024 ** 3:.1f} GB")
        except OSError as e:
            lines.append(f"{label}: unreadable ({e})")
    try:
        lines.append(f"time zone {time.tzname}, uptime "
                     f"{_uptime_s() / 3600:.1f} h")
    except Exception:
        pass
    return "\n".join(lines) + "\n"


def _uptime_s():
    if sys.platform != "win32":
        return 0.0
    import ctypes
    k = ctypes.windll.kernel32
    k.GetTickCount64.restype = ctypes.c_ulonglong
    return k.GetTickCount64() / 1000.0


def make_bundle(out_dir, app_dir, settings_dir, log_dir, control_dir,
                show_folder, programs=(), task_name="LTC Player",
                now=None):
    """Write the zip into `out_dir` and return (path, [what went in]).
    `programs` are the .exe paths whose --version is asked. Raises OSError
    only when the zip itself cannot be written."""
    now = now or time.localtime()
    name = time.strftime("LTC Player diagnostics %Y-%m-%d %H%M.zip", now)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    noted = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ABOUT THIS PC.txt",
                   about_this_pc(app_dir, settings_dir, show_folder))
        noted.append("about this PC")
        n = _add_folder(z, log_dir, "logs")
        noted.append(f"{n} log files")
        settings_pick = lambda f: (f.lower().endswith((".json", ".txt",
                                                       ".html", ".xml"))
                                   and not f.startswith("night_"))
        n = _add_folder(z, settings_dir, "settings", pick=settings_pick)
        noted.append(f"{n} settings and state files")
        nights = sorted(glob.glob(os.path.join(settings_dir, "night_*")))
        recent = [p for p in nights if wanted(p)][-JOURNAL_NIGHTS * 2:]
        for p in recent:
            _add_file(z, p, "journals/" + os.path.basename(p))
        noted.append(f"{len(recent)} journal files (last {JOURNAL_NIGHTS} "
                     f"nights)")
        n = _add_folder(z, control_dir, "control")
        noted.append(f"{n} control files (stop requests)")
        internal = os.path.join(app_dir, "_internal")
        for stamp in (os.path.join(internal, "VERSION"),
                      os.path.join(app_dir, "VERSION")):
            if os.path.isfile(stamp):
                _add_file(z, stamp, "app/VERSION")
                noted.append("the VERSION stamp")
                break
        try:
            z.writestr("app/program folder.txt", "\n".join(
                sorted(os.listdir(app_dir))) + "\n")
            inst = os.path.join(app_dir, "Installers")
            if os.path.isdir(inst):
                z.writestr("app/saved installers.txt", "\n".join(
                    sorted(os.listdir(inst))) + "\n")
        except OSError:
            pass
        versions = []
        for exe in programs:
            if os.path.isfile(exe):
                versions.append(_run([exe, "--version"], timeout=30))
        if versions:
            z.writestr("app/versions.txt", "\n".join(versions))
            noted.append(f"{len(versions)} programs' --version")
        if sys.platform == "win32":
            z.writestr("windows/sign-in task.txt",
                       _run(["schtasks", "/Query", "/TN", task_name, "/V",
                             "/FO", "LIST"]))
            z.writestr("windows/our processes.txt",
                       _run(["tasklist", "/FI", "IMAGENAME eq ltcplay.exe"])
                       + _run(["tasklist", "/FI", "IMAGENAME eq flamesafe.exe"])
                       + _run(["tasklist", "/FI",
                               "IMAGENAME eq ltcplay-deck.exe"])
                       + _run(["tasklist", "/FI",
                               "IMAGENAME eq LTC Player.exe"]))
            z.writestr("windows/network.txt", _run(["ipconfig", "/all"]))
            noted.append("the sign-in task, our processes, ipconfig")
            tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
            logs = sorted(glob.glob(os.path.join(tmp, "Setup Log*.txt")),
                          key=lambda p: os.path.getmtime(p))[-3:]
            for p in logs:
                _add_file(z, p, "setup/" + os.path.basename(p))
            if logs:
                noted.append(f"{len(logs)} Setup logs")
        z.writestr("CONTENTS.txt", "\n".join(noted) + "\n\nNot included, "
                   "on purpose: show files and media, lock files, and any "
                   "file whose name says pin, token, secret, key or "
                   "password.\n")
    return path, noted


def desktop_dir():
    """The signed-in account's Desktop, or the home folder."""
    home = os.path.expanduser("~")
    for d in (os.path.join(os.environ.get("USERPROFILE", home), "Desktop"),
              os.path.join(home, "Desktop")):
        if os.path.isdir(d):
            return d
    return home


def self_test():
    """Plain checks on the filter and a bundle made from a scratch folder."""
    import tempfile
    for bad in ("show.wav", "render.fseq", "ltcplay_output.lock",
                "ltcplay_remote_pins.json", "api_token.txt", "my key.json",
                "logo.png", "LTC Player Setup x.exe", "secret-note.txt",
                "ltcplay_users.json", "ltcplay_audit.json"):
        if wanted(bad):
            raise RuntimeError(f"{bad} would go in the bundle")
    for good in ("engine.log", "engine.log.2", "supervisor.log",
                 "showpc.json", "flamesafe.json", "ltcplay_schedule.json",
                 "night_2026-11-14.jsonl", "tonight_2026-11-14.json",
                 "stop-refused", "ltcplay_abort_latch.json", "keyboard.txt"):
        if not wanted(good):
            raise RuntimeError(f"{good} would be left out of the bundle")
    yield "the filter keeps media, locks and anything named pin, token, " \
          "secret, key or password out"
    work = tempfile.mkdtemp(prefix="ltcplay-diag-")
    try:
        settings = os.path.join(work, "settings")
        logs = os.path.join(settings, "logs")
        os.makedirs(logs)
        for n, body in (("showpc.json", "{}"), ("flamesafe.json", "{}"),
                        ("ltcplay_remote_pins.json", "SECRET"),
                        ("night_2026-11-14.jsonl", "{}\n"),
                        ("show.wav", "RIFF")):
            with open(os.path.join(settings, n), "w") as fh:
                fh.write(body)
        with open(os.path.join(logs, "engine.log"), "w") as fh:
            fh.write("hello\n")
        with open(os.path.join(logs, "engine.log.1"), "w") as fh:
            fh.write("older\n")
        out, noted = make_bundle(os.path.join(work, "out"), work, settings,
                                 logs, os.path.join(work, "control"),
                                 os.path.join(work, "shows"))
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            about = z.read("ABOUT THIS PC.txt").decode()
        want = {"ABOUT THIS PC.txt", "CONTENTS.txt", "logs/engine.log",
                "logs/engine.log.1", "settings/showpc.json",
                "settings/flamesafe.json",
                "journals/night_2026-11-14.jsonl"}
        if not want <= names:
            raise RuntimeError(f"the bundle is missing {want - names}")
        if any("pins" in n or n.endswith(".wav") for n in names):
            raise RuntimeError(f"the bundle carries a secret or media: "
                               f"{sorted(names)}")
        if "settings folder" not in about:
            raise RuntimeError("the about page is empty")
        yield "a bundle holds the logs, settings and journals, and no " \
              "PIN file or media"
    finally:
        shutil.rmtree(work, ignore_errors=True)
