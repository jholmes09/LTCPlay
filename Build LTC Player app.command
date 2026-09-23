#!/bin/bash
# Double-click this once, on the Mac that will run the show, to build
# "LTC Player.app" beside it.
#
# WHY IT IS BUILT HERE AND NOT SHIPPED READY-MADE
# An application without an Apple developer signature is refused by macOS the
# moment it arrives from somewhere else: "LTC Player is damaged and should be
# moved to the Trash". That is quarantine, not damage, and it is attached by
# the transfer, not by the app. An app built on the Mac that runs it is never
# quarantined, so the problem cannot happen. That is the whole reason this is
# a build script rather than a folder you copy.
#
# WHAT IT GIVES YOU OVER THE .command FILES
#   - Double-click, no Terminal window, a real icon in the Dock.
#   - A stable identity for macOS to hang the MICROPHONE permission on.
#     A bare python started by launchd has none, so macOS never asks and
#     hands it silence. That cost a rehearsal on 2026-09-14.
#   - Something named "LTC Player" in Activity Monitor instead of "Python".
#
# It changes nothing about how the show runs. The .command files keep working.
set -u
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

APP="LTC Player.app"
EXE="LTC Player"
BUNDLE_ID="com.jeffholmespresents.ltcplayer"
HERE="$(pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ltcplayapp.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

bye() { echo; echo "$1"; echo; read -r -p "Press return to close. "; exit "${2:-0}"; }
step() { echo "  $1"; }

echo
echo "LTC Player: build the application"
echo

# ---------------------------------------------------------------- checks
[ -x ./.venv/bin/python ] || bye "This folder is not installed yet.

Double-click 'Install ltcplay.command' first, then run this again." 1
[ -f ltcplay/cli.py ] || bye "The ltcplay program is not beside this script,
so there is nothing to build an app around." 1
command -v cc >/dev/null 2>&1 || bye "The compiler is missing, which means the
Command Line Tools are not installed. In Terminal, once:

    xcode-select --install

Wait for it to finish, then run this again." 1
command -v codesign >/dev/null 2>&1 || bye "codesign is missing, which means the
Command Line Tools are not installed. In Terminal, once:

    xcode-select --install

Wait for it to finish, then run this again." 1

if [ -d "$HERE/$APP" ]; then
  echo "There is already an app here. It will be replaced."
  echo
fi

# ---------------------------------------------------------------- build
# Everything is assembled in a scratch folder and signed THERE, then moved
# into place in one step. A bundle that is modified after it is signed has a
# broken seal, and on Apple Silicon a broken seal is refused at launch. That
# is exactly how the xLights bundle was broken once, by an edit to Info.plist
# after signing, so nothing here writes into the bundle after codesign runs.
B="$WORK/$APP"
mkdir -p "$B/Contents/MacOS" "$B/Contents/Resources" || bye "Could not build." 1

cat > "$WORK/stub.c" <<'CSTUB'
/* LTC Player: the application's main program.
 *
 * It exists so the bundle has a real signed executable of its own. It works
 * out where it is, then hands over to the Python in the virtual environment
 * beside the app. execv replaces this process rather than starting a second
 * one, so what macOS attaches the microphone permission to stays the app.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <limits.h>
#include <errno.h>
#include <fcntl.h>
#include <time.h>
#include <mach-o/dyld.h>

static void up(char *p) {              /* strip the last path component */
    char *s = strrchr(p, '/');
    if (s) *s = '\0';
}

/* AppleScript string literals cannot carry a quote or a backslash. Paths
 * should never contain either, but "should never" is how a diagnostic turns
 * into no dialog at all at the moment somebody needs one. */
static void clean(char *p) {
    for (; *p; p++) if (*p == '"' || *p == '\\') *p = '_';
}

/* A record that survives the dialog being dismissed, in a folder every app
 * can write to without asking macOS for anything. */
