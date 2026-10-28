"""Checks the booth alert notes the Windows installer ships
(packaging/windows/booth, installed as {app}\\Booth notes).

    python packaging/windows/check_booth_notes.py              report
    python packaging/windows/check_booth_notes.py --require    the full set
    python packaging/windows/check_booth_notes.py --folder DIR another folder

Every expected file (ltcplay/boothaudio.py EXPECTED_FILES, booth_<name>.wav)
is listed as found, with its length, or MISSING. A file that is there but
could not play (not a WAV this program reads, or longer than a booth note
may be, boothaudio.MAX_NOTE_S) always fails: it would never sound. A
missing note, or a booth_*.wav that is not an expected name (a misspelt
one would ship and never play), fails only with --require, or with
BOOTH_NOTES_REQUIRED=1 in the environment. The windows-app workflow
passes --require once the repository variable REQUIRE_BOOTH_NOTES is
true, so the PRs before the recordings arrive stay green.

In GitHub Actions each problem is also a ::warning:: or ::error::
annotation. Exit 0 when nothing fails, 1 when something does.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ltcplay import boothaudio as BA       # noqa: E402

FOLDER = os.path.join(HERE, "booth")


def check(folder):
    """{"found": [(file, length_s)], "missing": [file], "bad": {file:
    why}, "extra": [file]} for `folder`. Names are matched exactly, case
    included: the installer copies them as they are."""
    try:
        present = set(os.listdir(folder))
    except OSError:
        present = set()
    out = {"found": [], "missing": [], "bad": {}, "extra": []}
    for fn in BA.EXPECTED_FILES:
        if fn not in present:
            out["missing"].append(fn)
            continue
        path = os.path.join(folder, fn)
        try:
            _pcm, _ch, _rate, length = BA._announce()._wav_info(
                path, decode=False)
        except ValueError as e:
            out["bad"][fn] = str(e)
            continue
        if length > BA.MAX_NOTE_S:
            out["bad"][fn] = (f"{fn} is {length:.1f} s long; a booth note "
                              f"is at most {BA.MAX_NOTE_S:g} s.")
            continue
        out["found"].append((fn, length))
    expected = set(BA.EXPECTED_FILES)
    out["extra"] = sorted(n for n in present
                          if n.lower().endswith(".wav")
                          and n not in expected)
    return out


def _annotate(level, text):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title=booth notes::{text}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--require", action="store_true",
                    help="fail when any expected note is missing")
    ap.add_argument("--folder", default=FOLDER)
    args = ap.parse_args(argv)
    require = args.require or os.environ.get("BOOTH_NOTES_REQUIRED") == "1"
    r = check(args.folder)
    n = len(BA.EXPECTED_FILES)
    print(f"Booth notes in {args.folder}: {len(r['found'])} of {n} found "
          f"and playable" + (" (the full set is required)" if require
                             else ""))
    for fn, length in r["found"]:
        print(f"  ok       {fn}  {length:.1f} s")
    for fn in r["missing"]:
        print(f"  MISSING  {fn}")
    for fn, why in sorted(r["bad"].items()):
        print(f"  CANNOT PLAY  {fn}: {why}")
    for fn in r["extra"]:
        print(f"  NOT EXPECTED  {fn}: no note has this name, so it would "
              f"never play")
    failed = bool(r["bad"])
    for fn, why in sorted(r["bad"].items()):
        _annotate("error", f"{fn} cannot play: {why}")
    level = "error" if require else "warning"
    if r["missing"]:
        _annotate(level, f"{len(r['missing'])} of {n} booth notes are "
                         f"missing: {', '.join(r['missing'])}")
    if r["extra"]:
        _annotate(level, f"not an expected booth note name: "
                         f"{', '.join(r['extra'])}")
    if require and (r["missing"] or r["extra"]):
        failed = True
    if failed:
        print("FAILED: " + "; ".join(
            ([f"cannot play: {', '.join(sorted(r['bad']))}"] if r["bad"]
             else []) +
            ([f"missing: {', '.join(r['missing'])}"]
             if require and r["missing"] else []) +
            ([f"not expected: {', '.join(r['extra'])}"]
             if require and r["extra"] else [])))
        return 1
    if r["missing"]:
        print(f"{len(r['missing'])} missing; not required yet, so this "
              f"passes. Pass --require to make it fail.")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
