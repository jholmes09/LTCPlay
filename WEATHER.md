# The Weather block (wind at Thanksgiving Point)

The rack screen's main page has a small **Wind** block under the lasers.
It shows wind speed, wind direction and gusts for Thanksgiving Point in
Lehi, Utah (40.4263, -111.8875): one measured line and one forecast line,
each with its own age, from sources that know nothing of each other.

**It is advisory only.** It never arms, fires, holds or stops anything, and
nothing in the show waits for it. The on-site wind check decides, not this
screen. The block says so in small print.

## What each line means

- **Measured.** A real wind sensor, from the first source in this list
  that has a current reading (so one stalled feed does not blank the
  wind):
  1. **I-15 Point of the Mountain (UTALP).** UDOT's road weather station
     at the Point of the Mountain gap on I-15, about 2 miles northwest of
     the venue and about 250 ft higher. It sits right in the gap where the
     strong north and south winds come through, so in those winds it
     usually reads the same as the venue or a bit higher. The reading
     comes through the Iowa State University mesonet's free service
     (UDOT's own service needs a key).
  2. **The backup airports**, Provo (KPVU, about 17 miles south east, in
     the same valley as the venue) and then Salt Lake City (KSLC, about 25
     miles north). Their official airport weather reports (METARs) come
     through aviationweather.gov's free data service. An airport is
     farther away than UTALP and is not the venue, so the line says
     "backup", the airport, how far away it is and why UTALP was passed
     over, for example: "Measured, backup Provo airport (KPVU), 17 miles
     away: 12 mph, gusts not reported, from S (180°). 4 min old. Using it
     because UTALP (Iowa mesonet) stopped reporting at 3:40 AM, last
     reading 3 h 12 min ago."
  3. A Weather Service observation station, only if one is named in the
     settings (`nws_station`).

  When a backup is in use, or two current sources are more than 10 mph
  apart, the line says which source it uses and why.
- **Forecast for this spot (NWS).** The National Weather Service's hourly
  forecast for the exact spot, for the current hour, with gusts. "Issued"
  is how long ago the Weather Service last updated it; "this PC got it" is
  how long ago this PC last fetched it from the Weather Service.
- **NWS alerts.** If the Weather Service has a Wind Advisory, High Wind
  Warning, Red Flag Warning or another wind or fire alert in effect for the
  spot, it shows as an orange line at the top of the block.

Speeds are in mph. Direction is where the wind blows **from**, as a compass
point and in degrees, like "from NNW (338°)".

## Age, STALE and NO CURRENT DATA

Every number is shown with its age. A measured reading's age is how old
the reading itself is, not when this PC fetched it. After a restart the
block shows the last saved values with their true age.

| Line | Shown normally | Amber **STALE** | Grey **NO CURRENT DATA, use on-site wind check** |
|---|---|---|---|
| Measured | a source up to 20 min old | no source up to 20 min old, one 20 to 45 min old | every source over 45 min old, failing, or never received |
| Forecast | this PC got it in the last hour, and the Weather Service issued it in the last 18 h | this PC has not got it for 1 to 6 h, or the Weather Service has not updated it for over 18 h | this PC has not got it for over 6 h, or nothing for this hour |

Grey lines show no number at all. A measured reading over 45 min old is
never used, from any source.

**Why, in plain words.** Each line says why it is not normal. With no
current measured reading the line says why for each source, for example:
"Measured wind: NO CURRENT DATA, use on-site wind check. UTALP (Iowa
mesonet) stopped reporting at 3:40 AM, last reading 3 h 12 min ago. KPVU
(aviationweather.gov) did not answer: no answer in 8 s." "Stopped
reporting" means the service answered but has nothing newer from that
station; "did not answer" means this PC could not get an answer, and says
what went wrong. A STALE forecast says "Stale because this PC has not got
it from the NWS for ..." or "Stale because the NWS has not updated it for
...".

**Why the forecast is judged on when this PC got it** (Jeff, 2026-10-28).
The Weather Service updates the forecast for a spot when its forecasters
update their grid: routinely about twice a day, and more often when the
weather is changing. Between updates it keeps serving the same forecast,
so its issue time is often 3, 6 or 10 hours old while it is still the
Weather Service's current forecast. The old rule (STALE at 3 h after the
issue time, grey at 6 h) marked a good forecast STALE most of the day and
greyed it some afternoons. Now this PC fetches it every 15 minutes and the
forecast is current as long as a fetch worked in the last hour; the issue
time only makes it STALE past 18 h, which means the Weather Service missed
its updates.

