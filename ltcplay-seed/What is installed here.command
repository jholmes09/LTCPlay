#!/bin/bash
# Double-click this on any Mac to find out exactly what is installed in this
# folder: which program, which renders, whether the app is current, and what
# the show file is set to. Nothing is changed.
cd "$(dirname "$0")" || exit 1
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done
PY=./.venv/bin/python
[ -x "$PY" ] || PY=/usr/bin/python3
"$PY" - <<'PYEOF'
import json, os, subprocess, sys, time
sys.path.insert(0, os.getcwd())
here = os.getcwd()
print()
print("  " + here)
print()
try:
    from ltcplay import version as v
    bid, n, newest = v.build()
    print("  PROGRAM   %s" % v.status())
    print("            %d files, newest %s"
          % (n, time.strftime("%Y-%m-%d %H:%M", time.localtime(newest))))
    rel = v.release()
    if rel and rel.get("notes"):
        print("            %s" % rel["notes"])
    if not rel:
        print("            no VERSION stamp here, so this copy came from")
        print("            somewhere other than a cut release")
except Exception as e:
    print("  PROGRAM   could not be read (%s)" % e)
    bid = None

# A show file is a json with cues in it. Picking the first json in the
# folder alphabetically found a superseded one and reported it as THE show,
# which is the exact class of mistake this script exists to prevent.
shows = []
for f in sorted(os.listdir(here)):
    if not f.endswith(".json"):
        continue
    try:
        doc = json.load(open(os.path.join(here, f)))
    except Exception:
        continue
    if isinstance(doc, dict) and doc.get("cues"):
        shows.append((os.path.join(here, f), doc))
if len(shows) > 1:
    print()
    print("  NOTE      this folder holds %d show files. All of them:"
          % len(shows))
for tl_path, doc in shows:
    show = doc.get("show_dir") or ""
    show_abs = show if os.path.isabs(show) else os.path.join(here, show)
    try:
        from ltcplay import version as v
        sid, sn = v.show_build(show_abs, tl_path)
        print()
        print("  SHOW      %s" % os.path.basename(tl_path))
        print("            %s (%d files)" % (v.show_status(show_abs, tl_path), sn))
    except Exception:
        pass
    fseq = 0
    if os.path.isdir(show_abs):
        fseq = len([f for f in os.listdir(show_abs) if f.endswith(".fseq")])
    print("            folder: %s%s"
          % (show, "  (a link to %s)" % os.readlink(show_abs)
             if os.path.islink(show_abs) else ""))
    print("            %d renders, %d cues" % (fseq, len(doc.get("cues", []))))
    print("            when timecode is lost: %s" % doc.get("on_lost"))
    t = doc.get("trigger")
    if t:
        print("            Advatek scene triggers: %d scenes on universe %s"
              % (len(t.get("channels", {})), t.get("universe")))
        print("            armed at startup: %s"
              % ("yes" if t.get("enabled") else "no, it is the backup"))
    else:
        print("            Advatek scene triggers: not in this show file")
if not shows:
    print()
    print("  SHOW      no show file in this folder")

print()
app = os.path.join(here, "LTC Player.app")
if not os.path.isdir(app):
    print("  APP       not built here yet")
    print("            double-click 'Build LTC Player app.command'")
else:
    exe = os.path.join(app, "Contents", "MacOS", "LTC Player")
    built = time.strftime("%Y-%m-%d %H:%M",
                          time.localtime(os.path.getmtime(exe)))
    print("  APP       built %s" % built)
    builder = os.path.join(here, "Build LTC Player app.command")
    if os.path.exists(builder) and \
            os.path.getmtime(builder) > os.path.getmtime(exe):
        print("            OUT OF DATE: the builder is newer than the app.")
        print("            Double-click 'Build LTC Player app.command'.")
    try:
        r = subprocess.run(["codesign", "--verify", "--deep", "--strict", app],
                           capture_output=True)
        print("            signature: %s"
              % ("valid" if r.returncode == 0 else "NOT VALID, rebuild it"))
    except FileNotFoundError:
        print("            signature: cannot check, codesign is not installed")

print()
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:7878/api/state",
                                timeout=2) as fh:
        st = json.load(fh)
    print("  RUNNING   yes, on port 7878")
    print("            it is running build %s" % st.get("build_id", "?"))
    if bid and st.get("build_id") and st["build_id"] != bid:
        print("            THIS IS NOT THE PROGRAM IN THIS FOLDER.")
        print("            Stop it and start it again to pick up the files")
        print("            that are here now.")
    print("            show state: %s, output %s"
          % (st.get("state"), "ON" if st.get("running") else "idle"))
    print("            when timecode is lost: %s" % st.get("on_lost"))
except Exception:
    print("  RUNNING   nothing is serving on port 7878")
print()
PYEOF
echo
read -r -p "Press return to close. "
