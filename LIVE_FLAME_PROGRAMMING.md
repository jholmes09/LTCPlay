# Live flame programming

Program flame effects in xLights and see them on the real flames, while LTC Player and flamesafe keep every safety rule.

- **Who does what:** you arm and disarm in LTC Player (the Stream Deck, or hold to arm on the rack screen). xLights sends the flame commands.
- **The path:** xLights sends on this PC only. LTC Player passes the flame channels to flamesafe on its usual link. flamesafe stays the only program that talks to the flame node.
- **What flamesafe still does:** it fires only a group that is armed. It still applies lockouts, Abort and its own cutoffs.
- **A group with several safety channels arms as one.** The wave flamer is three fixtures, each with its own safety channel, in one flame group: one key, one hold to arm, one disarm. Live programming treats it as one group, and xLights can never set a safety channel. A fixture locked out for the night stays off; the group's other fixtures still arm.
- **The name:** this is not the Programming screen. It is also not a programming session (Rehearse and the scrub buttons). Those play a show file. This one listens to xLights. To fire the show file's own flame cues while you scrub, see "Fire flames while scrubbing" at the end.

**Before it goes near real flames:** a cold safety review, and a first test with Andy with the gas off.

## The rules (defaults, each one a setting or a decision for Jeff and Andy)

1. **xLights runs on the show PC.** LTC Player listens on 127.0.0.1 only. It refuses any other address. Nothing on the network can send flame values. The one exception is a Mac you name yourself: see "Programming from a Mac" below. It is off unless you set it.
2. **Leaving the mode disarms everything.** Leaving for any reason disarms every flame group and sets every flame value to zero.
3. **One sender only.** If a second sender appears, everything goes to zero, every group disarms and the mode ends, with the reason. A second sender is another sACN program, another priority, or flamesafe's own output looping back.
4. **It turns itself off.** That happens after 10 minutes with no xLights data, and after 4 hours in all.
5. **Who can turn it on:** someone with the Settings permission (admin), on the rack screen of the show PC, never from an iPad. **Turning it off:** anyone, on any screen, even signed out.
6. **One flame group at a time.** The rack screen refuses a hold on a second group and says why. The Stream Deck's own keys cannot be stopped in advance. A group counts as soon as it is being armed (flamesafe reports it as wanted), not only once it is armed. The mode then ends: every value goes to zero, every group disarms, and it says why. A second group armed from the Stream Deck can still fire xLights' values for about one flamesafe tick (25 ms) in the worst case; see T12 and the residual risks.

## What turns it off by itself

Each of these ends the mode. Each one disarms every flame group, sets the values to zero, and writes the reason in the journal and the audit log.

- Off, from any screen or from the red banner.
- Abort (from any screen, the Stream Deck or the scheduler), Hold, Start now, Run, or a show starting.
- Anything that zeroes or disarms the flames through the show (a Hold, an Abort, a show ending). This stops xLights at the flame link at once, the same way it stops fire.
- The listener stopping, or not checking in for 1 second (a watchdog looks every 0.2 s).
- More than 1000 packets a second reaching the listener, of any kind.
- A second sender, or flamesafe's own output coming back.
- A flood: more than 200 packets a second on one universe.
- Two groups armed.
- flamesafe not reporting.
- xlights_networks.xml changing while the mode is on.
- No xLights data for 10 minutes, or 4 hours in all.

LTC Player always starts with the mode off.

## When it will not turn on

The rack screen says the reason. It refuses to turn on while any of these is true:

- A show is live or held.
- Run is pressed.
- An Abort is standing (press Reset).
- flamesafe is not reporting.
- The flame link is not sending.
- A purge is selected or firing.
- More than one group is armed.
- The flame controller in xlights_networks.xml is not set as described in the steps below.
- Any other controller that is not Inactive ("Active" or "xLights Only") sends E1.31 to this PC.
- Any other controller, whatever its setting, uses one of the flame controller's universe numbers.
- flamesafe's own destination is this listener's address and port.
- The port is in use.

