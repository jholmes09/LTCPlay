"""Wind at the venue, for the rack screen's main page (WEATHER.md).

What the Weather block shows
============================
Wind speed, wind direction and gusts at Thanksgiving Point (Lehi, Utah),
from places that know nothing of each other, each with its own age:

  MEASURED   one line, the HIGHEST usable reading from these sources
             (Jeff, 2026-10-28: one stalled feed must not blank the wind;
             the highest, review of PR #154, since an airport can read low
             when the wind funnels through the Point of the Mountain gap):
               1. UDOT's road weather station at the I-15 Point of the
                  Mountain gap (UTALP), about 2 miles from the venue, read
                  through the Iowa State mesonet's free currents service
                  (no key; UDOT's own API needs a key Jeff could not get).
               2. The backup airport stations ("backup_stations", Provo
                  KPVU then Salt Lake City KSLC by default), their latest
                  METAR through aviationweather.gov's data API (no key).
               3. Optionally one National Weather Service observation
                  station, named in the settings (off unless named).
             The line names the source it used and its age. A backup
             station's reading is amber, says "backup station" and how far
             away it is, and says why UTALP was not used ("UTALP (Iowa
             mesonet) stopped reporting at 3:40 AM, last reading 3 h 12
             min ago") or what UTALP reads. With no current reading at all
             it says NO CURRENT DATA and why, per source, and no number.
  FORECAST   the National Weather Service's hourly forecast for the exact
             spot (its gridpoint data, which carries gusts; the plain
             hourly forecast if that fails).
  ALERTS     the National Weather Service's active alerts for the spot:
             Wind Advisory, High Wind Warning, Red Flag Warning and the
             other wind and fire ones, as a banner line.

Advisory only. Nothing here is a go or no-go: the on-site wind check
decides, and the page says so.

What this module is not allowed to do
=====================================
Display only. This module never imports, calls or is called by anything
that arms, fires, disarms, schedules or plays: no flamesafe, no conductor,
no flame link, no schedule. The selftest reads its imports to prove it.

It never slows the show. Every fetch is on this module's own thread, one
at a time, with an 8 second limit on the whole answer (not only on each
socket read), and the page reads only what that thread last stored, under
a lock held for a copy. The rack screen asks for it on its own slow timer.

Age, stale and no data (the rules, Jeff, 2026-10-26 and 2026-10-28)
===================================================================
Every measured number is shown with its age, and its age is the reading's
own time (the station's), never when this PC fetched it, so a restart that
shows the saved values shows their true age.

  measured   each source on its own limits (station_limits): the 5 minute
             mesonet feed is current up to 20 min, STALE to 45 min; the
             hourly airports current up to 75 min, STALE to 120 min. Past
             its own grey limit, or never received, a reading is not
             usable and never shown. The line shows the highest usable
             reading, current or STALE (a STALE one amber and marked so,
             the current ones said beside it), else none: grey NO
             CURRENT DATA, use on-site wind check, with no number.
  forecast   the NWS issues the point forecast when its forecasters update
             the grid, routinely about twice a day and more often when the
             weather changes, so its issue time ("updateTime") is often
             several hours old while it is still the NWS's current
             forecast. So the forecast's age is mainly how long since this
             PC last GOT it from the NWS: over 1 h, amber STALE; over 6 h,
             grey. And as a check on the NWS itself: issued over 18 h ago
             (missed updates), amber STALE; over 36 h, grey. The saved
             forecast not reaching this hour, or its time over 15 min in
             the future (this PC's clock): grey. The line says which.
  disagree   two current readings (measured from any source, or the
             forecast) whose speeds differ by more than 10 mph: "Sources
             disagree, verify on site", and the measured line says what
             the others read.
  internet   every source's last try could not reach its server at all:
             "No internet", over whatever is saved.

All of those numbers can be changed in ltcplay_remote.json's "weather"
block (CONFIG_KEYS); nothing has to be there.

Polling: each measured source and the alerts every 5 min, the forecast
every 15 min. A failed try is tried again after 1, 2, 4 ... minutes, never
more often than that and never less often than the normal interval.

The self test the show PC can run: `ltc weather --check` (run_check). It
fetches each source once, prints what came back and says pass or fail.
"""
import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SETTINGS_FILE = "ltcplay_remote.json"     # remote.SETTINGS_FILE
CACHE_FILE = "ltcplay_weather_cache.json"
# The selftest sets this so no engine it starts ever reaches the internet.
OFFLINE_ENV = "LTCPLAY_WEATHER_OFFLINE"

KNOT_MPH = 1.150779448
KMH_MPH = 0.621371192
MS_MPH = 2.236936292

TIMEOUT_S = 8.0           # the whole answer, not only one socket read
MAX_BYTES = 4_000_000     # an answer bigger than this is refused
RETRY_FIRST_S = 60.0      # a failed try: again after 1, 2, 4 ... min
POINTS_EVERY_S = 24 * 3600.0
# The page greys the whole block when it has not heard from the engine for
# this long (remote.html, wxBlock).
PAGE_DEAD_S = 60

NWS = "https://api.weather.gov"
IEM = "https://mesonet.agron.iastate.edu"
AWC = "https://aviationweather.gov"
HOSTS = ("api.weather.gov", "mesonet.agron.iastate.edu",
         "aviationweather.gov")
# How far back aviationweather.gov is asked for a station's reports.
AWC_HOURS = 2
# Names for the airport stations near the venue, for the measured line.
AIRPORT_NAMES = {"KPVU": "Provo airport", "KSLC": "Salt Lake City airport",
                 "KU42": "South Valley airport, West Jordan",
                 "KSPK": "Spanish Fork airport"}
CONTACT_DEFAULT = "https://github.com/jholmes09/ltcplay"

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")

# The NWS alerts that matter to flames: every wind one (but Wind Chill) and
# the fire weather ones.
FIRE_EVENTS = ("red flag warning", "fire weather watch",
               "extreme fire danger", "dust storm warning",
               "blowing dust warning", "blowing dust advisory",
               "severe thunderstorm warning")

ADVISORY = ("Advisory only. The on-site wind check decides, not this "
            "screen.")
NO_DATA = "NO CURRENT DATA, use on-site wind check"

# The settings (ltcplay_remote.json, "weather"), their defaults and what
# each may be. Anything else in the block is refused with a sentence.
DEFAULTS = {
    "enabled": True,
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
    "disagree_mph": 10,
}
CONFIG_KEYS = tuple(DEFAULTS)
_RANGES = {   # key: (lowest, highest)
    "lat": (-90, 90), "lon": (-180, 180),
    "observation_every_min": (2, 60), "forecast_every_min": (5, 180),
    "stale_min": (5, 240), "no_data_min": (10, 720),
    "backup_stale_min": (5, 240), "backup_no_data_min": (10, 720),
    "forecast_stale_hours": (1, 48), "forecast_no_data_hours": (1, 72),
    "forecast_issued_stale_hours": (6, 72),
    "forecast_issued_no_data_hours": (12, 168),
    "disagree_mph": (1, 100),
}
MAX_BACKUPS = 4
_ID = re.compile(r"^[A-Z0-9_]{2,16}$")


# ------------------------------------------------------------ settings --

