#!/bin/bash
# The front door. Everything anyone needs to do on a show day is on this menu,
# in the order they need to do it, so nobody has to remember a command line at
# 6pm with the house filling up.
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
# A copy over a network, a Dropbox sync or a zip round trip drops the execute
# bit and macOS then refuses to open these at all. Repair the whole set on the
# way in, so the next double-click works whatever moved these files here.
for f in *.command; do
  [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null
done
[ -e ./ltc ] && [ ! -x ./ltc ] && chmod +x ./ltc 2>/dev/null

# Keep the Mac awake for as long as this window is open. A MacBook that
# sleeps mid-show freezes the rig on whatever frame was last sent, and the
# clock this program runs on stops with it. -dims: no display sleep, no idle
# sleep, no disk sleep, and stay awake even on battery.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -dims -w $$ >/dev/null 2>&1 &
fi
if [ ! -x ./ltc ]; then
  echo "Not installed yet. Double-click 'Install ltcplay.command' first."
  read -r; exit 1
fi

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; OFF=$'\033[0m'
MODE="show"
TIMELINE=""

pick_timeline() {
  local files=(*_timeline.json *timeline*.json)
  local seen=() f
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    case "$f" in _superseded*) continue;; esac
    case " ${seen[*]} " in *" $f "*) continue;; esac
    seen+=("$f")
  done
  if [ ${#seen[@]} -eq 0 ]; then
    echo "No timeline files in this folder."; TIMELINE=""; return
  fi
  if [ ${#seen[@]} -eq 1 ]; then TIMELINE="${seen[0]}"; return; fi
  echo
  echo "Which show?"
  local i=1
  for f in "${seen[@]}"; do echo "   $i  $f"; i=$((i+1)); done
  echo
  read -r -p "   number: " n
  if [ "$n" -ge 1 ] 2>/dev/null && [ "$n" -le ${#seen[@]} ]; then
    TIMELINE="${seen[$((n-1))]}"
  fi
}

# The input is chosen once and kept in ltcplay_input.json. The menu only reads
# it back, so there is one place that knows the answer rather than two that can
# disagree.
saved_input() {
  [ -f ltcplay_input.json ] || { echo ""; return; }
  ./.venv/bin/python - <<'PY' 2>/dev/null
import json
try:
    d = json.load(open("ltcplay_input.json"))
    print(f"{d.get('device','?')}, in {d.get('channel',1)}")
except Exception:
    print("")
PY
}

pause() { echo; read -r -p "Press return."; }

pick_timeline

while true; do
  clear
  IN=$(saved_input)
  echo "  ${BOLD}ltcplay${OFF}   chase xLights sequences to incoming timecode"
  echo
  echo "  show      ${BOLD}${TIMELINE:-none chosen}${OFF}"
  if [ -n "$IN" ]; then
    echo "  input     ${BOLD}${IN}${OFF}"
  else
    echo "  input     ${DIM}not set, using the macOS default${OFF}"
  fi
  if [ "$MODE" = "show" ]; then
    echo "  mode      ${BOLD}SHOW${OFF}${DIM}   if timecode dies the set runs itself out on this Mac's clock${OFF}"
  else
    echo "  mode      ${BOLD}REHEARSAL${OFF}${DIM}   if timecode stops the rig holds where it is${OFF}"
  fi
  echo
  echo "  First time on a new rig, do 1 and 2. After that, start at 3."
  echo
  echo "   ${BOLD}1${OFF}  Find the timecode           ${DIM}listens to the jack and the interface, says which input has it${OFF}"
  echo "   ${BOLD}2${OFF}  Set the input               ${DIM}pick it once; every run uses it from then on${OFF}"
  echo "   ${BOLD}3${OFF}  Watch the timecode          ${DIM}decodes it, sends nothing, tells you the frame rate${OFF}"
  echo "   ${BOLD}4${OFF}  Validate the show           ${DIM}every sequence, every controller, no output${OFF}"
  echo "   ${BOLD}5${OFF}  Rehearse                    ${DIM}follows timecode, still sends nothing${OFF}"
  echo "   ${BOLD}6${OFF}  ${GRN}Run${OFF}                         ${DIM}sends to the rig${OFF}"
  echo
  echo "   ${BOLD}w${OFF}  Open the web page           ${DIM}same engine, clickable, works from a phone${OFF}"
  echo
  echo "   ${DIM}m  switch between show and rehearsal mode${OFF}"
  echo "   ${DIM}s  choose a different show        l  open today's log${OFF}"
  echo "   ${DIM}v  prove this copy works           t  a terminal in this folder${OFF}"
  echo "   ${DIM}q  quit${OFF}"
  echo
  read -r -p "  > " k
  case "$k" in
    1) clear; echo "Timecode must be RUNNING for this to find anything."; echo
       ./ltc find; pause ;;
    2) clear; ./ltc input; pause ;;
    3) clear; echo "Ctrl-C when you have seen enough."; echo
       ./ltc monitor; pause ;;
    4) clear; ./ltc check "$TIMELINE"; pause ;;
    5) clear
       if [ "$MODE" = "rehearsal" ]; then
         ./ltc run "$TIMELINE" --no-output --on-lost hold
       else
         ./ltc run "$TIMELINE" --no-output --on-lost freerun
       fi
       pause ;;
    6) clear
       echo "  ${RED}${BOLD}This sends to the lighting network.${OFF}"
       echo
       ./ltc check "$TIMELINE" --no-audio 2>&1 | tail -6
       echo
       read -r -p "  Type RUN to go live, anything else to back out: " ok
       if [ "$ok" = "RUN" ]; then
         clear
         if [ "$MODE" = "rehearsal" ]; then
           ./ltc run "$TIMELINE" --on-lost hold
         else
           ./ltc run "$TIMELINE" --on-lost freerun
         fi
       fi
       pause ;;
    w|W) clear
         # The login agent, if installed, already owns the port. Starting a
         # second engine here just fails with "Address already in use" and the
         # menu cannot pass --port. Round 3 of the audit, 2026-09-13.
         if [ -f "$HOME/Library/LaunchAgents/com.jeffholmespresents.ltcplay.plist" ]; then
           echo "Autostart is installed, so the engine is already running and"
           echo "will keep running by itself. The page is already up:"
           echo
           echo "    http://127.0.0.1:7878/"
           echo
           echo "(To go back to running it from a window, open"
           echo " 'Autostart ltcplay.command' and choose R.)"
           pause
           continue
         fi
         echo "Leave the window that opens alone: it is the engine."
         echo "Ctrl-C in it stops the show. Closing the browser does not."
         echo
         read -r -p "  Reach it from a phone or iPad on this network too? [y/N] " net
         if [ "$net" = "y" ] || [ "$net" = "Y" ]; then
           ./ltc serve --bind 0.0.0.0
         else
           ./ltc serve
         fi
         pause ;;
    m|M) [ "$MODE" = "show" ] && MODE="rehearsal" || MODE="show" ;;
    s|S) pick_timeline ;;
    l|L) [ -f ltcplay.log ] && { clear; tail -60 ltcplay.log; } || echo "No log yet."; pause ;;
    v|V) clear
       echo "Proving this copy of ltcplay, about 35 seconds."
       echo "It must end: all checks passed"
       echo
       ./.venv/bin/python selftest.py
       pause ;;
    t|T) clear; echo "Type 'exit' to come back to the menu."; echo; $SHELL ;;
    q|Q) clear; exit 0 ;;
  esac
done
