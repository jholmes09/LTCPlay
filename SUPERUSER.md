# The SuperUser (for Jeff)

Jeff's own guide. No screen links to it. USERS.md says the rest about
users and roles.

What this protects against: someone using the screens and the
double-click tools. A person who can write the settings folder and knows
the file formats can still change it, and that is by design: it is your
own emergency access ("In an emergency", below). See "The honest limit".

## What only the SuperUser sees

- **The nightly upload:** on or off, the server address, the token, the
  time, the days kept, Send now, and its state (last upload, days waiting,
  nights waiting, errors).
- **SuperUser changes:** adding, changing the role of, resetting or
  removing a SuperUser, or making someone one.
- **Remote access over Tailscale:** on or off, who gets in, its address
  and name, the firewall and route warnings, Restart (below).

For anyone else the upload's card is not on the Users screen at all (not
greyed out: not there). Its script is not sent to their screen, its row
is not in "Who may do what", and its routes answer exactly like a route
that does not exist. No screen anyone else sees names where its settings
are kept.

You use the upload's card on the show PC's own rack screen, signed in
there. Save and Send now ask for your password again, and so does every
SuperUser change on the Users screen (type it in "Your password, for
SuperUser changes"). On an iPad you work as an
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

This works only for the very first SuperUser. While the SuperUser's own
files are in the settings folder ("In an emergency", below), a new one
needs a SuperUser's password.

## A forgotten password

Double-click **Reset SuperUser sign-in** in the program folder. It asks
whose password to reset, then a SuperUser's name and password, then a new
temporary password twice. That person chooses their own at their next
sign-in on the rack screen. Any sign-in they still had ends at once.

If you are the only SuperUser and you forget yours, use "In an
emergency", below.

A wrong password typed again (for Save, Send now or a SuperUser change)
never counts toward your sign-in lockout, so someone at a screen you
left signed in cannot lock you out. After more than 5 wrong ones in a
row the password typed again pauses for a while; signing in still works.

## If the SuperUser record is changed outside LTC Player

LTC Player keeps a fingerprint of each SuperUser and of the list of
SuperUsers. If the users file is edited by hand, or an older version
changed a SuperUser, two things happen:

- That person acts as an admin, and every screen shows "SuperUser record
  changed outside LTC Player". The alert never holds a show.
- The audit log says what changed.

You confirm them by making them superuser again on the Users screen.

If the key, the record of who is a SuperUser, or a SuperUser's
fingerprint goes missing, NO SuperUser is trusted until one confirms:
sign in on the rack screen, then Users, "Confirm you are a SuperUser",
your password, Confirm SuperUser. Only someone LTC Player itself made a
SuperUser can confirm, with the password LTC Player last saved for them.
If that does not work, use "In an emergency", below.

The alert is raised once for each change found, not on every beat. A
users file edited by hand never stops LTC Player from starting: it
starts, says what changed, trusts no changed SuperUser for the upload
until they confirm, and then works.

## In an emergency (your own way back)

You can always get back in by hand, from the settings folder (Start
menu, LTC Player, "Settings and logs"). This is on purpose. Nothing
else is needed: no code, no other person.

**You know your password but are not a SuperUser any more** (the alert
says so and Confirm does not work):

1. Start menu, **Stop LTC Player**. Wait until it has stopped.
2. In the settings folder, delete these files (some may not be there):
   - `ltcplay_superuser.made`
   - `ltcplay_users_key.json`
   - `logs\audit\ltcplay_superusers.json`
   - `ltcplay_users_signal.json`
3. Start LTC Player. It raises one "SuperUser record changed outside LTC
   Player" alert; you are an admin for now.
4. Sign in as Jeff on the rack screen.
5. Double-click **Make me SuperUser**, type `Jeff` and your password.
   The first SuperUser path is open again because those files are gone.

**You forgot your password:**

1. Start menu, **Stop LTC Player**. Wait until it has stopped.
2. Delete the same four files as above.
3. Copy `ltcplay_users.json` somewhere safe first. Then open it in
   Notepad. Find your record (`"Jeff": {`). Its `"pw":` is followed by a
   block in `{ }` braces. Replace that whole block, braces included, with
   `null`, so the line reads `"pw": null,`. Save.
4. Start LTC Player. On the rack screen sign in as Jeff with no password:
   it asks you to choose one. Choose it.
5. Double-click **Make me SuperUser**, type `Jeff` and the new password.

If LTC Player then says the users file could not be read, a brace or
comma went missing: stop it, put your copy back, and do step 3 again.

Notes:
- Never delete `ltcplay_users.json` or `ltcplay_users.made`: that starts
  the users over for everyone (USERS.md).
- Leave the audit log alone. The audit log keeps the old lines, and the
  new grant is written there with an alert saying the first SuperUser
  path was used again after the SuperUser files were removed by hand.
- While any one of the four files above is there, Make me SuperUser asks
  for a current SuperUser's password instead.
- A test (`test_superuser_emergency_procedure_by_hand`) does exactly these
  steps, so a later change cannot break them unnoticed.

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
- you get your SuperUser back on the rack screen (Confirm SuperUser) or,
  if that does not work, with "In an emergency", above.

## The nightly upload (off until you switch it on)

In the Nightly upload card you set the server address (it must start
with https://), the shared token, the time of day (03:30 unless changed)
and how many days to keep here (at least 90). Then tick "Send the audit
log and the night journal to the server every night", type your
password, and Save.

- The audit log and the night journal go only to your own server.
  Nothing else is read or sent, never a password or token. The night
  journal names each operator and the screen they pressed on.
- The night journal is each closed night's three files
  (`night_YYYY-MM-DD.journal.txt`, `.jsonl` and `.summary.md`), each whole,
  with its checksum. A night still open is never sent. A night whose
  logging was incomplete is still sent, marked "incomplete" with its gap
  times, so your server can flag it. Incident folders are never sent.
  A file that changed after it was sent (a summary written again) is
  sent once more. A file your server refuses for good is listed on the
  card as not sent (and in the audit log), and the later nights still
  go. `tools/JOURNAL_FORMAT.md` says what the lines look like.
- Each finished day is sent once, whole, with its checksum and its chain
  fingerprints. It counts as sent only when your server answers that it
  got exactly that file.
- A failed send is tried again later (1 minute, then 5, 15, then hourly).
  A night the PC was off is caught up the next time.
- A day is removed from the PC only once the server has it AND it is
  older than the keep. A day not yet on the server is never removed.
  Night journal files keep their own 120 days. While the upload is on, a
  night file not yet on the server is kept up to 30 days longer, then
  removed.
- The settings and the token are kept in the settings folder, in
  `ltcplay_audit.json`, like flamesafe's link key. They are never in the
  program's files on GitHub, never on the screen, and never in the log
  or the diagnostics bundle.
- A failure to send changes nothing about the show.

Your server needs a receiver: `tools/audit_receiver.py` is one, and
`tools/AUDIT_RECEIVER_README.md` says how to run it.

## Remote access over Tailscale (off until you switch it on)

Your card, on the rack screen's Schedule tab, signed in there. Nobody else
sees it, its script or its routes (they answer like a route that is not
there), exactly like the upload's card.

**Switch it on.**

- Turn on, then Restart the engine between shows.
- Use the installer again: it adds the firewall rule.
- The card says: URL, address, listening, firewall rule.

**Say who gets in.**

- One Tailscale login per line, as Tailscale shows it.
- Empty means nobody. People's logins only, no tagged devices.
- Changes count within 3 seconds.

The show PC is `ignite-player.tail342a0a.ts.net` (100.75.53.81); the URL
to give people is `http://ignite-player.tail342a0a.ts.net:7878/`. The page
also answers to `ignite-player:7878` on your own devices, and to the
address, and to nothing else. Your own login is
`jeffholmespresents@gmail.com`. Andy uses his own Tailscale account: share
only the show PC to him from the Tailscale admin console (Machines, the
show PC, Share), and once he accepts, add his login exactly as Tailscale
shows it. A shared user must use the full name. The login is what counts,
never a device's address. For every connection the engine asks this PC's
own Tailscale who it is (`tailscale whois`); no answer, a tagged device or
a login not on your list is refused and audited.

Past that, a device on Tailscale is an iPad on the show network: the same
role matrix and the same "Arming from a screen". Every press that needs a
role needs a sign-in there, and a Tailscale sign-in ends by itself after
30 minutes with no press (the idle sign out can make that shorter, never
longer). Use long passwords: Tailscale reaches past the venue. Tailscale
cannot prove anyone is at the rig.

**Never share routes from the show PC.**

- No subnet routes, no exit node, ever.
- Not the show LAN, 10.112.4.0/24, above all.
- The card warns if this PC shares routes.

Shared routes would let tailnet devices reach the flame controllers
directly, past the engine, and arrive with a show network address, so
your list would not be asked. In the tailnet policy approve no routes and
no exit node: `"autoApprovers": {"routes": {}, "exitNode": []}`. Limit the
tailnet to your own devices on 7878:

```
{
  "tagOwners": {"tag:showpc": ["jeffholmespresents@gmail.com"]},
  "acls": [
    {"action": "accept", "src": ["jeffholmespresents@gmail.com"],
     "dst": ["tag:showpc:7878"]}
  ]
}
```

(A shared device reaches only the shared show PC. If you tag the show PC,
keep your own login on it as above, and remove the default allow-all
rule.)

**Never use Tailscale Serve, Funnel or HTTPS here.**

- They are refused as proxies.
- TCP Serve would make everyone the rack screen.
- Turn them off: `tailscale serve reset`.

Turning it off: Turn off (the door closes at once, during a show too),
then use the installer again, which removes the rule.

## The honest limit

This protects against someone using the screens and the double-click
tools. A person who can write the settings folder and knows the file
formats can still change it: the users file, the SuperUser's files and
the upload's settings. That is by design: it is your own emergency
access ("In an emergency", above). Most changes to a SuperUser made that
way are caught (that person acts as an admin and the alert shows), but
the emergency procedure itself works for anyone who follows it, and an
upload setting changed by hand is not caught. The only way to prevent
it is a separate Windows account for LTC Player, so other accounts
cannot write its settings folder. That is not built.

## What you decide

1. Your server's address, and the token. The upload stays off until
   then.
2. How long to keep the log on the show PC: 90 days unless you say more.
