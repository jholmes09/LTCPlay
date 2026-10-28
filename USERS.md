# Users, sign-in and the audit log

For Jeff. What it does, what it never does, and what you decide.

## In one paragraph

Everyone who works the show PC has a name and a password. When someone
is signed in on the rack screen, every button they press, every Stream
Deck key and every setting they change is written down under their name,
with the time, the screen it came from and whether it worked. If nobody
is signed in it is still written down, as "unsigned". Once a night LTC
Player can send that record to your server. Signed out, every screen is
view only, the rack screen on the show machine too: it shows everything
but may only Stop, Hold, Abort, Acknowledge and disarm. Signing in never
stands in the way of those: they work for everyone, signed in or not,
always.

## Who is on it

The first time the new version starts it makes these people, with no
password yet:

| Name   | Role     |
|--------|----------|
| Jeff   | admin    |
| Andy   | admin    |
| Dallen | operator |
| Dave   | operator |
| Jared  | operator |

No password is shipped with the program. The first time each person signs
in they pick their own (6 characters or more), at the rack screen on the
show PC: a name with no password yet cannot be claimed from an iPad. Until
someone has, anyone at the rack screen could pick it for them, so have
each person sign in once soon after the update.

If `ltcplay_users.json` is ever damaged, or deleted after users were set
up, LTC Player does not fall back to "nobody set up". Every screen shows
an alert, every control is locked except Stop, Hold, Abort, Acknowledge
and the disarms, and nobody can sign in. A good `ltcplay_users.json` put
back in the settings folder is used at once, with no restart. To start
over with no users: close LTC Player, remove `ltcplay_users.json` and
`ltcplay_users.made` from the settings folder (Start menu, LTC Player,
"Settings and logs"), and start it again; each person then chooses a
password at the rack screen.

If the file is there but cannot be opened (an antivirus scan holding it),
nothing is deleted and nobody is signed out. While LTC Player is running,
the last good copy is used for up to 10 seconds; after that, and at once
when LTC Player has just started and has never read it, every control is
locked as above until the file opens again. It is tried again all the
time and used the moment it opens. Changes on the Users screen wait until
it can be read. If it lasts, check the antivirus.

"deck" is kept for the Stream Deck's own journal lines and cannot be a
user's name, like "unsigned", "system" and "nobody".

## Signing in

The small button at the top right of the rack screen says who is signed
in ("Signed out" when nobody is). Press it to get to the Users screen:
pick your name, type your password, Sign in. Sign out is on the same
screen. The show screen itself has no sign-in on it and still fits one
screen with no scrolling.

- Five wrong passwords in a row lock that name for 30 seconds, then 60,
  doubling up to 5 minutes at most. It is never permanent, and an admin
  can clear it at once ("Clear sign-in lockouts").
- Idle sign out is OFF on the show PC. An admin can turn it on (minutes,
  on the Users screen); 0 means off.
