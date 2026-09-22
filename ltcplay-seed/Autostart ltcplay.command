#!/bin/bash
# Double-click this to make the web engine come back by itself.
#
# What it installs: a macOS LaunchAgent that starts `ltcplay serve` when you
# log in, and restarts it within seconds if it ever stops -- a crash, a
# force-quit, a Terminal window closed by accident. It does NOT start a show
# on its own: the engine comes up idle, with the page ready, and a person
# still presses Run. Bringing a rig to life unattended is not this script's
# decision to make.
#
# Why it exists: the program was hardened against everything that can go
# wrong INSIDE it, while the season's real risk is the process not being
# there at all and nobody standing at the laptop at 8pm. An adversarial
# design review said so on 2026-09-13 and it was right.
#
# To remove it, run this again and choose R.
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

LABEL="com.jeffholmespresents.ltcplay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(pwd)"
PORT=7878

# Never take an engine down that has a show on it, and never install under a
# window that already owns the port -- the agent would crash-loop every five
# seconds and nobody would find out until 8pm. Round 3 of the audit, 2026-09-13.
port_taken() {
  command -v lsof >/dev/null 2>&1 && \
    lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1
}
busy_now() {
  command -v curl >/dev/null 2>&1 && \
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
      | grep -q '"running": *true'
}

echo "ltcplay autostart"
echo
if [ -f "$PLIST" ]; then
  echo "Autostart is INSTALLED, pointing at:"
  AT=$(workdir_of "$PLIST")
  echo "    ${AT:-(could not read the path out of the plist)}"
  if [ -n "$AT" ] && [ ! -d "$AT" ]; then
    echo "    THAT FOLDER NO LONGER EXISTS, so nothing can start. Press R."
  fi
  echo
  printf "  [R] remove it    [I] reinstall it for THIS folder    [Q] quit : "
else
  echo "Autostart is NOT installed."
  echo
  echo "Installing it means: at login, this folder's web engine starts by"
  echo "itself and stays started. It comes up IDLE -- no show runs until"
  echo "somebody presses Run on the page."
  echo
  printf "  [I] install it    [Q] quit : "
fi
read -r ans
case "$ans" in
  [Rr]*)
    if busy_now; then
      echo
      echo "A SHOW IS RUNNING on the engine this would stop."
      echo "Stop the show on the page first, then run this again."
      echo
      read -r -p "Press return to close. " _
      exit 1
    fi
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
      launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "Removed. Nothing starts by itself now."
    ;;
  [Ii]*)
    if busy_now; then
      echo
      echo "A SHOW IS RUNNING on port $PORT. Installing would restart the"
      echo "engine under it and black out the rig. Stop the show first."
      echo
      read -r -p "Press return to close. " _
      exit 1
    fi
    if port_taken; then
      echo
      echo "Something is already listening on port $PORT -- almost certainly"
      echo "a 'Web ltcplay.command' window you have open. The agent cannot"
      echo "bind the port while that window lives, so it would fail every"
      echo "five seconds and you would not know until show night."
      echo
      echo "Close that window, then run this again."
      echo
      read -r -p "Press return to close. " _
      exit 1
    fi
    # Prefer the app bundle when it is there. A bare python started by
    # launchd has no bundle id and no Info.plist, so macOS never asks for the
    # microphone and hands it silence instead. See 'Build the login app'.
    # 'LTC Player.app' is the current one: its launcher finds the virtual
    # environment beside it by itself, so launchd needs nothing but the path.
    # 'ltcplay.app' is the older login-only bundle, kept working for anyone
    # who still has one.
    APPEXE=""
    APPBOOT=""
    SITEDIR=""
    PYHOME=""
    USE_APP=""
    NEWAPP="$HERE/LTC Player.app/Contents/MacOS/LTC Player"
    if [ -x "$NEWAPP" ]; then
      APPEXE="$NEWAPP"
      USE_APP=new
    else
      APPEXE="$HERE/ltcplay.app/Contents/MacOS/ltcplay"
      APPBOOT="$HERE/ltcplay.app/Contents/Resources/boot.py"
      if [ -x "$APPEXE" ] && [ -f "$APPBOOT" ]; then
        SITEDIR=$(./.venv/bin/python -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)
        PYHOME=$(cat "$HERE/ltcplay.app/Contents/Resources/pythonhome" 2>/dev/null)
        [ -n "$SITEDIR" ] && USE_APP=old
      fi
    fi
    if [ ! -x ./ltc ]; then
      echo
      echo "This folder has not been installed yet, so there is no engine to"
      echo "start. Double-click 'Install ltcplay.command' first, then run this."
      echo
      read -r -p "Press return to close. " _
      exit 1
    fi
    # A LaunchAgent cannot reliably reach a Dropbox / iCloud / OneDrive folder:
    # those are FileProvider mounts that need Full Disk Access and may not be
    # mounted at login. launchd then refuses to spawn the job and cannot even
    # create the log file, so it fails in total silence -- the agent is
    # "installed", nothing runs, and there is nothing to read.
    # Jeff hit exactly that on 2026-09-14.
    # Two kinds of folder a LaunchAgent cannot start from, and they fail the
    # same silent way: "Operation not permitted", nothing in the log, agent
    # installed, nothing running.
    #
    #   Cloud storage (Dropbox, iCloud, OneDrive, Google Drive) is a
    #   FileProvider mount that may not even exist at login.
    #
    #   Desktop, Documents and Downloads are TCC protected. A process
    #   launchd spawns does not inherit the consent the user gave Terminal
    #   or Finder, so it is refused. This one caught ME out: on 2026-09-14 I
    #   told Jeff to install the agent from the Desktop bundle and it failed
    #   for exactly this reason.
    #
    # A plain folder in the home directory is neither.
    BAD=""
    case "$HERE" in
      *"/Library/CloudStorage/"*|*"/Dropbox/"*|*"/Library/Mobile Documents/"*|\
      *"/OneDrive"*|*"/Google Drive"*)
        BAD="a cloud-synced folder. It may not be mounted at login, and macOS
