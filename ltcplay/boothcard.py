"""The Settings tab's Booth speaker card (Jeff, 2026-11-19): the device and
the output (or two) the booth notes play on, picked from this PC's own
outputs, so nobody edits ltcplay_remote.json by hand.

What is here is the card's own logic, with nothing to save and nothing to
play: the device list (announce.list_outputs, with the same duplicate name
and audio system handling as the Announcement output card), the check of a
pick against that list, the line under the pickers, and the revision a
page sends back so a stale page never saves over a newer setting.

remote.py owns the routes (GET and POST booth-speaker, the show PC only,
the Settings row), the refusals while a show is live or a note plays, the
save (remote.save_settings: every other key kept, checked by
boothaudio.parse_config before anything is written, written whole) and
putting the new BoothPlayer in place without a restart.
"""
import hashlib
import json

from . import boothaudio

NOTHING = "Nothing was changed."
# The confirm the card shows for a device whose show audio outputs the
# engine cannot tell (Reserved.unsure): the operator's own check.
CONFIRM_WORDS = ("These are the show mixer's own outputs: I checked the "
                 "channel is free")


class Reserved:
    """The outputs a booth note must never play on (cold review of #280,
    P2-1): the show audio's and the announcements', by device. `known`
    channels are refused outright; a device in `unsure` (the engine cannot
    tell which of its outputs the show audio uses) is refused unless the
    operator ticks CONFIRM_WORDS; `everywhere` makes every device unsure.
    A device is matched by its name, whatever the audio system, so the
    same interface listed under MME and WASAPI is one device."""

    def __init__(self):
        self.known = {}        # name key -> {channel: "in use by ..."}
        self.unsure = {}       # name key -> why it cannot be told
        self.everywhere = ""   # why no device can be told

    @staticmethod
    def _key(name):
        return str(name or "").strip().lower()

    @staticmethod
    def _same(a, b):
        # Windows' MME cuts a name to 31 characters: a cut name is the
        # same device (fails closed).
        return bool(a) and bool(b) and (a == b or (
            min(len(a), len(b)) >= 31 and (a.startswith(b) or
                                           b.startswith(a))))

    def add(self, device, channels, why):
        k = self._key(device)
        if not k:
            return
        got = self.known.setdefault(k, {})
        for c in channels or ():
            if isinstance(c, int) and not isinstance(c, bool) and c >= 1:
                got.setdefault(int(c), why)

    def unknown(self, device, why):
        k = self._key(device)
        if k:
            self.unsure.setdefault(k, why)

    def for_device(self, name):
        """({channel: "in use by ..."}, why unsure or "") for one device."""
        k = self._key(name)
        taken, unsure = {}, self.everywhere
        for dk, chans in self.known.items():
            if self._same(dk, k):
                for c, why in chans.items():
                    taken.setdefault(c, why)
        if not unsure:
            for dk, why in self.unsure.items():
                if self._same(dk, k):
                    unsure = why
                    break
        return taken, unsure


def _announce():
    from . import announce
    return announce


