# ltcplay, one page for the person running it

You are chasing pre-rendered light sequences to the timecode coming off the
audio feed. The program does not play audio, does not start anything, and does
not decide anything. Timecode rolls, lights follow. Timecode stops, lights go
back to the preshow look.

## Three ways in

**The app.** Double-click **LTC Player.app**. Nothing appears on screen except
your browser, which opens onto the show page. This is the one to use. It is
also the one macOS will ask about when the show needs the microphone, because
it is the only one with an identity of its own to ask about.

There is no window to close and no Dock icon. To stop it: press Stop on the
page, then double-click **Restart ltcplay.command**, which finds it and ends
it whether it was started by the app or from a Terminal window. In Activity
Monitor it may be listed under the name of the Python it runs rather than as
LTC Player, so Restart is the reliable way.

If there is no app in the folder, build it: double-click **Build LTC Player
app.command**. It takes a few seconds and only has to be done once per Mac.
It is built there rather than copied in because macOS refuses an application
that arrives from another machine until its quarantine mark is cleared.

**A web page.** Double-click **Web ltcplay.command**. A Terminal window opens
and your browser opens onto it. Everything below is on that page as buttons.

The Terminal window is the program. The page is a window onto it. Closing the
browser does NOT stop the show; Ctrl-C in that Terminal window does. If the
page ever says it has lost contact with the engine, the rig is still being
driven, and the Terminal window is where to look.

**A terminal menu.** Double-click **Run ltcplay.command** instead. Same engine,
same order of operations.

## Before the first run of the day

Double-click **Run ltcplay.command**. Everything is on that menu, in the order
you need it. Set the mode first, with `m`: REHEARSAL means a stop holds the
lights where they are, SHOW means a dead feed sends the rig to its preshow
look. Then work down the list.

**1, find the timecode.** With timecode running, this listens to the audio
interface, the headphone socket and the built-in mic, and tells you which input
has timecode on it and at what level. An interface has several inputs and
timecode is on exactly one; listening to the wrong one looks identical to a
dead cable. Do this once when the rig is first patched, and again any time the
interface changes.

**2, set the input.** Pick it from the list. It is written next to the launcher
and every run uses it from then on, so this is the last time you have to think
about it. The menu shows the current one at the top.

**3, watch the timecode.** Numbers should count up. If nothing appears here,
nothing else will work: it is the cable, the input, or the level. When you quit
it, it prints the frame rate it measured and the two lines to put in the show
file. Do that once, the first time you ever run against this source.

**4, validate the show.** Every sequence opens, no cue overlaps the one before
it, an audio input exists, every controller answers a ping. The page's
Validate does the file and layout checks but does NOT ping: for the ping,
use menu item 4 in the Run window.

**5, rehearse.** It follows timecode and still sends nothing, so you can watch
the screen do the right thing before the rig does anything at all.

**6, run.** It shows you the validation again and makes you type RUN.

**Only one copy at a time.** Rehearse and Run are a second copy of the show
program, and only one copy may run on a machine. While the LTC Player app,
the autostart engine or a Web window is running, they stop at once and say
what is running and how to stop it. Use the page of the one that is running,
or stop it first (quit the app, turn autostart off with `Autostart
ltcplay.command` then R, or Ctrl-C in its window) and then rehearse. A second
`ltc deck` is refused the same way.

**OTHER SENDER on the Stream Deck.** For about half a second after every
ltcplay restart the deck's flame keys read OTHER SENDER, and a cycle in that
half second does not count; cycle again. If it stays, a second copy of the
show program is talking to the flame safety program: every group has been
disarmed, and nothing can arm until it is gone.

**Alerts.** When something needs a person (the Stream Deck unplugged, a show
that did not start), a banner appears at the top of the rack screen and on
every remote page, in plain words, and a short note plays on the booth
speaker if one is set up (ltcplay_remote.json, booth_alert; off until it
is). It stays until you press Acknowledge on the banner or any key on the
Stream Deck. Acknowledge only clears the banner: nothing on the rig changes.
While an alert is open the Stream Deck's Start key reads ACK / ALERT in
blue; it goes back to START NOW once the alert is cleared. An audio alert,
or an alert the booth speaker could not play, also pulses the floor pixels
blue (alert_pixels in ltcplay_remote.json) until it is acknowledged.

