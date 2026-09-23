"""Show timeline: which FSEQ plays at which incoming timecode."""
import json
import os

from .tc import (normalize_rate, count_for, parse_tc, format_tc,  # noqa: F401
                 rate_label, tc_to_frames, frames_to_tc)


def _substitution_note(stored, cand):
    return (f"The show folder in this timeline is {stored}, which does not "
            f"exist on this machine. Using {cand} instead, which matches the "
            f"end of it. Fix \"show_dir\" in the timeline to stop this "
            f"happening every run.")


def resolve_show_dir(stored, timeline_path):
    """Find the show folder, even when the path in the file is from elsewhere.

    A timeline is written once and then opened on whatever machine is running
    the show. An absolute path written on one machine is meaningless on
    another, and the failure it produces is a bare "no such file" naming a
    directory the operator has never heard of. So: if the stored path is not
    there, try progressively shorter tails of it against the places a show
    folder actually lives. Returns (folder, note) and says what it did rather
    than substituting silently.
    """
    here = os.path.dirname(os.path.abspath(timeline_path))
    if not stored:
        return here, None
    # A RELATIVE show_dir means "beside this show file", not "beside whatever
    # folder you happened to be in". A bundle written for another machine
    # uses one, and resolving it against the working directory would make the
    # show depend on how it was launched.
    if not os.path.isabs(stored):
        cand = os.path.normpath(os.path.join(here, stored))
        if os.path.isdir(cand):
            return cand, None
    if os.path.isdir(stored):
        return os.path.abspath(stored), None

    parts = [p for p in stored.replace("\\", "/").split("/") if p]
    roots = [here, os.path.dirname(here), os.path.expanduser("~"),
             os.path.expanduser("~/Library/CloudStorage/Dropbox"),
             os.path.expanduser("~/Dropbox"),
             os.path.expanduser("~/Documents")]
    for i in range(len(parts)):
        tail = os.path.join(*parts[i:])
        for r in roots:
            if not r:
                continue
            cand = os.path.normpath(os.path.join(r, tail))
            if os.path.isdir(cand):
                note = _substitution_note(stored, cand)
                return cand, note
    raise ValueError(
        f"The show folder named in this timeline does not exist:\n"
        f"  {stored}\n"
        f"That path was written on a different machine. Open the timeline and "
        f"set \"show_dir\" to the folder holding the .fseq files and "
        f"xlights_networks.xml on THIS machine.")


class Cue:
    __slots__ = ("tc_text", "tc_seconds", "path", "name", "fseq", "_spans",
                 "duration", "_gaps")

    def __init__(self, tc_text, path, name=None):
        self.tc_text = tc_text
        self.path = path
        self.name = name or os.path.splitext(os.path.basename(path))[0]
        self.tc_seconds = None
        self.fseq = None
        self._spans = None
        self._gaps = None        # buffer stretches this cue does not address
        self.duration = None

    @property
    def end_seconds(self):
        if self.tc_seconds is None or self.duration is None:
            return None
        return self.tc_seconds + self.duration


