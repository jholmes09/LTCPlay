#!/bin/bash
# Writes the Advatek playback trigger settings into this install's show file,
# from the reference the boxes were configured from on 2026-09-15:
#
#   sACN (E1.31), universe 6999, fire 255, release 0
#   Set 1 Opener..Ending = 101..111,  Set 2 = 112..122,  PreShow = 123
#
# It asks how the trigger should reach the six controllers, checks the result
# against the real renders and the controller map, and changes nothing else
# in the show file.
set -u
cd "$(dirname "$0")" || exit 1
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

PY=./.venv/bin/python
[ -x "$PY" ] || PY=/usr/bin/python3

echo
echo "Advatek playback triggers: sACN universe 6999, channels 101 to 123"
echo
echo "  How should the trigger reach the six controllers?"
echo
echo "    1   Multicast.        The usual way. One packet. Needs the switch"
echo "                          to pass multicast to the boxes."
echo "    2   One per box.      Six packets, straight to the six addresses."
echo "                          Needs nothing from the network."
echo
read -r -p "  1 or 2 (return for 1): " HOW
case "${HOW:-1}" in
  2) MODE=unicast ;;
  *) MODE=multicast ;;
esac

TMP="$(mktemp "${TMPDIR:-/tmp}/settrig.XXXXXX")" || exit 1
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.getcwd())
mode = sys.argv[1]

ADVATEK = ["10.0.0.100", "10.0.0.110", "10.0.0.120",
           "10.0.0.121", "10.0.0.130", "10.0.0.131"]

here = os.getcwd()
shows = []
for f in sorted(os.listdir(here)):
    if not f.endswith(".json"):
        continue
    try:
        d = json.load(open(os.path.join(here, f)))
    except Exception:
        continue
    if isinstance(d, dict) and d.get("cues"):
        shows.append((os.path.join(here, f), d))
if not shows:
    sys.exit("  No show file in this folder. Nothing was changed.")
if len(shows) > 1:
    print("  This folder holds more than one show file:")
    for p, _ in shows:
        print("    ", os.path.basename(p))
    sys.exit("  Move the ones you are not using out of the way first. "
             "Nothing was changed.")

path, doc = shows[0]
cues = doc["cues"]
if len(cues) != 22:
    sys.exit("  The reference covers 22 cues and this show file has %d. "
             "Nothing was changed." % len(cues))

doc["trigger"] = {
    "enabled": False,
    "protocol": "sacn",
    "universe": 6999,
    "dest": "multicast" if mode == "multicast" else ADVATEK,
    "mute": ADVATEK,
    "channels": {c["fseq"]: 101 + i for i, c in enumerate(cues)},
    "idle_channel": 123,
    "pulse_frames": 3,
    "pulse_gap_ms": 50,
    "release": True,
}

# Prove it against the real renders and the real controller map BEFORE it is
# written. A show file that loads and is wrong is worse than one that refuses.
backup = path + ".before_triggers"
old = open(path, "rb").read()
open(backup, "wb").write(old)
json.dump(doc, open(path, "w"), indent=2)
try:
    from ltcplay import timeline as T, netmap as N
    tl = T.Timeline.load(path)
    nm = N.load(os.path.join(tl.show_dir, "xlights_networks.xml"))
    bad = tl.trigger.check_against(tl, nm)
except Exception as e:
    open(path, "wb").write(old)
    sys.exit("  It would not load: %s\n  Put back as it was. Nothing changed." % e)
if bad:
    open(path, "wb").write(old)
    print("  Written, then put back: the settings do not fit this show.")
    for b in bad:
        print("   -", b)
    sys.exit(1)

print()
print("  %s" % os.path.basename(path))
print("  the show file as it was is beside it, as %s"
      % os.path.basename(backup))
print("  %s" % tl.trigger.summary(nm))
print()
print("  %-5s %s" % ("CH", "CUE"))
for c in cues:
    print("  %-5d %s" % (doc["trigger"]["channels"][c["fseq"]], c["name"]))
print("  %-5d %s" % (123, "PreShow"))
print()
print("  Nothing else in the show file was touched. The mode is OFF; it is")
print("  armed from the show page, and only when you want it.")
PYEOF
"$PY" "$TMP" "$MODE"
rc=$?
echo
[ "$rc" = "0" ] || echo "  Nothing was changed."
read -r -p "Press return to close. "