static void logline(const char *msg) {
    const char *home = getenv("HOME");
    if (!home) return;
    char path[PATH_MAX];
    if (snprintf(path, sizeof(path), "%s/Library/Logs/LTCPlayer-start.log",
                 home) >= (int)sizeof(path)) return;
    FILE *f = fopen(path, "a");
    if (!f) return;
    time_t t = time(NULL);
    char when[64];
    strftime(when, sizeof(when), "%Y-%m-%d %H:%M:%S", localtime(&t));
    fprintf(f, "%s  %s\n\n", when, msg);
    fclose(f);
}

static void fail(const char *msg) {
    /* A double-clicked app that does nothing is the worst failure there is:
     * no window, no error, nothing to tell anyone. Put it on the screen AND
     * in a log, because the dialog is gone the moment it is dismissed. */
    char script[2 * PATH_MAX + 2048];
    logline(msg);
    snprintf(script, sizeof(script),
             "display dialog \"LTC Player cannot start.\n\n%s\" "
             "with title \"LTC Player\" buttons {\"OK\"} "
             "with icon stop default button 1", msg);
    fprintf(stderr, "LTC Player: %s\n", msg);
    /* selftest runs this launcher with no Python on purpose. It sets
     * LTCPLAY_NO_DIALOG=1 so no dialog lands on the screen of whoever is
     * testing. Only the dialog is skipped: the message above has already
     * been printed and logged. Nothing else ever sets it. */
    const char *nodialog = getenv("LTCPLAY_NO_DIALOG");
    if (nodialog && strcmp(nodialog, "1") == 0) _exit(70);
    execl("/usr/bin/osascript", "osascript", "-e", script, (char *)NULL);
    _exit(70);
}

/* Why a file could not be used, in the words of the thing that refused.
 * "It is missing" was wrong and sent Jeff looking for a file that was
 * sitting right there: the refusal was permission, or a copy of the app
 * running from somewhere the folder is not. 2026-09-15. */
static void fail_path(const char *what, const char *path, const char *folder,
                      int err) {
    char p[PATH_MAX], f[PATH_MAX], msg[2 * PATH_MAX + 1024];
    snprintf(p, sizeof(p), "%s", path);
    snprintf(f, sizeof(f), "%s", folder);
    clean(p);
    clean(f);
    const char *hint;
    if (strstr(p, "/AppTranslocation/") || strstr(f, "/AppTranslocation/")) {
        hint = "macOS is running this app from a temporary read-only copy, "
               "which is what it does to an app that arrived from another "
               "machine. Drag the app to another folder and back, then open "
               "it again.";
    } else if (err == EPERM || err == EACCES) {
        hint = "macOS refused this app access to that folder. Desktop, "
               "Documents and Downloads are protected. Move the whole "
               "package to your home folder, or allow LTC Player in System "
               "Settings, Privacy and Security, Files and Folders.";
    } else if (err == ENOENT) {
        hint = "Open the folder this app is in and double-click "
               "'Install ltcplay.command', then try again.";
    } else {
        hint = "Open the folder this app is in and double-click "
               "'Install ltcplay.command', then try again.";
    }
    snprintf(msg, sizeof(msg),
             "%s\n\n%s\n\nIt looked for:\n%s\n\nIts folder:\n%s\n\n"
             "macOS said: %s",
             what, hint, p, f, strerror(err));
    fail(msg);
}

