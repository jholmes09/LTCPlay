# Fire & Ice 2026: Pico bench report, 2026-09-25

Written by Claude Code on the show PC (VIOSO AnyStation Pico) for Jeff and the main development session. Headings B0 to B9 follow the main session's request; the full running log of the day, with every intermediate number, is `bench_evidence/daylog_2026-09-25.md`. Throwaway scripts are in `C:\Users\VIOSO\Desktop\Show\scratch` (not in the repo). Screenshots and captures are in `bench_evidence/`.

**Safety throughout:** no flames (flame hardware unplugged; the flamesafe code was not run, on Jeff's instruction). Both Ethernet ports unplugged for every test (checked before each run by `start_run.ps1`, which refuses otherwise); all show traffic went to 127.0.0.1. Audio went to Jeff's headphones or a Focusrite Scarlett Solo with nothing connected to the amps.

## Headline

| Item | Verdict | One line |
|---|---|---|
| B0 Machine | recorded | i3-1215U, Intel UHD only, 15.8 GB, Kingston 1 TB NVMe on the underside that overheats without airflow |
| B1 MadMapper follows Art-Net timecode | PASSED | Locks 14 to 17 ms after the first frame; within 17 ms (one 60 fps frame) over 7:20, 0 frames off at the end; hears only the address of its selected interface |
| B2 OSC control, port 8000 | PASSED (select, play, stop, audio level); video level FAILED (use surface opacity); no replies | Input port is a per-project setting (default 8010); bank select works by `/select`, not `active_bank` or `by_name`; each chasing bank needs its own interface; Jeff's non-chasing intermission works |
| B3 Heartbeat | PASSED, with a design consequence | A ramped OSC Float track sends MadMapper's position 60 times a second; silent while frozen, after the show and on other banks; suspend: last packet 6 ms before, back 51 ms after |
| B4 Freeze on one frame | PASSED (video, fades); audio needs the fade | Picture holds the exact frame for 30 s; audio runs on 0.35 s then silent, repeats 0.17 s on resume; a 1 s audio fade hides both; opacity fade to black is smooth |
| B5 Audio while chasing | PASSED, with a note | No drift; one 33 ms nudge per 7.4 min; listening test by Jeff outstanding |
| B6 Six video tracks | PASSED at panel size / FAILED at 1080p H.264 | Panel-sized: 38% CPU at 4K desktop, 19.5% at 1080p, smooth 30 fps; six 1080p H.264: 100% CPU |
| B7 ltcplay's own tests | PASSED | main 9291c35: all checks passed in 90.5 s |
| B8 BEYOND | IN PROGRESS (now activated) | |
| B9 Long soak | PASSED on the desk with airflow (1 h 34 min + 2 h); 4 h with intermission gaps: NOT YET RUN | Heat is the limit: the SSD stalled up to 20 s and froze the PC when the box sat flat on a desk |

## B0 The machine

| | |
|---|---|
| Windows | Windows 11 Pro 23H2, build 22631.4602 |
| CPU | 12th Gen Intel Core i3-1215U, 6 cores / 8 threads, 15 W; runs about 310% of base clock idle, 124 to 250% under show load |
| GPU | Intel UHD Graphics (integrated, no graphics card), driver 32.0.101.7080 (2025-11-02) |
| RAM | 15.8 GB |
| Storage | Kingston OM8SEP41024Q-A0 1 TB NVMe, firmware SBI00111 |
| Display | Samsung U32R59x at 3840 x 2160 until 21:57, then 1920 x 1080 at 60 Hz (temporary change, see B6) |
| Audio devices | Focusrite Scarlett Solo USB (plugged in about 19:20); Realtek jack (only present while headphones are plugged in); monitor HDMI audio |
| venv Python | 3.12.10 (numpy 2.5.3, zstandard 0.25.0, tzdata 2026.4) |
| MadMapper | MadMapperDemo 6.1.5 (file version 6.1.5.0), **trial**: cannot save, watermark |
| BEYOND | BEYOND 5.5 Demo (file version 5.5.0.1919) + BEYOND 3D Support 2.1, **trial**, activated by Jeff 2026-09-25 about 23:00 |
| ltcplay commit | `9291c35e4ff1aca006549ca09066882b723acb52` (main, includes PRs #8, #10, #11). Earlier tests used `aa7e6a8` and the PR #8 branch at `6fdf380` as stated in each item |
| Network adapters | Ethernet (Intel I219-V): disconnected. Ethernet 2 (Realtek): disconnected, configured 192.168.1.150/24. Wi-Fi (Intel AX200): 192.168.4.42/22, Windows category Public until Jeff set it to **Private** at about 23:25. Loopback 127.0.0.1. **In production there is no Wi-Fi, wired only; the Pico has two Ethernet ports** (Jeff), e.g. one for the pixel network and one for everything else |
| Desktop in OneDrive? | No: Desktop is `C:\Users\VIOSO\Desktop` |
| Firewall | No firewall prompt appeared for Python or MadMapper during these tests (all traffic was loopback). Rules present for the show programs: only "Pangolin BEYOND", inbound Allow, profiles Private and Public (created at BEYOND's install, before this session). **No rule for python.exe or MadMapperDemo.exe**: on the real rack network, inbound OSC or Art-Net to them may be blocked until rules exist (the installer in handoff section 10 is meant to add them). |
| Admin prompts | Windows is set to elevate administrators **without prompting** (ConsentPromptBehaviorAdmin 0): worth putting back to the default before the show (Jeff's call) |
| Remote access installed | TeamViewer (running), Parsec |

**Heat (the finding of the day, details in the day log):** at 10:12 the Pico froze solid (Ctrl+Alt+Del dead, no crash dump). From about 12:00 it crawled: disk reads took 1.4 s, the SSD's own counters showed a **20.5 s read and 19.1 s write** stall, SSD 60 to 65 C at idle and climbing, heatsink too hot to touch. Lifting the Pico one inch off the wooden desk at 13:19 stopped every stall within minutes (flight-recorder gaps over 15 s: 17 in the 19 minutes before, 0 after) and the SSD cooled. It then ran 3.5 hours of soak with no stall, SSD plateauing at 63 to 67 C. **The rack must give the Pico airflow on all sides, including underneath.** Also removed on Jeff's instruction: four virtual-monitor drivers and the Elgato Stream Deck app.

## B1 MadMapper follows Art-Net timecode

**Verdict: PASSED.** Follows on 127.0.0.1 or the LAN address (whichever matches its Interface setting), locks within one screen refresh, exact over 7:20.

**Settings, click by click** (MadMapper Demo 6.1.5, fresh launch; the trial cannot save):
1. Launch. Trial welcome: leave MadMapper ticked, MadLaser unticked, **Next**, then **Try MadMapper**.
2. **Project** box: **New Project**, Canvas **Video** ticked, **DMX** unticked, **OK**.
3. Bottom bar menu (reads "Scenes Cues Timelines" with an arrow): choose **Scenes Cues Timelines**; click **^** at the far right of that bar to open the panel.
4. Click **Conductor** in the panel's top bar.
5. Click **...** at the right end of the Conductor transport row. A **Settings** panel opens.
6. **Playback: External Sync**. **Sync Source: ArtNet** (choices LTC, MTC, ArtNet). **Interface: Localhost** (choices Localhost, Wi-Fi - 192.168.4.42, Loopback Pseudo-Interface 1 - 127.0.0.1). **Offset** 0:00:00:00.
7. **Check Interface every time:** on one rebuild it came up as Wi-Fi instead of Localhost and MadMapper silently ignored the timecode.

Command: `python -m ltcplay.cli tctest --node MadMapper=127.0.0.1 --start 00:00:00:00 --seconds 60`. tctest prints its warning first ("Test timecode is about to go to: MadMapper (127.0.0.1). Anything that follows timecode will play its cues, lasers included.") and sends 30 frames a second.

Results so far (details: day log, runs 1 to 3; screenshots `B1_*.png`):
- **Follows:** from paused at zero, playing on its own; after 1.00 s the counter read 0:00:00:53 (60 fps units) = 0.88 s, program start-up included.
- **Tracking, 60 s run** (counter vs time since tctest started, screenshots): 37.99 s -> 38.10 s; 45.43 s -> 45.35 s; 52.62 s -> 52.52 s. Within about 0.1 s, not growing. MadMapper's counter runs at 60 fps units (its project rate); the timecode is 30 fps.
- **Jump forward** (`--start 00:05:00:00 --seconds 20`): at 2.00 s the counter read 0:05:01:32; then steady (+/- one 60 fps frame).
- **Jump back to zero** (restart `--start 00:00:00:00` while holding 0:05:19:58): at 1.00 s it read 0:00:00:53.
- **When tctest ends:** MadMapper pauses **on the last frame sent** (0:00:59:58 = 00:00:59:29 at 30 fps; 0:05:19:58 after the 5 min run) and holds it. It does not freewheel or go black. (Its Art-Net interface log line: "Min FPS=2.000000 / Max FPS=44.000000 / Unicast=false".)
- **Restart a few seconds later:** re-locks on the first packets (the jump-back test above, and 120 cue restarts in soak 2 with a 2 s gap each).
- **Over long runs:** soak 1 (11+ cues of 7:20) and soak 2 (120 cues of 58 s) ran with MadMapper chasing throughout; the picture showed 29.8 to 29.9 new frames a second at every check, and MadMapper's audio stayed at a constant offset (B5).

**B1 measurements on main 9291c35, 23:05 to 23:36** (script `scratch/b1_lock.ps1`: launches tctest, notes when tctest prints its first line, which it does as it sends frame 0, polls MadMapper's counter until it changes, and saves counter snapshots; desktop at 1920 x 1080):

1. **Which address MadMapper hears:** only the address of the Interface chosen in its Conductor settings.

| MadMapper Interface | `--node MadMapper=127.0.0.1` | `--node MadMapper=192.168.4.42` (Pico's Wi-Fi address) |
|---|---|---|
| Localhost | follows | **not heard** (6 s, counter never moved) |
| Wi-Fi - 192.168.4.42 | **not heard** | follows |

   So with ltcplay and MadMapper on the same PC either works, as long as the node address and MadMapper's Interface match. On the rack network, choose the rack network card in MadMapper and point the node at the Pico's rack address or 127.0.0.1 accordingly.
3. **Time from first frame to following** (counter first changes after tctest's first packet), start at zero, 60 fps display: **14 ms, 16 ms, 17 ms** (Localhost), and 17 ms at the start of the 440 s run (Wi-Fi). Effectively the next screen refresh.
4. **440 s from zero** (`--node MadMapper=192.168.4.42 --start 00:00:00:00 --seconds 440`; `B1_440s_summary.png`): counter against time since the first packet: 59.79 s -> 0:00:59:47 (-7 ms), 119.80 -> 1:59:49 (+17 ms), 179.79 -> 2:59:48 (+10 ms), 239.80 -> 3:59:48 (0), 299.79 -> 4:59:47 (-7 ms), 359.80 -> 5:59:48 (0), 419.79 -> 6:59:47 (-7 ms), 439.30 -> 7:19:18 (0). **Within one 60 fps frame (17 ms) the whole way, no drift.** At the end it paused on **0:07:19:58 = ltcplay's last frame 00:07:19:29**: 0 frames off. No stutter visible in the snapshots; per-frame smoothness of the picture while chasing is in B6 (29.8 to 29.9 new frames a second).
5. **Start at 00:03:00:00** (`--start 00:03:00:00 --seconds 6`): counter first changed **332 ms** after the first packet (slower than from zero, most likely MadMapper seeking its audio file 3 minutes in); 0.87 s after the first packet it read 0:03:00:52 (180.87 s) and at 2.87 s 0:03:02:52: exact from then on.
6. **When tctest ends:** pauses and holds the last frame sent (60 s run: 0:00:59:58; 440 s run: 0:07:19:58). No freewheel, no black (picture stays on that frame).
7. **Restart from zero a few seconds later:** re-locks on the first packets (1.00 s after start: 0:00:00:53), and 120 restarts with 2 s gaps in soak 2 all re-locked.

## B2 OSC control of MadMapper, port 8000

**Verdict: PASSED for select, play, stop and master audio level; master video level: FAILED (no visible effect; use surface opacity instead); pause and play_from_beginning: ignored while chasing timecode; no replies from MadMapper.** Main 9291c35 not involved (MadMapper only); tctest used for timecode. 23:10 to 23:36.

**1. The setting** (it is a *project* setting, so it has to be redone every session with the trial): **Ctrl+,** (Preferences) > **Project** tab (not Application) > **OSC**: **Input Port** was 8010, set to **8000**; Enable Bonjour Discovery left on; **Feedback Port 9000**; **Feedback IP**: Custom, was 192.168.1.1, set to **127.0.0.1**. OK. Screenshots `B2_prefs_project_osc.png`, `B2_B3_osc_settings.png`. (The Application tab has no OSC settings: `B2_prefs_general.png`, `B2_prefs_devices.png`.)

Throwaway sender (`scratch/osc_send.py`), the core of it:

```python
def encode(address, args):          # OSC 1.0: padded address, ",tags", big-endian args
    tags, data = ",", b""
    for a in args:
        if isinstance(a, bool):  tags += "T" if a else "F"
        elif isinstance(a, int): tags += "i"; data += struct.pack(">i", a)
        elif isinstance(a, float): tags += "f"; data += struct.pack(">f", a)
        else: tags += "s"; data += pad(str(a).encode())
    return pad(address.encode()) + pad(tags.encode()) + data
```

Measured effect of each message: MadMapper's audio (Scarlett, loopback, RMS every 50 ms, `scratch/audio_level.py`), the brightness of its Stage output preview and whether its Conductor counter kept changing (`scratch/screen_probe.ps1`), while tctest sent timecode (`scratch/b2_run.py`, `b2_analyze.py`).

**2. What worked:**

| Message (address, type tag, value) | Result |
|---|---|
| `/timelines/Bank-2/select` `,T` (True) | **Works.** The selected bank becomes the live one: its picture and audio take over within about 0.3 s; the other bank's audio stops. Bytes: `2f 74 69 6d 65 6c 69 6e 65 73 2f 42 61 6e 6b 2d 32 2f 73 65 6c 65 63 74 00 00 00 00 2c 54 00 00` |
| `/timelines/active_bank` `,s` "Bank-1" | No effect |
| `/timelines/Bank-1/by_name` `,s` "Bank-1" | No effect |
| `/timelines/Bank-1/conductor/stop` (no arguments) | **Works, even while chasing:** audio RMS 0.478 -> 0.000, counter stopped although timecode kept arriving |
| `/timelines/Bank-1/conductor/play` `,T` | **Works:** after the stop above, playing and chasing again (audio back to 0.478) |
| `/timelines/Bank-1/conductor/pause` `,T` then `,F` | While chasing: no change (counter and audio carried on) |
| `/timelines/Bank-1/conductor/play_from_beginning` (no arguments) | While chasing: no change. With the bank on Playback Default (not chasing): starts from zero, audio 0.30 s after the message |
| `/master/master_audio_level` `,f` 0.5 / 0.0 / 1.0 | **Works, 0.0 to 1.0, linear:** RMS 0.478 -> 0.239 (exactly half) -> 0.000 -> 0.478. **Jumps** to the value (no built-in ramp) |
| same, stepped by the sender 1.0 -> 0.0 over 1 s (31 messages) | **Smooth fade**, RMS every 0.1 s: 0.478 0.470 0.424 0.387 0.336 0.284 0.238 0.197 0.139 0.102 0.052 0.007 0.000; up again the same way |
| `/master/master_video_level` `,f` 0.0 / 0.5 / 1.0, and 1 s ramps | **No measurable effect** on the Stage output preview (brightness unchanged within noise, twice). Not usable as fade to black in 6.1.5 as tested |
| `/surfaces/Quad-1/opacity` ... `/surfaces/Quad-6/opacity` `,f` 0.0 / 1.0 | **Works as fade to black:** output brightness 139 -> 10 (black apart from the trial watermark) and back to 120 |
| same, 1 s ramp in 31 steps, all six surfaces each step | **Smooth**, brightness every 0.1 s: 129 128 128 127 126 125 123 116 108 86 48 8 (down); 12 67 99 111 120 124 (up) |
| `/timelines/does_not_exist/conductor/stop` | Nothing happened; no reply |

Documented addresses (MadMapper 6 OSC list, docs.madmapper.com): `/timelines/Bank-[1-X]/by_name` STRING, `/select` BOOL, `/conductor/[play]` BOOL, `/conductor/[play_from_beginning]` nil, `/conductor/[stop]` nil, `/conductor/[pause]` BOOL, `/conductor/markers/go_to_marker_by_name` STRING, `/master/master_audio_level` FLOAT, `/master/master_video_level` FLOAT. **`/timelines/active_bank` (named in handoff section 4) did nothing here.**

**3. Two timelines** (made with the Bank menu > **New Timeline Bank...**; kept the default names Bank-1 and Bank-2 since `by_name` did not work): Bank-1 = the show (six montage tracks + an LTC audio track at hour 00), Bank-2 = an "intermission" with an LTC audio track at hour 01, so the recorded audio says which bank is playing and where.
- **Both banks on External Sync / ArtNet on the same Interface: the second bank never follows** (counter stays at 0, silent) while the first one does. **Each bank that chases needs its own Interface.** With Bank-1 on Wi-Fi - 192.168.4.42 and Bank-2 on Localhost, and tctest sending to both (`--node MadMapper=192.168.4.42 --node MM2=127.0.0.1`), selecting each by OSC and running tctest from zero, the selected bank played its own content in sync (audio position minus time since tctest launch, 0.45 s of audio-chain delay included): Bank-1 -0.450 s and -0.466 s; Bank-2 -0.443 s and -0.468 s; spread within each run 1 to 11 ms. On the rack network with one card this will need Localhost for one bank, or a second card.
- **Jeff's model for intermission (23:30):** it does not chase timecode: a Go by OSC, it loops on its own clock, then the show bank takes over. Set Bank-2 **Playback: Default** and turn on its loop button. Test (`scratch/b2_intermission.ps1`): `select` Bank-2 + `play_from_beginning`: intermission audio from its start 0.30 s later, running normally for 12 s with no timecode. `select` Bank-1 + tctest from zero: intermission stops and the show plays; its first 0.2 s were the show bank's old position (decoded 00:00:00:28) before it jumped to 00:00:00:00 at 0.49 s after the select, then exact. After the show, `select` Bank-2 + `play` `,T`: intermission again from its start 0.38 s later. **This works and avoids the one-interface-per-bank limit.** The 0.2 s of stale show audio at the switch is worth hiding (start the show bank muted, or `stop` it at the end of each show so it sits at zero).

**4. Replies:** a UDP listener on 127.0.0.1:9000 (the Feedback Port) for 150 s covering every message above: **0 packets**. MadMapper sent no acknowledgement and no error for an unknown address. (`scratch/udp_listen.py`)

## B3 Heartbeat from MadMapper

**Verdict: PASSED with a design consequence: the pulse is a position stream at 60/s, and it stops whenever the timeline is not moving (Hold, after the show, intermission bank).** 23:37 to 23:48, main 9291c35 tctest.

**1. Settings, click by click:**
1. OSC output destination (a per-project setting): Ctrl+, > **Project** > **OSC** > **OSC Outputs** **+**: a row "OSC Output-1", Bonjour **Custom**, IP **127.0.0.1**, Port **9001** (double-click the port cell and type). `B2_B3_osc_settings.png`.
2. Show bank Conductor: **+** next to Tracks > **Add OSC Track** > **Float** (choices Float, Integer, String, Color, Bool, Events). It appears under DATA as "OSC - /float-1", OSC Device **OSC Output-1**, Address `/float-1`, Min 0.00, Max 1.00 (Max changed to 440 had no effect on the values sent).
3. Keyframes: double-click on the track's lane adds one; select it and set **Time** and **Value** in the right-hand panel. Two keyframes: **0.000 s value 0**, **440.000 s value 1**, Interpolation Linear. `B3_osc_track.png`.

**Behaviour found:** an OSC track sends **only when its value changes** (one keyframe = one message, `/float-1 ,f 1.0`, once). During a ramp it sends once per rendered frame. So the heartbeat is a ramp across the whole show; its value x 440 is MadMapper's own position in seconds.

**2. 60 s capture while chasing** (`scratch/udp_listen.py` on 127.0.0.1:9001, tctest to both nodes): **3,717 packets in 62.2 s; interval min 0.0, mean 16.7, 99th percentile 22.0, max 258 ms**; one gap over 50 ms: **258 ms at position 60.08 s, exactly where the 60 s panel clips loop** (MadMapper stalls briefly when a clip wraps). Address `/float-1`, type tag `,f`, one float 0..1, sent **from 127.0.0.1 port 8000** (MadMapper's own OSC input port). Bytes: `2f 66 6c 6f 61 74 2d 31 00 00 00 00 2c 66 00 00 3f 80 00 00` = `/float-1 ,f 1.0`. **Value x 440 minus time since the first packet: median 2 ms** (min -244 ms at that loop stall, max 8 ms).

**3. When it keeps coming:**
- **Timecode frozen (B4 sender, 30 s on one frame): 0 packets for the whole freeze** (gap 29.84 s), 60/s again after resume, value continuing 20.45 s. Twice.
- **After timecode stops: 0 packets** (9 s checked).
- **Intermission bank playing (Bank-2, own clock): 0 packets** in 6 s: tracks belong to one bank; the intermission needs its own OSC track (which would then run whenever it plays, even without timecode).
- **Consequence for the watchdog:** "no pulse for 3 s" is also the normal state during Hold and between shows. The watchdog must only count silence as a fault while ltcplay's clock is actually running, and can use the value to check MadMapper's position against the clock (stale value or a position more than, say, 0.5 s off = fault).

**4. Process suspended** (`scratch/suspend.py`: NtSuspendProcess on MadMapperDemo.exe, the same as Resource Monitor's Suspend process, for 10.0 s mid-chase): **604 packets before, 0 while suspended, the last one 6 ms before the suspend call**; after resume the **first packet 51 ms later, value already 20.10 s** (the live position, not where it stopped), then normal (mean 16.6 ms, max 23 ms). So a frozen MadMapper goes silent within one frame, and the 3 s rule catches it with plenty of margin.

## B4 Freeze on one frame (the Hold button)

**Verdict: PASSED for video (holds the exact frame) and for fades (both smooth); audio NEEDS the fade: without it, the audio runs on 0.35 s after the freeze and repeats 0.17 s on resume.**

**30 s freeze with the throwaway sender, 23:45 to 23:52.** `scratch/freeze_sender.py` imports ltcplay's own `clock.arttimecode()` (so the 19 bytes are exactly what the show sends: `Art-Net\0`, OpTimeCode 0x9700 low byte first, protocol 14, filler, stream 0, frames, seconds, minutes, hours, type 3 = 30 fps non-drop) and paces them at 30 a second on absolute `perf_counter` deadlines like ltcplay's Ticker; 20 s normal from 00:00:00:00, then frame 599 (00:00:19:29) repeated for 30 s, then 00:00:20:00 onwards for 20 s (2,100 packets), to 192.168.4.42 and 127.0.0.1. Core of it:

```python
if phase_ends[0] <= k < phase_ends[1]:
    emit(frozen_at)                      # the same frame, 30 times a second
else:
    if k == phase_ends[1]:
        n = frozen_at + 1                # carry on from the next frame
    emit(n); n += 1
```

Recorded at the same time: MadMapper's audio decoded from its LTC track (loopback), its audio level every 50 ms, its output brightness and Conductor counter every ~50 ms, the B3 heartbeat. `scratch/b4_run.ps1`, `b4_analyze.py`.

1. **Video during the freeze: holds the exact frame.** Counter image: 1 distinct image in 321 samples over 29.5 s (before: 56 distinct in 56); output brightness steady at the frozen frame's level (median 130.8, 6 to 7 distinct values from the trial watermark). Not black, not playing. Nothing changed over the 30 s.
2. **Audio during the freeze:** carries on for **0.3 to 0.4 s** after the last new frame (level 0.48 at +0.3 s, 0.23 at +0.4 s), then **silence** for the rest of the freeze (level 0.000). No loop, no stutter, no fragment.
3. **Resume:** video continues from the next frame (counter moving at once). **Audio is back 0.2 to 0.3 s after the first new frame, starting 5 frames early** (decoded 00:00:19:28 at +50.288 s, then 00:00:20:00 at +50.338 s): a 0.17 s repeat of what played just before the freeze, then in sync. No click measurable by level (it fades in over one 50 ms block).
4. **Volume fade** (`/master/master_audio_level`, 31 steps over 1 s, 19.0 to 20.0 s, and back up 50.0 to 51.0 s; `scratch/b4_fade.py audio`): **smooth**, level every 0.1 s into the freeze 0.24 0.20 0.15 0.10 0.05 0.01 0.00; on resume 0.00 0.01 0.05 0.09 0.14 0.19 0.23 0.28 0.33 ... With the fade, **no audio is heard during the run-on or the resume repeat** (the first decoded frame after resume is 00:00:20:00).
5. **Fade to black** (`/surfaces/Quad-1..6/opacity`, same timing; `b4_fade.py video`): **smooth**, output brightness every 0.1 s: 138 138 138 137 137 136 134 133 130 127 119 103 86 47 6; **5.1 while held** (black apart from the watermark); up on resume 12 67 91 115 127 131 135 (about 0.6 s). (`/master/master_video_level` did nothing in B2, so opacity per surface is the command.)

**Earlier, ltcplay's own Hold (PR #10, `Session.clock_pause()` / `clock_resume()`)**, not the 30 s throwaway sender: (PR #10, `Session.clock_pause()` / `clock_resume()`), not yet with the 30 s throwaway sender.** Run `hold1`, 22:33 to 22:55: 20 cues of the 58 s bench show, each held at 20 s for 5 s. ltcplay main 9291c35. MadMapper chasing on loopback with six montage tracks and the audio track.

- **Pixels (ltcplay's own output, 26,256 px to the local receiver):** froze on the frame and repeated it through each hold (about 190 repeats per 5 s hold); **0 frames skipped** during, 14 skipped in total in the 3 s after resume across 19 holds; one frame number arrived out of order around most holds (to investigate).
- **MadMapper audio:** **silent while held** (no timecode decoded from its audio in any of the 19 holds). **On resume it restarts 4 to 5 frames (133 to 167 ms) earlier** than the last audio heard before the hold: its audio runs on briefly after the timecode freezes, then goes back to the frozen frame. Audible as the last ~0.15 s repeating, unless the music is faded out first (the Hold spec fades it).
- **ltcplay's own counter:** the clock's "skipped" count rose by about 600 per 5 s hold (11,424 after 19 cues) while the receiver saw almost nothing skipped. Looks like PR #10's pause is counted as skipped frames: **for the main session to check.**
- Video during a hold: MadMapper's counter was caught frozen at exactly 0:00:20:00 in a screenshot during one hold.

## Show music (Jeff, 2026-09-25)

`IgniteTheNight_Music_Unmixed_092526.wav`, downloaded from Jeff's link at his request: 48 kHz, 24-bit, stereo, 122 MB, **444.42 s (7:24.42)**. **That is 4.42 s longer than the 440 s show length in handoff section 5**: if the show clock stops at 7:20 the last 4.4 s of music are cut. Decision for Jeff and the main session: show_len_s 445, or trim the music. A listening test file with the music on the left and LTC on the right (`bench_media/IgniteTheNight_music_L_ltc_R.wav`, `scratch/make_music_ltc.py`) is ready for B5.

## B5 Audio while chasing

**Verdict: PASSED, with a note.** Measurement: a WAV whose audio *is* LTC (30 fps, from 00:00:00:00, `make_ltc_wav.py`) on a Conductor audio track; what Windows plays is recorded by loopback and decoded (`audio_ltc_monitor.py`), so every decoded frame says where MadMapper's audio was. `classify_audio.py` separates new-cue restarts, single misread frames, the recorder's own capture gaps, and real skips.
- **Device:** MadMapper Preferences, Audio: Driver Type **Direct Sound**, Audio Output **Speakers (Scarlett Solo USB)** (chosen from the list: None, Default Device, Speakers (Scarlett Solo USB), U32R59x), Sample Rate **44100**, Buffer **2048 samples**. It was on "Default Device" until changed. ASIO not tested (the show's DSP will be ASIO).
- **Soak 2, 111.7 min, 120 jumps to zero:** 15 real skips, all exactly +2 frames: **one 33 ms nudge every 7.4 minutes**; position constant against the cue clock (median -293 ms, 10th to 90th percentile -313 to -272 ms, first 30 s -270, last 30 s -294: no drift). At 1080p desktop: 0 real skips in 10 min.
- No pitch wobble measurable this way (a pitch change would show as decoded frames drifting in time; none did).
- **MadMapper log** (`%APPDATA%\MadMapper\MadMapper\Logs`): "ERROR Could not find selected audio input device in registry: Default Device" at start-up; nothing about audio output.
- **Lesson:** when the Realtek headphone jack was unplugged, that audio device vanished and MadMapper's audio went silent with no warning. Show audio must go to a fixed device.
- Outstanding: Jeff to listen once with real music.

## B6 Six video tracks

**Verdict: PASSED with panel-sized content; FAILED with six 1080p H.264 files.** Placeholders, not real content (no Fire & Ice video exists yet).
- **Six 1080p H.264** (`SnowballandFlurry_9MinuteCut.mp4`, 1920 x 1080, 29.97 fps, about 20 Mbit/s, six copies, free-running): CPU 88% average, **100% peak**; ltcplay lost 11% of pixel frames alongside. MadMapper decodes H.264 on the CPU ("FFMPEG Player (CPU)"), the GPU's video engine sat at 0%.
- **Six panel-sized uncompressed stand-ins** (310x215, 175x215, 405x282, 142x282, 358x257, 342x257, 29.97 fps, 60 s loops; sizes from the canvas handoff), on six Conductor montage tracks, following timecode, with ltcplay driving 26,256 pixels to a local receiver at the same time: 2 hours clean (soak 2). CPU 38% average, 89% peak; GPU 46% average, 62% peak; picture 29.8 to 29.9 new frames a second, longest gap 57 to 58 ms. Pixels: 40 fps, 0.45% frames skipped, no drift.
- **Desktop resolution matters:** same load at 4K desktop vs 1080p: CPU 37.8% vs 19.5%, MadMapper 1.5 vs 0.6 threads, GPU 43% vs 20%. Run the show screen at 1080p.
- **Masks** (black overlays from the canvas handoff polygons): no measurable load (12.8% vs 14.0% CPU, within noise).
- **Temperatures:** Windows exposes no real CPU/GPU temperature (the ACPI zone reads a fixed 27.9 C). SSD 63 to 67 C under show load with airflow.
- BEYOND in the same load: IN PROGRESS (B6 extra).

## B7 ltcplay's own tests

**Verdict: PASSED.** `python -u selftest.py` on main `9291c35`: last line **"all checks passed in 90.5s"**, no FAIL lines (log `selftest_main9291c35_2026-09-25.log`). Earlier: `aa7e6a8` all passed in 82.0 s; PR #8 branch all passed in 84.3 s.
- Pixel timing on this machine (from the selftest): "80 frames in 2.0s, mean interval 25.00ms (target 25ms), worst deviation 7.97ms, 90th pct deviation 2.55ms"; "time.monotonic is GetTickCount64() (resolution 1.56e-02s), time.perf_counter is QueryPerformanceCounter() (resolution 1.00e-07s) on win32; DIFFERENT".
- Under show load (10 min, 1080p): with PR #8 **1 frame skipped**, 99% of frames within 1.9 ms; the old main without PR #8 **3,728 frames skipped (16%)**, 11 ms. Worst frame lateness in the 2 h soak on the PR #8 build: 44.5 ms against the cue's clock, median -1.6 ms.

## B8 BEYOND

IN PROGRESS (activated by Jeff about 23:00). Correction noted: BEYOND runs on the Pico with Andy's licence. Pangolin's comparison table (read 2026-09-25): BEYOND Essentials and above receive Art-Net timecode, OSC, Art-Net and sACN; QuickShow does not.

## B9 Long soak

**Done so far (not yet the 4 h run with the handoff's intermission gaps):**
- Soak 1: 7:20 cues with 30 s gaps, 1 h 34 min clean until an accidental unplug (not a freeze: the last second before power loss was a clean 40 fps). Timecode 7 frames skipped in about 145,000; pixels 0 send errors; SSD 56 to 70 C.
- Soak 2: 120 cues of 58 s with 2 s gaps, 2 h, cold start: timecode 19 skipped in 208,781; pixels no drift (-1.7 ms first minute, -1.4 ms last); SSD 32 to 67 C, no stall; MadMapper memory 2.35 to 2.52 GB, not growing; flight recorder never paused over 7 s.
- Windows event log: the only problem entries today are the two unexpected shutdowns (10:12 freeze, 15:30 unplug).
- The 4 h run with 7:20 shows and the handoff's 20-minute interval, with the B3 heartbeat watched: to run overnight.
