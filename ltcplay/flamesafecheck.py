"""flamesafe.json, looked at from outside flamesafe (Jeff, 2026-11-06).

A syntax error in flamesafe.json made flamesafe and the Stream Deck program
exit at once, the supervisor started them again in a loop, the engine
refused to start, and LTC Player said only that the show page did not
answer. Now:

  - json_problem(path) says whether the file is valid JSON (an object), and
    if not, where: line and column, from the JSON error itself. It never
    checks flamesafe's own rules (flamesafe does, and refuses to run on a
    file that breaks them) and never guesses a config.
  - The supervisor does not start flamesafe or the Stream Deck program on
    such a file, and says why in supervisor.log and flamesafe.log.
  - The engine starts without its flame link and without flame groups
    (nothing can be armed from it; flamesafe is not running, so nothing
    reaches the flame controller), and the rack screen shows sentence() in
    red.
  - A last good copy is kept beside the file (last_good_path), written by
    the supervisor only after flamesafe has run on the file for a while
    (LAST_GOOD_AFTER_S): a file flamesafe itself refused never becomes the
    last good copy. Restoring it is a copy by hand; nothing restores it by
    itself.

Standard library only: the supervisor carries this file as a module of its
own (packaging/windows/ltcplay.spec), as it does showwindows.py.
"""
import json
import os
import time

LAST_GOOD_TAG = ".last-good"
# flamesafe exits within a second or two on a config it refuses; one that
# has run this long was loaded cleanly.
LAST_GOOD_AFTER_S = 20.0


def last_good_path(path):
    """flamesafe.json -> flamesafe.last-good.json, in the same folder."""
    root, ext = os.path.splitext(os.path.abspath(str(path)))
    return root + LAST_GOOD_TAG + (ext or ".json")


def json_problem(path):
    """None when `path` is valid JSON holding an object, or cannot be read
    at all (a missing file is said where it is used). Otherwise
    {"path", "line", "column", "what"}, the position from the JSON error.
    Never raises."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return bytes_problem(raw, path)


def bytes_problem(raw, path=""):
    """json_problem for a file's bytes already read. Never raises."""
    full = os.path.abspath(str(path)) if path else ""
    if raw.startswith(b"\xef\xbb\xbf"):
        # flamesafe reads the file as strict UTF-8 and refuses a BOM
        # (flamesafe/config.load), as an older Notepad saves it.
        return {"path": full, "line": 1, "column": 1,
                "what": "it was saved with a byte-order mark (BOM); save it "
                        "as UTF-8 without BOM"}
    try:
        # Exactly as flamesafe reads it: strict UTF-8, no BOM.
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        line = raw[:e.start].count(b"\n") + 1
        col = e.start - (raw.rfind(b"\n", 0, e.start) + 1) + 1
        return {"path": full, "line": line, "column": col,
                "what": "a character that is not UTF-8"}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        return {"path": full, "line": e.lineno, "column": e.colno,
                "what": e.msg}
    except (ValueError, RecursionError) as e:
        return {"path": full, "line": 1, "column": 1,
                "what": str(e) or type(e).__name__}
    if not isinstance(doc, dict):
        return {"path": full, "line": 1, "column": 1,
                "what": "the file is not one JSON object { ... }"}
    return None


def sentence(problem):
    """The rack screen's and the logs' words for a problem from
    json_problem: the file, the line and column, that flames are off, and
    the last good copy to copy back, when there is one."""
    path = problem["path"]
    name = os.path.basename(path) or "flamesafe.json"
    good = last_good_path(path)
    if os.path.isfile(good):
        try:
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.localtime(os.path.getmtime(good)))
        except OSError:
            when = "at a time that cannot be read"
        back = (f"Fix it, or copy the last good copy back over it: {good}, "
                f"saved {when}. It may be older than recent edits (a "
                f"channel moved since, for example): check it before you "
                f"use it. ")
    else:
        back = "Fix it (there is no last good copy yet). "
    return (f"{name} is not valid JSON at line {problem['line']}, column "
            f"{problem['column']} ({problem['what']}): {path}. Flames are "
            f"off: flamesafe is not running and no flame group can be "
            f"armed. " + back + "Then start LTC Player again. flamesafe.log "
            "says the same.")


def save_last_good(path, raw):
    """`raw`, the bytes flamesafe was started on and has run on cleanly,
    as the last good copy beside `path`, written to a temporary file and
    moved into place. Returns the copy's path when it was written, None
    when it already held these bytes or `raw` is not valid JSON. Raises
    OSError when it cannot be written."""
    if bytes_problem(raw, path) is not None:
        return None
    good = last_good_path(path)
    try:
        with open(good, "rb") as fh:
            if fh.read() == raw:
                return None
    except OSError:
        pass
    tmp = good + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, good)
    return good
