"""What Reset says about the rig (Jeff's field report, 2026-10-28: "when we
Reset, everything must be functioning"). After an Abort, Reset answered
only "the rig stays dark until a show starts", and nothing said that
MadMapper would not follow the next show.

summary() turns what the engine already knows (the rack screen's lamps,
remote.Remote.subsystems; the show clock's snapshot; the MadMapper and
BEYOND watches; the flame link's status; the announcements' view) into
one plain sentence per subsystem, in a fixed order: audio, pixels, flame
link, timecode out, MadMapper, BEYOND, lasers, announcements. Anything
unhealthy says so in capitals (NOT READY, NOT RUNNING) with what to do.

Pure: no sockets, no threads, no clock. It reads, it never sends, and it
never changes the rig. Never raises.
"""


def _lamp(lamps, name):
    for x in lamps or ():
        if isinstance(x, dict) and x.get("name") == name:
            return x.get("state"), str(x.get("word") or "")
    return None, ""


def _cap(text):
    text = str(text or "").strip().rstrip(".")
    return text[:1].upper() + text[1:] if text else ""


def _audio(lamps):
    st, word = _lamp(lamps, "Audio")
    if st == "ok":
        return True, "Audio ready."
    if st == "bad":
        return False, (f"Audio NOT READY ({word}): check the show audio "
                       f"interface, then press Stop and Run.")
    if st == "off":
        return None, "Audio: no show audio is running."
    return None, f"Audio: {word or 'not known'}."


def _pixels(lamps):
    st, word = _lamp(lamps, "Pixels")
    if st == "ok":
        return True, "Pixels ready."
    if st == "off":
        return False, "Pixels NOT READY: Run is not pressed. Press Run."
    if st == "bad":
        return False, ("Pixels NOT READY: nothing is leaving this PC. Check "
                       "the lighting network.")
    return None, f"Pixels: {word or 'not known yet'}."


def _flames(fl):
    if not isinstance(fl, dict) or not fl.get("connected"):
        return False, ("Flame link NOT READY: flamesafe is not connected. "
                       "Start flamesafe.")
    if fl.get("stale"):
        return False, ("Flame link NOT READY: no status from flamesafe. "
                       "Check that flamesafe is running.")
    groups = [g for g in fl.get("groups") or [] if isinstance(g, dict)]
    armed = [str(g.get("name") or "?") for g in groups
             if str(g.get("armed")).lower() == "armed"]
    if fl.get("fault"):
        return False, (f"Flame link: flamesafe reports a fault "
                       f"({fl['fault']}). See the Flames card. " +
                       (f"{len(armed)} group{'s' if len(armed) != 1 else ''}"
                        f" armed: {', '.join(armed)}." if armed else
                        "All groups disarmed."))
    if armed:
        return True, (f"Flame link OK, {len(armed)} group"
                      f"{'s' if len(armed) != 1 else ''} armed: "
                      f"{', '.join(armed)}.")
    return True, "Flame link OK, all groups disarmed."


def _timecode(clk):
    if not isinstance(clk, dict):
        return None, "Timecode: no show clock is running."
    failing = [str(x) for x in clk.get("failing") or []]
    if failing:
        return False, (f"Timecode NOT READY: {', '.join(failing)} "
                       f"{'is' if len(failing) == 1 else 'are'} not taking "
                       f"Art-Net timecode. Check the show network.")
    if clk.get("artnet") is None:
        return None, "Timecode: no Art-Net timecode receivers are set."
    held_to = clk.get("held_to")     # TimecodeOut.held_dests labels, or None
    if clk.get("holding") and held_to:
        return True, (f"Timecode ready, holding {clk['holding']} for "
                      f"MadMapper until the next show.")
    if clk.get("holding") and held_to is not None:
        return False, ("Timecode NOT HELD for MadMapper: no Art-Net "
                       "timecode receiver is named MadMapper at an address "
                       "of its own (away from BEYOND's, not broadcast). "
                       "Name MadMapper under 'clock.artnet.nodes' at its "
                       "own address.")
    return True, "Timecode ready."