**LASERS on the show screen (Fire & Ice).** Under the flame lamps and
the Disarm all flame groups button is a violet LASERS strip, apart from
everything for the flames. Disarm all lasers (right) is one click, no
confirm, signed in or not, from the rack screen and every iPad, whatever
else the page is doing: the lasers are blanked, BEYOND's laser output is
switched off and every laser is disarmed. Next to LASERS it says how many
are armed. Each laser has a chip (ARMED in amber, off, or locked) and so
does each laser group: click an armed one to disarm it at once; click one
that is off to arm it, with the same confirm and the same rules as the
Startup page. When an arm is refused the strip says why in
plain words. Disarm all flame groups does not disarm, blank or switch
off the lasers (it still ends a laser calibration test, as it always
has), and the Stream Deck has no laser key.

**Startup (Fire & Ice; it was called Morning and Shutdown).** The rack
screen has a Startup page (the button in the header). Shutdown is its own
labelled section on it: one press opens the Shutdown checklist. On it: the laser arm (every
laser starts DISARMED each time the engine starts; Arm on a laser, or Arm
on a laser group, then Arm in the confirm box; Disarm all lasers is one
click and always works, signed in or not; BEYOND's laser output comes on
only once EVERY laser is armed and none is locked out, because BEYOND's
only known command is for all lasers at once; Abort, Hold and the night
closing disarm every laser, and after Resume or Reset they stay dark until
you arm them again; the Lasers pill at the top of the rack screen says how
many are armed); one calibration test button per laser (the laser has to be
armed first) (ON keeps the grid up at low power until pressed again;
how BEYOND is told is not set yet, so each button says so until Jeff fills
in `lasers` in ltcplay_morning.json); the purge (click a machine or a group
to arm it, then hold the Space bar, or the Stream Deck's FIRE key, to fire;
let go and it stops; nothing fires while a show is live or an Abort
stands); lock out of any one machine or laser (the L button on it: one tap
locks it out, one tap on a locked one unlocks it, with no pop-up either
way; a line under the machine map, or under the lasers, says what was
done, with Undo for 5 seconds; a second tap on the same L within about a
second does nothing, so a double tap never locks and then unlocks; a
machine cannot be unlocked while its flame group is armed or being armed:
the line says "Disarm <group> first, then unlock <machine>"; a refusal is
said there in plain words; a lock stays across
restarts until unlocked here; a locked unit
cannot be armed or fired from anywhere and show cues skip it; a banner on
every screen says what is locked); and the Startup and Shutdown
checklists (pick your name, tick each step; every tick is recorded for
audit and the day's record is a PDF you can download, or Export all).
While the page is open the Stream Deck's Start key is FIRE and reads
PURGE ARMED on an armed group's key; it goes back to normal when you
leave the page. MORNING.md says how it all works and where every setting
lives.

**Stream Deck unplugged.** Unplugging or replugging the deck changes nothing
on the flame groups: whatever was armed stays armed, whatever was off stays
off, and the deck process keeps telling the flame safety program so. A
"Stream Deck" alert comes up (banner, booth note) with Acknowledge and Disarm
all; the groups wait for you, there is no timer. A key that was held down at
the moment of the unplug is forgotten and has to be pressed again. The
exception is the deck PROGRAM itself dying (a crash, not a cable): the flame
safety program then hears nothing for half a second and disarms every group,
and the program restarts in about 2 s with every group OFF. The hardware
e-stop is still the only emergency control.

## The screen

```
  LTC IN     01:14:22:11     LOCKED
  PLAYING    01:14:22:11     show            sync +2 ms
```

**LTC IN** is the last timecode that actually arrived. It freezes the instant
the feed stops. **PLAYING** is where the show is. When they read the same and
the state says LOCKED, you are synced. Nothing else on the screen matters more
than those two lines.

| state | what it means | what the rig is doing |
|---|---|---|
| LOCKED | timecode is arriving and moving | following it |
| PARKED | timecode is arriving but not moving, so they paused | holding that exact frame |
| FREEWHEEL | timecode stopped a moment ago | still running on its own clock, for 2 seconds |
| LOST | timecode has been gone 2 seconds | whatever the run was started with: preshow look, held frame, or dark |

PARKED is a pause, not a fault. The source is fine, the show is standing
still, and it picks up the moment they hit play.

**rate in** must match **timeline**. `29.97 non-drop` against `30 non-drop` is
not a rounding difference: it is 0.1%, about two seconds of lights-behind-music
by the end of a half hour set. If they disagree the screen says so in red at
the bottom, in words, with the fix.

**UP NEXT** names the next sequence and the timecode it starts at.

## When something is wrong

Anything wrong is printed in red at the bottom of the screen as a sentence.
Read that first. The short version:

- **"The show folder in this timeline is ... which does not exist."** The
  timeline was written on another machine. It has found the folder anyway and
  is carrying on; tell whoever owns the file to fix `show_dir` in it.
- **Nothing decodes.** Menu item 1. It will tell you whether the input is
  silent (wrong socket, or timecode is on a different input of the interface)
  or carrying audio that is not timecode. A MacBook headphone jack is only an
  input while a TRRS adapter is plugged in.
- **Everything reads silent and the input is a Dante device.** A Dante USB I/O
  Module carries only what the network was told to send it. Open Dante
  Controller and subscribe the timecode transmitter to this Mac's receiver.
  Until that subscription exists the channel is genuinely silent and no setting
  here will change it.
- **The input row says "silent" or "very low" or "clipping".** That is the trim
  on the interface, not this program. Aim for the meter sitting around half.
- **"No audio has arrived from <device>."** The interface, not the timecode
  generator. Check it is plugged in and powered. The input rebuilds itself once
  a second until it comes back, so plugging it back in is enough.
- **LOST while timecode is clearly running.** The audio feed to this machine
  has dropped. The rig is on the preshow look, which is what you want.
- **"No packet has been accepted by the network."** Network, not this program.
  Cable, switch, or the Mac's network interface. The program rebuilds its
  socket by itself every second until it works again; nothing to restart.
- **Wrong frame rate warning.** Fix `"fps"` in the timeline file and restart.
  Do not ignore it, and do not fix it by nudging the offset.
- **The rig is lit but not moving.** Look at the PLAYING line. If it is
  counting, the program is fine and the problem is downstream.

## Stopping

On the page: Stop and black out. In the terminal: Ctrl-C. Either way the rig is
blacked out on the way out, and so it is by a kill or a shutdown.

Closing the browser tab stops nothing.

## The log

Every run writes `ltcplay.log` beside the timeline: every cue change, every
state change, every jump, every network fault, with the timecode it happened
at. After a bad run, that file is the answer.

## When the timecode never comes

If the LTC line is dead and the music is playing anyway, press **GO from the
top** on the page. The show runs off this Mac's own clock from wherever you
tell it. The feed keeps being read and displayed, so you can see it come back;
**Back to timecode** hands the show over when it does.

This is the thing to reach for when the alternative is a preshow loop in front
of an audience for thirty minutes.

## The backup: letting the Advateks play their own scenes

This is not how the show normally runs, and you should not reach for it unless
the normal way has stopped working. Normally this Mac reads each sequence and
streams every frame to all 22 controllers. That is the only mode where the
whole rig is running one picture off one clock.

The six Advatek PixLite boxes can also hold the show on their own SD cards, as
recorded SHOWTime scenes. The button on the page marked **Hand the Advateks
their own recorded scenes** switches to that: this Mac stops sending pixels to
those six addresses and instead sends one sACN trigger at the top of each
cue to start that cue's recorded scene. The other sixteen controllers keep
being streamed to from the sequence exactly as before, because they have no
such playback and would otherwise go dark.

**When to use it.** The renders will not open, a sequence is corrupt, the
Mac is struggling to keep up, or anything else that stops the six biggest
boxes getting clean frames. Press the button. The Advateks carry on off their
own cards.

**What you give up, and it is not small.**

- This Mac cannot resync, pause or nudge a scene once it has started. The
  trigger starts it and the box runs it on its own clock.
- A jump restarts a scene from ITS beginning, which will not line up with the
  audio. GO and the skip buttons still move the sixteen live controllers
  correctly, so the two halves of the rig will disagree until the next cue.
- Over a long song the box's clock and the audio drift apart on their own.
  Nothing on this Mac can correct it.

**The one that will catch you.** A recorded scene is a photograph of a render.
If anyone re-renders a sequence in xLights and does not re-record the scenes
onto all six cards, this mode plays the OLD version on the Advateks while the
sixteen live controllers play the new one, in time with each other, looking
almost right. No software anywhere can detect that. If the renders have
changed since the scenes were recorded, this backup is not available to you.

**Before a season, prove it works.** Stop all output, arm the mode, jump to
cue 1 from the page and watch the Stage box.

The settings all live in the `"trigger"` block of the show file, and the page
prints what it is actually using, so check that first:

    "protocol": "sacn"          sACN, which is what the boxes listen for
    "universe": 6999            counted from 1 in sACN, so 6999 is 6999
    "dest": ["10.0.0.100", ...] one packet per controller

Set `"dest": "multicast"` instead and it works out the E1.31 group for the
universe by itself, which for 6999 is 239.255.27.87. Multicast is the usual
choice and it needs the switch to pass it. Naming the six addresses is the
predictable one and needs nothing from the network at all. Either is correct;
if the boxes do not respond to one, try the other.

Turning it off puts every controller back on the live stream immediately.

## The iPad

Once, on the show machine: open the page (http://127.0.0.1:7878/remote),
press Programming, and pick this machine's show Wi-Fi address under "Show
network". Then start it with the show network on (Run ltcplay, answer y to
the iPad question). There is no PIN, and signing in is optional (USERS.md): anything on the show
Wi-Fi that can reach that address can use the page, so keep the show Wi-Fi
to the crew.

On the iPad, on the show Wi-Fi, open `http://<that address>:7878/`. It goes
straight to the show screen. Everything you need during a show is on that
one screen, with no scrolling: the show state, the timecode, the current
show and how long is left in it, the next show, Start now, Hold, Resume,
Reset, the flame groups and the status strip along the top (deck,
flamesafe, audio, video, lasers, pixels). ABORT is a red stop sign in the
bottom right corner, on every screen, always in sight (it stays there while
the page scrolls; there is no gesture to find it). The page keeps a strip
free down its right edge, so no other button is ever under the sign. Point
at it (or reach it with the Tab key) and a line beside it says what a press
does now, for example "Nothing to abort right now". Abort is a DOUBLE TAP,
with no pop-up: the first tap only makes the sign bigger and flashing, with
TAP AGAIN TO ABORT on it (it grows upward, inside the free strip, so it
never covers another button), and sends nothing; a second tap anywhere on
the big sign while it is big (2 seconds) aborts, exactly as before. Two
taps closer together than about a sixth of a second count as one tap (a
bounce), so tap again. Any finger works, two
different fingers too, or the mouse twice (a double click), or Enter or
Space twice (holding the key down counts once); a finger then the mouse
counts as one tap. After 2 seconds
with no second tap the sign goes back by itself and nothing happened. It
works signed out, and with the connection lost it still sends and says
plainly if the engine did not answer. The Stream Deck's Abort is unchanged. If you pinch-zoom an iPad in on part of the page, the sign
is only in view when the zoomed part includes the bottom right corner: zoom
back out to find it. Start now asks once too. Hold, Resume and Reset are one
press. Disarm all flame groups is one press and never asks. It is a plain
disarm: every group goes off, and a group is armed again by holding its
button, with no Reset. Abort is different: it stops the
show, and every group then needs Arm again (let go, then hold) after Reset.
If the Stream Deck program is not running, or flamesafe does not report
every group off within a second, Disarm all flame groups disarms them the
way Abort does instead, and says so.

The lasers have ONE pill in the status strip, one plain sentence: who is
armed and BEYOND's laser output, for example "Lasers: all disarmed, output
dark", "Lasers: 2 of 3 armed, output off" or "Lasers: all armed, output allowed" (allowed: BEYOND may light them;
the show decides when)
("not known: no contact" while the page has lost the engine). A warning
about what the show last sent BEYOND is said in the sentence too, for
example "...; lit? link unknown". Point at it
for every detail: which lasers are armed, why the output stays off, why an
arm would be refused, and what the show last sent BEYOND. It never says lit
while BEYOND is not running: it reads LINK LOST first, in red, and if
BEYOND was running and stops, an alert says so. BEYOND never answers back, so "lit, unconfirmed" means the
lasers were sent lit and BEYOND is running; it cannot prove the beams are
on. Off the show PC, or with BEYOND on another machine, the pill's details
say "lit? link unknown".

