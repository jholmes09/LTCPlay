"""Build a timeline from a marker export (the CSV a DAW writes for a cue list).

The show is one soundtrack with markers at each song, so the markers already
hold the real running order and the real in-points.  That beats laying the
sequences end to end by duration, which silently drifts wherever a song has an
intro marker of its own.

Expected columns, tab or comma separated, header row present:
    Name    Start   Duration    Time Format     Type    Description
Only Name and Start are used.
"""
import csv
import os
import re

from .fseq import FSEQ
from .timeline import parse_tc, format_tc
from .tc import normalize_rate

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s):
    return _NORM.sub("", (s or "").lower())


def read_markers(path, fps):
    """[(name, seconds)] in file order."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        rows = list(csv.reader(fh, delimiter=delim))
    if not rows:
        raise ValueError(f"{path} is empty")
    head = [c.strip().lower() for c in rows[0]]
    try:
        i_name = head.index("name")
        i_start = head.index("start")
    except ValueError:
        raise ValueError(f"{path}: needs 'Name' and 'Start' columns, found {head}")
    out = []
    for r in rows[1:]:
        if len(r) <= max(i_name, i_start):
            continue
        name = r[i_name].strip()
        start = r[i_start].strip()
        if not name or not start:
            continue
        out.append((name, parse_tc(start, fps)))
    return out


def _song_part(filename):
    """'GPL 2026_Set 1_MonsterMash.fseq' -> 'MonsterMash'."""
    base = os.path.splitext(os.path.basename(filename))[0]
    return base.split("_")[-1]


def match(markers, fseq_paths, fps, tolerance=0.25):
    """Pair markers with sequences.

    Two passes.  First by name, scoring a match by how much of the marker name
    the file accounts for, so 'MonsterMash' prefers the 'Monster Mash' marker
    over 'Monster Mash Remix'.  Then a duration check against the marker grid:
    a sequence whose end does not land on a later marker is re-homed to the
    marker where it does fit exactly, which is how a song that begins at its
    own intro marker gets placed correctly.
    """
    grid = [t for _, t in markers]
    durations = {}
    for p in fseq_paths:
        try:
            with FSEQ(p) as f:
                durations[p] = f.duration_ms / 1000.0
        except Exception:
            durations[p] = None

    # pass 1: name
    assigned = {}          # marker index -> path
    notes = []
    for p in fseq_paths:
        if durations[p] is None:
            continue
        song = _norm(_song_part(p))
        if not song:
            continue
        best, best_score = None, 0.0
        for i, (name, _) in enumerate(markers):
            n = _norm(name)
            if song and (song in n or n in song):
                score = len(song) / max(len(n), 1) if song in n else len(n) / len(song)
                if score > best_score:
                    best, best_score = i, score
        if best is not None and best not in assigned:
            assigned[best] = p

    # pass 2: duration fit
    for i in sorted(assigned):
        p = assigned[i]
        end = markers[i][1] + durations[p]
        if any(abs(end - g) <= tolerance for g in grid) or \
           abs(end - (grid[-1] + (durations.get(assigned.get(len(markers)-1)) or 0))) <= tolerance:
            continue
        # does it fit exactly from some other marker?
        candidates = [j for j, (_, t) in enumerate(markers)
                      if j not in assigned and
                      any(abs(t + durations[p] - g) <= tolerance for g in grid)]
        if len(candidates) == 1:
            j = candidates[0]
            notes.append(f"{os.path.basename(p)}: name matched "
                         f"'{markers[i][0]}' at {format_tc(markers[i][1], fps)} but its "
                         f"{durations[p]:.1f}s runs past the next marker. Moved to "
                         f"'{markers[j][0]}' at {format_tc(markers[j][1], fps)}, where it "
                         f"ends exactly on a marker.")
            del assigned[i]
            assigned[j] = p
        else:
            notes.append(f"{os.path.basename(p)}: at '{markers[i][0]}' its "
                         f"{durations[p]:.1f}s does not end on a marker. Check it.")

    unprogrammed = [(markers[i][0], markers[i][1])
                    for i in range(len(markers)) if i not in assigned]
    return assigned, unprogrammed, notes, durations


_SET = re.compile(r"set\s*(\d+)", re.I)


def build(markers_csv, show_dir, fps=30, start="00:00:00:00", tolerance=0.25,
          restrict_set=True):
    import glob
    fps = normalize_rate(fps)
    markers = read_markers(markers_csv, fps)
    if not markers:
        raise ValueError(f"{markers_csv}: no markers found")
    files = sorted(glob.glob(os.path.join(show_dir, "*.fseq")))
    # If the marker file names a set, only consider sequences from that set.
    # Without this, Set 2's opener silently borrows Set 1's opener sequence,
    # which is a different piece of music, and the timeline looks complete
    # when two songs are actually unprogrammed.
    set_note = None
    if restrict_set:
        m = _SET.search(os.path.basename(markers_csv))
        if m:
            want = _norm("set" + m.group(1))
            in_set = [f for f in files if want in _norm(os.path.basename(f))]
            if in_set:
                dropped = len(files) - len(in_set)
                files = in_set
                set_note = (f"Restricted to the {len(files)} sequences whose name "
                            f"contains 'Set {m.group(1)}', ignoring {dropped} others.")
    assigned, unprogrammed, notes, durations = match(markers, files, fps, tolerance)
    offset = parse_tc(start, fps)
    cues = []
    for i in sorted(assigned):
        p = assigned[i]
        cues.append({"tc": format_tc(markers[i][1] + offset, fps),
                     "fseq": os.path.basename(p),
                     "name": markers[i][0]})
    doc = {"name": os.path.splitext(os.path.basename(markers_csv))[0],
           "fps": fps, "show_dir": show_dir, "cues": cues}
    if set_note:
        notes.insert(0, set_note)
    return doc, unprogrammed, notes, markers
