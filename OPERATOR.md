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

## Signing in: signed out is view only

Once the Users screen has any user (USERS.md), every screen needs someone
signed in to do anything that is not a stop or a disarm. That includes the
rack screen on the show machine itself: being at the machine is not a
sign-in.

- **Signed out, every screen is view only.** It still shows the show
  state, the schedule, the flame and laser lamps and the alerts. The chip
  at the top right reads "Sign in to use controls", and Start now, Resume,
  Reset, Run, every Hold to arm, a laser's Arm, the announcements and the
  settings switches are greyed out. Hold, Abort, Disarm all and every
  Disarm stay live.
- **Always, signed in or not:** Stop, Hold, Abort, Acknowledge an alert,
  Disarm all flame groups, disarming one group, and disarming lasers.
- **Needs a signed-in operator or admin:** Start now, Resume, Reset, Run,
  arming a flame group (hold to arm and Arm all), a purge, arming a laser,
  a laser test, unlocking, playing an announcement, the checklists,
  choosing the operator and editing tonight's list. Settings, show files
  and users need an admin as before.
- **Sign out** on the Users screen ends the sign-in at the engine. On the
  show machine it signs that machine out completely (the rack screen and
  the Stream Deck together), lets go of that screen's hold to arm if one is
  under way, and the screen is view only straight away. It does not disarm
  anything or stop live flame programming: use Disarm all or Stop for that.
- **If the users file is damaged or missing** after users were set up, an
  alert says so on every screen and every control is locked except Stop,
  Hold, Abort, Acknowledge and the disarms. Nobody can sign in. A good
  `ltcplay_users.json` put back in the settings folder is used at once, with
  no restart. To start over with no users: close LTC Player, remove
  `ltcplay_users.json` and `ltcplay_users.made` from the settings folder
  (Start menu, LTC Player, "Settings and logs"), and start it again.
- **The Stream Deck started before the engine** keeps its arm keys off
  until it has heard the engine once.
- **A name with no password yet** chooses it at the rack screen on the show
  machine, never from an iPad.
- **The Stream Deck** acts as whoever is signed in on the rack screen. With
  nobody signed in there, its Start key reads SIGN IN and its arm keys do
  not arm. Its Stop, Hold, Acknowledge and disarm keys always work. If the
  deck cannot hear from the engine for 2 seconds, its arm keys wait until
  it can.