The video chip says whether MadMapper is ready before any show plays. On
the show PC it reads "MadMapper running" (green) while the MadMapper
program is running, and "MadMapper NOT RUNNING" (red) when it is not; if
MadMapper was running and stops, an alert says so, once each time. Once
the show has sent the video something, what was last sent is added
("MadMapper running, lit" or "black"). This only checks that the program
is running: MadMapper never answers back, so it cannot tell whether the
screens show the picture. If the show file has a MadMapper heartbeat, a
playing show also reads "heartbeat OK", or "MadMapper running, no
heartbeat" (amber) when MadMapper's heartbeat stops arriving; between
shows and on Hold a silent heartbeat is normal and nothing is said. Off
the show PC, or with MadMapper on another machine, it cannot be told:
the chip reads "not sent yet" (grey) until a show sends something, and
its tooltip says why.

"Left in this show" counts down the show that is playing. On Hold it stands
still, because the show's own clock is stopped. Between shows the next
show's countdown is shown instead. If the show's length is not known,
nothing is shown rather than a guess.

Schedule (it was called Tonight) shows the night's shows on one screen; tap
a show to see its notes (why it was moved, skipped or added, or "No notes";
tap again to close). Programming has the scrubbing controls and, on the
show machine only, the Show file card (the Show and Programming switch, the
show file, Run and Stop), the Stream Deck operator and the show network.
Show: the scheduler starts each show at its time and scrubbing is off.
Programming: a rehearsal; play from, jump and loop work and the scheduler
starts no show. To switch, press Stop, pick the other side of the switch,
then Run. While a scheduled show is live, Programming is locked and the
switch says so. Back to the show returns to the show screen.

