# The Weather block (wind at Thanksgiving Point)

The rack screen's main page has a small **Wind** block under the lasers.
It shows wind speed, wind direction and gusts for Thanksgiving Point in
Lehi, Utah (40.4263, -111.8875), from two separate sources, each on its
own line with its own age.

**It is advisory only.** It never arms, fires, holds or stops anything, and
nothing in the show waits for it. The on-site wind check decides, not this
screen. The block says so in small print.

## What each line means

- **Measured, I-15 Point of the Mountain (UTALP).** A real wind sensor:
  UDOT's road weather station at the Point of the Mountain gap on I-15,
  about 2 miles northwest of the venue and about 250 ft higher. It sits
  right in the gap where the strong north and south winds come through, so
  in those winds it usually reads the same as the venue or a bit higher.
  The reading comes through the Iowa State University mesonet's free
  service (UDOT's own service needs a key).
- **Forecast for this spot (NWS).** The National Weather Service's hourly
  forecast for the exact spot, for the current hour, with gusts. "Issued"
  is how long ago the Weather Service last updated it.
- **NWS alerts.** If the Weather Service has a Wind Advisory, High Wind
  Warning, Red Flag Warning or another wind or fire alert in effect for the
  spot, it shows as an orange line at the top of the block.

Speeds are in mph. Direction is where the wind blows **from**, as a compass
point and in degrees, like "from NNW (338°)".

## Age, STALE and NO CURRENT DATA

Every number is shown with its age: how old the reading itself is, not when
this PC fetched it. After a restart the block shows the last saved values
with their true age.

| Line | Shown normally | Amber **STALE** | Grey **NO CURRENT DATA, use on-site wind check** |
|---|---|---|---|
| Measured | up to 20 min old | 20 to 45 min old | over 45 min old, or never received |
| Forecast | issued up to 3 h ago | issued 3 to 6 h ago | issued over 6 h ago, or nothing for this hour |

Grey lines show no number at all.

Other messages:

- **Sources disagree, verify on site.** The measured and forecast speeds are
  more than 10 mph apart. One of them is wrong for right now; go and check.
- **No internet.** This PC cannot reach the weather services at all. Any
  values still shown are the last saved ones, with their true age.
- **The whole block grey** with "This screen has not heard from the engine":
  the screen itself lost touch with LTC Player for over a minute.

The measured station and the alerts are checked every 5 minutes, the
forecast every 15 minutes. If a check fails it is tried again after 1, then
2, then 4 minutes, then at the normal interval.

## Firewall

The show PC needs **outbound HTTPS (TCP port 443)** to these two hosts
only, and nothing inbound:

- `api.weather.gov`
- `mesonet.agron.iastate.edu`

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
could not reach either weather service. The field names come from the
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
  "nws_station": "",
  "contact": "",
  "observation_every_min": 5,
  "forecast_every_min": 15,
  "stale_min": 20,
  "no_data_min": 45,
  "forecast_stale_hours": 3,
  "forecast_no_data_hours": 6,
  "disagree_mph": 10
}
```

- `enabled`: false hides the block.
- `nws_station`: a Weather Service observation station to show as a second
  measured line (the self check lists the nearby ones). Empty means none.
  The Provo and Salt Lake airports are too far away to describe the venue.
- `contact`: an email or web address the Weather Service can reach the
  owner at; it goes in each request's User-Agent, as they ask.

A key the block does not know, or a value out of range, is refused with a
sentence on the block itself, and weather stays off until it is fixed. The
rest of `ltcplay_remote.json` is not affected.

Weather is on by itself for the Fire and Ice show (Thanksgiving Point).
Any other show (GPL at Dollywood) shows no block unless its
`ltcplay_remote.json` has a `"weather"` block.
