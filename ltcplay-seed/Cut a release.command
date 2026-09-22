#!/bin/bash
# Jeff only. Stamps this folder as a numbered release and writes the VERSION
# file that travels with it, so every machine can say what it has and whether
# anyone has edited it since.
#
# Run it in the folder you are about to package, AFTER the last code change
# and AFTER the renders are final. It changes nothing except VERSION.
set -u
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

PY=./.venv/bin/python
[ -x "$PY" ] || PY=/usr/bin/python3

# The worker goes in a file rather than down a heredoc: a heredoc IS stdin,
# so anything that tries to ask a question gets end-of-file instead.
TMP="$(mktemp "${TMPDIR:-/tmp}/cutrelease.XXXXXX")" || {
  echo; echo "Could not make a temporary file. Nothing was changed."; echo
  read -r -p "Press return to close. "; exit 1
}
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<'PYEOF'
import json, os, subprocess, sys, time
sys.path.insert(0, os.getcwd())
from ltcplay import version as v

here = os.getcwd()
today = time.strftime("%Y.%m.%d")
prev = v.release() or {}
n = 1
if str(prev.get("release", "")).startswith(today):
    try:
        n = int(str(prev["release"]).rsplit(".", 1)[1]) + 1
    except Exception:
        n = 2
name = "%s.%d" % (today, n)

tl = None
for f in sorted(os.listdir(here)):
    if not f.endswith(".json"):
        continue
    try:
        d = json.load(open(os.path.join(here, f)))
    except Exception:
        continue
    if isinstance(d, dict) and d.get("cues"):
        tl = os.path.join(here, f)
        break
show_dir = ""
if tl:
    sd = json.load(open(tl)).get("show_dir") or ""
    show_dir = sd if os.path.isabs(sd) else os.path.join(here, sd)

notes = " ".join(sys.argv[1:]).strip()
if not notes:
    # Double-clicked, so there is no command line to put it on. Ask in a way
    # a double-click can answer.
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'text returned of (display dialog "One line about this release, '
             'or leave it empty." with title "Cut a release" default answer "" '
             'buttons {"Cancel", "Cut it"} default button 2)'],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            print("\n  Cancelled. VERSION was not changed.\n")
            raise SystemExit(0)
        notes = r.stdout.strip()
    except FileNotFoundError:
        notes = ""

bid = v.build()[0]
sid = v.show_build(show_dir, tl)[0] if show_dir else ""
doc = {"release": name, "build": bid, "show": sid,
       "made": time.strftime("%Y-%m-%dT%H:%M:%S"), "notes": notes}
with open(os.path.join(here, v.STAMP), "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")

print()
print("  release  %s" % name)
print("  build    %s" % bid)
print("  show     %s" % (sid or "no show folder here"))
if notes:
    print("  notes    %s" % notes)
print()
print("  Written to VERSION. Every machine that gets this folder reports that")
print("  line, and says MODIFIED if anyone changes a file in it.")
print()
PYEOF
"$PY" "$TMP" "$@"
read -r -p "Press return to close. "
