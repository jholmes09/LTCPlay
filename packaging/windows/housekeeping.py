"""Housekeeping for the show PC's own files: log rotation, old-file pruning
and the free-disk check. Plain functions on paths, no Windows calls, so the
supervisor's self-check and the selftest can run them on any machine.

A show PC runs for weeks with nobody looking at its disk. The programs'
logs are rotated by size, in generations (ltcplay.log, ltcplay.log.1,
ltcplay.log.2, ltcplay.log.3), so the newest few hundred megabytes are
always there for a diagnostics bundle and nothing grows without end. Soak
folders, rotated logs and night journals older than their keep are
removed. When the disk is low the supervisor says so, in its log and in a
file the engine's rack screen can read.

Nothing here ever touches a show file, a settings file or a saved
installer (the installers have their own keep in supervisor.py).
"""
import os
import shutil
import time

LOG_LIMIT = 20 * 1024 * 1024      # a program's log is rotated above this
LOG_KEEP = 3                      # generations kept beside the live log
LIVE_LOG_WARN = 200 * 1024 * 1024  # a live log above this is said in the log
SOAK_KEEP_DAYS = 30
JOURNAL_KEEP_DAYS = 120
DISK_LOW_BYTES = 5 * 1024 * 1024 * 1024    # 5 GB free: say so
DISK_CRITICAL_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB: say so loudly
DISK_FILE = "disk-low.txt"        # in the settings folder while the disk is low


def rotate(path, limit=LOG_LIMIT, keep=LOG_KEEP):
    """Rotate `path` into generations when it is over `limit` bytes:
    path.(keep) is dropped, each path.N becomes path.(N+1), path becomes
    path.1. Returns True when it rotated. A file another process holds
    open (Windows) is left alone and False is returned: the caller's next
    start tries again. Never raises."""
    try:
        if os.path.getsize(path) <= limit:
            return False
    except OSError:
        return False
    try:
        old = path + ".old"            # the one-generation scheme before this
        if os.path.exists(old):
            if os.path.exists(f"{path}.{keep}"):
                os.remove(old)
            else:
                os.replace(old, f"{path}.{keep}")
        for n in range(keep, 0, -1):
            src = f"{path}.{n}"
            if not os.path.exists(src):
                continue
            if n == keep:
                os.remove(src)
            else:
                os.replace(src, f"{path}.{n + 1}")
        os.replace(path, f"{path}.1")
        return True
    except OSError:
        return False


def oversize_logs(folder, limit=LIVE_LOG_WARN):
    """[(name, bytes)] of the live logs in `folder` over `limit`: the ones
    their program holds open, which are rotated at its next start."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".log"):
            continue
        try:
            size = os.path.getsize(os.path.join(folder, n))
        except OSError:
            continue
        if size > limit:
            out.append((n, size))
    return out


def _older_than(path, days, now=None):
    try:
        return (now or time.time()) - os.path.getmtime(path) > days * 86400
    except OSError:
        return False


def prune(settings_dir, log_dir, now=None, remove=None):
    """Remove what is past its keep. Returns the sentences for the log, one
    per removal, and never raises. `remove` is for the tests.

      - rotated logs beyond LOG_KEEP generations (logs\\*.log.N, N > keep);
      - soak folders (settings\\soak\\<run>) older than SOAK_KEEP_DAYS;
      - night journals (settings\\night_*.jsonl and their .txt twins)
        older than JOURNAL_KEEP_DAYS;
      - downloaded installers (settings\\downloads) beyond the newest two.
    Show files, settings files and tonight's files are never touched."""
    now = now or time.time()
    said = []

    def rm(path):
        try:
            if remove is not None:
                remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            said.append(f"removed {path}")
        except OSError as e:
            said.append(f"could not remove {path}: {e}")
    try:
        for n in sorted(os.listdir(log_dir)):
            stem, _, gen = n.rpartition(".")
            if stem.endswith(".log") and gen.isdigit() and int(gen) > LOG_KEEP:
                rm(os.path.join(log_dir, n))
    except OSError:
        pass
    soak = os.path.join(settings_dir, "soak")
    try:
        for n in sorted(os.listdir(soak)):
            p = os.path.join(soak, n)
            if os.path.isdir(p) and _older_than(p, SOAK_KEEP_DAYS, now):
                rm(p)
    except OSError:
        pass
    try:
        for n in sorted(os.listdir(settings_dir)):
            if n.startswith("night_") and (n.endswith(".jsonl") or
                                           n.endswith(".txt")):
                p = os.path.join(settings_dir, n)
                if _older_than(p, JOURNAL_KEEP_DAYS, now):
                    rm(p)
    except OSError:
        pass
    downloads = os.path.join(settings_dir, "downloads")
    try:
        exes = sorted((os.path.join(downloads, n) for n in os.listdir(downloads)
                       if n.lower().endswith(".exe")), key=os.path.getmtime)
        for p in exes[:-2]:
            rm(p)
    except OSError:
        pass
    return said


