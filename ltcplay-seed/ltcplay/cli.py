"""ltcplay - chase xLights FSEQ sequences to incoming SMPTE LTC, on a Mac.

Written as a rehearsal tool and since taken into production. One process on
one machine is the whole risk; see the honest risk list in README.md.
"""
import argparse
import json
import os
import sys
import threading
import time

from . import audio as audio_mod
from . import display as display_mod
from . import netmap as netmap_mod
from . import settings as settings_mod
from . import timeline as timeline_mod
from .ltc import LTCDecoder
from .output import Sender
from .player import Player, LOCKED, FREEWHEEL, LOST
from .showlog import ShowLog
from .tc import tc_to_frames

BLOCK = 512     # 10.7ms at 48kHz; the floor on input latency


def _err(msg):
    print(f"error: {msg}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- audio ---
def _import_sounddevice():
    try:
        import sounddevice
        return sounddevice
    except Exception as e:
        raise SystemExit(
            "sounddevice is not installed or could not load PortAudio.\n"
            f"  {e}\n"
            "Run the installer again, or: pip install sounddevice numpy")


def cmd_devices(args):
    sd = _import_sounddevice()
    inputs = audio_mod.list_inputs(sd)
    if not inputs:
        print("No audio input on this Mac. A USB interface has to be plugged "
              "in and powered before it shows up here, and the headphone jack "
              "is only an input while a TRRS adapter is in it.")
        return 1
    print(audio_mod.describe(inputs))
    print("\nA name is what to put in the timeline or after --device: indexes "
          "move when\nanything else is plugged or unplugged, names do not. "
          "`ltcplay find` will tell you\nwhich device AND which of its inputs "
          "the timecode is actually on.")
    return 0


def cmd_input(args):
    """Choose the input once, and keep it.

    Searching for the timecode every time you sit down is fine the first day
    and tiresome by the third. This writes the answer next to the launcher so
    every later run just uses it."""
    if args.clear:
        print("Cleared." if settings_mod.clear() else "Nothing was set.")
        return 0

    sd = _import_sounddevice()
    inputs = audio_mod.list_inputs(sd)
    cands = audio_mod.candidates(inputs)
    saved = settings_mod.load()

    if args.device:
        try:
            dev = audio_mod.resolve_device(sd, args.device)
        except audio_mod.DeviceError as e:
            return _err(str(e))
        ch = int(args.channel or 1)
        if ch > dev["channels"]:
            return _err(f"{dev['name']} has {dev['channels']} input(s), so "
                        f"there is no input {ch}.")
        settings_mod.save(dev["name"], ch, args.rate)
        print(f"Set: {dev['name']}, input {ch}. Saved to "
              f"{settings_mod.FILENAME}.")
        return 0

    if saved:
        print(f"Currently set: {saved.get('device')}, input "
              f"{saved.get('channel', 1)}"
              + (f", {saved['rate']}Hz" if saved.get("rate") else ""))
        live = [d for d in inputs
                if saved.get("device", "").lower() in d["name"].lower()]
        if not live:
            print("  That device is NOT attached right now.")
    else:
        print("No input is set, so ltcplay uses whatever macOS calls the "
              "default.")
    print()

    if not cands:
        print("Nothing attached can carry timecode from outside this Mac.")
        print(audio_mod.describe(inputs))
        return 1

    print("Choose one:")
    for i, d in enumerate(cands, 1):
        note = audio_mod._KIND_NOTE.get(d["kind"], "")
        print(f"  {i}  {d['name']}   {d['channels']} in @ {d['rate']}Hz"
              + (f"   {note}" if note else ""))
    print(f"  0  clear it and use the system default")
    if not sys.stdin.isatty():
        print("\nNot a terminal, so nothing was changed. Run this from the "
              "menu, or use\n  ./ltc input --device \"NAME\" --channel N")
        return 0
    try:
        pick = input("\n  number: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if pick == "0":
        print("Cleared." if settings_mod.clear() else "Nothing was set.")
        return 0
    try:
        dev = cands[int(pick) - 1]
        if int(pick) < 1:
            raise ValueError
    except (ValueError, IndexError):
        return _err(f"{pick!r} is not one of the numbers listed.")

    ch = 1
    if dev["channels"] > 1:
        try:
            raw = input(f"  which of its {dev['channels']} inputs carries "
                        f"timecode? [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if raw:
            try:
                ch = int(raw)
            except ValueError:
                return _err(f"{raw!r} is not an input number.")
            if not 1 <= ch <= dev["channels"]:
                return _err(f"{dev['name']} has inputs 1 to {dev['channels']}.")
    settings_mod.save(dev["name"], ch)
    print(f"\nSet: {dev['name']}, input {ch}. Saved to "
          f"{settings_mod.FILENAME}, and used by every run from now on.")
    if dev["channels"] > 1 and not args.no_check:
        print("Run `./ltc find` with timecode rolling if you are not sure "
              "which input it is on.")
    return 0


def _fingerprint(path, blocks=8, block=65536):
    """Size plus a sample hash: enough to notice a file changing, fast enough
    to run over 18 sequences before a show without anybody waiting."""
    import hashlib
    size = os.path.getsize(path)
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as fh:
        if size <= blocks * block:
            h.update(fh.read())
        else:
            for i in range(blocks):
                fh.seek(int(i * (size - block) / (blocks - 1)))
                h.update(fh.read(block))
    return size, h.hexdigest()[:16]


MANIFEST = "ltcplay_verified.json"


def cmd_verify(args):
    """Prove the sequences are the ones you think they are.

    A filename is not evidence. Every xLights FSEQ records the audio file it
    was rendered against, and that is: a sequence named LetsDance that was
    rendered from MonsterMash.mp3 says so in its own header, whatever anybody
    called it afterwards. This checks that, the run length against the cue
    grid, the channel range against the controller map, and a fingerprint of
    every file against the last time you ran it, so you can tell whether
    anything moved underneath you since."""
    import json
    from . import models as models_mod
    from .fseq import FSEQ
    tl = timeline_mod.Timeline.load(args.timeline)
    if tl.show_dir_note:
        print(f"note: {tl.show_dir_note}\n", file=sys.stderr)

    # A bundle ships a SHA-256 of every render it copied. Checking it is the
    # whole point of bundling, and `verify` did not look: a corrupted copy
    # passed both `check` and `verify` and failed 16 seconds into a cue, on
    # the rig, with a diagnosis blaming xLights. Round 3, 2026-09-13.
    bpath = os.path.join(os.path.dirname(os.path.abspath(args.timeline)),
                         "ltcplay_bundle.json")
    bundle_bad = []
    if os.path.exists(bpath):
        try:
            bman = json.load(open(bpath)).get("files", {})
        except (ValueError, OSError):
            bman = {}
        import hashlib as _hl
        for bname, rec in sorted(bman.items()):
            f = os.path.join(tl.show_dir, bname)
            if not os.path.exists(f):
                bundle_bad.append(f"{bname}: shipped in this bundle and is "
                                  f"no longer there")
                continue
            if os.path.getsize(f) != rec.get("bytes"):
                bundle_bad.append(f"{bname}: {os.path.getsize(f)} bytes now, "
                                  f"{rec.get('bytes')} when it was bundled")
                continue
            h = _hl.sha256()
            with open(f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != rec.get("sha256"):
                bundle_bad.append(f"{bname}: the bytes have changed since "
                                  f"this bundle was made")
        print(f"bundle: {len(bman)} file(s) checked against the SHA-256 "
              f"recorded when it was built"
              + ("" if not bundle_bad else f"  -- {len(bundle_bad)} BAD"))

    mpath = os.path.join(os.path.dirname(os.path.abspath(args.timeline)), MANIFEST)
    old = {}
    if os.path.exists(mpath) and not args.no_manifest:
        try:
            old = json.load(open(mpath)).get("files", {})
        except (ValueError, OSError):
            old = {}

    print(f"{tl.name or os.path.basename(args.timeline)}: {len(tl.cues)} cues")
    print(f"Reading from {tl.show_dir}\n")

    # The sparse ranges in an FSEQ are absolute channel positions, so they are
    # a fingerprint of the model layout it was rendered against. When most of a
    # show agrees and a few do not, those few are old renders: they leave whole
    # props dark and write to channels that have since been given to something
    # else. Nothing about the filename or the audio shows this.
    layouts = {}
    for cue in tl.cues:
        if not os.path.exists(cue.path):
            continue
        try:
            with FSEQ(cue.path) as f:
                key = tuple(f.sparse_ranges or [(0, f.channel_count)])
        except Exception:
            continue
        layouts.setdefault(key, []).append(os.path.basename(cue.path))
    majority = max(layouts, key=lambda k: len(layouts[k])) if layouts else ()
    maj_starts = {s0: ln for s0, ln in majority}
    pmap = None
    try:
        pmap = models_mod.load(tl.show_dir,
                               netmap_mod.load(getattr(args, "networks", None)
                                               or os.path.join(
                                                   tl.show_dir,
                                                   "xlights_networks.xml")))
    except Exception:
        pmap = None

    def prop(ch):
        n = pmap.name_at(ch) if pmap else None
        return f"  ({n})" if n else ""

    # One sequence used at two timecodes is a deliberate thing (the same
    # ending closing both sets), not a mistake. Saying so keeps it from reading
    # as a duplicate somebody should go and fix.
    used = {}
    for c in tl.cues:
        used.setdefault(os.path.basename(c.path), []).append(c.tc_text)

    fresh, problems, notes = {}, [], []
    problems.extend(bundle_bad)
    rows = []
    for i, cue in enumerate(tl.cues):
        path = cue.path
        base = os.path.basename(path)
        row = {"tc": cue.tc_text, "name": cue.name, "file": base,
               "verdict": [], "media": None, "renderer": None}
        if not os.path.exists(path):
            row["verdict"].append("FILE MISSING")
            problems.append(f"{cue.name}: {path} is not there")
            rows.append(row)
            continue
        try:
            with FSEQ(path) as f:
                dur = f.duration_ms / 1000.0
                row["media"] = f.media_file
                row["renderer"] = f.renderer
                row["seconds"] = dur
                row["channels"] = f.channel_count
                f_ranges = list(f.sparse_ranges or [])
                ceiling = tl_total(tl, args)
                over = sum(ln for s, ln in
                           (f.sparse_ranges or [(0, f.channel_count)])
                           if s + ln > ceiling)
        except Exception as e:
            row["verdict"].append(f"WILL NOT OPEN: {e}")
            problems.append(f"{cue.name}: {e}")
            rows.append(row)
            continue

        if over:
            row["verdict"].append(f"{over} channels past the controller map")
            problems.append(
                f"{cue.name}: {over} channels are addressed past {ceiling}, "
                f"the end of the controller map, and will not be output. "
                f"Re-render after a controller change.")

        # Layout, against what the rest of the show agrees on.
        mine = {s0: ln for s0, ln in (f_ranges or [])}
        if majority and mine and mine != maj_starts:
            missing = sorted(set(maj_starts) - set(mine))
            foreign = sorted(set(mine) - set(maj_starts))
            short = sorted(s0 for s0 in set(mine) & set(maj_starts)
                           if mine[s0] != maj_starts[s0])
            row["verdict"].append("RENDERED AGAINST A DIFFERENT LAYOUT")
            detail = [f"{cue.name} ({base}) was rendered against a different "
                      f"model layout from the other "
                      f"{len(layouts[majority])} sequences."]
            for s0 in missing:
                detail.append(f"    does not contain ch {s0+1}..{s0+maj_starts[s0]}"
                              f"{prop(s0 + 1)} -- it stays dark")
            for s0 in foreign:
                detail.append(f"    writes ch {s0+1}..{s0+mine[s0]}"
                              f"{prop(s0 + 1)} -- today those channels are "
                              f"something else")
            for s0 in short:
                detail.append(f"    covers {mine[s0]} of "
                              f"{maj_starts[s0]} channels at ch {s0+1}"
                              f"{prop(s0 + 1)}")
            detail.append("    Re-render it in xLights.")
            problems.append("\n".join(detail))

        size, digest = _fingerprint(path)
        fresh[base] = {"size": size, "hash": digest,
                       "mtime": int(os.path.getmtime(path))}
        was = old.get(base)
        if was:
            if was.get("hash") != digest or was.get("size") != size:
                row["verdict"].append("CHANGED since last verify")
                notes.append(f"{base} has changed since you last ran verify. "
                             f"Re-check it is the render you want.")
        elif old:
            row["verdict"].append("new since last verify")

        # Identity: what audio was this actually rendered against?
        stem = os.path.splitext(base)[0]
        if row["media"]:
            mstem = os.path.splitext(os.path.basename(row["media"]))[0]
            a, b = _norm_stem(mstem), _norm_stem(stem)
            if a != b and a not in b and b not in a:
                row["verdict"].append(f"RENDERED FROM {os.path.basename(row['media'])}")
                problems.append(
                    f"{cue.name}: the file is called {base} but it was rendered "
                    f"against {os.path.basename(row['media'])}. One of the two "
                    f"is wrong, and the audio is the one that decides.")
        else:
            row["verdict"].append("no media recorded")
            notes.append(f"{base} does not record the audio it was rendered "
                         f"from, so its identity cannot be proved from inside "
                         f"the file. Older xLights renders look like this.")

        # Does it end where the next cue begins?
        nxt = tl.cues[i + 1] if i + 1 < len(tl.cues) else None
        end = cue.tc_seconds + dur
        row["ends"] = tl.format(end)
        if nxt is not None:
            gap = nxt.tc_seconds - end
            row["gap"] = gap
            if gap < -0.05:
                row["verdict"].append(f"OVERLAPS THE NEXT CUE BY {-gap:.1f}s")
                problems.append(f"{cue.name} runs {-gap:.1f}s into {nxt.name}")
        rows.append(row)

    w = max((len(r["file"]) for r in rows), default=10)
    for r in rows:
        mark = "ok" if not r["verdict"] else "; ".join(r["verdict"])
        media = os.path.basename(r["media"]) if r["media"] else "-"
        print(f"  {r['tc']}  {r['file']:{w}s}")
        print(f"               rendered from {media}"
              + (f"   {r['seconds']:.1f}s -> {r.get('ends','?')}" if r.get("seconds") else ""))
        shared = used.get(r["file"], [])
        if len(shared) > 1:
            print(f"               also used at "
                  + ", ".join(t for t in shared if t != r["tc"])
                  + "  (one file, deliberately)")
        print(f"               {mark}")

    renderers = {r["renderer"] for r in rows if r["renderer"]}
    if len(layouts) > 1:
        print(f"\n  {len(layouts)} different model layouts in one show. The "
              f"majority has {len(majority)} ranges;\n  the odd ones are "
              f"listed as problems below.")
    if len(renderers) > 1:
        notes.append("These were rendered by more than one xLights build: "
                     + ", ".join(sorted(renderers)) + ". Not wrong on its own, "
                     "but worth knowing if one of them behaves oddly.")
    elif renderers:
        print(f"\n  all rendered by {list(renderers)[0]}")

    if not args.no_manifest:
        # A read-only show folder must not throw away everything verify just
        # worked out. It used to traceback here, AFTER printing the report.
        # Round 4 of the audit, 2026-09-13.
        try:
            with open(mpath, "w") as fh:
                json.dump({"timeline": os.path.basename(args.timeline),
                           "checked": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "files": fresh}, fh, indent=2)
        except OSError as e:
            print(f"\n  Could not record the fingerprints ({e.strerror or e}). "
                  f"This folder is not\n  writable, so verify cannot tell you "
                  f"next time whether anything changed.", file=sys.stderr)
        else:
            print(f"\n  fingerprints written to {MANIFEST}; run verify again "
                  f"before the show\n  and it will tell you if anything "
                  f"changed.")

    if notes:
        print("\nWorth knowing:")
        for n in notes:
            print(f"  - {n}")
    if problems:
        print("\nProblems:")
        for pr in problems:
            print(f"  - {pr}")
        return 1
    print("\nEvery sequence is the one this show file names, rendered from the "
          "matching audio,\nand every one agrees with the same model layout.")
    return 0


def tl_total(tl, args):
    """Channel ceiling from the controller map, when one is reachable."""
    try:
        nm_path = getattr(args, "networks", None) or os.path.join(
            tl.show_dir, "xlights_networks.xml")
        return netmap_mod.load(nm_path).total_channels
    except Exception:
        return 1 << 30


def _norm(s):
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _norm_stem(s):
    """Normalise a sequence or media stem for comparison.

    Track numbers are a naming convention, not identity: a render called
    "GPL 2026_Set 1_02_MonsterMash" is the same song as "GPL 2026_Set 1_
    MonsterMash.mp3", and comparing the raw stems calls every numbered file in
    the show a mismatch. Segments that are ONLY digits are dropped; "Set 1" is
    not, so Set 1 against Set 2 is still caught."""
    import re as _re
    parts = [p for p in _re.split(r"[_\-]", s or "") if not p.strip().isdigit()]
    return _norm("".join(parts))


def cmd_at(args):
    """Say exactly which file plays at a given timecode, without running anything.

    "It played the wrong one" is almost impossible to chase during a show and
    trivial to settle beforehand, so this answers it for any moment: the cue,
    the file, the full path it was read from, and the frame."""
    tl = timeline_mod.Timeline.load(args.timeline)
    if tl.show_dir_note:
        print(f"note: {tl.show_dir_note}\n", file=sys.stderr)
    try:
        t = tl.parse(args.timecode)
    except ValueError as e:
        return _err(str(e))
    cue = tl.cue_at(t)
    nxt = tl.next_cue(t)
    print(f"At {args.timecode} ({tl.rate_label} fps), reading from:")
    print(f"  {tl.show_dir}\n")

    from .fseq import FSEQ
    if cue is None:
        print(f"  Nothing has started yet. The first cue is {nxt.name} at "
              f"{nxt.tc_text}.")
        print(f"  Until then the rig shows: "
              f"{tl.gaps or 'blackout'} (the 'gaps' setting).")
        return 0
    path = cue.path
    if not os.path.exists(path):
        print(f"  {cue.name} starts at {cue.tc_text} but its file is MISSING:")
        print(f"    {path}")
        return 1
    with FSEQ(path) as f:
        dur = f.duration_ms / 1000.0
        step = f.step_time_ms
        count = f.frame_count
    offset = t - cue.tc_seconds
    if offset >= dur:
        print(f"  Nothing is playing. {cue.name} ran from {cue.tc_text} to "
              f"{tl.format(cue.tc_seconds + dur)} and has finished.")
        if nxt:
            print(f"  Next is {nxt.name} at {nxt.tc_text}, in "
                  f"{nxt.tc_seconds - t:.1f}s.")
        else:
            print("  There is no cue after it.")
        print(f"  In between the rig shows: {tl.gaps or 'blackout'}.")
        return 0
    idx = int(offset * 1000.0 // step)
    from .tc import format_seq
    # The headline is the xLights position, not the offset in seconds: this
    # command exists so a note written against show timecode can be found
    # inside the sequence, and xLights counts in M:SS.mmm from the top of
    # the sequence.
    print(f"  {format_seq(offset)} into {cue.name}   "
          f"(xLights position; frame {idx})")
    print(f"    file    {os.path.basename(path)}")
    print(f"    path    {path}")
    with FSEQ(path) as f:
        if f.media_file:
            print(f"    audio   {os.path.basename(f.media_file)}   "
                  f"(recorded inside the sequence by xLights)")
    print(f"    frame   {idx} of {count}   ({step}ms steps, "
          f"{format_seq(dur)} long)")
    print(f"    runs    {cue.tc_text} to {tl.format(cue.tc_seconds + dur)}")
    if nxt:
        print(f"    then    {nxt.name} at {nxt.tc_text}")
    return 0


def cmd_find(args):
    """Listen to every input channel and report where timecode actually is."""
    sd = _import_sounddevice()
    dev = None
    if args.device:
        try:
            dev = audio_mod.resolve_device(sd, args.device)
        except audio_mod.DeviceError as e:
            return _err(str(e))
    allin = audio_mod.list_inputs(sd)
    if dev:
        targets = [dev]
    elif args.all:
        targets = audio_mod.hardware_first(allin)
    else:
        targets = audio_mod.candidates(allin)
    if not targets:
        return _err("Nothing attached can carry timecode from outside this "
                    "Mac.\n" + audio_mod.describe(allin))
    skipped = len(allin) - len(targets)
    print(f"Listening to {len(targets)} device(s) for {args.seconds:g}s each. "
          f"Timecode must be RUNNING for this to find anything.")
    if skipped and not args.all:
        print(f"Skipping {skipped} software or phone input(s) that cannot "
              f"carry it. Use --all to include them.")
    print()
    found = []
    for d in targets:
        res = audio_mod.scan(sd, LTCDecoder, seconds=args.seconds, device=d)[0]
        kind = d.get("kind", "hardware")
        tag = "" if kind == "hardware" else f"   ({kind})"
        head = f"  [{d['index']}] {d['name']}   {d['channels']} in{tag}"
        if res["error"]:
            print(f"{head}\n        could not open it: {res['error']}")
            continue
        print(f"{head} @ {res['rate']}Hz")
        for c in res["channels"]:
            bar = _meter(c["level"])
            if c["frames"] > 3:
                from .tc import rate_label
                lbl = (rate_label(c["rate"], c["drop"]) if c["rate"]
                       else "rate not settled")
                print(f"        in {c['channel']}  {bar} {c['level']:.2f}  "
                      f"TIMECODE  {c['last']}  {lbl}"
                      f"{'  <-- use this' if not found else ''}")
                found.append((d, c))
            elif c["verdict"] == "silent":
                print(f"        in {c['channel']}  {bar} {c['level']:.2f}  silent")
            else:
                print(f"        in {c['channel']}  {bar} {c['level']:.2f}  "
                      f"audio, but no timecode in it ({c['verdict']})")
    print()
    if not found:
        print("No timecode on any input. Either it is not running, or the "
              "cable is in an output,\nor the interface is muted. Check the "
              "level column: a channel reading 0.00 is not\nreceiving "
              "anything at all.")
        real = [d for d in targets if d.get("kind", "hardware") == "hardware"]
        if not real:
            print("\nNothing scanned was real audio hardware. Everything on "
                  "this Mac is software or\nits own microphone, and none of it "
                  "can carry timecode from outside.")
        for d in real:
            n = d["name"].lower()
            if "dante" in n or "avb" in n or "aes67" in n:
                print(f"\n{d['name']} is a network audio device, not a cable "
                      f"input. It only\ncarries what the network has been told "
                      f"to send it, so if its channels read 0.00\nthe "
                      f"subscription has not been made. Open Dante Controller "
                      f"and subscribe the\ntimecode transmitter's channel to "
                      f"this receiver, then run this again. Nothing in\nthis "
                      f"program can create that routing.")
                break
        return 1
    d, c = found[0]
    print(f"Timecode is on {d['name']}, input {c['channel']}.")
    if c["level"] < 0.05:
        print(f"The level is low ({c['level']:.2f}). It decodes, but turn the "
              f"trim up if you can.")
    elif c["level"] > 0.9:
        print(f"The level is hot ({c['level']:.2f}) and may clip. Turn the trim "
              f"down.")
    print("\nPut this in the timeline:\n")
    print('    "input": {')
    print(f'      "device": "{d["name"]}",')
    print(f'      "channel": {c["channel"]}')
    print("    },")
    print(f"\nOr set it once and stop looking:\n"
          f"    ./ltc input --device \"{d['name']}\" --channel {c['channel']}")
    if len(found) > 1:
        print(f"\nNote: {len(found)} inputs carry timecode. The first is used "
              f"above; pick deliberately if they are different sources.")
    return 0


def _meter(level, width=10):
    on = int(round(min(1.0, max(0.0, level)) * width))
    return "[" + "=" * on + " " * (width - on) + "]"


class _WavSource:
    """Feeds a WAV file in real time. Lets you rehearse without the rig."""

    def __init__(self, path, block=BLOCK):
        import wave
        self.w = wave.open(path, "rb")
        self.rate = self.w.getframerate()
        self.channels = self.w.getnchannels()
        self.width = self.w.getsampwidth()
        if self.width not in (2, 3, 4):
            raise ValueError(f"{path}: {self.width*8}-bit WAV not supported")
        self.block = block

    def blocks(self):
        import struct
        scale = float(1 << (self.width * 8 - 1))
        t0 = time.monotonic()
        sent = 0
        while True:
            raw = self.w.readframes(self.block)
            if not raw:
                return
            n = len(raw) // (self.width * self.channels)
            out = []
            for i in range(n):
                off = i * self.width * self.channels
                b = raw[off:off + self.width]
                if self.width == 2:
                    v = struct.unpack_from("<h", b)[0]
                elif self.width == 4:
                    v = struct.unpack_from("<i", b)[0]
                else:
                    v = int.from_bytes(b, "little", signed=True)
                out.append(v / scale)
            sent += n
            due = t0 + sent / self.rate
            slp = due - time.monotonic()
            if slp > 0:
                time.sleep(slp)
            yield out, time.monotonic()


# ----------------------------------------------------------------- init ---
def cmd_init(args):
    doc = timeline_mod.starter(args.show_dir, fps=args.fps, start=args.start,
                               gap_seconds=args.gap)
    if not doc["cues"]:
        return _err(f"no .fseq files found in {args.show_dir}")
    # Beside the launcher, not inside the show folder. The Run menu and the
    # web page only look beside the launcher, so a show file written into the
    # render folder was invisible to both and the operator had no way to
    # reach it. Round 3 of the audit, 2026-09-13.
    if args.out:
        out = args.out
    else:
        name = os.path.basename(os.path.normpath(args.show_dir)) or "show"
        safe = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in name)
        out = os.path.join(settings_mod.folder(), f"{safe}_timeline.json")
        n = 2
        while os.path.exists(out):
            out = os.path.join(settings_mod.folder(),
                               f"{safe}_timeline_{n}.json")
            n += 1
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2)
    print(f"Wrote {out} with {len(doc['cues'])} cues at {args.fps} fps.")
    print("It is beside the launcher, so it appears in the Run menu and on "
          "the web page.")
    print("Edit the 'tc' values to your real running order before using it.")
    return 0


def cmd_showdir(args):
    """Read or change the folder a show file plays its sequences from."""
    tl_path = os.path.abspath(args.timeline)
    with open(tl_path) as fh:
        doc = json.load(fh)
    if not args.folder:
        print(doc.get("show_dir") or "(beside the show file)")
        return 0
    ok, why, counts = check_show_folder(args.folder)
    print(f"{counts['fseq']} sequences, "
          f"{'a' if counts['networks'] else 'NO'} controller map")
    if not ok and not (args.anyway and counts["fseq"]):
        return _err(why)
    if not ok:
        print(f"note: {why}", file=sys.stderr)
    doc["show_dir"] = os.path.abspath(os.path.expanduser(args.folder))
    write_json(tl_path, doc)
    print(f"{os.path.basename(tl_path)} now plays from {doc['show_dir']}")
    print("Run `verify` before the show: the cue names in this file have not "
          "changed, and the new folder may not hold the same renders.")
    return 0


def write_json(path, doc):
    """Replace a JSON file in one step, or not at all.

    A show file half-written by a crash mid-dump loads as nothing at all,
    and the folder picker writes this file from the web page while the
    operator is standing at the rig.
    """
    tmp = path + ".new"
    try:
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def check_show_folder(path):
    """Is this a folder ltcplay can play a show out of?

    Returns (ok, why, counts). A folder with no .fseq files is not a show
    folder however much it looks like one, and a folder with no
    xlights_networks.xml has no channel map, so nothing could be addressed.
    """
    folder = os.path.abspath(os.path.expanduser(path or ""))
    counts = {"fseq": 0, "networks": False}
    if not os.path.isdir(folder):
        return False, f"{folder} is not a folder on this Mac.", counts
    try:
        names = os.listdir(folder)
    except OSError as e:
        return False, f"{folder} cannot be read: {e.strerror or e}", counts
    counts["fseq"] = sum(1 for n in names if n.lower().endswith(".fseq"))
    counts["networks"] = "xlights_networks.xml" in names
    if not counts["fseq"]:
        return False, (f"{folder} holds no .fseq files, so there is nothing "
                       f"to play from it."), counts
    if not counts["networks"]:
        return False, (f"{folder} has no xlights_networks.xml, so there is no "
                       f"channel map and nothing could be addressed. Export "
                       f"it from xLights into this folder."), counts
    return True, "", counts


# ---------------------------------------------------------------- check ---
def _load_all(args):
    tl = timeline_mod.Timeline.load(args.timeline,
                                    fps=getattr(args, "fps", None),
                                    drop=getattr(args, "drop", None))
    if tl.show_dir_note:
        print(f"note: {tl.show_dir_note}", file=sys.stderr)
    nm_path = args.networks or os.path.join(tl.show_dir, "xlights_networks.xml")
    if not os.path.exists(nm_path):
        raise ValueError(
            f"Could not find the controller map:\n  {nm_path}\n"
            f"That is xlights_networks.xml, and it should sit in the show "
            f"folder beside the .fseq files. Either \"show_dir\" in the "
            f"timeline points somewhere else, or pass --networks with the "
            f"real path.")
    nm = netmap_mod.load(nm_path)
    return tl, nm, nm_path


def cmd_check(args):
    tl, nm, nm_path = _load_all(args)
    print(f"Timeline : {args.timeline}")
    print(f"           {len(tl.cues)} cues at {tl.rate_label} fps")
    print(f"Network  : {nm_path}")
    print(f"           {nm.summary()}")
    for name, why, span in nm.skipped:
        print(f"           skipped {name}: {why} ({span} channels reserved)")
    sender = _NullSender(nm)
    p = Player(tl, nm, sender)
    problems = p.open_cues()
    print()
    prev_end = None
    for cue in tl.cues:
        if cue.fseq is None:
            print(f"  {cue.tc_text}  {cue.name}  MISSING")
            continue
        end = cue.tc_seconds + cue.duration
        overlap = ""
        if prev_end is not None and cue.tc_seconds < prev_end - 0.001:
            # A cue that starts on the frame the last one ends is rounding, not
            # a mistake: the later cue wins and the earlier one loses its final
            # frame, which nobody can see. Flagging those trains an operator to
            # ignore the flag, so only a real overlap gets one.
            over = prev_end - cue.tc_seconds
            if over > 2 * (cue.fseq.step_time_ms / 1000.0):
                overlap = f"  <-- OVERLAPS THE CUE BEFORE IT BY {over:.2f}s"
        prev_end = end
        print(f"  {cue.tc_text}  {cue.name}  "
              f"{cue.duration:6.1f}s  ends {timeline_mod.format_tc(end, tl.fps)}"
              f"  {cue.fseq.channel_count}ch@{cue.fseq.step_time_ms}ms{overlap}")
        print(f"               -> {os.path.basename(cue.path)}")
    if tl.idle_fseq:
        print(f"\n  preshow loop: {os.path.basename(tl.idle_fseq)}"
              + ("" if os.path.exists(tl.idle_fseq) else "   MISSING"))
    if tl.gaps:
        print(f"  between cues: {tl.gaps}")

    if not args.no_audio:
        problems += _check_audio()
    if not args.no_ping:
        problems += _check_reachable(nm)

    if problems:
        print("\nProblems:")
        for p_ in problems:
            print(f"  - {p_}")
        return 1
    print("\nAll cues load, audio input present, every controller answers.")
    return 0


def _check_audio():
    """An input that does not exist is the commonest reason nothing decodes."""
    try:
        import sounddevice as sd
    except Exception as e:
        return [f"sounddevice will not load, so no timecode can be read: {e}"]
    try:
        ins = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception as e:
        return [f"could not list audio devices: {e}"]
    if not ins:
        return ["no audio input device at all. On a MacBook the headphone jack "
                "only becomes an input while a TRRS adapter is plugged in."]
    print(f"\n  audio inputs: " + ", ".join(d["name"] for d in ins[:4])
          + (" ..." if len(ins) > 4 else ""))
    return []


def _check_reachable(nm, timeout=1.0):
    """Ping every controller once, in parallel.

    ArtNet and E1.31 are fire and forget, so nothing downstream ever tells you
    a controller is off. One ping before the run does."""
    import concurrent.futures
    import subprocess
    ips = sorted({u.ip for u in nm.universes})
    if not ips:
        return []

    def ping(ip):
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1000", "-t", "1", ip],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=timeout + 1.5)
            return ip, r.returncode == 0
        except Exception:
            return ip, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(ping, ips))
    dead = [ip for ip, ok in results if not ok]
    print(f"  controllers : {len(ips) - len(dead)} of {len(ips)} answer a ping")
    if dead:
        return [f"{len(dead)} controller(s) did not answer: "
                + ", ".join(dead[:8]) + (" ..." if len(dead) > 8 else "")
                + ". They may be off, or on another subnet, or simply not "
                  "answer ping. Output is one way, so this is a hint, not proof."]
    return []


class _NullSender:
    def __init__(self, nm):
        self.universe_count = len(nm.universes)
        self.packets_sent = 0
        self.send_errors = 0

    def send_frame(self, channels):
        self.packets_sent += self.universe_count

    def blackout(self):
        pass

    def close(self):
        pass


# ------------------------------------------------------------------ run ---
def _ltc_seconds(fr, tl):
    """Incoming frame digits to seconds, in the timeline's own frame of
    reference.  Deliberately uses the timeline's drop flag rather than the
    source's, so the chase stays self consistent; a disagreement between the
    two is reported as a warning on screen instead of leaking in here as a two
    frame jump at every minute boundary."""
    return tc_to_frames(fr.h, fr.m, fr.s, fr.f, tl.count, tl.drop) / tl.fps


def cmd_run(args):
    from .session import Session, SessionError
    try:
        sess = Session(
            args.timeline, no_output=args.no_output, wav=args.wav,
            bind=args.bind, networks=args.networks, log_path=args.log,
            no_log=args.no_log, idle=args.idle, gaps=args.gaps,
            on_lost=args.on_lost, offset_ms=args.offset_ms,
            jump_threshold=args.jump_threshold, freewheel_ms=args.freewheel_ms,
            hold_ms=args.hold_ms, on_end=args.on_end, fps=args.fps,
            drop=args.drop, device=args.device, channel=args.channel,
            rate=args.rate, echo_log=args.quiet,
            allow_missing=args.allow_missing,
            auto_reload=(settings_mod.load_prefs()["auto_reload"]
                         if args.auto_reload is None else args.auto_reload)).open()
    except SessionError as e:
        return _err(str(e))

    for n in sess.notes:
        print(f"note: {n}", file=sys.stderr)
    for pr in sess.problems:
        print(f"warning: {pr}", file=sys.stderr)

    if getattr(args, "go", None):
        # A terminal operator had no GO at all: on a dead-line night the only
        # route was Ctrl-C (a blackout in front of the audience), then the web
        # page. Round 3 of the audit, 2026-09-13.
        try:
            _go_at = sess.tl.parse(args.go)
        except ValueError:
            match = [c for c in sess.tl.cues
                     if c.name.lower() == args.go.strip().lower()]
            if not match:
                return _err(f"--go {args.go!r} is not a timecode or the name "
                            f"of a cue in this show.")
            _go_at = match[0].tc_seconds
        sess.player.go(_go_at)
    if getattr(args, "preshow", False):
        if sess.player.idle_cue is None:
            return _err("--preshow needs a preshow sequence: set \"idle\" in "
                        "the show file to a .fseq in the show folder.")
        sess.player.override = "preshow"
    try:
        sess.start()
    except SessionError as e:
        return _err(str(e))
    except Exception as e:
        # The audio layer raises its own types. An operator at a console does
        # not deserve a traceback for "the interface is busy".
        try:
            sess.stop()
        except Exception:
            pass
        return _err(f"{e}")
    print(sess.banner)
    if sess.player.override == "preshow":
        print("        HELD ON THE PRESHOW LOOK. Timecode is being read but "
              "is not driving the rig.")
    if sess.player.freerun_epoch is not None:
        print("        FREE RUNNING from " + sess.tl.format(_go_at) +
              " on this Mac's own clock. The timecode feed is being read but "
              "is not driving the rig.")
    print("        when you re-render in xLights: "
          + ("it loads by itself" if sess.player.auto_reload
             else "nothing loads until you restart or press Reload on the "
                  "web page"))
    print(f"        timecode lost -> {sess.player.on_lost}; between cues -> "
          f"{sess.player.gaps}; a paused source holds its frame")
    print(f"        input: {sess.input_summary}")
    if not args.wav and not (sess.audio and sess.audio.attached):
        print("        THE TIMECODE INPUT IS NOT OPEN. The show is running "
              "and the rig is")
        print("        holding the preshow look. The input is retried every "
              "second and")
        print("        picked up the moment it appears; nothing needs "
              "restarting.")
        if sess.input_error:
            print(f"        {sess.input_error.splitlines()[0]}")
    if not args.wav:
        print("Waiting for LTC. macOS will ask for microphone access the "
              "first time.")

    _hold_the_mac_awake()
    p, dec, tl = sess.player, sess.dec, sess.tl
    # Anything said before the first paint is gone a tenth of a second later:
    # `paint` homes the cursor and clears to the end of the screen. Every
    # start-up note and preflight problem used to be printed and then wiped,
    # which is how a preshow that would not load became invisible. Hand them
    # to the display so they stay on screen.
    sc = display_mod.Screen(colour=sys.stdout.isatty() and not args.no_colour,
                            cols=_term_cols())
    sc.standing = list(sess.notes) + list(sess.problems)
    started = sess.started_at
    last_beat = [started]

    def tick_ui():
        if args.quiet:
            print(display_mod.one_line(p, dec, tl), flush=True)
        else:
            sc.cols = _term_cols()
            display_mod.paint(display_mod.render(p, dec, tl, sc, started),
                              sys.stdout)
        now = time.monotonic()
        if sess.log and now - last_beat[0] >= 60.0:
            last_beat[0] = now
            sess.log.event("heartbeat",
                           f"{p.state} {p.source} frames={p.frames_sent} "
                           f"ltc={p.ltc_frames_in} jumps={p.jumps} "
                           f"send_err={getattr(sess.sender,'send_errors',0)} "
                           f"reopens={getattr(sess.sender,'reopens',0)} "
                           f"loop_err={p.loop_errors} "
                           f"restarts={p.thread_restarts}")

    stopping = _install_signals()
    try:
        if args.wav:
            last_draw = [0.0]

            def on_block():
                now = time.monotonic()
                if now - last_draw[0] > 0.1:
                    last_draw[0] = now
                    tick_ui()
            sess.pump_wav(stop_check=stopping, on_block=on_block)
        else:
            while not stopping():
                time.sleep(0.1)
                tick_ui()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.quiet and sys.stdout.isatty():
            sys.stdout.write("\033[?25h")
        sess.stop()
        print()
        _say_blackout(sess)
    return 0


def cmd_bundle(args):
    """Make a self-contained show folder that can be carried to another Mac.

    Two problems, one command. A show that lives in a Dropbox folder is being
    re-synced under the player all night; and a second machine needs the code,
    the launchers, the show file, the controller map AND the renders, which
    are the one thing nobody remembers to copy.

    So: copy everything into one folder, rewrite the show file to point at
    itself, and record a hash of every render. `ltcplay verify` on the other
    Mac then proves the copy is byte-for-byte the show you tested."""
    import hashlib
    import shutil
    from . import brand as brand_mod
    src_tl = os.path.abspath(args.timeline)
    tl = timeline_mod.Timeline.load(src_tl)
    out = os.path.abspath(args.out)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Bundling into the folder you are running from deletes the program while
    # it is using it: the first thing this does is rmtree the destination's
    # ltcplay package. Round 2 of the audit did exactly that by following the
    # "pass --force" advice in the message below.
    if os.path.normcase(out) == os.path.normcase(here) or \
            os.path.normcase(here).startswith(os.path.normcase(out) + os.sep):
        return _err(f"{out} is where this program is installed. Bundling into "
                    f"it would delete the copy that is running. Pick a folder "
                    f"somewhere else, for example ~/Desktop/GPL_Show.")
    if os.path.exists(out) and os.listdir(out):
        if not args.force:
            return _err(f"{out} already has something in it. Pick an empty "
                        f"folder, or pass --force to write into it anyway.")
    shows = os.path.join(out, "show")
    os.makedirs(shows, exist_ok=True)

    # 1. the program
    pkg = os.path.join(out, "ltcplay")
    if os.path.isdir(pkg):
        # Only ever replace a folder that IS an ltcplay package. --force used
        # to rmtree anything called `ltcplay` in the destination, including
        # somebody's notes folder. Round 3 of the audit, 2026-09-13.
        if not os.path.exists(os.path.join(pkg, "player.py")):
            return _err(f"{pkg} already exists and is not an ltcplay "
                        f"package, so this would delete it. Pick another "
                        f"folder.")
        shutil.rmtree(pkg)
    shutil.copytree(os.path.join(here, "ltcplay"), pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("Install ltcplay.command", "Run ltcplay.command",
              "Web ltcplay.command", "Autostart ltcplay.command",
              "Move somewhere macOS allows.command",
              "Find every copy.command",
              "Restart ltcplay.command",
              "Build the login app.command",
              "README.md", "OPERATOR.md",
              "selftest.py", brand_mod.FILENAME):
        p_ = os.path.join(here, f)
        if os.path.exists(p_):
            shutil.copy2(p_, os.path.join(out, f))
            if f.endswith(".command"):
                os.chmod(os.path.join(out, f), 0o755)

    # 2. the show: every file the timeline names, plus the maps beside it
    wanted = {}
    for c in tl.cues:
        wanted[os.path.basename(c.path)] = c.path
    if tl.idle_fseq:
        wanted[os.path.basename(tl.idle_fseq)] = tl.idle_fseq
    for extra in ("xlights_networks.xml", "xlights_rgbeffects.xml"):
        p_ = os.path.join(tl.show_dir, extra)
        if os.path.exists(p_):
            wanted[extra] = p_

    manifest, missing, total = {}, [], 0
    for name, p_ in sorted(wanted.items()):
        if not os.path.exists(p_):
            missing.append(name)
            continue
        dst = os.path.join(shows, name)
        shutil.copy2(p_, dst)
        h = hashlib.sha256()
        with open(dst, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        size = os.path.getsize(dst)
        total += size
        manifest[name] = {"sha256": h.hexdigest(), "bytes": size}
        print(f"  {size/1e6:8.1f} MB  {name}")
    if missing:
        for m in missing:
            print(f"  MISSING    {m}", file=sys.stderr)
        if not args.force:
            return _err(f"{len(missing)} file(s) the show names are not there, "
                        f"so this bundle would not play. Fix them, or pass "
                        f"--force to build an incomplete bundle deliberately.")

    # 3. the show file, pointing at its own copy
    doc = json.load(open(src_tl))
    doc["show_dir"] = "show"
    # And every path inside it. An absolute fseq or idle path points at the
    # machine that made the bundle: on the other Mac the bundle reported the
    # very files it had just copied as MISSING. Round 2, 2026-09-13.
    for c in doc.get("cues", []):
        if isinstance(c.get("fseq"), str):
            c["fseq"] = os.path.basename(c["fseq"])
    for key in ("idle", "preshow", "idle_fseq"):
        v = doc.get(key)
        if isinstance(v, str) and v:
            doc[key] = os.path.basename(v)
        elif isinstance(v, dict) and isinstance(v.get("fseq"), str):
            v["fseq"] = os.path.basename(v["fseq"])
    json.dump(doc, open(os.path.join(out, os.path.basename(src_tl)), "w"),
              indent=2)

    json.dump({"made": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "from": src_tl,
               "by": brand_mod.contact_line(),
               "files": manifest},
              open(os.path.join(out, "ltcplay_bundle.json"), "w"), indent=2)

    print(f"\n{len(manifest)} files, {total/1e6:.0f} MB, in {out}")
    print("\nOn the other Mac: copy that whole folder anywhere, double-click")
    print("'Install ltcplay.command' once, then 'Web ltcplay.command'.")
    print("If a .command file opens in TextEdit instead of running, a copy")
    print("over the network dropped its permission. In Terminal, once:")
    print(f"    chmod +x '<that folder>'/*.command")
    if args.zip:
        base = out.rstrip(os.sep)
        made = shutil.make_archive(base, "zip", out)
        print(f"\nZipped: {made} ({os.path.getsize(made)/1e6:.0f} MB)")
    return 0


def cmd_serve(args):
    """Run the engine and serve the page onto it.

    The page is a window. The engine lives in this process, so closing the
    browser, sleeping the iPad you were watching from, or losing wifi does not
    touch a running show."""
    import webbrowser
    from . import web as web_mod
    _hold_the_mac_awake()
    folder = os.path.abspath(args.folder or settings_mod.folder())
    try:
        httpd = web_mod.serve(folder, port=args.port, bind=args.bind,
                              token=args.token)
    except OSError as e:
        return _err(f"Could not listen on {args.bind}:{args.port}: {e}\n"
                    f"Something else may already be using that port. Try "
                    f"--port 7879.")
    token = httpd.token
    host = "127.0.0.1" if args.bind in web_mod.LOOPBACK else args.bind
    if host in ("0.0.0.0", "::"):
        # "serve on every interface" is not an address anybody can open.
        # Print the one the phone has to type. Round 3, 2026-09-13.
        host = _lan_address() or host
    url = f"http://{host}:{args.port}/" + (f"?t={token}" if token else "")
    from . import brand as brand_mod
    _b = brand_mod.load()
    print(f"{_b['product']}  {brand_mod.contact_line(_b)}\n")
    print(f"ltcplay is running. Open this page:\n\n    {url}\n")
    print(f"Show files: {folder}")
    if token:
        print(f"\nServing on the network, so the page needs the token in that "
              f"link.\nAnyone who can reach {host}:{args.port} and has it can "
              f"black out the rig.")
    print("\nLeave this window open. It is the engine; the page is only a "
          "window onto it,\nso closing the browser does not stop a running "
          "show. Ctrl-C here does.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    # A closed Terminal window is a SIGHUP and a `kill` is a SIGTERM, and
    # neither used to reach this. The rig was left holding whatever frame was
    # last sent, while the page cheerfully said the show was still running.
    # Found by an adversarial audit, 2026-09-13.
    stopping = _install_signals()

    def _watch():
        while not stopping():
            time.sleep(0.2)
        httpd.shutdown()

    threading.Thread(target=_watch, daemon=True,
                     name="ltcplay-signal").start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nstopping")
        sess = httpd.control.session
        httpd.control.stop()
        httpd.server_close()
        _say_blackout(sess)
    return 0


def _lan_address():
    """This Mac's address on the venue network, as a phone would type it."""
    import socket as _s
    try:
        k = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            k.connect(("192.0.2.1", 9))       # TEST-NET-1; nothing is sent
            return k.getsockname()[0]
        finally:
            k.close()
    except OSError:
        return None


def _hold_the_mac_awake():
    """Stop the Mac sleeping for as long as this process lives.

    Done here rather than only in the launchers so it covers every way the
    engine gets started, including the login agent -- where wrapping the
    program in `caffeinate` would have made caffeinate the job's main process
    and left the engine orphaned, holding the port and the rig, when launchctl
    stopped it. Round 2 of the audit, 2026-09-13.
    """
    if sys.platform != "darwin":
        return None
    import shutil
    import subprocess
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        return subprocess.Popen([exe, "-dims", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _say_blackout(sess):
    """Say what actually happened to the rig, not what we hoped.

    "stopped, outputs blacked out" used to print whether or not a single
    packet left the machine. With the socket down, or bound to an address
    that had gone away, the controllers kept the last lit frame and the
    operator walked away believing the rig was dark."""
    if sess is None:
        print("stopped. Nothing was running, so nothing was sent.")
        return
    if getattr(sess, "no_output", False):
        print("stopped. This run was display only, so the rig was never "
              "receiving from it.")
    elif getattr(sess, "blackout_sent", False):
        print("stopped, blackout sent to every universe.")
    else:
        print("STOPPED, BUT THE BLACKOUT DID NOT GO OUT. The network was "
              "not reachable, so the rig is holding its last frame. Black "
              "it out from the console or power-cycle the controllers.")
    if getattr(sess, "log", None):
        print(f"Log: {sess.log.path}")


def _term_cols():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _install_signals():
    """Return a predicate that goes true on ctrl-c or a kill.

    A show tool that leaves the rig lit when it exits is worse than one that
    crashes, so both signals route to the same clean stop as ctrl-c."""
    import signal
    flag = {"stop": False}

    def handler(signum, frame):
        flag["stop"] = True

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass
    return lambda: flag["stop"]


def cmd_monitor(args):
    """Show incoming LTC and nothing else. Use this to prove the feed first.

    This is also where you settle the frame rate. The digits cannot tell you
    whether a source is 29.97 or 30, so the rate is measured against the sound
    card's sample clock and the exact line to put in the timeline is printed
    back to you. Do that here, before the show, not on the run screen."""
    from .tc import rate_label
    lvl = audio_mod.Level()
    if args.wav:
        src = _WavSource(args.wav)
        sr = src.rate
        stream = src.blocks()
        dev = None
    else:
        sd = _import_sounddevice()
        saved = settings_mod.load()
        try:
            dev = audio_mod.resolve_device(sd, args.device or saved.get("device"))
            channel = int(args.channel or saved.get("channel") or 1)
            if channel > dev["channels"]:
                return _err(f"{dev['name']} has {dev['channels']} input(s), "
                            f"so there is no input {channel}.")
            sr = audio_mod.negotiate_rate(sd, dev, args.rate or saved.get("rate"),
                                          channels=channel)
        except audio_mod.DeviceError as e:
            return _err(str(e))
        stream = None
    dec = LTCDecoder(sr)
    last = [None, 0]
    if dev:
        print(f"monitoring {dev['name']}, in {channel} of {dev['channels']}, "
              f"{sr}Hz. ctrl-c to stop")
        print("If nothing appears, try `ltcplay find`: it listens to every "
              "input at once.")
    else:
        print(f"monitoring at {sr}Hz, ctrl-c to stop")

    tty = sys.stdout.isatty()
    last_print = [0.0]
    settled = [None]
    repeat = [0]

    def report(frames):
        for fr in frames:
            if last[0] is not None and str(fr) == str(last[0]):
                repeat[0] += 1
            else:
                repeat[0] = 0
            last[0] = fr
            last[1] += 1
        if not last[0]:
            return
        now = time.monotonic()
        if not tty and now - last_print[0] < 1.0:
            return
        last_print[0] = now
        rate, drop, confident = dec.detected_rate
        if rate is None:
            r = "rate not known yet"
        else:
            r = rate_label(rate, drop)
            if dec.measured_fps:
                r += f" (measured {dec.measured_fps:.3f})"
        park = "  PARKED, same frame repeating" if repeat[0] > 5 else ""
        line = (f"  {last[0]}   {_meter(lvl.hold)} {lvl.hold:.2f}   "
                f"{last[1]} frames   {r}   "
                f"{dec.sync_errors} sync errors{park}")
        sys.stdout.write(("\r" + line + "   ") if tty else (line + "\n"))
        sys.stdout.flush()
        if confident and settled[0] is None:
            settled[0] = (rate, drop)

    src_obj = None
    try:
        if stream is not None:
            for samples, t in stream:
                lvl.feed(samples)
                report(dec.feed(samples))
        else:
            src_obj = audio_mod.InputSource(
                sd, dev, channel, sr, BLOCK,
                lambda block, t: report(dec.feed(block)))
            src_obj.start()
            lvl = src_obj.level
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if src_obj is not None:
            src_obj.stop()
    print()
    if settled[0]:
        rate, drop = settled[0]
        print(f"\nThis source is {rate_label(rate, drop)}. Put these two lines "
              f"in the timeline:\n")
        print(f'    "fps": {rate:g},')
        print(f'    "drop": {"true" if drop else "false"},')
        print("\nOr run once with them: "
              f"--fps {rate:g}{' --drop' if drop else ''}")
    elif last[1]:
        print("\nNot enough clean timecode to settle the frame rate. It needs "
              "a few seconds of continuous signal crossing a second boundary.")
    elif lvl.hold < 0.02:
        print(f"\nNothing arrived on this input at all (level {lvl.hold:.2f}). "
              f"Either the\ncable is in the wrong socket, or timecode is on a "
              f"different input of this\ninterface. `ltcplay find` listens to "
              f"every input at once and says which.")
    else:
        print(f"\nAudio is arriving (level {lvl.hold:.2f}, {lvl.verdict()}) "
              f"but none of it decodes as\ntimecode. Either this input carries "
              f"something else, or the level is wrong.\n`ltcplay find` will "
              f"say which input has the timecode on it.")
    return 0


def cmd_markers(args):
    """Build a timeline from a DAW marker export."""
    from . import markers as markers_mod
    doc, unprogrammed, notes, marks = markers_mod.build(
        args.csv, args.show_dir, fps=args.fps, start=args.start)
    out = args.out or os.path.splitext(args.csv)[0] + "_timeline.json"
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2)
    print(f"Wrote {out}")
    print(f"  {len(marks)} markers, {len(doc['cues'])} of them have a sequence\n")
    for c in doc["cues"]:
        print(f"  {c['tc']}  {c['name']:32s} {c['fseq']}")
    if unprogrammed:
        print("\n  Markers with no sequence (not programmed yet, or handled elsewhere):")
        for name, t in unprogrammed:
            print(f"    {timeline_mod.format_tc(t + timeline_mod.parse_tc(args.start, args.fps), args.fps)}  {name}")
    if notes:
        print("\n  Notes:")
        for n in notes:
            print(f"    - {n}")
    return 0


def cmd_retime(args):
    """Recompute every cue timecode back to back, in the order written."""
    with open(args.timeline) as fh:
        doc = json.load(fh)
    fps = int(doc.get("fps", 30))
    show_dir = doc.get("show_dir") or os.path.dirname(os.path.abspath(args.timeline))
    from .fseq import FSEQ
    t = timeline_mod.parse_tc(args.start or doc["cues"][0]["tc"], fps)
    for c in doc["cues"]:
        p = c["fseq"] if os.path.isabs(c["fseq"]) else os.path.join(show_dir, c["fseq"])
        with FSEQ(p) as f:
            dur = f.duration_ms / 1000.0
        c["tc"] = timeline_mod.format_tc(t, fps)
        print(f"  {c['tc']}  {os.path.basename(p):45s} {dur:7.1f}s")
        t += dur + args.gap
    with open(args.timeline, "w") as fh:
        json.dump(doc, fh, indent=2)
    print(f"\nRewrote {args.timeline}: {len(doc['cues'])} cues, "
          f"ends {timeline_mod.format_tc(t, fps)}")
    return 0


def cmd_gen(args):
    """Write an LTC WAV. Play it into the input, or use it with --wav."""
    import wave, struct
    from .ltc import synthesize
    from .tc import normalize_rate, count_for
    fps = normalize_rate(args.fps)
    h, m, sec, f = (int(x) for x in args.start.replace(";", ":").split(":"))
    total = int(round(args.seconds * fps))
    ifps = count_for(fps)
    drop = bool(args.drop)
    if drop and ifps != 30:
        return _err("drop frame only exists at 29.97 or 30")
    # Written in chunks: a 20 minute file held as one Python list is gigabytes.
    chunk = ifps * 30
    w = wave.open(args.out, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(args.rate)
    n0 = tc_to_frames(h, m, sec, f, ifps, drop)
    level = 1.0
    done = 0
    while done < total:
        k = min(chunk, total - done)
        n = n0 + done
        from .tc import frames_to_tc
        hh, mm, ss, ff = frames_to_tc(n, ifps, drop)
        au, level = synthesize(hh, mm, ss, ff, fps, args.rate,
                               frames=k, amplitude=args.level, drop=drop,
                               start_level=level, with_level=True)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
                               for v in au))
        done += k
    w.close()
    print(f"Wrote {args.out}: {args.seconds}s of {fps:g}fps "
          f"{'drop' if drop else 'non-drop'} LTC from {args.start} "
          f"at {args.rate}Hz, level {args.level}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ltcplay", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list audio inputs").set_defaults(func=cmd_devices)

    inp = sub.add_parser("input", help="choose the input once, and keep it")
    inp.add_argument("--device", help="set it without being asked")
    inp.add_argument("--channel", type=int)
    inp.add_argument("--rate", type=int)
    inp.add_argument("--clear", action="store_true",
                     help="forget it and use the system default")
    inp.add_argument("--no-check", action="store_true", dest="no_check")
    inp.set_defaults(func=cmd_input)

    vf = sub.add_parser("verify", help="prove every sequence is the right one")
    vf.add_argument("timeline")
    vf.add_argument("--networks")
    vf.add_argument("--no-manifest", action="store_true", dest="no_manifest",
                    help="do not read or write the fingerprint file")
    vf.set_defaults(func=cmd_verify)

    at = sub.add_parser("at", help="which file plays at a given timecode")
    at.add_argument("timeline")
    at.add_argument("timecode", help="for example 02:05:00:00")
    at.set_defaults(func=cmd_at)

    fi = sub.add_parser("find", help="find which input the timecode is on")
    fi.add_argument("--device", help="only look at this one")
    fi.add_argument("--seconds", type=float, default=3.0)
    fi.add_argument("--all", action="store_true",
                    help="also scan software and phone inputs")
    fi.set_defaults(func=cmd_find)

    i = sub.add_parser("init", help="write a starter timeline for a show folder")
    i.add_argument("show_dir")
    i.add_argument("--fps", default=30)
    i.add_argument("--start", default="01:00:00:00")
    i.add_argument("--gap", type=float, default=0.0)
    i.add_argument("--out")
    i.set_defaults(func=cmd_init)

    c = sub.add_parser("check", help="validate a timeline without sending anything")
    c.add_argument("timeline")
    c.add_argument("--networks")
    c.add_argument("--no-ping", action="store_true",
                   help="skip the controller reachability check")
    c.add_argument("--no-audio", action="store_true",
                   help="skip the audio input check")
    c.add_argument("--fps")
    c.add_argument("--drop", action="store_true", default=None)
    c.set_defaults(func=cmd_check)

    m = sub.add_parser("monitor", help="show incoming LTC only")
    m.add_argument("--device", help="input device name (stable) or index")
    m.add_argument("--channel", type=int)
    m.add_argument("--rate")
    m.add_argument("--wav")
    m.set_defaults(func=cmd_monitor)

    r = sub.add_parser("run", help="chase timecode and output")
    r.add_argument("timeline")
    r.add_argument("--networks")
    r.add_argument("--device", help="input device name (stable) or index")
    r.add_argument("--channel", type=int,
                   help="which input of that device carries timecode, 1-based")
    r.add_argument("--rate", help="sample rate; default is whatever the device "
                                  "will take")
    r.add_argument("--wav", help="read LTC from a WAV instead of the audio input")
    r.add_argument("--bind", help="local IP to send from")
    r.add_argument("--no-output", action="store_true",
                   help="decode and display but send no packets")
    r.add_argument("--offset-ms", type=float, default=0.0,
                   help="positive makes the rig run later; trims input latency")
    r.add_argument("--jump-threshold", type=float, default=0.15)
    r.add_argument("--freewheel-ms", type=int, default=250)
    r.add_argument("--hold-ms", type=int, default=2000)
    r.add_argument("--on-end", choices=("blackout", "hold"), default="blackout")
    r.add_argument("--quiet", action="store_true",
                   help="plain log lines, no redraw")
    r.add_argument("--no-colour", "--no-color", action="store_true",
                   dest="no_colour")
    r.add_argument("--idle", metavar="FSEQ",
                   help="sequence to loop whenever timecode is not running "
                        "(the preshow look); overrides the timeline's own")
    r.add_argument("--gaps", choices=("blackout", "idle", "hold"),
                   help="what fills the space between cues while timecode runs")
    r.add_argument("--on-lost",
                   choices=("preshow", "hold", "blackout", "freerun"),
                   dest="on_lost",
                   help="what the rig does when timecode disappears mid-cue: "
                        "freerun runs the set out on this Mac's clock from "
                        "where the feed died and keeps running even if the "
                        "feed returns, until it is handed back by hand; "
                        "hold freezes the frame it reached; preshow "
                        "returns to the preshow look (default); blackout "
                        "sends zeros")
    r.add_argument("--fps", help="override the timeline's frame rate for this "
                                 "run: 23.976, 24, 25, 29.97 or 30")
    r.add_argument("--drop", action="store_true", default=None,
                   help="override the timeline to drop frame for this run")
    r.add_argument("--log", help="show log file (default: ltcplay.log beside "
                                 "the timeline)")
    r.add_argument("--no-log", action="store_true")
    r.add_argument("--allow-missing", action="store_true",
                   help="run even though a cue will not open, leaving its "
                        "whole slot dark. Off by default: a render that is "
                        "half written is the usual cause and waiting a "
                        "minute is the usual fix")
    r.add_argument("--auto-reload", action="store_true", default=None,
                   help="load each re-render by itself as xLights finishes "
                        "writing it, without stopping the chase. For "
                        "rehearsal; leave it off on a show night. Remembers "
                        "whatever you last chose on the web page")
    r.add_argument("--go", metavar="TC|CUE",
                   help="start FREE RUNNING from this timecode or cue, on "
                        "this Mac's own clock, ignoring the timecode feed")
    r.add_argument("--preshow", action="store_true",
                   help="start held on the preshow look instead of following "
                        "timecode; press Back to timecode on the web page, "
                        "or restart, to follow it")
    r.set_defaults(func=cmd_run)

    mk = sub.add_parser("markers", help="build a timeline from a DAW marker CSV")
    mk.add_argument("csv")
    mk.add_argument("show_dir")
    mk.add_argument("--fps", default=30)
    mk.add_argument("--start", default="00:00:00:00",
                    help="timecode the soundtrack starts at")
    mk.add_argument("--out")
    mk.set_defaults(func=cmd_markers)

    rt = sub.add_parser("retime",
                        help="recompute cue times back to back, keeping file order")
    rt.add_argument("timeline")
    rt.add_argument("--start", help="default: the first cue's existing timecode")
    rt.add_argument("--gap", type=float, default=0.0, help="seconds between cues")
    rt.set_defaults(func=cmd_retime)

    sv = sub.add_parser("serve", help="run the engine and open the web page")
    sv.add_argument("--folder", help="where the show files are "
                                     "(default: this folder)")
    sv.add_argument("--port", type=int, default=7878)
    sv.add_argument("--bind", default="127.0.0.1",
                    help="0.0.0.0 to reach it from a phone or iPad on the "
                         "same network; a token is then required")
    sv.add_argument("--token", help="use this token instead of a generated one")
    sv.add_argument("--no-browser", action="store_true", dest="no_browser")
    sv.set_defaults(func=cmd_serve)

    sdp = sub.add_parser("showdir", help="see or change the folder a show "
                                        "file plays its sequences from")
    sdp.add_argument("timeline")
    sdp.add_argument("folder", nargs="?",
                     help="the new folder; leave it off to just see the "
                          "current one")
    sdp.add_argument("--anyway", action="store_true",
                     help="accept a folder with no xlights_networks.xml "
                          "in it; --networks must then be passed to every "
                          "command that plays this show")
    sdp.set_defaults(func=cmd_showdir)

    bd = sub.add_parser("bundle", help="make a self-contained copy of the "
                                       "show for another Mac")
    bd.add_argument("timeline")
    bd.add_argument("out", help="folder to build the bundle in")
    bd.add_argument("--zip", action="store_true",
                    help="also make a .zip beside it")
    bd.add_argument("--force", action="store_true",
                    help="write into a folder that is not empty, and build "
                         "even if a render the show names is missing")
    bd.set_defaults(func=cmd_bundle)

    g = sub.add_parser("gen", help="write a test LTC wav")
    g.add_argument("out")
    g.add_argument("--start", default="01:00:00:00")
    g.add_argument("--seconds", type=float, default=60.0)
    g.add_argument("--fps", default=30,
                   help="23.976, 24, 25, 29.97 or 30")
    g.add_argument("--drop", action="store_true",
                   help="set the drop frame flag (29.97 only)")
    g.add_argument("--rate", type=int, default=48000)
    g.add_argument("--level", type=float, default=0.4)
    g.set_defaults(func=cmd_gen)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as e:
        return _err(str(e))
    except PermissionError as e:
        return _err(f"{e.filename or 'that file'} cannot be written: "
                    f"{e.strerror}.\nThis folder is read-only, or belongs to "
                    f"another account. Copy the show somewhere you own and "
                    f"run it from there.")
    except OSError as e:
        # A stack trace at a console at 8pm helps nobody. Anything the OS
        # refuses gets a sentence with the file in it. Round 4, 2026-09-13.
        return _err(f"{e.strerror or e}"
                    + (f": {e.filename}" if getattr(e, "filename", None) else ""))


if __name__ == "__main__":
    sys.exit(main())
