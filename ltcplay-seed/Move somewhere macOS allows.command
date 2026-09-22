#!/bin/bash
# Double-click this if the login agent refused to install because of where
# this folder is.
#
# macOS will not let a login agent read Desktop, Documents, Downloads, or any
# cloud-synced folder. It refuses with "Operation not permitted", writes no
# log, and the agent looks installed while nothing runs. A plain folder in
# your home directory is none of those things.
#
# This moves the whole folder there. Nothing inside it changes.
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

HERE="$(pwd)"
NAME="$(basename "$HERE")"
DEST="$HOME/$NAME"

say() { echo; echo "$1"; echo; read -r -p "Press return to close. "; }

# This moves an INSTALL. Run from an update pack it would move the pack, which
# leaves a second folder lying around and updates nothing. The pack ships this
# file only because it is one of the launchers that gets copied INTO the
# install. Jeff ran it from the pack, 2026-09-14, and was right to call it
# confusing.
if [ -f "$HERE/Apply this update.command" ] && [ ! -d "$HERE/show" ] \
   && [ ! -d "$HERE/.venv" ]; then
  say "This is an update pack, not an installed copy of ltcplay, so there is
nothing here to move.

To apply the update, double-click:

    Apply this update.command

It finds your existing install by itself and copies the program into it.
You do not need to move anything first."
  exit 0
fi

# An install whose show folder is a LINK to somewhere else -- the way this
# player ships inside the xLights package, sharing one copy of the renders
# with xLights instead of carrying a second 600MB copy -- cannot be moved on
# its own. The link is relative, so moving this folder away from the show
# folder it points at silently leaves an install with no sequences. Move the
# folder that holds BOTH.
if [ -L "$HERE/show" ]; then
  target="$(readlink "$HERE/show")"
  case "$target" in
    ..*|/*)
      say "This copy of ltcplay shares its show folder with xLights instead of
carrying its own, so it cannot be moved on its own:

    show  ->  $target

Moving just this folder would leave it with no sequences.

Move the folder that holds BOTH of them:

    $(dirname "$HERE")

Drag that whole folder to your home folder, then run this again if the
login agent still refuses."
      exit 0
      ;;
  esac
fi

echo "ltcplay: move to a folder macOS allows a login agent to reach"
echo
echo "  from:  $HERE"
echo "  to:    $DEST"
echo

case "$HERE" in
  "$HOME/"*) inner="${HERE#$HOME/}" ;;
  *) inner="" ;;
esac
case "$HERE" in
  "$HOME/Desktop"|"$HOME/Desktop/"*|"$HOME/Documents"|"$HOME/Documents/"*|\
  "$HOME/Downloads"|"$HOME/Downloads/"*|*"/Library/CloudStorage/"*|\
  *"/Dropbox/"*|*"/Library/Mobile Documents/"*|*"/OneDrive"*|*"/Google Drive"*)
    : ;;
  *)
    say "This folder is already somewhere a login agent can reach it.
Nothing to move. Run 'Autostart ltcplay.command' from here."
    exit 0 ;;
esac

if [ -e "$DEST" ]; then
  say "There is already something at:
    $DEST
Move or rename it first, then run this again. Nothing was changed."
  exit 1
fi

# Never move a folder with a show on the rig underneath it.
if command -v curl >/dev/null 2>&1 && \
   curl -fsS --max-time 2 "http://127.0.0.1:7878/api/state" 2>/dev/null \
     | grep -q '"running": *true'; then
  say "A SHOW IS RUNNING. Stop it on the page first. Nothing was changed."
  exit 1
fi

printf "Move it? [y/N] : "
read -r ans
case "$ans" in
  [Yy]*) ;;
  *) say "Nothing was changed." ; exit 0 ;;
esac

if ! mv "$HERE" "$DEST"; then
  say "The move failed and nothing was changed. The usual cause is that
Finder or Terminal has the folder open. Close them and try again."
  exit 1
fi

# The venv hard-codes the path it was built at, so it has to be rebuilt.
if [ -d "$DEST/.venv" ]; then
  rm -rf "$DEST/.venv" "$DEST/ltc" 2>/dev/null
  REDO="Its Python environment was tied to the old path, so it was removed.
Run 'Install ltcplay.command' in the new folder before anything else."
else
  REDO="Run 'Install ltcplay.command' in the new folder first."
fi
chmod +x "$DEST"/*.command 2>/dev/null

say "Moved to:
    $DEST

$REDO
Then 'Autostart ltcplay.command' will install without complaint.

This window's folder no longer exists. Open the new one in Finder."
