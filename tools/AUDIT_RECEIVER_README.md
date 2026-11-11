# The audit receiver, for Jeff's server

`audit_receiver.py` takes the nightly audit upload from LTC Player and
keeps it. It is one file and needs only Python 3 (nothing to install).

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
   tick "Send the audit log to the server every night", and Save. "Send
   now" sends at once.

LTC Player only ever sends to an https:// address.

## Checking what you have

    python3 audit_receiver.py verify /srv/ltc-audit

It reads every stored day again and says "ok" or "BAD" for each, and
whether each day follows on from the one before. Exit code 0 means every
day verifies.

## Keep in mind

- Keep the token file private. Anyone with it can send files (but cannot
  overwrite or read what is stored).
- Back up the store folder like any other record you need to keep.
