# ltcplay

Chase xLights FSEQ sequences to incoming SMPTE/EBU linear timecode, natively on
a Mac. Written for rehearsals after xSchedule's macOS build was dropped, and
since taken into production on *Great Pumpkin Luminites* at Dollywood, 2026.

## What it is

A terminal program with a local web page onto it. It listens for LTC on an
audio input, works out where the show is, and streams the matching frame of the
matching sequence out over ArtNet or E1.31 to your controllers. It reads your
show folder's `xlights_networks.xml` for the channel map, so it stays aligned
with xLights without being configured twice.

## What it is not, and what that costs you

It plays no audio and it runs no schedule. More importantly, **it is one
process on one Mac**, and that is the whole risk:

- **No second machine takes over.** If this Mac dies, the rig holds its last
  frame. There is no failover and no protocol-priority spare. If that is not
  acceptable for your show, run FPP.
- **It needs the machine awake and the window open.** The launchers hold the
  Mac awake with `caffeinate` while they are running. Closing the window stops
  the show (cleanly, with a blackout).
- **Nothing starts it for you unless you ask.** `Autostart ltcplay.command`
  installs a login agent that brings the engine back within seconds of any
  crash or force-quit. It comes up *idle*; a person still presses Run.

What it does have, for when the night goes wrong: a preshow look that covers
every gap and every lost feed, a **GO** that runs the show off this Mac's own
clock when the timecode line is dead for good, a hold-this-look override, and
a reload that swaps a re-render in without stopping the chase.

Before a season, read **the honest risk list** at the end of this file.

## Install

Double-click **Install ltcplay.command** once. It builds a private Python
environment beside these files and installs numpy, sounddevice and zstandard
into it. Nothing is installed system-wide.

macOS will ask for microphone access the first time you run it. That prompt is
for the audio input; there is no way to read LTC without granting it.

## Use

Everything runs through **Run ltcplay.command**, which drops you at a prompt, or
from Terminal inside this folder:

    ./ltc devices                     # which audio inputs exist
    ./ltc init /path/to/ShowFolder    # write a starter timeline
    ./ltc check timeline.json         # validate without sending anything
    ./ltc monitor                     # show incoming LTC only
    ./ltc run timeline.json           # chase and output
    ./ltc gen test.wav --seconds 300  # make an LTC file to test against

Prove the chain in that order. `monitor` first: if timecode does not appear
there, nothing else matters. Then `run --no-output` to watch it follow cues
without touching the rig. Then for real.

## The timeline

    {
      "name": "GPL 2026 Set 1",
      "fps": 30,
      "show_dir": "/Users/you/Dropbox/PROJECTS/Dollywood/GPL26_xLights",
      "cues": [
        {"tc": "01:00:00:00", "fseq": "GPL 2026_Set 1_Opener.fseq"},
        {"tc": "01:02:30:00", "fseq": "GPL 2026_Set 1_MonsterMash.fseq"}
      ]
    }

`tc` is the incoming timecode at which that sequence's first frame plays. Cues
run until the sequence ends; after that the rig blacks out until the next cue.
`./ltc init` writes one of these covering every FSEQ in a folder so you have
something to edit rather than something to type.

Three optional keys matter for a real venue:

    "input": {                          // where timecode comes in
      "device": "MOTU M4",              // part of the name is enough
      "channel": 2                      // WHICH input of that box, 1-based
    },
    "fps": 29.97,                       // 23.976, 24, 25, 29.97 or 30
    "drop": true,                       // 29.97 only
    "idle": "Preshow Loop.fseq",        // plays whenever timecode is not running
    "gaps": "idle",                     // between cues: blackout (default), idle, hold
    "on_lost": "preshow"                // timecode gone: preshow (default), hold, blackout

`fps` is the one to get right. 29.97 and 30 count to the same number and print
the same digits, so a mismatch looks like nothing and drifts 0.1%: about two
seconds of lights behind music across a half hour set. Settle it with
`./ltc monitor`, which measures the rate against the sound card's sample
clock and prints the exact two lines to paste into the timeline. The run screen
checks the same thing and says so in words if the file is wrong, and `--fps
29.97 --drop` overrides the file for one run when there is no time to edit it.

`idle` is the preshow look. It loops from the moment the program starts until
timecode arrives, and returns two seconds after timecode stops. `gaps` says
what happens between cues while timecode is still running: `blackout` is the
default, `idle` keeps the preshow look up until the next cue, `hold` freezes on
the last frame.

## The input

A headphone jack is one device with one input. A USB interface is not, and
every difference is a way to lose a show quietly:

- **It has several inputs and timecode is on exactly one of them.** Listening
  to the wrong one is indistinguishable from a dead cable. `./ltc find`
  listens to every input of every device at once and tells you which one has
  timecode on it, at what level, at what rate.