blocks a login agent from reaching it." ;;
      "$HOME/Desktop"|"$HOME/Desktop/"*|\
      "$HOME/Documents"|"$HOME/Documents/"*|\
      "$HOME/Downloads"|"$HOME/Downloads/"*)
        BAD="a folder macOS protects. Desktop, Documents and Downloads all
need permission that a login agent does not have, so it is refused with
\"Operation not permitted\"." ;;
    esac
    if [ -n "$BAD" ]; then
      SUGGEST="$HOME/ltcplay"
      echo
      echo "This folder is $BAD"
      echo
      echo "    $HERE"
      echo
      echo "Move the whole folder to your home folder instead:"
      echo
      echo "    mv \"$HERE\" \"$SUGGEST\""
      echo
      echo "then run 'Install ltcplay.command' there, and this again."
      echo "Nothing else needs to change; the show travels with the folder."
      echo
      read -r -p "Press return to close. " _
      exit 1
    fi
    if [ "$USE_APP" = "new" ]; then
      PROGARGS="    <string>$APPEXE</string>
    <string>serve</string>
    <string>--no-browser</string>"
      echo "Using LTC Player.app, so macOS has something to ask about."
    elif [ "$USE_APP" = "old" ]; then
      PROGARGS="    <string>$APPEXE</string>
    <string>$APPBOOT</string>
    <string>--site=$SITEDIR</string>"
      echo "Using the older ltcplay.app bundle."
    else
      PROGARGS="    <string>$HERE/ltc</string>
    <string>serve</string>
    <string>--no-browser</string>"
      echo "No app here. Build one with 'Build LTC Player app.command' if the"
      echo "agent turns out not to hear timecode."
    fi
    ENVBLOCK=""
    if [ "$USE_APP" = "old" ] && [ -n "$PYHOME" ]; then
      ENVBLOCK="  <key>EnvironmentVariables</key>
  <dict><key>PYTHONHOME</key><string>$PYHOME</string></dict>"
    fi
    echo
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$PROGARGS
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
$ENVBLOCK
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$HERE/ltcplay_autostart.log</string>
  <key>StandardErrorPath</key><string>$HERE/ltcplay_autostart.log</string>
</dict>
</plist>
PLISTEOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
    launchctl unload "$PLIST" 2>/dev/null
    if launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || \
       launchctl load "$PLIST" 2>/dev/null; then
      # launchctl returning 0 means the job was ACCEPTED. It says nothing
      # about whether the program runs. Claiming "installed and running" on
      # that is how an agent that never started looked like a success.
      echo
      printf "Waiting for the engine to answer"
      up=""
      for _i in $(seq 1 20); do
        sleep 1
        printf "."
        if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" \
             >/dev/null 2>&1; then up=yes; break; fi
      done
      echo
      if [ -z "$up" ]; then
        echo
        echo "THE AGENT WAS ACCEPTED BUT THE ENGINE NEVER ANSWERED."
        echo "It is installed and it is not working. What it said:"
        echo
        if [ -s "$HERE/ltcplay_autostart.log" ]; then
          tail -15 "$HERE/ltcplay_autostart.log" | sed 's/^/    /'
        else
          echo "    (nothing was written to $HERE/ltcplay_autostart.log,"
          echo "     which usually means launchd could not reach this folder"
          echo "     at all -- a permissions or cloud-storage problem, not a"
          echo "     problem with the show)"
        fi
        echo
        echo "Run this again and press R to remove it, then use"
        echo "'Web ltcplay.command' until it is sorted."
        echo
        read -r -p "Press return to close. " _
        exit 1
      fi
      echo "Installed, and the engine answered on port $PORT."
      echo "It will come back by itself if it ever stops."
      echo
      echo "No browser opens by itself. Go to:  http://127.0.0.1:7878/"
      echo
      echo "It starts IDLE. A show still needs somebody to press Run."
      echo "The engine holds the Mac awake by itself while it is up."
      echo
      echo "TEST THE MICROPHONE NOW, BEFORE YOU WALK AWAY."
      echo
      echo "  With timecode running, press Run on the page and watch the"
      echo "  LTC IN number. If it counts, this agent can hear and you are"
      echo "  done."
      echo
      echo "  If it never counts, and 'Web ltcplay.command' CAN hear the"
      echo "  same timecode, this agent has no microphone access. macOS"
      echo "  grants that per program and does NOT prompt for a login"
      echo "  agent, so there is nothing to click and nothing to grant."
      echo "  Run this again, press R to remove the agent, and use"
      echo "  'Web ltcplay.command' instead: it runs under Terminal, which"
      echo "  already has the grant. Confirmed on 2026-09-14."
      echo
      echo "  Do not run 'Web ltcplay.command' while this agent is"
      echo "  installed. They both want port 7878."
      echo
      echo "Log: $HERE/ltcplay_autostart.log"
    else
      echo "launchctl refused it. The file is at:"
      echo "  $PLIST"
      rm -f "$PLIST"
    fi
    ;;
  *) echo "Nothing changed." ;;
esac
echo
read -r -p "Press return to close. " _
