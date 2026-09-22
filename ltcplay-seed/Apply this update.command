#!/bin/bash
# Double-click this from inside an update folder to put its program files
# into an ltcplay that is already installed and working.
#
# It copies ONLY the program: the ltcplay package, the launchers and the self
# test. It never touches .venv, your show file, the show folder, your saved
# input or your logs.
cd "$(dirname "$0")" || exit 1
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

LABEL="com.jeffholmespresents.ltcplay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=7878
SRC="$(pwd)"
# The files to install live in _payload so the pack has exactly one
# clickable thing in it. Running a tool from inside an update pack finds no
# show file and no environment, and says so in a way that reads like a
# fault. Show Mac, 2026-09-15.
[ -d "$SRC/_payload" ] && SRC="$SRC/_payload"
bye() { echo; echo "$1"; echo; read -r -p "Press return to close. "; exit "${2:-0}"; }

[ -f "$SRC/ltcplay/cli.py" ] || bye "This is not an update folder: no ltcplay
package sits beside this script." 1
[ -d "$SRC/show" ] && bye "This looks like a FULL bundle, not an update pack.
Use it as its own folder instead of copying it into another one." 1

echo "ltcplay: apply this update"
echo
echo "From: $SRC"
echo
echo "Looking for installs on this Mac."
echo

# An install is a folder with the package AND a built environment.
TARGETS=()
while IFS= read -r d; do
  [ -n "$d" ] || continue
  [ "$d" = "$SRC" ] && continue
  [ -x "$d/.venv/bin/python" ] || continue
  TARGETS+=("$d")
done < <(find "$HOME" -maxdepth 6 -name "cli.py" -path "*/ltcplay/cli.py" \
           -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null \
         | sed 's|/ltcplay/cli.py$||' | sort -u)