## Setting up xLights (once)

Items marked **[verify in xLights]** are names I could not check without xLights in front of me. If your xLights calls it something else, stop and ask before guessing.

1. **Find the flame controller.** In xLights, open the Controllers tab (older versions call it Setup) **[verify in xLights]**. Find the controller whose name is `flame_controller` in `ltcplay_fire_ice.json`.
2. **Protocol: E1.31** (also called sACN). Not Art-Net: Art-Net on this PC belongs to MadMapper and BEYOND.
3. **IP address: `127.0.0.1`.**
   - Never the flame node's address.
   - Never Multicast.
   - Unicast only.
4. **Universes and channel counts: leave them exactly as they are.** LTC Player reads the same `xlights_networks.xml`, so the channels line up with the renders.
5. **Active setting: "xLights Only"** **[verify in xLights]**. Never "Active".
   - With "xLights Only", xLights sends and LTC Player's own show output does not.
   - LTC Player refuses a show whose flame controller is Active.
6. **Duplicate frame suppression: off** for this controller, if xLights offers it **[verify in xLights]**. If it is on, a held flame value stops being re-sent and LTC Player zeroes it after 0.25 s. The flame stutters. That is safe but confusing.
7. **Priority:** leave it at the default **[verify in xLights]**. flamesafe always sends at 200 whatever xLights uses.
8. **Leave every other controller alone, with two rules:**
   - No other controller that is Active or "xLights Only" may send E1.31 to 127.0.0.1.
   - No other controller may use the flame controller's universe numbers, even at another address.

   xLights sends all its controllers with one sender identity, so LTC Player could not tell them apart. It refuses the mode if either rule is broken.
9. **Save.** Then render the show again once. A render older than `xlights_networks.xml` gets no flame cues in a show (CUES FAULT). This is a one-time change, not something to do every session.

## Using it

1. Check that no show is running and Run is not pressed.
2. Sign in as an admin on the rack screen.
3. Go to Programming, then Live flame programming, then Turn on, and confirm. The red LIVE FLAME PROGRAMMING bar appears on every screen.
4. Arm one group from the Stream Deck (hold to arm), as for a show.
5. In xLights, turn on output to lights **[verify in xLights: the light bulb button]**, then play or scrub your sequence. Only the armed group fires. Disarmed or locked out machines do not.
6. **Never use the xLights Test tools while a group is armed** **[verify in xLights: Tools, Test]**. An "all on" test drives the flame channels like any other channel.
7. To finish, use any one of these:
   - Turn off (the card or the red bar).
   - Abort.
   - Hold.
   - Run.
   - Start now.

Every group disarms.

## The first test (hardware off)

Do this first, with Andy:

1. **Gas off and key switch off.** Point flamesafe's destination at a spare address, not the flame node.
   - Never point it at 127.0.0.1 port 5568. That is this listener, and the mode refuses it.
2. **Turn the mode on.** Arm one group, and play a sequence with one flame effect.
3. **Watch the per-group values** on the rack screen's Info and in the journal.
4. **Check each of these:**
   - Disarm the group: its value goes to 0.
   - Lock the machine: its value goes to 0.
   - Abort: everything goes to 0 and the mode ends.
   - Quit xLights: zero within 0.25 s.
   - Run: the mode ends.
   - A second sACN sender: the mode ends.
   - Arm a second group: the mode ends.
5. **Then the node, with Andy.** Connect the node with the gas still off and repeat. Only then turn the gas on, with one machine.

## Programming from a Mac

Off by default. With it off, everything above is exactly as before. Turn it on only when you program in xLights on a Mac on the show network.

**What changes:** LTC Player also listens on the show PC's own show network address, and takes E1.31 from exactly one address: the Mac's. It still relays the flame channels to flamesafe through its own flame link, exactly as above. flamesafe is not changed and is still the only program that talks to the flame node. xLights on the Mac never sends to the flame node.

