# LTC Player on the show PC: install, update, go back, send diagnostics

One page, for the person at the PC. Nothing here needs a Terminal or a
command prompt. Every step is a double-click or a Start menu item.

## Before the first install (once)

1. Sign in to the Windows account that will run the show. Install while
   signed in to THAT account: LTC Player starts by itself only when that
   account signs in.
2. Settings, Privacy and security, Windows Security, App and browser
   control: Smart App Control OFF. (LTC Player is not signed yet; with
   Smart App Control on, Windows may refuse to start it.)
3. The rest of the PC settings (no sleep, no Windows Update during show
   weeks, the account signs in by itself) are the checklist: Start menu,
   LTC Player, "Show PC checklist", or SHOW PC CHECKLIST.txt in the
   program folder. Do them once; they take 15 minutes.

## Install or update (under 5 minutes)

1. On the Releases page, https://github.com/jholmes09/LTCPlay/releases,
   take the newest release. Download "LTC Player Setup <version>.exe".
   If this PC is on the show network only, download it on another
   computer and bring it here on a USB stick.
2. Check the download (optional). Beside every installer on the Releases
   page is a ".sha256" file with the installer's fingerprint, also printed
   in the release notes. "Check for an update" (below) checks it for you.
   For a hand download, anyone with a command prompt can compare it:
   `Get-FileHash "LTC Player Setup <version>.exe" -Algorithm SHA256`.
   If it does not match, download again. Skip this if you prefer.
3. Make sure no show is running (the rack screen says what is on). The
   installer refuses while a show runs, and changes nothing.
4. Double-click the installer. Windows says "Windows protected your PC":
   click "More info", then "Run anyway". Click through Next. Leave "Start
   LTC Player automatically when this Windows account signs in" ticked.
5. The installer stops LTC Player the safe way (flamesafe sends its safe
   zeros), replaces the program, and starts LTC Player again. The rack
   screen comes back by itself within about 30 seconds and shows the new
   version in its header, next to the device name, like
   "LTC Player release bench-2026.10.15-abc1234, build 1a2b3c4d5e".
   That string must match the release you downloaded.

Your settings, logs, schedule, flamesafe.json and show files are never
touched by an install, an update, a rollback or an uninstall. They live in
Start menu, LTC Player, "Settings and logs" and in the show folder.

## Check for an update from the PC

Start menu, LTC Player, "Check for an update". It looks at the Releases
page, says whether this PC has the newest, and offers to download, check
the SHA-256 and install it. It refuses while a show is running. With no
internet on the show PC it says so and you use the USB stick way above.

## Go back to the version you had before

Start menu, LTC Player, "Roll back to the previous version". Every
installer that ran on this PC is kept in the program folder (the newest
three), and this runs the one before the current one. It stops LTC Player
the safe way first, refuses while a show is running, and leaves your
settings and show files alone. If it says no earlier version is kept,
download the version you want from the Releases page and run it like an
update.

## Something is wrong: send the diagnostics bundle

Start menu, LTC Player, "Diagnostics bundle (send this to Jeff)". It
writes one zip on the Desktop, named "LTC Player diagnostics <date>
<time>.zip", and opens the folder with it selected. Send that file. It
holds the logs, the settings, tonight's files, the night journals and the
version of every program. It holds NO show files, no audio or video, no
renders and no PIN or key files. Take the bundle before you restart or
reinstall, so the logs that show the fault are still in it.

## Every day

- LTC Player starts by itself when the PC signs in and opens the rack
  screen full screen. If one of its programs stops, it is started again
  within seconds; if LTC Player itself stops, within a minute.
- Start menu, LTC Player: starts it if needed and opens the rack screen.
- Start menu, Stop LTC Player: stops everything the safe way. Refused
  while a show is running. Stopped stays stopped until you start it again
  or restart the PC.
- NEVER use Task Manager's End task on flamesafe, ltcplay or LTC Player:
  End task stops the flame safety program without its safe zeros.
- The rack screen's banners say what needs a person. "Acknowledge"
  clears a banner and changes nothing on the rig.
- If the rack screen is dark red and says "The show engine did not
  start", read the sentence: it names the file or setting to fix. Fix it,
  then Start menu, LTC Player.
- If the header says MODIFIED SINCE, someone edited a program file by
  hand. Reinstall the release from the Releases page.

## If the PC has no audio interface attached

The show audio plays on the interface named in the show file (the
PreSonus or the Scarlett). With it unplugged, the rack screen's Audio
chip reads "not attached" and a banner names the device and the outputs
Windows can see. Plug it into the same USB port as always; the engine
finds it within half a minute. Nothing reaches the rig until it is there.