if [ ${#TARGETS[@]} -eq 0 ]; then
  bye "No installed copy of ltcplay found under $HOME.

An update pack cannot run a show on its own: it has no sequences. Use the
full GPL2026_LTCPlay folder instead, or run 'Install ltcplay.command' in
whichever folder you meant to update." 1
fi

if [ ${#TARGETS[@]} -eq 1 ]; then
  DEST="${TARGETS[0]}"
  echo "Found one install:"
  echo "    $DEST"
else
  echo "Found more than one install. Which one is the show?"
  echo
  i=0
  for d in "${TARGETS[@]}"; do
    i=$((i+1))
    seq=$(ls "$d/show" 2>/dev/null | grep -ci '\.fseq$')
    echo "  [$i] $d"
    echo "      $seq sequences"
  done
  echo
  printf "  number, or q to quit: "
  read -r pick
  case "$pick" in
    ''|*[!0-9]*) bye "Nothing was changed." ;;
  esac
  [ "$pick" -ge 1 ] 2>/dev/null && [ "$pick" -le ${#TARGETS[@]} ] || bye "Nothing was changed."
  DEST="${TARGETS[$((pick-1))]}"
fi
echo

if command -v curl >/dev/null 2>&1 && \
   curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
     | grep -q '"running": *true'; then
  bye "A SHOW IS RUNNING. Stop it on the page first. Nothing was changed." 1
fi

printf "Update it? [y/N] : "
read -r ans
case "$ans" in [Yy]*) ;; *) bye "Nothing was changed." ;; esac
echo

# Stop the engine first. New code in a folder a running process is reading is
# how you get half of one version and half of another.
AGENT_WAS_UP=""
if [ -f "$PLIST" ]; then
  echo "Stopping the login agent."
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null
  AGENT_WAS_UP=yes
  sleep 2
fi
if pgrep -f "ltcplay.cli|LTC Player.app/Contents/Resources/boot.py" >/dev/null 2>&1; then
  echo "Stopping a running ltcplay window."
  pkill -f "ltcplay.cli|LTC Player.app/Contents/Resources/boot.py" 2>/dev/null
  sleep 2
fi

echo "Copying the program."
# EVERYTHING that is about to change gets kept, not just the package. A
# rollback that restores the package and leaves the new launchers behind
# produces an install where the tools and the code disagree, which is worse
# than either version on its own. Show Mac, 2026-09-15.
BACKUP="$DEST/.ltcplay_rollback"
rm -rf "$BACKUP" "$DEST/ltcplay.previous" 2>/dev/null
mkdir -p "$BACKUP" || bye "Could not make room to keep the old copy, so
nothing was changed." 1
[ -d "$DEST/ltcplay" ] && cp -R "$DEST/ltcplay" "$BACKUP/ltcplay"
[ -d "$DEST/Tools" ] && cp -R "$DEST/Tools" "$BACKUP/Tools"
for f in "$DEST"/*.command "$DEST/selftest.py"; do
  [ -e "$f" ] && cp "$f" "$BACKUP/" 2>/dev/null
done
restore() {
  rm -rf "$DEST/ltcplay" "$DEST/Tools" 2>/dev/null
  [ -d "$BACKUP/ltcplay" ] && cp -R "$BACKUP/ltcplay" "$DEST/ltcplay"
  [ -d "$BACKUP/Tools" ] && cp -R "$BACKUP/Tools" "$DEST/Tools"
  for f in "$BACKUP"/*.command "$BACKUP/selftest.py"; do
    [ -e "$f" ] && cp "$f" "$DEST/" 2>/dev/null
  done
  chmod +x "$DEST"/*.command "$DEST"/Tools/*.command 2>/dev/null
}
cp -R "$DEST/ltcplay" "$DEST/ltcplay.previous" 2>/dev/null
if ! cp -R "$SRC/ltcplay/." "$DEST/ltcplay/"; then
  echo "The copy failed. Putting the old install back."
  restore
  bye "Nothing was changed." 1
fi
find "$DEST/ltcplay" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
cp "$SRC"/*.command "$DEST"/ 2>/dev/null
# The rarely-used launchers live in Tools/ so the folder a show operator
# opens has four things in it instead of twelve. Keep them there.
if [ -d "$SRC/Tools" ]; then
  mkdir -p "$DEST/Tools"
  cp "$SRC"/Tools/*.command "$DEST/Tools"/ 2>/dev/null
  chmod +x "$DEST"/Tools/*.command 2>/dev/null
  # An older install had them all at the top level. Move ours down rather
  # than leave two copies of each, which is how the wrong one gets run.
  for f in "$SRC"/Tools/*.command; do
    n=$(basename "$f")
    [ -f "$DEST/$n" ] && rm -f "$DEST/$n" 2>/dev/null
  done
fi
rm -f "$DEST/Apply this update.command" 2>/dev/null
rm -f "$DEST/Build the login app.command" 2>/dev/null
[ -f "$SRC/selftest.py" ] && cp "$SRC/selftest.py" "$DEST/"
# The app builder needs its icon source. Without it the app still builds,
# just without an icon, which looks like something went wrong.
if [ -d "$SRC/LTC Player.iconset" ]; then
  rm -rf "$DEST/LTC Player.iconset" 2>/dev/null
  cp -R "$SRC/LTC Player.iconset" "$DEST/" 2>/dev/null
fi
chmod +x "$DEST"/*.command 2>/dev/null
[ -e "$DEST/ltc" ] && chmod +x "$DEST/ltc" 2>/dev/null

echo
echo "Proving it, about 35 seconds."
echo
REPORT="$SRC/selftest_result.txt"
"$DEST/.venv/bin/python" "$DEST/selftest.py" > "$REPORT" 2>&1
tail -4 "$REPORT"
if grep -q "all checks passed" "$REPORT"; then
  rm -rf "$DEST/ltcplay.previous" "$BACKUP" 2>/dev/null
  OK=yes
  # Things the suite found wrong with the SHOW FILE are not a reason to
  # refuse the program: this script is not allowed to touch the show file,
  # so refusing would mean the update could never be applied at all.
  if grep -q "SHOW FILE" "$REPORT"; then
    echo
    echo "The program is fine. The SHOW FILE needs attention:"
    sed -n 's/^  SHOW FILE  /    /p' "$REPORT"
    echo
    echo "  Open the Tools folder in the install and run"
    echo "  'Set the Advatek triggers.command'."
  fi
else
  echo
  echo "THE SELF TEST FAILED. Putting the whole old install back."
  echo "The whole run was kept here, so it can be read:"
  echo "    $REPORT"
  restore
  OK=""
fi

if [ -z "$OK" ]; then
  bye "The update was undone and the old program is back in place.
Run 'Run ltcplay.command' and press v to confirm it still passes.

Send me selftest_result.txt from this update folder and I can tell you
what failed." 1
fi

echo "  all checks passed"
if [ -n "$AGENT_WAS_UP" ]; then
  echo
  echo "Starting the login agent again."
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || \
    launchctl load "$PLIST" 2>/dev/null
  for _i in $(seq 1 20); do
    sleep 1
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1 && break
  done
fi

bye "Updated:
    $DEST

Your show file, sequences, saved input and Python environment were not
touched. Nothing needs reinstalling."