def disk_state(path, usage=shutil.disk_usage):
    """("ok" | "low" | "critical", free bytes, sentence) for the disk that
    holds `path`. Never raises: a disk that cannot be read is "ok" with an
    empty sentence, since a false alarm is worse than none."""
    try:
        free = usage(path).free
    except OSError:
        return "ok", None, ""
    gb = free / (1024 ** 3)
    if free < DISK_CRITICAL_BYTES:
        return ("critical", free,
                f"The disk holding {path} has only {gb:.1f} GB free. LTC "
                f"Player's logs and journals may stop being written and a "
                f"show could fail to start. Free some space now (empty the "
                f"Recycle Bin, remove old video renders).")
    if free < DISK_LOW_BYTES:
        return ("low", free,
                f"The disk holding {path} has {gb:.1f} GB free. Free some "
                f"space before the next show night.")
    return "ok", free, ""


def write_disk_file(settings_dir, state, sentence):
    """DISK_FILE in the settings folder carries the sentence while the disk
    is low or critical, and is removed once it is fine again. The engine's
    rack screen reads it. Never raises."""
    path = os.path.join(settings_dir, DISK_FILE)
    try:
        if state == "ok":
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"{state}\n{sentence}\n")
    except OSError:
        pass


def self_test():
    """Plain checks on a scratch folder; raises on the first wrong answer."""
    import tempfile
    work = tempfile.mkdtemp(prefix="ltcplay-housekeeping-")
    try:
        logs = os.path.join(work, "logs")
        os.makedirs(logs)
        live = os.path.join(logs, "engine.log")
        with open(live, "wb") as fh:
            fh.write(b"x" * 10)
        if rotate(live, limit=100):
            raise RuntimeError("a small log was rotated")
        for i in range(5):
            with open(live, "wb") as fh:
                fh.write(bytes([65 + i]) * 200)
            if not rotate(live, limit=100, keep=3):
                raise RuntimeError(f"rotation {i} did not happen")
        have = sorted(os.listdir(logs))
        if have != ["engine.log.1", "engine.log.2", "engine.log.3"]:
            raise RuntimeError(f"generations wrong: {have}")
        with open(os.path.join(logs, "engine.log.1"), "rb") as fh:
            if fh.read(1) != b"E":
                raise RuntimeError(".1 is not the newest generation")
        yield "logs rotate into three generations, newest first"
        with open(os.path.join(logs, "engine.log.7"), "wb") as fh:
            fh.write(b"old")
        soak = os.path.join(work, "soak", "run-old")
        os.makedirs(soak)
        os.utime(soak, (1, 1))
        fresh = os.path.join(work, "soak", "run-new")
        os.makedirs(fresh)
        nj = os.path.join(work, "night_2020-01-01.jsonl")
        open(nj, "w").close()
        os.utime(nj, (1, 1))
        keep = os.path.join(work, "showpc.json")
        open(keep, "w").close()
        os.utime(keep, (1, 1))
        said = prune(work, logs)
        gone = {os.path.basename(s.split("removed ", 1)[-1]) for s in said}
        if gone != {"engine.log.7", "run-old", "night_2020-01-01.jsonl"}:
            raise RuntimeError(f"prune removed the wrong things: {said}")
        if not os.path.exists(keep) or not os.path.exists(fresh):
            raise RuntimeError("prune touched a settings file or a fresh "
                               "soak")
        yield "prune removes only old soaks, old journals and spare log " \
              "generations"

        class U:
            def __init__(self, free):
                self.free = free
        st, _, text = disk_state(work, usage=lambda p: U(500 * 1024 ** 2))
        if st != "critical" or "0.5 GB" not in text:
            raise RuntimeError(f"critical disk read wrong: {st} {text}")
        st, _, text = disk_state(work, usage=lambda p: U(3 * 1024 ** 3))
        if st != "low" or "3.0 GB" not in text:
            raise RuntimeError(f"low disk read wrong: {st} {text}")
        st, _, text = disk_state(work, usage=lambda p: U(100 * 1024 ** 3))
        if st != "ok" or text:
            raise RuntimeError(f"a fine disk read wrong: {st} {text}")
        write_disk_file(work, "low", "x")
        if not os.path.exists(os.path.join(work, DISK_FILE)):
            raise RuntimeError("the disk file was not written")
        write_disk_file(work, "ok", "")
        if os.path.exists(os.path.join(work, DISK_FILE)):
            raise RuntimeError("the disk file was not removed")
        yield "disk: 5 GB free is low, 1 GB is critical, and the rack " \
              "screen's file follows it"
    finally:
        shutil.rmtree(work, ignore_errors=True)
