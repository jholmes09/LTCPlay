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
renders and no PIN, password, token or key files. Take the bundle before
you restart or reinstall, so the logs that show the fault are still in it.

## Users, sign-in and the audit log

The first start of this version makes Jeff and Andy (admins) and Dallen,
Dave and Jared (operators), with no password: each picks their own the
first time they sign in, from the user button at the top right of the
rack screen. Nobody has to sign in for Abort, Hold, Stop or a disarm.
Every press is written to `logs\audit` in the settings folder, under the
name signed in or "unsigned". The nightly upload to Jeff's server is off
until an admin sets it on the Users screen. USERS.md (in the program
folder) says all of it in plain words.

## The first time it runs (a fresh PC)

LTC Player writes three files the first time it starts on a PC that has
none of them, so flamesafe, the Stream Deck program and the rack screen
all come up instead of a bare page (before 2026-10-18 a fresh install
started the engine alone and the Stream Deck "never connected"):

- `flamesafe.json`: the shipped example, sending to this PC only
  (127.0.0.1), three groups, unconfirmed. Nothing can reach a flame
  controller from it. Before any flame hardware is connected, set the
  real destination, universe, groups and link key, and set confirmed to
  true.
- `ltcplay_schedule.json`: the example's show times. Set the real ones on
  the rack screen: Programming, "Schedule: the show times".
- `ltcplay_fire_ice.json`: `scheduler_performs` false (the scheduler
  decides but starts nothing) and the flamesafe config above.
- `ltcplay_announce.json` (2026-10-26): the operator announcements, with
  NO output device yet and the three recordings expected in the
  `Announcements` folder beside it (delayed.wav, cancellation.wav,
  cannot_continue.wav). The engine runs announcements from it by itself;
  until the device's exact name is set and the files are copied there,
  the rack screen says "Output device: not set" and "Recording missing"
  and nothing plays. The system default output is never used.
- `ltcplay_remote.json`, only when there is none: the booth speaker block
  (`booth_alert`) with no device, so the booth alert notes stay off until
  it is set.

All of them live in Start menu, LTC Player, "Settings and logs". A file that
is already there is never touched. The rack screen's "Getting started"
card lists what is still missing (a show folder with a show file in it,
the schedule) and where to click; it goes away once both exist.

Arming flames from the rack screen (or an iPad) is OFF until you turn it on: on the show PC's rack screen, Programming, "Arming from a screen", Turn on (an admin once users exist; it is `"screen_arming": true` in ltcplay_remote.json in "Settings and logs"; the card lists everything it also lets iPads do: purge, unlock, laser tests and laser arming).

## The show folder

The engine serves the folder named by `show_folder` in showpc.json
(Start menu, LTC Player, "Settings and logs"). Change it there, or on
the Programming screen's "Show folder" card. LTC Player restarts the
engine with the new folder once no show is running. That card says which
folder the engine serves, and the rack screen says it in red while
showpc.json names another.

## Show PC setup the soak taught us (2026-10-21)

- **One address per Art-Net receiver.** Every program that receives
  Art-Net needs its own address. MadMapper counts twice: its timeline
  sync and its DMX input are each a receiver. Two receivers on one
  address starve one of them (BEYOND never chased timecode). What worked:
  two Windows loopback adapters, "ShowNet MadMapper" at 10.250.1.1 and
  "ShowNet BEYOND" at 10.250.2.1, with the show file's
  `clock.artnet.nodes` set to `"MadMapper": "10.250.1.1"` and
  `"BEYOND": "10.250.2.1"`. Add an adapter in Device Manager: Action,
  Add legacy hardware, Network adapters, Microsoft, "Microsoft KM-TEST
  Loopback Adapter"; then give it its address and name.
- **MadMapper's Sync Source.** After reopening the project it can be
  empty. Set it to Art-Net and save the project.
- **48 kHz.** Set the interface to 48 kHz in its own app (PreSonus
  Universal Control). Otherwise Run is refused with "will not play N
  output(s) at 48000 Hz".
- **Windows Time running.** With the service stopped the clock was 2.7 s
  behind. services.msc, Windows Time, Startup type Automatic, Start. The
  Morning page's Engine checks says it loudly when the clock is off by
  more than 0.5 s and the service is stopped.
- **Pagefile.** 15.8 GB of memory, the engine's show audio 1.0 to 1.5 GB,
  MadMapper about 3.4 GB: with a 2 GB pagefile about 84% of the commit
  limit was in use. Set it to System managed (sysdm.cpl, Advanced,
  Performance Settings, Advanced, Virtual memory) or at least 16 GB.
- **Render after every layout change.** A render older than
  xlights_networks.xml plays no flame cues. The rack screen, the Stream
  Deck and the Morning page say CUES FAULT and name the files.

## Which mode am I in, and how do I minimize or close the window

- The header's MODE pill says it on every screen: SHOW (a show file is
  running for real; the scheduler starts each show at its time),
  PROGRAMMING (a rehearsal is running; the scrubbing controls work and the
  scheduler starts nothing) or NOT RUNNING (nothing reaches the rig until
  Run is pressed on the Programming screen). The small "i" button next to
  it explains, and shows this LTC Player's version.
- Start menu, LTC Player opens the rack screen as an ordinary Edge app
  window, maximized on the show monitor, with Minimize, Maximize and
  Close in its title bar (no address bar or tabs). Alt+F4 also closes it,
  or press the "i" button, "Close this window". Closing the window never
  stops the show: the engine runs on its own. A folder opened from the
  page (Open the show folder, Open the settings folder) is brought to the
  front; if it is not showing, it is on the taskbar. Start menu, "Rack
  screen in a normal window" opens the same page in the default browser.

## Every day

- LTC Player starts by itself when the PC signs in and opens the rack
  screen maximized on the show monitor. If one of its programs stops, it is started again
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