- **Its index moves.** Unplug a webcam and the box that was device 3 is device
  2. Always set it by name; `"device": "MOTU M4"` survives a replug, `3` does
  not.
- **It owns its own clock** and may refuse 48000 outright. The rate is
  negotiated with the device and the one actually used is printed at startup.
- **It can be unplugged or bumped mid-show.** PortAudio does not raise when
  that happens, the callbacks just stop, which downstream looks exactly like
  the timecode generator being switched off. The input watches its own callback
  clock and rebuilds itself when it goes quiet, and the run screen says so.

The run screen carries a level meter for the chosen input. Timecode that is
too quiet decodes nothing and timecode that clips decodes badly, and both read
as "the feed is broken" unless you can see the level next to it.

    ./ltc devices          every input, usable ones separated from software
    ./ltc find             which one actually has timecode on it
    ./ltc input            choose it once; every run uses it from then on
    ./ltc input --clear    forget it and go back to the macOS default

`find` only listens to inputs that can carry timecode from outside the Mac: a
real interface, the headphone socket with a TRRS adapter in it, and the
built-in microphone. Your phone over Continuity and every device installed by
Teams, Zoom, Loom, a webcam app or a loopback driver are skipped, because a Mac
talking to itself cannot carry a timecode feed and scanning nine of them is
half a minute of watching zeros. `--all` includes them anyway. Anything the
program does not recognise is always treated as real hardware and scanned
first: hiding the one interface that matters would be far worse than a
cluttered list.

`./ltc input` writes `ltcplay_input.json` next to the launcher. That file
describes THIS Mac and what is plugged into it, which is why it beats the
`"input"` block in a show file: a show file travels between rigs and cannot
know where it was opened. When the two name different devices the run says so
rather than quietly picking one. `--device` on the command line beats both.

Device, channel and rate travel together. Input 2 of one interface means
nothing on another, so a channel never survives a change of device.

## Moving a show between machines

`show_dir` in a timeline is an absolute path, and an absolute path written on
one machine is meaningless on another. Rather than failing with a bare "no such
file" naming a folder nobody recognises, the loader tries progressively shorter
tails of the stored path against the places a show folder actually lives, and
says out loud when it substitutes one:

    note: The show folder in this timeline is /sessions/.../PROJECTS/Dollywood/
    GPL26_xLights, which does not exist on this machine. Using /Users/you/
    Dropbox/PROJECTS/Dollywood/GPL26_xLights instead, which matches the end of
    it. Fix "show_dir" in the timeline to stop this happening every run.

When nothing matches it says which line to edit, rather than printing a path
and stopping.

## Network audio: Dante, AVB, AES67

A Dante USB I/O Module or an AVIO adapter is not a cable input. It shows up as
an ordinary audio device but it only carries what the Dante network has been
told to send it, so an unsubscribed receiver reads as perfect silence. `find`
says so specifically when it sees one, because "level 0.00" on a Dante channel
means the subscription is missing, not that the cable is bad.

That routing is made in Dante Controller, by subscribing the timecode
transmitter's channel to this Mac's receiver. Nothing in this program can
create it.

## Pause, and losing the feed

These are three different things and the program treats them as three.

**The source is paused and still transmitting.** Most decks and DAWs that are
parked keep sending one frame number over and over. That reads as `PARKED`: the
signal is healthy, the show is standing still. The rig holds the exact frame
the source is sitting on and the clock is frozen, so hitting play resumes from
there with one snap rather than a hundred small corrections. Nothing to
configure.

**The source stops transmitting.** At the audio layer this is identical to a
pulled cable, and no program can tell them apart, so what happens is a policy
you choose rather than something the program guesses:

    --on-lost preshow    back to the preshow loop (default; the show answer)
    --on-lost hold       freeze on the frame it reached (the rehearsal answer)
    --on-lost blackout   go dark

Either way there is a 2 second freewheel first (`--hold-ms`), during which the
show keeps running on its own clock, so a brief dropout is invisible.

In rehearsal use `hold`: they stop, the lights stay put, they go again from bar
40 and it picks up. On a show night use the default: if the feed has genuinely
failed, a rig frozen mid-cue is worse than a rig on its preshow look.

**Timecode jumps.** A jump larger than `--jump-threshold` (0.15s) is treated as
deliberate and snapped to immediately. Smaller differences are slewed, so
decode noise does not make the show stutter.

## The web page

    ./ltc serve                 open it on this Mac
    ./ltc serve --bind 0.0.0.0  reach it from a phone or iPad on the same network

Or double-click **Web ltcplay.command**.