int main(int argc, char *argv[]) {
    char raw[PATH_MAX], exe[PATH_MAX];
    uint32_t sz = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &sz) != 0) fail("Its own path is too long.");
    if (!realpath(raw, exe)) fail("Its own path could not be resolved.");

    char contents[PATH_MAX], folder[PATH_MAX];
    strncpy(contents, exe, sizeof(contents) - 1);
    contents[sizeof(contents) - 1] = '\0';
    up(contents);                       /* Contents/MacOS */
    up(contents);                       /* Contents      */
    strncpy(folder, contents, sizeof(folder) - 1);
    folder[sizeof(folder) - 1] = '\0';
    up(folder);                         /* LTC Player.app */
    up(folder);                         /* the folder it sits in */

    char py[PATH_MAX], boot[PATH_MAX];
    /* Checked, not assumed: a silently truncated path would send this at
     * some other file entirely. */
    if (snprintf(py, sizeof(py), "%s/.venv/bin/python", folder)
            >= (int)sizeof(py)) fail("Its folder path is too long.");
    if (snprintf(boot, sizeof(boot), "%s/Resources/boot.py", contents)
            >= (int)sizeof(boot)) fail("Its folder path is too long.");

    /* open(), not access(): a real file operation is what makes macOS ask
     * for permission to a protected folder. access() is refused silently and
     * reports the file as missing when it is sitting right there. */
    int fd = open(py, O_RDONLY);
    if (fd < 0)
        fail_path("The Python environment could not be opened.", py, folder,
                  errno);
    close(fd);
    fd = open(boot, O_RDONLY);
    if (fd < 0)
        fail_path("The app's own start-up file could not be opened.", boot,
                  folder, errno);
    close(fd);
    if (chdir(folder) != 0)
        fail_path("Its own folder could not be opened.", folder, folder,
                  errno);

    /* LaunchServices adds -psn_0_nnnn on some versions of macOS. Python would
     * treat it as an unknown option and refuse to start. */
    char **out = calloc((size_t)argc + 3, sizeof(char *));
    if (!out) fail("Out of memory.");
    int n = 0;
    /* argv[0] is only a name, and it is the name Activity Monitor shows.
     * Handing python its own path put "python3.14" in the list, which is
     * not something anybody can pick out of thirty other things at 9pm.
     * The file that actually runs is `py`, the first argument to execv. */
    out[n++] = exe;
    out[n++] = boot;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0) out[n++] = argv[i];
    out[n] = NULL;

    execv(py, out);
    fail("The Python environment would not start.");
    return 70;
}
CSTUB

cc -O2 -Wall -o "$B/Contents/MacOS/$EXE" "$WORK/stub.c" 2>"$WORK/cc.err" \
  || bye "The launcher would not compile:

$(head -5 "$WORK/cc.err")

Nothing was built." 1
chmod +x "$B/Contents/MacOS/$EXE"
step "launcher compiled"

cat > "$B/Contents/Resources/boot.py" <<'BOOT'
"""What LTC Player runs when you double-click it.

Started by the bundle's launcher with the folder the app lives in as the
working directory, which is where the show file, the sequences and the
virtual environment are.

An app has no window and no Terminal, so anything this program prints goes
nowhere and any error it hits disappears. On 2026-09-15 that produced the
worst possible symptom: the icon animated, and then nothing, with no way to
find out why. So everything it says is written to a log, and anything that
stops it starting is put on the screen.
"""
import os
import subprocess
import sys
import threading
import time
import traceback

folder = os.getcwd()
sys.path.insert(0, folder)

PORT = "7878"
URL = "http://127.0.0.1:" + PORT + "/"
LOG = os.path.expanduser("~/Library/Logs/LTCPlayer-start.log")


def note(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")
    except Exception:
        pass


def say(msg):
    """On the screen, because a log nobody knows about is not a report."""
    note(msg)
    clean = msg.replace("\\", "/").replace('"', "'")
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             'display dialog "LTC Player\n\n' + clean + '" '
             'with title "LTC Player" buttons {"OK"} with icon stop '
             'default button 1'],
            timeout=300)
    except Exception:
        pass


# --selfcheck is used by the build script to prove, through a real launch,
# that this app starts and can load everything it needs. The answer is
# written OUTSIDE the bundle: a file written inside would break the
# signature the check is there to verify.
if "--selfcheck" in sys.argv[1:]:
    import json
    result = {"folder": folder, "python": sys.executable, "ok": False}
    try:
        import numpy, sounddevice, zstandard        # noqa: F401
        from ltcplay.cli import main                # noqa: F401
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    with open(os.path.join(folder, ".ltcplay_appcheck"), "w") as fh:
        json.dump(result, fh)
    sys.exit(0 if result["ok"] else 1)


