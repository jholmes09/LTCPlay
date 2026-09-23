#!/bin/bash
# Double-click this to find every copy of ltcplay on this Mac and see which
# one is real.
#
# Two copies of a show player on one machine is how the wrong one ends up
# running. It has already happened once this season: the chase was perfect
# and the renders were last year's.
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

LABEL="com.jeffholmespresents.ltcplay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
workdir_of() {   # the folder a LaunchAgent plist points at
  [ -f "$1" ] || return 1
  local v
  v=$(sed -n 's|.*<key>WorkingDirectory</key>[[:space:]]*<string>\(.*\)</string>.*|\1|p' "$1" | head -1)
  if [ -z "$v" ]; then
    v=$(grep -A1 "<key>WorkingDirectory</key>" "$1" | tail -1 \
        | sed -n 's|.*<string>\(.*\)</string>.*|\1|p')
  fi
  [ -n "$v" ] && printf '%s\n' "$v"
}

AGENT=$(workdir_of "$PLIST")

echo "Looking for every copy of ltcplay on this Mac. This takes a moment."
echo

FOUND=$(find "$HOME" -maxdepth 6 -name "cli.py" -path "*/ltcplay/cli.py" \
          -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null \
        | sed 's|/ltcplay/cli.py$||' | sort -u)

if [ -z "$FOUND" ]; then
  echo "No copy of ltcplay found under $HOME."
  echo
  read -r -p "Press return to close. "
  exit 0
fi

n=0
while IFS= read -r d; do
  [ -n "$d" ] || continue
  n=$((n+1))
  size=$(du -sh "$d" 2>/dev/null | cut -f1)
  inst="NOT installed"
  [ -x "$d/.venv/bin/python" ] && inst="installed"
  seq=$(ls "$d/show" 2>/dev/null | grep -ci '\.fseq$')
  [ "$seq" = "0" ] && seq="no show folder" || seq="$seq sequences"
  if grep -q "_reset_portaudio" "$d/ltcplay/audio.py" 2>/dev/null; then
    ver="2026-09-14 or newer"
  else
    ver="OLDER, before the USB recovery fix"
  fi
  where="ok"
  case "$d" in
    "$HOME/Desktop"|"$HOME/Desktop/"*|"$HOME/Documents"|"$HOME/Documents/"*|\
    "$HOME/Downloads"|"$HOME/Downloads/"*)
      where="macOS protects this folder: the login agent CANNOT run here" ;;
    *"/Library/CloudStorage/"*|*"/Dropbox/"*|*"/Library/Mobile Documents/"*|\
    *"/OneDrive"*|*"/Google Drive"*)
      where="cloud synced: the login agent CANNOT run here, and the show is
              being re-synced underneath it" ;;
  esac
  mark=""
  [ -n "$AGENT" ] && [ "$AGENT" = "$d" ] && mark="  <<< the login agent starts THIS one"

  echo "  [$n] $d$mark"
  echo "      $size   $inst   $seq"
  echo "      code: $ver"
  [ "$where" != "ok" ] && echo "      WHERE: $where"
  echo
done <<< "$FOUND"

echo "-------------------------------------------------------------------"
echo "Keep ONE. The one to keep is the copy that is all of these:"
echo "   installed, has its sequences, code 2026-09-14 or newer, and is"
echo "   NOT in Desktop, Documents, Downloads or a cloud folder."
echo
if [ -n "$AGENT" ]; then
  echo "The login agent is currently pointed at:"
  echo "   $AGENT"
  if [ ! -d "$AGENT" ]; then
    echo
    echo "   THAT FOLDER NO LONGER EXISTS, so nothing starts at login."
    echo "   Run 'Autostart ltcplay.command', press R, then install it"
    echo "   again from the copy you keep."
  fi
else
  echo "No login agent is installed."
fi
echo
echo "Drag the copies you do not want to the Trash yourself. This script"
echo "never deletes anything."
echo
read -r -p "Press return to close. "
