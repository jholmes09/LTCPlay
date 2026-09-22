#!/bin/bash
# Run this once. It builds a private Python environment beside these files and
# installs the three libraries ltcplay needs into it. Nothing is installed
# system-wide and nothing outside this folder is touched. Safe to run again.
cd "$(dirname "$0")" || exit 1

# NOT "ltcplay": that name belongs to the python package directory sitting right
# here, and redirecting a file over a directory fails in a way that only shows
# up when somebody actually runs the installer.
LAUNCHER=ltc

fail() { echo; echo "$1"; echo; echo "Press return to close."; read -r; exit 1; }

echo "Installing ltcplay into a private Python environment in this folder."
echo
[ -f ltcplay/cli.py ] || fail "This script has to sit in the same folder as the
'ltcplay' package directory, and that directory is missing or incomplete."
[ -d "$LAUNCHER" ] && fail "Cannot create the '$LAUNCHER' command: a folder of
that name is already here. Move or rename it and run this again."

PY=$(command -v python3) || fail "python3 is not installed.
In Terminal run:  xcode-select --install
then run this again."

# /usr/bin/python3 on a Mac that has never had Xcode is a STUB. It exists,
# command -v finds it, and it does nothing until the Command Line Tools are
# installed: macOS pops its own 'developer tools not found' dialog and every
# call fails. Testing that python3 is PRESENT proves nothing, so run it.
# Jeff hit this on the second Mac, 2026-09-14.
if ! PYV=$("$PY" -V 2>&1); then
  fail "macOS has not installed its Command Line Tools yet, so python3 cannot
run. It is a free Apple download and takes a few minutes.

In Terminal, run:

    xcode-select --install

Click Install, let it finish, then double-click this file again.

(python3 said: $PYV)"
fi
echo "Using $PY ($PYV)"

# An earlier half-finished install leaves a .venv that cannot be repaired and
# fails here every time until it is removed.
if [ -d .venv ] && [ ! -x .venv/bin/python ]; then
  echo "Removing a half-built Python environment from an earlier attempt."
  rm -rf .venv
fi

# Keep the real error. "Could not create the Python environment" on its own
# is the second useless sentence in a row after macOS's own dialog.
if ! VENVLOG=$("$PY" -m venv .venv 2>&1); then
  case "$VENVLOG" in
    *xcrun*|*"developer path"*|*"Command Line Tools"*|*"developer tools"*)
      fail "macOS has not installed its Command Line Tools yet.

In Terminal, run:

    xcode-select --install

Click Install, let it finish, then double-click this file again.

(python3 said: $VENVLOG)" ;;
    *"Read-only file system"*|*"Permission denied"*)
      fail "This folder cannot be written to, so the Python environment
cannot be built here. Copy the whole folder somewhere you own, for example
your Desktop, and run this again.

(python3 said: $VENVLOG)" ;;
    *)
      fail "Could not create the Python environment.

$VENVLOG" ;;
  esac
fi
./.venv/bin/pip install --upgrade pip --quiet
./.venv/bin/pip install numpy sounddevice zstandard \
  || fail "Installing the libraries failed. Are you online?"

cat > "$LAUNCHER" <<'LAUNCH'
#!/bin/bash
cd "$(dirname "$0")" || exit 1
exec ./.venv/bin/python -m ltcplay.cli "$@"
LAUNCH
chmod +x "$LAUNCHER"

# And every double-clickable file beside it. A copy over a network, a Dropbox
# sync, a zip round trip or a write from another machine all drop the execute
# bit, and macOS then refuses to run them with "you do not have appropriate
# access privileges" -- which reads like a permissions problem with the Mac
# rather than a missing +x. It happened on 2026-09-13.
for f in *.command; do
  [ -e "$f" ] && chmod +x "$f"
done

echo
echo "Checking that it loads:"
"./$LAUNCHER" --help >/dev/null || fail "The ltcplay package did not load.
Nothing else will work until that does."
echo "  ok"

echo
echo "Checking the audio library:"
./.venv/bin/python - <<'CHECK'
try:
    import sounddevice
    print(f"  ok, PortAudio {sounddevice.get_portaudio_version()[1]}")
except Exception as e:
    print(f"  PROBLEM: sounddevice will not load: {e}")
    print("  Everything else is installed, but nothing can read timecode until")
    print("  this works. Try:")
    print("    ./.venv/bin/pip install --force-reinstall sounddevice")
CHECK

echo
echo "Running the self-test (about 25 seconds):"
./.venv/bin/python selftest.py 2>&1 | tail -3

echo
echo "Audio inputs this Mac can see right now:"
echo
"./$LAUNCHER" devices

# Build the application while we are here. It is optional: everything works
# without it. What it adds is a double-click that needs no Terminal window,
# and an identity macOS can attach the microphone permission to, which a bare
# python started by launchd does not have. It is built HERE rather than
# shipped because an app that arrives from another Mac is refused as
# "damaged" until its quarantine mark is cleared.
if [ -x "./Build LTC Player app.command" ] && command -v codesign >/dev/null 2>&1 \
   && command -v cc >/dev/null 2>&1; then
  echo
  echo "Building LTC Player.app..."
  if printf '\n' | "./Build LTC Player app.command" >/dev/null 2>&1 \
     && [ -d "./LTC Player.app" ]; then
    echo "  built. Double-click 'LTC Player.app' to run the show."
  else
    echo "  it did not build. Nothing is wrong with the install; run"
    echo "  'Build LTC Player app.command' on its own to see why."
  fi
fi

cat <<EOF

-------------------------------------------------------------------
Done. Now double-click 'Run ltcplay.command' and start at item 1.

From Terminal in this folder the command is ./$LAUNCHER, for example:
  ./$LAUNCHER find
  ./$LAUNCHER run set1_timeline.json

The first time it listens to an input, macOS will ask for microphone
access for Terminal. Say yes; there is no way to read timecode without
it. If you miss the prompt: System Settings > Privacy & Security >
Microphone, and switch Terminal on.
-------------------------------------------------------------------

EOF
echo "Press return to close."
read -r
