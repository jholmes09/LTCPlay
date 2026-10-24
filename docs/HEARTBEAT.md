# The engine heartbeat

The show engine (`ltc serve`, LTC Player's engine on the show PC) publishes
a heartbeat that a watchdog program or a smart power relay can read. It is
publish only: it answers `GET` and nothing else. Nothing in LTC Player
power cycles the PC, restarts the engine or acts on the heartbeat. What a
watchdog does with it is decided outside this program, by a person.

## Where it is

    GET http://127.0.0.1:7881/heartbeat

On the show PC itself, always (unless turned off). Optionally also on the
show network address, same port, when `ltcplay_remote.json` says:

    "heartbeat": {"show_network": true}

Other settings in the same block: `"port": 7881`, `"file": true` (also
write the same JSON to `ltcplay_heartbeat.json` in the settings folder,
every second), `"off": true`.

## What it says

    {"service": "ltcplay-engine", "v": 1,
     "counter": 1234,                  one more every second
     "ts_utc": "2026-10-24T05:31:55.120Z", "ts_unix": 1793000000.12,
     "mono_s": 8123.4,                 the engine's monotonic clock
     "beat_age_s": 0.4,                seconds since the beat last ran
     "pid": 4120, "started_utc": "...", "every_s": 1.0,
     "engine": {"run": true, "state": "Run", "armed_groups": 0,
                "schedule": "SHOW", "next_start": "2026-10-24T20:00:00-04:00",
                "frames_out": 812345, "since_ok_s": 0.02},
     "black_box": {"state": "recording", "age_s": 2.1}}

`state` is `Run`, `starting` or `idle`. `armed_groups` is the number of
flame groups flamesafe last said were armed, or `unavailable`.

## How a watchdog reads it

Poll every 5 seconds, with a 3 second timeout. Keep the last `counter`.

* **Healthy**: an answer, `counter` higher than last time, `beat_age_s`
  under 3.
* **Engine thread stuck**: answers, but `counter` has not moved for 15 s
  or `beat_age_s` is over 15.
* **Engine or PC frozen**: no answer for 30 s (six polls in a row).

A new `pid` or `started_utc` means the engine was restarted; the counter
starts again from 1.

## What it must never do

Never power cycle, restart or otherwise act while `engine.run` is true or
`engine.armed_groups` is above 0, and never act on one missed poll. A
frozen PC during a show is a person's call: flamesafe and the hardware
e-stop keep the flames safe whatever the PC does. Any automatic action
outside a show (for example, a relay that power cycles a PC frozen for
10 minutes with nothing scheduled) is Jeff's to decide and set up, and
lives in the watchdog, never in LTC Player.

## The black box

The heartbeat's `black_box` is the recorder in `ltcplay/blackbox.py`: one
JSON line every 5 seconds in `logs/blackbox/blackbox.jsonl` in the
settings folder (CPU, memory, disk free, a disk write probe, the
temperatures Windows will give without an administrator, and the engine's
state), written and flushed to disk line by line so the last line before a
hard reset survives. `python -m ltcplay.blackbox` lists each run, every
gap, and the last record.