Fire flames while scrubbing, on the Programming screen, lets the show's own
flame cues fire while you play, jump and loop in Programming. It is off
every time LTC Player starts, and only an admin on the show machine can
turn it on. While it is on, a red FLAMES LIVE IN REHEARSAL bar is on every
screen with its Turn off button. LIVE_FLAME_PROGRAMMING.md has the rules.

The time zone on the schedule card is a list of plain names (for example
Mountain Time (Denver)); the show's local time is shown under it. The
engine checks the zone again when the schedule is saved.

Each flame group has ONE button, and its colour is the group's state:
green armed, bronze off, amber held back (flashing: re-arm needed after an
Abort, a fault or a lost link), WAIT (the re-arm wait). While the group is
off the button is Hold to arm: press and hold it, as before. While the group
is armed or asked for, the button still says the state in big letters
(ARMED, ARM AGAIN, WAIT), with DISARM under it and "One press disarms": one
press disarms that group at once. Arming is always a hold: the Enter key on
Hold to arm only says so. Lifting the finger that just armed a group never disarms it;
a new press does. ARM AGAIN reads "Disarm, then hold" on the button: press
it once, then hold it. The Stream Deck says ARM AGAIN on the key too.

Under each group's button a small line counts its machines: "armed 0 of 3
machines" while it is off, "armed 3 of 3 machines" once flamesafe has armed
it (every machine in a group arms together), with any machine locked out
said apart ("1 locked out"; it is left out of the count and never fires).
The wave flamer, with a safety per fixture, keeps its own count of safeties
up. If the Startup page's machine list cannot be read the line is left
off.

