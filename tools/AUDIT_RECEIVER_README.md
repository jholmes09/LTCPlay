# The audit receiver, for Jeff's server

`audit_receiver.py` takes the nightly audit and night journal upload from
LTC Player and keeps it. It is one file and needs only Python 3.7 or
newer (nothing to install).

## What it does

- Accepts a file only with the right token.
- Accepts only LTC Player audit day files (like `2026-10-20.engine.jsonl`).
- Checks the checksum LTC Player sent, and the file's chain of
  fingerprints, line by line, and that the day follows on from the day
  before it already holds.
- Keeps each file under `<store>/<date>/`, and lists every file it took in
  `<store>/index.jsonl` (with "chain_ok" true or false).
- Answers "got it" only after the file is safely on disk. LTC Player marks
  a day as sent only on that answer, and otherwise tries again later.
- Never overwrites: the same file sent twice is simply acknowledged again;
  a different file under a name it already has is kept beside it, named
  `.conflict-<time>`, and refused.

## The night journal

Since 2026-11-12 LTC Player also sends each closed night's journal, with
the same token, to the same address:

- `night_2026-11-09.journal.txt` (one plain line per event),
  `night_2026-11-09.jsonl` (the same events, every field) and
  `night_2026-11-09.summary.md` (the morning read).
- Each is checked against its checksum and kept under
  `<store>/night-journal/<night>/`, listed in
  `<store>/night-journal/index.jsonl`, and answered with the same "got it"
  as an audit file.
- A night file that changed on the PC after it was sent (a summary
  written again) comes once more, naming the copy it replaces. The newer
  is kept, and the older stays beside it as `.replaced-<checksum>`. Any
  other different file under a stored name is refused and kept beside it
  once, as `.conflict-<checksum>`.
- The journal names the operators and the screens they pressed on.
- A night whose logging was not complete still comes, marked
  `"incomplete": true` with its gap times. The index keeps both, so a
  cross-check can flag that night.
- Any other file name, or one with a folder in it, is refused.
- `tools/JOURNAL_FORMAT.md` says what each line looks like.

## Running it

1. Pick a long random token (at least 16 characters) and put it alone in
   a file, for example `token.txt`. Type the same token into LTC Player:
   rack screen, user chip, Users screen, Audit log, "Shared token".
2. Give it HTTPS. Either:
   - give it your certificate and key:

         python3 audit_receiver.py --store /srv/ltc-audit \
             --token-file token.txt --cert fullchain.pem --key privkey.pem \
             --port 8443

   - or run it without `--cert` on 127.0.0.1 behind a web server that does
     HTTPS (Caddy, nginx) and forwards to it:

         python3 audit_receiver.py --store /srv/ltc-audit \
             --token-file token.txt --bind 127.0.0.1 --port 8099

3. In LTC Player set the server address to the https address that reaches
   it, for example `https://audit.example.com:8443/ltc` (any path works),
   tick "Send the audit log and the night journal to the server every
   night", and Save. "Send
   now" sends at once.

LTC Player only ever sends to an https:// address.

## Checking what you have

    python3 audit_receiver.py verify /srv/ltc-audit

It reads every stored day again and says "ok" or "BAD" for each, and
whether each day follows on from the one before. It also checks every
night journal file against the checksum it came with. Exit code 0 means
everything verifies.

## Keep in mind

- Keep the token file private. Anyone with it can send files (but cannot
  overwrite or read what is stored).
- Back up the store folder like any other record you need to keep.