- **The journal and the audit log name the signed-in person**, never a
  name picked from a list. A stop or a disarm pressed with nobody signed
  in is written as "unsigned".

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
is), and plays again every 30 seconds (the booth's response_s) until
someone acknowledges it. It stays until you press Acknowledge on the
banner or any key on the Stream Deck. Acknowledge only clears the banner:
nothing on the rig changes.

**An alert still open when a show is due** (Jeff, 2026-10-26). Between
shows an alert does nothing automatic. But if a show's time comes while
any alert is open, the schedule goes on Hold at once instead of starting
the show: the booth note sounds and the state card on the rack screen
says "Held: alert open". For an audio, video, flames, flame link,
timecode or start alert, a 60 second countdown shows there; if nobody
acknowledges the alert by then, the Delayed announcement plays once on
the PA. A Stream Deck or gate alert holds quietly, with no announcement.
Acknowledging cancels the countdown and the announcement (with two such
alerts open, both have to be acknowledged), even while the recording is
still loading; once it has started it plays to its end. Acknowledging
does NOT start the show: it waits until you press Resume (then Start now
for the delayed show, as after any Hold). Pressing Resume on a held
schedule while alerts are open is your choice: from this Hold or one of
your own, before the show's time or after it, the show it lets start is
not held again for those alerts (or for some of them, if others were
acknowledged meanwhile); a NEW alert still holds it. It speaks only for
that one show: the show after it is held again if the alert is still
open, and resuming a paused, running show says nothing about the next. The automatic announcement
changes nothing else on the rig: no laser is disarmed and nothing about
the flames changes. If the recording or the announcement output is
missing, it stays held and the state card says so. A show that
is already running is never held, stopped, disarmed or announced over by
this. Stop on the Stream Deck works the same while held.
While an alert is open the Stream Deck's Start key reads ACK / ALERT in
blue; it goes back to START NOW once the alert is cleared. An audio alert,
or an alert the booth speaker could not play, also pulses the floor pixels
blue (alert_pixels in ltcplay_remote.json) until it is acknowledged.

**The Stream Deck's keys** (Jeff, 2026-10-26). Six keys, three on top and
three below:

| START / STOP | Floor G | Cat-Walk G |
|---|---|---|
| **Blue Flames** | **Fireball** | ANNOUNCE |

Key 5 reads ANNOUNCE while LTC Player runs announcements (it is blank and
does nothing otherwise). One press opens page 2, see Announcements below.

The deck has **no Hold key and no Abort key** any more. Hold, Resume,
Abort and Reset are on the Rack screen (and the hardware e-stop is where it
always was). The four flame keys arm and disarm Floor G (front row),
Cat-Walk G (cat-walk), Blue Flames (wave flamer) and Fireball (fireball):
hold a key to arm its group, tap it to disarm. With no ltcplay_deck.json
those four go on keys 1 to 4 by their names (any case; cat walk and
catwalk count as cat-walk), wherever flamesafe's config lists them. Every
other group follows in flamesafe's order on the keys left over: if one of
the four names is missing, the next group takes its key, and a group past
key 4 has no key. So check the labels when flamesafe's groups change. The
deck writes its key map to the journal every time it starts.

**Announcements** (Jeff, 2026-10-26). Three recorded messages: Delayed,
Cancellation and Cannot continue. On the rack screen they are three buttons
on the show screen below the flame groups and Disarm all (so they never
push those off a small screen), with Stop announcement beside them; on the Stream Deck they are page 2 (press ANNOUNCE):

| START / STOP | DELAYED | CANCEL |
|---|---|---|
| **CANNOT CONTINUE** | **STOP ANNC.** | **BACK** |

- One press plays. There is no double tap and no hold. Only one plays at
  a time; a second press while one plays is refused.
- **Between shows** a press puts the schedule on Hold first, so the next
  show does not start while you are away. That Hold does what your own
  Hold does: live flame programming and fire while scrubbing end, and
  every laser is disarmed (arm them again by hand). The rack screen says HELD FOR
  ANNOUNCEMENT, with a Stop button, while it plays. When it ends the
  schedule STAYS on Hold: press Resume when you are ready to carry on.
- **During a show** the announcement plays over the show and pauses,
  holds and stops nothing, even when it is pressed the very second a show
  starts. If the show has to end, press Stop (or Abort) yourself. The
  banner then reads ANNOUNCEMENT PLAYING.
- On the show screen the Announcements line itself says HELD FOR
  ANNOUNCEMENT (or ANNOUNCEMENT PLAYING) and Stop announcement lights;
  every other screen shows it as a banner with Stop.
- If a press held the schedule and then the recording could not play, it
  says **HELD, ANNOUNCEMENT FAILED** and why. The schedule stays held
  until you press Resume.
- **Stop announcement** (and STOP ANNC. on the deck) ends only the sound.
  A Hold it made stays until Resume. An announcement never disarms, stops
  or latches anything by itself.
- A recording that is not on this PC yet reads **Recording missing** on
  its button (NO FILE on the deck) and the button does nothing. Copy the
  three WAV files (16, 24 or 32 bit, or 32-bit float, at most 5 minutes)
  into the Announcements folder beside ltcplay_announce.json, named
  delayed.wav, cancellation.wav and cannot_continue.wav, then restart LTC
  Player.
- The sound goes to ONE output, named exactly in ltcplay_announce.json
  (`"device"`), never the system default (that is where the show audio
  is). The first run of LTC Player writes the file with no device, so the
  rack screen says "Output device: not set" and every button stays off
  until you type the output's exact name (as Windows lists it) and restart
  LTC Player. A named output that is unplugged reads "not attached".
- The deck's page 2 hides the four flame keys while it is up, because the
  deck has only six keys. It goes back to the flame keys by itself: BACK,
  ten seconds with no key pressed, an Abort, an alert, the Startup page
  opening, or a deck alarm. START / STOP is the same key on both pages.
  It goes back for the ten seconds only when LTC Player has just said
  nothing is playing: while one plays, or while the deck has not heard
  from LTC Player, STOP ANNC. stays there (after a whole minute with no
  word from LTC Player the flame keys come back anyway). When it goes back by itself (ten
  seconds, an alert, the Startup page) the flame keys start no ARM for a
  second and a half and until the flame faces are really on the keys, so
  a press meant for an announcement never arms a group. A tap that
  DISARMS an armed group always works at once (a disarm is never held
  back). A press the engine refused shows on its key for a few seconds
  in a word (NO FILE, ONE PLAYING, SHOW ENDED, PAGE STALE, NO OUTPUT).
- If the engine's announcement status has not been read for a few
  seconds, the rack screen says "Announcement status unavailable" and the
  deck says NO STATUS, rather than show an old PLAYING; Stop stays
  pressable.
- Announcements are pressed on the show PC itself (the rack screen and the
  Stream Deck), not from an iPad.

The **booth speaker** is separate from announcements: it plays the short
alert notes (the Stream Deck unplugged, a show that did not start) to a
private speaker in the booth, never the PA. It is the "booth_alert" block
in ltcplay_remote.json: `"device"` (the exact output name) and
`"channels"` (the output pair, like [15, 16]). That is all it needs: the
notes Jeff recorded ship with the installer (2026-10-28). The first run
writes the block with no device, so it is off; the Announcements line on
the rack screen says "Booth speaker: not set" until it is set, and then
how many notes it found, like "Booth speaker: StudioLive 16R, 23 of 23
notes found", or which are missing. A missing note stays silent, the
banner says so and the floor pulse runs instead; nothing else is played
in its place.

Where a note comes from, first one found wins:

1. `"files"` in the booth_alert block, when it names a WAV for that note,
   like `"files": {"deck": "C:\\Notes\\my deck.wav"}`. Optional. If that
   file is missing the note is silent; it is never replaced by another.
2. Your own copy: `booth_<name>.wav` in a folder named `Booth notes` in
   Start menu, LTC Player, "Settings and logs". No install, update,
   rollback or uninstall ever touches that folder. Delete your copy to go
   back to the shipped note.
3. The shipped note: `Booth notes` in the LTC Player program folder.
   Every install and update empties it and puts the shipped set back, so
   do not edit the notes there.

A note is at most 8 seconds long. Display only alerts (the aim slots
differ) never sound. Every other alert repeats its note every 30 seconds
(response_s) until acknowledged. The recorded notes:

| File | Plays when |
|---|---|
| booth_gate.wav, booth_audio.wav, booth_video.wav, booth_deck.wav, booth_start.wav, booth_flames.wav, booth_link.wav, booth_timecode.wav, booth_lasers.wav, booth_users.wav | an alert of that kind is open (lasers and users sound from 2026-10-28) |
| booth_test.wav | a test of the booth speaker; no screen has a button for it yet |
| booth_held.wav | once, when the schedule holds at showtime for an open alert, before that alert's own note |
| booth_preroll_audio.wav, booth_preroll_video.wav, booth_preroll_timecode.wav, booth_preroll_lasers.wav, booth_preroll_flames.wav, booth_preroll_network.wav, booth_preroll_hold.wav, booth_preroll_ok.wav, booth_continue_anyway.wav | the pre-roll check, once it is built (PR #162); silent until then |
| booth_clean_ready.wav, booth_clean_done.wav | the wave flamer cleaning routine, once it is built (PR #159); silent until then |

**START / STOP.** While nothing is running the key reads START NOW. Once
Run has been pressed and playback is going it reads STOP RUN: hold it for
half a second to Stop. Stop first turns every flame group off, at once,
then does exactly what the Rack screen's Stop does: live flame programming
ends, every flame group is sent off again through the show's flame link
(the flame half of an Abort, so each group needs a fresh arm), playback
stops, the outputs go dark, and no show starts until Run is pressed again.
The deck's Stop is a complete halt: every laser is disarmed too.
Unlike Abort it does not latch the rig: nothing needs a Reset. A tap does
nothing; let go before the ring fills and nothing is sent.

**Stop during a scheduled show** (Jeff, 2026-10-26). The key keeps saying
NOW PLAYING, PAUSED or AUDIO LOST, but holding it half a second still
stops everything: every flame group and laser is disarmed, the show ends
(faded to dark as an Abort is, but with no latch and no Reset needed),
playback stops, and no show starts until Run is pressed again. Stopping
the last show of the night closes the night a moment later, as an Abort
does. Because Run is no longer pressed, each later show that night raises
a "The show did not start" alert when its time comes, until the last
show's time has passed or Run is pressed again: acknowledge each one. To
disarm just one group during a show, tap its key on the deck, or use the
Rack screen.

**If the deck cannot read the show computer** (it is down, or still
starting), the key reads STOP ALL, and the same half second hold still
stops: the deck turns every flame group off itself first, then asks the
show computer for the rest, and says ENGINE FAULT if it cannot reach it
(or CHECK RACK if it took the Stop but has not answered yet). If the
flames and lasers went off but playback itself did not stop, the key reads
PLAYBACK FAILED: press Stop on the Rack screen. If the show itself did not
end, the key flashes SHOW NOT STOPPED: press Abort on the Rack screen. The key also reads STOP
ALL when nothing is running but a flame group is armed, or live flame
programming or fire while scrubbing is on, and the hold turns everything
off. An alert waiting on the deck never eats a
Stop: a quick tap only acknowledges it, and a full half second hold stops
(the key switches to HOLD TO STOP as soon as it is held, so let go to
cancel). With the Morning page open, key 0 is FIRE for the purge, except
whenever anything is running (a show live, Run pressed and playback
going, or live flame programming or fire while scrubbing on), the deck
cannot read the show computer, or a group is armed for
anything but the purge (a group the purge selection wants counts as the
purge's, even if it was armed by hand first): then it is Stop. If FIRE is
being held when a show or run starts, FIRE ends and the same held key
becomes a Stop hold at once: keep holding to stop, let go to cancel. The Rack screen's Stop is the same complete halt, during a show
too (see Stop, Hold and Reset on the rack screen, below). After an Abort the deck's border goes red and
the keys grey out, but STOP RUN still works while a run is going, as on
the Rack screen. If Stop stopped playback but could not send the flame
groups off, the Rack screen says "Stopped, but the flame disarm FAILED"
and the deck's key flashes DISARM FAILED: press Disarm all flame groups,
or use the hardware e-stop.

**A flame group with no key.** A group past key 4 has no key: arm it by
holding its Hold to arm button on the Rack screen, disarm it there, with
Disarm all flame groups, a Stop on the deck or any Abort. Its name on the
Rack screen's flame card says "(no Stream Deck key)". A purge click on
the Morning page never arms it, and a Fireball is never purged at all.

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

**WIND on the show screen (Fire & Ice).** Under the lasers. Advisory only:
it never arms, holds or stops anything; the on-site wind check decides.
The measured line shows the highest usable reading from UTALP (the I-15
Point of the Mountain station) and the backup airports (Provo, Salt Lake
City). A backup reading is amber and says "backup station", the airport,
how far away it is and why UTALP was not used ("UTALP (Iowa mesonet)
stopped reporting at 3:40 AM, last reading 3 h 12 min ago") or what UTALP
reads. If the highest reading is STALE it is still shown, amber and
marked STALE, with the current ones beside it. NO CURRENT DATA, use
on-site wind check, with no number, means no
source has a usable reading (UTALP over 45 min old, an airport over 2 h);
the line says why for each one. The forecast line goes STALE when this PC
has not got it from the Weather Service for an hour, or the Weather
Service has not updated it for 18 h, and grey at 6 h and 36 h; it says
which. WEATHER.md
has the rules and the settings.

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
in `lasers` in ltcplay_morning.json); Calibrate all lasers (the test on
for every armed laser that is not locked out, one after another; it names
any it skipped and why) and Calibration off (every test off, always
works); Open BEYOND (on the show PC only: brings the BEYOND window to the
front, any time no show is live, with the calibration tests on or off,
so you can adjust there; it is refused while a purge or a hold to arm is
going on, on Hold and between shows; come back to the rack screen (click
it, or its taskbar button) for Calibration off; it sends nothing to
BEYOND; if Windows will not let it come
forward, BEYOND's button on the taskbar flashes, click it); the purge (click a machine or a group
to arm it, then hold the Space bar, or the Stream Deck's FIRE key, to fire;
a group button arms every machine in it that can be armed and says which
it skipped and why, such as one locked out; it is green when all of the
group is armed, amber with the count and the reason when only part is,
and while any of it is armed, clicking it again takes the whole group off;
let go and it stops; nothing fires while a show is live or an Abort
stands); lock out of any one machine or laser (the L button on it: one tap
locks it out, one tap on a locked one unlocks it, with no pop-up either
way; a line under the machine map, or under the lasers, says what was
done, with Undo for 5 seconds; a tap does what the screen showed when you
tapped (a machine shown unlocked is locked), and if another screen already
did it the line says so and nothing is sent; a second tap on the same L
within about a second does nothing, so a double tap never locks and then
unlocks; a machine cannot be unlocked while its flame group is armed or
being armed, or while flamesafe is not reporting, from any screen or the
Stream Deck: the engine refuses it and the line says "Disarm <group> first,
then unlock <machine>"; a refusal is
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

**The Fireball page (Fire & Ice, when flamesafe's config has a group named
fireball).** The header's Fireball button opens one big button per Fireball
channel, numbered from flamesafe's config (on the Pico: 55 to 59), lowest
channel first. The fireball group must list exactly five fire slots; if it
does not, the page says "Fireball page unavailable: flamesafe.json fireball
group must list five fire slots" and does nothing.

- **Fuel Charge**, **Air Charge** and **Purge** are toggles: press once for
  ON, press again for OFF.
- **Valve Open** and **Igniter** are on only while your finger is on the
  button. Lift it and they go off.

ON is 255 on that channel. Nothing on this page arms. Arm the Fireball as
always, with its hold to arm (the show screen's button or its Stream Deck
key), signed in. Until it is armed every button is grey, says why, and
nothing is sent: flamesafe keeps every Fireball channel at zero, the Valve
Open included, unless the Fireball is armed. The Fireball arms only with
every channel at zero, so this page keeps them all off until it is armed,
and turns them all off when it disarms (if one still shows ON, it says "turn
Fuel, Air, Purge off before arming").

**Everything goes off by itself** (within a quarter of a second) on: Stop,
Hold, Abort, Disarm Fireball, Disarm all flame groups or the Fireball's
deck key; flamesafe not reporting; leaving the page, closing it or hiding
it; this page's status not being live; signing out; the connection
dropping; and the All off button (any screen). Losing focus (another window
clicked) lets go of Valve Open and Igniter. After any of these nothing
comes back on by itself: press again.

**When it will not work** it says why in plain words: a show live or on
Hold, an Abort standing, Run pressed (the show file owns the channels),
live flame programming or fire flames while scrubbing on, a purge selected
on the Startup page (and that purge waits while a Fireball channel is on),
any screen in Arrange mode, the Fireball locked out, or the purge switch
off (`purge_arming`, and from an iPad `screen_arming`, in
ltcplay_remote.json). One screen at a time. Signed out it is view only;
turning a channel on needs the arm permission (an operator or admin), and
All off always works. The values go out the way the Startup page's purge
does, on the engine's own flame link to flamesafe, which stays the only
program that writes to the flame node.

**First test at the rack, fuel valve closed and the key switch on test or
off, with Andy:**

1. With the Fireball disarmed, every button is grey and says "Arm the
   Fireball first". Press each one: nothing clicks at the head.
2. Arm the Fireball with its hold to arm (every channel is off, so it
   arms). The page reads ARMED.
3. Each toggle on and off once (Fuel Charge, Air Charge, Purge): the head
   follows, and goes off when pressed again.
4. Hold Valve Open, then Igniter: on while held, off the moment you lift.
5. With Fuel Charge on: press Disarm Fireball, then (armed again) Abort,
   Stop, Hold, the Fireball's deck key, and close the page. Each time every
   channel goes off at once and stays off.
6. Pull the network cable of the iPad (or close the browser) while holding
   Valve Open: it goes off within a second.
7. Only then open the fuel valve, one channel at a time.


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

## Stop, Hold and Reset on the rack screen

Jeff, 2026-10-28. Each screen has one Stop, where the button that started
things was:

- **Stop.** On the show screen, Start now turns into a red **Stop** while a
  show is live (playing, or paused on Hold). On the Programming screen, Run
  turns into **Stop** while Run is in force. Stop asks once, then does
  exactly what the Stream Deck's Stop does, a complete halt: every flame
  group and every laser is disarmed first, nothing is fired, a live show
  ends and fades to dark, then playback stops, so Run is off. Nothing is
  latched: no Reset is needed. Press Run to bring the rig back; each flame
  group needs arming again. The line under the buttons says what happened
  ("Stopped. The show ended and the rig is fading to dark...") or, if any
  part did not work, which part, and what to press. If the engine refuses,
  it says why in its own words. Stop works the same from the rack
  screen, an iPad and the Stream Deck, signed in or not. Right after Stop
  turns back into Start now (or Run) the button waits 2 seconds before it
  takes a press, and a press made just as the show ended or started does
  nothing and says so: a Stop never turns into a start.
- **Hold** pauses the show where it is (or, between shows, keeps the next
  show from starting). **Resume** carries on.
- **Reset** works from Hold. With a show paused on Hold, Reset ends that
  show: it fades to dark, nothing is latched, and no flame group is
  disarmed. With the schedule on Hold between shows, Reset ends the Hold.
  Either way it also clears the fault flag, and it never starts or
  continues a show by itself: a show whose time has already come waits as
  a delayed show for Start now (even one still inside the late grace), the
  next show starts at its own time (only while Run is pressed), and if an
  alert is open at that time the schedule still holds for it.
  After an Abort, Reset ends the Abort as it always has; if the schedule is
  also on Hold, a second Reset ends the Hold.
- **ABORT** keeps its own meaning: tap twice, the show stops, everything is
  disarmed, and the rig stays latched until Reset.

**When a show does not start, it says why.** The alert, and the show's
line on the Schedule screen, give the real reason and what to press, for
example "Show 1 did not start: Run has not been pressed, so nothing may
reach the rig; press Run on the Programming screen, then Start now". Start
now that cannot work is refused at once with the same words, and adds no
FAULT line. Right after Run the show audio takes a few seconds to load:
Start now then says "audio loading, starts when ready". Pressed then, it
waits for the audio (at most 20 seconds) and reads **Loading, starting when
ready**; the show starts as soon as the audio is in. Hold, Stop or Abort
cancels the wait and nothing starts, and so does any Hold the program
makes itself (for an announcement, or at showtime for an open alert).
While a Stop is still disarming and fading to dark, Start now and Run on
any screen are refused until it has finished, and say so. Only the show's own audio counts: a
slow or broken intermission file never holds the show back.

## How long a show runs

Jeff's field report, 2026-10-28. With the show audio played by ltcplay
("audio_master"), a show runs to the longest of its audio, its lights
file (the cue's FSEQ), and the show length set in the show file's
`clock.zones.show_len_s` when there is one. ltcplay cannot read the
video's length from MadMapper, so a video longer than both the audio and
the lights file needs that setting. The schedule's own show_len_s is the
time the night plans for a show and is not used here.

- **Audio shorter than the lights file.** Run says so first, naming both
  lengths, for example "The show audio for Show is 451.46 s, shorter than
  the lights file (454.17 s)". The show still starts. After the audio
  ends the rest plays in silence on this computer's clock: the Art-Net
  timecode, the lights, the flame cues and the video carry on to the end,
  and nothing reads as the audio lost. An LTC timecode track inside the
  show audio (`clock.audio.ltc_output`) is part of the audio, so it ends
  with the audio; Run says so when there is one. Export the audio to the
  full length to hear it to the end.
- **`clock.zones.show_len_s`** may make the show at most 60 s longer than
  its audio and lights file. More than that refuses Run, naming the
  numbers (a typo must never run an hour of silent timecode). Past the
  end of the lights file the lights and the flame cues are dark, Run says
  so, and that is not a fault.
- **The schedule's show_len_s** must be at least the show's real length.
  If it is shorter, ltcplay does not start and says which number to put
  in the schedule file.
- **At the end of a show** the timecode holds its last frame, sent 30
  times a second, so MadMapper stays on the last picture instead of going
  back to the start. The show still ends at that moment: the pixels go to
  the idle look and the scheduler goes to the intermission (flame cues to
  zero, lasers blanked) at the real end. The held frame goes to
  MadMapper alone (the timecode receiver named MadMapper in the show
  file), never to BEYOND, never by broadcast, whatever the lasers'
  blanking mode. The held frame stops when the next show starts, on Abort, or on
  Stop.
- **Abort and Stop** are as before: they stop the timecode at once,
  including after the audio has ended. A longer show never delays them.

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
the iPad question). There is no PIN. Once users are set up (USERS.md), an
iPad signed out is view only, like every screen ("Signing in" above); it
can still Stop, Hold, Abort, Acknowledge and disarm. Anything on the show
Wi-Fi that can reach that address can see the page, so keep the show Wi-Fi
to the crew.

After the last show of the night, everyone is signed out on their own:
10 minutes after the last show ends, every screen that was signed in
before it ended is signed out, once. It waits while a show is live, on
Hold or aborted, and while anything is armed or going on at the rig (a
hold to arm, a purge, a laser test, an armed laser or flame group), and
goes as soon as that is clear. Anyone who signs in after the last show
ended stays signed in for the rest of the night. It only happens on a
night with a show on the schedule. An admin
can change the minutes, or turn it off with 0, on the Users screen
(USERS.md).

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
the big sign while it is big (2 seconds) aborts, exactly as before, however
fast the second tap comes. Any finger works, two
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
("not known: no contact" while the page has lost the engine). What the show
last sent BEYOND is said in the sentence when it matters, for example
"...; lit, unconfirmed" or "...; lit? link unknown", and a laser arm that
cannot be read reads "arm not known". Point at it
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
shows and on Hold a silent heartbeat is normal and nothing is said. The
chip's tooltip also says what MadMapper's heartbeat track (/float-1) must
end at: the show's real length (see "How long a show runs"), whatever
ltcplay_fire_ice.json says. Once a show has played a minute, ltcplay
measures where MadMapper's track really ends; if that is wrong the chip
turns amber and says exactly what to set, for example "MadMapper
running, set /float-1 to end at 454.17 s, it ends at 444.42 s". Change
the track's length in MadMapper and the chip turns green during the next
show. This lamp is the only place a wrong track length shows: ltcplay's
heartbeat drift alarm is not switched on in this build, so there is no
drift alert. Off
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
show file, and Run, which reads Stop in the same place while Run is in
force), the Stream Deck operator and the show network.
Show: the scheduler starts each show at its time and scrubbing is off.
Programming: a rehearsal; play from, jump and loop work and the scheduler
starts no show. To switch, press Stop, pick the other side of the switch,
then Run. While a scheduled show is live, Programming is locked and the
switch says so. Back to the show returns to the show screen.

In Programming on a show that plays its own show audio (Fire & Ice), the
show audio is the playhead, and everything follows it together: the
music, the Art-Net timecode (so MadMapper's video and BEYOND follow), the
pixels and the show's flame cues. Play from starts the show exactly the
way Start now does, from the point you typed (MadMapper's show bank, the
audio from that point, then the rig comes up for the show). Back 10 s,
Back 5 s, Ahead 5 s and Ahead 10 s move the audio, the timecode and the
pixels at once; while paused they move the paused point, and Continue
plays from there. Pause is the show's own Hold (flame cues to zero,
lasers blanked and disarmed, music and timecode frozen) and Continue its
own Resume. The A/B loop jumps the audio back to mark A each time it
passes mark B; both marks have to be in the same cue. Stop the show
audio (the button that says Follow timecode again on other shows) takes
the rig out of the show (flame cues zero, lasers blanked) and stops the
music. The screen says Playing on the show audio or Paused on the show
audio.

While Programming is playing, Hold on any screen or the Stream Deck
pauses it the same as Pause, and also holds the schedule, so no show
starts until someone presses Resume (Continue carries the playback on but
leaves the schedule held). Abort (after it disarms every flame group and
laser, as always) stops the playback the way Stop the show audio does.
Start now and Run are refused while Programming is playing: stop the
Programming playback first.
The Rack screen's Stop and the Stream Deck's Stop stop it too. If no
operator page is open for 60 seconds, the playback stops by itself and
the night journal says so; press Play from to carry on.

A Pause pressed while a Hold is still fading out freezes where the fade
ends, and Back and Ahead are refused until it has frozen.

Nothing on the screen arms anything. Flame cues still go only to groups
an operator has armed, through flamesafe, and nothing fires for half a
second after a jump, a loop, Play from or Continue; a cue you jump over is not
played, and a flame already on where you land stays off until it ends.
The lasers stay dark in Programming: the laser gate lights them only
while a scheduled show is live. BEYOND's own timeline still follows the
timecode, so its position moves with the playhead.

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
A lock flamesafe has not taken yet reads "1 lock pending", a lockout that
cannot be trusted reads "lockout not trusted", and with the engine out of
contact the line reads "armed: not known".
The wave flamer, with a safety per fixture, keeps its own count of safeties
up. If the Startup page's machine list cannot be read the line is left
off.

A group with aim slots (the wave flamer's angle, `aim` in flamesafe.json)
has one more small line on its card, read only, for example "Aim 1, 7, 13:
loaded; ARMED; asked 54, 128, 201, sent 54, 128, 201". Loaded means
flamesafe and LTC Player have the same aim list. Asked is what the show
asked for on those slots, sent is what flamesafe really sent: 0 while the
group is not armed (it then reads held or disarmed), by design: the head
then sits at its end stop. Hover
the line to read all of it. If it says NOT loaded, an Aim slots alert says
the same in full: flamesafe's list, LTC Player's list, the flamesafe.json LTC
Player read when it started, and what to do (put the aim list in the
flamesafe.json flamesafe uses, check LTC Player reads that same file, then
quit LTC Player and start it again). It is display only: it never holds
a show at showtime, never plays the Delayed announcement, never sounds
the booth speaker and changes nothing on the Stream Deck (no ACK key, page
2 stays open). Stop, Hold, Abort and Disarm all work as usual while it
is up, and it clears itself once the lists agree. Both
programs also write their aim list and the file they read to the journal
when they start.

If the Startup page says a machine does not fit flamesafe's layout (for
example "slot 1 is on the fixture whose safety slot is None, not 6"), the
sentence ends with the full path of the flamesafe config it was checked
against. If that is not the flamesafe.json you edited, LTC Player is
reading another one.

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

Show stays Show (Jeff, 2026-10-28: "even when I press show mode, it turns
on programming mode"). The mode is what Run asked for, nothing else: Run
with Show picked is a real show and the pill reads SHOW; it reads
PROGRAMMING only after Run with Programming picked (or Rehearse, or
Rehearsal on the old page). A Fire & Ice show is on this machine's own
clock (the show audio), and such a show always holds its frame when its
clock stops, Show or Programming; before this fix that hold made every
Fire & Ice Run read PROGRAMMING and turned the switch to Programming after
a page reload, so the next Run really was a rehearsal. The switch on the
Show file card starts on the mode the engine is in, and on Show when
nothing is running (after a restart too); no mode is kept across a
restart. The old page's "When the timecode stops" switch follows the same
mode and says it in words under the switch.

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
disarmed from the screen (the Stream Deck's group keys and Stop, and the
hardware e-stop, are), and with no BEYOND OSC address the lasers are not
blanked from here.
After the press, the engine's answer stays on the screen as a banner until
it is seen, never only as a one-line note.

## The show file, and making one

The show file is the playlist: a small `.json` file that says which
render (`.fseq`) plays at which timecode, at which frame rate, and from
which folder. The renders themselves stay exactly as xLights made them;
LTC Player only reads them.

To make one from a folder of renders, on the show machine:

1. Render the sequences in xLights into the show folder, and export
   `xlights_networks.xml` into the same folder.
2. On the Programming screen's "Show folder" card, press "Make a show file
   from this folder" (or "Make a show file" on the Getting started card,
   which shows while the folder has renders and no show file). It lists
   every render, alphabetically, back to back from 01:00:00:00. It is written
   into the show folder as `<folder name>_timeline.json`, or `_2`, `_3`
   when that name is taken: an existing show file is never replaced. It is
   chosen in the Show file chooser at once. It is refused while a show
   file is running or a scheduled show is live, and says why when the
   folder has no `.fseq` files or does not exist.
3. Open the new file in Notepad (Open the show folder) and set each cue's
   `"tc"` to the timecode that render starts at in your show.
4. Check the frame rate. The file's `"fps"` is a starting guess (30, or 25
   when every render is 40 ms a frame; a render's frame time is not the
   show's timecode rate), and the card says so. Set `"fps"` to the rate
   the timecode really runs at, and add `"drop": true` for 29.97
   drop-frame: 29.97 and 30 look identical but drift about 2 seconds over
   a half hour. Read the rate in the timecode source's own settings. Once
   the show file is running (step 6), the Programming screen's Show file
   card says, in amber, when the timecode coming in runs at another rate
   or drop-frame setting than the show file's (29.97 against 30 only once
   the engine has measured it, after a few seconds of timecode).
5. Press "Check the show file" on the same card. It says how many cues it
   found, any render it could not find, any cue that starts before the one
   before it has ended, and anything else wrong.
6. Run it in Programming mode (the Show file card: Programming, then Run)
   to rehearse it. Nothing reaches the rig until Run.
