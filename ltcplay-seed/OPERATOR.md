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