**Set it up (once):**

1. **Copy the show PC's `xlights_networks.xml` to the Mac.** Always this way round, never from the Mac to the PC. LTC Player reads the PC's file for the channel map, so the universes and channel counts must be identical.
2. **On the PC's file, the flame controller sends E1.31 unicast to the PC's own show network address** (for example `10.0.0.10`), not `127.0.0.1`, not the flame node, never Multicast. Its Active setting stays "xLights Only". Make this change on the PC, then copy the file to the Mac again (step 1).
3. **No other controller** that is not Inactive may send E1.31 to the PC's show network address or to `127.0.0.1`, and none may use a flame universe number. Same rules as before, now for the new address too.
4. **Give the Mac a fixed address** on the show network (a reservation or a manual address). It must be a private address (10.x, 172.16.x to 172.31.x or 192.168.x). It may not be the PC's address, the flame node's, or any other device in `ltcplay_fire_ice.json`, flamesafe's config or `xlights_networks.xml`. Every address in those files must be written plainly (like `10.0.0.60`; `localhost` is fine, and an OSC path such as MadMapper's heartbeat `address` `/float-1` is not an address): a name or an address with leading zeros cannot be compared, so the mode refuses to start.
5. **Set it in `ltcplay_remote.json`** on the show PC:

   ```json
   "live_flame": {
     "allowed_source_ip": "10.0.0.20",
     "listen_ip": "10.0.0.10"
   }
   ```

   - `allowed_source_ip` is the Mac's address. `null` or left out means this PC only.
   - `listen_ip` is the show PC's own show network address, and it is required with `allowed_source_ip`. The mode refuses to start if it is not an address of this PC.
   - LTC Player then listens on that address and on `127.0.0.1`, never on every address, and never joins a multicast group.
6. **Compare the map fingerprint.** When the mode is on, the red LIVE FLAME PROGRAMMING bar shows `map fingerprint` with 8 letters and digits, and `source:` with the sender that holds the lock (`Mac 10.0.0.20`, `this PC`, or none yet). The fingerprint is a hash of the whole `xlights_networks.xml` on the PC: every controller's name, state, addresses, protocols, universes and channel counts. On the Mac, the helper `tools/xlights_map_fingerprint.py` prints the fingerprint of the Mac's copy, and says in plain words how the flame controller is set (protocol, where it sends, its state). A Cowork session linked to the Mac runs it: `python3 tools/xlights_map_fingerprint.py <the Mac's xlights_networks.xml> "<flame controller name>"`. They must match. If they do not, turn the mode off and copy the PC's file to the Mac again.

   **What the engine cannot see:** LTC Player cannot see what the Mac's xLights actually sends, or where. It sees only packets that reach the show PC. If the Mac's xLights were set to send the flame controller somewhere else (the flame node, Multicast), the PC would never know. Only two things cover that: the Mac's `xlights_networks.xml` is a copy of the PC's file, and its fingerprint matches the one on the rack screen.

**What ends it, in addition to the list above:** a packet from any address that is not `127.0.0.1` or the Mac's (another device, the PC's own show network address). A packet that arrives on the wrong socket: this PC's packets count only on the 127.0.0.1 socket, the Mac's only on the `listen_ip` socket. A second sender, from the Mac or from this PC, also ends it: one sender identity, from one place, per session.

Everything else is the same: an admin turns it on on the show PC's own rack screen, never from an iPad; anyone can turn it off; one group at a time; 10 minutes idle and 4 hours in all; Abort, Hold, Run, Start now and a show starting end it.