The button under the groups is two-way. While every group is off it reads
Hold to arm all flame groups, in green with a green outline: press and HOLD
it for the full hold, and every group this screen can arm is held to arm at
once, each by exactly the same rules as its own button. Let go early and
every hold still under way is let go; a group the Stream Deck already armed
during the hold stays armed (its button then reads Disarm). A
group that is locked out, still in its re-arm wait or refused stays out, and
the line beside it names each one and why. While live flame programming is
on with one group at a time (or turns on during the hold), or an Abort
stands, Arm all arms nothing, stops, and says so. While any
group is armed or asked for (one or several), or the lamps are not known, it
reads Disarm all flame groups: one press, as before. With arming from a
screen switched off it always reads Disarm all flame groups.

If the page shows a red CONNECTION LOST bar, the iPad has lost the engine.
The show carries on by itself and a Hold stays held. Start now, Resume, Reset
and the programming buttons go grey until it is back.

Arming from the iPad or the rack screen (when screen arming is switched on;
it is off until someone turns it on at the show machine: Programming,
"Arming from a screen", Turn on; while it is off the button reads "Arming
off"): press and HOLD a group's "Hold to arm" button until the bar fills and
the lamp goes green. If it is refused, the line beside Disarm all flame
groups and the controls' message line say why. Lift early and nothing happens. One hold at a time per
group. The Stream Deck does the arming for you, with all of its own rules,
so it has to be plugged in and running, and an operator has to be chosen
for it (Programming, Stream Deck operator). If the page loses the engine,
or the lamps are not live, a hold stops and the button cannot arm; it still
disarms with one press. Disarm on the page is one press. The journal says which screen or iPad did it. The
physical key switch on the flame system is still the master: off means
nothing fires.

## What it will not do

It will not play audio and it will not start on a schedule.

It will not fail over to a second machine. **If this Mac dies, the rig holds
its last frame.** That is the risk you are carrying every night; there is no
version of this program that removes it. What there is:

- `Autostart ltcplay.command` brings the engine back within seconds of a crash
  or a force-quit, idle, with the page ready.
- The launchers hold the Mac awake while they are open.
- The preshow look covers every gap, every lost feed and the interval **when
  the show file says `"gaps": "idle"`**. Without that key the gaps and the
  interval are black. Check it before a season.

Read the honest risk list at the end of README.md before a season.

## The rack screen's header: mode, version, getting started

The MODE pill in the header says which mode the engine is in, on every
screen, from the engine's own state and never from which screen is open:
SHOW (a show file is running for real; the scheduler starts each show at
its time; scrubbing is off), PROGRAMMING (a rehearsal is running; the
scrubbing controls work and the scheduler starts nothing) or NOT RUNNING
(no show file is running; nothing reaches the rig until Run is pressed on
the Programming screen). The small "i" button beside it opens a panel with
this LTC Player's release and build id (also in `/api/remote/status` as
`version`), the device, the mode sentence, and how to leave a full screen
window (Alt+F4, or its Close this window button on the show PC).

On a fresh machine the show screen carries a "Getting started" card with
the three things to set up and a button for each: the show folder (open
it, or choose another on the Programming screen; the show's music is the
stems the show file names under that folder, there is no separate music
folder), the schedule (Programming, "Schedule: the show times": the show
nights, the first show, the minutes between shows and when the last one
ends, saved to the schedule file and loaded at once, refused while a show
is live), and flamesafe with the Stream Deck (what the show PC's LTC
Player says about each program, and why one is not running). The card
goes away once a show file and a schedule exist.

The Abort confirm lists what the press will do from this screen and, in
red, what it cannot: with no flame link in the engine, the flames are NOT
disarmed from the screen (the Stream Deck's Abort and the hardware e-stop
are), and with no BEYOND OSC address the lasers are not blanked from here.
After the press, the engine's answer stays on the screen as a banner until
it is seen, never only as a one-line note.
