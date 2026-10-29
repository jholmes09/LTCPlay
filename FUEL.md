# The Fuel page

How much fuel each flame unit has left, and how many shows that is.
Open it with the **Fuel** button at the top of the rack screen (or on any
iPad on the show network). The button turns amber when a unit needs a
refill soon.

## What it tracks

- Every **G-Flame** (front row and cat-walk) has its own **100 lb propane**
  tank.
- Every **Showven** wave flamer has its own **15 gallon** tank.
- **Fireballs are not tracked.** Their sensors are being looked at
  separately.

Each unit gets a tile: a health bar (green, amber, red), the **shows left**,
and a line in plain words. A unit that is locked out stays on the page; it
just burns nothing while it is locked.

## How it measures

LTC Player watches the flame frames it sends to flamesafe and counts, for
each unit, the **seconds its flame is on**: a fire value of 229 or more on a
G-Flame, 254 or more on a Showven's ignition, duration or preset channel (CH3 to
CH5). It never changes or slows a flame frame;
it only watches them.

**When in doubt, it reads emptier, never fuller.**

- A group flamesafe says is not armed burns nothing (flamesafe holds it off).
- If flamesafe's armed state is not known, the flame is counted anyway. The
  page says so, and the tank may read a little low, never high.
- If some flame could not be counted (LTC Player stopped without closing,
  or the tracker fell behind), the page says so at the top and those units
  read **Not sure** until their next refill. A "not sure" bar is never
  green.
- A Showven's angle (CH1) and speed (CH2, 255 while the head moves) are
  never counted: they light nothing, so a head that sweeps with no flame
  burns nothing.
- A Showven firing from its preset sequence channel (CH5) is counted only
  while that channel reads 254 or more.

Flame time goes in one of three piles:

- **Shows.** A show is a performance, started by the schedule or by hand
  (Run, Start now). It counts once its timecode has run 30 seconds, whether
  it plays to the end or is aborted. Time on Hold does not count toward the
  30 seconds, and a short break in the timecode does not split one show into
  two. A start that fails, and anything in a Programming session, is not a
  show.
- **Purges.** A G-Flame's purge burns its own tank, so it counts. A Showven
  purges from separate purge fuel, so it does not.
- **Other.** Programming playback with flames, live flame programming and
  rehearsal runs. Real fuel, never a show.

## How to refill

When a tank has been filled, press **Refilled** on that unit's tile and say
yes. If every tank was filled, press **Refill all** at the top and say yes. If
the list of units changed since the page was drawn, it refuses and asks you
to look again.

You need to be signed in as an **operator** or an **admin** to log a refill
(the user chip at the top of the screen). Anyone can look at the page.
Each refill is saved at once with who logged it and when.

## How it learns

It learns how long a tank lasts from your refills: the flame seconds
between two refills of one unit is one tank. It uses every unit of the same
kind (all the G-Flames share what they learn, all the Showvens share
theirs).

- Until it has seen 3 tanks it uses the **smallest** one and says it is
  still learning. A forgotten Refilled press makes a tank look twice as
  big, so only a small one is trusted.
- From 3 tanks on it takes the middle value of the last 5 and leaves out one
  that is far off.
- A tank much longer than the others is named at the top: a Refilled press
  was probably missed.

- Until a kind has a full tank between two refills, its tiles say
  **Learning** and show the flame time and shows since the refill.
- **Shows left** is what is left of the tank divided by that unit's average
  flame per show over its last 10 shows, rounded down. It needs at least 3
  shows first.
- Tip: refill when tanks are really empty or really full. If tanks are
  often swapped early, it learns that your tanks last as long as you let
  them.

If you already know roughly how many shows a tank lasts, you can give a
starting number so the page has something to show while it learns (below).
Before the page knows how much flame a show uses, that is only a count of
shows that leaves out purges, so it is shown as **rough, may be high** and
never green.

## How to fix a mistake

An **admin** sees **Recent refills** at the bottom of the page. Press
**Undo** on the wrong one and say yes. It stays in the record, marked undone
with who undid it, and that unit counts from its refill before.

## Settings (optional)

In `ltcplay_morning.json` (the settings folder), a `fuel` block. Leave any
key out to keep the default. A key it does not know is refused, and the
Fuel page says which and why.

```json
"fuel": {
  "kinds": {
    "gflame": {"label": "100 lb propane", "count_purge": true, "start_shows_per_tank": null},
    "waver":  {"label": "15 gal", "count_purge": false, "start_shows_per_tank": null}
  },
  "learn_refills": 5,
  "recent_shows": 10,
  "min_shows": 3,
  "min_show_s": 30,
  "amber_below_percent": 35,
  "red_below_percent": 15,
  "refill_soon_shows": 3
}
```

- `start_shows_per_tank`: your starting guess of shows per tank.
- `count_purge`: whether a purge counts against that kind's tank.
- `fire_at` (per kind): the lowest fire value that makes flame (229 and 254
  by default). Change it if a Showven in your show fires another way.
- `outlier_ratio`: how far off a tank may be and still be learned from
  (2 by default).
- The bar is amber under `amber_below_percent` and red under
  `red_below_percent`; it says **Refill soon** at `refill_soon_shows` shows
  left or fewer.

## Where it is kept

`fuel.json` in the settings folder (the same folder as the Morning page's
files). It is written safely (a new file, then swapped in), at once on every
refill and undo, at least every 30 seconds while flames burn, and when LTC
Player stops (it never holds up the flames or the shutdown to do so). A show running when LTC Player restarts is still counted. If
the file can ever not be read, it is kept aside under another name, never
written over, and the page says so.
