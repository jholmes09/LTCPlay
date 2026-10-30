# Fire & Ice bench: running the `fire-ice-integration` branch on the Pico

This branch puts the pieces together so the real rig can be tested on the
Windows Pico before each piece is merged: the show conductor (Hold, Resume,
Abort), the BEYOND laser link, the MadMapper video link, the show scheduler,
and the show audio clock. GPL at Dollywood is not affected: none of this
loads unless ltcplay is started with a schedule.

## What runs it

Double-click **Fire and Ice bench.bat** in the ltcplay folder. It runs:

    py -m ltcplay.cli serve --schedule

and opens the ltcplay page in the browser. Leave its window open: it is the
engine. Closing it stops everything. (If `py` is not found, the bench
session can run the same line with `python` in place of `py`.)

`--schedule` with no file named uses the schedule in
`%LOCALAPPDATA%\ltcplay\`. Starting with a schedule is what makes this Fire
& Ice; without it ltcplay runs exactly as it does for GPL.

## The three files to set up

All three live in `%LOCALAPPDATA%\ltcplay\` except the show file, which
lives in the show folder.

### 1. The schedule: `ltcplay_schedule.json`

Copy `ltcplay\ltcplay_schedule.example.json` and set tonight's times so a
show starts a few minutes from now. `show_len_s` must be at least as long as
the show audio, or ltcplay refuses to start and says so.

### 2. The Fire & Ice settings: `ltcplay_fire_ice.json`

This file is new. Without it the show conductor is still built, but with no
lasers and no video, and the scheduler only pretends (a dry run). Example:

    {
      "scheduler_performs": false,
      "show_cue": "Show",
      "madmapper": {
        "host": "127.0.0.1", "port": 8000,
        "show_bank": "Bank-1", "intermission_bank": "Bank-2",
        "surfaces": ["Quad-1", "Quad-2"],
        "heartbeat": {"show_len_s": 440}
      },
      "beyond": {"host": "127.0.0.2", "port": 8100}
    }

What each setting does:

- `scheduler_performs`: the one switch that lets the scheduler act.
  - `false` (the default, and what to start with): the scheduler only
    decides. At show time it writes "Not performed, dry run" in the journal,
    and the show "ends" on the scheduler's own clock. Nothing is started.
    Hold, Resume and Abort still reach the show conductor, but it only acts
    on a show that is actually playing.
  - `true`: at each scheduled show time ltcplay selects the MadMapper show
    bank and starts the show audio, which starts the timecode, which starts
    the pixels, MadMapper and BEYOND. The show then ends when its audio
    ends, not on a clock. At closing the flame cues go to zero, the lasers
    are blanked and the pixels go black. It only starts a show if **Run has
    been pressed** on the ltcplay page; otherwise the show is recorded as a
    failed start and nothing reaches the rig. It also does not start a show
    while an Abort is still latched (see open questions below).
  - Only the exact words `true` or `false` are accepted.
- `show_cue`: which cue in the show file is the show. Leave it out to use
  the first cue.
- `madmapper`: MadMapper's OSC address, its bank names, and the names of the
  surfaces to fade. Leave the whole block out if MadMapper is not on the
  bench; the conductor then says "No MadMapper is configured" and skips it.
- `beyond`: BEYOND's OSC address. In BEYOND: Settings > OSC > OSC Settings,
  turn receiving on, incoming port 8100. Leave the block out if BEYOND is
  not on the bench.

### 3. The show file, in the show folder

The show file's `clock` block must use `"source": "audio_master"`: ltcplay
plays the show's multi-track audio itself and the timecode comes from it.
Its `artnet.nodes` must name MadMapper and BEYOND (on the bench:
MadMapper `127.0.0.1`, BEYOND `127.0.0.2`). The cue named by `show_cue`
needs its stems under `clock.audio.cues`.

## What has to be connected

- **Show audio interface**, named in the show file's `clock.audio.device`.
  ltcplay will not start a show without it.
- **MadMapper**, running on the Pico, with OSC input on, the show bank set to
  chase Art-Net timecode, and the heartbeat track sending to port 9001.
- **BEYOND**, running on the Pico, OSC input on port 8100, chasing the same
  Art-Net timecode.
- **Pixels**: the controllers named in the show folder's xLights networks
  file, as for any ltcplay show.
- **flamesafe** (the flame safety program) runs on its own, with its own
  config, set up for **3 flame groups**. Andy fills in each group's name
  and channels at the bench; this branch does not assign heads to groups.
  See the flame note below before connecting anything with gas.

## How to run a bench test

1. Start MadMapper and BEYOND.
2. Double-click **Fire and Ice bench.bat**. The window prints "Fire & Ice:"
   and what it found (scheduler performs or dry run, MadMapper, BEYOND).
3. On the page, pick the show file and press **Run**. Nothing reaches the
   rig before this.
4. Watch the schedule panel. The scheduler moves from preshow to the show at
   the scheduled time. The page at `/api/conductor` shows what the show
   conductor last did and any faults.
5. Everything the conductor does is written in the night journal with the
   reason, including every refusal and every fault.

## What is real on this branch

- **Lasers (BEYOND)**: Hold, Abort, intermission and preshow send a real
  blank. Lasers come back only once the timecode is moving and only while
  the scheduler is in a show, never in intermission, and only while every
  laser is armed on the Morning and Shutdown page (Jeff, 2026-10-22,
  MORNING.md "Lasers: arm and disarm"). Every engine start is disarmed and
  sends BEYOND's DisableLaserOutput; Abort and Hold disarm and send it too. Abort blanks them at
  once (Jeff and Andy, 2026-10-04: BEYOND only accepts on or off).
- **How the lasers are blanked** (`beyond_blank` in ltcplay_fire_ice.json):
  `"osc"` (brightness 0 or 100 on BEYOND's
  `/beyond/master/livecontrol/brightness`, through the `"beyond"` block's
  host and port, 127.0.0.1:8100 unless set; the default since Jeff,
  2026-10-07), `"timecode"` (for BEYOND Essentials, which has no OSC input)
  or `"both"`. With `"osc"` and no `"beyond"` block nothing blanks the
  lasers, so a rig with lasers needs that block. In timecode
  mode BEYOND gets its own Art-Net timecode stream (the show file's node
  named BEYOND, 127.0.0.2 on the bench; MadMapper's stream is never
  touched). Whenever the lasers must be dark it jumps to the black zone,
  hour 23 (`beyond_black_hour`), 23:00:00:00 and running; when they may
  light it goes back to the show's own timecode on the next frame, the
  exact held frame after a Hold. **Andy: leave hour 23 empty in the BEYOND
  show, and keep BEYOND's "keep running when timecode stops" OFF.**
- **Video (MadMapper)**: Hold and Abort fade every surface to black; Resume
  and show start bring them back; Abort then stops the show bank.
- **Music and timecode**: Hold fades the show audio over 0.25 s and freezes
  the timecode where it stops; Resume fades back in; Abort fades over 1 s
  and stops.
- **Pixels**: Hold and Abort put the pixels to black **at once** (the pixel
  output has no fade), and Resume and show start bring back the look that
  was there before.

## What is NOT wired yet

- **MadMapper output check: bench-verify the rule** (PR #176). LTC
  Player reads MadMapper 6's video output as on only when MadMapper has a
  visible window with no title bar covering a whole display. That is an
  assumption, not yet seen on a real MadMapper 6. On the show PC: with the
  output on the projectors, the rack screen's lamp **MadMapper output**
  must read on; turn the output off (Output menu, Fullscreen) and it must
  read OFF within a few seconds; the supervisor log describes MadMapper's
  windows (class, owner, title bar, rect). Until this passes, Continue
  anyway leaves nothing off for a failed MadMapper output check (the video
  plays as it is). If the lamp reads OFF with the projectors lit, set
  `"madmapper_output_check": false` in ltcplay_fire_ice.json and press
  Reload engine. Also note the class of BEYOND's and MadMapper's message
  boxes in that log: the rack screen keeper treats Windows' dialog class
  (#32770) as a dialog it never covers.

- **Flames: ltcplay sends no flame cues at all on this branch.** There is
  no flame link from ltcplay to flamesafe yet. flamesafe will show "Show
  program has not answered yet: disarmed" and no group can arm. That is the
  safe state, and it means no fire can be tested through ltcplay yet. Every
  flame step the conductor takes is written in the journal saying it reached
  nothing.
- **Abort from the screen cannot disarm flame groups.** The flamesafe link
  has no disarm message yet. Every screen Abort writes a fault line saying
  so. The Stream Deck's own Abort (PR #31, not on this branch) disarms
  through its own link.
- **No buttons for Hold, Resume or Abort.** This branch adds no page button
  or route that presses them. They come from the Stream Deck (PR #31) once it
  is plugged in. Until then the bench can test the
  scheduled preshow, show start, show end and closing; Hold, Resume and
  Abort are proven by the selftest with fake devices.
- **Announcements** (Jeff, 2026-10-26): the rack screen's three buttons and
  the Stream Deck's page 2. Between shows a press holds the schedule first
  (Resume carries on); during a show it plays over the show and pauses,
  holds and stops nothing. The show PC starts them by itself from
  ltcplay_announce.json, which the first run writes with no output device:
  set the exact name, copy the recordings, restart. OPERATOR.md says how.
- **Stream Deck**: not on this branch. It plugs in later through the
  conductor this branch builds.
- **Start now**: when PR #30's next fix lands, Start now runs an extra show
  instead of starting the next scheduled show early.
- **Intermission video**: the scheduler's intermission does not start the
  MadMapper intermission bank yet. Closing does not stop MadMapper.
- **Rehearsal mode** (Hold freezes instead of fading) exists in the
  conductor but has no switch on the page yet.
- **MadMapper heartbeat watchdog** is built but not armed by the show.