**First test from the Mac:** the same gas off first test as below, before anything else. Add one check: send from another computer on the show network (or change the Mac's address) and see the mode end.

## Settings (ltcplay_remote.json, all optional)

```json
"live_flame": {
  "port": 5568,
  "idle_off_s": 600,
  "max_session_s": 14400,
  "one_group_at_a_time": true,
  "networks_xml": null,
  "allowed_source_ip": null
}
```

- **`listen_ip`:** may only be `127.0.0.1`, unless `allowed_source_ip` is set: then it is required and is the show PC's own show network address. Anything else is refused.
- **`allowed_source_ip`:** the one Mac that may send (see "Programming from a Mac"). `null` means this PC only.
- **`networks_xml`:** names the `xlights_networks.xml` xLights uses. Only needed when the show folder has more than one and they disagree.

## Threat notes (for the safety review)

flamesafe is not changed:

- Its wire, its key and its sender lock are all the same.
- It still sees one sender.
- The new values travel as ordinary flame frames on the engine's existing flame link, the same way as the Morning page's purge frame (flamelink.py `program_set`, beside `purge_set`).

| # | How it could fire when it should not | What stops it |
|---|---|---|
| T1 | The mode is left on at show time | Always off at start. Run, Start now, Hold, Abort and a show going live all end it. The flame link reads the program frame only while no show is live. While the mode is on, the show's own cues are held at zero. |
| T2 | Wrong channel map | The map comes only from `xlights_networks.xml`, read by the code the renders use. The mode refuses a missing, doubled or Active controller, or one that is not E1.31 unicast 127.0.0.1. It ends when the file changes. |
| T3 | xLights reaches the node directly (multicast, or the node's IP) | Refused at entry: the flame controller must be E1.31 to 127.0.0.1 only, and not Active. The physical key switch is the backstop. |
| T4 | The xLights Test tools, "all on", or a stray effect | Only an armed group fires, one group at a time. flamesafe refuses to arm while a fire value is held ("dirty edge"). Operator rule: no Test while armed. |
| T5 | flamesafe's own output loops back into the listener | Refused at entry when its destination is this listener's address and port. Its CID or source name on the wire ends the mode. |
| T6 | Startup glitch: xLights opens with a value up | Nothing listens until the mode is on. Each channel stays zero until xLights has sent it at zero. A stream that stops and restarts must show zero again. |
| T7 | Two senders | The first sender (its CID and priority) is locked. Any other ends the mode with every group disarmed. One xLights sends every controller with the same identity, so at entry no other controller may send E1.31 to this PC or use a flame universe (review of PR #96, P2-1). |
| T8 | xLights or the engine hangs with a value up | A universe goes to zero 0.25 s after its last packet. The flame link drops the frame 0.25 s after the last update, on its own clock. If the engine dies, flamesafe zeroes fire in 100 ms and disarms in 500 ms. |
| T9 | A device on the network sends sACN | By default the listener binds 127.0.0.1 only and a non-loopback source ends the mode. With `allowed_source_ip` it also binds the PC's own `listen_ip` (never 0.0.0.0, no multicast join), and a packet from any address but loopback and the Mac's ends the mode, with every group disarmed and the values at zero. |
| T15 | A wrong Mac address, or one that is really another device | The address must be one private IPv4 address written plainly. Loopback, multicast, broadcast, link local, public addresses and names are refused. At entry it may not be this PC's own, flamesafe's destination (the flame node), any controller in `xlights_networks.xml` or any address in `ltcplay_fire_ice.json` or flamesafe's config. `listen_ip` must be an address of this PC. |
| T16 | A device on the show network spoofs the Mac's address | Not prevented; this is a residual risk. Its limits: unicast only to the PC, the origin tied to the socket, the one sender lock (CID, priority and origin: a second CID, or the same one from loopback, ends the mode), one group at a time, a group still has to be armed by hold to arm on the Stream Deck, flamesafe's own rules, and the physical key switch. |
| T17 | The Mac's xLights is set differently from the PC's file (another layout, or the flame controller sending to the node or Multicast) | The engine cannot see the Mac's actual output. The map comes only from the PC's `xlights_networks.xml`. The rack screen shows a fingerprint of that whole file; `tools/xlights_map_fingerprint.py` prints the Mac's and says how its flame controller is set. Operator rule: the file is only ever copied PC to Mac, and the two fingerprints match before arming. |
| T10 | Purge and program at once | The mode is refused while a purge is selected. The Morning page fires nothing while the mode is on. At the link, the mode's frame replaces the purge frame. |
| T11 | Malformed or flooding input | Strict E1.31 parsing: every length, vector and the start code are checked, and preview data is ignored. The sACN sequence rule applies. More than 200 packets a second on a universe ends the mode. |
| T12 | A second group armed from the Stream Deck | The engine cannot refuse a deck key in advance (that would need a Stream Deck change). On a clean arm edge flamesafe raises that group's arm channel in the same tick the group becomes wanted, and holds only its fire channels at zero for 3 ticks (75 ms). The engine sees the group in flamesafe's status (up to 25 ms later), then on the next xLights packet or the next 50 ms pass, shuts the flame link's slot at once and disarms every group; that zero frame reaches flamesafe on the link's next frame (up to 25 ms). Worst case, that adds up to a little more than the 75 ms quiet window, so xLights' values could fire on the second group for about one tick (25 ms). flamesafe's own "dirty edge" rule refuses to arm a group while a fire value of 15 or more is held on it, so a second group with xLights already holding a flame there does not arm at all. **A residual risk:** about one tick (25 ms) of xLights' values on a second group armed from the Stream Deck, only when that group arms clean, only while the mode is on, and still behind flamesafe and the key switch. |
| T13 | An old listener after a quick Off then On | Every session gets a number from the flame link, and the link refuses a frame from any other session. |
| T14 | Ending it holding up an Abort | At an Abort, Hold or Start now press only the flame link's program slot is shut (no disk, no wait). The journal, the audit line and the disarm of every group are written on a thread of their own, after the lasers. |

**Residual risks:**

- **Timing.** The checks run every 50 ms, so for up to 50 ms an armed group can fire from xLights after Run is pressed somewhere other than this engine's own buttons.
  - An Abort, a Hold or anything else that zeroes the flames ends the mode at the flame link at once.
  - **A second group armed from the Stream Deck's own keys** can fire xLights' values for about one flamesafe tick (25 ms) in the worst case, before the mode ends and every group disarms (T12). Removing it needs the Stream Deck to refuse the key itself, which this change does not touch.
- **The Stream Deck as a user.** The engine trusts a request from this PC that says it comes from the Stream Deck (`screen: "Stream Deck"` on loopback) to act as the person signed in on the show machine. Anything running on the show PC could claim that. This is the existing rule, not a new one.
- **Sampling.** xLights frames are sampled at the flame link's 40 Hz, so one 25 ms pulse can be missed or doubled.
- **A spoofed Mac.** With `allowed_source_ip` set, a device on the show network that sends from the Mac's address is taken as the Mac until a second sender identity appears (T16). It is limited to an armed group, one group at a time, behind flamesafe and the key switch. The show network must be closed to outside devices. Leaving `allowed_source_ip` off removes this risk.
- **The Mac's own output.** The engine cannot see what the Mac's xLights sends, or where (T17). Only the Mac's file being a copy of the PC's, with matching fingerprints, covers it.
- **Equal priority.** A multicast or node-addressed flame controller that someone sets Active outside this mode would still merge at equal priority on the node. That risk exists today, and this mode refuses to start while it is set.

## Fire flames while scrubbing

A different switch from live flame programming. It plays the flame cues that are already in the show's render, at the place you are scrubbing to on the Programming screen. xLights is not involved.

- **Where:** Programming screen, "Fire flames while scrubbing". The same rule as live flame programming: someone with the Settings permission (admin) turns it on, confirmed, on the show PC itself, never from an iPad. Anyone can turn it off, on any screen, signed in or not.
- **Off every start.** LTC Player always starts with it off.
- **Only one of the two.** It will not turn on while live flame programming is on, and live flame programming will not turn on while it is on. Each says why.
- **What you see:** a red FLAMES LIVE IN REHEARSAL bar on every screen (the rack screen and Programming) while it is on, with where the scrub is and a Turn off button.
- **The path:** the same as a show. LTC Player reads the flame channels from the show's render at the scrub position and sends them to flamesafe on its usual flame link. flamesafe is not changed. It fires only a group that is armed, and its lockouts, Abort, hold to arm and cutoffs all apply. A disarmed or locked out group never fires.

### When a flame value goes out

Only when all of these are true:

1. A show file is running in Programming (Run with Programming chosen), and no scheduled show is live.
2. The Programming screen's transport is playing: after Play from, a Jump, Continue, or the loop. Not paused, not following timecode, and no Blackout or Preshow look on the pixels. A free run that LTC Player started by itself because timecode was lost does not count: nothing fires from it.
3. The show's music clock is not playing a cue.
4. The render passes the same checks as in a show: not older than `xlights_networks.xml`, the right number of channels, and the flame controller where it was when the render was checked. Between cues and past the end of a render the flames are zero.

### The seek rule (how a scrub cannot fire a burst)

- Every press that moves the position is a landing, however small: Play from, every Jump, Pause, Continue, the loop, and the loop jumping back to A. LTC Player counts every move, and that count is the real guard. A position that is more than 0.15 s off the time that has passed since the last landing is a landing too; that is only a backstop.
- A new render of the cue under the playhead (xLights saved it again while you scrub) is a landing too: zero at once, then the same half second.
- After a landing, and after turning it on, every flame value is zero until the position has played on steadily for half a second.
- This is decided channel by channel. A flame channel that is already up where you landed stays zero until it has gone to zero once. So a single long flame you jumped into the middle of never fires, and dragging or jumping through the show never fires anything on the way. A pulsing effect that is part way through where you land can fire again at its next pulse, after the half second.
- A position that stops for a tenth of a second counts as stopped.

### Who can move the position while it is on

While it is on, Play from, Jump, Continue, the loop (on or off) and setting mark A or B work only on the show PC itself, or for someone signed in with the Settings permission (admin). An iPad that is signed out, or signed in without Settings, is refused with: "Flames are live in rehearsal; move the position from the show PC or sign in as an admin." Pause, Follow timecode again and turning it off still work from anywhere.

### What turns it off by itself

Each of these turns it off, sets every flame value to zero at once, and writes the reason in the journal and the audit log:

- Turn off (the switch or the red bar).
- Abort, Hold, Start now, Run, or Stop.
- A show going live: a scheduled show starting, or the show's music clock playing a cue.
- Anything that zeroes the show's flame cues (a Hold, an Abort, the end of a show).
- Live flame programming starting.
- The session no longer being Programming. (A standing Abort or Hold also stops it, but in Programming the scheduler rarely has one; flamesafe's own Abort latch and hold to arm are what keep a group from firing after an Abort.)
- A purge selected or firing on the Morning page, or the flame link to flamesafe not sending.

**Turning it off does not disarm.** The groups you armed for the session stay armed, as after a show. Disarm them on the Stream Deck when you are done.

### Also

- While it is on, the Morning page fires nothing and a purge selection stops it from turning on.
- A live show's own flame cues are not affected: this has its own path, and it is never on during a show.
- The Stream Deck is unchanged. It shows armed groups as always; the red bar on the screens is where this switch shows.
- **First test:** gas off and key switch off, as for live flame programming. Arm one group, play into a flame cue, and check: the cue fires only after half a second of steady play; Pause zeroes it; a Jump into the middle of a cue does not fire it; a disarmed group stays at zero; Abort zeroes everything and turns it off.