def open_page():
    """Open the browser once the port actually answers.

    The engine opening its own browser was swallowing every failure, so a
    page that never appeared looked exactly like an app that never started.
    And opening one before the port is up lands the browser on a connection
    error that it then sits on."""
    import socket
    for _ in range(150):
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", int(PORT)))
            note("the engine is listening on " + PORT)
            break
        except OSError:
            time.sleep(0.1)
        finally:
            try:
                s.close()
            except Exception:
                pass
    else:
        say("The show page never came up.\n\nThe engine started but nothing "
            "is listening on port " + PORT + ". Something else may be using "
            "it. There is a full record in:\n\n~/Library/Logs/"
            "LTCPlayer-start.log")
        return
    try:
        subprocess.Popen(["/usr/bin/open", URL])
        note("opened " + URL)
    except Exception as e:
        say("The show is running at:\n\n" + URL + "\n\nbut the browser "
            "would not open by itself (" + repr(e) + "). Type that address "
            "in yourself.")


try:
    from ltcplay.cli import main
except Exception:
    say("The show program could not be loaded.\n\n"
        + traceback.format_exc()[-1200:])
    sys.exit(70)

args = [a for a in sys.argv[1:] if a != "--selfcheck"]
serving = not args
if serving:
    args = ["serve", "--port", PORT, "--bind", "127.0.0.1", "--no-browser"]
    threading.Thread(target=open_page, daemon=True).start()

# Everything the engine prints, including the reason it could not start,
# goes to the log. Without this it goes to a terminal that does not exist.
rc = 70
try:
    with open(LOG, "a", buffering=1) as fh:
        fh.write("\n" + time.strftime("%Y-%m-%d %H:%M:%S")
                 + "  starting in " + folder + "\n")
        sys.stdout = sys.stderr = fh
        try:
            rc = main(args)
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
except SystemExit:
    raise
except Exception:
    say("LTC Player stopped with an error.\n\n"
        + traceback.format_exc()[-1200:])
    sys.exit(70)

if serving and rc:
    tail = ""
    try:
        tail = "".join(open(LOG).readlines()[-8:])
    except Exception:
        pass
    say("LTC Player could not start.\n\n" + tail)
note("exited with " + repr(rc))
sys.exit(rc or 0)
BOOT
step "boot script written"

cat > "$B/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LTC Player</string>
  <key>CFBundleDisplayName</key><string>LTC Player</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXE</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>LTC Player listens to an audio input to read SMPTE timecode and run the lighting show.</string>
  <key>NSDesktopFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
# LSUIElement: no Dock icon and no menu bar. This program has no windows of
# its own -- its screen is the web page -- and an app with a Dock icon and no
# event loop is reported by macOS as "not responding" within seconds. It is
# still called "LTC Player" in Activity Monitor, which is where you quit it.
step "Info.plist written"

if [ -d "$HERE/$EXE.iconset" ] && command -v iconutil >/dev/null 2>&1; then
  if iconutil -c icns "$HERE/$EXE.iconset" -o "$B/Contents/Resources/icon.icns" \
       2>/dev/null; then
    step "icon built"
  else
    step "icon could not be built; carrying on without one"
  fi
else
  step "no icon source beside this script; carrying on without one"
fi

# ------------------------------------------------------- sign, then nothing
if ! codesign --force --sign - --identifier "$BUNDLE_ID" --timestamp=none \
      "$B" >"$WORK/sign.err" 2>&1; then
  bye "codesign refused to sign the app:

$(head -5 "$WORK/sign.err")

Nothing was built." 1
fi
step "signed"

if ! codesign --verify --deep --strict --verbose=2 "$B" >"$WORK/ver.err" 2>&1; then
  bye "The app does not verify after signing, which means macOS would refuse
to launch it:

$(head -5 "$WORK/ver.err")

Nothing was built." 1
fi
step "signature verifies"

# ------------------------------------------------------------------ install
rm -rf "$HERE/$APP" 2>/dev/null
if ! /usr/bin/ditto "$B" "$HERE/$APP"; then
  bye "The app was built but could not be copied into this folder." 1
fi
# ditto preserves the signature; prove it rather than assume it.
if ! codesign --verify --deep --strict "$HERE/$APP" >/dev/null 2>&1; then
  rm -rf "$HERE/$APP"
  bye "The app lost its signature on the way into this folder. Nothing was
installed." 1
fi
step "installed as $APP"