**The Terminal window it opens is the engine. The page is a window onto it.**
Close the browser, sleep the iPad you were watching from, lose wifi: the show
carries on, because none of those things are where it lives. Ctrl-C in that
Terminal window is what stops it. A web UI that owned the engine would add a
whole new way to lose a show, which is the opposite of the point.

The page shows the same two clocks, the same cue and countdown, the same
warnings as the terminal screen, because both read the same functions. It also
carries the input picker, a Find button, the mode switch, Validate, Rehearse,
Run and Stop. Run asks before it sends anything, and Validate sends nothing at
all, which is tested by listening on the ArtNet port during a validation and
requiring silence.

Serving on `0.0.0.0` mints a token and puts it in the link, whether or not you
asked for one: anyone who can reach that port can black out the rig, and a
venue network is not a private one. Requests from the machine itself never need
it.

One show runs at a time. A second start is refused with a reason, because two
players on the same universes fight frame by frame and the rig looks broken.

## The run screen

    LTC IN     01:14:22:11     LOCKED
    PLAYING    01:14:22:11     show            sync +2 ms

**LTC IN** is the last timecode that actually arrived, and it freezes the
instant the feed stops. **PLAYING** is where the show is, and it keeps
free-rolling through a short dropout. Two lines, one glance, and the gap
between them is the problem.

Below that: the detected frame rate with its measured value, the current
sequence with how much is left, and the next one with the timecode it starts
at. Anything wrong is written at the bottom as a sentence, in red, with the
fix. `OPERATOR.md` is the one page to hand to whoever is running it.

Every run writes `ltcplay.log` beside the timeline: cue changes, state changes,
jumps, network faults, each stamped with the timecode it happened at.

## Behaviour you should know before you trust it

**Jumps.** A timecode change larger than 150ms is treated as a deliberate jump
and snapped to immediately, forwards or backwards. Smaller differences are
slewed in gently so decode noise does not make the rig stutter. Change the
boundary with `--jump-threshold`.

**Timecode stops.** After 250ms with no LTC it freewheels on its own clock and
shows FREEWHEEL. After `hold_ms` (2 seconds by default, and a show file
should set it higher) it gives up, shows LOST and goes to the `on_lost` look,
which is the preshow loop unless you changed it. Both
are adjustable (`--freewheel-ms`, `--hold-ms`).

**Latency.** There is real lag between the timecode and the lights: audio input
buffering, decode, and the network. Expect somewhere around 50 to 100ms. If
things feel consistently late against the music, trim it with `"offset_ms"` in the show file (or `--offset-ms` from a terminal).
Positive values push the rig later.

**Frame rate.** Set `fps` in the timeline to what your source actually sends.
The decoder reports the nominal rate it sees; it cannot tell 29.97 from 30 from
the bitstream alone, because they differ by a tenth of a percent.

**Channels past the end of your map.** If a sequence addresses channels beyond
what `xlights_networks.xml` covers, those channels are dropped and `check` warns
you. That usually means the sequence was rendered before a controller changed.

## Ctrl-C

Stops cleanly and sends three blackout frames, then says whether they
actually reached the network. If the program is killed some other
way, the rig holds its last frame until something else takes over.

## The honest risk list

Written after an adversarial audit on 2026-09-13 that attacked this program on
four fronts. Everything it confirmed is fixed; these are the things that are
still true by design.

1. **One machine, one process, one cable.** No spare, no failover, no
   changeover. The mitigations are the login agent (restarts in seconds), the
   preshow look (a dark rig is never the resting state) and GO (the show can
   run without the feed). None of them survives the Mac itself dying.
2. **ArtNet has no priority.** A hot spare would need the show moved to sACN,
   where a second sender at a lower priority is picked up by the receivers with
   nobody touching anything. Worth doing if this runs a second season.
3. **The renders should not live in a sync folder during a show.** Dropbox can
   replace a file under a reader. `ltcplay bundle <show> <folder>` makes a
   self-contained copy with a hash of every render; run the show from that.
4. **Timecode is trusted after two agreeing frames, not one.** A single
   corrupt frame is ignored and counted on screen. A stream of them means a bad
   cable, and the show will drift on its own clock until it is fixed.
5. **UDP never reports delivery.** `check` pings every controller before a run.
   During a run, "0 send errors" means the packets left this Mac, not that
   anything received them.

## Moving it to another Mac

    ./ltc bundle gpl2026_timeline.json ~/Desktop/GPL_Show --zip

That writes one folder holding the program, the launchers, the show file, the
controller map and every render the show names, with a SHA-256 of each. Copy it
anywhere, double-click **Install ltcplay.command** once, then **Web
ltcplay.command**. `./ltc verify` on the new machine proves the copy is
byte-for-byte the show you tested.
