# Live flame programming

Program flame effects in xLights and see them on the real flames, while LTC Player and flamesafe keep every safety rule.

- **Who does what:** you arm and disarm in LTC Player (the Stream Deck, or hold to arm on the rack screen). xLights sends the flame commands.
- **The path:** xLights sends on this PC only. LTC Player passes the flame channels to flamesafe on its usual link. flamesafe stays the only program that talks to the flame node.
- **What flamesafe still does:** it fires only a group that is armed. It still applies lockouts, Abort and its own cutoffs.
- **The name:** this is not the Programming screen. It is also not a programming session (Rehearse and the scrub buttons). Those play a show file. This one listens to xLights.

**Before it goes near real flames:** a cold safety review, and a first test with Andy with the gas off.

## The rules (defaults, each one a setting or a decision for Jeff and Andy)

1. **xLights runs on the show PC.** LTC Player listens on 127.0.0.1 only. It refuses any other address. Nothing on the network can send flame values.
2. **Leaving the mode disarms everything.** Leaving for any reason disarms every flame group and sets every flame value to zero.
3. **One sender only.** If a second sender appears, everything goes to zero, every group disarms and the mode ends, with the reason. A second sender is another sACN program, another priority, or flamesafe's own output looping back.
4. **It turns itself off.** That happens after 10 minutes with no xLights data, and after 4 hours in all.
5. **Who can turn it on:** someone with the Settings permission (admin), on the rack screen of the show PC, never from an iPad. **Turning it off:** anyone, on any screen, even signed out.
6. **One flame group at a time.** The rack screen refuses a hold on a second group and says why. The Stream Deck's own keys cannot be stopped in advance. A group counts as soon as it is being armed (flamesafe reports it as wanted), not only once it is armed. The mode then ends at once: every value goes to zero, every group disarms, and it says why.

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

## Settings (ltcplay_remote.json, all optional)

```json
"live_flame": {
  "port": 5568,
  "idle_off_s": 600,
  "max_session_s": 14400,
  "one_group_at_a_time": true,
  "networks_xml": null
}
```

- **`listen_ip`:** may only be `127.0.0.1`. Anything else is refused.
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
| T9 | A device on the network sends sACN | The listener binds 127.0.0.1 only. A non-loopback source ends the mode. |
| T10 | Purge and program at once | The mode is refused while a purge is selected. The Morning page fires nothing while the mode is on. At the link, the mode's frame replaces the purge frame. |
| T11 | Malformed or flooding input | Strict E1.31 parsing: every length, vector and the start code are checked, and preview data is ignored. The sACN sequence rule applies. More than 200 packets a second on a universe ends the mode. |
| T12 | A second group armed from the Stream Deck | The engine cannot refuse a deck key in advance (that would need a Stream Deck change). A group counts once flamesafe reports it wanted, which is at least 3 of flamesafe's ticks (75 ms) before it can be armed. It is checked on every xLights packet and on every 50 ms pass, and the mode ends with zeros first. flamesafe's own rule refuses to arm a group while a fire value is held on it ("dirty edge"), so a second group with xLights already sending a flame does not arm at all. |
| T13 | An old listener after a quick Off then On | Every session gets a number from the flame link, and the link refuses a frame from any other session. |
| T14 | Ending it holding up an Abort | At an Abort, Hold or Start now press only the flame link's program slot is shut (no disk, no wait). The journal, the audit line and the disarm of every group are written on a thread of their own, after the lasers. |

**Residual risks:**

- **Timing.** The checks run every 50 ms, so for up to 50 ms an armed group can fire from xLights after Run is pressed somewhere other than this engine's own buttons.
  - An Abort, a Hold or anything else that zeroes the flames ends the mode at the flame link at once.
  - A second group from the Stream Deck: worst case, flamesafe's status reaches the engine (25 ms) after the group has armed, then up to one xLights packet or 50 ms. In practice it is seen while the group is still only wanted, before it can arm.
- **The Stream Deck as a user.** The engine trusts a request from this PC that says it comes from the Stream Deck (`screen: "Stream Deck"` on loopback) to act as the person signed in on the show machine. Anything running on the show PC could claim that. This is the existing rule, not a new one.
- **Sampling.** xLights frames are sampled at the flame link's 40 Hz, so one 25 ms pulse can be missed or doubled.
- **Equal priority.** A multicast or node-addressed flame controller that someone sets Active outside this mode would still merge at equal priority on the node. That risk exists today, and this mode refuses to start while it is set.
