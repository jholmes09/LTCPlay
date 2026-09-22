#!/bin/bash
# Double-click this to run the show from a web page instead of a terminal.
#
# This window IS the engine. The page is a window onto it, so closing the
# browser does not stop a running show. Closing THIS window does.
cd "$(dirname "$0")" || exit 1
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

PORT=7878
BIND=127.0.0.1

# An already-running server is the old code. The page is read off disk on
# every load, this program is not, so a server left over from before an
# update serves a new page against old routes and every new button answers
# "no such thing here". Rather than explain that, end it: whatever is holding
# the port goes, and this window becomes the server.
#
# Only ltcplay's own processes are touched. Anything else on the port is
# reported and left alone, because killing a stranger's process to take a
# port is not this script's business.
# If the login agent owns this port, do not fight it: KeepAlive respawns it
# the moment this window kills it, and whoever binds first wins. Round 2 of
# the audit, 2026-09-13.
AGENT="$HOME/Library/LaunchAgents/com.jeffholmespresents.ltcplay.plist"
if [ -f "$AGENT" ]; then
  echo "Autostart is installed, so the engine is already running and will"
  echo "keep running by itself. Just open the page:"
  echo
  echo "    http://127.0.0.1:$PORT/"
  echo
  echo "(To go back to running it from a window, open 'Autostart"
  echo " ltcplay.command' and choose R.)"
  echo
  read -r -p "Press return to close. " _
  exit 0
fi

OLD=$(pgrep -f "ltcplay.cli serve|LTC Player.app/Contents/Resources/boot.py" 2>/dev/null || true)
if [ -n "$OLD" ]; then
  # But never take down a live show to do it. Ask the old server what it is
  # doing first; if a show is running, the rig goes dark the moment it dies,
  # and that decision is not this script's to make.
  BUSY=no
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
        | grep -q '"running": *true'; then
      BUSY=yes
    fi
  fi
  if [ "$BUSY" = "yes" ]; then
    echo "A show is RUNNING on the server that is already open."
    echo "Restarting would black out the rig."
    echo
    echo "Stop the show on the web page first, then run this again."
    echo
    read -r -p "Press return to close. " _
    exit 1
  fi
  echo "Stopping the idle ltcplay web server that is already running, so"
  echo "this window serves the current code."
  # shellcheck disable=SC2086
  kill $OLD 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "ltcplay.cli serve|LTC Player.app/Contents/Resources/boot.py" >/dev/null 2>&1 || break
    sleep 0.3
  done
  # shellcheck disable=SC2086
  pgrep -f "ltcplay.cli serve|LTC Player.app/Contents/Resources/boot.py" >/dev/null 2>&1 && kill -9 $OLD 2>/dev/null
  echo
fi
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Something else is already listening on port $PORT and it is not"
  echo "ltcplay. Quit it, or this page will not load:"
  lsof -nP -iTCP:$PORT -sTCP:LISTEN
  echo
  read -r -p "Press return once it is closed. " _
fi
if [ "$1" = "--network" ] || [ "$1" = "network" ]; then
  BIND=0.0.0.0
  echo "Serving on the whole network so a phone or iPad can reach it."
  echo "A token is required; it is in the link below."
  echo
fi
exec ./ltc serve --port "$PORT" --bind "$BIND"