def check_config(block, where="ltcplay_remote.json, weather"):
    """The weather settings with every default filled in, or ValueError
    with one plain sentence. `block` is the "weather" object (or None)."""
    cfg = dict(DEFAULTS)
    cfg["backup_stations"] = list(DEFAULTS["backup_stations"])
    cfg["station_limits"] = {}
    if block is None:
        return cfg
    if not isinstance(block, dict):
        raise ValueError(f"{where} has to be a block in curly brackets, "
                         f'like {{"enabled": true}}.')
    for k in block:
        if k not in DEFAULTS:
            raise ValueError(f"{where}: {k!r} is not a weather setting. "
                             f"The ones it knows are: "
                             f"{', '.join(CONFIG_KEYS)}.")
    for k, v in block.items():
        d = DEFAULTS[k]
        if isinstance(d, bool):
            if v is not True and v is not False:
                raise ValueError(f"{where}: {k} has to be true or false.")
        elif isinstance(d, dict):
            v = _check_station_limits(v, f"{where}: {k}")
        elif isinstance(d, list):
            if not isinstance(v, list) or \
                    not all(isinstance(x, str) for x in v):
                raise ValueError(f"{where}: {k} has to be a list of station "
                                 f'names in quotes, like ["KPVU", "KSLC"], '
                                 f"or [] for none.")
            v = [x.strip().upper() for x in v]
            for x in v:
                if not _ID.match(x):
                    raise ValueError(f"{where}: {k} {x!r} is not a station "
                                     f"name (capital letters, numbers and "
                                     f"_ only, like KPVU).")
            if len(set(v)) != len(v):
                raise ValueError(f"{where}: {k} names a station twice.")
            if len(v) > MAX_BACKUPS:
                raise ValueError(f"{where}: {k} can name at most "
                                 f"{MAX_BACKUPS} stations.")
        elif isinstance(d, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                    not math.isfinite(v):
                raise ValueError(f"{where}: {k} has to be a number.")
            lo, hi = _RANGES[k]
            if not lo <= v <= hi:
                raise ValueError(f"{where}: {k} has to be from {lo} to "
                                 f"{hi}; it says {v}.")
        else:
            if not isinstance(v, str):
                raise ValueError(f"{where}: {k} has to be text in quotes.")
            v = v.strip()
            if k in ("mesonet_network", "mesonet_station", "nws_station"):
                v = v.upper()
                if v and not _ID.match(v):
                    raise ValueError(f"{where}: {k} {v!r} is not a station "
                                     f"name (capital letters, numbers and "
                                     f"_ only, like UTALP).")
                if not v and k != "nws_station":
                    raise ValueError(f"{where}: {k} cannot be empty.")
            elif len(v) > 120:
                raise ValueError(f"{where}: {k} is too long.")
        cfg[k] = v
    if cfg["no_data_min"] <= cfg["stale_min"]:
        raise ValueError(f"{where}: no_data_min ({cfg['no_data_min']}) has "
                         f"to be more than stale_min ({cfg['stale_min']}).")
    if cfg["backup_no_data_min"] <= cfg["backup_stale_min"]:
        raise ValueError(f"{where}: backup_no_data_min "
                         f"({cfg['backup_no_data_min']}) has to be more than "
                         f"backup_stale_min ({cfg['backup_stale_min']}).")
    if cfg["forecast_no_data_hours"] <= cfg["forecast_stale_hours"]:
        raise ValueError(f"{where}: forecast_no_data_hours has to be more "
                         f"than forecast_stale_hours.")
    if cfg["forecast_issued_no_data_hours"] <= \
            cfg["forecast_issued_stale_hours"]:
        raise ValueError(f"{where}: forecast_issued_no_data_hours has to be "
                         f"more than forecast_issued_stale_hours.")
    # A forecast fetched less often than its stale limit would go STALE
    # between two good fetches.
    if cfg["forecast_every_min"] >= cfg["forecast_stale_hours"] * 60:
        raise ValueError(f"{where}: forecast_every_min "
                         f"({cfg['forecast_every_min']}) has to be less than "
                         f"forecast_stale_hours ({cfg['forecast_stale_hours']}"
                         f" h, {cfg['forecast_stale_hours'] * 60:g} min), or "
                         f"a good forecast goes STALE between fetches.")
    for sid in cfg["station_limits"]:
        st, nd = station_limits(cfg, sid,
                                hourly=sid != cfg["mesonet_station"])
        if nd <= st:
            raise ValueError(f"{where}: station_limits {sid}: no_data_min "
                             f"({nd:g}) has to be more than stale_min "
                             f"({st:g}).")
    return cfg


def _check_station_limits(v, where):
    """{"KPVU": {"stale_min": 75, "no_data_min": 120}, ...} checked, its
    names in capitals, or ValueError."""
    if not isinstance(v, dict):
        raise ValueError(f'{where} has to be a block like {{"KPVU": '
                         f'{{"stale_min": 75, "no_data_min": 120}}}}.')
    out = {}
    for sid, lim in v.items():
        sid = str(sid).strip().upper()
        if not _ID.match(sid):
            raise ValueError(f"{where}: {sid!r} is not a station name.")
        if not isinstance(lim, dict) or not lim or \
                any(k not in ("stale_min", "no_data_min") for k in lim):
            raise ValueError(f"{where}: {sid} has to be a block with "
                             f"stale_min and or no_data_min, in minutes.")
        for k, x in lim.items():
            lo, hi = _RANGES[k]
            if isinstance(x, bool) or not isinstance(x, (int, float)) or \
                    not math.isfinite(x) or not lo <= x <= hi:
                raise ValueError(f"{where}: {sid} {k} has to be a number "
                                 f"from {lo} to {hi}.")
        out[sid] = dict(lim)
    return out


def station_limits(cfg, station, hourly=True):
    """(stale_min, no_data_min) for a measured station: its own entry in
    station_limits, else the 5 minute feed's (stale_min, no_data_min) for
    the mesonet station, the hourly airport's (backup_stale_min,
    backup_no_data_min) for the others."""
    if hourly:
        st, nd = cfg["backup_stale_min"], cfg["backup_no_data_min"]
    else:
        st, nd = cfg["stale_min"], cfg["no_data_min"]
    own = (cfg.get("station_limits") or {}).get(station) or {}
    return own.get("stale_min", st), own.get("no_data_min", nd)


def load_config(folder):
    """(settings, "") or (DEFAULTS copy, sentence why weather is off). No
    file, or no "weather" block in it: the defaults, on."""
    path = os.path.join(folder, SETTINGS_FILE)
    if not os.path.exists(path):
        return dict(DEFAULTS), ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        return dict(DEFAULTS), (f"{path} could not be read ({e}), so "
                                f"weather is off until it is fixed.")
    if not isinstance(doc, dict):
        return dict(DEFAULTS), (f"{path} is not a settings object, so "
                                f"weather is off until it is fixed.")
    try:
        return check_config(doc.get("weather")), ""
    except ValueError as e:
        return dict(DEFAULTS), f"{e} Weather is off until this is fixed."


# ------------------------------------------------------- small helpers --

def compass(deg):
    """16 point compass name for a direction in degrees, or ""."""
    if deg is None:
        return ""
    return COMPASS[int(math.floor((float(deg) % 360) / 22.5 + 0.5)) % 16]


def compass_deg(name):
    """Degrees for a 16 point compass name ("NNW" 337.5), or None."""
    n = str(name or "").strip().upper()
    return COMPASS.index(n) * 22.5 if n in COMPASS else None


def parse_time(text):
    """Epoch seconds from an ISO 8601 time ("2026-10-26T15:05:00Z",
    "...+00:00", "...-06:00"), or ValueError. A time with no zone is UTC."""
    t = str(text or "").strip()
    if not t:
        raise ValueError("no time given")
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    d = datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_interval(text):
    """(start, end) epoch seconds from an NWS validTime like
    "2026-10-26T15:00:00+00:00/PT2H" or ".../P1DT6H"."""
    a, _, dur = str(text or "").partition("/")
    m = _DUR.match(dur.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"not a time span: {text!r}")
    d, h, mi = (int(x or 0) for x in m.groups())
    start = parse_time(a)
    return start, start + d * 86400 + h * 3600 + mi * 60


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                return None
        else:
            return None
    return float(v) if math.isfinite(v) else None


def to_mph(value, unit):
    """`value` in `unit` (an NWS unitCode or uom) as mph, or None for no
    value. ValueError for a unit this does not know."""
    v = _num(value)
    if v is None:
        return None
    u = str(unit or "").split(":")[-1].lower()
    if u in ("km_h-1", "km/h", "kmh"):
        return v * KMH_MPH
    if u in ("m_s-1", "m/s"):
        return v * MS_MPH
    if u in ("kn", "knot", "kt", "knots"):
        return v * KNOT_MPH
    if u in ("mi_h-1", "mph", "mi/h"):
        return v
    raise ValueError(f"wind speed in a unit this does not know: {unit!r}")


# --------------------------------------------------------------- parsers --
# Each takes the decoded JSON and returns plain numbers, or raises
# ValueError with a sentence. Speeds in mph, directions in degrees the wind
# blows FROM, times in epoch seconds.

def parse_mesonet(doc, station):
    """The Iowa State mesonet's /api/1/currents.json: {"data": [{"station",
    "utc_valid", "sknt" (knots), "gust" (knots), "drct" (degrees)}]}
    (field names read from the service's own source, iemws currents.py).
    -> {"speed_mph", "gust_mph", "dir_deg", "valid"}."""
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise ValueError("the mesonet's answer has no data list")
    rows = [r for r in doc["data"] if isinstance(r, dict)]
    mine = [r for r in rows if str(r.get("station", "")).upper() == station]
    if not mine:
        raise ValueError(f"the mesonet has no recent reading for {station}")
    r = mine[0]
    try:
        valid = parse_time(r.get("utc_valid"))
    except ValueError as e:
        raise ValueError(f"the mesonet's reading for {station} has no "
                         f"usable time ({e})")
    speed = _num(r.get("sknt"))
    if speed is None:
        raise ValueError(f"the mesonet's reading for {station} has no wind "
                         f"speed")
    gust = _num(r.get("gust"))
    drct = _num(r.get("drct"))
    return _in_range({"speed_mph": speed * KNOT_MPH,
                      "gust_mph": None if gust is None else gust * KNOT_MPH,
                      "dir_deg": None if drct is None else drct % 360,
                      "valid": valid}, f"the mesonet's reading for {station}")


def parse_nws_observation(doc):
    """api.weather.gov /stations/{id}/observations/latest: properties
    windSpeed / windGust / windDirection as {"unitCode", "value"} and
    timestamp. -> {"speed_mph", "gust_mph", "dir_deg", "valid"}."""
    p = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(p, dict):
        raise ValueError("the NWS observation has no properties")
    try:
        valid = parse_time(p.get("timestamp"))
    except ValueError as e:
        raise ValueError(f"the NWS observation has no usable time ({e})")

    def qv(name):
        q = p.get(name)
        if not isinstance(q, dict):
            return None, None
        return q.get("value"), q.get("unitCode")
    v, u = qv("windSpeed")
    speed = to_mph(v, u)
    if speed is None:
        raise ValueError("the NWS observation has no wind speed")
    gv, gu = qv("windGust")
    dv, _du = qv("windDirection")
    d = _num(dv)
    return _in_range({"speed_mph": speed, "gust_mph": to_mph(gv, gu),
                      "dir_deg": None if d is None else d % 360,
                      "valid": valid}, "the NWS observation")


_METAR_WIND = re.compile(r"(?:^|\s)(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)"
                         r"(?=\s|$)")


def parse_awc(doc, station, lat=None, lon=None):
    """aviationweather.gov /api/data/metar?ids=...&format=json: a list of
    METARs, each {"icaoId", "obsTime" (epoch s), "reportTime", "wdir"
    (degrees, or "VRB"), "wspd" (knots), "wgst" (knots, missing or null
    with no gust), "rawOb", "lat", "lon"}. The newest one for `station`.
    A field it lacks is read from the raw METAR's wind group (like
    34012G20KT). -> {"speed_mph", "gust_mph", "dir_deg", "variable",
    "valid", "miles"}."""
    if doc is None or doc == []:
        raise ValueError(f"aviationweather.gov has no report from {station} "
                         f"in the last {AWC_HOURS} h")
    if not isinstance(doc, list):
        raise ValueError("aviationweather.gov's answer is not a list of "
                         "reports")
    best = None
    for r in doc:
        if not isinstance(r, dict) or \
                str(r.get("icaoId", "")).upper() != station:
            continue
        # Epoch seconds (2001 to 5138); anything else, milliseconds
        # included, is not trusted: the report time stands in.
        valid = _num(r.get("obsTime"))
        if valid is None or not 1e9 <= valid < 1e11:
            try:
                valid = parse_time(r.get("reportTime"))
            except (ValueError, TypeError):
                valid = None
        if valid is None:
            if best is None:
                best = (None, r)
            continue
        if best is None or best[0] is None or valid > best[0]:
            best = (valid, r)
    if best is None:
        raise ValueError(f"aviationweather.gov has no report from {station} "
                         f"in the last {AWC_HOURS} h")
    valid, r = best
    if valid is None:
        raise ValueError(f"aviationweather.gov's report from {station} has "
                         f"an unreadable time stamp")
    raw = str(r.get("rawOb") or "")
    m = _METAR_WIND.search(raw)
    wdir, wspd, wgst = r.get("wdir"), _num(r.get("wspd")), _num(r.get("wgst"))
    unit = "kn"
    if wspd is None and m:
        wdir, wspd = m.group(1), float(m.group(2))
        wgst = None if m.group(3) is None else float(m.group(3))
        unit = "m/s" if m.group(4) == "MPS" else "kn"
    elif "wgst" not in r and m and m.group(3) and m.group(4) == "KT":
        wgst = float(m.group(3))      # the field absent: the raw G25KT
    if wspd is None:
        raise ValueError(f"aviationweather.gov's report from {station} has "
                         f"no wind speed")
    variable = str(wdir).strip().upper() == "VRB"
    d = None if variable else _num(wdir)
    miles = None
    la, lo = _num(r.get("lat")), _num(r.get("lon"))
    if None not in (la, lo, lat, lon):
        miles = round(_distance_miles(lat, lon, la, lo), 1)
    return _in_range({"speed_mph": to_mph(wspd, unit),
                      "gust_mph": None if wgst is None else to_mph(wgst, unit),
                      "dir_deg": None if d is None else d % 360,
                      "variable": variable, "valid": valid, "miles": miles},
                     f"aviationweather.gov's report from {station}")


MAX_MPH = 200     # a wind speed past this, or below 0, is not a reading


def _in_range(out, who):
    """`out` (a parsed reading) when its speed and gust are 0 to 200 mph,
    else ValueError: no usable reading."""
    for k, what in (("speed_mph", "wind speed"), ("gust_mph", "gust")):
        v = out.get(k)
        if v is not None and not 0 <= v <= MAX_MPH:
            raise ValueError(f"{who} gave a {what} of {v:.0f} mph, which "
                             f"cannot be right")
    return out


def _series(layer, conv):
    """[[start, end, value], ...] from a gridpoint layer {"uom", "values":
    [{"validTime", "value"}]}, values passed through conv(value, uom)."""
    out = []
    if not isinstance(layer, dict) or not isinstance(layer.get("values"),
                                                     list):
        return out
    uom = layer.get("uom") or layer.get("unitCode")
    for e in layer["values"]:
        if not isinstance(e, dict):
            continue
        try:
            a, b = parse_interval(e.get("validTime"))
        except ValueError:
            continue
        v = conv(e.get("value"), uom)
        if v is not None:
            out.append([a, b, round(v, 2)])
    return out


def parse_grid(doc, now, keep_s=36 * 3600):
    """api.weather.gov /gridpoints/{wfo}/{x},{y} (the forecast's own data,
    which carries gusts): properties.updateTime and the windSpeed,
    windGust, windDirection layers. -> {"issued", "speed", "gust", "dir"},
    each layer a list of [start, end, value] reaching `keep_s` ahead."""
    p = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(p, dict):
        raise ValueError("the NWS forecast data has no properties")
    try:
        issued = parse_time(p.get("updateTime"))
    except ValueError as e:
        raise ValueError(f"the NWS forecast data has no issue time ({e})")

    def deg(v, _u):
        d = _num(v)
        return None if d is None else d % 360

    def cut(rows):
        return [r for r in rows if r[1] > now - 3600 and r[0] < now + keep_s]
    speed = cut(_series(p.get("windSpeed"), to_mph))
    if not speed:
        raise ValueError("the NWS forecast data has no wind speeds")
    return {"issued": issued, "speed": speed,
            "gust": cut(_series(p.get("windGust"), to_mph)),
            "dir": cut(_series(p.get("windDirection"), deg))}


_MPH_TEXT = re.compile(r"(\d+(?:\.\d+)?)")


def parse_hourly(doc, now, keep_s=36 * 3600):
    """api.weather.gov .../forecast/hourly: properties.periods with
    startTime, endTime, windSpeed ("10 mph", "5 to 10 mph", or a
    {"unitCode", "value"}), windDirection ("NNW") and maybe windGust.
    The same shape as parse_grid, for when the gridpoint data fails."""
    p = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(p, dict) or not isinstance(p.get("periods"), list):
        raise ValueError("the NWS hourly forecast has no periods")
    try:
        issued = parse_time(p.get("updateTime") or p.get("updated") or
                            p.get("generatedAt"))
    except ValueError as e:
        raise ValueError(f"the NWS hourly forecast has no issue time ({e})")
    units = str(p.get("units") or "us")

    def speed_of(v):
        if isinstance(v, dict):
            return to_mph(v.get("value") if v.get("value") is not None
                          else v.get("maxValue"), v.get("unitCode"))
        if isinstance(v, str):
            nums = [float(x) for x in _MPH_TEXT.findall(v)]
            if not nums:
                return None
            unit = "km/h" if "km" in v.lower() or units == "si" else "mph"
            return to_mph(max(nums), unit)
        return None
    speed, gust, dirs = [], [], []
    for per in p["periods"]:
        if not isinstance(per, dict):
            continue
        try:
            a, b = parse_time(per.get("startTime")), parse_time(
                per.get("endTime"))
        except ValueError:
            continue
        if b <= now - 3600 or a >= now + keep_s:
            continue
        s = speed_of(per.get("windSpeed"))
        if s is not None:
            speed.append([a, b, round(s, 2)])
        g = speed_of(per.get("windGust"))
        if g is not None:
            gust.append([a, b, round(g, 2)])
        d = compass_deg(per.get("windDirection"))
        if d is not None:
            dirs.append([a, b, d])
    if not speed:
        raise ValueError("the NWS hourly forecast has no wind speeds")
    return {"issued": issued, "speed": speed, "gust": gust, "dir": dirs}


def wind_event(name):
    n = str(name or "").strip().lower()
    return ("wind" in n and "chill" not in n) or n in FIRE_EVENTS


def parse_alerts(doc):
    """api.weather.gov /alerts/active?point=lat,lon: features[].properties
    {event, headline, ends, expires, status, messageType}. -> the wind and
    fire ones in effect: [{"event", "headline", "ends"}], one per event."""
    if not isinstance(doc, dict) or not isinstance(doc.get("features"),
                                                    list):
        raise ValueError("the NWS alerts answer has no features list")
    out, seen = [], set()
    for f in doc["features"]:
        p = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(p, dict) or not wind_event(p.get("event")):
            continue
        if str(p.get("status") or "Actual") != "Actual" or \
                str(p.get("messageType") or "") == "Cancel":
            continue
        ev = str(p.get("event")).strip()
        if ev.lower() in seen:
            continue
        seen.add(ev.lower())
        ends = None
        for k in ("ends", "expires"):
            try:
                ends = parse_time(p.get(k))
                break
            except (ValueError, TypeError):
                continue
        out.append({"event": ev, "headline": str(p.get("headline") or ""),
                    "ends": ends})
    return out


def parse_points(doc):
    """api.weather.gov /points/{lat},{lon} -> the URLs for this spot."""
    p = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(p, dict):
        raise ValueError("the NWS point lookup has no properties")
    out = {}
    for k in ("forecastGridData", "forecastHourly", "observationStations"):
        v = p.get(k)
        if isinstance(v, str) and v.startswith(NWS + "/"):
            out[k] = v
    if "forecastGridData" not in out and "forecastHourly" not in out:
        raise ValueError("the NWS point lookup names no forecast for this "
                         "spot")
    return out


def at(series, now):
    """The value of [[start, end, value], ...] covering `now`, or None."""
    for a, b, v in series or ():
        if a <= now < b:
            return v
    return None


# ----------------------------------------------------------- fetching --

class FetchError(Exception):
    """A fetch that failed. `offline` is True when the server could not
    be reached at all (no internet, no name lookup, no answer)."""

    def __init__(self, text, offline=False):
        super().__init__(text)
        self.offline = offline


def user_agent(contact=""):
    return (f"LTCPlay-ShowControl/1.0 (wind readout; "
            f"{contact or CONTACT_DEFAULT})")


def http_get(url, headers, timeout=TIMEOUT_S, clock=time.monotonic):
    """(status, body bytes). The whole answer within `timeout` seconds
    (give or take the one read under way, itself limited to `timeout`)."""
    deadline = clock() + timeout
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = getattr(r, "status", 200)
            # read1: what has arrived, so a slow drip meets the deadline.
            read = getattr(r, "read1", r.read)
            chunks, n = [], 0
            while True:
                if clock() > deadline:
                    raise FetchError(f"no full answer in {timeout:g} s",
                                     offline=True)
                c = read(65536)
                if not c:
                    break
                n += len(c)
                if n > MAX_BYTES:
                    raise FetchError("the answer was too large")
                chunks.append(c)
            return status, b"".join(chunks)
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        raise FetchError(f"the server answered HTTP {e.code}")
    except urllib.error.URLError as e:
        why = e.reason
        if isinstance(why, (socket.timeout, TimeoutError)):
            raise FetchError(f"no answer in {timeout:g} s", offline=True)
        if isinstance(why, socket.gaierror):
            raise FetchError("the name could not be looked up (no "
                             "internet, or DNS is blocked)", offline=True)
        if "CERTIFICATE" in str(why).upper():
            raise FetchError(f"the secure connection was refused: {why}")
        raise FetchError(f"could not connect: {why}", offline=True)
    except (socket.timeout, TimeoutError):
        raise FetchError(f"no answer in {timeout:g} s", offline=True)
    except (ConnectionError, OSError) as e:
        raise FetchError(f"could not connect: {e}", offline=True)


def get_json(get, url, contact="", timeout=TIMEOUT_S, empty_ok=False):
    """Decoded JSON from `url` through `get` (http_get's shape). With
    `empty_ok`, "204 No Content" or an empty body is None (aviationweather.gov
    answers that way when it has no report)."""
    hdr = {"User-Agent": user_agent(contact),
           "Accept": "application/geo+json, application/json"}
    status, body = get(url, hdr, timeout)
    if empty_ok and (status == 204 or (status == 200 and not body.strip())):
        return None
    if status != 200:
        raise FetchError(f"the server answered HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise FetchError(f"the answer was not readable JSON ({e})")


def mesonet_url(cfg):
    return (f"{IEM}/api/1/currents.json?network={cfg['mesonet_network']}"
            f"&station={cfg['mesonet_station']}")


def awc_url(stations):
    """One request for every backup airport (a list, or one id)."""
    ids = stations if isinstance(stations, str) else ",".join(stations)
    return (f"{AWC}/api/data/metar?ids={ids}&format=json"
            f"&hours={AWC_HOURS}")


def points_url(cfg):
    return f"{NWS}/points/{cfg['lat']:.4f},{cfg['lon']:.4f}"


def alerts_url(cfg):
    return f"{NWS}/alerts/active?point={cfg['lat']:.4f},{cfg['lon']:.4f}"


def nws_obs_url(cfg):
    return f"{NWS}/stations/{cfg['nws_station']}/observations/latest"


# ------------------------------------------------------------- service --

class Weather:
    """The fetching thread, the saved values and the view the page draws.
    `get` is http_get's shape (tests pass canned answers), `wall` the
    epoch clock the ages are read on, `clock` the monotonic one the polls
    are timed on."""

    def __init__(self, folder, cfg=None, config_error="", get=None,
                 wall=time.time, clock=time.monotonic, log=print):
        self.folder = folder
        self.cfg = dict(cfg or DEFAULTS)
        self.config_error = config_error
        self.get = get or http_get
        self.wall = wall
        self.clock = clock
        self.log = log
        self.cache_path = os.path.join(folder, CACHE_FILE) if folder else ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.offline_for_tests = bool(os.environ.get(OFFLINE_ENV))
        obs = self.cfg["observation_every_min"] * 60.0
        fc = self.cfg["forecast_every_min"] * 60.0
        # name: every_s. The order is the order they are tried: the
        # measured sources in their order (measured_sources), then the
        # alerts and the forecast. The backup airports are one request
        # ("awc"), whose answer is shared out to each "awc:<id>".
        names = [n for n, _l, _s in measured_sources(self.cfg)]
        self.every = {}
        for n in names:
            self.every["awc" if n.startswith("awc:") else n] = obs
        self.every.update(alerts=obs, forecast=fc)
        # name -> {"data", "error", "offline", "answered", "fails", "due",
        # "tried"}; data is what the view reads. "answered": the last try
        # reached the server and got an answer, which had no usable reading.
        self.src = {n: {"data": None, "error": "", "offline": False,
                        "answered": False, "fails": 0, "due": 0.0,
                        "tried": False}
                    for n in list(self.every) + names}
        self.points = None           # parse_points + "got_at", "key"
        self._cache_said = False
        self._load_cache()

    # -- identity of what a saved value is for -------------------------
    def _key(self, name):
        c = self.cfg
        if name == "mesonet":
            return f"{c['mesonet_network']}/{c['mesonet_station']}"
        if name == "nws_obs":
            return f"nws/{c['nws_station']}"
        if name.startswith("awc:"):
            return f"awc/{name[4:]}"
        return f"{c['lat']:.4f},{c['lon']:.4f}"

    # -- the disk cache ---------------------------------------------------
    def _load_cache(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            self.log(f"Weather: the saved values could not be read ({e}); "
                     f"starting without them.")
            return
        if not isinstance(doc, dict) or doc.get("version") != 1:
            return
        for name, s in self.src.items():
            ent = (doc.get("sources") or {}).get(name)
            if isinstance(ent, dict) and ent.get("key") == self._key(name) \
                    and self._sane(name, ent.get("data")):
                s["data"] = ent["data"]
        pt = doc.get("points")
        if isinstance(pt, dict) and pt.get("key") == self._key("points") \
                and isinstance(pt.get("got_at"), (int, float)):
            try:
                parse_points({"properties": pt})
                self.points = pt
            except ValueError:
                pass

    @staticmethod
    def _sane(name, d):
        if not isinstance(d, dict):
            return False
        if name in ("mesonet", "nws_obs") or name.startswith("awc:"):
            return isinstance(d.get("valid"), (int, float)) and \
                isinstance(d.get("speed_mph"), (int, float))
        if name == "forecast":
            return isinstance(d.get("issued"), (int, float)) and \
                isinstance(d.get("speed"), list)
        if name == "alerts":
            return isinstance(d.get("checked"), (int, float)) and \
                isinstance(d.get("alerts"), list)
        return False

    def _save_cache(self):
        if not self.cache_path:
            return
        with self._lock:
            doc = {"version": 1, "saved_at": self.wall(),
                   "sources": {n: {"key": self._key(n), "data": s["data"]}
                               for n, s in self.src.items()
                               if s["data"] is not None},
                   "points": self.points}
        tmp = f"{self.cache_path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            if not self._cache_said:
                self._cache_said = True
                self.log(f"Weather: the values could not be saved to "
                         f"{self.cache_path} ({e}); a restart will start "
                         f"without them.")

    # -- one source -------------------------------------------------------
    def _json(self, url):
        return get_json(self.get, url, self.cfg["contact"])

    def _points(self):
        now = self.wall()
        p = self.points
        if p and now - p["got_at"] < POINTS_EVERY_S:
            return p
        try:
            fresh = parse_points(self._json(points_url(self.cfg)))
        except (FetchError, ValueError):
            if p:                    # an old lookup still names the grid
                return p
            raise
        fresh.update(got_at=now, key=self._key("points"))
        self.points = fresh
        return fresh

    def fetch(self, name):
        """One source's new data, or FetchError / ValueError."""
        cfg = self.cfg
        if name == "mesonet":
            return parse_mesonet(self._json(mesonet_url(cfg)),
                                 cfg["mesonet_station"])
        if name == "nws_obs":
            return parse_nws_observation(self._json(nws_obs_url(cfg)))
        if name == "awc":
            # Every backup airport in one request: {id: reading, or the
            # sentence why there is none}.
            ids = cfg["backup_stations"]
            doc = get_json(self.get, awc_url(ids), cfg["contact"],
                           empty_ok=True)
            out = {}
            for sid in ids:
                try:
                    out[sid] = parse_awc(doc, sid, cfg["lat"], cfg["lon"])
                except ValueError as e:
                    out[sid] = str(e)
            return out
        if name == "alerts":
            return {"checked": self.wall(),
                    "alerts": parse_alerts(self._json(alerts_url(cfg)))}
        if name == "forecast":
            pts = self._points()
            first = None
            for k, parse in (("forecastGridData", parse_grid),
                             ("forecastHourly", parse_hourly)):
                if k not in pts:
                    continue
                try:
                    out = parse(self._json(pts[k]), self.wall())
                    out["from"] = "hourly" if k == "forecastHourly" else "grid"
                    # When this PC last got it: what its staleness is
                    # mainly judged on (judge, the forecast).
                    out["fetched_at"] = self.wall()
                    return out
                except (FetchError, ValueError) as e:
                    first = first or e
            raise first or ValueError("no forecast for this spot")
        raise ValueError(f"no such source {name}")

    def poll_once(self):
        """Try every source that is due. Returns the names tried."""
        tried = []
        for name in list(self.every):
            if self._stop.is_set():
                break
            s = self.src[name]
            if self.clock() < s["due"]:
                continue
            tried.append(name)
            try:
                if self.offline_for_tests:
                    raise FetchError("weather fetching is off for the "
                                     "self test", offline=True)
                data = self.fetch(name)
                err, offline, answered = "", False, True
            except FetchError as e:
                data, err, offline, answered = None, str(e), e.offline, False
            except (ValueError, KeyError, TypeError) as e:
                data, err, offline, answered = (None, str(e) or
                                                type(e).__name__, False, True)
            except Exception as e:                   # noqa: BLE001
                data, err, offline, answered = (None, f"{type(e).__name__}: "
                                                f"{e}", False, False)
            with self._lock:
                s["tried"] = True
                if name == "awc":
                    self._share_awc(data, err, offline, answered)
                    if data is not None:
                        data = {"checked": self.wall()}
                if data is not None:
                    s.update(data=data, error="", offline=False,
                             answered=True, fails=0,
                             due=self.clock() + self.every[name])
                else:
                    s["fails"] += 1
                    s.update(error=err, offline=offline, answered=answered,
                             due=self.clock() + self.retry_s(name,
                                                             s["fails"]))
            if data is not None:
                self._save_cache()
        return tried

    def _share_awc(self, got, err, offline, answered):
        """The one aviationweather.gov answer, shared out to each backup
        airport's own entry (under the lock)."""
        for sid in self.cfg["backup_stations"]:
            t = self.src[f"awc:{sid}"]
            t["tried"] = True
            one = None if got is None else got.get(sid)
            if isinstance(one, dict):
                t.update(data=one, error="", offline=False, answered=True)
            elif got is not None:
                t.update(error=str(one or "no answer for this station"),
                         offline=False, answered=True)
            else:
                t.update(error=err, offline=offline, answered=answered)

    def retry_s(self, name, fails):
        """After `fails` failures in a row: 1, 2, 4 ... min, up to the
        normal interval."""
        return min(self.every[name], RETRY_FIRST_S * 2 ** max(0, fails - 1))

    def next_due_in(self):
        with self._lock:
            due = min(self.src[n]["due"] for n in self.every)
        return max(0.0, due - self.clock())

    # -- the thread -------------------------------------------------------
    def start(self):
        if not self.cfg["enabled"] or self.config_error or \
                self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-weather")
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:                   # noqa: BLE001
                self.log(f"Weather: {type(e).__name__}: {e}")
            self._stop.wait(min(30.0, max(1.0, self.next_due_in())))

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=TIMEOUT_S + 2)

    # -- what the page draws ---------------------------------------------
    def view(self):
        with self._lock:
            snap = {n: dict(s) for n, s in self.src.items()}
        return judge(snap, self.cfg, self.wall(), self.config_error,
                     self.offline_for_tests)


# --------------------------------------------------------------- rules --

def age_text(s):
    """"just now", "6 min", "1 h 20 min", "2 days"."""
    s = max(0, int(s))
    m = s // 60
    if m < 1:
        return "under 1 min"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} days"


def wind_text(speed, gust, deg, variable=False):
    """"12 mph, gusts 21 mph, from NNW (338°)"."""
    sp = int(round(speed))
    out = f"{sp} mph"
    if gust is not None:
        out += f", gusts {int(round(gust))} mph"
    else:
        out += ", gusts not reported"
    if sp == 0 and (deg is None or deg == 0):
        return out + ", calm"
    if deg is None:
        return out + (", direction variable" if variable else
                      ", direction not reported")
    return out + f", from {compass(deg)} ({int(round(deg)) % 360}°)"


def _clock_text(epoch):
    d = datetime.fromtimestamp(epoch).astimezone()
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def measured_sources(cfg):
    """[(name, label, short)] of the measured sources, in the order they
    are used: the mesonet station, the backup airport stations
    (aviationweather.gov), then the NWS station if one is named."""
    out = [("mesonet", f"{cfg['mesonet_name']} ({cfg['mesonet_station']})",
            f"{cfg['mesonet_station']} (Iowa mesonet)")]
    for sid in cfg.get("backup_stations") or ():
        nm = AIRPORT_NAMES.get(sid)
        out.append((f"awc:{sid}", f"{nm} ({sid})" if nm else sid,
                    f"{sid} (aviationweather.gov)"))
    if cfg.get("nws_station"):
        sid = cfg["nws_station"]
        out.append(("nws_obs", f"NWS station {sid}",
                    f"{sid} (api.weather.gov)"))
    return out


def measured_state(d, now, stale_s, dead_s):
    """(state, age) of one measured source's saved reading `d`: "ok"
    (current), "stale", "none" (over the no data limit, or no reading:
    never shown) or "future" (timed over 15 min ahead: this PC's clock)."""
    if d is None:
        return "none", None
    age = now - d["valid"]
    if age < -15 * 60:
        return "future", age
    if age > dead_s:
        return "none", age
    return ("stale" if age > stale_s else "ok"), max(0.0, age)


def _bare(text):
    return str(text or "").strip().rstrip(".")


def _cap(text):
    return text[:1].upper() + text[1:]


def _why_not(r, now, cfg):
    """Why measured source row `r` is not current, in plain words, or ""
    when it is."""
    short, s, d, st, age = r["short"], r["s"], r["d"], r["st"], r["age"]
    err = _bare(s.get("error")) if s.get("tried") else ""
    if st == "ok":
        return ""
    if st == "future":
        return (f"the {short} reading is timed in the future; check this "
                f"PC's clock")
    if st == "stale":
        return (f"the last {short} reading is {age_text(age)} old, over its "
                f"{r['stale_min']:g} min limit")
    at_ = ""
    if d is not None:
        at_ = (f" at {_clock_text(d['valid'])}" if age < 20 * 3600 else "")
    if err and not s.get("answered"):
        txt = (f"{short} did not answer: {err}" if s.get("offline") else
               f"{short} could not be read: {err}")
        if d is not None:
            txt += f"; last reading{at_}, {age_text(age)} ago"
        return txt
    if d is not None:
        txt = (f"{short} stopped reporting{at_}, last reading "
               f"{age_text(age)} ago")
        return txt + (f" ({err})" if err else "")
    if err:
        return f"{short} answered with no usable reading: {err}"
    return f"{short} not read yet"


def judge(src, cfg, now, config_error="", offline_for_tests=False):
    """The Weather block, as the page draws it: {"enabled", "state",
    "where", "lines": [{"kind", "level", "text"}], "banner": [...],
    "disagree", "internet", "advisory", "error"}. Every number in a line
    carries its age. `level` is "ok", "stale" (amber, STALE), "backup"
    (amber: a current reading from a backup station) or "none" (grey)."""
    out = {"enabled": bool(cfg.get("enabled", True)) and not config_error,
           "where": (f"Thanksgiving Point area ({cfg['lat']:.4f}, "
                     f"{cfg['lon']:.4f})"),
           "lines": [], "banner": [], "disagree": "", "internet": "",
           "advisory": ADVISORY, "error": config_error,
           "page_dead_s": PAGE_DEAD_S}
    if config_error:
        out["state"] = "error"
        return out
    if not cfg.get("enabled", True):
        out["state"] = "off"
        return out
    fstale_s = cfg["forecast_stale_hours"] * 3600
    fdead_s = cfg["forecast_no_data_hours"] * 3600
    fissued_s = cfg["forecast_issued_stale_hours"] * 3600
    fidead_s = cfg["forecast_issued_no_data_hours"] * 3600
    current = []                 # (who, speed) of every reading not grey

    def tail(s):
        if s.get("error") and s.get("tried"):
            return f" The last try failed: {_bare(s['error'])}."
        return ""

    # -- measured: the highest usable reading, any source -----------------
    # Conservative (review of PR #154): the line leads with the HIGHEST
    # reading inside its own source's grey limit, current or STALE; a
    # STALE one is amber and marked so, with the current ones said beside
    # it. Each source has its own limits: the 5 minute mesonet feed 20 / 45
    # min, the hourly airports 75 / 120 min (station_limits). A reading
    # past its grey limit is never a candidate.
    rows = []
    for i, (name, label, short) in enumerate(measured_sources(cfg)):
        s = src.get(name) or {}
        d = s.get("data")
        sid = short.split(" ")[0]
        st_min, nd_min = station_limits(cfg, sid, hourly=name != "mesonet")
        st, age = measured_state(d, now, st_min * 60, nd_min * 60)
        rows.append({"i": i, "name": name, "label": label, "short": short,
                     "who": sid, "s": s, "d": d, "st": st, "age": age,
                     "stale_min": st_min, "no_data_min": nd_min})
    for r in rows:
        r["why"] = _why_not(r, now, cfg)
    usable = [r for r in rows if r["st"] in ("ok", "stale")]
    pick = max(usable, key=lambda r: (r["d"]["speed_mph"], -r["i"])) \
        if usable else None
    line = {"kind": "measured",
            "sources": [{"source": r["name"], "level": r["st"],
                         "text": r["why"] or "current"} for r in rows]}
    if pick is None:
        line.update(source="", level="none",
                    text=f"Measured wind: {NO_DATA}. "
                         + ". ".join(_cap(r["why"]) for r in rows) + ".")
    else:
        d, age = pick["d"], pick["age"]
        lab = pick["label"]
        if pick["i"] > 0:
            miles = d.get("miles")
            lab = (f"backup station {lab}"
                   + (f", {miles:.0f} mi away" if miles else ""))
        said = wind_text(d["speed_mph"], d["gust_mph"], d["dir_deg"],
                         d.get("variable", False))
        text = (f"Measured, {lab}: {said}. "
                f"{age_text(age)} old.{tail(pick['s'])}")
        ahead = [r for r in rows[:pick["i"]] if r not in usable]
        if ahead:
            text += (" Using the backup because "
                     + "; ".join(r["why"] for r in ahead) + ".")
        # Said beside it: UTALP when a backup leads, any reading over the
        # disagree limit away, and every current one when a STALE one
        # leads.
        others = [r for r in usable if r is not pick and
                  (r["i"] < pick["i"] or
                   (pick["st"] == "stale" and r["st"] == "ok") or
                   abs(r["d"]["speed_mph"] - d["speed_mph"]) >
                   cfg["disagree_mph"])]
        # The page puts STALE before a stale line; beside other readings
        # the line also says which one is STALE and why.
        if pick["st"] == "stale" and others:
            text += (f" STALE: over its {pick['stale_min']:g} min limit.")
        if others:
            text += (" " + "; ".join(
                _cap(f"{r['short']} reads "
                     f"{int(round(r['d']['speed_mph']))} mph, "
                     f"{age_text(r['age'])} old, "
                     + (f"STALE (over its {r['stale_min']:g} min limit)"
                        if r["st"] == "stale" else "current"))
                for r in others)
                + ". This line shows the highest reading.")
        level = ("stale" if pick["st"] == "stale" else
                 "backup" if pick["i"] > 0 else "ok")
        line.update(source=pick["name"], level=level, text=text,
                    age_s=int(age), speed_mph=round(d["speed_mph"], 1))
        # The line's own reading, and any other measured one that differs
        # from it by more than the limit (the banner below says it too).
        for r in [pick] + others:
            if r is pick or abs(r["d"]["speed_mph"] - d["speed_mph"]) > \
                    cfg["disagree_mph"]:
                current.append((f"measured {r['who']}",
                                r["d"]["speed_mph"]))
    out["lines"].append(line)

    # -- the forecast -----------------------------------------------------
    # Judged mainly on when this PC last GOT it (fetched_at): the NWS issue
    # time is often hours old while it is still the current forecast. Its
    # issue time is a check on the NWS itself: over 18 h amber, over 36 h
    # grey (a stuck grid served as new for days).
    s = src.get("forecast") or {}
    d = s.get("data")
    label = "Forecast for this spot (NWS)"
    line = {"kind": "forecast", "source": "forecast"}
    sp = None if d is None else at(d["speed"], now)
    issued_age = got_age = None
    future = False
    if d is not None:
        got = d.get("fetched_at")
        got = d["issued"] if not isinstance(got, (int, float)) else got
        future = got - now > 15 * 60 or d["issued"] - now > 15 * 60
        issued_age = max(0.0, now - d["issued"])
        got_age = max(0.0, now - got)
    if d is None or sp is None or future or got_age > fdead_s or \
            issued_age > fidead_s:
        why = ""
        if future:
            why = " Its time is in the future; check this PC's clock."
        elif d is not None and sp is None:
            why = " The saved forecast does not reach this hour."
        elif d is not None and got_age > fdead_s:
            why = (f" This PC last got it from the NWS "
                   f"{age_text(got_age)} ago, over the "
                   f"{cfg['forecast_no_data_hours']:g} h limit. Issued "
                   f"{age_text(issued_age)} ago.")
        elif d is not None:
            why = (f" The NWS has not updated it for "
                   f"{age_text(issued_age)}, over the "
                   f"{cfg['forecast_issued_no_data_hours']:g} h limit.")
        line.update(level="none", text=f"{label}: {NO_DATA}.{why}"
                    f"{'' if s.get('tried') or d else ' Not read yet.'}"
                    f"{tail(s)}")
    else:
        stale = []
        if got_age > fstale_s:
            stale.append(f"this PC has not got it from the NWS for "
                         f"{age_text(got_age)} (the limit is "
                         f"{cfg['forecast_stale_hours']:g} h)")
        if issued_age > fissued_s:
            stale.append(f"the NWS has not updated it for "
                         f"{age_text(issued_age)} (it normally does at "
                         f"least twice a day; the limit is "
                         f"{cfg['forecast_issued_stale_hours']:g} h)")
        line.update(level="stale" if stale else "ok",
                    text=f"{label}: {wind_text(sp, at(d['gust'], now), at(d['dir'], now))}. "
                         f"Issued {age_text(issued_age)} ago; this PC got "
                         f"it {age_text(got_age)} ago."
                         + (" Stale because " + " and ".join(stale) + "."
                            if stale else "") + tail(s),
                    age_s=int(got_age), issued_age_s=int(issued_age),
                    speed_mph=round(sp, 1))
        current.append(("forecast", sp))
    out["lines"].append(line)

    # Sources disagree.
    if len(current) >= 2:
        hi = max(current, key=lambda c: c[1])
        lo = min(current, key=lambda c: c[1])
        if hi[1] - lo[1] > cfg["disagree_mph"]:
            out["disagree"] = (
                "Sources disagree, verify on site: "
                + ", ".join(f"{n} {int(round(v))} mph" for n, v in current)
                + ".")

    # The NWS alerts.
    s = src.get("alerts") or {}
    d = s.get("data")
    age = None if d is None else now - d["checked"]
    if d is None or age > cfg["no_data_min"] * 60:
        out["alerts_line"] = {"level": "none", "text":
                              "NWS wind and fire alerts: not checked "
                              "recently." + tail(s)}
    else:
        live = [a for a in d["alerts"]
                if a.get("ends") is None or a["ends"] > now]
        for a in live:
            until = (f" until {_clock_text(a['ends'])}"
                     if a.get("ends") else "")
            out["banner"].append(f"NWS {a['event'].upper()} in effect"
                                 f"{until}. Checked {age_text(age)} ago.")
        out["alerts_line"] = {
            "level": "ok", "text":
            (f"NWS wind and fire alerts for this spot: "
             f"{'see above' if live else 'none'}. Checked "
             f"{age_text(max(0, age))} ago.")}

    # No internet: every source's last try could not reach its server.
    tried = [x for x in src.values() if x.get("tried")]
    if tried and all(x.get("error") and x.get("offline") for x in tried):
        out["internet"] = ("No internet: this PC cannot reach the weather "
                           "services (" + ", ".join(HOSTS) + ")."
                           + (" Weather fetching is off for the self test."
                              if offline_for_tests else
                              " Anything below is the last saved reading, "
                              "with its true age."))
    levels = [ln["level"] for ln in out["lines"]]
    out["state"] = ("none" if all(lv == "none" for lv in levels) else
                    "stale" if any(lv != "ok" for lv in levels) else "ok")
    return out


def hosts_text():
    """"api.weather.gov, mesonet.agron.iastate.edu and aviationweather.gov"."""
    return ", ".join(HOSTS[:-1]) + " and " + HOSTS[-1]


def view_when_missing():
    """The view for an engine without a weather service."""
    return {"enabled": False, "state": "off", "lines": [], "banner": [],
            "disagree": "", "internet": "", "advisory": ADVISORY,
            "error": "", "where": "", "page_dead_s": PAGE_DEAD_S,
            "text": "Weather is not running in this engine."}


def has_block(folder):
    """True when ltcplay_remote.json in `folder` has a "weather" key (or
    cannot be read, so its block cannot be ruled out)."""
    path = os.path.join(folder, SETTINGS_FILE)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return True
    return not isinstance(doc, dict) or "weather" in doc


def start_for(folder, default_on=True, log=print):
    """The engine's weather service (web.serve), or None. Settings from
    ltcplay_remote.json in `folder`, saved values from the same folder,
    its thread started unless switched off or the settings are wrong.
    `default_on` False (an engine that is not the Thanksgiving Point
    show's, like GPL at Dollywood): None unless the file has a weather
    block, so no other venue is ever shown Lehi's wind."""
    if not default_on and not has_block(folder):
        return None
    cfg, err = load_config(folder)
    if err:
        log(f"Weather: {err}")
    return Weather(folder, cfg, err, log=log).start()


# ---------------------------------------------------- the self check --

def _distance_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * \
        math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


AIRPORTS = ("KPVU", "KSLC")    # Provo and Salt Lake airports: not this spot


def nearby_stations(doc, lat, lon, n=8):
    """[(miles, id, name)] nearest first, from /gridpoints/.../stations."""
    out = []
    for f in (doc or {}).get("features") or ():
        try:
            p = f["properties"]
            lo, la = f["geometry"]["coordinates"][:2]
            out.append((_distance_miles(lat, lon, la, lo),
                        str(p["stationIdentifier"]), str(p.get("name", ""))))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return sorted(out)[:n]


def run_check(folder, get=None, out=print, raw_chars=1200, wall=time.time):
    """`ltc weather --check`: fetch every source once, print what came
    back and a plain pass or fail for each. Returns 0 when all passed."""
    get = get or http_get
    cfg, err = load_config(folder)
    out("Weather self check")
    out(f"Settings: {os.path.join(folder, SETTINGS_FILE)}")
    if err:
        out(f"FAIL  settings: {err}")
        return 1
    out(f"Spot: {cfg['lat']}, {cfg['lon']}. Measured, in the order used: "
        + ", ".join(short for _n, _l, short in measured_sources(cfg)) + ".")
    if not cfg["enabled"]:
        out("Note: weather is switched off in the settings (enabled: "
            "false); checking anyway.")
    hdr = {"User-Agent": user_agent(cfg["contact"]),
           "Accept": "application/geo+json, application/json"}
    results = []
    now = wall()

    def fetch(title, url):
        out("")
        out(f"== {title}")
        out(f"GET {url}")
        try:
            status, body = get(url, hdr, TIMEOUT_S)
        except FetchError as e:
            out(f"  could not fetch: {e}"
                + ("  (this PC cannot reach it: internet or firewall)"
                   if e.offline else ""))
            return None
        text = body.decode("utf-8", "replace")
        out(f"  HTTP {status}, {len(body)} bytes. The start of it:")
        out("  " + text[:raw_chars].replace("\n", "\n  "))
        if status != 200:
            return None
        try:
            return json.loads(text)
        except ValueError as e:
            out(f"  not readable JSON: {e}")
            return None

    def verdict(title, fn):
        try:
            said = fn()
            out(f"PASS  {title}: {said}")
            results.append(True)
        except (ValueError, KeyError, TypeError) as e:
            out(f"FAIL  {title}: {e}")
            results.append(False)

    def need(doc):
        if doc is None:
            raise ValueError("nothing usable came back (see above)")
        return doc

    def obs_said(d):
        said = wind_text(d["speed_mph"], d["gust_mph"], d["dir_deg"],
                         d.get("variable", False))
        return (f"{said}, {age_text(now - d['valid'])} old"
                + (f", {d['miles']:.0f} miles away" if d.get("miles")
                   else ""))

    doc = fetch(f"Measured: mesonet {cfg['mesonet_station']}",
                mesonet_url(cfg))
    verdict(f"measured, mesonet {cfg['mesonet_station']}",
            lambda: obs_said(parse_mesonet(need(doc),
                                           cfg["mesonet_station"])))
    for sid in cfg["backup_stations"]:
        doc = fetch(f"Measured, backup: {sid} (aviationweather.gov)",
                    awc_url(sid))
        verdict(f"measured, backup {sid} (aviationweather.gov)",
                lambda doc=doc, sid=sid: obs_said(parse_awc(
                    need(doc), sid, cfg["lat"], cfg["lon"])))
    if cfg["nws_station"]:
        doc = fetch(f"Measured: NWS station {cfg['nws_station']}",
                    nws_obs_url(cfg))
        verdict(f"measured, NWS station {cfg['nws_station']}",
                lambda: obs_said(parse_nws_observation(need(doc))))
    doc = fetch("NWS point lookup", points_url(cfg))
    pts = None
    try:
        pts = parse_points(need(doc))
    except ValueError as e:
        out(f"FAIL  NWS point lookup: {e}")
        results.append(False)
    if pts:
        out(f"PASS  NWS point lookup: grid {pts.get('forecastGridData')}")
        results.append(True)
        for key, parse, title in (
                ("forecastGridData", parse_grid, "forecast, gridpoint data"),
                ("forecastHourly", parse_hourly, "forecast, hourly")):
            if key not in pts:
                continue
            doc = fetch(f"NWS {title}", pts[key])

            def said(doc=doc, parse=parse):
                f = parse(need(doc), now)
                sp = at(f["speed"], now)
                if sp is None:
                    raise ValueError("no forecast for this hour")
                return (f"{wind_text(sp, at(f['gust'], now), at(f['dir'], now))}"
                        f", issued {age_text(now - f['issued'])} ago")
            verdict(f"NWS {title}", said)
        if "observationStations" in pts:
            doc = fetch("NWS observation stations near the spot "
                        "(for choosing nws_station; not pass or fail)",
                        pts["observationStations"])
            for miles, sid, name in nearby_stations(doc, cfg["lat"],
                                                    cfg["lon"]):
                note = "  (airport, not this spot)" if sid in AIRPORTS \
                    else ""
                out(f"  {sid:8s} {miles:5.1f} miles  {name}{note}")
    doc = fetch("NWS active alerts for the spot", alerts_url(cfg))

    def alerts_said():
        a = parse_alerts(need(doc))
        return ("wind or fire alerts in effect: "
                + ", ".join(x["event"] for x in a)) if a else \
            "read; no wind or fire alerts in effect"
    verdict("NWS alerts", alerts_said)
    out("")
    if all(results):
        out(f"RESULT: PASS. All {len(results)} checks passed.")
        return 0
    out(f"RESULT: FAIL. {results.count(False)} of {len(results)} checks "
        f"failed. If every one says it could not connect, this PC has no "
        f"internet or the firewall blocks it: allow outbound HTTPS (TCP "
        f"443) to {hosts_text()}.")
    return 1