Other messages:

- **Sources disagree, verify on site.** Two current speeds (the measured
  line's, a backup airport's, or the forecast's) are more than 10 mph
  apart. One of them is wrong for right now; go and check.
- **No internet.** This PC cannot reach the weather services at all. Any
  values still shown are the last saved ones, with their true age.
- **The whole block grey** with "This screen has not heard from the engine":
  the screen itself lost touch with LTC Player for over a minute.

Each measured source and the alerts are checked every 5 minutes (the
backups too, so they are ready when UTALP stops), the forecast every 15
minutes. If a check fails it is tried again after 1, then
2, then 4 minutes, then at the normal interval.

## Firewall

The show PC needs **outbound HTTPS (TCP port 443)** to these three hosts
only, and nothing inbound:

- `api.weather.gov`
- `mesonet.agron.iastate.edu`
- `aviationweather.gov` (the backup airports)

If the show network has no internet, the block says "No internet" and the
show runs exactly the same.

## Checking it works: the weather self check

Run `ltc weather --check` on the show PC (on Windows, `ltcplay.exe weather
--check` from the LTC Player folder). It fetches each source once, prints
the start of what came back, and says PASS or FAIL for each, then RESULT:
PASS or RESULT: FAIL. It also lists the Weather Service's observation
stations nearest the venue, with their distance in miles, in case a closer
one is wanted later.

**UNVERIFIED against live data.** When this was built, the build machine
could not reach any of the weather services (aviationweather.gov included,
2026-10-28). The field names come from the
services' published shapes (the mesonet's own source code, and real
Weather Service answers kept as test samples by another project), not from
a live fetch. Run the self check on the show PC once before relying on the
block.

## Settings (optional)

Nothing has to be set. To change something, add a `"weather"` block to
`ltcplay_remote.json` in the settings folder. Every key is optional:

```json
"weather": {
  "enabled": true,
  "lat": 40.4263,
  "lon": -111.8875,
  "mesonet_network": "UT_RWIS",
  "mesonet_station": "UTALP",
  "mesonet_name": "I-15 Point of the Mountain",
  "backup_stations": ["KPVU", "KSLC"],
  "nws_station": "",
  "contact": "",
  "observation_every_min": 5,
  "forecast_every_min": 15,
  "stale_min": 20,
  "no_data_min": 45,
  "forecast_stale_hours": 1,
  "forecast_no_data_hours": 6,
  "forecast_issued_stale_hours": 18,
  "disagree_mph": 10
}
```

- `enabled`: false hides the block.
- `backup_stations`: the airport stations tried, in order, when UTALP has
  no current reading, by their four letter airport code (aviationweather.gov
  serves every official airport weather station). `[]` means no backup; at
  most 4. Provo is first because it is in the same valley as the venue.
  South Valley airport in West Jordan (`KU42`, about 14 miles north) is a
  little closer but is in the Salt Lake valley, across the Point of the
  Mountain gap; it can be added if wanted.
- `nws_station`: a Weather Service observation station to use last, after
  the backups (the self check lists the nearby ones). Empty means none.
- `stale_min`, `no_data_min`: the measured limits, in minutes (20 and 45).
- `forecast_stale_hours`, `forecast_no_data_hours`: how long since this PC
  last got the forecast before it is STALE (1 h) and grey (6 h).
  Before 2026-10-28 these were counted from the issue time, with 3 and 6.
- `forecast_issued_stale_hours`: how long since the Weather Service last
  updated the forecast before it is STALE (18 h).
- `contact`: an email or web address the Weather Service can reach the
  owner at; it goes in each request's User-Agent, as they ask.

A key the block does not know, or a value out of range, is refused with a
sentence on the block itself, and weather stays off until it is fixed. The
rest of `ltcplay_remote.json` is not affected.

Weather is on by itself for the Fire and Ice show (Thanksgiving Point).
Any other show (GPL at Dollywood) shows no block unless its
`ltcplay_remote.json` has a `"weather"` block.