class Timeline:
    def __init__(self, fps, cues, name="", show_dir="", drop=False,
                 idle_fseq=None, gaps=None, on_lost=None, input=None):
        self.fps = normalize_rate(fps)
        self.count = count_for(self.fps)
        self.drop = bool(drop)
        self.cues = cues
        self.name = name
        self.show_dir = show_dir
        self.idle_fseq = idle_fseq
        self.gaps = gaps
        self.on_lost = on_lost
        # Which audio input the timecode arrives on. Held in the show file so
        # nobody has to remember it, and so a change of interface is one edit
        # in one place rather than a different command line per operator.
        self.input = input or {}
        # How long a hole between cues is treated as frame-rounding rather
        # than as a gap. None means the player's default.
        self.bridge_ms = None
        # How long to free-roll through a dead feed, and the latency trim.
        # None means the command line's default.
        self.hold_ms = None
        self.offset_ms = None
        self.show_dir_note = None
        # Advatek SHOWTime scene triggers, the ALTERNATE playback mode. None
        # means this show file does not describe one and the mode cannot be
        # armed. Direct FSEQ playback is and stays the primary path.
        self.trigger = None
        # The show clock (clock.py). None means the path every show has
        # always had: LTC in on an audio input, decoded, chased. Only a show
        # file with a "clock" block ever imports the clock code.
        self.clock = None

    @property
    def rate_label(self):
        return rate_label(self.fps, self.drop)

    # Every setting this file may carry. Anything else is a typo, and a typo
    # in a show file has to be loud.
    KEYS = frozenset((
        "name", "fps", "drop", "show_dir", "cues",
        "idle", "preshow", "idle_fseq", "gaps", "on_lost",
        "bridge_ms", "hold_ms", "offset_ms", "input", "notes", "trigger",
        "clock",
    ))

    @classmethod
    def load(cls, path, fps=None, drop=None):
        """`fps` and `drop` override the file, for the console at 10pm when the
        source turns out to be 29.97 and nobody wants to edit JSON."""
        with open(path) as fh:
            doc = json.load(fh)
        # A setting spelled wrong is worse than a setting missing: the file
        # loads, the run looks healthy, and the thing you asked for silently
        # does not happen. Spelling "idle_fseq" instead of "idle" cost a
        # preshow look once. Say so at load time instead.
        unknown = [k for k in doc if k not in cls.KEYS]
        if unknown:
            raise ValueError(
                f"{path}: {', '.join(repr(k) for k in sorted(unknown))} "
                f"is not a setting this file has. It takes: "
                f"{', '.join(sorted(cls.KEYS))}.")
        fps = normalize_rate(fps if fps is not None else doc.get("fps", 30))
        drop = bool(doc.get("drop", False) if drop is None else drop)
        if drop and count_for(fps) != 30:
            raise ValueError(f"{path}: drop frame is only defined at 29.97 or 30")
        show_dir, show_dir_note = resolve_show_dir(doc.get("show_dir"), path)

        def resolve(p):
            return p if os.path.isabs(p) else os.path.join(show_dir, p)

        idle = doc.get("idle") or doc.get("preshow") or doc.get("idle_fseq")
        if isinstance(idle, dict):
            idle = idle.get("fseq")
        idle = resolve(idle) if idle else None

        gaps = doc.get("gaps")
        if gaps not in (None, "blackout", "idle", "hold"):
            raise ValueError(f"{path}: 'gaps' must be blackout, idle or hold")
        inp = doc.get("input") or {}
        if not isinstance(inp, dict):
            raise ValueError(f"{path}: 'input' must be an object like "
                             f'{{"device": "MOTU M4", "channel": 2}}')
        for k in inp:
            if k not in ("device", "channel", "rate"):
                raise ValueError(f"{path}: 'input' has no setting {k!r}; "
                                 f"it takes device, channel and rate")
        if "channel" in inp and (not isinstance(inp["channel"], int)
                                 or inp["channel"] < 1):
            raise ValueError(f"{path}: 'input.channel' counts from 1")

        bridge_ms = doc.get("bridge_ms")
        if bridge_ms is not None:
            if not isinstance(bridge_ms, (int, float)) or bridge_ms < 0:
                raise ValueError(f"{path}: 'bridge_ms' is a number of "
                                 f"milliseconds, 0 or more")
            if bridge_ms > 2000:
                raise ValueError(f"{path}: 'bridge_ms' of {bridge_ms} would "
                                 f"hold a finished cue for {bridge_ms/1000:.1f}s "
                                 f"before the gap look appears. It exists to "
                                 f"bridge frame-rounding between cues, not to "
                                 f"extend them; keep it under 2000.")

        # How long the show free-rolls through a dead feed before the rig
        # goes to the on_lost look. The default, 2s, is a rehearsal number:
        # "they stopped, go back to idle". On a show night a 3 second hiccup
        # on the LTC line -- a bumped cable, a Dante resubscribe -- would put
        # the preshow look on the rig in the middle of a song and then snap
        # back. The playback clock is accurate on its own for far longer than
        # that, so a show wants tens of seconds.
        hold_ms = doc.get("hold_ms")
        if hold_ms is not None:
            if not isinstance(hold_ms, (int, float)) or hold_ms < 100:
                raise ValueError(f"{path}: 'hold_ms' is a number of "
                                 f"milliseconds, 100 or more")
        offset_ms = doc.get("offset_ms")
        if offset_ms is not None:
            if not isinstance(offset_ms, (int, float)) or abs(offset_ms) > 5000:
                raise ValueError(f"{path}: 'offset_ms' is a latency trim in "
                                 f"milliseconds, between -5000 and 5000")

        on_lost = doc.get("on_lost")
        if on_lost not in (None, "preshow", "hold", "blackout", "freerun"):
            raise ValueError(f"{path}: 'on_lost' must be preshow, hold, "
                             f"blackout or freerun")
        if on_lost == "preshow" and not idle:
            raise ValueError(f"{path}: 'on_lost' is 'preshow' but no 'idle' "
                             f"sequence is set")
        if gaps == "idle" and not idle:
            raise ValueError(f"{path}: 'gaps' is 'idle' but no 'idle' sequence "
                             f"is set")

        cues = []
        seen = {}
        for i, c in enumerate(doc.get("cues", [])):
            if "tc" not in c or "fseq" not in c:
                raise ValueError(f"cue {i} needs both 'tc' and 'fseq'")
            cue = Cue(c["tc"], resolve(c["fseq"]), c.get("name"))
            cue.tc_seconds = parse_tc(c["tc"], fps, drop)
            if cue.tc_seconds in seen:
                raise ValueError(f"two cues both start at {c['tc']}: "
                                 f"{seen[cue.tc_seconds]} and {cue.name}")
            seen[cue.tc_seconds] = cue.name
            cues.append(cue)
        cues.sort(key=lambda c: c.tc_seconds)
        tl = cls(fps, cues, doc.get("name", ""), show_dir, drop, idle, gaps,
                 on_lost, inp)
        tl.bridge_ms = bridge_ms
        tl.hold_ms = hold_ms
        tl.offset_ms = offset_ms
        tl.show_dir_note = show_dir_note
        # Parsed last, so a bad trigger block reports against a timeline that
        # is otherwise known good.
        from .trigger import TriggerConfig
        tl.trigger = TriggerConfig.parse(doc.get("trigger"), path)
        # Imported only when asked for, so a show file without a clock block
        # never loads a line of it.
        if doc.get("clock") is not None:
            from .clock import ClockConfig
            tl.clock = ClockConfig.parse(doc["clock"], path)
        return tl

    def _index_at(self, tc_seconds):
        """Index of the last cue whose start is at or before tc, else -1."""
        lo, hi, best = 0, len(self.cues) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.cues[mid].tc_seconds <= tc_seconds:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def cue_at(self, tc_seconds):
        """The cue in force at this timecode, or None before the first cue."""
        i = self._index_at(tc_seconds)
        return self.cues[i] if i >= 0 else None

    def next_cue(self, tc_seconds):
        """The next cue that has not started yet, or None past the last one.

        Strictly after, so at the exact frame a cue starts the display already
        names the one after it rather than pointing at what is playing."""
        i = self._index_at(tc_seconds) + 1
        return self.cues[i] if i < len(self.cues) else None

    def format(self, seconds):
        return format_tc(seconds, self.fps, self.drop)

    def parse(self, text):
        return parse_tc(text, self.fps, self.drop)


def starter(show_dir, fps=30, start="01:00:00:00", gap_seconds=0.0):
    """Build a timeline covering every FSEQ in a show folder, back to back.

    Written as a starting point to edit, not as a guess at the real running
    order: the sequences are laid out alphabetically with a fixed gap."""
    import glob
    from .fseq import FSEQ
    fps = normalize_rate(fps)
    files = sorted(glob.glob(os.path.join(show_dir, "*.fseq")))
    t = parse_tc(start, fps)
    cues = []
    for p in files:
        try:
            with FSEQ(p) as f:
                dur = f.duration_ms / 1000.0
        except Exception:
            continue
        cues.append({"tc": format_tc(t, fps), "fseq": os.path.basename(p)})
        t += dur + gap_seconds
    return {"name": os.path.basename(show_dir.rstrip("/")),
            "fps": fps, "show_dir": show_dir, "cues": cues}
