"""What is actually installed here, worked out from the files themselves.

There was no answer to "which version am I running" on any machine, on a
night when four of them existed. A number someone has to remember to bump is
worse than nothing, because it is wrong exactly when it matters. So this is
derived from the bytes: hash every file that makes the program behave the way
it behaves, and print a short digest of the lot. Two machines showing the same
build id are running the same program. Different ids mean different programs,
whatever anybody remembers doing.
"""
import hashlib
import json
import os
import time

# Everything whose contents change what this program does. Not the show file,
# not the sequences, not the logs: those are the SHOW, and they are stamped
# separately so a code change and a render change cannot be confused.
#
# Found, not listed. A hardcoded list silently ignored a launcher that was
# ADDED, so a folder with a new script in it reported the same build id as
# one without it. 2026-09-15.
EXTRA = ("ltc", "selftest.py")


def _package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def folder():
    return os.path.dirname(_package_dir())


# The stamp itself is never hashed. It records what the build id WAS at the
# moment a release was cut, so including it would change the answer it
# records. Everything else about the program is fair game.
STAMP = "VERSION"


def _files():
    """Every file that decides behaviour, in a fixed order."""
    pkg = _package_dir()
    out = []
    for root, dirs, names in os.walk(pkg):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for n in sorted(names):
            if n.endswith(".pyc"):
                continue
            out.append(os.path.join(root, n))
    here = folder()
    names = sorted(n for n in os.listdir(here) if n.endswith(".command"))
    tools = os.path.join(here, "Tools")
    if os.path.isdir(tools):
        names += sorted(os.path.join("Tools", n) for n in os.listdir(tools)
                        if n.endswith(".command"))
    for n in names + list(EXTRA):
        p = os.path.join(here, n)
        if os.path.isfile(p):
            out.append(p)
    return [p for p in out if os.path.basename(p) != STAMP]


def build():
    """(id, file count, newest mtime) for the program as it sits on disk."""
    h = hashlib.sha256()
    newest = 0.0
    files = _files()
    for p in files:
        rel = os.path.relpath(p, folder())
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        try:
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            newest = max(newest, os.path.getmtime(p))
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:10], len(files), newest


def show_build(show_dir, timeline_path=None):
    """The fingerprint of the RENDERS, and nothing else.

    This is the number the recorded Advatek scenes are checked against, so it
    has to change when and only when a render changes. It used to include the
    show file, which meant editing a trigger channel made every recorded
    scene look stale when not one pixel had moved. The show file's settings
    are on the panel and in the report already; they do not belong in here.
    2026-09-15. `timeline_path` is accepted and ignored, so callers that
    passed it keep working."""
    h = hashlib.sha256()
    n = 0
    if show_dir and os.path.isdir(show_dir):
        for name in sorted(os.listdir(show_dir)):
            if not name.endswith((".fseq", ".xml")):
                continue
            p = os.path.join(show_dir, name)
            if not os.path.isfile(p):
                continue
            h.update(name.encode("utf-8", "replace"))
            h.update(b"\0")
            st = os.stat(p)
            # Size and mtime, not contents: a show folder is 600MB and this
            # runs at startup. Any re-render changes both.
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
            n += 1
    return h.hexdigest()[:10], n


def release():
    """The release this folder was cut as, if it was cut as one.

    A content hash says whether two machines match. It does not say which is
    newer, and nobody can read one out over a phone. So a release also gets a
    plain dated number that sorts, and the hash it had when it was cut."""
    p = os.path.join(folder(), STAMP)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        return None
    return doc if isinstance(doc, dict) else None


def status():
    """One line that identifies this copy, and says if it has been edited.

    The modified case is the one that matters. Two machines can both say
    'release 2026.09.15' while one of them has a file somebody changed at
    11pm, and that is precisely the night it will matter."""
    bid, count, newest = build()
    rel = release()
    if not rel:
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
                if newest else "unknown")
        return f"unreleased, build {bid} ({count} files, newest {when})"
    name = rel.get("release", "?")
    was = rel.get("build")
    if was and was != bid:
        return (f"release {name}, MODIFIED SINCE (build {bid}, "
                f"this release was cut as {was})")
    return f"release {name}, build {bid}"


def show_status(show_dir, timeline_path=None):
    """The same for the renders, against what the release was cut with."""
    sid, n = show_build(show_dir, timeline_path)
    rel = release() or {}
    was = rel.get("show")
    if was and was != sid:
        return f"show {sid}, DIFFERENT RENDERS from this release ({was})"
    return f"show {sid}"


def describe():
    bid, count, newest = build()
    when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))
            if newest else "unknown")
    return f"build {bid} ({count} files, newest {when})"
