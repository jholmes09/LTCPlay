# The SuperUser (for Jeff)

Jeff's own guide. No screen links to it. USERS.md says the rest about
users and roles.

## What only the SuperUser sees

- **The nightly upload:** on or off, the server address, the token, the
  time, the days kept, Send now, and its state (last upload, days waiting,
  errors).
- **SuperUser changes:** adding, changing the role of, resetting or
  removing a SuperUser, or making someone one.

For anyone else the upload's card is not on the Users screen at all (not
greyed out: not there). Its script is not sent to their screen, its row
is not in "Who may do what", and its routes answer exactly like a route
that does not exist. No screen anyone else sees names where its settings
are kept.

You use the upload's card on the show PC's own rack screen, signed in
there. Each Save asks for your password again. On an iPad you work as an
admin; over Tailscale the card is never there at all. Your sign-in on the
rack screen ends after 15 minutes with no press, whatever the idle
settings say.

Every grant, every refused attempt (who and what, never a password or the
token) and every change to the upload is in the audit log.

## Becoming the SuperUser, once

1. Sign in as Jeff on the rack screen, so the name has your own password.
2. On the show PC, open the LTC Player program folder and double-click
   **Make me SuperUser**.
3. Type your name, then your own password (it stays hidden).

It says "Jeff is now a SuperUser." Nothing needs restarting, and nothing
on the rig changes.

## A forgotten password

Double-click **Reset SuperUser sign-in** in the program folder. It asks
whose password to reset, then a SuperUser's name and password, then a new
temporary password twice. That person chooses their own at their next
sign-in on the rack screen. Any sign-in they still had ends at once.

Once a SuperUser exists, both launchers need a current SuperUser's
password. So if you are the only SuperUser and you forget yours, a
technician has to recover it:

1. Remove `su_fp` from your record in `ltcplay_users.json`. You then act
   as an admin, and every screen shows the alert.
2. An admin resets your password.
3. You sign in and choose your own.
4. Double-click **Make me SuperUser** again.

## If the SuperUser record is changed outside LTC Player

LTC Player keeps a fingerprint of each SuperUser and of the list of
SuperUsers. If the users file is edited by hand, or an older version
changed a SuperUser, two things happen:

- That person acts as an admin, and every screen shows "SuperUser record
  changed outside LTC Player". The alert never holds a show.
- The audit log says what changed.

You confirm them by making them superuser again on the Users screen.

## An older version (after Roll back)

An older version does not know the SuperUser role.

**What it cannot do:**
- give you, or anyone, admin powers through the SuperUser role. You are
  view only there; Stop, Hold, Acknowledge and every disarm still work.

**What it can do** (its own old rules, which this build cannot prevent
while the older version runs):
- an admin can change your role, reset or clear your password, or
  remove you.

**When this version runs again:**
- a password or role an older version changed does not match your
  fingerprint, so you act as an admin and the alert shows;
- a password an older version cleared or reset can never be claimed;
- you get your SuperUser back with the steps in "A forgotten password".

## The nightly upload (off until you switch it on)

In the Nightly upload card you set the server address (it must start
with https://), the shared token, the time of day (03:30 unless changed)
and how many days to keep here (at least 90). Then tick "Send the audit
log to the server every night", type your password, and Save.

- Only the audit day files are ever sent. Nothing else leaves the PC.
- Each finished day is sent once, whole, with its checksum and its chain
  fingerprints. It counts as sent only when your server answers that it
  got exactly that file.
- A failed send is tried again later (1 minute, then 5, 15, then hourly).
  A night the PC was off is caught up the next time.
- A day is removed from the PC only once the server has it AND it is
  older than the keep. A day not yet on the server is never removed.
- The settings and the token are kept in the settings folder, in
  `ltcplay_audit.json`, like flamesafe's link key. They are never in the
  program's files on GitHub, never on the screen, and never in the log
  or the diagnostics bundle.
- A failure to send changes nothing about the show.

Your server needs a receiver: `tools/audit_receiver.py` is one, and
`tools/AUDIT_RECEIVER_README.md` says how to run it.

## The honest limit

This is software separation only. Anyone with Windows administrator
rights on the show PC can edit the settings files or stop LTC Player. A
SuperUser record changed that way is caught (it acts as an admin and the
alert shows), but an upload setting changed by hand is not. Give Andy a
standard (not administrator) Windows account on the show PC.

## What you decide

1. Your server's address, and the token. The upload stays off until
   then.
2. How long to keep the log on the show PC: 90 days unless you say more.
