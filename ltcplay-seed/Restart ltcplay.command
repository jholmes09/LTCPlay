#!/bin/bash
# Double-click this to force ltcplay to start over from nothing.
#
# Some faults survive a Stop and a Run because the PROCESS keeps running.
# The one that bit us: pulling a USB interface leaves PortAudio holding a
# stale device list, and every later attempt to open an input fails for the
# life of that process. Only a real restart clears it.
#
# There is no window to close when the login agent is running it, which is
# what this is for.
cd "$(dirname "$0")" || exit 1
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

LABEL="com.jeffholmespresents.ltcplay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=7878
UID_="$(id -u)"

answered() {
  command -v curl >/dev/null 2>&1 && \
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1
}
show_running() {
  command -v curl >/dev/null 2>&1 && \
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
      | grep -q '"running": *true'
}
wait_up() {
  printf "Waiting for it to come back"
  for _i in $(seq 1 25); do
    sleep 1; printf "."
    if answered; then echo; echo; echo "It is back, on http://127.0.0.1:$PORT/"; return 0; fi
  done
  echo; return 1
}

echo "ltcplay: force a restart"
echo

if ! answered; then
  echo "Nothing is answering on port $PORT, so there is nothing to restart."
  if [ -f "$PLIST" ]; then
    echo "The login agent is installed, so it should have been up. Starting it."
    launchctl kickstart "gui/$UID_/$LABEL" 2>/dev/null || \
      launchctl start "$LABEL" 2>/dev/null
    wait_up || echo "It still did not answer. Run 'Autostart ltcplay.command' to look at it."
  else
    echo "Open 'Web ltcplay.command' or 'Run ltcplay.command' to start it."
  fi
  echo
  read -r -p "Press return to close. "
  exit 0
fi

if show_running; then
  echo "A SHOW IS RUNNING RIGHT NOW."
  echo
  echo "Restarting drops output for a few seconds. The rig will go dark and"
  echo "come back on the preshow look, then pick the timecode up again."
  echo
  printf "  Type RESTART to do it anyway, anything else to back out: "
  read -r ok
  [ "$ok" = "RESTART" ] || { echo; echo "Nothing was changed."; echo; read -r -p "Press return to close. "; exit 0; }
  echo
fi

if [ -f "$PLIST" ]; then
  echo "Restarting the login agent."
  # kickstart -k kills it and starts it again in one step. Older macOS needs
  # the stop/start pair; KeepAlive brings it back either way.
  launchctl kickstart -k "gui/$UID_/$LABEL" 2>/dev/null || {
    launchctl stop "$LABEL" 2>/dev/null
    sleep 2
    launchctl start "$LABEL" 2>/dev/null
  }
  if wait_up; then
    echo "It starts idle. Press Run on the page to put the show back on."
  else
    echo "It did not come back. Look at:"
    echo "    $(pwd)/ltcplay_autostart.log"
  fi
else
  echo "The login agent is not installed, so this is a Run or Web window."
  echo "Closing that window is the restart. Killing it from here instead:"
  echo
  PIDS=$(pgrep -f "ltcplay.cli|LTC Player.app/Contents/Resources/boot.py" 2>/dev/null | tr '\n' ' ')
  if [ -z "$PIDS" ]; then
    echo "  No ltcplay process found to stop. Close the window by hand."
  else
    echo "  stopping: $PIDS"
    # TERM first so it blacks the rig out on its way down.
    kill $PIDS 2>/dev/null
    sleep 3
    STILL=$(pgrep -f "ltcplay.cli|LTC Player.app/Contents/Resources/boot.py" 2>/dev/null | tr '\n' ' ')
    [ -n "$STILL" ] && { echo "  still up, forcing: $STILL"; kill -9 $STILL 2>/dev/null; }
    echo
    echo "Stopped. Open 'Web ltcplay.command' to start it again."
  fi
fi
echo
read -r -p "Press return to close. "
