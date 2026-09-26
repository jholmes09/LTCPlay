# Fire & Ice 2026: Pico bench report, 2026-09-25

Written by Claude Code on the show PC (VIOSO AnyStation Pico) for Jeff and the main development session. Headings B0 to B17 follow the main session's request; the full running log of the day, with every intermediate number, is `bench_evidence/daylog_2026-09-25.md`. Throwaway scripts are in `C:\Users\VIOSO\Desktop\Show\scratch` (not in the repo). Screenshots and captures are in `bench_evidence/`.

**Safety throughout:** no flames (no flame hardware in the building; the flamesafe code was run only in B10, on Jeff's permission once the main session said it was ready, and only to a loopback listener). Both Ethernet ports unplugged for every test (checked before each run by `start_run.ps1`, which refuses otherwise); all show traffic went to 127.0.0.1 or 127.0.0.2. Audio went to Jeff's headphones or a Focusrite Scarlett Solo with nothing connected to the amps.

## Headline

| Item | Verdict | One line |
|---|---|---|
| B0 Machine | recorded | i3-1215U, Intel UHD only, 15.8 GB, Kingston 1 TB NVMe on the underside that overheats without airflow |
| B1 MadMapper follows Art-Net timecode | PASSED | Locks 14 to 17 ms after the first frame; within 17 ms (one 60 fps frame) over 7:20, 0 frames off at the end; hears only the address of its selected interface |
| B2 OSC control, port 8000 | PASSED (select, play, stop, audio level); video level FAILED (use surface opacity); no replies | Input port is a per-project setting (default 8010); bank select works by `/select`, not `active_bank` or `by_name`; each chasing bank needs its own interface; Jeff's non-chasing intermission works |
| B3 Heartbeat | PASSED, with a design consequence | A ramped OSC Float track sends MadMapper's position 60 times a second; silent while frozen, after the show and on other banks; suspend: last packet 6 ms before, back 51 ms after |
| B4 Freeze on one frame | PASSED (video, fades); audio needs the fade | Picture holds the exact frame for 30 s; audio runs on 0.35 s then silent, repeats 0.17 s on resume; a 1 s audio fade hides both; opacity fade to black is smooth |
| B5 Audio while chasing | PASSED, with a note | No drift; one 33 ms nudge per 7.4 min; listening test by Jeff outstanding |
| B6 Six video tracks | PASSED at panel size / FAILED at 1080p H.264 | Panel-sized: 38% CPU at 4K desktop, 19.5% at 1080p, 28% with BEYOND too, smooth 30 fps; six 1080p H.264: 100% CPU |
| B7 ltcplay's own tests | PASSED | main 9291c35: all checks passed in 90.5 s |
| B8 BEYOND | PASSED with required settings | Follows on 127.0.0.2 while MadMapper takes 127.0.0.1, both within a frame over 60 s; blank by OSC brightness 0; turn off "Keep running" or it plays through Hold; OSC port must not be 8000; demo stops after 1 to 2 h (crashed at 2 h overnight, B9) |
| B10 Flame safety program (flamesafe-core d5b398f) | PASSED | Suites pass on Windows; 40.0 packets/s at priority 200, all zero, no sequence breaks, idle and under show load (p99 interval 25.5 ms, 0.84% of a thread); survives a dead destination; clean stop sends zeros then stream-terminated; hard kill stops at once with no zeros (as documented); bad configs refuse with exit 2 |
| B9 Long soak | PASSED (ltcplay, MadMapper, pixels); BEYOND demo FAILED at its 2 h limit | 12 shows of 7:24 every 20 min, 3 h 47 min on main fa5274a: 0 timecode frames skipped, MadMapper 1 to 28 ms behind with no drift, heartbeat never silent over 75 ms, ltcplay 22.1 MB flat, SSD 61 to 64 °C with the box lifted. BEYOND demo crashed on its time-limit box at 2 h and sat frozen but "running". Findings: pixel repeat/skip pairs for 3 min in show 6; BEYOND's own audio probably mixed into MadMapper's output; intermission clip did not loop (bench setup) |
| B11 Web page under show load | PASSED with one viewer / FAILED with several | One open page: no effect on the pixels. Two viewers: 181 repeat/skip pairs and a 79 ms gap. Five: about 7% of frames dropped. Ten: 12 to 14 fps all show. The page also has no Play for the master clock (GO runs pixels only, no timecode) |
| B12 Show JSON saved with a UTF-8 BOM (PR #16, c226d80) | PASSED | main refuses the file (and its page silently leaves the show out of the list); the branch lists it, checks it, verifies it and starts it; selftest passes on Windows |
| B13 Web page load on the snapshot fix (PR #18, e43f415) | PASSED | 2 and 5 viewers: 0 skips (main: 181 and 1,185). 10 unthrottled clients: 40 fps, 623 skips at 313 answered requests/s (main: 12 to 14 fps at 21/s). The page shows GO within 13 ms, Stop 134 ms, Run 82 ms |
| B14 MadMapper and BEYOND link modules (PR #17, 4d91045) | PASSED, 4 notes | Banks, fades, blank and unblank all work as in B2, B4 and B8; BlackOut and MasterPause refused, nothing sent; watchdog alarms 3.0 s after a freeze and recovers within 10 ms; Hold order silent and dark. Notes: one false drift line per recovery; MadMapper sits 2 frames further behind after a Resume; stop does not rewind a non-chasing bank; BEYOND's own audio reaches the show output |
| B15 MadMapper offset over 10 Holds in one show | No ratchet, no recovery | -10 ms before any Hold; -42 after Hold 1, -57 after Hold 2, then creeping back about 2 ms per Hold to -43 over the last 60 s; flat within seconds of each resume; worst -64 ms, inside the 100 ms allowance; every freeze lands exactly on the held frame |
| B16 Timecode by broadcast (Jeff's yes) | FAILED for MadMapper / PASSED for BEYOND | Broadcast to 192.168.7.255 and 255.255.255.255 reached BEYOND (listening on 0.0.0.0) but not MadMapper (listening on 192.168.4.42 and 127.0.0.1 only); the show must send to each program's own address |
| B17 BEYOND long run on the demo | PASSED for the 2 h the demo allows | 6 shows: BEYOND followed each identically (beams 0.23 to 0.32 s after start, dark after each show), memory flat at 1.3 GB, no effect on ltcplay (0 frames skipped) or MadMapper (1 to 34 ms). The demo shows its 1 h limit box but keeps running, then crashes at exactly 2 h (3 times now) |

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
- **With BEYOND as well (B6 extra), 2026-09-26 00:32 to 00:35, main 9291c35, 1080p desktop:** three 58 s cues of the full show layout (ltcplay 26,256 pixels + Art-Net timecode to MadMapper on 192.168.4.42 and BEYOND on 127.0.0.2 from `bench58_both_timeline.json`; MadMapper six montage tracks + audio to the Scarlett; BEYOND's DemoShow timeline with its stock beam effects, preview on screen). **CPU 28.1% average, 61.8% peak; GPU 21.7% average, 25.8% peak;** clock 226% of base. The same without BEYOND (res_1080, 22:09): CPU 19.5%, GPU 19.9%. **So BEYOND adds about 9 points of CPU (about 0.7 of a thread) and about 2 points of GPU.** Pixels unaffected: median 40 fps, 3 frames skipped in 3 minutes, 99% within 2.1 ms (median second), longest gap 43 ms. Memory: BEYOND 584 MB, MadMapper 2.14 GB.
- **ltcplay rejects a show file with a UTF-8 BOM** ("Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1"). Windows PowerShell and Notepad often write one. For the main session: read show files with `utf-8-sig`, or say so in the error.

## B7 ltcplay's own tests

**Verdict: PASSED.** `python -u selftest.py` on main `9291c35`: last line **"all checks passed in 90.5s"**, no FAIL lines (log `selftest_main9291c35_2026-09-25.log`). Earlier: `aa7e6a8` all passed in 82.0 s; PR #8 branch all passed in 84.3 s.
- Pixel timing on this machine (from the selftest): "80 frames in 2.0s, mean interval 25.00ms (target 25ms), worst deviation 7.97ms, 90th pct deviation 2.55ms"; "time.monotonic is GetTickCount64() (resolution 1.56e-02s), time.perf_counter is QueryPerformanceCounter() (resolution 1.00e-07s) on win32; DIFFERENT".
- Under show load (10 min, 1080p): with PR #8 **1 frame skipped**, 99% of frames within 1.9 ms; the old main without PR #8 **3,728 frames skipped (16%)**, 11 ms. Worst frame lateness in the 2 h soak on the PR #8 build: 44.5 ms against the cue's clock, median -1.6 ms.

## B8 BEYOND

**Verdict: PASSED for following Art-Net timecode on the same PC as MadMapper (with a split of loopback addresses) and for a blank command; the default "Keep running" setting FAILS Hold (the lasers keep playing through a freeze) and must be turned off.** BEYOND 5.5 **Essentials Demo** (activated by Jeff about 23:00; **the demo quits after about 1 hour per launch**; on the overnight launch it instead showed "Demo Time Limit Reached" at 2 h and crashed, see B9; either way it bounds any soak that includes BEYOND). No laser hardware connected (checked: no Pangolin device in Windows, both Ethernet ports unplugged); BEYOND drew only to its on-screen preview. BEYOND runs on the Pico with Andy's licence (Jeff, 2026-09-25; handoff section 2 is out of date). 2026-09-26 00:00 to 00:35, tctest from main 9291c35.

**Settings, click by click** (BEYOND keeps these between launches, unlike the MadMapper trial):
1. First launch: language box (English), then "Welcome to BEYOND" > **Go BEYOND...**, then "Select BEYOND version" > **BEYOND Essentials**. It then opens a Pangolin promo video in VLC (close it) and, after an unclean exit, a "Problem encountered during the last session" box (**Cancel**, then **No** to deleting the logs). My screen capture could not see some of these boxes; they are ordinary Windows dialogs and I pressed their buttons by name.
2. **Settings > Configuration > Timecode In:** Timecode routing **Always to Time line**; Timecode types: **Enable Art Net timecode** on, Enable MIDI timecode off, SNTC off; Time smooth filter: **Keep running even though timecode stops OFF** (default ON, see B8.2); Enable time smooth filter on; Timecode timeout 1.0 s. **OK.** `B8_timecode_in_set.png`, `B8_timecode_in_nokeeprunning.png`.
3. Settings > Configuration > **Network:** ArtNet adapter "Automatic (default connection)" (left as is; BEYOND then binds 0.0.0.0:6454). `B8_network.png`.
4. Timeline view (**Timeline** button). The Essentials demo opens its own "DemoShow" (audio, video and scanner tracks with stock beam effects), used as the test content.
5. **File > Show properties > Time code input: Enable incoming timecode** on (offset 0, Add). Without it, BEYOND shows a pop-up that the show does not have timecode enabled (Jeff saw it; my capture did not). `B8_show_tc_input.png`.
6. Toolbar **TC-IN** on. **It switches itself off** after BlackOut, after a Configuration OK and after some OSC commands (below); when it is off, timecode is counted ("ArtNet TC IN ... messages") but the timeline does not move. Check it before every show.
7. **Settings > OSC > OSC Settings:** Enable receiving OSC messages **on** (default off), Incoming port **8100** (default **8000, the same as MadMapper's**: on one PC they must differ). `B8_osc_settings.png`. Settings > OSC > **OSC Monitor** lists every message received (used as evidence below).

**B8.0 Both at once, one PC.** Who holds UDP 6454 with both running: MadMapper binds **127.0.0.1:6454 and 192.168.4.42:6454** (specific addresses); BEYOND binds **0.0.0.0:6454** (every address). Neither complained. Windows gives a packet to the most specific binding, so:

| tctest destination | MadMapper | BEYOND |
|---|---|---|
| 127.0.0.1 | follows | not received (timeline still; message count unchanged) |
| 192.168.4.42 (Pico's own Wi-Fi address) | follows | not received |
| **127.0.0.2** (any other loopback address) | not received | **received and follows** (240 of 240 messages counted in 8 s) |
| broadcast | not received (B16) | received (B16) |

**So: MadMapper on 127.0.0.1 (or the card's address), BEYOND on 127.0.0.2, and ltcplay sends each packet to both nodes.** One tctest to both (`--node MadMapper=192.168.4.42 --node BEYOND=127.0.0.2 --seconds 60`, `scratch/b8_both.ps1`): at +5, 15, 30, 45 and 59 s, BEYOND's display read 00:04:52, 00:14:52, 00:29:48, 00:44:52, 00:58:52 (s:1/60) and MadMapper's heartbeat position 4.850, 14.850, 29.832, 44.848, 58.848 s: **both within one 60 fps frame of each other at every check, over 60 s.** `B8_0_both_beyond.png`. On the rack network the same rule will apply to the card's address: whichever program binds the specific address gets the packets, so BEYOND should listen on its own address (a second Ethernet port, or 127.0.0.2 if ltcplay is on the same PC).

**B8.1 Lock time** (`scratch/b8_lock.ps1`, BEYOND's time display polled): counter first changed **149 ms** after the first packet (first run, from a stopped timeline), later runs changed before the first printed line (it was still moving from the previous run), and **33 ms** after a clean restart at 00:31. At +2.85 s after the first packet BEYOND read 00:02:49 or :50 (1/60 s units) = 2.82 s, i.e. about 30 ms behind the stream. `B8_lock_summary.png`.

**B8.2 Frozen timecode** (`scratch/freeze_sender.py` to 127.0.0.2: 20 s normal, 15 s on frame 00:00:19:29, then on; `B8_2_summary_keeprunning_on.png` and `_off.png`):
- **"Keep running even though timecode stops" ON (the default): BEYOND ignores the freeze and keeps playing** (display 00:21:56 at +22 s, 00:33:56 at +34 s, preview full of moving beams), then jumps back to 20:54 when timecode moves again. **Unsafe for Hold: the lasers keep running.** It also kept playing after timecode ended altogether.
- **OFF:** BEYOND runs on about 1 s after the last new frame (to 00:20:59, the 1.0 s timeout), then **stops and the preview goes black** for the rest of the freeze; on resume it jumps to the current time (20:54) and follows. After timecode ends it stops about 1 s later. So with this setting a frozen or lost timecode blanks the lasers within about 1 s. That is not a static beam, but it is 1 s of run-on, so Hold should still send a real blank first.

**B8.3 Blank commands by OSC** (to 127.0.0.1:8100, while BEYOND chased tctest; `scratch/b8_blank.ps1`, `b8_unblank.ps1`, `b8_bright.ps1`; every message confirmed in BEYOND's OSC Monitor, `B8_3_summary.png`, `B8_3b_summary.png`, `B8_3c_summary.png`). Addresses from Pangolin's OSC list (wiki.pangolin.com, beyond:osc_commands).

| Message (type) | What BEYOND did |
|---|---|
| `/beyond/general/BlackOut` (no args) | **Output stops at once; preview black.** The toolbar goes from "Stop output" to "Show it now". But BEYOND's Blackout also resets live controls and **restarts the application core** (Configuration > Blackout page, `B8_blackout_settings.png`), and afterwards **TC-IN was off**. Output comes back only by pressing "Show it now"; a second BlackOut does not undo it. |
| `/beyond/general/EnableLaserOutput` | Did **not** bring output back after BlackOut (preview stayed black). |
| `/beyond/general/DisableLaserOutput` | Accepted (in the monitor); no visible change in the preview (the laser-output enable only matters with hardware attached). |
| `/beyond/master/livecontrol/brightness` `,f` 0 then 100 | **Preview black at 0, back at 100, timeline keeps running and TC-IN stays on.** The cleanest blank/un-blank for Hold. |
| `/beyond/general/MasterPause` `,i` 1 then 0 | Pause button lit; beams frozen (a static frame = a static beam, the unsafe case); `,i 0` released it. |

**Recommendation for Hold:** brightness to 0 (the blank), with "Keep running" off as the backstop, and brightness 100 on Resume. Avoid BlackOut for Hold (it needs a button press to recover and turns TC-IN off) and never MasterPause. Brightness is a *preview* result: with Andy's hardware, check that brightness 0 truly blanks the laser output (not just dims it).

**B8.4 Changing source port:** tctest sends from an ephemeral port (a new one every run); BEYOND accepted every run (message counter and timeline). PASSED.

**B8.5 Show vs intermission:** NOT TESTED. Both start at 00:00:00:00. Candidates from Pangolin's OSC list: `/beyond/general/StartCue` / `SelectCue` (string) to select a different timeline show before the clock starts, or an hour offset per show in File > Show properties > Time code input. Needs Andy's show files. **What is already known (2026-09-26):** in the layout the soak used, ltcplay sends no timecode between shows and MadMapper's intermission bank does not chase. So with "Keep running" off, BEYOND stops and goes black about 1 s after each show's timecode ends (B8.2). With no second BEYOND show, **the lasers are dark for the whole intermission**, which may be exactly what is wanted. A laser look during intermission needs either a second BEYOND show selected by OSC, or its own timecode range. That is a question for Jeff and Andy before it is a test.

## B10 The flame safety program on Windows

**Verdict: PASSED on every item.** Branch `flamesafe-core` at **`d5b398f`** (PR #12, not merged), in its own worktree `C:\Users\VIOSO\Desktop\Show\wt-flamesafe`; no code changed. Run on Jeff's first-hand approval in this session (2026-09-26, "When the other session says flame related code testing is ready, I give you explicit permission"), after the main session said it was ready. **No flame hardware anywhere in the building (Jeff); both Ethernet ports unplugged (checked); every packet went to 127.0.0.1.** 00:36 to 00:47.

Config: a copy of `flamesafe\flamesafe.example.json` (unconfirmed rev 6 numbers, universe 1, 6 groups, example link key) with only `destination` changed to **127.0.0.1:5578** (a throwaway listener) and `log_dir` set; `scratch\flamesafe_bench.json`. Throwaway tools: `scratch\flame_sink.py` (E1.31 listener logging source, priority, universe, sequence, options and whether all 512 slots are zero) and `scratch\b10_run.py` (starts `python -m flamesafe`, measures its CPU, stops it cleanly with Ctrl+Break or hard with TerminateProcess, the same as Task Manager's End task). flamesafe was run with the **base Python 3.12** (`C:\Users\VIOSO\AppData\Local\Programs\Python\Python312\python.exe`): the venv's `python.exe` on Windows is a launcher that starts the real interpreter as a hidden child, so a kill or CPU reading aimed at the launcher misses flamesafe itself. **Worth knowing for the real launcher and the Task Scheduler entry.**

1. **Suites on Windows:** `python -u -m flamesafe.test_flamesafe`: **"flamesafe: all checks passed in 11.7s"**, no FAIL lines. `python -u selftest.py` in the same worktree: **"all checks passed in 106.1s"**, no FAIL lines (pixel note: 81 frames in 2.0 s, mean 25.00 ms, 90th percentile 0.44 ms).
2. **Idle, 60 s:** **2,482 packets in 61.90 s = 40.08 per second**; interval min 0.00, **mean 24.95, 99th percentile 25.64, max 33.30 ms**; **priority 200** on every packet; **universe 1**; **every one of the 512 slots zero in every packet** (nothing can arm without the Stream Deck, and the journal says so: "no arm input in this build: every group stays disarmed and the flame universe is all zeros"); **sequence numbers continuous (0 breaks, wrapping at 255)**; source name `flamesafe`.
3. **Under show load, 3 minutes** (the full layout: ltcplay 26,256 pixels + Art-Net timecode to MadMapper and BEYOND, MadMapper six montage tracks + audio, BEYOND timeline): **7,003 packets in 174.93 s = 40.03 per second; interval mean 24.98, 99th percentile 25.46, max 25.90 ms**; priority 200, universe 1, all zero, 0 sequence breaks. **flamesafe CPU 0.84% of one thread** (idle runs: 0.44% and 1.08%). The show's own pixels in the same run: median 40 fps, 3 frames skipped in 3 minutes, longest gap 52 ms; system CPU 27.0% average, GPU 21.8%.
4. **Nobody listening (the SIO_UDP_CONNRESET case):** flamesafe sent to 127.0.0.1:5578 with no listener for 30 s (and its status frames to 127.0.0.1:5572 with no listener for the whole run): **no crash, no error lines in the journal** (4 lines total: two config, start, stop), CPU 0.44%. **A listener started at +30 s received packets at once: 1,289 in 32.08 s = 40.16 per second**, all zero, 0 sequence breaks.
5. **Kill tests:**
   - **Clean stop (Ctrl+Break, handled the same as Ctrl+C):** exited in 27 ms with code 0. The listener saw the last normal packet (seq 172, all zero), then **3 all-zero packets (seq 173 to 175, options 0) then 3 all-zero packets with the stream-terminated bit (seq 175 to 177, options 0x40)**, all within the same millisecond; journal "stop: flame universe zeroed and the stream terminated".
   - **Hard kill (TerminateProcess):** **the last packet arrived 20 ms before the kill call** (the next 25 ms tick never came), **no zeros and no stream-terminated packets after it**, and no "stop" line in the journal, exactly as `service.py` and CONTRACT.md describe ("A hard kill sends no zeros"). Here the last packet was all zeros because nothing was armed; with a group armed and firing, the node would hold that last packet until its own sACN-loss timeout. **The "dead PC" case therefore rests on the PixLite Aux port's loss behaviour and each G-Flame's Max. Flame Duration** (CONTRACT.md bench items 1 and 2, which need the real hardware).
6. **Bad configs** (each run with `python -m flamesafe <file>`):
   - A misspelt key (`destinaton`): **exit 2**, "flamesafe will not start: the config has a key this program does not know: destinaton. Check the spelling against flamesafe.example.json."
   - A missing file: **exit 2**, "flamesafe will not start: the config file C:\Users\VIOSO\Desktop\Show\scratch\does_not_exist.json cannot be read: No such file or directory."
   - Two heads on one fire slot (cat-walk's first fire slot set to 411, front row's): **exit 2**, "flamesafe will not start: cat-walk and front row share fire slot 411. Arming either one would zero that slot for 3 frames under the other."
   - No argument: exit 2, "Usage: python -m flamesafe <config.json>".
7. **Journal:** to stdout and to `<log_dir>\flamesafe.log` (here `C:\Users\VIOSO\Desktop\Show\scratch\flamesafe_log\flamesafe.log`, 15 lines over 5 runs), one line per event, local time to the second:

```
2026-09-26 00:43:22  config: no arm input in this build: every group stays disarmed and the flame universe is all zeros
2026-09-26 00:43:22  start: flame universe 1 to 127.0.0.1:5578 at sACN priority 200, 40 Hz; frames in on 127.0.0.1:5571, status out to 127.0.0.1:5572
2026-09-26 00:46:17  stop: flame universe zeroed and the stream terminated
```

   Each start also writes the config's UNCONFIRMED note in full. With `log_dir` null (the example's default) the journal goes to stdout only: on a show PC with no console that means no file, so the real config should set `log_dir` (under `%LOCALAPPDATA%`, per handoff section 10).

Not tested (needs the Stream Deck driver, build step 7b): arming, fire values passing, the dwell, the link from ltcplay (no ltcplay side exists yet), status-frame contents.

## B9 Long soak

**Verdict: PASSED for ltcplay, MadMapper and the pixels over 3 h 47 min: 12 shows of 7:24 on the handoff's 20-minute interval, with an intermission bank between shows. BEYOND FAILED at 2 h, but that is the demo's time limit, not the show layout (below). Two small findings for the main session: a patch of pixel frame repeat/skip pairs in show 6, and the intermission clip not looping.** Ran 2026-09-26 00:58 to 04:46 on main **`fa5274a`** (the approved soak plan), with the Pico lifted about an inch for airflow. Both Ethernet ports were unplugged and all traffic went to 127.0.0.1 or 127.0.0.2. Audio went to the Scarlett with nothing connected to its outputs.

**What ran.** Throwaway driver `bench_show/drive_soak.py` (not in the repo). It opened ltcplay's `Session` on `bench444_timeline.json`: one cue, `bench444.fseq`, 26,256 pixels, 155 universes, 444.42 s (the length of Jeff's music). Art-Net timecode went to MadMapper at 192.168.4.42 (the Pico's own Wi-Fi address) and BEYOND at 127.0.0.2. Every 1200 s it did this:
1. OSC `/timelines/Bank-2/conductor/stop` and `/timelines/Bank-1/select` to MadMapper, 2 s before the slot.
2. `clock_play("Bench")`.
3. Read ltcplay's last-sent timecode at 60, 220 and 440 s.
4. When the cue ended, selected Bank-2 (the intermission, not chasing) and started it with `play_from_beginning`.

MadMapper ran six looping panel-sized montage tracks, Bank-1's audio clip, and the B3 heartbeat (an OSC Float track ramping 0 to 1 over 444.42 s, sent to 127.0.0.1:9001). BEYOND ran its DemoShow timeline chasing timecode, with "Keep running" off.

The recorders were:
- the pixel sink (every packet, per-second stats),
- the heartbeat listener,
- the LTC decoder on the Scarlett loopback (right channel),
- a 1 s CPU and GPU sampler,
- a 10-minute memory and event-log snapshot,
- the SSD watchdog and flight recorder.

**Results, all 12 shows:**

| Item | Result |
|---|---|
| Show length (ltcplay) | 444.51 to 444.52 s every show (cue 444.42 s plus the end frame) |
| Timecode | **0 frames skipped in 12 shows** (319,992 packets to the two nodes), 0 send errors |
| ltcplay memory | 22.0 MB after show 1, **22.1 MB after shows 2 to 12** (no growth) |
| MadMapper position vs ltcplay (heartbeat × 444.42 s at 60, 220, 440 s, 36 readings) | **MadMapper 1 to 28 ms behind** ltcplay's clock, mean 15.7 ms, median 14.4 ms. **No drift within a show, none across the night.** Per-show means: -24, -13, -17, -11, -20, -18, -17, -8, -17, -14, -18, -11 ms. Every reading is under one 30 fps frame (33 ms). |
| Heartbeat | 26,664 to 26,668 packets per show (60/s); first packet 10 to 22 ms after ltcplay started; **longest gap 75 ms (shows 2 and 3; 23 to 33 ms in the others), 0 gaps over 100 ms, 0 over 3 s**; silent within 0.05 to 0.08 s of the end |
| Heartbeat between shows | One packet per intermission, value 1.0, **exactly 2 s before each show**: that is MadMapper re-sending the Float track's current value when the driver selects Bank-1 again. Otherwise silent. **A heartbeat watcher must expect one packet at bank select.** |
| Pixels | 40 fps median. Skipped frames per show: 0, 1, 0, 3, 0, **51**, 0, 0, 0, 0, 2, 0. **Longest gap 58 ms**, 1 incomplete frame in 5,340 show seconds, 0 send errors. Show 6 is explained below. |
| Audio (LTC on Bank-1's right channel, decoded from the Scarlett) | Steady within every show: the first half and second half medians are identical to the millisecond, so **no audio drift in 7:24**. The offset from ltcplay's start differs between shows: -217 to -272 ms. That includes the recorder's own capture latency, so the spread (55 ms, under 2 frames) is what matters, not the absolute value: it is where MadMapper's audio locks each time a cue starts. |
| Intermission | Bank-2's LTC (hour 01) started **within about 1 s of every show ending**, 11 of 11. Show start after intermission: the decoder saw 00:00:00:01 about 0.25 s after ltcplay started. |
| MadMapper memory | 2,387 MB at 00:55, **2,400 MB at 04:45**: +13 MB in 3 h 50 min (about 3.4 MB an hour). Not a concern for a show night. |
| CPU / GPU | CPU mean 22.5% (23.8% while BEYOND ran, 21.9% after); GPU 3D 21.6%. The highest one-second samples (72 to 87%) came at 00:56, 01:27, 01:57 and 02:45, each for a single sample. |
| SSD | 61 to 64 °C for the whole run. The drive's own worst read/write latency counters stayed at their since-boot values (1,257 / 720 ms, set at boot 19:28). **No new stall.** The watchdog raised no alarm. |
| Windows | **0 new error or critical events in the System log**; 0 unexpected shutdowns. The Windows Time service is not running, so the clock was never adjusted during the run. |

**BEYOND: the demo's time limit, then a crash (FAILED, demo only).** BEYOND was launched at 23:53:18. At **01:53:22, exactly 2 hours later**, the Essentials Demo opened a **"Demo Time Limit Reached"** window, and while that box was up BEYOND crashed. BEYOND's crash handler wrote a local problem report (`C:\BEYOND55_Demo\Log\SendToPangolin---BEYONDProblemReport.txt`; nothing was sent). It gives:
- program up time 2 hours,
- version 5.5.0.1919,
- `EAccessViolation: Access violation at address 000000000273CA6A in module 'BEYOND.exe'. Read of address 00000000000000C0`,
- a main-thread call stack inside `MessageBoxW`.

From then on BEYOND showed "An error occurred in the application" (continue / restart / close, `B9_show6.png`). Its main window was disabled, so **it followed nothing for shows 4 to 12**. The process stayed alive: 600 to 646 MB, Windows "Responding", about 1% CPU. **A check that only asks whether BEYOND.exe is running would not have noticed.**
- This is the demo's limit. B8 said the demo quits after 1 hour; the earlier launches that day did end after 61 and 70 minutes, but this one ran to 2 hours and then crashed instead of quitting. The crash sits inside the demo's own message box. It says nothing about Andy's licensed copy, which has no time limit, **but the licensed BEYOND still needs its own multi-hour soak on this PC before opening night**.
- For show control: a laser program can be up but frozen on a dialog. Watching its OSC or the preview is the only way to know it is following. The same B3-style heartbeat idea could apply if BEYOND can send OSC on a timeline event.

**Audio decode was noisy while BEYOND was alive.** Shows 1 to 3 had about 1,450 decoder jumps each; shows 4 to 12 had **1 each** (the restart at 00:00:00:00). The only change at 01:53 was BEYOND stopping. BEYOND's DemoShow has its own audio track. **Most likely BEYOND was playing its demo audio into the same Windows output, mixed with MadMapper's.** I did not prove this; the logs are encrypted. **For the show: in BEYOND, set its audio output to none (or a different device) so it cannot mix into MadMapper's output to the DSP.** This also means the earlier finding that "LTC under music decodes about 90% clean" (B5) was probably BEYOND's audio, not resampling.

**Pixels in show 6: repeat/skip pairs.** From 02:39:20 to 02:42:34 (1:14 to 4:28 into show 6), 45 seconds each had one frame repeated and the next frame index skipped: 39 frames in the second, **gap never over 48 ms**, the right content one frame late then back. Then, at 02:42:38, ltcplay's pixel timing against the cue stepped by 23.5 ms. The median lateness moved from -1.8 ms to -25.3 ms and stayed there for the rest of the show. For comparison, show 5 held a steady -3.2 ms.
- CPU was normal (15 to 23%) from 02:39 to 02:41, before I took any screenshots. From 02:41:03 to 02:42:41 I was capturing the screen and searching BEYOND's files, and CPU went up to 58%. That may have made the second half worse, but it did not start it.
- No other show did this. There were no clock changes (the time service is off) and no event log entries.
- **For the main session:** worth a look at what makes the pixel scheduler repeat one frame and skip the next one about every 5 to 20 s, and then re-anchor 23.5 ms later. It is invisible on LEDs at this size (one frame late for 25 ms), but it is not the clean 40 fps of the other 11 shows.

**Intermission clip did not loop.** Bank-2 played its 120 s LTC clip (`LTC_intermission_hour1_120s.wav`) once after each show and was then silent for the rest of the 12.5-minute gap (`B9_intermission_after_show12.png`: timeline at 3:20 and still playing, clip bar longer than the audio). This is my bench setup, not a MadMapper fault: the timeline is longer than its clip. **For Jeff's real intermission: make the Bank-2 timeline exactly as long as its content (or loop the clip itself), and check it loops once, by ear.**

**Bench caveat:** at 02:41 the Bank-1 audio clip was labelled `LTC_audio_track_460s.wav` in MadMapper (`B9_show6.png`), not the music-plus-LTC file I meant to load. The timecode channel measurement stands. I cannot say from this run whether Jeff's music played on the left channel.

**Earlier soaks the same day** (for the record): soak 1, 7:20 cues with 30 s gaps, 1 h 34 min clean until an accidental unplug; soak 2, 120 cues of 58 s, 2 h, timecode 19 skipped in 208,781, pixels no drift, SSD 32 to 67 °C, no stall. The two unexpected shutdowns of 09-25 (10:12 freeze, 15:30 unplug) were the only problem events.

Evidence: `bench_evidence/B9_show6.png`, `B9_intermission_after_show12.png`, `B9_soak3_analysis.txt` (per-show heartbeat, drift points, pixels, memory), `B9_beyond_problem_report_head.txt`.

## B11 ltcplay's web page under show load

**Verdict: PASSED with one page open; FAILED with several viewers. Each extra viewer of the page costs the pixels frames: two viewers cause occasional repeat/skip pairs and one 79 ms gap, five drop about 7% of frames (31 fps in the first minute), and ten flat out drop the pixels to 12 to 14 fps for the whole show.** Also: **on main the page has no control that starts the master clock.** 2026-09-26 05:01 to 05:49, main **`fa5274a`**, `python -m ltcplay.cli serve --folder bench_show --no-browser` (the ltcplay venv), bound to 127.0.0.1:7878. Show `bench444_timeline.json` (26,256 pixels, 155 universes, 40 fps). MadMapper was playing its six panel tracks (not chasing: see the next paragraph). BEYOND was not running (crashed, B9). Ethernet unplugged. Every run was 7:24 of pixels, recorded by the same pixel sink as B9.

**The page cannot start the show clock.** With `"clock": {"source": "artnet_master"}` the page validates the show ("Show clock: artnet_master, Art-Net timecode to MadMapper 192.168.4.42, BEYOND 127.0.0.2"). It starts it with **Run, and send to the rig** (state STANDBY, output black) and shows the clock's status. But the only way to start playing from the page is **GO** ("Run on this Mac's clock"). GO calls `player.go()`, a free run of the pixels, and **sends no Art-Net timecode**, so MadMapper and BEYOND do not move. `Session.clock_play()` has no route in `web.py`. So every run below is a GO free run of the pixels, started with the page's own `/api/go` (`{"at": "Bench"}`), without timecode to MadMapper. **For the main session:** the operator needs a Play (and Hold/Resume) for the master clock on the page, or the show can only be started from code.

The page itself polls `/api/state` about 4 times a second, and `/api/log?n=40` about every 2.5 s. The in-app browser, measured over 10 s: 42 requests.

| Run | Viewers | Pixels over 7:24 | Longest gap | Page requests | CPU (whole PC) |
|---|---|---|---|---|---|
| 1 | **one real page** (Chromium, visible) | 40 fps; **2 skips, 2 repeats** | 49 ms | ~4/s; each `/api/state` **61 ms average** during the show (2.6 ms idle), max 85 ms | 17.1% |
| 2 | **none** (page closed, GO by `curl`) | 40 fps; 2 skips, 2 repeats | 46 ms | none | 11.1% |
| 6 | **two** page-like clients (`scratch/web_hammer.py 2 420 … 0.25`) | 40 fps in most minutes; **181 skips, 178 repeats**; 43 seconds under 39 fps | **79 ms** | 8/s, median 88 ms, max 218 ms | 25.4% |
| 5 | **five** page-like clients | **1,185 skips** (about 7% of frames); **31 fps in the first minute**, then 36 to 39 fps | 58 ms | 19/s (could not keep 4 Hz each), median 166 ms, max 535 ms | 61.0% |
| 4 | **ten** clients polling as fast as they can | **12 to 14 fps for the whole show**; **11,160 skips**; every second under 39 fps | **133 ms** | 21/s, median 492 ms, max 921 ms | 68.2% |

- **Timing held; the frame rate did not.** In every run the frames that were sent stayed on the cue's time (lateness median -12 to +12 ms, worst 30 ms): the engine drops frames to stay on time rather than drifting. On LEDs that is a stutter, not a slip.
- **One viewer costs nothing measurable** (run 1 vs run 2: the same 2 skips and 2 repeats, the gap 49 vs 46 ms). The 6% extra CPU is the browser drawing the page.
- Each `/api/state` answer took 2.6 ms with nothing playing and 61 ms during the show. Ten clients got only 21 answers a second in total. The server is a `ThreadingHTTPServer`, and `state()` takes no lock, so this looks like **the web threads and the pixel loop competing for Python's interpreter lock in one process**. That is a guess from the numbers; I did not profile it.
- The engine survived everything. There were 0 failed requests, and `serve.err` shows only 5 `ConnectionResetError` tracebacks, from clients hanging up at the end. `/api/stop` blacked out and stopped cleanly. The engine used 3,634 CPU seconds in 48 minutes, more than one core on average, mostly during runs 4 and 5.
- `sounddevice is not installed or could not load PortAudio` shows in the INPUT panel. That is expected: this venv was built without it and the master clock needs no input.

**What this means for the show:** until the main session changes this, **only one device should have the ltcplay page open during a show** (the operator's). Crew phones and iPads should not keep it open. Worth fixing before opening night: the rig will be run from a page, and a second tablet left open on a shelf is realistic. Ideas for the main session, in plain words: answer `/api/state` from a snapshot the engine refreshes a few times a second, rather than building it per request; poll less often; or serve the page from a separate process.

Evidence: `B11_web_runs.txt` (the per-run summaries above, from `scratch/web_px.py` and `scratch/web_hammer.py`).


## B12 Show files saved with a UTF-8 BOM (PR #16)

**Verdict: PASSED.** Branch `accept-utf8-bom` at **`c226d80`** (PR #16, not merged), in its own worktree `wt-bom`, run on 2026-09-26 at 06:00; no code changed. The test show was a copy of the bench444 folder (`bench_bom\`, the FSEQ hard-linked), with the show JSON re-saved by PowerShell 5.1 `Set-Content -Encoding UTF8`. Its first bytes are **EF BB BF 7B**; the original's are 7B 0D 0A. Loopback only, no output to anything.

| | main `fa5274a` | branch `c226d80` |
|---|---|---|
| `check` | exit 2, "Unexpected UTF-8 BOM (decode using utf-8-sig)" | reads the cue (444.4 s, 78,768 ch at 25 ms); exit 1, the same as main on the no-BOM file, because this venv has no `sounddevice` |
| `verify` | exit 2, same error | exit 0, "Every sequence is the one this show file names" |
| `serve`: show list (`/api/timelines`) | **the show is not listed at all** (empty list, no error shown) | listed: "Fire and Ice bench", 1 cue |
| `serve`: Validate (`/api/check`) | ok false, BOM error | ok true |
| `serve`: Run (`/api/start`, no_output) | HTTP error, BOM error | started, STANDBY, display only; `/api/stop` clean |
| `selftest.py` on Windows | | **"all checks passed in 99.1s"**, 0 FAIL, including the five new BOM checks |

A side note from the same run: `check` counts a missing `sounddevice` as a Problem (exit 1) even for a show whose clock is `artnet_master`, where no audio input is used.

## B13 Web page under show load, again, on the snapshot fix (PR #18)

**Verdict: PASSED. The fix works: two and five viewers now cost the pixels nothing (0 skips), where main dropped up to 7% of frames. Ten unthrottled clients, answered 15 times faster than main could, hold 40 fps with 623 skips (main: 12 to 14 fps and 11,160 skips). The page still reacts to its own buttons at once.** Branch `web-state-snapshot` at **`e43f415`** (PR #18, not merged; contains main `fa5274a`), in its own worktree `wt-snap`, with no code changed. Run on 2026-09-26 from 08:05 to 08:45 using B11's method exactly: the same bench444 show (26,256 pixels, 40 fps), GO free runs, the same pixel sink, `scratch/web_hammer.py` and `scratch/web_px.py`. Loopback only, Ethernet unplugged. BEYOND was not running.

| Viewers | main `fa5274a` (B11) | branch `e43f415` |
|---|---|---|
| none | 2 skips, 2 repeats; longest gap 46 ms; CPU 11.1% | 1 skip, 1 repeat; 49 ms; CPU 11.5% |
| one real page (in-app Chromium) | 2 skips, 2 repeats; 49 ms; CPU 17.1%; `/api/state` 61 ms average (max 85) | 4 skips, 6 repeats; 59 ms; CPU 14.6%; `/api/state` **31 ms** average (max 69) |
| two page-like clients (4 Hz each) | 181 skips, 178 repeats, 43 s under 39 fps; 79 ms; CPU 25.4%; requests median 88 ms | **0 skips, 0 repeats**; 32 ms; CPU 15.2%; requests **median 1.6 ms**, p95 64, max 81 |
| five page-like clients | 1,185 skips (about 7%), 31 fps in the first minute; 58 ms; CPU 61.0%; 19 requests/s, median 166 ms | **0 skips, 0 repeats**; 29 ms; CPU 16.6%; 20 requests/s, **median 1.4 ms**, p95 62, max 118 |
| ten clients, as fast as they can | **12 to 14 fps all show**, 11,160 skips; 133 ms; CPU 68.2%; **21** requests/s answered, median 492 ms | **40 fps median**, 623 skips, 24 repeats, 102 s under 39 fps; 57 ms; CPU 57.2%; **313** requests/s answered, median 5.7 ms, p99 172 ms |

- **The ten-client run's 213,287 "errors" were my tool, not ltcplay.** `web_hammer.py` opens a new connection for every request. At about 800 attempts a second, Windows ran out of local ports: every error was `WinError 10048` ("Only one usage of each socket address"), with about 17,000 connections waiting to close (checked with a 20 s repeat; `netstat` showed 17,250 in TIME_WAIT). The server answered 131,391 requests with no error of its own. `serve.err` holds only `ConnectionResetError` tracebacks from clients hanging up. So the ten-client row is a harder test than on main (15 times the answered requests), not the same one.
- **Where it still costs:** in the two- and five-client runs about 1 request in 20 took around 60 ms (p95 62 to 64 ms), against 1.5 ms for the rest. That looks like the one request per 0.2 s window that rebuilds the snapshot, and so still pays for the hashing the Mac session found. It is harmless at these rates. If it matters later, the build id could be computed once at start, or when the files change, rather than 5 times a second.
- **The page after its own buttons** (one real page, a timer in the page checking its text every 10 ms): **GO to "FREERUN" on screen: 12 to 13 ms** in three repeats (the very first GO of the session took 415 ms). **Stop and black out to "NOTHING RUNNING": 134 ms. Run to "STANDBY": 82 ms.** `/api/go` answered in 2 ms and `/api/start` in 28 ms. The page updates at once after a press.
- Caveats:
  - In the automated runs my batch stopped the recorders 20 s before the end of each show, so those rows cover 424 of the 444 s.
  - BEYOND was not running here, and was not in B11 either.

Evidence: `B13_web_runs.txt`.


## B14 The MadMapper and BEYOND link modules (PR #17)

**Verdict: PASSED on M1 to M5, with four notes for the main session.** Branch `madmapper-link` at **`4d91045`** (PR #17, not merged), in its own worktree `wt-mmlink`, with no code changed. Run on 2026-09-26 from 08:50 to 09:10. The modules were driven directly by a throwaway script, `scratch/b14.py`, against the real MadMapper 6.1.5 trial and a freshly restarted BEYOND Essentials Demo. Timecode came from a throwaway 30 fps Art-Net sender built on ltcplay's own `arttimecode()`. Its "freeze" keeps sending the same frame, which is what ltcplay's Hold sends. It sent to MadMapper at 192.168.4.42 and BEYOND at 127.0.0.2. Loopback plus the Pico's own address only; Ethernet unplugged. The branch's `selftest.py` on Windows: **"all checks passed in 107.8s"**, 0 FAIL.

Measured with:
- the Scarlett loopback, as an LTC decoder and as a 50 ms RMS meter;
- a screen probe of MadMapper's stage view and of BEYOND's laser preview, about every 80 ms;
- MadMapper's heartbeat on 9001.

The screen and audio figures include about 0.1 to 0.3 s of capture delay.

**Before the tests: BEYOND's own sound goes to the show output (confirms the B9 suspicion).** With MadMapper's `master_audio_level` at 0 and BEYOND's DemoShow playing, the Scarlett carried **RMS 0.17**, BEYOND's demo soundtrack. That drops to 0.000 with BEYOND's Audio track switched off (the circle icon on the track header). **Setup rule for the show PC: switch off the audio track in BEYOND's show, or give BEYOND a different audio device.**

**BEYOND after a fresh launch** (recorded for the setup steps):
- Enabling timecode for the show and pressing TC-IN was not enough. The timeline stayed at 00:00:00:00 while its Art-Net counter ran.
- It followed only after **pressing its own Play once, then turning TC-IN back on** during timecode.
- "Show it now" (output on) turns TC-IN off again, so press TC-IN last.
- Timecode stopping switches BEYOND's output off by itself (`TimecodeToEnableOutput=1` in BEYOND.ini), and it came back on by itself when timecode resumed (M5).

| Test | Result |
|---|---|
| **M1 Link bank commands** | **PASSED, as in B2.** The intermission bank's LTC decoded from the output:<br>- `play_from_beginning("Bank-2")` started it at 01:00:00:00 (first frame decoded 0.4 s after the command, including capture).<br>- `stop_bank` ended it within 0.2 s (twice).<br>- `select_bank` swapped the Conductor's bank (screenshots).<br>- `show_ended(1)` stopped Bank-1, selected Bank-2 and restarted it at 01:00:00:02.<br>- `show_started(2)` stopped Bank-2 (audio ended 0.2 s later) and selected Bank-1.<br>**Note:** on a non-chasing bank MadMapper's `conductor/stop` works like pause. The playhead stayed at 0:00:07:05 (`B14_m1_stop_Bank-2.png`), and `play_bank` then carried on from 01:00:07. The module never relies on stop rewinding (it uses `play_from_beginning`), but the comment in `show_ended` that stop leaves the show bank "at zero" is not what the trial does. For the chasing show bank this does not matter: the next timecode positions it. |
| **M2 fades** | **PASSED, as in B2 and B4.**<br>- `fade_audio(1,0)`: level 0.478 to 0.000 in an even ramp over 1.0 s, every 50 ms sample lower than the last; `fade_audio(0,1)` back to 0.478 the same way. Each call returned after 1.001 s.<br>- `fade_video` on Quad-1 to 6: stage brightness 130 to 2.6 (black but for the trial watermark) and back to 140, **the same shape as B4**: it holds for the first half and drops in the last 0.5 s (129.9 128.5 123.6 117.0 98.7 77.7 25.3 2.6). The ramp is linear in opacity, and MadMapper's output is not linear in brightness. A ramp shaped for the eye (for example opacity = t squared) would look more even. Cosmetic. |
| **M3 BEYOND blank and unblank** (port 8100, BEYOND following timecode) | **PASSED.** `blank()`: preview brightness 1.9 to **0.1 by the next sample, under 75 ms**. `unblank()`: beams back (0.1 to 4.8) **within 60 ms**. `health()` = last_command "unblank", packets_sent 2. |
| **M3 forbidden addresses** (a `Beyond` aimed at a throwaway listener on 8199, never at BEYOND) | **PASSED.** `_send("/beyond/general/BlackOut")` and `_send("/beyond/general/MasterPause")` both **raised `BeyondConfigError`** with a plain sentence, and **the listener received nothing from either**. A control `blank()` through the same object did arrive (1 packet: brightness 0.0). Public methods are only `blank`, `unblank`, `health`, `close`. (The guard is an exact string match; nothing public takes an address, so that is enough.) |
| **M4 Watchdog** (real heartbeat on 9001 /float-1, show length 444.42 s, a 60 s show) | **PASSED, with one false note.**<br>- Healthy: heartbeat age never over **22 ms**; drift (MadMapper minus the sender's position) **-3 to -38 ms, median -17 ms** over 512 samples (B9's soak: 1 to 28 ms behind). The default 100 ms flag never fired.<br>- **MadMapper suspended for 5 s** (at 32.4 s into the show): state "fault" and the sentence "MadMapper stopped answering 33 s into show 1. Video may be frozen. The rest of the show carries on." at 35.42 s, **3.0 s after the last packet**. "MadMapper answered again" at 37.44 s, **within 10 ms of the resume**.<br>- **False drift note:** the first packet after the resume carried MadMapper's stale position, so the watchdog logged **"MadMapper is 5016 ms behind ltcplay's own timecode."** and 9 ms later "back in step". One spurious line per recovery. Suggest: skip the drift check for the first packet or so after a recovery.<br>- Disarmed at the end, then timecode stopped: state "quiet", **no alarm** in 16 s.<br>- Between shows, `select_bank("Bank-2")` then `("Bank-1")` while disarmed: `packets_in` stayed **3,327**, and no drift or state change (B9's single select packet is ignored). |
| **M5 Hold order by hand** (`Link(beyond=Beyond)`, `hold(1, clock=sender)`, 8.8 s held, `resume(1, clock=sender)`) | **PASSED; looks and measures right.**<br>- Hold: **BEYOND preview black within one sample (under 60 ms)**, then **music faded 0.35 to 0.000 over 1 s**, then the sender froze at 76.900 s. **Level 0.000 for the whole hold: no audio run-on.** MadMapper froze on exactly **76.900 s** (heartbeat silent 8.67 s). BEYOND's own clock ran on about 1.2 s past the frozen frame ("Keep running" off, then its 1 s timeout) and switched its output off (`B14_m5_held.png`), but it was already blanked.<br>- Resume: sender first, then unblank, then fade up. MadMapper moved on from **76.917 s**, 0.10 s after the sender (no jump). **Beams back 0.17 s after resume**, output switched back on by itself. **Music rose smoothly from 0 to 0.34 over 1 s**; the first sample above silence was 0.008, and no repeat blip showed. |

**Note on MadMapper after a Resume.** Before the Hold, MadMapper was 19 to 26 ms behind the sender. After the Resume it stayed **75 to 82 ms behind** for all 15 s measured, about two 30 fps frames more. The audio's LTC shifted the same way (from -299 to -330 ms, including capture delay). The picture and sound were unaffected, but that is **most of the watchdog's 100 ms drift allowance used up after a single Hold**. Worth checking over a longer run whether it creeps back, and whether repeated Holds add up.

Evidence: `B14_madmapper_link.txt` (driver journals, M4 health samples, the numbers above), `B14_m1_stop_Bank-2.png`, `B14_m5_held.png`.


## B15 MadMapper's offset over ten Holds in one show

**Verdict: no ratchet, and no recovery either. After the first two Holds MadMapper settles about 1 to 1.5 frames later than it started, stays there, and then creeps back about 2 ms with each further Hold. Worst single reading -64 ms: inside the watchdog's 100 ms allowance all show.** Asked for by the main session after B14's note 2. Run on 2026-09-26 at 09:10 to 09:18, with the same throwaway sender and module as B14 (`scratch/b14.py b15`, `madmapper-link` `4d91045`).

The run:
- One show of 444.42 s from 00:00:00:00 to MadMapper (192.168.4.42, Bank-1 chasing, its heartbeat keyframed 0 to 1 over 444.42 s).
- **Ten Holds of 5 s**, at show seconds 35, 75, 115 and so on to 395, each done by `Link.hold(i, clock=sender)`: a 1 s music fade, then the freeze. The freeze keeps sending the same frame. Each Hold ended with `Link.resume(i, clock=sender)`.
- No BEYOND. 14,833 timecode packets, 26,690 heartbeat packets.
- Offset = heartbeat value × 444.42 minus the sender's own position at that instant; negative means MadMapper is behind.
- Analysis: `scratch/b15_an.py`, evidence `B15_offsets.txt`.

| Hold (show s) | 10 s before the fade: median (range) | 10 s after the resume: median (range) | after the resume: 0 to 2 s / 2 to 5 s / 5 to 10 s |
|---|---|---|---|
| before any Hold (5 to 34 s) | **-10 ms** (-25 to +8) | | |
| 1 (35) | -10 (-16 to -4) | **-42** (-48 to -36) | -42 / -42 / -42 |
| 2 (75) | -42 | **-57** (-64 to -51) | -57 / -57 / -57 |
| 3 (115) | -57 | -55 | -55 / -55 / -55 |
| 4 (155) | -55 | -53 | -53 / -53 / -53 |
| 5 (195) | -53 | -52 | -52 / -52 / -52 |
| 6 (235) | -52 | -50 | -50 / -49 / -50 |
| 7 (275) | -49 | -48 | -47 / -48 / -48 |
| 8 (315) | -47 | -46 | -46 / -46 / -46 |
| 9 (355) | -46 | -44 | -44 / -44 / -44 |
| 10 (395) | -44 | -42 | -43 / -43 / -42 |
| **last 60 s** | **-43 ms** (-50 to -36) | | |

- **The three questions:**
  - Ratchet? **No.** Only Holds 1 and 2 moved it later (+32 ms, then +15 ms). Holds 3 to 10 each moved it about 2 ms earlier.
  - Recover within seconds? **No.** It is flat to the millisecond from 0 to 10 s after each resume, and the same 40 s later, just before the next Hold.
  - Stay at +2 frames? **About that.** It stayed 1 to 1.5 frames (33 to 47 ms) later than at the start, and ended the show at -43 ms.
- **The freeze itself is exact every time.** MadMapper's last position before each freeze was the held frame exactly (35.000, 75.000 … 395.000), reached 0.23 to 0.29 s after the sender froze: MadMapper's own lag, not run-on past the frame. Its first position after each resume was 1 frame on (35.017 …), 0.08 to 0.10 s after the sender restarted. **No jumps**, and no heartbeat while held.
- For the watchdog: the default 100 ms drift allowance was never reached (worst -64 ms, after Hold 2). An operator who holds a lot will see the drift figure sit around -40 to -60 ms rather than -10 to -25.
- Why the first Holds move it and later ones do not was not measured. It looks like how MadMapper re-locks after timecode stops, not like anything ltcplay sends: the sender's timing is identical before and after each Hold (the same 30 fps deadlines, the held frame repeated, then the next frame).


## B16 Art-Net timecode by broadcast

**Verdict: FAILED for MadMapper, PASSED for BEYOND. Broadcast reaches BEYOND but not MadMapper, so the show must send timecode to each program's own address (as B8.0 recommends), not broadcast.** Run on 2026-09-26 at 11:49 with Jeff's go-ahead ("You have approval for broadcasting the time code"). The broadcast went onto the Pico's Wi-Fi network, the only one connected; both Ethernet ports were unplugged (checked).
- **Sender:** `ltcplay tctest --broadcast` from main `fa5274a`, 15 s per address.
- **MadMapper:** Bank-1 chasing, its interface on the Wi-Fi address, heartbeat on 9001.
- **BEYOND:** Essentials Demo, freshly restarted.

Who held UDP 6454 during the test: MadMapper on **192.168.4.42** and **127.0.0.1** (specific addresses), BEYOND on **0.0.0.0** (every address).

| Destination | MadMapper | BEYOND (its "ArtNet TC IN" counter) |
|---|---|---|
| **192.168.7.255** (the Wi-Fi subnet's broadcast: the Pico is 192.168.4.42/22) | **not received**: no heartbeat in 15 s beyond the one packet from selecting Bank-1 | **received**: 00:10:10:03, count up about 30 a second (`B16_beyond_A.png`) |
| **255.255.255.255** | **not received**: no heartbeat | **received**: 00:20:10:04, count up about 30 a second (`B16_beyond_B.png`) |
| **127.255.255.255** (loopback broadcast, stays inside the Pico; 11:57) | **not received**: 0 heartbeats in 15 s | **received**: 00:30:10:01, count up about 30 a second (`B16_beyond_D.png`) |
| control: **192.168.4.42** (unicast) | **follows**: 611 heartbeat packets in 10 s | |

- MadMapper listens only on the specific addresses of its chosen interface, and on this PC that does not include broadcasts. I did not look for an "all interfaces" choice in MadMapper's interface list; the B1 choices were single interfaces.
- tctest printed its warnings before sending ("Test timecode is about to go to: broadcast (…). Anything that follows timecode will play its cues, lasers included" and "broadcast: This looks like a laser system. Confirm the laser operator is ready before you continue.") and then sent without a question, as designed.
- BEYOND's own timeline did not follow in these two runs: after the restart it sat "Stopped" and needed its Play-then-TC-IN routine (B14). Its counter is what shows reception, the same measure B8.0 used.
- **The Wi-Fi broadcasts left the Pico.** 192.168.7.255 and 255.255.255.255 were sent on Jeff's first-hand yes, as 15 s of test timecode each, onto the Pico's Wi-Fi network, before his condition (relayed by the main session) arrived: broadcast on the Wi-Fi only if nothing else on that network follows Art-Net timecode. I did not check the rest of that network first. The loopback test (127.255.255.255) meets that condition and gives the same answer.
- **For the show:** send timecode to each program's own address (MadMapper's interface address, BEYOND's address), never broadcast. Broadcast would also reach anything else on the network that follows Art-Net timecode.


## B17 BEYOND long run on the demo (the most one launch allows)

**Verdict: PASSED for the two hours the demo allows. BEYOND followed all 6 shows identically, with steady memory and no effect on ltcplay, MadMapper or the pixels. The demo itself stops working at exactly 2 hours, repeatably.** Jeff, 2026-09-26: the licence arrives only on site, so the long run can only be done on the demo. Run from 11:55 to 13:47. The layout was the same as B9:
- ltcplay main `fa5274a` via `drive_soak.py`: 6 shows of 7:24 every 20 minutes, Art-Net timecode to MadMapper at 192.168.4.42 and BEYOND at 127.0.0.2, 26,256 pixels, intermission bank between shows.
- **BEYOND Essentials Demo**, launched at 11:46:37 and set up per B14: timecode enabled for the show; its own Play, then TC-IN during timecode; DemoShow's audio track switched off.
- BEYOND in front, with its laser preview measured about 12 times a second, a screenshot every 2 minutes (`soak4/shots`), and memory every 10 minutes.
- Loopback plus the Pico's own address; Ethernet unplugged.

| Show | ltcplay and MadMapper | BEYOND's laser preview |
|---|---|---|
| 1 to 6 | **0 timecode frames skipped** in every show; ltcplay 22.0 MB throughout; MadMapper **1 to 34 ms behind** (median 15 ms, 18 readings); heartbeat never silent over 104 ms; pixels 40 fps, 13 skipped in 2,670 busy seconds, longest gap 58 ms | **Beams from 0.23 to 0.32 s after each show started**, the same on/off pattern every show (**91 to 100% the same as show 1**), lit in 40% of each show (the demo's own show ends at 3:23), and **dark after every show** (0 lit samples in the minute after) |

- **BEYOND's memory:** 1,298 to 1,317 MB from 12:05 to 13:35, **not growing**. (Last night's launch sat at about 600 MB: this launch has the demo's video track loaded.) MadMapper 2,304 to 2,308 MB. No new Windows problem events.
- **The demo's limit, now seen three times:**
  - **At 1 hour** (the box appeared between the 12:45:44 and 12:47:45 screenshots; launch plus 1 hour was 12:46:37), BEYOND shows **"Demo Time Limit Reached: The demo version of BEYOND has a time limit of 1 hour. Because this time limit has been reached, the demo version of BEYOND will now exit."** It does **not** exit. With the box left open, **BEYOND kept following shows 4, 5 and 6 exactly as before** (`B17_beyond_following_behind_limit_box.png`, 13:38 in show 6: beams in the preview, output on).
  - **At 2 hours, it crashes:** 13:46:42, 2 hours and 5 seconds after launch. `EAccessViolation`, "Read of address 00000000000000C0". The code address was 000000000211643C this time, against 000000000273CA6A on 09-26 at 01:53 and 10:47. The screenshot at 13:46:07 shows the crash handler's "Please wait a moment" over the limit box (`B17_beyond_2h_crash.png`).
  - After that it sits on "An error occurred in the application", still "Responding", following nothing (as in B9).
- **So on the demo, 2 hours per launch is the ceiling, and it ends in a crash, not a clean exit.** A show night longer than 2 hours cannot be tested on the demo. That test waits for Andy's licence on site.
- Cost of watching: this run's heartbeat gaps (up to 104 ms, against 75 ms in B9) came with BEYOND in front and a screen probe plus screenshots running. Nothing was dropped.

Evidence: `B17_beyond_2h_crash.png`, `B17_beyond_following_behind_limit_box.png`, `B17_beyond_problem_report_head.txt` (machine and user names removed), `B17_long_run.txt` (per-show numbers).

## Not tested yet

- **ASIO audio.** The Pico has no Focusrite ASIO driver: the Scarlett Solo runs on Windows' own USB audio driver. The ASIO drivers installed are all PreSonus and Behringer (AudioBox, Quantum, Studio, StudioLive, X-USB and others). The show's real interface (USB to the DSP) and its driver are needed for this test: **on site** (Jeff, 2026-09-26). I did not download or install a driver.
- **Rack network** (two Ethernet ports, real controllers): needs the rack.
- **Licensed BEYOND, longer than 2 hours** (B9, B17): the demo cannot go past 2 hours per launch; needs Andy's licence, on site.