# --------------------------------------------------- the test that matters
# Signing and verifying prove the bytes are consistent. They do NOT prove
# macOS will LAUNCH it: that goes through LaunchServices, which applies rules
# codesign never sees. On 2026-09-14 an earlier bundle signed, verified, and
# was then refused as "damaged" at the next reboot, hours after the build said
# it was fine. So: actually open it, the way a double-click does, and wait for
# it to say it got there.
rm -f "$HERE/.ltcplay_appcheck"
open -a "$HERE/$APP" --args --selfcheck >"$WORK/open.err" 2>&1 || true
ok=""
for _ in $(seq 1 40); do
  [ -f "$HERE/.ltcplay_appcheck" ] && { ok=yes; break; }
  sleep 0.25
done
if [ -z "$ok" ]; then
  WHY=$(head -3 "$WORK/open.err")
  rm -rf "$HERE/$APP"
  bye "The app was built and signed, but macOS would not run it:

${WHY:-It started and never reported back.}

The app has been removed rather than left for you to find out on a show
night. Nothing else changed: 'Web ltcplay.command' still works." 1
fi
if ! grep -q '"ok": *true' "$HERE/.ltcplay_appcheck"; then
  WHY=$(sed -n 's/.*"error": *"\([^"]*\)".*/\1/p' "$HERE/.ltcplay_appcheck")
  rm -rf "$HERE/$APP"
  rm -f "$HERE/.ltcplay_appcheck"
  bye "The app launches but cannot load what it needs:

${WHY:-unknown}

Run 'Install ltcplay.command' again, then this. Nothing was installed." 1
fi
rm -f "$HERE/.ltcplay_appcheck"
step "macOS launched it and it loaded the show code and the audio library"
# Worth being exact about what that just proved. 'open' was run from this
# Terminal window, and an app launched that way can inherit the folder
# permissions Terminal already has. A double-click in Finder does not. So
# this check catches a broken signature, a bundle Gatekeeper refuses and a
# missing library -- it does NOT prove Finder can reach the show folder.
# That caught us out on 2026-09-15: the build passed and the double-click
# failed. The app now says exactly which folder it could not open.
step "note: a Finder double-click is the real test; this one runs from here"

# ------------------------------------------- what a COPY of it would meet
# Built here, this app has no quarantine mark and opens on a double-click.
# The moment it is AirDropped, emailed or downloaded it gets one, and an app
# without an Apple developer signature is then refused. Say so now, with the
# actual verdict, instead of letting somebody discover it in a park at dusk.
COPYNOTE="A copy of this app sent to another Mac will be refused until the
quarantine mark is cleared. Build it on that Mac instead."
if command -v xattr >/dev/null 2>&1 && command -v spctl >/dev/null 2>&1; then
  Q="$WORK/q"
  mkdir -p "$Q"
  /usr/bin/ditto "$HERE/$APP" "$Q/$APP" 2>/dev/null
  xattr -w com.apple.quarantine \
    "0081;00000000;LTCPlayerBuildTest;" "$Q/$APP" 2>/dev/null
  if spctl -a -t exec "$Q/$APP" >/dev/null 2>&1; then
    COPYNOTE="A copy of this app is accepted by Gatekeeper on this Mac even
after a transfer, which is unusual and may not hold on another one. Building
it on the Mac that runs it is still the reliable way."
  fi
fi

echo
echo "Built: $HERE/$APP"
codesign -dv "$HERE/$APP" 2>&1 | sed -n 's/^/  /p' | head -3
cat <<EOF

HOW TO USE IT

  Double-click 'LTC Player.app'. No window opens; it starts the engine and
  the show page opens in your browser at

      http://127.0.0.1:7878/

  It starts IDLE. Nothing goes to the rig until you press Run.

  To quit it: press Stop on the page, then quit it in Activity Monitor,
  where it is listed as "LTC Player". Or run 'Restart ltcplay.command'.

MICROPHONE

  The first time you press Run with timecode arriving, macOS should ask for
  microphone access for LTC Player. Say yes. There is no way to read
  timecode without it.

  If no dialog appears and LTC IN does not count, open System Settings,
  Privacy and Security, Microphone, and switch LTC Player on.

ABOUT COPYING IT

  $COPYNOTE

EOF
read -r -p "Press return to close. "