def _madmapper(mm):
    if not isinstance(mm, dict):
        return None, ""
    link = mm.get("link")
    if link == "lost":
        return False, ("MadMapper NOT RUNNING: start MadMapper and its "
                       "project.")
    head = "MadMapper running" if link == "up" else "MadMapper"
    hb = mm.get("heartbeat")
    if hb == "silent":
        # Asked with no show playing, silent means the heartbeat listener
        # itself is broken (madmapperwatch._heartbeat_word: bind_error).
        return False, (f"{head}: heartbeat listener NOT WORKING "
                       f"({_cap(mm.get('heartbeat_why')) or 'no reason'}). "
                       f"Whether the video follows cannot be told until it "
                       f"is fixed.")
    if hb is None:
        return None, (f"{head}: whether it follows cannot be told (no "
                      f"heartbeat is set up).")
    if hb == "ok":
        return True, f"{head}, heartbeat OK."
    return None, (f"{head}: heartbeat not seen yet (it must follow once a "
                  f"show starts; Start now sets its show bank following "
                  f"timecode again).")


def _beyond(by):
    if not isinstance(by, dict):
        return None, ""
    link = by.get("link")
    if link == "up":
        return True, "BEYOND running."
    if link == "lost":
        return False, "BEYOND NOT RUNNING: start BEYOND and its show."
    return None, f"BEYOND: {_cap(by.get('why')) or 'not known'}."


def _lasers(lamps):
    st, word = _lamp(lamps, "Lasers")
    ast, aword = _lamp(lamps, "Laser arm")
    if st is None and ast is None:
        return None, ""
    if st == "bad":
        return False, f"Lasers NOT READY: {word}."
    if ast == "bad":
        return False, f"Laser arm NOT READY: {aword}."
    parts = []
    if st is not None:
        parts.append(f"lasers {word}")
    if ast is not None:
        parts.append(f"laser arm {aword}")
    # Ready only with a known laser state; "none" or "not known" is not.
    ok = True if st == "ok" and ast in (None, "ok", "off") else None
    return ok, _cap(", ".join(parts)) + "."


def _announcements(ann):
    if not isinstance(ann, dict) or not ann.get("configured"):
        return None, ""
    if ann.get("stale") or not ann.get("ok"):
        return False, (f"Announcements NOT READY: "
                       f"{_cap(ann.get('error')) or 'not known'}.")
    items = [i for i in ann.get("items") or [] if isinstance(i, dict)]
    gone = [str(i.get("name") or i.get("id") or "?") for i in items
            if not i.get("available")]
    if gone:
        return False, (f"Announcements NOT READY: {', '.join(gone)} cannot "
                       f"play. See the Announcements card.")
    if ann.get("device_set") and not ann.get("device_available"):
        return False, (f"Announcements NOT READY: the output "
                       f"{ann.get('device') or ''} is not available.")
    return True, "Announcements ready."


def summary(lamps=(), clock=None, madmapper=None, beyond=None, flames=None,
            announce=None):
    """(sentence, items): `items` is [{"name", "ok", "text"}] in the fixed
    order, ok True (healthy), False (needs attention) or None (not known,
    or nothing to judge); a subsystem this engine does not have
    (MadMapper, BEYOND, announcements not configured) is left out. The
    sentence is every item's text, then what needs attention."""
    items = []
    for name, fn, arg in (("audio", _audio, lamps),
                          ("pixels", _pixels, lamps),
                          ("flame link", _flames, flames),
                          ("timecode", _timecode, clock),
                          ("MadMapper", _madmapper, madmapper),
                          ("BEYOND", _beyond, beyond),
                          ("lasers", _lasers, lamps),
                          ("announcements", _announcements, announce)):
        try:
            ok, text = fn(arg)
        except Exception as e:                   # noqa: BLE001
            ok, text = None, (f"{_cap(name)}: could not be read "
                              f"({type(e).__name__}).")
        if text:
            items.append({"name": name, "ok": ok, "text": text})
    bad = [i["name"] for i in items if i["ok"] is False]
    text = " ".join(i["text"] for i in items)
    if bad:
        text += f" Needs attention: {', '.join(bad)}."
    return text, items
