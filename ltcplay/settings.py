"""Which input this Mac uses, remembered between runs.

Kept in a plain file next to the launcher rather than inside a show file,
because the two answer different questions. A show file describes the show and
travels with it; this describes the machine you are standing at and what is
plugged into it. Copying a show folder between two rigs should not carry one
rig's audio interface with it.
"""
import json
import os

from . import appdata

FILENAME = "ltcplay_input.json"
FIELDS = ("device", "channel", "rate")
# Kept in the same file for the same reason: it describes how this machine is
# being used, not what the show is.
PREFS_FILE = "ltcplay_prefs.json"
PREFS = {"auto_reload": False}


def folder():
    """The folder holding the launcher, which is one above this package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _machine_folder():
    """Where this machine's settings live: beside the launcher on a Mac, as
    they always have; %LOCALAPPDATA%\\ltcplay on Windows, where the program
    folder may not be writable and must never be a synced one."""
    return appdata.folder() if appdata.WINDOWS else folder()


def path():
    return os.path.join(_machine_folder(), FILENAME)


def load():
    p = path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    for k in FIELDS:
        if doc.get(k) not in (None, ""):
            out[k] = doc[k]
    if "channel" in out:
        try:
            out["channel"] = int(out["channel"])
        except (TypeError, ValueError):
            del out["channel"]
        else:
            if out["channel"] < 1:
                del out["channel"]
    return out


def save(device, channel=1, rate=None):
    doc = {"device": device, "channel": int(channel)}
    if rate:
        doc["rate"] = int(rate)
    doc["_note"] = ("Which audio input ltcplay uses on this Mac. Delete this "
                    "file to go back to the system default, or run "
                    "'./ltc input' to change it.")
    with open(path(), "w") as fh:
        json.dump(doc, fh, indent=2)
    return doc


def clear():
    p = path()
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def resolve(saved, timeline_input, cli):
    """Work out the input to use, and say where the answer came from.

    Device, channel and rate travel together as one set. A channel number is
    only meaningful for the device it was chosen for, so inheriting input 2
    from a show file while using a different interface from the saved setting
    would quietly point at the wrong socket. Whichever source supplies the
    device supplies the whole set; an explicit --channel or --rate then
    overrides on top.

    The saved setting beats the show file on purpose. A show file cannot know
    which Mac it is being opened on or what is plugged into it; the saved
    setting is a statement about this machine. When the two name different
    devices that is worth saying out loud rather than silently picking one, so
    the conflict comes back alongside the answer.
    """
    cli = cli or {}
    sources = (("the command line", cli),
               ("your saved input", saved or {}),
               ("the show file", timeline_input or {}))

    out, source = {}, {}
    for label, d in sources:
        if d.get("device"):
            for k in FIELDS:
                if d.get(k) not in (None, ""):
                    out[k] = d[k]
                    source[k] = label
            break

    # An explicit channel or rate on the command line applies whatever device
    # was picked, because "the same box, the other input" is a real thing to
    # ask for at a console.
    for k in ("channel", "rate"):
        if cli.get(k) not in (None, ""):
            out[k] = cli[k]
            source[k] = "the command line"

    conflict = None
    tl_dev = (timeline_input or {}).get("device")
    sv_dev = (saved or {}).get("device")
    if tl_dev and sv_dev and tl_dev.lower() != sv_dev.lower() \
            and not cli.get("device"):
        conflict = (f"The show file asks for {tl_dev!r} but this Mac is set to "
                    f"{sv_dev!r}. Using the saved setting, because the show "
                    f"file cannot know what is plugged in here. Run "
                    f"'./ltc input' to change it, or pass --device to override "
                    f"both.")
    return out, source, conflict


def prefs_path():
    return os.path.join(_machine_folder(), PREFS_FILE)


def load_prefs():
    """Operator preferences that survive a restart, with defaults filled in."""
    out = dict(PREFS)
    try:
        with open(prefs_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        return out
    if not isinstance(doc, dict):
        return out
    for k, default in PREFS.items():
        if k in doc and isinstance(doc[k], type(default)):
            out[k] = doc[k]
    return out


def save_pref(key, value):
    if key not in PREFS:
        raise KeyError(f"{key!r} is not a saved preference")
    doc = load_prefs()
    doc[key] = value
    doc["_note"] = ("How ltcplay is set up on this Mac. Delete this file to "
                    "go back to the defaults.")
    with open(prefs_path(), "w") as fh:
        json.dump(doc, fh, indent=2)
    return doc
