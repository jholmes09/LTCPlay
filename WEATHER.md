# The Weather block (wind at Thanksgiving Point)

The rack screen's main page has a small **Wind** block under the lasers.
It shows wind speed, wind direction and gusts for Thanksgiving Point in
Lehi, Utah (40.4263, -111.8875): one measured line and one forecast line,
each saying when it was last updated, from sources that know nothing of
each other.

It never arms, fires, holds or stops anything, and nothing in the show
waits for it.

## What each line means

- **Measured.** A real wind sensor: the **highest usable reading** from
  these sources (so one stalled feed does not blank the wind, and a
  backup that reads low never hides a stronger wind measured elsewhere):
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
     through aviationweather.gov's free data service, both in one
     request. An airport is farther away than UTALP and is not the venue
     (Provo often reads low when the wind funnels through the gap), so a
     backup reading is **amber**, says "backup station", the airport and
     how far away it is, and why UTALP was not used or what UTALP reads,
     for example: "Measured, backup station Provo airport (KPVU), 17 mi
     away: 12 mph, gusts not reported, from S (180°). Last updated Oct 26,
     11:56 AM. Using the backup because UTALP (Iowa mesonet) stopped
     reporting, last updated Oct 26, 8:48 AM."
  3. A Weather Service observation station, only if one is named in the
     settings (`nws_station`).

  A STALE reading still counts if it is the highest: when UTALP reads 30
  mph but is 25 min old and Provo reads 8 mph and is current, the line
  shows UTALP's 30 mph, amber and marked STALE, and says Provo's beside it
  ("STALE: over its 20 min limit. KPVU (aviationweather.gov) reads 8 mph,
  last updated Oct 26, 11:56 AM, current."). Any other reading more than
  10 mph from the one shown is said the same way.
- **Forecast for this spot (NWS).** The National Weather Service's hourly
  forecast for the exact spot, for the current hour, with gusts. "NWS
  issued" is when the Weather Service last updated it; "Last updated" is
  when this PC last fetched it from the Weather Service.
- **NWS alerts.** If the Weather Service has a Wind Advisory, High Wind
  Warning, Red Flag Warning or another wind or fire alert in effect for the
  spot, it shows as an orange line at the top of the block.

Speeds are in mph. Direction is where the wind blows **from**, as a compass
point and in degrees, like "from NNW (338°)".

## Age, STALE and NO CURRENT DATA

Every number says when it was last updated, as a date and a time, never
how long ago (Jeff, 2026-10-30): "Last updated Oct 30, 2:14 PM". The time
is in the show's time zone, the "timezone" in ltcplay_schedule.json
(America/Denver when there is none), with no zone name, as the rack
screen's show times are. A measured reading's time is the reading's own,
not when this PC fetched it, so after a restart the block shows the last
saved values with their true time. With nothing received yet a line says
"No reading yet", with no time. A time ahead of this PC's clock is said as
the PC's time now.

The rules below still go by each value's age (how long before now that
time is); only the words changed. A STALE line still says STALE.

| Line | Shown normally | Amber **STALE** | Grey **NO CURRENT DATA, use on-site wind check** |
|---|---|---|---|
| Measured, UTALP (a reading every 5 min) | up to 20 min old | 20 to 45 min old | over 45 min old, failing, or never received |
| Measured, an airport (a reading every hour) | up to 75 min old (amber: a backup) | 75 to 120 min old | over 120 min old, failing, or never received |
| Forecast | this PC got it in the last hour, and the Weather Service issued it in the last 18 h | this PC has not got it for 1 to 6 h, or the Weather Service has not updated it for 18 to 36 h | this PC has not got it for over 6 h, the Weather Service has not updated it for over 36 h, its time is in the future, or nothing for this hour |

The measured line shows the highest reading inside its own source's grey
limit, current or STALE; if there is none, grey. A wind speed below 0 or
over 200 mph is not a reading.
Grey lines show no number at all. A reading past its own source's grey
limit is never shown. The airports report once an hour (around 10 minutes
before the hour), so their limits are longer; with the 5 minute limits
they would go blank for part of every hour.

**Why, in plain words.** Each line says why it is not normal. With no
current measured reading the line says why for each source, for example:
"Measured wind: NO CURRENT DATA, use on-site wind check. UTALP (Iowa
mesonet) stopped reporting, last updated Oct 26, 8:48 AM. KPVU
(aviationweather.gov) did not answer: no answer in 8 s." "Stopped
reporting" means the service answered but has nothing newer from that
station; "did not answer" means this PC could not reach the service, and
"could not be read" means the service answered with an error; both say
what went wrong. A STALE forecast says "Stale because this PC has not got
it from the NWS within the 1 h limit" or "Stale because the NWS has not
updated it within the 18 h limit".

**Why the forecast is judged on when this PC got it** (Jeff, 2026-10-28).
The Weather Service updates the forecast for a spot when its forecasters
update their grid: routinely about twice a day, and more often when the
weather is changing. Between updates it keeps serving the same forecast,
so its issue time is often 3, 6 or 10 hours old while it is still the
Weather Service's current forecast. The old rule (STALE at 3 h after the
issue time, grey at 6 h) marked a good forecast STALE most of the day and
greyed it some afternoons. Now this PC fetches it every 15 minutes and the
forecast is current as long as a fetch worked in the last hour; the issue
time makes it STALE past 18 h, which means the Weather Service missed
its updates, and grey past 36 h, so a stuck forecast served as new for days
never stays on screen as a number.

Other messages:

- **Sources disagree, verify on site.** Two current speeds (the measured
  line's, a backup airport's, or the forecast's) are more than 10 mph
  apart. One of them is wrong for right now; go and check.
- **No internet.** This PC cannot reach the weather services at all. Any
  values still shown are the last saved ones, with when each was last
  updated.
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
  "backup_stale_min": 75,
  "backup_no_data_min": 120,
  "station_limits": {},
  "forecast_stale_hours": 1,
  "forecast_no_data_hours": 6,
  "forecast_issued_stale_hours": 18,
  "forecast_issued_no_data_hours": 36,
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
- `stale_min`, `no_data_min`: UTALP's limits, in minutes (20 and 45).
- `backup_stale_min`, `backup_no_data_min`: the airports' limits, in
  minutes (75 and 120).
- `station_limits`: limits for one station, over the ones above, like
  `{"KPVU": {"stale_min": 60, "no_data_min": 90}}` (each 5 to 240 and 10
  to 720 min; no data has to be more than stale).
- `forecast_stale_hours`, `forecast_no_data_hours`: how long since this PC
  last got the forecast before it is STALE (1 h) and grey (6 h).
  Before 2026-10-28 these were counted from the issue time, with 3 and 6.
  The forecast is fetched every `forecast_every_min` (15), which has to be
  less than `forecast_stale_hours`, or a good forecast would go STALE
  between fetches; a setting that breaks this is refused with a sentence.
- `forecast_issued_stale_hours`, `forecast_issued_no_data_hours`: how long
  since the Weather Service last updated the forecast before it is STALE
  (18 h) and grey (36 h).
- `contact`: an email or web address the Weather Service can reach the
  owner at; it goes in each request's User-Agent, as they ask.

A key the block does not know, or a value out of range, is refused with a
sentence on the block itself, and weather stays off until it is fixed. The
rest of `ltcplay_remote.json` is not affected.

Weather is on by itself for the Fire and Ice show (Thanksgiving Point).
Any other show (GPL at Dollywood) shows no block unless its
`ltcplay_remote.json` has a `"weather"` block.