- Sign out after the last show is ON: 10 minutes after the last show of
  the night has ended, everyone who was signed in before it ended is
  signed out, once. It waits while a show is live, on Hold, or after an
  Abort until Reset, and while anything is armed or going on at the rig
  (a hold to arm, a purge, a laser test, an armed laser or flame group),
  then goes as soon as that is clear. A show started by hand with Start
  now counts as a show, on a night that has a show on the schedule; on a
  night with no show on the schedule it never happens. Someone who signs
  in after the last show ended stays signed in for the rest of the
  night. The night is the schedule's own, up to its 2 AM reset, so a show
  ending just before the reset still gets its sign-out. A blank or wrong
  number in the box is refused and nothing is saved. An admin can change the minutes on the
  Users screen ("Sign everyone out this many minutes after the last show
  of the night"); 0 means off. The audit log says "after the last show"
  for each one.
- The Stream Deck has no sign-in of its own. It acts as whoever is signed
  in on the rack screen of that PC, and shows their name on the Start key
  for a few seconds when it changes (SIGNED IN / NAME, or SIGNED OUT).
  With nobody signed in there its Start key reads SIGN IN and its arm keys
  do not arm; Stop, Hold, Acknowledge and its disarms always work.
- Sign out ends the sign-in at the engine, not just on the page. On the
  show machine it signs the whole machine out (the rack screen and the
  Stream Deck together) and lets go of any hold to arm under way.

## Adding people (admins)

Jeff and Andy (any admin) can, on the Users screen of the show PC:

- add a person: name, role and a temporary password. They type the
  temporary password at their first sign-in and must choose their own;
- reset a password: a new temporary one, or none (they choose one at
  their next sign-in);
- change a role, or remove a person (their audit lines stay).

The last admin can never be removed or made something else, so nobody
can lock every admin out. Every one of these changes is in the audit log,
including the ones that were refused.

## Who may do what

This table is on the Users screen, in one place, and an admin can change
the ticks there. These are the proposed defaults:

| What | admin | operator | viewer | signed out |
|---|---|---|---|---|
| **Abort, Hold, Stop, every disarm (flames and lasers), letting go, locking a machine, Acknowledge** | always | always | always | always |
| Start now, Resume, Reset, Run a show file, rehearsal scrubbing, playing an announcement, choosing the operator | yes | yes | no | never |
| Arm flames and lasers (hold to arm, the deck's arm keys, a purge, arming a laser) | yes | yes | no | never |
| Unlock a machine, turn a laser test on | yes | yes | no | never |
| Tick the Morning and Shutdown checklists | yes | yes | no | never |
| Show files and setup (show folder, choosing the show, opening a folder, schedule, tonight's list, layout) | yes | yes | no | never |
| Settings (show network, arming from a screen, fire flames while scrubbing, audit upload, idle sign out, sign out after the last show) | yes | no | no | never |
| Users (add, remove, passwords, roles, lockouts) | yes | no | no | never |

"Signed out" is view only (your field report, 2026-10-28). It replaces
the 2026-10-07 rule that nobody needed to sign in to run or arm. The
"signed out" column cannot be ticked by anyone, and the program refuses
those rows for nobody signed in whatever the settings file says. Being at
the show machine itself is not a sign-in: the rack screen is asked like
an iPad.

The first row can never be unticked by anyone. It is not in the settings
at all: it is written into the program.

On the rack screen and the iPad, the first row's buttons never ask anyone to
sign in: the ABORT stop sign in the bottom right corner (tap it twice), a flame group's
button while it reads Disarm, and Disarm all flame groups. Arm all flame
groups is arming: it follows the "Arm flames" row, group by group, and a
group the screen may not arm stays out and is named.

## The audit log

One line per event, in one file per day, in the settings folder under
`logs\audit` (Start menu, LTC Player, "Settings and logs"). The engine,
the Stream Deck program and LTC Player itself each write their own file
(`2026-10-20.engine.jsonl`, `.deck.jsonl`, `.supervisor.jsonl`).

Each line says: the time (UTC and local), who (or "unsigned", or "system"
for what the program did by itself), where (web, deck, engine,
supervisor), what was pressed or done, what to (a group, a machine, a
cue), and what came of it (accepted, refused or error, with the reason),
plus which screen or window it came from.

What is logged: every Stream Deck key going down and coming up (so holds
and the release of FIRE are there), every button on the rack screen or an
iPad that does something, sign-ins and sign-outs and failed ones, settings
and user changes, shows starting and ending, Hold and Abort taking effect,
flame groups arming and disarming, machines locked out.

What is never logged: passwords, anything typed into a password box, the
server token, mouse movement, or the screens' many-times-a-second status
checks. It is not a keylogger: it only sees presses inside LTC Player.

**Tamper evidence.** Each line carries a fingerprint of the line before
it, and each day starts from a fingerprint of the day before. Change,
remove or add a line, or remove a whole day, and the chain breaks at that
spot. "Check the chain now" on the Users screen checks it; so does
`python -m ltcplay.audit verify` for a technician.

**It never gets in the way.** The log is written in the background. If
the disk is full or the log falls behind, the press still happens; the
lost lines are counted and written as a "gap" line once it can write
again, and the rack screen's status strip shows "Audit: not writing"
meanwhile.

## The nightly upload (off until you switch it on)

On the Users screen, under Audit log, an admin sets: the server address
(it must start with https://), the shared token, the time of day (03:30
unless changed) and how many days to keep here (at least 90). Then tick
"Send the audit log to the server every night" and Save.

- Only the audit day files are ever sent. Nothing else leaves the PC.
- Each finished day is sent once, whole, with its checksum and its chain
  fingerprints. It counts as sent only when your server answers that it
  got exactly that file.
- A failed send is tried again later (1 minute, then 5, 15, then hourly).
  A night the PC was off is caught up the next time.
- A day is removed from the PC only once the server has it AND it is older
  than the keep. A day not yet on the server is never removed.
- The token is kept in the settings folder (`ltcplay_audit.json`), like
  flamesafe's link key: never in the program's files on GitHub, never on
  the screen, never in the log or the diagnostics bundle.
- A failure to send changes nothing about the show.

Your server needs a receiver: `tools/audit_receiver.py` is one, and
`tools/AUDIT_RECEIVER_README.md` says how to run it.

## What you decide

1. The names and roles above, and whether the matrix defaults are right
   ("signed out" is now view only and fixed).
2. Your server's address, and the token (the upload stays off until then).
3. How long to keep the log on the show PC (90 days unless you say more).
4. Whether the Stream Deck should ever need its own sign-in (today: no,
   it acts as the rack screen's user, and with nobody signed in there it
   cannot arm or start).