def revision(block):
    """A short hash of the booth_alert block as the file has it now (None
    when there is none). The page sends back the one it read; a save is
    refused when the file's is different."""
    raw = json.dumps(block, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def choices(outs, want="", host="", reserved=None):
    """Every output on this PC for the device picker: [{"name", "hostapi",
    "channels", "label", "same_name", "cannot_pick", "picked"}]. The same
    rules as announce.Announcer.outputs: a name listed more than once needs
    its audio system; two with the same name on the same audio system
    cannot be told apart and cannot be picked."""
    A = _announce()
    counts, per_api = {}, {}
    for d in outs:
        k = d["name"].strip().lower()
        counts[k] = counts.get(k, 0) + 1
        ka = (k, d["hostapi"].strip().lower())
        per_api[ka] = per_api.get(ka, 0) + 1
    want = (want or "").strip().lower()
    host = (host or "").strip().lower()
    out = []
    for d in outs:
        taken, unsure = (reserved.for_device(d["name"]) if reserved
                         is not None else ({}, ""))
        k = d["name"].strip().lower()
        same = counts[k] > 1
        twin = per_api[(k, d["hostapi"].strip().lower())] > 1
        out.append({"name": d["name"], "hostapi": d["hostapi"],
                    "channels": int(d["channels"]),
                    "label": A.describe_output(d) + (
                        ", the same name twice here: rename one in "
                        "Windows Sound settings" if twin else ""),
                    "same_name": same, "cannot_pick": twin,
                    "reserved": {str(c): w for c, w in sorted(
                        taken.items())},
                    "unsure": unsure,
                    "picked": bool(want) and k == want and (
                        not same or d["hostapi"].strip().lower() == host)})
    return out


def validate(outs, device, hostapi, channels, reserved=None,
             confirm=False):
    """A pick checked against this PC's real outputs: (name, hostapi or "",
    (a,) or (a, b)) as the file will have them, or ValueError with a plain
    sentence ending "Nothing was changed.". device "" is the booth speaker
    off: ("", "", ()) and the channels are not looked at. `reserved`
    (Reserved) refuses a channel the show audio or the announcements use
    on that device, and a device whose outputs cannot be told unless
    `confirm` is the JSON word true."""
    A = _announce()
    if not isinstance(device, str):
        raise ValueError(f"Pick a device from the list. {NOTHING}")
    name = device.strip()
    if not name:
        return "", "", ()
    hits = [d for d in outs if d["name"].strip().lower() == name.lower()]
    if not hits:
        raise ValueError(f"{A._clean(name)} is not an output on this PC. "
                         f"{NOTHING}")
    if len(hits) > 1:
        h = hostapi.strip().lower() if isinstance(hostapi, str) else ""
        pick = [d for d in hits if d["hostapi"].strip().lower() == h]
        if len(pick) > 1:
            raise ValueError(A.same_api_sentence(name, pick) + " " + NOTHING)
        if len(pick) != 1:
            raise ValueError(
                f"{A._clean(name)} matches {len(hits)} outputs on this PC: "
                f"pick one: {'; '.join(A.describe_output(d) for d in hits)}"
                f". {NOTHING}")
        dev = pick[0]
        host = dev["hostapi"]
    else:
        dev = hits[0]
        host = ""
    if not isinstance(channels, (list, tuple)) or \
            len(channels) not in (1, 2) or \
            not all(isinstance(c, int) and not isinstance(c, bool)
                    for c in channels):
        raise ValueError(f"Pick Channel A, and Channel B or none. {NOTHING}")
    chans = tuple(channels)
    if any(c < 1 for c in chans):
        raise ValueError(f"Channels are counted from 1. {NOTHING}")
    if len(chans) == 2 and chans[0] == chans[1]:
        raise ValueError(f"Channel A and Channel B are the same output. "
                         f"Pick another for B, or none. {NOTHING}")
    have = int(dev["channels"])
    for c in chans:
        if c > have:
            raise ValueError(f"{A._clean(dev['name'])} has only {have} "
                             f"outputs, so channel {c} is not there. "
                             f"{NOTHING}")
    if reserved is None:
        # Never saved without knowing what the show uses (fails closed).
        raise ValueError(f"The outputs the show audio uses could not be "
                         f"read. {NOTHING}")
    taken, unsure = reserved.for_device(dev["name"])
    for c in chans:
        if c in taken:
            raise ValueError(f"Output {c} of {A._clean(dev['name'])} is in "
                             f"use by {taken[c]}. A booth note never plays "
                             f"there: pick another channel. {NOTHING}")
    if unsure and confirm is not True:
        raise ValueError(f"LTC Player cannot tell which outputs of "
                         f"{A._clean(dev['name'])} the show audio uses: "
                         f"{unsure}. Check the channel is free on the mixer, "
                         f"then tick \"{CONFIRM_WORDS}\" and Save. {NOTHING}")
    return dev["name"], host, chans


def line(player, sd=None):
    """The check line under the pickers, for the setting in use now: the
    booth player's own checks (BoothPlayer.check, the same lookup a note
    makes; notes and check_files), never a second opinion. Never raises."""
    try:
        if player is None or not getattr(player, "enabled", False):
            return {"ok": None,
                    "text": "Booth speaker: off. Pick a device and a "
                            "channel, then Save."}
        name = player.device_name
        ok, why = player.check(sd)
        if not ok:
            return {"ok": False, "text": f"Booth speaker: {why}"}
        nv = player.notes()
        text = (f"Booth speaker: {name}, "
                f"{boothaudio.outputs_words(player.config['channels'])}, "
                f"{nv['found']} of {nv['expected']} notes found.")
        bad = player.check_files()
        if nv["missing"]:
            text += f" Missing: {', '.join(nv['missing'])}."
        if bad:
            names = ", ".join(boothaudio.note_file(k) for k in sorted(bad))
            text += f" Cannot play: {names}."
        return {"ok": not nv["missing"] and not bad, "text": text}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "text": f"Booth speaker: could not be checked "
                                     f"({type(e).__name__}: {e})."}
