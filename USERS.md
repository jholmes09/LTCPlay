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
but may only Stop, Hold, Acknowledge and disarm. Signing in never
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

Nobody is made a SuperUser at first run ("The SuperUser", below).

No password is shipped with the program. The first time each person signs
in they pick their own (6 characters or more), at the rack screen on the
show PC: a name with no password yet cannot be claimed from an iPad. Until
someone has, anyone at the rack screen could pick it for them, so have
each person sign in once soon after the update.

If `ltcplay_users.json` is ever damaged, or deleted after users were set
up, LTC Player does not fall back to "nobody set up". Every screen shows
an alert, every control is locked except Stop, Hold, Acknowledge
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
time and used the moment it opens. A change on the Users screen made
meanwhile is refused with "try again in a moment" and nothing is changed;
press it again once the file opens. If it lasts, check the antivirus.

"deck" is kept for the Stream Deck's own journal lines and cannot be a
user's name, like "unsigned", "system" and "nobody".

## Signing in

The button at the top right of the screen says who is signed in ("Sign
in" when nobody is). Press it to sign in: tap your name, type your
password, Sign in. Signed in, the same button offers Users (the Users
pane: your own password, the users, who may do what and the audit log)
and Sign out. The Users pane also opens from Settings, Users. The show
screen itself has no sign-in form on it.

- Five wrong passwords in a row lock that name for 30 seconds, then 60,
  doubling up to 5 minutes at most. It is never permanent, and an admin
  can clear it at once ("Clear sign-in lockouts").
- Idle sign out is OFF on the show PC. An admin can turn it on (minutes,
  on the Users screen); 0 means off.
- Sign out after the last show is ON: 10 minutes after the last show of
  the night has ended, everyone who was signed in before it ended is
  signed out, once. It waits while a show is live or on Hold, and while
  anything is armed or going on at the rig
  (a hold to arm, a purge, a laser test, an armed laser or flame group),
  then goes as soon as that is clear. A show started by hand with Start
  now counts as a show, on a night that has a show on the schedule; on a
  night with no show on the schedule it never happens. Someone who signs
  in after the last show ended stays signed in for the rest of the
  night. The night is the schedule's own, up to where it hands over to
  the next (2 AM, or later while a show of it still has to run), so a
  show ending just before then still gets its sign-out. A blank or wrong
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

## The SuperUser

One level above admin, for Jeff. A SuperUser can do everything an admin
can, and is the only one who adds, changes the role of, resets the
password of, or removes a SuperUser, and the only one who sees a few
things that are Jeff's alone. Nobody else is shown those at all.

An admin (Andy) keeps everything else: users, roles, who may do what,
idle sign out, sign out after the last show, the audit log's state and
"Check the chain now". An admin can never make anyone a SuperUser
(themselves included), never change, reset or remove a SuperUser, and no
tick in "Who may do what" can change that. The last SuperUser can never
be removed or made something else.

A SuperUser that LTC Player did not make itself (the users file edited by
hand, or changed by an older version) acts as an admin until a SuperUser
confirms them, and every screen shows an alert saying so.

This protects against someone using the screens and the double-click
tools. A person who can write the settings folder and knows the file
formats can still change the users file and the SuperUser's files. That
is by design: it is Jeff's own way in, in an emergency. The only way to
prevent it is a separate Windows account for LTC Player, and that is not
built.

## Adding people (admins)

Jeff and Andy (any admin) can, on the Users screen of the show PC:

- add a person: name, role and a temporary password. They type the
  temporary password at their first sign-in and must choose their own;
- reset a password: a new temporary one, or none (they choose one at
  their next sign-in);
- change a role, or remove a person (their audit lines stay).

The last admin can never be removed or made something else, so nobody
can lock every admin out (a SuperUser counts as an admin here). Only a
SuperUser can change a SuperUser. Every one of these changes is in the audit log,
including the ones that were refused.

## Who may do what

This table is on the Users screen, in one place, and an admin can change
the ticks there. These are the proposed defaults:

| What | SuperUser | admin | operator | viewer | signed out |
|---|---|---|---|---|---|
| **Stop, Hold, every disarm (flames and lasers), letting go, locking a machine, a hazer disarm, Hazers off, the fuel pump off, Fireball practice's Done, Acknowledge** | always | always | always | always | always |
| Start now, Start at a cue, Resume, Run a show file, rehearsal scrubbing, playing an announcement, choosing the operator | always | yes | yes | no | never |
| Arm flames and lasers (hold to arm, the deck's arm keys, a purge, momentary or timed, the Fireball page's channels, the wave flamer cleaning, arming a laser, arming a hazer, the hazer levels, turning the fuel pump on, Fireball practice's open and Step, which need the row above too) | always | yes | yes | no | never |
| Unlock a machine, turn a laser test on | always | yes | yes | no | never |
| Tick the Morning and Shutdown checklists | always | yes | yes | no | never |
| Show files and setup (show folder, choosing the show, opening a folder, schedule, tonight's list, layout) | always | yes | yes | no | never |
| Settings (show network, arming from a screen, moving Programming playback from an iPad while flames follow it, idle sign out, sign out after the last show, the play-from cues, the fireball timeline) | always | yes | no | no | never |
| Users (add, remove, passwords, roles, lockouts; a SuperUser only by a SuperUser) | always | yes | no | no | never |
| The nightly upload (SuperUser only; shown only to a SuperUser) | always | never | never | never | never |

"Signed out" is view only (your field report, 2026-10-28). It replaces
the 2026-10-07 rule that nobody needed to sign in to run or arm. The
"signed out" column cannot be ticked by anyone, and the program refuses
those rows for nobody signed in whatever the settings file says. Being at
the show machine itself is not a sign-in: the rack screen is asked like
an iPad.

The first row can never be unticked by anyone. It is not in the settings
at all: it is written into the program.

On the rack screen and the iPad, the first row's buttons never ask anyone to
sign in: Stop (tap it twice; there is no Abort any more), a flame group's
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
and user changes, shows starting and ending, Hold and Stop taking effect,
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

## What you decide

1. The names and roles above, and whether the matrix defaults are right
   ("signed out" is now view only and fixed).
2. Whether the Stream Deck should ever need its own sign-in (today: no,
   it acts as the rack screen's user, and with nobody signed in there it
   cannot arm or start).
