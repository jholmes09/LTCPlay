"""The show-network remote: the few show controls an iPad (or the rack
screen) may press, over web.py's server. No sign-in (Jeff, 2026-10-07).

What this is, and what it is not
================================

The engine and the show live in the `ltc serve` process. The page is only a
window onto them: closing it, an iPad that sleeps or loses the Wi-Fi, a
connection that drops, none of those is an event this module acts on. There
is no timer here that does anything to the show when a page goes quiet, and
the selftest proves a dropped page changes nothing.

Who may press what
------------------
- No sign-in, no PIN, no operator names (Jeff, 2026-10-07). What keeps the
  page to the show is the network: the engine listens only on the one
  listed show-network address and on 127.0.0.1 (never 0.0.0.0), and
  refuses anything that looks proxied, cross-site or misaddressed.
- The journal names the surface a press came from, never a person: "Rack
  screen" for the machine itself, "iPad at <its address>" for a device on
  the show network (screen "iPad"). Taken from the request's own address,
  never from the request body.
- The Stream Deck keeps Jeff's 2026-10-01 rule and never comes through here.

The controls
------------
Start now, Hold, Resume and Abort are the scheduler's own operator events
(schedule_service.Service.operator_press: the same schedule.step every
press goes through, journaled with who and which screen, the Abort latch
saved before the conductor is asked). Reset is Service.reset_conductor.
"Disarm all flame groups" is a PLAIN disarm (Jeff, 2026-10-23): every
group's disarm handed to the Stream Deck process, exactly as a group's own
Disarm is, so no fresh arm cycle is forced; only when that cannot be
trusted to land (the deck not reading this engine, or flamesafe not
confirming every group off within PLAIN_CONFIRM_S) does the show's own
flames_disarm_all go (the flame link's disarm_all, the Abort's, which
forces one). Journaled with the surface and which screen.

ARMING (Jeff, 2026-10-03, its own PR): the page never arms anything. A
hold to arm (arm-hold, repeated every 100 ms while the
finger is down) is read by the Stream Deck process (deck-input) as a remote
press of that group's key. The deck owns flamesafe's arm link and runs its
own rules (the hold, the refractory window, the latched refusal) and
flamesafe runs all of its own (consent, dwell, the post-Abort window, the
round-4 veto, the second-copy guard) exactly as for a finger on the deck.
It needs a listed address (above), a page status and a
flamesafe status no older than ARM_FRESH_S, heartbeats the engine actually
received for SCREEN_HOLD_S, and screen_arming on in ltcplay_remote.json
(OFF unless the file says true; the Programming screen on the show machine
itself turns it on and off, route screen-arming). A gap in the heartbeats,
an Abort, a disarm or a sign out lets the hold go; an interrupted hold
never carries on.

Every refusal is said on the page in a sentence that names the reason
(2026-10-22, Jeff: "I'm supposed to be able to enable flames via the
screen"; a hold that went nowhere used to look like a working one). The
engine refuses, and says why, what the Stream Deck would refuse anyway:
screen arming off, an Abort standing, the Stream Deck program not reading
this engine, this device in Arrange mode on the Morning page, every machine
in the group locked out. What only the deck knows (its re-arm wait after a
disarm, its own latch) it reports back on its next deck-input read
(`refused`), and the hold's next heartbeat carries the sentence. None of
this loosens anything: each one is a refusal of a press the deck or
flamesafe would refuse anyway, said out loud.


Fresh state
-----------
Start now, Resume and Reset act on what the page
shows, so they are refused when the page's last status is more than
FRESH_S old (the page sends the `served_at` of the status it is showing).
Hold, Abort and disarm are never refused for that: they only take risk
away, and a press that reduces risk must never wait on a stale page.

The network
-----------
The engine listens on ONE show-network address (ltcplay_remote.json,
`show_network_address`, set from the page on the show machine) on the fixed
port FIXED_PORT, plus loopback for the machine itself. Anything that looks
proxied (a Forwarded, X-Forwarded-For, Via or similar header) is refused,
from anywhere, loopback included: a proxy or tunnel on the show machine
would make the whole world look like the machine itself. NEVER put remote
access software, a tunnel or a port forward on the show machine.

On the Windows show PC (2026-10-28) the supervisor starts the engine with
--network-if-saved: the saved address when there is one (a saved address
this PC does not have falls back to this PC only, said on the card), else
this PC only. The installer's one firewall rule (FIREWALL_RULE) lets in
port 7878 for ltcplay.exe from this PC's own subnet only. The Show network
card (network_view) says what is saved, where the engine listens, whether
that rule is there (read with netsh, never changed) and the address to
type. A request by another name for this PC gets a short page naming that
address (web.wrong_host_page); the Host check is not loosened.
"""
import contextlib
import hmac
import ipaddress
import itertools
import json
import math
import os
import re
import socket
import threading
import time

from . import appdata
from . import settings as settings_mod
from . import users as users_mod
from . import version as version_mod
from .morning import (TIMED_DEFAULT_S, TIMED_MAX_DEFAULT_S,
                      TIMED_MAX_LIMIT_S, TIMED_MIN_S, TIMED_PRESETS_S)
from .fuelpump import (DEFAULT_S as PUMP_DEFAULT_S,
                       MAX_DEFAULT_S as PUMP_MAX_DEFAULT_S,
                       MAX_LIMIT_S as PUMP_MAX_LIMIT_S, MIN_S as PUMP_MIN_S)
from .playhead import ShowAudioPlayhead

FIXED_PORT = 7878
DISK_FILE = "disk-low.txt"          # packaging/windows/housekeeping.DISK_FILE
DISK_READ_S = 30.0
SETTINGS_FILE = "ltcplay_remote.json"
# How old the page's status may be for a press that acts on what it shows.
FRESH_S = 2.0
# How old flamesafe's last status may be before the lamps read "stale"
# (CONTRACT.md: no status frame for 1 s is red for the safety program).
FLAME_STALE_S = 1.0

# The routes this module answers, and nothing else. No "arm" among them.
CONTROL_ROUTES = ("start-now", "hold", "resume", "abort", "reset",
                  "disarm-all", "stop")
# Stop from a screen (Jeff, 2026-10-28; review of #144, F7): the Stream
# Deck's complete halt (Remote.halt), then playback stops. A live show's
# fade is let run this long at most first (web.HALT_FADE_WAIT_S).
STOP_FADE_WAIT_S = 3.0
# Programming-session transport (Jeff, 2026-10-03): play from a timecode,
# jump, pause and continue, an A/B loop, back to following timecode. Only
# in a programming session (a show started from the page as Rehearse, or in
# Rehearsal mode, or a rehearsal conductor), and never while a scheduled
# show is live; see programming(). While the show's flame cues follow the
# transport (programming_flames_follow), a press that moves it needs the
# show machine or the Settings permission: programming_move_refusal.
TRANSPORT_ROUTES = ("play-from", "jump", "pause", "continue", "mark-a",
                    "mark-b", "loop", "follow")
FRESH_ROUTES = frozenset(("start-now", "resume", "reset")
                         + TRANSPORT_ROUTES)
JUMP_MAX_S = 600.0
CONFIRM_ROUTES = frozenset(("start-now", "abort"))
# Arming from a screen (Jeff, 2026-10-03; its own PR and safety review).
# The page never arms anything itself: a hold here is a remote press of the
# group's key on the Stream Deck process, which owns flamesafe's arm link
# and runs every one of its own and flamesafe's rules on it. See
# flamesafe/CONTRACT.md, "Arming from a screen".
ARM_ROUTES = ("arm-hold", "arm-release", "group-disarm")
ARM_FRESH_S = 1.0      # the page's status, and flamesafe's, no older
# Disarm all flame groups is a plain disarm through the Stream Deck (Jeff,
# 2026-10-23): it goes that way only while the deck read this engine within
# The Lasers lamp's tooltip while it reads lit (review of PR #97, P3): what
# the BEYOND watch can and cannot tell.
BEYOND_LAMP_TITLE = ("Lit is what this program last sent. BEYOND never "
                     "answers, and the watch only sees that BEYOND's program "
                     "is running on this PC: a hung BEYOND, or one whose OSC "
                     "input is off, still reads as running.")
# The Video lamp's tooltip (Jeff, 2026-10-26): what the MadMapper watch can
# and cannot tell.
MADMAPPER_LAMP_TITLE = ("MadMapper is running; whether the screens show the "
                        "picture is not something it can tell. MadMapper "
                        "never answers, and the watch only sees that its "
                        "program is running on this PC.")
# The same, while it is not seen running (or cannot be told).
MADMAPPER_LINK_TITLE = ("This is a link check, never a picture check: "
                        "MadMapper never answers, so whether the screens "
                        "show the picture is not something it can tell.")
# PLAIN_DECK_FRESH_S, and flamesafe must report every group off within
# PLAIN_CONFIRM_S, else the flame link's disarm_all (the Abort's) goes.
PLAIN_DECK_FRESH_S = 0.5
PLAIN_CONFIRM_S = 1.0
# A screen's per-group Disarm (2026-10-29, Jeff's "armed in 2.0 but
# disarmed in 1.0"): the same rule as Disarm all, answered only once it is
# known. Confirmed only by a fresh flamesafe status received at least
# GROUP_DISARM_SETTLE_S after the Stream Deck read the disarm (its next
# main-loop pass and flamesafe's next tick both fit in that), looked at
# every GROUP_DISARM_STEP_S for up to PLAIN_CONFIRM_S.
GROUP_DISARM_SETTLE_S = 0.15
GROUP_DISARM_STEP_S = 0.02
BEAT_STALE_S = 0.25    # a hold with no heartbeat for this long is let go
SCREEN_HOLD_S = 1.0    # beat-evidenced hold before the deck may fire
# The Morning and Shutdown page (Jeff, 2026-10-17; morning.py, MORNING.md).
# None of these touches the arm link: the purge selection is published on
# deck-input for the Stream Deck process to act on, the fire heartbeat sets
# the flame link's purge frame, the lockout writes the file flamesafe reads.
MORNING_GET_ROUTES = ("morning", "checklist-pdf", "checklist-export")
MORNING_POST_ROUTES = ("morning-open", "purge-select", "purge-fire",
                       "purge-timed",
                       "purge-stop", "purge-clear", "laser-test",
                       "open-beyond",
                       "laser-arm", "lock",
                       "layout", "checklist-tick", "checklist-note",
                       "checklist-verify", "checklist-tally-retry")
# The Fuel page (fuel.py, FUEL.md): read by anyone on the show network; a
# refill needs a signed-in operator or admin, an undo an admin (fuel.py).
FUEL_GET_ROUTES = ("fuel",)
FUEL_POST_ROUTES = ("fuel-refill", "fuel-undo")
# The Fireball page (fireball.py): read by anyone; a channel ON needs the
# arm row (users.classify), everything off never.
FIREBALL_GET_ROUTES = ("fireball",)
FIREBALL_POST_ROUTES = ("fireball-beat", "fireball-off")
# Wave flamer cleaning (cleaning.py, the Shutdown page): read by anyone; a
# beat that runs it is an arm press (users.classify), Stop never.
CLEANING_GET_ROUTES = ("cleaning",)
CLEANING_POST_ROUTES = ("cleaning-beat", "cleaning-stop")
# The hazers (hazers.py, Jeff 2026-10-29): read by anyone in the status;
# arming a hazer and setting its levels are arm presses (users.classify)
# and from the show network need screen_arming; a disarm and Hazers off
# never.
HAZER_POST_ROUTES = ("hazer", "hazers-off", "hazer-level")
# The fireball's fuel pump (fuelpump.py, Jeff 2026-10-30): read by anyone in
# the status; {"on": true} is an arm press (users.classify) and from the
# show network needs screen_arming; anything else turns it off, never
# refused.
PUMP_POST_ROUTES = ("fuel-pump",)
GET_ROUTES = ("whoami", "status", "network", "deck-input",
              "announce-outputs") + \
    MORNING_GET_ROUTES + FUEL_GET_ROUTES + FIREBALL_GET_ROUTES + \
    CLEANING_GET_ROUTES
# "alert" (alerts.py, 2026-10-12): raise and clear only from this machine
# (the Stream Deck process, over loopback); Acknowledge from any page.
POST_ROUTES = CONTROL_ROUTES + TRANSPORT_ROUTES + ARM_ROUTES + (
    "network", "screen-arming", "alert", "live-flame",
    "announce-output", "announce-test-tone") + \
    MORNING_POST_ROUTES + FUEL_POST_ROUTES + FIREBALL_POST_ROUTES + \
    CLEANING_POST_ROUTES + HAZER_POST_ROUTES + PUMP_POST_ROUTES
# Live flame programming (flameprog.py, LIVE_FLAME_PROGRAMMING.md): ON only
# on the show machine itself (and the role matrix's Settings, users.py), with
# a confirm; OFF from any surface, signed in or not: it only takes risk away.
LIVE_FLAME_ENABLES = (
    "On, LTC Player listens for xLights on this PC only (E1.31 to "
    "127.0.0.1), or also from the one Mac named in its settings, and passes the flame controller's channels to flamesafe "
    "through its own flame link. flamesafe still fires only a group that "
    "is armed (hold to arm on the Stream Deck), and still applies lockouts "
    "and Abort. One flame group may be armed at a time. It turns itself "
    "off, and disarms every flame group, on Off, Abort, Hold, Start now, "
    "Run, a show starting, a second sender, no xLights data for a while, "
    "or the session limit. Never use xLights' test tools while a group is "
    "armed.")
# The switch "Fire flames while scrubbing" is gone (Jeff, 2026-10-29: "as
# long as I am armed, then jumping around timecode will work"). On a show
# that plays from the show audio, Programming playback is the show's own
# path, so its flame cues go to flamesafe whenever it plays; flamesafe
# fires only armed groups. The Programming screen says so in one line.
PROGRAMMING_FLAMES_LINE = ("Flames follow the show while you play here. "
                           "Only armed groups fire. Hold, Stop or Abort "
                           "ends it.")
# A page from before the change may still post the switch's route: it is
# answered, never acted on.
REMOVED_ROUTES = {
    "scrub-flames": ("Fire flames while scrubbing is not needed any more, so "
                     "it was removed. Flames follow the show while you play "
                     "on the Programming screen, and only armed groups "
                     "fire. To stop flames press Hold, Stop or Abort, or "
                     "disarm on the Stream Deck. Reload this page. Nothing "
                     "was changed."),
}
# screen-arming (2026-10-22): the switch for arming from a screen, set on
# the show machine itself, never from the network. announce-output (Jeff's
# field report, 2026-10-28, item 10): the announcement output picker,
# Programming, Announcement output; Settings permission (users.py).
LOCAL_ONLY = frozenset(("network", "screen-arming", "announce-output",
                        "announce-test-tone", "preroll-check"))
# Interface 2.0 (Jeff, 2026-10-28): which page /remote serves a screen that
# has not chosen one with its own ltcplay_ui cookie (web.py). 1 is the page
# every screen had (remote.html), 2 is remote2.html. Read and set on the
# show machine itself only (never from the network), the role matrix's
# Settings to set it (users.py). A preference, never a safety setting: a
# value that is not 1 or 2 reads as 1 and never breaks the settings file.
UI_DEFAULT_ROUTE = "ui-default"
UI_VERSIONS = (1, 2)
# Operator announcements (announce.py; Jeff, 2026-10-26): the rack screen's
# three buttons and its Stop, and the Stream Deck's page 2 keys, which post
# here from this machine. One press plays (no double tap). From the show
# machine only: a public announcement is not an iPad press. Between shows
# a Play Holds the schedule first; during a show it plays over the show and
# Holds, pauses and stops nothing. Stop ends the announcement's audio only.
ANNOUNCE_ROUTES = ("announce-play", "announce-stop")
# How long the rack screen's announcement block is reused before the
# service is asked again (it asks the audio system for the device): the
# page polls four times a second. A press always asks at once.
ANNOUNCE_VIEW_S = 1.0
# A snapshot older than this is not shown as fact (announce_view).
ANNOUNCE_STALE_S = 4.0
# What the Stream Deck reports back about a screen hold it would not take
# (deck-input's `refused`, streamdeck.ScreenKeys): its code, and what the
# page says. The deck's own rules decide; the engine only passes it on.
DECK_REFUSALS = {
    "neverpurged": ("The Stream Deck refused it: {g} is never purged, so a "
                    "purge click never arms it."),
    "screenonly": ("The Stream Deck refused it: {g} has no key on the deck, "
                   "so a purge click never arms it. Hold its Hold to arm "
                   "button instead."),
    "refractory": ("The Stream Deck refused it: {g} was disarmed less than "
                   "2 s ago, and it waits that long before it takes a new "
                   "arm. Lift your finger, wait a moment, then hold again."),
    "latched": ("The Stream Deck refused it: an Abort is standing. Press "
                "Reset, then hold again."),
    "operator": ("The Stream Deck refused it: no operator is chosen for it "
                 "(Programming, Stream Deck operator, on the show machine)."),
    "role": ("The Stream Deck refused it: the role matrix does not let the "
             "person signed in on the show machine arm."),
    "key": ("The Stream Deck refused it: {g}'s own key on the deck is being "
            "pressed. Let go of one of them."),
}
DECK_REFUSED_MAX = 16
# How long a hold whose group became asked for waits for the deck to say it
# armed it from this hold (deck-input `taken`) before it ends uncredited.
TAKEN_WAIT_S = 0.5
# Everything the screen_arming switch lets screens do (review of PR #90,
# P2-1). The page's card and confirm box say the same words (selftest).
SCREEN_ARMING_ENABLES = (
    "On, this rack screen and every device on the show network can hold "
    "to arm a flame group (through the Stream Deck, with all of its rules "
    "and flamesafe's) and disarm one flame group. Devices on the show "
    "network can also select and fire a purge, unlock a machine, turn a "
    "laser test on and arm a laser (this rack screen does those without this switch).")
# What the page says when screen arming is switched off (the default).
ARMING_OFF = ("Arming from a screen is switched off on this show PC. To turn "
              "it on: on the show machine itself, Programming, Arming from "
              "a screen, Turn on. Until then arm from the Stream Deck.")
# Users and the audit log (users.py, Jeff, 2026-10-20). Sign in and out
# from any page; the Users screen's changes from the show machine only.
USER_GET_ROUTES = ("users",)
USER_POST_ROUTES = ("signin", "signout", "my-password", "users",
                    "role-matrix", "clear-lockouts", "idle-signout",
                    "last-show-signout",
                    "audit-settings", "audit-upload-now", "audit-verify")

# Any of these on a request means something forwarded it. A browser on the
# show network never sends them; a reverse proxy, tunnel or CDN does.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host",
                 "x-forwarded-proto", "x-forwarded-port",
                 "x-forwarded-server", "x-real-ip", "via",
                 "x-original-forwarded-for", "cf-connecting-ip",
                 "true-client-ip", "x-client-ip", "x-cluster-client-ip",
                 "fastly-client-ip", "client-ip", "x-proxyuser-ip",
                 "x-original-url", "x-rewrite-url", "cf-ray",
                 "x-amzn-trace-id", "ngrok-trace-id")

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")

# First run and setup (Jeff, 2026-10-17: a fresh install was "just a
# shell", with no screen to make a schedule or choose the show folder).
# This machine only, never the network: they read and write files beside
# the settings. None of them touches the rig: the show folder change asks
# the Windows supervisor to restart the engine, which it refuses during a
# show; the schedule is refused while a show is live.
SETUP_GET_ROUTES = ("setup", "schedule-file")
SETUP_POST_ROUTES = ("schedule-file", "open-folder", "show-folder", "screen",
                     "show-select", "show-file-new", "reload-engine")
PROGRAMS_FILE = "programs.json"      # packaging/windows/supervisor.py writes
FIRST_RUN_FILE = "first-run.json"    # what the supervisor wrote at first run
SHOWPC_FILE = "showpc.json"          # the supervisor's own settings
SCREEN_CLOSE_FILE = "close-rack-screen"   # the supervisor closes its Edge
RESTART_ENGINE_FILE = "restart-engine"    # the supervisor restarts ltcplay
PROGRAMS_READ_S = 3.0
FLAMESAFE_ENV = "LTCPLAY_FLAMESAFE_CONFIG"     # cli.py and ltcwin.py agree


def settings_folder():
    """The same place as this machine's other settings."""
    return appdata.folder() if appdata.WINDOWS else settings_mod.folder()


# ------------------------------------------------- the show folder served --
# Soak 2026-10-21: showpc.json named soak_show, and the engine served
# Documents\LTC Shows. The supervisor gives the engine showpc.json's
# show_folder as its --folder, so that is the folder meant; it read the
# file only when it started, and adopted an engine already running, so a
# hand edit did not reach the engine. The supervisor now follows the file;
# the rack screen says which folder is served, and says it loudly when
# showpc.json names another.

def _norm_folder(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(
        os.path.expanduser(str(p)))))


def _rate_words(fps, drop):
    fps = float(fps)
    if round(fps) == 30:
        return f"{fps:g} fps {'drop-frame' if drop else 'non-drop'}"
    return f"{fps:g} fps{' drop-frame' if drop else ''}"


# The timecode is "arriving" while its last frame is at most this old:
# the decoder keeps its last rate reading after the feed stops, and a rate
# is never said for a feed that is not there (review of #140).
RATE_ARRIVING_S = 1.0


def rate_mismatch(cs):
    """A sentence when the timecode coming in (the engine's own reading,
    session.snapshot's rate_in, rate_drop and rate_confident, the same one
    display.warnings_for judges) does not match the running show file's
    fps and drop, else "". Only while timecode is arriving (ltc_age under
    RATE_ARRIVING_S). The frame count and the drop bit are read off every
    frame; a rate (29.97 against 30, which count alike) is said only once
    it is measured (rate_confident), and the show file's own rate is never
    said as the incoming one. Display only. Never raises."""
    try:
        rate = cs.get("rate_in")
        fps = cs.get("timeline_fps")
        age = cs.get("ltc_age")
        arriving = age is not None and float(age) <= RATE_ARRIVING_S
        if not cs.get("running") or rate is None or fps is None or \
                not arriving:
            return ""
        rate, fps = float(rate), float(fps)
        drop = bool(cs.get("rate_drop"))
        tdrop = bool(cs.get("timeline_drop"))
        sure = bool(cs.get("rate_confident"))
        count_off = round(rate) != round(fps)
        speed_off = sure and abs(rate - fps) > 0.01
        drop_off = drop != tdrop
        if not (count_off or speed_off or drop_off):
            return ""
        dword = "drop-frame" if drop else "non-drop"
        if count_off or speed_off:
            if sure:
                incoming = f"runs at {_rate_words(rate, drop)}"
            else:
                incoming = (f"counts {round(rate)} frames a second, "
                            f"{dword}")
            keys = '"fps" and "drop"' if drop_off else '"fps"'
        else:
            incoming = f"is {dword}"
            keys = '"drop"'
        return (f"The timecode coming in {incoming}; the show file says "
                f"{_rate_words(fps, tdrop)}. Cues drift until {keys} in "
                f"the show file match the timecode.")
    except Exception:                            # noqa: BLE001
        return ""


def show_folder_mismatch(served, wanted):
    """The sentence when showpc.json's show_folder (`wanted`) is not the
    folder this engine serves (`served`, its --folder); "" when they are
    the same or showpc.json names none."""
    if not isinstance(wanted, str) or not wanted.strip():
        return ""
    wanted = wanted.strip()
    if served and _norm_folder(served) == _norm_folder(wanted):
        return ""
    return (f"showpc.json names the show folder {wanted}, but this engine "
            f"is serving {served or 'no folder'}: the show files listed "
            f"here, and the show that runs, come from "
            f"{served or 'nowhere'}. LTC Player restarts the engine with "
            f"the folder in showpc.json by itself once no show is running "
            f"(press Stop on the Programming screen). If this stays, set "
            f"the folder there (Show folder, Use this folder), or Stop LTC "
            f"Player and start it again.")


# ------------------------------------------------------ the PC's clock --
# Soak 2026-10-21: the show PC's clock was 2.7 s behind with the Windows
# Time service stopped. Shows start by this clock.
CLOCK_LOUD_S = 0.5
TIME_SERVICE_READ_S = 60.0
CHECKS_READ_S = 15.0
_TIME_FIX = ("Start it: press Windows+R, type services.msc, open Windows "
             "Time, set Startup type to Automatic and press Start. Then "
             "Settings, Time and language, Date and time, Sync now.")


def windows_time_service(run=None, windows=None):
    """"running", "stopped", or "" when it cannot be told (not Windows,
    no answer). From `sc query w32time`'s state number (4 running, 1
    stopped, 2 and 3 on the way), which no Windows language changes."""
    windows = appdata.WINDOWS if windows is None else windows
    if not windows:
        return ""
    if run is None:
        import subprocess
        run = subprocess.run
    try:
        r = run(["sc", "query", "w32time"], capture_output=True, text=True,
                timeout=5, creationflags=0x08000000)
    except Exception:                            # noqa: BLE001
        return ""
    m = re.search(r"STATE\s*:\s*(\d+)", str(getattr(r, "stdout", "") or ""))
    if not m:
        return ""
    return "running" if m.group(1) in ("2", "4") else "stopped"


def judge_pc_clock(offset_s, service):
    """(level, sentence) for the Morning checks: `offset_s` is the
    scheduler's last time server check (positive: this PC is behind; None
    not known), `service` windows_time_service(). "bad" (loud) when the
    clock is off by more than CLOCK_LOUD_S and the time service is
    stopped: nothing will correct it."""
    known = isinstance(offset_s, (int, float)) and \
        not isinstance(offset_s, bool) and math.isfinite(offset_s)
    size = abs(offset_s) if known else None
    way = ("behind" if known and offset_s > 0 else "ahead of")
    off = known and size > CLOCK_LOUD_S
    if service == "stopped":
        if off:
            return ("bad", f"This PC's clock is {size:.1f} s {way} the time "
                           f"server and the Windows Time service is "
                           f"STOPPED, so nothing will correct it. Shows "
                           f"start by this clock. {_TIME_FIX}")
        if known:
            return ("warn", f"The Windows Time service is stopped. The clock "
                            f"is within {size:.2f} s of the time server now, "
                            f"but it drifts without it (2.7 s behind in the "
                            f"soak of 2026-10-21). {_TIME_FIX}")
        return ("warn", f"The Windows Time service is stopped, and this PC's "
                        f"clock could not be checked against a time server. "
                        f"{_TIME_FIX}")
    if off:
        tail = (" The Windows Time service is running; Settings, Time and "
                "language, Date and time, Sync now puts it right at once."
                if service == "running" else "")
        return ("warn", f"This PC's clock is {size:.1f} s {way} the time "
                        f"server. Shows start by this clock.{tail}")
    if not known:
        return ("unknown", "This PC's clock could not be checked against a "
                           "time server" + (", though the Windows Time "
                           "service is running." if service == "running"
                           else "."))
    return ("ok", f"This PC's clock is within {size:.2f} s of the time "
                  f"server" + (", and the Windows Time service is running."
                               if service == "running" else "."))


# Each write has a temp file of its own: two threads of this process
# writing the same file never share one (review of PR #180: two saves at
# once could interleave in "<path>.<pid>.tmp").
_TMP_SEQ = itertools.count(1)


def _write_json(path, doc, private=False):
    tmp = (f"{path}.{os.getpid()}.{threading.get_ident()}."
           f"{next(_TMP_SEQ)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# Held by Remote.save_schedule_file from the check to the answer.
_SCHEDULE_SAVE_LOCK = threading.Lock()


# --------------------------------------------------------- the network --

# A plain IP address and nothing else: digits, hex letters, dots and
# colons. No IPv6 zone ID (the "%..." suffix ipaddress accepts with any
# characters in it), no spaces, quotes or semicolons (review of #164, B2).
PLAIN_IP = re.compile(r"[0-9A-Fa-f.:]+")


def check_show_address(text):
    """The show-network address as written, or ValueError with a sentence.
    One real address of this machine: never every interface, never
    loopback, never multicast, never a zone ID or anything but the plain
    address."""
    text = str(text or "").strip()
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an address. Write this machine's "
                         f"address on the show Wi-Fi, like 10.20.0.5.")
    if not PLAIN_IP.fullmatch(text) or getattr(ip, "scope_id", None):
        raise ValueError(f"{text!r} is not a plain address. Write only this "
                         f"machine's address on the show Wi-Fi, like "
                         f"10.20.0.5.")
    if ip.is_unspecified:
        raise ValueError(f"{text} means every network this machine is on, "
                         f"the venue's and the internet's included. Use its "
                         f"address on the show Wi-Fi only.")
    if ip.is_loopback:
        raise ValueError(f"{text} is the machine itself; an iPad cannot "
                         f"reach it. Use the address on the show Wi-Fi.")
    if ip.is_multicast or ip.is_link_local and ip.version == 6:
        raise ValueError(f"{text} cannot be served on. Use the address on "
                         f"the show Wi-Fi.")
    return str(ip)


def load_settings(folder=None):
    """{"show_network_address": str or None, "flamesafe_config": str or
    None}. A missing file is all None; a broken one raises ValueError."""
    path = os.path.join(folder or settings_folder(), SETTINGS_FILE)
    # screen_arming is OFF unless the file says true (review of PR #43,
    # P0-4): arming from a screen came in from PR #41, whose own safety
    # review decides whether it is switched on.
    # purge_arming (Jeff, 2026-10-17, the Morning and Shutdown page) is ON
    # unless the file says false: a click on that page arms a machine for
    # a purge, from the Rack screen itself; from a device on the show
    # network it also needs screen_arming (MORNING.md).
    # purge_timed_max_s (Jeff, 2026-10-29, the timed purge): the longest
    # timed purge this show machine allows, 30 s unless an admin sets it in
    # this file, never more than morning.TIMED_MAX_LIMIT_S; purge_timed_s
    # the duration last started here, the page's default.
    # pump_max_s (Jeff, 2026-10-30, the fuel pump): the longest run time,
    # 60 minutes unless an admin sets it, never more than
    # fuelpump.MAX_LIMIT_S. The default run is always 5 minutes.
    out = {"show_network_address": None, "flamesafe_config": None,
           "screen_arming": False, "purge_arming": True, "booth_alert": None,
           "alert_pixels": None, "ui_default": 1,
           "purge_timed_max_s": TIMED_MAX_DEFAULT_S,
           "purge_timed_s": TIMED_DEFAULT_S,
           "pump_max_s": PUMP_MAX_DEFAULT_S,
           "path": path}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} could not be read: {e}")
    return settings_from_doc(doc, path, out)


def settings_from_doc(doc, path, out=None):
    """load_settings's checks on a parsed document: the settings, or
    ValueError naming the file and what is wrong. save_settings runs the
    same checks on what it is about to write, before writing it."""
    if out is None:
        out = {"show_network_address": None, "flamesafe_config": None,
               "screen_arming": False, "purge_arming": True,
               "booth_alert": None, "alert_pixels": None,
               "ui_default": 1, "purge_timed_max_s": TIMED_MAX_DEFAULT_S,
               "purge_timed_s": TIMED_DEFAULT_S,
               "pump_max_s": PUMP_MAX_DEFAULT_S,
               "path": path}
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a settings object.")
    if doc.get("show_network_address"):
        out["show_network_address"] = check_show_address(
            doc["show_network_address"])
    if doc.get("flamesafe_config"):
        out["flamesafe_config"] = str(doc["flamesafe_config"])
    if "screen_arming" in doc:
        # One switch for screen and browser arming, off by default. Only
        # the JSON word true turns it on; anything else that is not false
        # is refused rather than guessed at.
        v = doc["screen_arming"]
        if v is not True and v is not False:
            raise ValueError(f"{path}: screen_arming must be true or false.")
        out["screen_arming"] = v
    if "purge_arming" in doc:
        v = doc["purge_arming"]
        if v is not True and v is not False:
            raise ValueError(f"{path}: purge_arming must be true or false.")
        out["purge_arming"] = v
    for k, lo in (("purge_timed_max_s", TIMED_MIN_S),
                  ("purge_timed_s", TIMED_MIN_S)):
        if k not in doc:
            continue
        v = doc[k]
        if isinstance(v, bool) or not isinstance(v, int) or \
                not lo <= v <= TIMED_MAX_LIMIT_S:
            # Never unlimited, never a guess: a wrong value is refused.
            raise ValueError(f"{path}: {k} must be a whole number of "
                             f"seconds from {lo} to {TIMED_MAX_LIMIT_S}.")
        out[k] = v
    for k in ("pump_max_s",):
        if k not in doc:
            continue
        v = doc[k]
        if isinstance(v, bool) or not isinstance(v, int) or \
                not PUMP_MIN_S <= v <= PUMP_MAX_LIMIT_S:
            # Never unlimited, never a guess: a wrong value is refused.
            raise ValueError(f"{path}: {k} must be a whole number of "
                             f"seconds from {PUMP_MIN_S} to "
                             f"{PUMP_MAX_LIMIT_S}.")
        out[k] = v
    if doc.get("booth_alert") is not None:
        # Checked by boothaudio.parse_config, which owns the shape; a bad
        # block is refused here, at load, never at show time.
        from .boothaudio import parse_config
        out["booth_alert"] = parse_config(doc["booth_alert"],
                                          f"{path} booth_alert")
    if doc.get("alert_pixels") is not None:
        out["alert_pixels"] = check_alert_pixels(doc["alert_pixels"], path)
    v = doc.get("ui_default")
    out["ui_default"] = v if (isinstance(v, int) and not isinstance(v, bool)
                              and v in UI_VERSIONS) else 1
    return out


def check_alert_pixels(doc, path="ltcplay_remote.json"):
    """The floor pixels the blue alert pulse paints (player.py):
    {"start": 1, "count": 300, "order": "RGB"}, start the 1-based
    absolute channel of the first pixel. Returns (start0, count, order)
    with start0 counted from 0, or raises ValueError."""
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: alert_pixels has to be an object like "
                         f'{{"start": 1, "count": 300, "order": "RGB"}}.')
    start, count = doc.get("start"), doc.get("count")
    order = str(doc.get("order", "RGB") or "RGB").upper()
    for name, v in (("start", start), ("count", count)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"{path}: alert_pixels {name} has to be a "
                             f"whole number from 1 up.")
    if sorted(order) != ["B", "G", "R"]:
        raise ValueError(f"{path}: alert_pixels order has to be the three "
                         f"letters R, G and B in the pixels' order, like "
                         f"RGB or GRB.")
    return (start - 1, count, order)


def save_settings(folder, **changes):
    """Change some settings in ltcplay_remote.json and keep every other one
    (review of PR #90, P2-2). A file that cannot be read or parsed is
    never overwritten: ValueError naming it, nothing written. What would
    be written is checked first (settings_from_doc); a change that fails
    is ValueError and nothing is written. The old file is kept beside it
    as ltcplay_remote.json.bak, and the new one goes in whole (a temp file,
    then a replace)."""
    path = os.path.join(folder or settings_folder(), SETTINGS_FILE)
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise ValueError(f"{path} could not be read ({e}), so it was "
                             f"not changed. Fix or remove that file on the "
                             f"show machine, then try again.")
        if not isinstance(doc, dict):
            raise ValueError(f"{path} is not a settings object, so it was "
                             f"not changed. Fix or remove that file on the "
                             f"show machine, then try again.")
    doc = dict(doc)
    doc.update(changes)
    settings_from_doc(doc, path)            # ValueError: nothing written
    if os.path.exists(path):
        import shutil
        shutil.copyfile(path, path + ".bak")
    _write_json(path, doc)
    return load_settings(folder)


# The private IPv4 ranges (RFC 1918): the only addresses the page offers
# to save. Never loopback, link-local (169.254), carrier or public ones; a
# show network on another range can still be typed, and is checked by
# check_show_address the same way as always.
RFC1918 = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_rfc1918(text):
    """True for a private IPv4 address (10/8, 172.16/12, 192.168/16)."""
    try:
        ip = ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return False
    return ip.version == 4 and any(ip in n for n in RFC1918)


def address_candidates(found=None):
    """This machine's private IPv4 addresses (RFC 1918 only), for the page
    to offer. Best effort: the stdlib has no interface list, so this asks
    the name service and the routing table, and the operator can also type
    one. `found` replaces that lookup (tests)."""
    seen = []
    for a in (_local_ipv4s() if found is None else found):
        if a not in seen and is_rfc1918(a):
            seen.append(a)
    return seen


def _local_ipv4s():
    out = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET):
            out.append(sa[0])
    except OSError:
        pass
    try:
        k = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            k.connect(("192.0.2.1", 9))        # TEST-NET-1; nothing is sent
            out.append(k.getsockname()[0])
        finally:
            k.close()
    except OSError:
        pass
    return out


# The Windows Firewall rule the installer adds (installer.iss) and removes
# on uninstall: inbound TCP 7878, ltcplay.exe only, from this PC's own
# subnet only. The page only reads it (netsh ... show rule), never changes
# it.
FIREWALL_RULE = "LTC Player rack page"
FIREWALL_READ_S = 30.0
# Windows' own programs, started by their full path under
# %SystemRoot%\System32, never by a bare name a folder on PATH or the
# engine's own folder could answer to (security review of #164).
NETSH = ("netsh.exe",)
POWERSHELL = ("WindowsPowerShell", "v1.0", "powershell.exe")


def system_tool(*parts, root=None, exists=None):
    """The full path of a program under %SystemRoot%\\System32, or None
    when it is not there."""
    import ntpath
    root = root or os.environ.get("SystemRoot") or r"C:\Windows"
    path = ntpath.join(root, "System32", *parts)
    return path if (exists or os.path.isfile)(path) else None


def firewall_rule_state(run=None, windows=None, find=None):
    """("present" | "off" | "missing" | "unknown" | "none", sentence) for
    the Windows Firewall rule, read with netsh. Read only. Never raises.
    "none": not Windows, no rule needed."""
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        return ("none", "This is not a Windows PC, so there is no Windows "
                        "Firewall rule to check.")
    exe = (find or system_tool)(*NETSH)
    if exe is None:
        return ("unknown", "The Windows Firewall rule could not be read "
                           "(netsh.exe is not in System32).")
    try:
        import subprocess
        if run is None:
            run = subprocess.run
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # One command line as typed: netsh reads name="..." itself.
        p = run(f'"{exe}" advfirewall firewall show rule '
                f'name="{FIREWALL_RULE}" verbose',
                capture_output=True, text=True, timeout=10, **kw)
        text = (p.stdout or "") + (p.stderr or "")
    except Exception as e:                       # noqa: BLE001
        return ("unknown", f"The Windows Firewall rule could not be read "
                           f"({type(e).__name__}).")
    if p.returncode != 0:
        return ("missing", f"The Windows Firewall rule \"{FIREWALL_RULE}\" "
                           f"is missing, so other machines are blocked. Run "
                           f"the LTC Player installer again: it adds the "
                           f"rule.")
    enabled = [ln.split(":", 1)[1].strip().lower() for ln in
               text.splitlines() if ln.strip().lower().startswith(
                   "enabled:")]
    if enabled and all(v == "no" for v in enabled):
        return ("off", f"The Windows Firewall rule \"{FIREWALL_RULE}\" is "
                       f"there but switched off, so other machines are "
                       f"blocked. Run the LTC Player installer again.")
    return ("present", f"The Windows Firewall lets machines on this PC's "
                       f"own subnet in on port {FIXED_PORT} (rule "
                       f"\"{FIREWALL_RULE}\").")


# The address goes in as $env:LTC_SHOW_ADDRESS: PowerShell takes a
# variable's value as one argument and never parses it as code.
CATEGORY_ENV = "LTC_SHOW_ADDRESS"
CATEGORY_COMMAND = ("(Get-NetConnectionProfile -InterfaceIndex "
                    "(Get-NetIPAddress -IPAddress $env:LTC_SHOW_ADDRESS "
                    "-ErrorAction Stop).InterfaceIndex -ErrorAction Stop)"
                    ".NetworkCategory")


def network_category(address, run=None, windows=None, find=None):
    """(category, sentence) for the network that owns `address` on this
    PC, as Windows names it: "Public", "Private", "DomainAuthenticated",
    "unknown", or "none" (not Windows). Read only (Get-NetIPAddress and
    Get-NetConnectionProfile). Only a private IPv4 address (RFC 1918) is
    ever looked up, and it reaches PowerShell as an environment variable,
    never pasted into the command text (review of #164, B2). Never
    raises."""
    windows = (os.name == "nt") if windows is None else windows
    if not windows:
        return "none", ""
    if not is_rfc1918(address):
        return "unknown", ""
    ip = str(ipaddress.ip_address(str(address).strip()))
    exe = (find or system_tool)(*POWERSHELL)
    if exe is None:
        return "unknown", ("Which kind of network Windows calls this one "
                           "could not be read (powershell.exe is not in "
                           "System32).")
    try:
        import subprocess
        if run is None:
            run = subprocess.run
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env[CATEGORY_ENV] = ip
        p = run([exe, "-NoProfile", "-NonInteractive", "-Command",
                 CATEGORY_COMMAND],
                capture_output=True, text=True, timeout=15, env=env, **kw)
        words = [w.strip() for w in (p.stdout or "").splitlines()
                 if w.strip()]
        cat = words[-1] if p.returncode == 0 and words else ""
    except Exception:                            # noqa: BLE001
        cat = ""
    if cat == "Public":
        return cat, ("Windows calls the network with this address Public. "
                     "The firewall rule still lets this subnet in, but if "
                     "Windows Security blocks every incoming connection on "
                     "public networks, other machines are blocked. To make "
                     "it Private: Settings, Network and internet, Wi-Fi or "
                     "Ethernet, this network, Network profile type, "
                     "Private network.")
    if cat in ("Private", "DomainAuthenticated"):
        return cat, (f"Windows calls the network with this address "
                     f"{'Domain' if cat.startswith('Domain') else cat}.")
    return "unknown", ("Which kind of network Windows calls this one "
                       "(Public or Private) could not be read.")


def page_url(address, port=FIXED_PORT):
    """The address to type on another machine, or None."""
    if not address:
        return None
    a = str(address)
    if ":" in a:
        a = f"[{a}]"
    return f"http://{a}:{port}/"


def looks_proxied(headers):
    """The first forwarding header on the request, or ""."""
    for h in PROXY_HEADERS:
        if headers.get(h) is not None:
            return h
    return ""


def host_ok(host_header, client_is_loopback, bind, port):
    """True when the Host header names this server the way the client
    reached it. Stops a web page elsewhere from steering a browser at the
    engine under another name (DNS rebinding)."""
    if not host_header:
        return client_is_loopback          # curl and the launcher's checks
    h = host_header.strip().lower()
    if h.startswith("["):
        name = h[:h.find("]") + 1] if "]" in h else h
        rest = h[len(name):]
    else:
        name, _, rest = h.partition(":")
        rest = ":" + rest if rest else ""
    if rest and rest != f":{port}":
        return False
    if client_is_loopback:
        return name in LOOPBACK_NAMES
    want = bind.lower()
    return name == want or name == f"[{want}]"


def _open_in_file_manager(path):
    """The folder in Explorer (Windows) or the Finder (Mac). On Windows the
    new Explorer window is then brought to the front: opened by the engine
    (a background process) it has no right to the foreground and appeared
    BEHIND the rack screen, so Jeff saw nothing (2026-10-19)."""
    import subprocess
    import sys
    if os.name == "nt":
        import threading
        before = _explorer_windows()
        os.startfile(path)                       # noqa: S606
        threading.Thread(target=_explorer_to_front, args=(path, before),
                         daemon=True, name="ltcplay-explorer-front").start()
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                          path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)


EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")
EXPLORER_FRONT_WAIT_S = 4.0


def pick_explorer_window(path, before, now, reuse=False):
    """The window handle of the Explorer window just opened for `path`, from
    the top-level windows `before` and `now` as [(hwnd, class, title)]: a
    new Explorer window whose title is the folder's name or its full path
    (Explorer shows one or the other), else the one new Explorer window.
    None while there is none yet, or two new ones and neither is titled
    for this folder (nothing else's window is ever pulled forward). With
    `reuse` (the wait is over and nothing new came), an Explorer window
    that was already open on this folder: Explorer reuses it instead of
    opening a second one, and it is still behind the rack screen."""
    seen = {w[0] for w in before}
    new = [w for w in now if w[0] not in seen and w[1] in EXPLORER_CLASSES]
    full = os.path.normpath(path).rstrip("\\/").lower()
    name = full.replace("\\", "/").rsplit("/", 1)[-1] or full
    titled = [w for w in new if (w[2] or "").strip().lower() in (name, full)]
    if titled:
        return titled[0][0]
    if len(new) == 1:
        return new[0][0]
    if reuse and not new:
        again = [w for w in now if w[1] in EXPLORER_CLASSES and
                 (w[2] or "").strip().lower() in (name, full)]
        if again:
            return again[0][0]
    return None


def _explorer_windows():
    """Every visible top-level window as (hwnd, class, title). Windows
    only; [] anywhere else or on any error."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        out = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                   wintypes.LPARAM)

        def each(hwnd, _lp):
            if u.IsWindowVisible(hwnd):
                cls = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(hwnd, cls, 64)
                title = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(hwnd, title, 512)
                out.append((int(hwnd), cls.value, title.value))
            return True
        u.EnumWindows(proto(each), 0)
        return out
    except Exception:                            # noqa: BLE001
        return []


def _explorer_to_front(path, before, wait_s=EXPLORER_FRONT_WAIT_S):
    """Wait for the Explorer window opened for `path` (up to wait_s) and
    put it in front, restored if it was minimized. Best effort: a window
    that cannot be found or moved is left to the taskbar, which the page
    says. Windows only."""
    if os.name != "nt":
        return False
    import time as _t
    deadline = _t.monotonic() + wait_s
    hwnd = None
    while hwnd is None and _t.monotonic() < deadline:
        hwnd = pick_explorer_window(path, before, _explorer_windows())
        if hwnd is None:
            _t.sleep(0.1)
    if hwnd is None:
        hwnd = pick_explorer_window(path, before, _explorer_windows(),
                                    reuse=True)
    if hwnd is None:
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        SW_RESTORE, VK_MENU, KEYEVENTF_KEYUP = 9, 0x12, 0x0002
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        # A background process may not take the foreground; a tap of the
        # Alt key releases Windows' foreground lock for the next call, the
        # usual way round it. SwitchToThisWindow is the belt behind that.
        u.keybd_event(VK_MENU, 0, 0, 0)
        u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        u.SetForegroundWindow(hwnd)
        u.SwitchToThisWindow(hwnd, True)
        u.BringWindowToTop(hwnd)
        return True
    except Exception:                            # noqa: BLE001
        return False


# ------------------------------------------------ flamesafe's status --

def load_flamesafe_status_link(path):
    """(ip, mirror_port, key, group names) from a flamesafe config, read as
    plain JSON (ltcplay never imports flamesafe). ValueError when it has no
    status_mirror_port: the deck holds status_port."""
    with open(path, encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    link = doc.get("link") or {}
    if "status_mirror_port" not in link:
        raise ValueError(f"{path} has no link.status_mirror_port, so "
                         f"flamesafe sends this engine no copy of its "
                         f"status. Add one (a free loopback port, like "
                         f"5574) to show the flame lamps on the page.")
    names = [str(g.get("name")) for g in (doc.get("groups") or [])]
    return (link.get("status_ip", "127.0.0.1"),
            int(link["status_mirror_port"]), str(link["key"]), names)


def decode_status(data, key):
    """A flamesafe status frame, or None unless it is keyed with ours."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("t") != "status":
        return None
    if not hmac.compare_digest(str(obj.get("k", "")), key):
        return None
    return obj


def _list(v):
    """`v` if it is a list, else an empty one (a status field from an older
    flamesafe that does not send it)."""
    return v if isinstance(v, list) else []


def _ints(v):
    """A status list of whole numbers, or None when it is not one (a field
    from an older flamesafe that does not send it, or a bad one)."""
    if not isinstance(v, list) or any(
            isinstance(x, bool) or not isinstance(x, int) for x in v):
        return None
    return list(v)


def _arm(v):
    """A status arm value: a whole number 1..255, else None."""
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 256:
        return None
    return v


class FlameStatus:
    """flamesafe's status frames, read for display only. CONTRACT.md:
    there is no path from a status frame to flame output, and nothing read
    here is ever sent anywhere."""

    def __init__(self, ip, port, key, names=(), clock=time.monotonic):
        self.ip, self.port, self.key = ip, port, key
        self.names = list(names)
        self.clock = clock
        self.last = None
        self.last_at = None
        self._sock = None
        self._stop = threading.Event()

    @classmethod
    def from_config(cls, path, clock=time.monotonic):
        ip, port, key, names = load_flamesafe_status_link(path)
        return cls(ip, port, key, names, clock=clock)

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((self.ip, self.port))
        s.settimeout(0.2)
        self._sock = s
        threading.Thread(target=self._loop, daemon=True,
                         name="ltcplay-flame-status").start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _a = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            self.note(data)

    def note(self, data):
        obj = decode_status(data, self.key)
        if obj is not None:
            self.last, self.last_at = obj, self.clock()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def view(self):
        age = (None if self.last_at is None
               else self.clock() - self.last_at)
        stale = age is None or age > FLAME_STALE_S
        groups = []
        last = self.last or {}
        for g in last.get("groups") or []:
            groups.append({"name": str(g.get("name", "")),
                           "armed": str(g.get("armed", "")),
                           "wanted": bool(g.get("wanted")),
                           "reason": str(g.get("reason", "")),
                           "dwell_s": g.get("dwell_s", 0),
                           "locked_fire": list(g.get("locked_fire") or []),
                           # flamesafe multi-safety (2026-10-26): one entry
                           # per safety slot; the card shows the count line
                           # only for a group with more than one.
                           "safeties_up": [bool(x) for x in
                                           _list(g.get("safeties_up"))],
                           "safeties_locked": [bool(x) for x in
                                               _list(g.get(
                                                   "safeties_locked"))],
                           # The value this group arms at (2026-10-26, a
                           # safety_profile may give it its own); None from
                           # an older flamesafe that does not send it.
                           "arm_value": _arm(g.get("arm_value")),
                           # Aim slots (2026-10-28): flamesafe's own list,
                           # None from an older flamesafe that does not
                           # send it; with the fire slots and what was asked
                           # and sent on them, for the card's aim line.
                           "aim_slots": _ints(g.get("aim_slots")),
                           "fire_slots": _ints(g.get("fire_slots")) or [],
                           "commanded_fire": _ints(
                               g.get("commanded_fire")),
                           "sent_fire": _ints(g.get("sent_fire"))})
        if not groups:
            groups = [{"name": n, "armed": "unknown", "wanted": False,
                       "reason": "", "dwell_s": 0} for n in self.names]
        if stale:
            # Never show a lamp from an old frame as if it were now.
            for g in groups:
                g["armed"] = "unknown"
        lk = last.get("lockout") if isinstance(last.get("lockout"), dict) \
            else None
        return {"connected": True, "stale": stale,
                "age_ms": None if age is None else int(age * 1000),
                "fault": str(last.get("fault", "")) if not stale else "",
                # flamesafe's own top-level `confirmed` (CONTRACT.md: false
                # until Andy confirms the config's numbers). The rack
                # screen's Info popup says it; the Stream Deck's keys no
                # longer carry a mark for it (2026-10-19). None while no
                # fresh frame says either way.
                "confirmed": (bool(last.get("confirmed", True))
                              if self.last is not None and not stale
                              else None),
                "groups": groups,
                # The hazers as flamesafe reports them (2026-10-29), None
                # while stale or from an older flamesafe.
                # Each entry's kind (2026-10-30): "hazer", or "switch" for
                # the fuel pump with its on value; an entry from a
                # flamesafe that says no kind is a hazer.
                "passthrough": ([
                    {"name": str(p.get("name", "")),
                     "kind": str(p.get("kind") or "hazer"),
                     "on": (p.get("on") if isinstance(p.get("on"), int)
                            and not isinstance(p.get("on"), bool)
                            else None),
                     "slots": _ints(p.get("slots")) or [],
                     "asked": bool(p.get("asked")),
                     "enabled": bool(p.get("enabled")),
                     "passing": bool(p.get("passing")),
                     "why": str(p.get("why", ""))}
                    for p in last["passthrough"] if isinstance(p, dict)]
                    if isinstance(last.get("passthrough"), list)
                    and not stale else None),
                # What flamesafe says it is holding at zero (the lockout
                # file as it read it), for the page to compare with its own.
                "lockout": ({"slots": [int(s) for s in lk.get("slots") or []
                                       if isinstance(s, int)],
                             "state": str(lk.get("state", ""))}
                            if lk is not None and not stale else None)}


def _preroll_hold_s():
    from .preroll import CONTINUE_HOLD_S
    return CONTINUE_HOLD_S


# ------------------------------------------------------- the remote --

def cookie_token(header):
    """The users.COOKIE value from a Cookie header, or ""."""
    for part in str(header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ltcplay_user" and re.match(r"^[A-Za-z0-9_-]{16,64}$", v):
            return v
    return ""


class Ctx:
    """Where a request came from: the machine itself, or a device on the
    show network, and who is signed in on that page, if anyone (users.py;
    nobody has to be). `session` is users.Users.session()'s dict."""

    def __init__(self, local, ip, session=None, token="", window=""):
        self.local = local
        self.ip = ip
        self.session = session
        self.token = token
        self.window = window

    @property
    def user(self):
        """The signed-in name, or "" for nobody."""
        return (self.session or {}).get("name") or ""

    @property
    def source(self):
        """The audit log's source: the device, and the page's window id."""
        return f"{self.device} {self.window}".strip()

    @property
    def screen(self):
        """The screen-list name the scheduler checks."""
        return "Rack screen" if self.local else "iPad"

    @property
    def device(self):
        """What the journal calls it: the surface, never a person."""
        return "Rack screen" if self.local else f"iPad at {self.ip}"


def countdown_view(sched, show, cue_now):
    """What the page's countdown shows (Jeff, 2026-10-07). During a show,
    running or held, the time left in THAT show: its length minus where
    the engine's own show clock is in it (session.snapshot()'s "now", so a
    Hold, which freezes the clock, freezes this too). A show whose length
    is not known shows no countdown at all, never a guess. Between shows,
    the countdown to the next one, as before. None means show nothing.
    Display only: nothing here starts or stops anything."""
    sched = sched or {}
    if sched.get("attached"):
        live = sched.get("state") in ("SHOW", "PAUSED")
        held = sched.get("state") == "PAUSED"
    else:
        live = bool((show or {}).get("running")) and cue_now is not None
        held = False
    if live:
        now = cue_now if isinstance(cue_now, dict) else {}
        dur, el = now.get("duration"), now.get("elapsed")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                   and math.isfinite(x) for x in (dur, el)) or dur <= 0:
            return None
        return {"kind": "left", "label": "Left in this show",
                "s": round(max(0.0, dur - max(0.0, el)), 1),
                "running": not held}
    nx = sched.get("next") if sched.get("attached") else None
    if isinstance(nx, dict):
        # The exact figure when the engine gives one: the page counts down
        # from it on its own clock. Whole seconds, rounded down, aged by
        # the page, made the last digit flicker (soak 2026-10-21).
        for key in ("in_s_exact", "in_s"):
            v = nx.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) \
                    and math.isfinite(v):
                return {"kind": "next", "label": "Next show", "s": v,
                        "running": True}
    return None


# The deck's worst USB key image write (deck-input's usb_ms) at or past
# which the rack screen's Deck chip turns amber: the same 1 s the deck's own
# journal line uses (streamdeck.USB_SLOW_S; selftest pins the two together).
DECK_USB_SLOW_MS = 1000


def deck_usb_ms(query):
    """usb_ms from a deck-input query string, as an int from 0 to 10
    minutes, or None when absent or not a number. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("usb_ms") or []
        ms = int(vals[-1])
    except (ValueError, TypeError, IndexError):
        return None
    if ms < 0:
        return None
    return min(ms, 600000)


def deck_nokey(query):
    """The flamesafe group indices a deck-input query says have no key on
    the Stream Deck (`nokey=4,5`; review of #126, P1), for the Rack
    screen's flame card. Display only. Never raises."""
    import urllib.parse
    out = set()
    try:
        vals = urllib.parse.parse_qs(query or "").get("nokey") or []
        for part in ",".join(vals).split(",")[:DECK_REFUSED_MAX]:
            if part.isdigit():
                out.add(int(part))
    except (ValueError, TypeError):
        return set()
    return out


def deck_layout_default(query):
    """True when a deck-input query says `layout=default`: the Stream Deck
    could not use ltcplay_deck.json and keys the default layout (review of
    PR #120, P2-1), for the Deck chip. Display only. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("layout") or []
    except (ValueError, TypeError):
        return False
    return bool(vals) and vals[-1] == "default"


def deck_usb_fail(query):
    """usb_fail from a deck-input query string: key image writes the deck
    did not take in the last minute (review of PR #83, P2-2), as an int
    from 0 to 100000, 0 when absent or not a number. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("usb_fail") or []
        n = int(vals[-1])
    except (ValueError, TypeError, IndexError):
        return 0
    return max(0, min(n, 100000))


# The Deck chip's live truth (Jeff, 2026-10-26, the Pico: the chip said
# ok with the Stream Deck unplugged and its FAULT acknowledged, because the
# deck PROGRAM keeps reading this engine while the hardware is away). Green
# only while all three hold, each read off the deck's latest deck-input
# read: the program was heard within PLAIN_DECK_FRESH_S, it says the USB
# deck is present (hw=1), and its last arm frame went out within
# DECK_ARM_FRESH_MS (arm_ms), flamesafe's own arm_stale_ms. Display only:
# nothing that arms, disarms or stops reads these.
DECK_ARM_FRESH_MS = 500


def deck_hw(query):
    """hw from a deck-input query string: True when the Stream Deck program
    says its USB deck is present (hw=1), False when it says it is not
    (hw=0), None when it says nothing (an older deck program) or anything
    else. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("hw") or []
        v = vals[-1]
    except (ValueError, TypeError, IndexError):
        return None
    return True if v == "1" else False if v == "0" else None


def deck_arm_ms(query):
    """arm_ms from a deck-input query string: how long before the read the
    deck's last arm frame went out, as an int from 0 to 10 minutes, or None
    when absent or not a number. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("arm_ms") or []
        ms = int(vals[-1])
    except (ValueError, TypeError, IndexError):
        return None
    if ms < 0:
        return None
    return min(ms, 600000)


def deck_refused(query):
    """[(hold id, code)] from a deck-input query's `refused`, "7.refractory"
    entries joined by commas (streamdeck.ScreenKeys), only codes
    DECK_REFUSALS knows, at most DECK_REFUSED_MAX. Never raises."""
    import urllib.parse
    out = []
    try:
        vals = urllib.parse.parse_qs(query or "").get("refused") or []
        for part in ",".join(vals).split(",")[:DECK_REFUSED_MAX]:
            hid, _, code = part.partition(".")
            if code in DECK_REFUSALS and hid.isdigit():
                out.append((int(hid), code))
    except (ValueError, TypeError):
        return []
    return out


# A deck's rearm and prefused words are shown only while its reads keep
# coming (it reads 20 times a second); older than this, nothing is said.
DECK_REARM_FRESH_S = 1.0
DECK_REARM_MAX_MS = 60000


def deck_rearm(query):
    """({group: ms left in the Stream Deck's re-arm refractory window},
    {group: purge refusal code}) from a deck-input query's `rearm`
    ("0.1500,2.300") and `prefused` ("1.refractory"); codes DECK_REFUSALS
    knows only. Display only: the page's "Re-arm in 1.5 s" (Jeff,
    2026-10-26). Never raises."""
    import urllib.parse
    left, pref = {}, {}
    try:
        q = urllib.parse.parse_qs(query or "")
        for part in ",".join(q.get("rearm") or []).split(",")[:DECK_REFUSED_MAX]:
            gi, _, ms = part.partition(".")
            if gi.isdigit() and ms.isdigit():
                left[int(gi)] = min(int(ms), DECK_REARM_MAX_MS)
        for part in ",".join(q.get("prefused") or []).split(",")[:DECK_REFUSED_MAX]:
            gi, _, code = part.partition(".")
            if gi.isdigit() and code in DECK_REFUSALS:
                pref[int(gi)] = code
    except (ValueError, TypeError):
        return {}, {}
    return left, pref


def deck_taken(query):
    """[hold id] from a deck-input query's `taken`: screen holds the Stream
    Deck itself armed a group from (streamdeck.ScreenKeys.note_taken), at
    most DECK_REFUSED_MAX. Never raises."""
    import urllib.parse
    try:
        vals = urllib.parse.parse_qs(query or "").get("taken") or []
        return [int(h) for h in ",".join(vals).split(",")[:DECK_REFUSED_MAX]
                if h.isdigit()]
    except (ValueError, TypeError):
        return []


def sent_words(sent):
    """What an announcement actually sent, in plain words: the route, the
    output and its audio system, the channels, the seconds and the peak
    level (Jeff's field report, 2026-10-28)."""
    if not isinstance(sent, dict):
        return "no details"
    where = ("through the show audio on " if sent.get("route") ==
             "show audio" else "on its own output ") + \
        str(sent.get("device") or "?")
    if sent.get("hostapi"):
        where += f" ({sent['hostapi']})"
    peak = sent.get("peak_dbfs")
    return (f"{where}, {sent.get('channels')} channel(s), "
            f"{float(sent.get('length_s') or 0):.1f} s, peak "
            f"{'silence' if peak is None else f'{peak:.1f} dBFS'}")


class Remote:
    """The routes under /api/remote/. `control` is web.Control, `schedule`
    the scheduler service (or None), `flame_status` a FlameStatus (or
    None), `flame_disarm` a callable(reason) returning something with .ok
    and .sentence (the show's flames_disarm_all), or None to take it from
    the scheduler's conductor when one is attached."""

    def __init__(self, control, schedule=None, folder=None,
                 flame_status=None, flame_disarm=None,
                 clock=time.monotonic, wall=time.time, log=print):
        self.control = control
        self.schedule = schedule
        self.folder = folder or (schedule.state_dir if schedule is not None
                                 else settings_folder())
        self.flame_status = flame_status
        self._flame_disarm = flame_disarm
        self.flame_cues = None       # fire_ice.FlameCues, set by web.serve
        # The rig check before each show (preroll.PreRoll), set by
        # web.serve with the scheduler's; None without one.
        self.preroll = None
        # Where this engine really listens, set by web.serve:
        # {"addresses": [...], "port": int, "why": sentence or ""}. None
        # (a Remote built without serve) reads "not known".
        self.listen = None
        # configwatch.ConfigWatch, set by web.serve for `ltc serve`: the
        # settings files changed on disk since this engine (or the deck,
        # flamesafe, the supervisor) read them. Display only.
        self.config_watch = None
        # Aim slots (2026-10-28): (this engine's aim slots, the flamesafe
        # config it read them from), set by web.serve once the flame link
        # is built; None without one. aim_view compares them with
        # flamesafe's own and raises a display only "aim" alert while
        # they differ.
        self.flame_aim = None
        self._aim_said = None        # the mismatch sentence last raised
        self._aim_alert_id = None    # ...and its alert's id
        # Flame frame timing (2026-10-29): () -> the FlameLink or None, set
        # by web.serve; the late frames last raised as the display only
        # "timing" alert (_frame_timing_work).
        self.flame_link_get = None
        self._timing_said = 0
        self._aim_lock = threading.Lock()
        # beyondwatch.BeyondWatch, set by web.serve with BEYOND configured:
        # the Lasers lamp never says lit while BEYOND is not running.
        self.beyond_watch = None
        # Open BEYOND on the Startup page (windowfront.py, Jeff,
        # 2026-10-26): BEYOND's OSC host from the settings, set by
        # web.serve (None: not set, BEYOND is looked for on this PC), and
        # the call that brings its window forward (a fake in the selftest).
        self.beyond_host = None
        self.open_beyond_fn = None
        # MadMapper's video output (showwindows.OutputLook), or None with
        # the reason it is not checked (web._attach_mm_output).
        self.mm_output = None
        self.mm_output_why = ""
        # The last show seen ended ({"night", "ended"}), kept so a night
        # that closes at its 02:00 reset before the sign-out is due still
        # gets it (review of PR #125, P2-8).
        self._last_show_seen = None
        # madmapperwatch.MadMapperWatch, set by web.serve with MadMapper
        # configured, and its heartbeat Watchdog and HeartbeatConfig: the
        # Video lamp says whether MadMapper is running before a show plays.
        self.madmapper_watch = None
        self.madmapper_heartbeat = None
        self.madmapper_heartbeat_cfg = None
        # madmapperwatch.FollowCheck, made on the first _follow_work, and
        # the id of the "follow" alert it raised (Jeff's field report,
        # 2026-10-28: MadMapper not following the show's timecode).
        self._follow = None
        self._follow_alert_id = None
        self.morning = None          # morning.Morning, set by web.serve
        self.fuel = None             # fuel.FuelTracker, set by web.serve
        self.fireball = None         # fireball.FireballPanel, web.serve
        self.cleaning = None         # cleaning.CleaningPanel, web.serve
        # hazers.Hazers, set by web.serve (Fire & Ice with a flame link),
        # and the "hazers" mismatch alert's sentence and id.
        self.hazers = None
        self._hz_said = None
        self._hz_alert_id = None
        self._hz_lock = threading.Lock()
        # fuelpump.FuelPump, set by web.serve when flamesafe's config has a
        # fuel pump (2026-10-30).
        self.pump = None
        self.live_flame = None       # flameprog.LiveFlame, set by web.serve
        self.clock = clock
        self.wall = wall
        self._log = log
        self.marks = {"a": None, "b": None}     # loop marks, show seconds
        # The Programming playhead on the show audio (playhead.py): one
        # per engine, so its watchdog and its record of the playback it
        # started outlive a single press.
        self.playhead = ShowAudioPlayhead(
            control, schedule, threaded=True,
            journal=lambda text, outcome: self._journal(
                "", "Programming screen", "programming playback", outcome,
                text))
        # Start now asks again under the scheduler's lock, after its tick
        # and any wait for the show audio (a Play from may have landed).
        if schedule is not None and hasattr(schedule, "start_veto"):
            schedule.start_veto = self.programming_playback_refusal
        # Screen arm holds: group index -> {token, who, device, start,
        # beat, id}; per-group disarms waiting for the deck to read them.
        import collections
        self._arm_lock = threading.Lock()
        self._holds = {}
        self._hold_ids = 0
        self._disarms = collections.deque(maxlen=64)
        # Disarm ids start from the wall clock in milliseconds (review of
        # PR #97, P2-3): the Stream Deck's ScreenKeys keeps the highest id
        # it has seen across an engine restart, so a new engine's ids must
        # always be above the old one's, or its first disarms are dropped.
        # The show engine (web.serve with accounts, `ltc serve`) then
        # lifts it to the range the last engine reserved (reserve_disarm_
        # ids, second review of #97, P3-3).
        try:
            self._disarm_ids = max(0, int(wall() * 1000))
        except Exception:                        # noqa: BLE001
            self._disarm_ids = int(time.time() * 1000)
        # The show in use, written to SELECTED_SHOW_FILE off the request
        # (review of #102, P2-c): the name waiting to be written, the lock
        # the writes take in turn, and an event set while none is waiting.
        self._sel_pending = None
        self._sel_lock = threading.Lock()
        self._sel_idle = threading.Event()
        self._sel_idle.set()
        # Disarm all flame groups (plain): which press is the latest, and
        # how its confirmation is scheduled (a test runs it by hand).
        self._plain_gen = 0
        self._later = self._timer
        self._deck_seen = None       # clock() of the deck's last read
        # How many times the deck has read deck-input: a per-group Disarm
        # is confirmed only on a status after the deck's next read.
        self._deck_reads = 0
        # How a per-group Disarm waits between looks (a test steps it).
        self._pause = time.sleep
        # The deck's worst USB frame write of its last minute, in ms, as
        # its last deck-input read carried it (streamdeck.UsbMeter), or
        # None: the rack screen's Deck chip. Display only.
        self._deck_usb_ms = None
        self._deck_usb_fail = 0
        # The deck's own word, on its last deck-input read, on its USB
        # hardware (deck_hw: True, False, or None from an older deck) and
        # on its arm link (deck_arm_ms). The Deck chip's; display only.
        self._deck_hw = None
        self._deck_arm_ms = None
        # The deck's re-arm waits and purge refusals: (left, prefused,
        # clock() read), display only (deck_rearm).
        self._deck_rearm = ({}, {}, None)
        # Screen holds the Stream Deck would not take, as its deck-input
        # reads reported them: hold id -> DECK_REFUSALS code.
        self._deck_refused = {}
        self._version = None         # version_line(), once asked
        self.audio_watch = None      # audiowatch.AudioInterfaceWatch, F&I
        # The rack screen's Weather block (weather.py, WEATHER.md), set by
        # web.serve. Display only; None: the block says it is not running.
        self.weather = None
        # announce.AnnounceService, set by web.serve when announcements are
        # configured (the `announce` property); None: the rack screen's
        # block says they are not. Its view is refreshed on a thread of its
        # own (_ann_loop), never on a status or deck-input request: those
        # never take the service's lock or ask the audio system.
        self._announce = None
        self._ann_cache = None       # (clock(), view)
        self._ann_thread = None
        # The Windows supervisor's word on the disk (housekeeping.py writes
        # disk-low.txt in the settings folder while it is low or critical,
        # and removes it when it is fine again). Read at most every 30 s.
        self.disk_file = os.path.join(settings_folder(), DISK_FILE)
        self._disk = (None, 0.0)
        # The alert service (alerts.py, 2026-10-12): the rack screen's
        # banners, the booth note and the blue pulse. web.serve gives it
        # the booth player and the pulse's pixel range from the settings;
        # the scheduler raises its failed-start alert through alert_sink.
        from .alerts import Alerts
        self.alerts = Alerts(journal=self._alert_journal, clock=clock,
                             wall=wall, show_running=self._show_live,
                             on_change=self._apply_pulse)
        self.alert_pixels = None     # (start0, count, order) or None
        if schedule is not None and hasattr(schedule, "alert_sink"):
            schedule.alert_sink = self.alerts.raise_
        # The alert hold (alerthold.py, Jeff 2026-10-26): at showtime with
        # an alert open the schedule Holds and the booth speaker says so.
        # Nothing plays on the public PA automatically (Jeff, 2026-10-30).
        # The booth re-sound runs after every scheduler tick
        # (_alert_tick), on a worker of its own.
        from .alerthold import AlertHold
        self._booth_wake = threading.Event()
        self._alert_stop = threading.Event()
        self._alert_lock = threading.Lock()
        self._booth_thread = None
        self.alert_hold = AlertHold(self.alerts, schedule, clock=clock)
        if schedule is not None and hasattr(schedule, "showtime_gate"):
            schedule.showtime_gate = self.alert_hold.gate
            schedule.on_alert_hold = self.alert_hold.started
            schedule.tick_hooks.append(self._alert_tick)
        # Users and the audit log (users.py, audit.py, auditsend.py; Jeff,
        # 2026-10-20), set by web.serve. None: no sign-in, nothing logged,
        # every press as before.
        self.users = None
        self.audit = None
        self.uploader = None

    DISARM_IDS_FILE = "ltcplay_disarm_ids.json"
    DISARM_ID_RESERVE = 10_000_000

    def reserve_disarm_ids(self):
        """At the show engine's start, before any press: the next disarm id
        is the larger of the wall clock's (set in __init__) and the range
        the last engine reserved in the settings folder (a PC clock stepped
        back between two runs must not put the new ids under the old,
        which the Stream Deck would drop); then this engine reserves its
        own range (DISARM_ID_RESERVE ids) for the next one. Written once,
        here, never on a press. Never raises."""
        with self._arm_lock:
            seed = self._disarm_ids
        path = os.path.join(self.folder, self.DISARM_IDS_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                seed = max(seed, int(json.load(fh).get("next", 0)))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"next": seed + self.DISARM_ID_RESERVE}, fh)
        except OSError:
            pass
        with self._arm_lock:
            self._disarm_ids = max(self._disarm_ids, seed)
        return seed

    def _alert_journal(self, text, fault=False, action="alert",
                       outcome="", who="", screen="", **_kw):
        self._journal(who or "system", screen or "engine", action,
                      outcome or ("fault" if fault else "done"), text,
                      fault=fault)

    def _show_live(self):
        """True while the scheduler says a show is live (SHOW or PAUSED):
        the pulse's during-show rule (alerts.py)."""
        svc = self.schedule
        if svc is None:
            return False
        try:
            return svc.state_view().get("state") in ("SHOW", "PAUSED")
        except Exception:                # noqa: BLE001
            return False

    def _apply_pulse(self):
        """The blue floor pulse follows the open alerts: a flag the live
        player reads on its own tick (player.alert_pulse), set here on
        every alert change and again on every status read, so a Run
        pressed after the alert was raised still shows it."""
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        if p is None:
            return
        try:
            p.alert_pixels = self.alert_pixels
            p.alert_pulse = self.alerts.pulse_wanted()
        except Exception:                # noqa: BLE001
            pass

    # -- who ---------------------------------------------------------------
    def context(self, local, ip):
        return Ctx(local, ip)

    def users_active(self):
        """True once any user exists (users.py): then every name in the
        journal is the signed-in user's, never one a body carries. False
        without users, or when the users file cannot be asked."""
        u = getattr(self, "users", None)
        if u is None:
            return False
        try:
            return bool(u.active())
        except Exception:                        # noqa: BLE001
            return False

    def signed_in_name(self, ctx, body=None):
        """The authenticated name a press is from, with users on: this
        page's own session, or for the Stream Deck process (this machine,
        screen "Stream Deck") whoever is signed in on this machine's rack
        screen; users.UNSIGNED when nobody is. Never a name from the body
        (Jeff's field report, 2026-10-28: the journal said "By Jeff" with
        nobody signed in)."""
        from . import users as users_mod
        name = getattr(ctx, "user", "")
        if name:
            return name
        body = body if isinstance(body, dict) else {}
        if getattr(ctx, "local", False) and \
                str(body.get("screen") or "") == "Stream Deck":
            try:
                mu = self.users.machine_user()
            except Exception:                    # noqa: BLE001
                mu = None
            if mu and mu.get("name"):
                return mu["name"]
        return users_mod.UNSIGNED

    def _actor(self, ctx, body=None):
        """(who, screen, surface) for a press. With users on (users.py):
        who is the signed-in user (signed_in_name), or "unsigned", and
        never looked up on the operator list; the screen is the one the
        Stream Deck process names on this machine, else the request's own.
        Without users, as before: from the network the surface it came
        from, by the request's own address, whatever the body says; on the
        machine itself the Stream Deck process names its chosen operator
        (or none) and "Stream Deck" (EngineConductor), and that is kept,
        checked against the lists as before; the page sends neither, and
        is the Rack screen."""
        body = body if isinstance(body, dict) else {}
        if self.users_active():
            screen = ctx.screen
            if ctx.local:
                screen = str(body.get("screen") or "").strip() or screen
            return self.signed_in_name(ctx, body), screen, True
        if ctx.local:
            who = str(body.get("who") or "").strip()
            screen = str(body.get("screen") or "").strip()
            if who or screen:
                return who, screen or "Rack screen", False
        # Signed in (users.py): the journal names the person, the surface
        # stays the screen. Still a surface press: no operator list lookup.
        user = getattr(ctx, "user", "")
        if user:
            return user, ctx.screen, True
        return ctx.device, ctx.screen, True

    def _journal(self, who, screen, action, outcome, text, fault=False):
        if self.schedule is not None:
            try:
                self.schedule.journal_press(who, screen, action, outcome,
                                            text, fault=fault)
                return
            except Exception:
                pass
        s = getattr(self.control, "session", None)
        log = getattr(s, "log", None)
        try:
            if log is not None:
                log.event("remote", text)
            else:
                self._log(text)
        except Exception:
            pass

    # -- the routes --------------------------------------------------------
    def get(self, route, ctx, query=""):
        name = route[len("/api/remote/"):]
        if name in SETUP_GET_ROUTES:
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, (self.setup_view() if name == "setup"
                         else self.schedule_file_view())
        if name == "whoami":
            return 200, self.whoami(ctx)
        if name == UI_DEFAULT_ROUTE:
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, {"default": self.ui_default()}
        if name in USER_GET_ROUTES:
            return self.users_get(ctx)
        if name == "deck-input":
            # The Stream Deck process, on this machine. Never the network:
            # it carries the holds the deck acts on.
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.deck_input(query)
        if name == "status":
            return 200, self.status(ctx)
        if name == "restart-check":
            # The Windows supervisor, on this machine, right before it
            # stops the engine for a restart-engine request.
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, {"refusal": self.restart_refusal()}
        if name == "weather":
            # Display only: what the weather thread last stored.
            from . import weather as weather_mod
            w = self.weather
            return 200, (w.view() if w is not None
                         else weather_mod.view_when_missing())
        if name == "network":
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return 200, self.network_view()
        if name == "announce-outputs":
            if not ctx.local:
                return 403, {"error": "Only on the show machine itself."}
            return self.announce_outputs()
        if name in MORNING_GET_ROUTES:
            return self.morning_get(name, query, ctx)
        if name in FUEL_GET_ROUTES:
            from . import fuel as fuel_mod
            return fuel_mod.route_get(self.fuel, ctx, self.users, self.wall)
        if name in FIREBALL_GET_ROUTES:
            from . import fireball as fireball_mod
            return fireball_mod.route_get(self.fireball, ctx, self.wall)
        if name in CLEANING_GET_ROUTES:
            from . import cleaning as cleaning_mod
            return cleaning_mod.route_get(self.cleaning, ctx, self.wall)
        return 404, {"error": "no such thing here"}

    def post(self, route, body, ctx):
        """(code, body, extra headers)."""
        name = route[len("/api/remote/"):]
        body = body if isinstance(body, dict) else {}
        if name in REMOVED_ROUTES:
            # A page from before the change: told plainly, nothing done.
            return 410, {"error": REMOVED_ROUTES[name], "removed": True}, {}
        if "who" in body and self.users_active():
            # A name in the body (the Morning page's checklist, the Stream
            # Deck's chosen operator) is never who pressed: the signed-in
            # user is, or "unsigned" (2026-10-28).
            body = dict(body, who=self.signed_in_name(ctx, body))
        if name in SETUP_POST_ROUTES:
            if not ctx.local:
                return 403, {"error": "The show folder, the schedule file "
                                      "and this window are set on the show "
                                      "machine itself, not from here."}, {}
            return (*self.setup_post(name, body),) + ({},)
        if name == UI_DEFAULT_ROUTE:
            if not ctx.local:
                return 403, {"error": "The default interface is set on the "
                                      "show machine itself, not from here. "
                                      "Nothing was changed."}, {}
            return (*self.set_ui_default(body, ctx),) + ({},)
        if name in LOCAL_ONLY and not ctx.local:
            what = ("Arming from a screen is switched on and off"
                    if name == "screen-arming" else
                    "The rig is checked" if name == "preroll-check" else
                    "The announcement output is picked"
                    if name == "announce-output" else
                    "The test tone is played"
                    if name == "announce-test-tone" else
                    "The show network is set")
            return 403, {"error": f"{what} on the show machine itself, not "
                                  f"from here. Nothing was changed."}, {}
        if name == "network":
            return (*self.set_network(body),) + ({},)
        if name == "screen-arming":
            return (*self.set_screen_arming(body, ctx),) + ({},)
        if name == "preroll-check":
            return (*self.preroll_check(body, ctx),) + ({},)
        if name == "preroll-continue":
            return (*self.preroll_continue(body, ctx),) + ({},)
        if name == "announce-output":
            return (*self.announce_output(body, ctx),) + ({},)
        if name == "announce-test-tone":
            return (*self.announce_test_tone(ctx),) + ({},)
        if name == "live-flame":
            return (*self.live_flame_post(body, ctx),) + ({},)
        if name in USER_POST_ROUTES:
            return self.users_post(name, body, ctx)
        if name in MORNING_POST_ROUTES:
            return (*self.morning_post(name, body, ctx),) + ({},)
        if name in FUEL_POST_ROUTES:
            from . import fuel as fuel_mod
            return (*fuel_mod.route_post(self.fuel, name, body, ctx,
                                         self.users),) + ({},)
        if name in FIREBALL_POST_ROUTES:
            from . import fireball as fireball_mod
            return (*fireball_mod.route_post(self.fireball, self, name, body,
                                             ctx),) + ({},)
        if name in CLEANING_POST_ROUTES:
            from . import cleaning as cleaning_mod
            return (*cleaning_mod.route_post(self.cleaning, self, name, body,
                                             ctx),) + ({},)
        if name in ANNOUNCE_ROUTES:
            return (*self.announce_post(name, body, ctx),) + ({},)
        if name in HAZER_POST_ROUTES:
            return (*self.hazer_post(name, body, ctx),) + ({},)
        if name in PUMP_POST_ROUTES:
            return (*self.pump_post(body, ctx),) + ({},)
        if name in CONTROL_ROUTES or name in TRANSPORT_ROUTES:
            return (*self.press(name, body, ctx),) + ({},)
        if name == "arm-hold":
            return (*self.arm_hold(body, ctx),) + ({},)
        if name == "arm-release":
            return (*self.arm_release(body, ctx),) + ({},)
        if name == "group-disarm":
            return (*self.group_disarm(body, ctx),) + ({},)
        if name == "alert":
            return (*self.alert(body, ctx),) + ({},)
        return 404, {"error": "no such thing here"}, {}

    # -- the rig check (preroll.py) ------------------------------------------
    def preroll_view(self):
        """The latest rig check for every screen, or None without one.
        Display only; never raises."""
        pr = self.preroll
        if pr is None:
            return None
        try:
            v = pr.view()
        except Exception as e:                   # noqa: BLE001
            return {"state": "idle", "items": [], "text": "",
                    "error": f"{type(e).__name__}: {e}"}
        if v.get("cleared"):
            # A failure a later check cleared (Jeff's field report,
            # 2026-10-29): what to press now, in the same words on every
            # screen. Nothing starts by itself.
            v["cleared"]["say"] = self._cleared_say(v["cleared"])
        return v

    def _cleared_say(self, cleared=None):
        m = getattr(self.schedule, "machine", None)
        st = getattr(m, "state", None)
        held = (cleared or {}).get("shows", 1)
        if m is not None and m.delayed() is not None:
            # A delayed show waits for Start now, held or not (review A
            # round 2 of #176): Resume does not start it.
            return (f"Rig check passed. Press Start now for show "
                    f"{m.delayed().n}.")
        if st == "HOLD" and held:
            # Resume releases the show it cleared, which is then checked
            # again. A cleared alert alone ("could not run") leaves no show
            # for Resume to release (review 2 of #176): Start now, below.
            return "Rig check passed. Press Resume."
        # Not held: the show it failed for is never started by itself
        # after a failure, so Start now (it checks again).
        return "Rig check passed. Press Start now to start the show."


    PRECHECK_CLEAR_S = 60.0

    def preroll_check(self, body, ctx):
        """Pre-check, and Check again after a failure (the rack screen, an
        admin: the role matrix's Settings row, web.py): the same checks a
        show's pre-roll makes, with nothing started. It raises no alert of
        its own; a pass clears a failure in force (and its preroll alert),
        and a failure while one is in force refreshes it
        (preroll.PreRoll._clear_failure, _refresh_failure). Only between
        shows, with Run pressed, never in the minute before a show (its
        own pre-roll runs then) unless a failure is in force: a failed
        show is never checked again by itself (preroll.PreRoll.claim), so
        Check again is let through then. Returns at once; the result is
        in the status's "preroll"."""
        pr = self.preroll
        who = ctx.user or "unsigned"
        if pr is None:
            return 409, {"error": "The rig check runs only in the Fire & Ice "
                                  "engine with the scheduler performing. "
                                  "Nothing was changed."}
        svc = self.schedule
        m = getattr(svc, "machine", None)
        why = ""
        if m is not None and m.state in self.LIVE_STATES:
            why = "a show is live"
        elif getattr(self.control, "session", None) is None:
            why = ("Run has not been pressed; press Run on the Programming "
                   "screen first")
        elif self.programming_playback_refusal():
            why = "Programming is playing"
        elif pr.checking():
            why = "the rig is being checked already"
        elif svc is not None and svc.halting():
            why = "a Stop is under way"
        elif m is not None and m.next_slot() is not None:
            left = (m.next_slot().start - svc.clock()).total_seconds()
            if 0 <= left < self.PRECHECK_CLEAR_S + pr.window_s and \
                    not pr.failure_in_force():
                why = (f"show {m.next_slot().n} is due in {left:.0f} s and "
                       f"its own rig check runs by itself")
        if why:
            return 409, {"error": f"Check the rig was refused: {why}. "
                                  f"Nothing was changed."}
        self._precheck_n = getattr(self, "_precheck_n", 0) + 1
        key = ("pre-check", self._precheck_n)
        threading.Thread(target=pr.run, daemon=True,
                         name="ltcplay-precheck",
                         args=(key, f"a Pre-check by {who}"),
                         kwargs={"alerts": False, "manual": True,
                                 "by": f"Check again by {who}"}).start()
        return 200, {"ok": True, "text": "Checking the rig. Nothing is "
                                         "started, nothing is armed and "
                                         "nothing is played; the screens "
                                         "go black until the next show."}

    def preroll_continue(self, body, ctx):
        """Continue anyway after a failed rig check (Jeff, 2026-10-28): a
        signed-in person with the run permission (the role matrix's Show
        row, web.py) HOLDS the button: {"phase": "begin", "token"} when the
        finger goes down, {"phase": "done", "token"} once it has been held
        preroll.CONTINUE_HOLD_S, timed here, not by the page. A tap, an
        unsigned screen or no failed check does nothing. The show then
        starts now with every failed output left off for it
        (Service.continue_anyway)."""
        pr = self.preroll
        svc = self.schedule
        if pr is None or svc is None:
            return 409, {"error": "Continue anyway is only in the Fire & Ice "
                                  "engine with the rig check. Nothing was "
                                  "changed."}
        who = ctx.user or ""
        if not self.users_active() or not who:
            return 403, {"error": "Continue anyway needs a signed-in "
                                  "operator or admin. Nothing was changed."}
        token = str(body.get("token") or "")[:64]
        screen = ctx.screen if ctx.local else ctx.device
        r = pr.last_failed()
        if r is None:
            return 409, {"error": "There is no failed rig check to continue "
                                  "past. Nothing was changed."}
        if body.get("phase") == "begin":
            if not token:
                return 400, {"error": "No hold token. Nothing was changed."}
            pr.hold_begin(token, who)
            return 200, {"ok": True, "holding": True,
                         "hold_s": _preroll_hold_s()}
        if body.get("phase") != "done" or not pr.hold_done(token, who):
            return 409, {"error": "Continue anyway needs the button held "
                                  "until it fills; a tap does nothing. "
                                  "Nothing was changed."}
        got = svc.continue_anyway(who, screen)
        return (200 if got.get("ok") else 409), (
            got if got.get("ok") else {"error": got.get("text", "")})

    # -- alerts (alerts.py) ------------------------------------------------
    def alert(self, body, ctx):
        """POST /api/remote/alert. From this machine only: {"raise": kind,
        "text": sentence, "options": [...]} and {"clear": kind}, the
        Stream Deck process's and the scheduler's. From any page: {"id":
        n, "action": "acknowledge"}; an Acknowledge with no id clears
        every open alert (a deck key). Acknowledge changes nothing on the
        rig; the options that choose are refused with a sentence."""
        who, screen, _surface = self._actor(ctx, body)
        if "raise" in body or "clear" in body:
            if not ctx.local:
                return 403, {"error": "Alerts are raised on the show "
                                      "machine itself, not from here."}
            if "raise" in body:
                try:
                    a = self.alerts.raise_(body.get("raise"),
                                           body.get("text"),
                                           body.get("options") or
                                           ("Acknowledge",))
                except ValueError as e:
                    return 400, {"error": str(e)}
                return 200, {"text": f"Alert {a.kind} is up: {a.text}",
                             "alert": a.view()}
            ok, text = self.alerts.clear(kind=str(body.get("clear") or ""),
                                         action="Confirm", who=who,
                                         screen=screen, address=ctx.ip)
            return (200 if ok else 409), ({"text": text} if ok
                                          else {"error": text})
        action = str(body.get("action") or "").strip()
        aid = body.get("id")
        if aid is not None and (not isinstance(aid, int) or
                                isinstance(aid, bool)):
            return 400, {"error": "id has to be the alert's number."}
        ok, text = self.alerts.clear(id=aid, action=action or "Acknowledge",
                                     who=who, screen=screen, address=ctx.ip)
        if not ok:
            # A cleared alert journals itself (alerts.py). A press that
            # clears nothing (the alert already gone, a wrong id, an
            # option that chooses) used to leave no line at all, so an ACK
            # pressed on the deck could never be found in the journal
            # afterwards (the Pico, 2026-10-18). Who, when and what, every
            # time.
            label = ("Acknowledge" if (action or "").lower() == "acknowledge"
                     else action or "Acknowledge")
            self._journal(who, screen, "alert", "refused",
                          f"{label} pressed by "
                          f"{who or 'someone'} on the {screen or 'unknown screen'}"
                          f"{' for alert ' + str(aid) if aid is not None else ''}"
                          f" cleared nothing: {text}")
            return 409, {"error": text}
        return 200, {"text": text}

    def whoami(self, ctx):
        out = {"local": ctx.local, "device": ctx.device,
               "screen": ctx.screen, "fresh_s": FRESH_S}
        u = self.users
        if u is not None:
            s = getattr(ctx, "session", None) or {}
            out["user"] = s.get("name") or None
            out["role"] = s.get("role") or None
            try:
                out["users_active"] = u.active()
            except Exception:                    # noqa: BLE001
                out["users_active"] = False
            # The truth about this screen (Jeff's field report,
            # 2026-10-28): with users on and nobody signed in it is view
            # only, on this machine too; "may" is what the role matrix lets
            # this page do, the same answer the web layer gives a press.
            # Safety presses are not in it: they always may.
            from . import users as users_mod
            try:
                out["may"] = {p: bool(u.may(out["role"], p))
                              for p in users_mod.PERM_KEYS}
            except Exception:                    # noqa: BLE001
                out["may"] = {p: False for p in users_mod.PERM_KEYS}
            out["view_only"] = bool(out["users_active"] and
                                    not out["user"])
            if ctx.local:
                # Who the Stream Deck acts as: whoever is signed in on
                # this machine's rack screen, or nobody.
                try:
                    mu = u.machine_user()
                except Exception:                # noqa: BLE001
                    mu = None
                out["machine_user"] = (mu or {}).get("name") or None
        a = self.audit
        if a is not None:
            try:
                st = a.status()
                out["audit_fault"] = st["fault"] or (
                    f"{st['waiting_gap']} audit event(s) not written yet"
                    if st["waiting_gap"] else "")
            except Exception as e:               # noqa: BLE001
                out["audit_fault"] = f"{type(e).__name__}: {e}"
        return out

    # -- users and the audit log (users.py, audit.py, auditsend.py) ---------
    def user_view_for_deck(self):
        """deck-input's "user": who is signed in on this machine, and what
        the role matrix lets the deck's arm keys do for them (or for
        nobody signed in). None without users."""
        u = self.users
        if u is None:
            return None
        try:
            mu = u.machine_user()
            role = mu["role"] if mu else None
            # "active": users are on, so the deck names the signed-in user
            # (or unsigned) in its journal, never its chosen operator.
            return {"name": mu["name"] if mu else None, "role": role,
                    "active": bool(u.active()),
                    "may": {p: u.may(role, p) for p in ("arm", "show")}}
        except Exception:                        # noqa: BLE001
            # Who is signed in could not be worked out: the deck may not
            # arm or start on it (fail closed; review of PR #143, 5). Its
            # Stop, Hold and disarms never ask.
            return {"name": None, "role": None, "active": True,
                    "fault": True, "may": {"arm": False, "show": False}}

    def users_get(self, ctx):
        """GET users: what the sign-in card and the Users screen show. The
        names to pick from for anyone; the list, the matrix, the settings
        and the audit status only on the show machine itself."""
        u = self.users
        if u is None:
            return 404, {"error": "Users are not set up in this engine."}
        me = getattr(ctx, "session", None) or {}
        out = {"active": u.active(), "names": u.names(),
               "me": {"name": me.get("name"), "role": me.get("role")},
               "local": ctx.local}
        if ctx.local:
            out.update(u.view())
            out["may_admin"] = u.may(me.get("role"), "users")
            out["may_settings"] = u.may(me.get("role"), "settings")
            out["audit"] = self.audit_view()
        return 200, out

    def audit_view(self):
        """The Users screen's audit card: the writer, the chain, the upload."""
        out = {"writer": None, "chain": None, "upload": None}
        a = self.audit
        if a is not None:
            try:
                out["writer"] = a.status()
            except Exception as e:               # noqa: BLE001
                out["writer"] = {"fault": f"{type(e).__name__}: {e}"}
        up = self.uploader
        if up is not None:
            try:
                out["upload"] = up.status()
            except Exception as e:               # noqa: BLE001
                out["upload"] = {"last_error": f"{type(e).__name__}: {e}"}
        out["chain"] = getattr(self, "_chain_said", None)
        return out

    def audit_verify(self):
        """Every day file here, verified now (audit.verify_folder)."""
        from . import audit as audit_mod
        a = self.audit
        if a is None:
            return 404, {"error": "The audit log is not on in this engine."}
        a.flush(2.0)
        sent = audit_mod._load_sent(a.folder)
        ok, results, problems = audit_mod.verify_folder(
            a.folder, sent, today=audit_mod.local_day(self.wall()))
        said = {"ok": ok, "files": len(results),
                "lines": sum(r["count"] for r in results),
                "problems": problems[:20],
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(self.wall()))}
        self._chain_said = said
        return 200, said

    def users_post(self, name, body, ctx):
        """The sign-in and Users routes. Passwords pass through to users.py
        and are never journaled, logged or answered back."""
        u = self.users
        if u is None:
            return 404, {"error": "Users are not set up in this engine."}, {}
        from . import users as users_mod
        cookie = (f"{users_mod.COOKIE}=%s; HttpOnly; SameSite=Strict; "
                  f"Path=/")
        if name == "signin":
            ok, text, token, needs_new = u.sign_in(
                body.get("name"), body.get("password"), ctx.local,
                ctx.source, new_password=body.get("new_password"))
            if not ok:
                return 401, {"error": text, "needs_new_password": needs_new}, {}
            return 200, {"ok": True, "text": text}, {
                "Set-Cookie": cookie % token}
        if name == "signout":
            # The session ends at the engine, not just in the page: on this
            # machine every session made here ends with it (users.sign_out),
            # and any hold to arm under way on a screen is let go now
            # (Jeff's field report, 2026-10-28).
            ended = u.sign_out(getattr(ctx, "token", ""), ctx.source,
                               local=ctx.local)
            if ended:
                # A timed purge this sign-in started ends with it (Jeff,
                # 2026-10-29); the purge's own check on every send is the
                # belt.
                _who = ctx.user or "the person who started it"
                self.timed_purge_cut(f"{_who} signed out on the {ctx.device}",
                                     ctx.user, ctx.device,
                                     token=getattr(ctx, "token", "") or "")
            if ended:
                # Only this device's own hold (a hold belongs to the device
                # it came from), and only when a sign-in really ended: a
                # sign-out from somewhere else never lets go of another
                # screen's hold (review of PR #143, 1).
                try:
                    self.cancel_holds("the screen was signed out",
                                      device=ctx.device)
                except Exception:                # noqa: BLE001
                    pass
            return 200, {"ok": True, "text": "Signed out. This screen is "
                                             "view only now: Stop, Hold, "
                                             "Abort, Acknowledge and every "
                                             "disarm still work."}, {
                "Set-Cookie": (cookie % "") + "; Max-Age=0"}
        me = getattr(ctx, "session", None) or {}
        if name == "my-password":
            if not me.get("name"):
                return 403, {"error": "Sign in first."}, {}
            try:
                text = u.change_own_password(me["name"], body.get("old"),
                                             body.get("new"))
            except ValueError as e:
                return 400, {"error": str(e)}, {}
            return 200, {"ok": True, "text": text}, {}
        if name == "audit-verify":
            return (*self.audit_verify(),) + ({},)
        # Everything else changes users or settings: the show machine only.
        if not ctx.local:
            return 403, {"error": "Users and settings are changed on the "
                                  "show machine itself, not from here."}, {}
        perm = users_mod.REMOTE_ROUTES.get(name)
        if not u.may(me.get("role"), perm):
            return 403, {"error": f"Sign in as someone who may: "
                                  f"{users_mod.perm_label(perm)}. Nothing "
                                  f"was changed.", "role_refused": True}, {}
        try:
            if name == "users":
                op = str(body.get("op") or "")
                who = body.get("name")
                if op == "add":
                    text = u.add(who, body.get("role"), body.get("password"))
                elif op == "remove":
                    if str(who or "").lower() == str(me.get("name") or
                                                     "").lower():
                        raise ValueError("You cannot remove yourself while "
                                         "signed in.")
                    text = u.remove(who)
                elif op == "role":
                    text = u.set_role(who, body.get("role"))
                elif op == "password":
                    text = u.set_password(who, body.get("password"))
                elif op == "clear-password":
                    text = u.clear_password(who)
                else:
                    raise ValueError("op has to be add, remove, role, "
                                     "password or clear-password.")
            elif name == "role-matrix":
                text = u.set_matrix(body.get("matrix"))
            elif name == "clear-lockouts":
                text = u.clear_lockouts()
            elif name == "idle-signout":
                text = u.set_idle(body.get("minutes"))
            elif name == "last-show-signout":
                text = u.set_last_show(body.get("minutes"))
            elif name == "audit-settings":
                from . import auditsend
                ch = {k: body[k] for k in ("enabled", "url", "token", "time",
                                           "keep_days") if k in body}
                auditsend.save_config(self.folder, ch)
                text = "The audit upload settings were saved."
            elif name == "audit-upload-now":
                up = self.uploader
                if up is None:
                    raise ValueError("The audit upload is not running in "
                                     "this engine.")
                threading.Thread(target=up.run, kwargs={"force": True},
                                 daemon=True,
                                 name="ltcplay-audit-upload-now").start()
                text = ("Sending now, in the background. The status below "
                        "says how it went.")
            else:
                return 404, {"error": "no such thing here"}, {}
        except (ValueError, OSError) as e:
            return 400, {"error": str(e)}, {}
        return 200, {"ok": True, "text": text}, {}

    def firewall_view(self):
        """firewall_rule_state(), read at most every FIREWALL_READ_S."""
        now = self.clock()
        got = getattr(self, "_firewall_cache", None)
        if got is None or now - got[0] >= FIREWALL_READ_S:
            got = (now, firewall_rule_state(
                run=getattr(self, "_firewall_run", None),
                windows=getattr(self, "_firewall_windows", None),
                find=getattr(self, "_tool_find", None)))
            self._firewall_cache = got
        return got[1]

    def category_view(self, addr):
        """network_category(addr), read at most every FIREWALL_READ_S."""
        now = self.clock()
        got = getattr(self, "_category_cache", None)
        if got is None or got[1] != addr or now - got[0] >= FIREWALL_READ_S:
            got = (now, addr, network_category(
                addr, run=getattr(self, "_category_run", None),
                windows=getattr(self, "_firewall_windows", None),
                find=getattr(self, "_tool_find", None)))
            self._category_cache = got
        return got[2]

    def network_view(self):
        """The Show network card on the show PC (GET network, LOCAL_ONLY):
        the address saved, whether this engine listens on it, the firewall
        rule, the address to type on another machine, and whether a
        restart of the engine is needed. Read only."""
        try:
            saved = load_settings(self.folder)
            err = None
        except ValueError as e:
            saved, err = {}, str(e)
        addr = saved.get("show_network_address")
        listen = getattr(self, "listen", None) or {}
        addrs = list(listen.get("addresses") or [])
        port = listen.get("port") or FIXED_PORT
        on_saved = bool(addr) and addr in addrs
        fw_state, fw_text = self.firewall_view()
        lines = []
        if err:
            lines.append(err)
        elif not addr:
            lines.append("No show network address is saved, so only this "
                         "PC can open the page. Pick this PC's address on "
                         "the show network below and save it.")
        else:
            lines.append(f"Saved address: {addr}.")
        if addrs:
            lines.append("The engine is listening on " +
                         " and ".join(f"{a}:{port}" for a in addrs) + ".")
        else:
            lines.append("Where the engine is listening is not known.")
        why = str(listen.get("why") or "")
        if why:
            lines.append(why)
        restart = None
        if addr and not on_saved and addrs:
            reason = self.reload_engine_refusal()
            restart = {"needed": True, "offered": reason is None,
                       "why": reason or ""}
            lines.append(f"The engine is not listening on {addr} yet. "
                         f"Restart the engine between shows to use it.")
        lines.append(fw_text)
        category = None
        if fw_state != "none":
            if port != FIXED_PORT:
                # The installer's rule opens 7878 only (review of #164).
                lines.append(f"The engine runs on port {port}, but the "
                             f"firewall rule opens port {FIXED_PORT} only, "
                             f"so other machines are blocked. Set \"port\" "
                             f"back to {FIXED_PORT} in showpc.json.")
            if addr:
                category, cat_text = self.category_view(addr)
                if cat_text:
                    lines.append(cat_text)
        url = page_url(addr, port) if on_saved else None
        if url and fw_state in ("present", "none"):
            lines.append(f"On your Mac or iPad, on the same network, type "
                         f"{url}")
        elif url:
            lines.append(f"Once the firewall rule is there, type {url} on "
                         f"your Mac or iPad, on the same network.")
        return {"saved": addr, "error": err, "port": port,
                "candidates": address_candidates(),
                "listening": addrs, "on_saved": on_saved,
                "firewall": {"state": fw_state, "text": fw_text},
                "category": category,
                "url": url, "restart": restart, "lines": lines}

    def set_network(self, body):
        try:
            addr = check_show_address(body.get("address"))
            save_settings(self.folder, show_network_address=addr)
        except (ValueError, OSError) as e:
            return 400, {"error": str(e)}
        view = self.network_view()
        if view["on_saved"]:
            note = "Saved. The engine is already listening on it."
        elif os.path.isfile(os.path.join(settings_folder(), SHOWPC_FILE)):
            note = ("Saved. The engine starts listening on it after a "
                    "restart: press Restart the engine between shows.")
        else:
            note = ("Saved. Quit the Web ltcplay window and start it again "
                    "on the show network to use it.")
        return 200, dict(view, ok=True, note=note)

    def ui_default(self):
        """1 or 2: the page /remote serves a screen with no ltcplay_ui
        cookie of its own (web.py). 1 whenever the settings file cannot be
        read: the page every screen had."""
        try:
            v = load_settings(self.folder).get("ui_default")
        except Exception:                        # noqa: BLE001
            return 1
        return v if (isinstance(v, int) and not isinstance(v, bool)
                     and v in UI_VERSIONS) else 1

    def set_ui_default(self, body, ctx):
        """POST ui-default {"default": 1|2}, on the show machine only (the
        role matrix's Settings, users.py). Only the JSON numbers 1 and 2;
        anything else changes nothing. Changes which page a screen that has
        not chosen gets; nothing on the rig."""
        v = body.get("default")
        if not isinstance(v, int) or isinstance(v, bool) or \
                v not in UI_VERSIONS:
            return 400, {"error": "default has to be 1 or 2. Nothing was "
                                  "changed."}
        # Who: the signed-in person or "unsigned", never a name in the body.
        who = ctx.user or "unsigned"
        screen = ctx.screen
        try:
            save_settings(self.folder, ui_default=v)
        except ValueError as e:
            return 409, {"error": f"{e} The default interface was not "
                                  f"changed."}
        except OSError as e:
            return 500, {"error": f"ltcplay_remote.json could not be "
                                  f"written ({e}). The default interface "
                                  f"was not changed."}
        self._journal(who, screen, "interface default", str(v),
                      f"{who} set the default interface to {v}.0 on the "
                      f"{screen}.")
        return 200, {"ok": True, "default": v,
                     "text": f"Screens that have not chosen now get {v}.0. "
                             f"Each screen can still switch for itself."}

    def set_screen_arming(self, body, ctx):
        """POST screen-arming {"on": true|false}, on the show machine only
        (LOCAL_ONLY; the role matrix's Settings to turn it on, users.py).
        Only the JSON words true and false: anything else changes nothing
        (the lesson of PR #84). Turning it off lets every screen hold go."""
        on = body.get("on")
        if on is not True and on is not False:
            return 400, {"error": "on has to be true or false. Nothing was "
                                  "changed."}
        # Who: the signed-in person or "unsigned", never a name the caller
        # wrote in the body (review of PR #90, P3-4).
        who, screen = (ctx.user or "unsigned"), ctx.screen
        try:
            save_settings(self.folder, screen_arming=on)
        except ValueError as e:
            # Nothing was written (save_settings checks before writing).
            return 409, {"error": f"{e} Arming from a screen was not "
                                  f"changed."}
        except OSError as e:
            return 500, {"error": f"ltcplay_remote.json could not be "
                                  f"written ({e}). Arming from a screen "
                                  f"was not changed."}
        if not on:
            self.cancel_holds("arming from a screen was switched off")
            # A purge selection made from a device on the show network
            # needed this switch: it goes too (review of PR #90, P3-6).
            m = self.morning
            pg = getattr(m, "purge", None) if m is not None else None
            try:
                if pg is not None and pg.owner and \
                        pg.owner != "Rack screen":
                    pg.clear("arming from a screen was switched off")
            except Exception:                    # noqa: BLE001
                pass
        text = (f"Arming from a screen is ON. {SCREEN_ARMING_ENABLES}"
                if on else
                "Arming from a screen is OFF. Arm from the Stream Deck.")
        self._journal(who, screen, "screen arming",
                      "on" if on else "off",
                      f"{who} switched arming from a screen "
                      f"{'ON' if on else 'OFF'} on the {screen}.")
        return 200, {"ok": True, "enabled": on, "text": text}

    def _booth_notes(self):
        """How many of the recorded booth notes this machine has, for the
        rack screen's booth line (boothaudio.notes_view): the booth
        player's own files and folders, or with no player the folders a
        note would be looked for in. None when it cannot be said."""
        try:
            from . import boothaudio
            view = getattr(self.alerts.booth, "notes", None)
            if callable(view):
                return view()
            return boothaudio.notes_view(
                {}, boothaudio.note_folders(self.folder))
        except Exception:                        # noqa: BLE001
            return None

    def _alert_tick(self):
        """After each scheduler tick, on the scheduler's thread: wakes the
        booth worker and returns at once. The work itself (the booth
        re-sound's device lookup and file read) is the worker's, so a slow
        disk or device never stalls a scheduler tick (review of PR #133,
        4)."""
        if self._alert_stop.is_set():
            return
        self._booth_wake.set()
        with self._alert_lock:
            self._start_alert_workers()

    def _start_alert_workers(self):
        """Called with _alert_lock held: one booth worker, ever, whichever
        thread ticks first (a second one could sound a note twice)."""
        if self._booth_thread is None:
            self._booth_thread = threading.Thread(
                target=self._worker, daemon=True,
                args=(self._booth_wake, self._booth_work),
                name="ltcplay-alert-booth")
            self._booth_thread.start()

    def _worker(self, wake, work):
        while True:
            wake.wait()
            wake.clear()
            if self._alert_stop.is_set():
                return
            work()

    def close_alerts(self):
        """Stop the alert worker (web.serve's server_close)."""
        self._alert_stop.set()
        self._booth_wake.set()

    def _booth_work(self):
        """The booth note again for alerts still open. Never raises."""
        self._frame_timing_work()
        self._follow_work()
        try:
            self.alerts.tick()
        except Exception:                        # noqa: BLE001
            pass

    def rig_health(self):
        """righealth.summary() of this engine as it is now: (sentence,
        items). Display only: reads what the rack screen already shows.
        Never raises."""
        from . import righealth
        try:
            try:
                cs = self.control.state() or {}
            except Exception:                    # noqa: BLE001
                cs = {}
            try:
                fl = self.flame_status.view() if self.flame_status else None
            except Exception:                    # noqa: BLE001
                fl = None
            clk = cs.get("clock")
            if isinstance(clk, dict):
                # Where a held frame can go (TimecodeOut.held_dests): never
                # said to be held for MadMapper when it cannot be.
                clk = dict(clk, held_to=None)
                try:
                    out = getattr(getattr(self.control.session, "clock",
                                          None), "out", None)
                    held = getattr(out, "held_dests", None)
                    if callable(held):
                        clk["held_to"] = [str(lb) for lb, _ip in held()]
                except Exception:                # noqa: BLE001
                    pass
            return righealth.summary(
                lamps=self.subsystems(cs), clock=clk,
                madmapper=self.madmapper_link(False),
                beyond=self.beyond_link(), flames=fl,
                announce=self.announce_view())
        except Exception as e:                   # noqa: BLE001
            return (f"The rig's state could not be read "
                    f"({type(e).__name__}).", [])

    def _reset_health(self, r, who, screen):
        """A Reset that went through says, in plain words, how every
        subsystem stands (Jeff's field report, 2026-10-28), in its answer
        and in the journal. A refused Reset is answered as before. Never
        raises."""
        try:
            if not isinstance(r, dict) or not r.get("ok"):
                return r
            text, items = self.rig_health()
            said = str(r.get("text") or "").strip()
            if said.startswith("Reset."):
                said = said[len("Reset."):].strip()
            out = dict(r, text=" ".join(x for x in ("Reset done.", said,
                                                    text) if x),
                       health=items)
            self._journal(who, screen, "reset", "rig state",
                          f"After {who or 'someone'}'s Reset on the "
                          f"{screen}: {text}",
                          fault=any(i.get("ok") is False for i in items))
            return out
        except Exception:                        # noqa: BLE001
            return r

    def _follow_work(self):
        """Is MadMapper following the show's timecode (madmapperwatch.
        FollowCheck)? On the alert worker, after each scheduler tick: a
        display only "follow" alert (alerts.DISPLAY_ONLY: no booth note,
        no pulse, never holds a show, never on the Stream Deck) once the
        timecode has moved for the heartbeat block's follow_s with no
        heartbeat from MadMapper, cleared when the heartbeat arrives.
        Only with a "madmapper.heartbeat" block; never while MadMapper is
        not running (its own alert). Sends nothing anywhere. Never
        raises."""
        try:
            from . import madmapperwatch as mmw
            wd = self.madmapper_heartbeat
            cfg = self.madmapper_heartbeat_cfg
            if wd is None or cfg is None:
                return
            if self._follow is None:
                self._follow = mmw.FollowCheck(clock=self.clock)
            fc = self._follow
            aid = self._follow_alert_id
            if fc.raised and aid is not None and \
                    not any(a.id == aid for a in self.alerts.open()):
                fc.dropped()
                self._follow_alert_id = None
            _length, pos = mmw.show_reading(
                getattr(self.control, "session", None))
            w = self.madmapper_watch
            link = w.state().get("link") if w is not None else "unknown"
            can = not getattr(wd, "bind_error", None) and link != "lost"
            what = fc.step(pos, wd.heard_age_s(),
                           float(getattr(cfg, "timeout_s", 3.0) or 3.0),
                           float(getattr(cfg, "follow_s", 5.0) or 5.0), can)
            if what == "raise":
                a = self.alerts.raise_("follow", mmw.FOLLOW_TEXT)
                self._follow_alert_id = getattr(a, "id", None)
            elif what == "clear":
                aid, self._follow_alert_id = self._follow_alert_id, None
                self._journal("system", "engine", "video", "following",
                              "MadMapper is following timecode again: its "
                              "heartbeat arrived.")
                if aid is not None and any(a.id == aid
                                           for a in self.alerts.open()):
                    # Only the one raised here, by its id.
                    self.alerts.clear(id=aid, who="LTC Player",
                                      action="Confirm", screen="engine")
        except Exception:                        # noqa: BLE001
            pass

    def _flame_link(self):
        get = self.flame_link_get
        try:
            return get() if get is not None else None
        except Exception:                        # noqa: BLE001
            return None

    TIMING_TEXT = ("Flame frames late: {n} in the last hour, worst {w} ms "
                   "between two frames to flamesafe. flamesafe holds a fire "
                   "value for {lim} ms after the last frame (its "
                   "fire_hold_ms), so a flame pulse may have been cut "
                   "short. The show carries on; nothing was changed.")

    def _frame_timing_work(self):
        """Flame frames late (flamelink.FrameTiming, 2026-10-29): on the
        alert worker, a display only "timing" alert (alerts.DISPLAY_ONLY:
        shown and journaled, no booth note, never holds a show) each time
        the last hour has more frames over flamesafe's fire_hold_ms than
        when it was last raised. It stays until a person acknowledges it,
        like every alert. Sends nothing anywhere. Never raises."""
        try:
            link = self._flame_link()
            if link is None:
                return
            n, worst = link.late_last_hour()
            if n > self._timing_said:
                # The limit from the link itself: this module never
                # imports the flame link (the GPL wall).
                lim = (link.timing_view() or {}).get("limit_ms", 100)
                self.alerts.raise_("timing", self.TIMING_TEXT.format(
                    n=n, w=f"{worst:.0f}", lim=lim))
            self._timing_said = n
        except Exception:                        # noqa: BLE001
            pass

    # -- announcements (announce.py) ----------------------------------------
    ANNOUNCE_OFF = ("Announcements are not running in this engine. LTC "
                    "Player starts them with ltcplay_announce.json in the "
                    "settings folder; restart LTC Player once it is there.")

    @property
    def announce(self):
        return self._announce

    @announce.setter
    def announce(self, ann):
        """Attach the announcements service: its view is read once now (at
        startup, not on a request) and then every ANNOUNCE_VIEW_S on a
        thread of its own."""
        self._announce = ann
        self._ann_cache = None
        if ann is None:
            return
        self._ann_refresh()
        if self._ann_thread is None:
            self._ann_thread = threading.Thread(
                target=self._ann_loop, daemon=True,
                name="ltcplay-announce-view")
            self._ann_thread.start()

    def _ann_loop(self):
        while True:
            time.sleep(ANNOUNCE_VIEW_S)
            if self._announce is not None:
                self._ann_refresh()

    @staticmethod
    def _ann_view_from(st):
        return {"configured": True, "ok": bool(st.get("ok")),
                "error": st.get("error"), "device": st.get("device"),
                "device_set": bool(st.get("device_set")),
                "device_available": bool(st.get("device_available")),
                "device_reason": st.get("device_reason"),
                "hostapi": st.get("hostapi"),
                "route": st.get("route"),
                "route_ok": bool((st.get("route") or {}).get("ok")),
                "last_sent": st.get("last_sent"),
                "over_show": bool(st.get("over_show")),
                "held_failed": st.get("held_failed"),
                "items": [{k: i.get(k) for k in
                           ("id", "name", "available", "missing", "reason",
                            "length_s")}
                          for i in st.get("announcements") or []],
                "playing": st.get("playing")}

    def _ann_refresh(self, st=None):
        """The view, from the service's own status (which takes its lock
        and asks the audio system for the device): only ever called off
        the status and deck-input paths (the view thread, attaching, and a
        press's own route). Never raises."""
        ann = self._announce
        if ann is None:
            return
        try:
            v = self._ann_view_from(st if st is not None else ann.status())
        except Exception as e:                   # noqa: BLE001
            # Not a fact about what plays: stale, so nothing reads it as
            # "nothing is playing" (final review of #129).
            v = {"configured": True, "ok": False, "stale": True,
                 "error": f"Announcement status unavailable: it could not "
                          f"be read ({type(e).__name__}: {e}).",
                 "items": [], "playing": None}
        self._ann_cache = (self.clock(), v)

    def announce_view(self):
        """The rack screen's and the Stream Deck's view of the three
        announcements: each one ready, or "recording missing", or why not;
        the output device; what is playing and whether it Held the
        schedule; a Hold whose announcement then failed. The last snapshot
        the view thread made: never the service's lock, never the audio
        system, on this path. Display only; never raises."""
        if self._announce is None:
            return {"configured": False, "why": self.ANNOUNCE_OFF}
        c = self._ann_cache
        if c is None:
            return {"configured": True, "ok": False, "stale": True,
                    "error": "Announcement status unavailable: still being "
                             "read.", "items": [], "playing": None}
        age = max(0.0, self.clock() - c[0])
        v = dict(c[1], age_s=round(age, 1))
        if age > ANNOUNCE_STALE_S:
            # The view thread has not refreshed it (a hung audio query, a
            # stuck lock): never a stale PLAYING or ready (review of PR
            # #129, second round, 7). Stop stays pressable.
            v.update(ok=False, stale=True, playing=None, held_failed=None,
                     error=f"Announcement status unavailable: not read for "
                           f"{age:.0f} s.")
        return v

    def announce_hold(self, sched):
        """The hold_requester web.serve gives the announcements: the
        scheduler's hold_for_announcement (which, under its own lock and
        after its own tick, holds nothing while a show is running), then,
        only when the schedule really is on Hold, what the operator's own
        Hold does for safety at once: live flame programming
        cut, every laser disarmed (press(); review of PR
        #129, second round, 3). A running show is never touched."""
        def hold(who, screen, detail=None):
            refusal, epoch = sched.hold_for_announcement(who, screen,
                                                         detail=detail)
            if refusal:
                return refusal, epoch
            # The state checked and the cuts made under the scheduler's
            # own lock (re-entrant; none of them waits): a Start now
            # landing between the Hold and here leaves a running show
            # untouched (delta review of #129, 3).
            locked = getattr(sched, "_locked", None)
            held = False
            with (locked() if locked is not None
                  else contextlib.nullcontext()):
                m = getattr(sched, "machine", None)
                if m is not None and m.state == "HOLD":
                    held = True
                    why = (f"{who or 'someone'} held the schedule for an "
                           f"announcement on the {screen}")
                    self.timed_purge_cut(why, who, screen)
                    self.live_flame_cut(why, who, screen)
                    self._lasers_disarm_now(who, screen, "announcement")
            if held:
                # Programming playback on the show audio (playhead.py,
                # #142): the operator's Hold is its Pause, so the music
                # freezes rather than play on under the announcement. The
                # same hook the Hold press uses, outside the scheduler's
                # lock. A scheduled show is never this (programming()).
                self._announce_pauses_programming(who, screen)
            return refusal, epoch
        return hold

    def _announce_pauses_programming(self, who, screen):
        """Pause Programming playback for an announcement's Hold, as the
        Hold press does (remote.press, playhead.hold_press). Never
        raises: the announcement still plays if the pause fails, and the
        journal says so."""
        ph = getattr(self, "playhead", None)
        try:
            if ph is None or not (self.programming()[0] and ph.active()):
                return
        except Exception:                        # noqa: BLE001
            return
        try:
            said = ph.hold_press(who, screen)
            fault = False
        except Exception as e:                   # noqa: BLE001
            said = (f"Programming playback did NOT pause "
                    f"({type(e).__name__}: {e}). Press Pause on the "
                    f"Programming screen.")
            fault = True
        try:
            self._journal(who, screen, "hold", "fault" if fault else "done",
                          f"An announcement held the schedule on the "
                          f"{screen}. {said}", fault=fault)
        except Exception:                        # noqa: BLE001
            pass

    def _ann_press_begin(self, name):
        """The laser arm told a Hold-like press is under way (a Play), or
        None. Never raises."""
        la = getattr(self.morning, "laser_arm", None) \
            if name == "announce-play" else None
        if la is None:
            return None
        try:
            la.press_begin()
            return la
        except Exception:                        # noqa: BLE001
            return None

    @staticmethod
    def _ann_press_end(la):
        if la is None:
            return
        try:
            la.press_end()
        except Exception:                        # noqa: BLE001
            pass

    def announce_post(self, name, body, ctx):
        """POST announce-play {"id"} and announce-stop: one press each, from
        the show machine (the rack screen, or the Stream Deck process, which
        names itself and its chosen operator). Who is the signed-in person,
        the deck's operator, or the surface itself. Every refusal is a
        sentence; nothing here ever disarms, stops or latches anything, and
        Stop ends only the announcement's audio (the schedule's Hold, if it
        held one, stays until Resume)."""
        if not ctx.local:
            return 403, {"error": "Announcements are played from the show "
                                  "machine itself (the rack screen or the "
                                  "Stream Deck), not from here. Nothing was "
                                  "changed."}
        ann = self.announce
        if ann is None:
            return 409, {"error": self.ANNOUNCE_OFF + " Nothing was "
                                                      "changed."}
        who, screen, surface = self._actor(ctx, body)
        if not who:
            who, surface = screen, True
        if name == "announce-play":
            # A Play acts on what the page shows (which recordings are
            # there, what is playing): refused on a stale page, like Start
            # now. Stop only takes something away and is never refused.
            why = self._stale(body)
            if why:
                return 409, {"error": why, "stale": True}
        # A Play may put the schedule on Hold, and a Hold disarms every
        # laser: the laser arm is told a Hold-like press is under way before
        # any work and until its answer is made, exactly as for the
        # operator's own Hold (press(); review of PR #87, P2-2), so an arm
        # confirmed in that window is refused.
        arm = self._ann_press_begin(name)
        try:
            if name == "announce-play":
                st = ann.play(str(body.get("id") or ""), who, screen,
                              surface=surface)
                p = st.get("playing") or {}
                text = (f"{p.get('name') or 'The announcement'} was sent: "
                        f"{sent_words(p.get('sent'))}. "
                        + ("The schedule is held for it; press Resume when "
                           "you are ready to carry on."
                           if p.get("held_schedule") else
                           p.get("note") or
                           "It plays over the show; nothing was paused or "
                           "stopped."))
            else:
                st = ann.stop(who, screen, surface=surface)
                text = ("The announcement was stopped. Nothing else was "
                        "changed.")
        except ValueError as e:
            self._ann_refresh()
            return 409, {"error": str(e), "announce": self.announce_view()}
        except Exception as e:                   # noqa: BLE001
            self._ann_refresh()
            return 500, {"error": f"The announcement press failed "
                                  f"({type(e).__name__}: {e}). Nothing "
                                  f"else was changed."}
        finally:
            self._ann_press_end(arm)
        self._ann_refresh(st if isinstance(st, dict) else None)
        return 200, {"ok": True, "text": text,
                     "announce": self.announce_view()}

    def announce_outputs(self):
        """GET announce-outputs: the picker's list, every output on this PC
        with its audio system and channel count, and which one is picked
        (Jeff's field report, 2026-10-28, item 10). The show machine only.
        Display only: picking is announce-output."""
        ann = self.announce
        if ann is None:
            return 409, {"error": self.ANNOUNCE_OFF}
        try:
            outs = ann.outputs()
        except ValueError as e:
            return 409, {"error": str(e)}
        except Exception as e:                   # noqa: BLE001
            return 500, {"error": f"The outputs could not be listed "
                                  f"({type(e).__name__}: {e})."}
        return 200, {"outputs": outs, "device": ann.device_name or "",
                     "hostapi": ann.options.get("hostapi")}

    def announce_output(self, body, ctx):
        """POST announce-output {"device": name, "hostapi": name or null}:
        the announcement's own output, used while the show audio is not
        running, saved to ltcplay_announce.json and used at once. The show
        machine only (LOCAL_ONLY), Settings permission (users.py), the
        same as the other settings here. "" clears it."""
        ann = self.announce
        if ann is None:
            return 409, {"error": self.ANNOUNCE_OFF + " Nothing was "
                                                      "changed."}
        who = ctx.user or "unsigned"
        try:
            text = ann.save_device(body.get("device"), body.get("hostapi"),
                                   who=who, screen=ctx.screen)
        except ValueError as e:
            return 409, {"error": str(e)}
        except OSError as e:
            return 500, {"error": f"ltcplay_announce.json could not be "
                                  f"written ({e}). Nothing was changed."}
        self._ann_refresh()
        return 200, {"ok": True, "text": text,
                     "announce": self.announce_view()}

    def announce_test_tone(self, ctx):
        """POST announce-test-tone: 1 s of a soft 440 Hz at -20 dBFS through
        the route and output an announcement would use now (Jeff's field
        report, 2026-10-28), so the route is checked with no recording.
        The show machine only (LOCAL_ONLY), Settings permission; never
        during a show; never Holds anything."""
        ann = self.announce
        if ann is None:
            return 409, {"error": self.ANNOUNCE_OFF + " Nothing was "
                                                      "changed."}
        who = ctx.user or "unsigned"
        try:
            st = ann.test_tone(who, ctx.screen or "Rack screen")
        except ValueError as e:
            self._ann_refresh()
            return 409, {"error": str(e), "announce": self.announce_view()}
        except Exception as e:                   # noqa: BLE001
            self._ann_refresh()
            return 500, {"error": f"The test tone failed ({type(e).__name__}"
                                  f": {e}). Nothing else was changed."}
        self._ann_refresh(st)
        p = st.get("playing") or {}
        return 200, {"ok": True,
                     "text": f"Test tone sent: {sent_words(p.get('sent'))}. "
                             f"Listen for it; nothing was held.",
                     "announce": self.announce_view()}

    # -- presses -----------------------------------------------------------
    def _stale(self, body):
        """A sentence when the page's status is too old for this press."""
        try:
            seen = float(body.get("seen"))
        except (TypeError, ValueError):
            return ("The page has not shown a status yet. Wait for it to "
                    "connect, then press again. Nothing was changed.")
        age = self.wall() - seen / 1000.0
        if age > FRESH_S or age < -FRESH_S:
            return (f"The page was showing a status {age:.1f} s old, so "
                    f"this press was refused. Wait for it to reconnect, "
                    f"then press again. Nothing was changed.")
        return None

    def press(self, name, body, ctx):
        """A show control press. An Abort or a Hold tells the laser arm it
        is under way before ANY other work and until its answer is made
        (review of PR #87, P2-2): an arm confirmed in that window, before
        the scheduler's latch or Hold is set, is refused."""
        arm = getattr(self.morning, "laser_arm", None) \
            if name in ("abort", "hold") else None
        if arm is not None:
            try:
                arm.press_begin()
            except Exception:                    # noqa: BLE001
                arm = None
        try:
            return self._press(name, body, ctx)
        finally:
            if arm is not None:
                try:
                    arm.press_end()
                except Exception:                # noqa: BLE001
                    pass

    def _press(self, name, body, ctx):
        # The press's place in line, taken before anything else (an Abort's
        # disarm and laser blank, a lock): the scheduler compares a Reset's
        # with the latest Abort's, so work done first never reorders them.
        stamp = getattr(self.schedule, "stamp_press", None)
        pressed = stamp() if stamp is not None else None
        who, screen, surface = self._actor(ctx, body)
        if name == "stop":
            # Only takes risk away: never refused for a stale page or a
            # missing confirm, from any surface (review of #144, F7).
            return self.stop_press(who, screen)
        if name in FRESH_ROUTES:
            why = self._stale(body)
            if why:
                self._journal(who, screen, name, "refused",
                              f"{who or 'Someone'}'s {name} on the {screen} "
                              f"was refused. {why}")
                return 409, {"error": why, "stale": True}
        if name == "start-now":
            why = self.programming_playback_refusal()
            if why:
                self._journal(who, screen, name, "refused",
                              f"{who or 'Someone'}'s Start now on the "
                              f"{screen} was refused. {why}")
                return 409, {"ok": False, "error": why, "text": why}
        flames = None
        if name == "abort":
            # The flames first, every time (review of PR #43, P0-1): every
            # flame group disarmed through the flame link before any
            # scheduler step, any lock or any journal line, whatever the
            # scheduler is doing and whether or not a show is live. The
            # scheduler takes an Abort only in SHOW or PAUSED, and the
            # conductor only while something plays; a group armed before a
            # show, between shows or after one must still come off. Nothing
            # here waits on a disk (P0-5). Before the confirm check too
            # (safety audit of bench-build, P2 b): a disarm only removes
            # risk, so an unconfirmed Abort still takes the flames off; the
            # rest of it is refused below.
            flames = self._disarm_now(who, screen, "Abort")
        if name in ("abort", "hold", "start-now"):
            # A timed purge ends at the press too (Jeff, 2026-10-29), from
            # every surface, before the confirm check: it only takes risk
            # away. The flame link's purge frame is cleared on this thread.
            _lbl = {"abort": "Abort", "hold": "Hold",
                    "start-now": "Start now"}[name]
            self.timed_purge_cut(f"{who or 'someone'} pressed {_lbl} on the "
                                 f"{screen}", who, screen)
            # Live flame programming ends at the press, from every surface,
            # before the confirm check and before the scheduler: it only
            # takes risk away. Only the flame link's program slot is shut
            # here (cheap, no disk, no wait: review of PR #96, P3-3); the
            # journal line, the audit line and the disarm of every group go
            # on a thread of their own, after the lasers below.
            self.live_flame_cut(f"{who or 'someone'} pressed {name} on the "
                                f"{screen}", who, screen)
        if name == "abort":
            # The hazers OFF at the press (Jeff, 2026-10-29: "Abort turns
            # off the hazers"), before the confirm check: it only takes
            # something away. The flame link's disarm_all above did it at
            # the link already; this is the belt, and the journal line.
            self.hazers_cut(f"{who or 'someone'} pressed Abort on the "
                            f"{screen}", who, screen)
            # The fuel pump OFF at the press too (Jeff, 2026-10-30), the
            # same way: the link's disarm_all did it already; the belt.
            self.pump_cut(f"{who or 'someone'} pressed Abort on the "
                          f"{screen}", who, screen)
        if name in ("abort", "hold"):
            # Every laser disarmed at the press too (Jeff, 2026-10-22,
            # ruling 3), from every surface, before the confirm check and
            # before the scheduler: only the arm's state changes on this
            # thread; the blank and BEYOND's DisableLaserOutput go on the
            # arm's own sender, so nothing here waits on BEYOND.
            self._lasers_disarm_now(who, screen, name)
        if name in CONFIRM_ROUTES and body.get("confirmed") is not True:
            if flames is not None and flames[0] is not None:
                why = ("Confirm it on the page first. Every flame group was "
                       "sent a disarm; nothing else was changed.")
            else:
                why = "Confirm it on the page first. Nothing was changed."
            self._journal(who, screen, name, "refused",
                          f"{who or 'Someone'}'s {name} on the {screen} was "
                          f"refused. {why}")
            return 400, {"error": why}
        if name == "abort":
            # And the lasers, the same way (re-review P1-A): blanked here,
            # before the scheduler's step and its save, not after them.
            lasers = self._lasers_dark_now()
        if name in ("abort", "disarm-all"):
            # No screen hold survives an Abort or a disarm, whoever pressed
            # it and whether or not it goes on to be accepted.
            self.cancel_holds(f"{who or 'someone'} pressed {name} on the "
                              f"{screen}")
            # Nor a purge or a laser test (the Morning and Shutdown page):
            # Abort wins over everything, before the scheduler is asked.
            m = self.morning
            if m is not None and name == "abort":
                try:
                    m.on_abort(who, screen)
                except Exception:
                    pass
        if name == "disarm-all":
            # Disarm all flame groups (2026-10-23): the purge selection is
            # cleared first (in memory only), so the deck cannot want a
            # purge group again right after the disarm; then the disarm;
            # the laser calibration tests end only after it, off its path.
            m = self.morning
            purge = getattr(m, "purge", None)
            if purge is not None:
                try:
                    purge.clear(f"Disarm all flame groups pressed by "
                                f"{who or screen or 'someone'}")
                except Exception:
                    pass
            out = self.disarm_all(who, screen)
            tests = getattr(m, "lasers", None)
            if tests is not None:
                try:
                    tests.all_off("Disarm all flame groups pressed")
                except Exception:
                    pass
            return out
        # Programming playback on the show audio (review of #142): Hold is
        # its Pause and Abort its Stop, after every disarm above, and no
        # Start now goes over it. A scheduled show is never this
        # (programming() refuses it, and active() is only the very cue
        # Play from started).
        ph = self.playhead if name in ("hold", "abort") else None
        if ph is not None and not (self.programming()[0] and ph.active()):
            ph = None
        if name == "abort":
            code, out = self.abort(who, screen, flames, lasers, pressed)
            if ph is not None:
                stopped = True
                try:
                    said = ph.abort_press(who, screen)
                except Exception as e:              # noqa: BLE001
                    stopped = False
                    said = (f"Programming playback did NOT stop "
                            f"({type(e).__name__}: {e}). Press Stop the "
                            f"show audio, or Stop.")
                self._journal(who, screen, "abort",
                              "done" if stopped else "fault",
                              f"{who or 'Someone'} pressed Abort on the "
                              f"{screen}. {said}", fault=not stopped)
                # No scheduled show was live to stop: the answer says what
                # did stop, the Programming playback.
                text = " ".join(t for t in (flames[1] if flames else "",
                                            lasers[1], said) if t)
                ok = stopped and not (flames and flames[0] is False)
                out = dict(out, programming=said, stopped=stopped, ok=ok,
                           text=text)
                out.pop("error", None)
                if not ok:
                    out["error"] = text
                code = 200 if ok else 409
            return code, out
        if ph is not None:                          # hold
            # Jeff's ruling: Hold is a schedule pause. The schedule's own
            # Hold first (so the next show does not start until Resume),
            # and its conductor step let land; then the Programming Pause,
            # so the music freezes (the schedule's Hold alone only takes
            # the rig out of the show). Continue does not lift the
            # schedule's Hold; Resume does.
            svc = self.schedule
            m = getattr(svc, "machine", None)
            sched = ""
            # A Start now waiting for the show audio never outlives a Hold
            # (#144), whatever state the schedule is in.
            cancel = getattr(svc, "cancel_start_wait", None)
            if cancel is not None:
                try:
                    cancel(f"Hold was pressed on the {screen}")
                except Exception:                   # noqa: BLE001
                    pass
            if svc is not None and getattr(m, "state", None) in ("IDLE",
                                                                 "STANDBY"):
                try:
                    r = svc.operator_press("hold", who, screen,
                                           pressed=pressed, surface=surface)
                    sched = str(r.get("text") or "")
                except ValueError as e:
                    sched = str(e)
                flush = getattr(svc, "flush_conductor", None)
                if flush is not None:
                    try:
                        flush(2.0)
                    except Exception:               # noqa: BLE001
                        pass
                ph.settle_conductor()
            try:
                said = ph.hold_press(who, screen)
            except Exception as e:                  # noqa: BLE001
                self._journal(who, screen, "hold", "refused",
                              f"{who or 'Someone'}'s Hold on the {screen} "
                              f"was refused. {e}")
                return 409, {"ok": False, "error": str(e), "text": str(e)}
            said = f"{sched} {said}".strip()
            self._journal(who, screen, "hold", "done",
                          f"{who or 'Someone'} pressed Hold on the "
                          f"{screen}. {said}")
            return 200, {"ok": True, "text": said}
        if name in TRANSPORT_ROUTES:
            why = self.programming_move_refusal(name, body, ctx)
            if why:
                self._journal(who, screen, name, "refused",
                              f"{who or 'Someone'}'s {name} on the {screen} "
                              f"was refused. {why}")
                return 403, {"error": why}
            return self.transport(name, body, who, screen)
        svc = self.schedule
        if svc is None:
            return 409, {"error": "There is no scheduler running in this "
                                  "engine, so there is no show to press "
                                  "this on. Nothing was changed."}
        try:
            # surface: the press names the surface, not a person on the
            # operator list (Jeff, 2026-10-07). Every other rule the
            # scheduler has for these presses still applies.
            if name == "reset":
                # From a Hold, Reset ends it (and a paused show) and clears
                # the fault flag; starts nothing (Jeff, 2026-10-28). Else,
                # and always while an Abort stands, the Abort's Reset.
                fn = getattr(svc, "reset_hold", None)
                r = fn(who, screen, surface=surface) if fn else None
                if r is None:
                    r = svc.reset_conductor(who, screen, pressed=pressed,
                                            surface=surface)
                r = self._reset_health(r, who, screen)
            else:
                r = svc.operator_press(name, who, screen,
                                       confirmed=body.get("confirmed") is True,
                                       surface=surface)
        except ValueError as e:
            return 400, {"error": str(e)}
        return (200 if r.get("ok") else 409), r

    # -- programming-session transport ----------------------------------
    LIVE_STATES = ("SHOW", "PAUSED")
    PROGRAMMING_PLAYING = ("Stop the Programming playback first (Stop the "
                           "show audio on the Programming screen). Nothing "
                           "was started.")

    def programming_playback_refusal(self):
        """The sentence that refuses Start now and Run while Programming
        playback is on (playhead.py), or ""."""
        try:
            on = self.programming()[0] and self.playhead.active()
        except Exception:                           # noqa: BLE001
            on = False
        return self.PROGRAMMING_PLAYING if on else ""

    def programming(self):
        """(True, "") in a programming session, else (False, why)."""
        svc = self.schedule
        if svc is not None:
            m = getattr(svc, "machine", None)
            if m is not None and m.state in self.LIVE_STATES:
                return False, ("A scheduled show is live. Only Hold, Resume "
                               "and Abort work during a show; scrubbing is "
                               "for programming sessions.")
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        if s is None or not s.running or p is None:
            return False, ("Nothing is playing. To scrub, on the show "
                           "machine choose Programming on the Show file "
                           "card and press Run.")
        cond = getattr(svc, "conductor", None)
        # A show whose clock is this machine has its player's on_lost set
        # to "hold" whatever was asked (Session.open), so there the Run's
        # own choice decides: Programming asks for "hold" (re-review of
        # #142, P1 b). Any other show, the player's own, as always.
        clk = getattr(s, "clock", None)
        if getattr(clk, "master", False) is True and \
                hasattr(s, "on_lost_arg"):
            asked = getattr(s, "on_lost_arg", None) == "hold"
        else:
            asked = getattr(p, "on_lost", "") == "hold"
        rehearsal = (getattr(s, "no_output", False)
                     or asked
                     or getattr(cond, "mode", None) == "rehearsal")
        if not rehearsal:
            return False, ("This show was started in Show mode. Scrubbing "
                           "only works in Programming: press Stop, choose "
                           "Programming on the Show file card, then Run.")
        return True, ""

    def transport(self, name, body, who, screen):
        ok, why = self.programming()
        label = who or "Someone"
        if not ok:
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{why}")
            return 409, {"error": why}
        c = self.control
        p = c.session.player
        tl = c.session.tl
        # The show audio as the one playhead (playhead.py, Jeff
        # 2026-10-28): audio, timecode, video, pixels, flame cues and
        # lasers follow it through the show's own paths. Otherwise (GPL, a
        # show following timecode) the free run below, as before.
        ph = self.playhead
        if not ph.applies():
            ph = None
        fb = self.fireball
        if ph is not None and name in ("play-from", "loop") and \
                fb is not None and fb.active():
            # One source of fire values at a time: the Fireball page has a
            # channel on (it cuts itself once Run is pressed; this covers
            # the moment between).
            why = ("A Fireball channel is on. Turn the Fireball page off "
                   "first. Nothing was played.")
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{why}")
            return 409, {"error": why}

        def here():
            if ph is not None:
                at = ph.here()
                if at is None:
                    raise ValueError("There is no show time yet. Play from "
                                     "a timecode first.")
                return at
            if p.freerun_paused_at is not None:
                return p.freerun_paused_at
            tc = p.tc_seconds
            if tc is None or tc < 0:
                raise ValueError("There is no show time to start from yet. "
                                 "Play from a timecode first.")
            return tc

        def ensure_freerun():
            if p.freerun_epoch is None:
                c.go(tl.format(here()))

        try:
            if name == "play-from":
                at = str(body.get("at") or "").strip()
                if not at:
                    raise ValueError("Type a timecode to play from, like "
                                     "00:01:30:00, or a cue name.")
                if ph is not None:
                    pos = ph.resolve(at)
                    said = (f"played from "
                            f"{tl.format(ph.play_from(pos, who, screen))} "
                            f"on the show audio")
                else:
                    r = c.go(at)
                    said = f"played from {r['at']}"
            elif name == "jump":
                try:
                    secs = float(body.get("seconds"))
                except (TypeError, ValueError):
                    raise ValueError("Jump by a number of seconds.")
                if not (-JUMP_MAX_S <= secs <= JUMP_MAX_S) or secs == 0:
                    raise ValueError(f"Jump by up to {JUMP_MAX_S:g} s either "
                                     f"way.")
                if ph is not None:
                    at = ph.jump(secs)
                else:
                    ensure_freerun()
                    at = p.nudge(secs)
                said = f"jumped {secs:+g} s to {tl.format(at)}"
            elif name == "pause" and ph is not None:
                at = ph.pause(who, screen)
                said = f"paused at {tl.format(at)}"
            elif name == "continue" and ph is not None:
                at = ph.cont(who, screen)
                said = f"continued from {tl.format(at)}"
            elif name == "pause":
                ensure_freerun()
                at = p.freerun_pause(True)
                said = f"paused at {tl.format(at)}"
            elif name == "continue":
                ensure_freerun()
                at = p.freerun_pause(False)
                said = f"continued from {tl.format(at)}"
            elif name in ("mark-a", "mark-b"):
                at = here()
                self.marks[name[-1]] = at
                said = f"set mark {name[-1].upper()} at {tl.format(at)}"
            elif name == "loop":
                if body.get("on") is True:
                    a, b = self.marks["a"], self.marks["b"]
                    if a is None or b is None:
                        raise ValueError("Set mark A and mark B first.")
                    if ph is not None:
                        ph.loop(a, b, who, screen)
                    else:
                        p.set_loop(a, b)
                    if ph is None and p.freerun_epoch is None:
                        c.go(tl.format(a))
                    said = (f"turned the loop on, {tl.format(a)} to "
                            f"{tl.format(b)}")
                else:
                    if ph is not None:
                        ph.loop(None, None)
                    else:
                        p.set_loop(None, None)
                    said = "turned the loop off"
            elif ph is not None:                    # follow
                ph.follow(who, screen)
                said = "stopped the show audio"
            else:                                   # follow
                c.release()
                said = "handed the show back to the timecode"
        except ValueError as e:
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{e}")
            return 400, {"error": str(e)}
        except Exception as e:                      # SessionError and kin
            self._journal(who, screen, name, "refused",
                          f"{label}'s {name} on the {screen} was refused. "
                          f"{e}")
            return 400, {"error": str(e)}
        text = f"{label} {said} on the {screen} (programming session)."
        self._journal(who, screen, name, "done", text)
        return 200, {"ok": True, "text": text}

    def _transport_view(self):
        s = getattr(self.control, "session", None)
        p = getattr(s, "player", None)
        ok, why = self.programming()
        out = {"programming": ok, "why": why, "freerun": False,
               "paused": False, "loop": None, "marks": {}}
        if p is None or s is None:
            return out
        tl = s.tl
        out["freerun"] = p.freerun_epoch is not None
        out["paused"] = p.freerun_paused_at is not None
        out["loop"] = ([tl.format(p.loop[0]), tl.format(p.loop[1])]
                       if p.loop else None)
        out["marks"] = {k: (tl.format(v) if v is not None else None)
                        for k, v in self.marks.items()}
        ph = self.playhead
        ph.seen()            # an operator page is open: the watchdog's beat
        v = ph.view() if ph.applies() else None
        if v is not None:
            # The show audio is the playhead (playhead.py).
            out.update(playhead="show_audio", playing=v["playing"],
                       paused=v["paused"], loop=v["loop"], freerun=False)
        return out

    # -- arming from a screen (its own PR) ---------------------------------
    def arming_enabled(self):
        try:
            return load_settings(self.folder)["screen_arming"] is True
        except (ValueError, OSError):
            return False              # a broken settings file: off

    def _group_index(self, body):
        """(index, name) of the flame group the page named, from the names
        flamesafe itself reports, or ValueError."""
        fs = self.flame_status
        names = []
        if fs is not None:
            names = [g["name"] for g in fs.view()["groups"]]
        g = body.get("group")
        if isinstance(g, int) and not isinstance(g, bool) and \
                0 <= g < len(names):
            return g, names[g]
        for i, n in enumerate(names):
            if isinstance(g, str) and n.lower() == g.strip().lower():
                return i, n
        raise ValueError("That is not a flame group flamesafe reports.")

    def _abort_standing(self):
        """True while an Abort is latched (the scheduler's or the
        conductor's latch, as show_live_refusal reads it). The Stream Deck
        takes no screen hold then; the engine says so first."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        cond = getattr(svc, "conductor", None)
        return bool(getattr(m, "abort_latched", False) or
                    getattr(cond, "latched", False))

    def arm_refusal(self, gname, device):
        """Why a screen hold on `gname` from `device` would arm nothing
        right now, in a sentence for the page, or "". Each is something
        the Stream Deck or flamesafe refuses anyway; the engine says it
        before a hold that cannot work fills its bar. Checked on the first
        press and on every heartbeat."""
        if self._abort_standing():
            return ("An Abort is standing, so nothing arms until it is "
                    "Reset. Press Reset, then hold again.")
        why = self.live_flame_arm_refusal(gname)
        if why:
            return why
        seen = self._deck_seen
        if seen is None or self.clock() - seen > self.DECK_QUIET_S:
            return (f"The Stream Deck program has not read this engine in "
                    f"the last {self.DECK_QUIET_S:g} s, and it is what arms "
                    f"a group from a screen. Look at the Deck chip at the "
                    f"top. Nothing was armed.")
        m = self.morning
        if m is not None:
            try:
                arranging = m.device_arranging(device)
            except Exception as e:                   # noqa: BLE001
                return (f"Whether {device} is in Arrange mode could not be "
                        f"read ({type(e).__name__}), so nothing arms from "
                        f"it. Nothing was armed.")
            if arranging:
                return (f"{device} is in Arrange mode on the Startup "
                        f"page: nothing arms from it until Save "
                        f"layout or Cancel there (a page that has closed "
                        f"lets go by itself within 2 s).")
            try:
                locked = m.group_locked_out(gname)
            except Exception:                        # noqa: BLE001
                locked = False      # flamesafe still holds a locked group
            if locked:
                return (f"Every machine in {gname} is locked out on the "
                        f"Startup page, so flamesafe will not "
                        f"arm it. Unlock one there first.")
        return ""

    def _drop_hold(self, i, token=None):
        with self._arm_lock:
            h = self._holds.get(i)
            if h is not None and (token is None or h["token"] == token):
                del self._holds[i]
                return h
        return None

    def cancel_holds(self, why, device=None):
        """Every screen arm hold let go, at once (an Abort, a Hold, a
        disarm), or with `device` only that device's own (a sign-out). The
        deck sees them gone on its next read."""
        with self._arm_lock:
            if device is None:
                gone, self._holds = self._holds, {}
            else:
                gone = {i: h for i, h in self._holds.items()
                        if h["token"] == device}
                for i in gone:
                    del self._holds[i]
        for h in gone.values():
            self._journal(h["who"], h["device"], "arm hold", "cancelled",
                          f"{h['who']}'s hold to arm group {h['group']} on "
                          f"the {h['device']} was let go: {why}.")
        return len(gone)

    def arm_hold(self, body, ctx):
        """The page's hold to arm one group: the first call starts it, and
        the page repeats it every 100 ms while the finger stays down. Each
        call checks everything again; any check that fails lets go."""
        if not self.arming_enabled():
            # First, before anything else is looked at (review of PR #43,
            # P0-4): with screen arming switched off nothing about a hold
            # is accepted, whoever asks.
            return 403, {"error": ARMING_OFF, "let_go": True,
                         "arming_off": True}
        # The hold belongs to the surface it came from: one device, one
        # hold, and another device cannot carry it on. With users on it is
        # the signed-in user's, by name (the web layer refused it already
        # for nobody signed in), so the Stream Deck's journal names them.
        who, device = ctx.device, ctx.screen
        if self.users_active():
            who = self.signed_in_name(ctx, body)
        token = ctx.device
        try:
            i, gname = self._group_index(body)
        except ValueError as e:
            return 400, {"error": str(e)}

        def refuse(code, why):
            h = self._drop_hold(i, token)
            if h is not None or body.get("hold_id") is None:
                self._journal(who, device, "arm hold", "refused",
                              f"{who}'s hold to arm {gname} on the "
                              f"{device} was refused. {why}")
            return code, {"error": why, "let_go": True}
        try:
            seen = float(body.get("seen"))
        except (TypeError, ValueError):
            seen = None
        if seen is None or abs(self.wall() - seen / 1000.0) > ARM_FRESH_S:
            return refuse(409, "The page's status is more than 1 s old. "
                               "Wait for it to be live, then hold again.")
        fl = self.flame_status.view() if self.flame_status else None
        if not fl or fl["stale"] or fl["age_ms"] is None or \
                fl["age_ms"] > ARM_FRESH_S * 1000:
            return refuse(409, "flamesafe has not reported in the last "
                               "second, so its real armed state is not "
                               "known. Nothing can arm from here until it "
                               "does.")
        g = fl["groups"][i]
        if g["armed"] == "armed" or g["wanted"]:
            if body.get("hold_id") is not None:
                # This device's own hold, and the group is now asked for.
                # Credited to the hold only when the Stream Deck said it
                # armed the group from THIS hold (deck-input `taken`;
                # review of PR #90, P3-1): a deck key or the Morning page
                # may have armed it meanwhile. The deck's word follows
                # flamesafe's status by a read or two, so it is waited
                # for up to TAKEN_WAIT_S; then the hold ends without credit.
                now = self.clock()
                with self._arm_lock:
                    h = self._holds.get(i)
                    mine = (h is not None and h["token"] == token and
                            h["id"] == body.get("hold_id"))
                    took = mine and \
                        self._deck_refused.get(h["id"]) == "taken"
                    wait = False
                    if mine and not took:
                        h.setdefault("wanted_at", now)
                        wait = now - h["wanted_at"] < TAKEN_WAIT_S
                        if wait:
                            h["beat"] = now
                    if mine and not wait:
                        del self._holds[i]
                        self._deck_refused.pop(h["id"], None)
                if mine and wait:
                    return 200, {"ok": True, "hold_id": h["id"],
                                 "held_s": round(h["beat"] - h["start"], 3),
                                 "needs_s": SCREEN_HOLD_S}
                if mine:
                    if took:
                        text = (f"{gname} is asked for: the Stream Deck took "
                                f"the hold and set it. flamesafe's lamp shows "
                                f"when it is armed.")
                        what = (f"{who}'s hold to arm {gname} on the "
                                f"{device} was taken by the Stream Deck "
                                f"after {h['beat'] - h['start']:.1f} s.")
                    else:
                        text = (f"{gname} is asked for. flamesafe's lamp "
                                f"shows when it is armed.")
                        what = (f"{who}'s hold to arm {gname} on the "
                                f"{device} ended: the group was asked for "
                                f"without the Stream Deck saying this hold "
                                f"did it.")
                    self._journal(who, device, "arm hold",
                                  "taken" if took else "ended", what)
                    return 200, {"ok": True, "armed": True, "taken": took,
                                 "hold_id": h["id"], "text": text,
                                 "held_s": round(h["beat"] - h["start"], 3),
                                 "needs_s": SCREEN_HOLD_S}
            return refuse(409, f"{gname} is already armed or asked for.")
        why = self.arm_refusal(gname, ctx.device)
        if why:
            return refuse(409, why)
        now = self.clock()
        if body.get("hold_id") is not None:
            with self._arm_lock:
                code = self._deck_refused.get(body.get("hold_id"))
            if code is not None and code != "taken":
                # The Stream Deck would not take this hold (its own rules,
                # streamdeck.Controller.screen_pass): said, and let go. The
                # code rides along for the page's own words (2026-10-26).
                c, b = refuse(409, DECK_REFUSALS[code].format(g=gname))
                b["code"] = code
                return c, b
            # A heartbeat for a hold already under way. If that hold is not
            # the live one any more (a gap longer than BEAT_STALE_S, an
            # Abort, a disarm, a restart), it is over: a hold that was
            # interrupted never carries on, the finger has to come up and
            # go down again.
            with self._arm_lock:
                h = self._holds.get(i)
                live = (h is not None and h["token"] == token and
                        h["id"] == body.get("hold_id") and
                        now - h["beat"] <= BEAT_STALE_S and
                        "wanted_at" not in h)
                if live:
                    h["beat"] = now
                    return 200, {"ok": True, "hold_id": h["id"],
                                 "held_s": round(now - h["start"], 3),
                                 "needs_s": SCREEN_HOLD_S}
            if h is not None and h["token"] == token and \
                    h["id"] == body.get("hold_id") and "wanted_at" in h:
                return refuse(409, f"{gname} was asked for and then let go "
                                   f"again during this hold. Lift your "
                                   f"finger and hold again.")
            return refuse(409, "The hold was interrupted. Lift your finger "
                               "and hold again.")
        with self._arm_lock:
            h = self._holds.get(i)
            # A live hold is nobody else's to take over or restart: not
            # another device's, and with no sign-in not another tab's on
            # the same device either (Jeff, 2026-10-07). Only one hold at a
            # time, and a new one starts only once it has gone.
            if h is not None and now - h["beat"] <= BEAT_STALE_S:
                theirs = h["who"]
                h = "busy"
            else:
                self._hold_ids += 1
                h = {"token": token, "who": who, "device": device,
                     "group": gname, "start": now, "beat": now,
                     "id": self._hold_ids}
                self._holds[i] = h
        if h == "busy":
            why = (f"{theirs} is already holding {gname}. Only one hold at "
                   f"a time.")
            self._journal(who, device, "arm hold", "refused",
                          f"{who}'s hold to arm {gname} on the {device} was "
                          f"refused. {why}")
            return 409, {"error": why, "let_go": True}
        self._journal(who, device, "arm hold", "started",
                      f"{who} started holding to arm {gname} on the "
                      f"{device}. The Stream Deck arms it only if the hold "
                      f"lasts {SCREEN_HOLD_S:g} s and every rule allows it.")
        return 200, {"ok": True, "hold_id": h["id"], "held_s": 0.0,
                     "needs_s": SCREEN_HOLD_S}

    def arm_release(self, body, ctx):
        try:
            i, gname = self._group_index(body)
        except ValueError:
            return 200, {"ok": True}
        h = self._drop_hold(i, ctx.device)
        if h is not None:
            held = h["beat"] - h["start"]
            self._journal(h["who"], h["device"], "arm hold", "let go",
                          f"{h['who']} let go of {gname} on the "
                          f"{h['device']} after {held:.1f} s.")
        return 200, {"ok": True}

    def group_disarm(self, body, ctx, confirm=True):
        """Disarm one group: handed to the Stream Deck process, which owns
        the arm link, as an instant tap of that group's key. Never waits
        on a fresh page, and never gated by a role or a sign-in.

        The same rule as Disarm all flame groups (2026-10-29): the answer
        says what is known, never only "sent".
          - the Stream Deck program has not read this engine within
            PLAIN_DECK_FRESH_S, or flamesafe's status is not reaching this
            engine (nothing could confirm it): the forced disarm at once,
            sent before anything is written to disk (as Abort's
            _disarm_now);
          - else flamesafe must report the group off and not wanted, in a
            fresh status received GROUP_DISARM_SETTLE_S or more after the
            deck read the disarm, within PLAIN_CONFIRM_S: "confirmed";
            else the forced disarm then.
        The forced disarm is the show's flames_disarm_all, the flame
        link's disarm_all (the Abort's; the link has no one-group
        disarm): every group needs Arm again, every hazer goes off, and
        the show's flames stay at zero until Hold then Resume or a new
        run. Said so in the answer, journaled as a fault, with a flames
        alert. `confirm` False (the wave flamer cleaning, which confirms
        with its own Disarm all fallback) answers once the disarm is
        handed over, as before."""
        try:
            i, gname = self._group_index(body)
        except ValueError as e:
            return 400, {"error": str(e)}
        who, device, _surface = self._actor(ctx, body)
        if not self.arming_enabled():
            # The Stream Deck reads a screen's per-group disarm only while
            # screen arming is on (review of PR #43, P0-4): said, never
            # "sent" to nobody.
            why = ("Disarming one group from a screen goes through the "
                   "Stream Deck's screen link, which is off with screen "
                   "arming. Use Disarm all flame groups, or the group's key "
                   "on the Stream Deck. Nothing was changed.")
            self._journal(who, device, "disarm", "refused",
                          f"{who or 'Someone'}'s Disarm {gname} on the "
                          f"{device} was refused. {why}")
            return 409, {"error": why}
        seen = self._deck_seen
        deck_fresh = (seen is not None and
                      self.clock() - seen <= PLAIN_DECK_FRESH_S)
        status_fresh = self._flame_status_fresh()
        reads0 = self._deck_reads
        # A timed purge firing this group ends at the press, before the
        # deck has read the disarm (Jeff, 2026-10-29).
        self.timed_purge_cut(f"{who or 'someone'} pressed Disarm {gname} "
                             f"on the {device}", who, device, group=gname)
        self._drop_hold(i)
        with self._arm_lock:
            self._disarm_ids += 1
            self._disarms.append({"id": self._disarm_ids, "group": i,
                                  "name": gname, "who": who,
                                  "device": device})
        forced = None
        if confirm and not deck_fresh:
            forced = ("Deck did not answer",
                      f"the Stream Deck program has not read this engine in "
                      f"the last {PLAIN_DECK_FRESH_S:g} s, and it is what "
                      f"carries a group's Disarm")
        elif confirm and not status_fresh:
            forced = ("No word from flamesafe",
                      "flamesafe's status is not reaching this engine, so "
                      "nothing can confirm the Disarm")
        if forced is not None:
            # The disarm first, the disk after (Abort's _disarm_now).
            sent = self._group_force_send(who, device, gname, forced[1])
        self._journal(who, device, "disarm", "sent",
                      f"{who or 'Someone'} pressed Disarm {gname} on the "
                      f"{device}. Sent to the Stream Deck, which owns the "
                      f"arm link; flamesafe's lamp shows when it is off.")
        if not confirm:
            return 200, {"ok": True, "text": f"Disarm {gname} sent."}
        if forced is not None:
            return self._group_disarm_said(who, device, gname, forced, sent)
        if self._group_seen_off(i, gname, reads0):
            text = f"Disarm {gname} confirmed: flamesafe reports it off."
            self._journal(who, device, "disarm", "confirmed",
                          f"{who or 'Someone'}'s Disarm {gname} on the "
                          f"{device} is confirmed: flamesafe reports it off "
                          f"and not asked for.")
            return 200, {"ok": True, "confirmed": True, "text": text}
        late = ("Not confirmed",
                f"flamesafe did not report {gname} off within "
                f"{PLAIN_CONFIRM_S:g} s of the Disarm")
        sent = self._group_force_send(who, device, gname, late[1])
        return self._group_disarm_said(who, device, gname, late, sent)

    def _flame_status_fresh(self):
        """True when flamesafe's status reaches this engine (a fresh
        view). Never raises."""
        try:
            v = self.flame_status.view()
            return bool(v) and not v.get("stale")
        except Exception:                        # noqa: BLE001
            return False

    def _group_seen_off(self, i, gname, reads0):
        """True once a fresh flamesafe status, received at least
        GROUP_DISARM_SETTLE_S after the deck read deck-input again (its
        read count past `reads0`), reports group `i` (still named
        `gname`) disarmed and not wanted; False when none has by
        PLAIN_CONFIRM_S. Never raises."""
        fs = self.flame_status
        end = time.monotonic() + PLAIN_CONFIRM_S
        read_at = None
        for _step in range(int(PLAIN_CONFIRM_S / GROUP_DISARM_STEP_S) + 1):
            try:
                if read_at is None and self._deck_reads > reads0:
                    read_at = fs.clock()
                got = fs.last_at
                if read_at is not None and got is not None and \
                        got - read_at >= GROUP_DISARM_SETTLE_S:
                    v = fs.view()
                    g = (v.get("groups") or [])[i]
                    if not v.get("stale") and g.get("name") == gname and \
                            g.get("armed") == "disarmed" and \
                            not g.get("wanted"):
                        return True
            except Exception:                    # noqa: BLE001
                pass
            if time.monotonic() >= end:
                break
            try:
                self._pause(GROUP_DISARM_STEP_S)
            except Exception:                    # noqa: BLE001
                break
        return False

    def _group_force_send(self, who, device, gname, why):
        """The forced disarm of a per-group Disarm: the show's
        flames_disarm_all (the flame link's disarm_all, the Abort's: every
        group, a fresh arm cycle, every hazer off, the show's flames at
        zero until Hold then Resume or a new run; the link has no
        one-group disarm). (ok, its sentence). Nothing journaled here.
        Never raises."""
        label = who or "An unnamed operator"
        fn = self._disarm_fn()
        if fn is None:
            return False, ("no flame link is connected in this engine, so "
                           "nothing could be sent")
        try:
            r = fn(f"{label} pressed Disarm {gname} on the {device} "
                   f"({why}: every group disarmed the Abort way)")
            return (bool(getattr(r, "ok", False)),
                    str(getattr(r, "sentence", "") or "").strip())
        except Exception as e:                   # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def _group_disarm_said(self, who, device, gname, cause, sent):
        """The answer, journal line (a fault) and flames alert for a
        per-group Disarm that was forced (`cause` is (lead, why), `sent`
        is _group_force_send's (ok, sentence)). Says what the forced
        disarm does, and "sent", never "disarmed": only flamesafe's
        status can say that."""
        label = who or "An unnamed operator"
        lead, why = cause
        ok, said = sent
        if ok:
            alert = (f"{lead}: ALL groups disarm sent. Disarm {gname} "
                     f"forced ({why}): the flame link has no one-group "
                     f"disarm, so the Abort's disarm was sent to every "
                     f"flame group, every hazer is off, and the show's "
                     f"flames stay at zero until Hold then Resume or a new "
                     f"run. Each group needs Arm again (let go, then hold) "
                     f"before it can fire. {said}").strip()
        else:
            alert = (f"Disarm {gname} COULD NOT be confirmed: {why}, and "
                     f"the forced disarm did not go out either "
                     f"({said or 'no answer'}). {gname} may still be "
                     f"armed: press "
                     f"Abort, or disarm at the Stream Deck.")
        self._journal(who, device, "disarm", "forced" if ok else "fault",
                      f"{label} pressed Disarm {gname} on the {device}. "
                      f"{alert}", fault=True)
        try:
            self.alerts.raise_("flames", alert)
        except Exception:                        # noqa: BLE001
            pass
        if ok:
            return 200, {"ok": True, "confirmed": False, "forced": True,
                         "text": alert}
        return 409, {"ok": False, "confirmed": False, "forced": False,
                     "error": alert}

    def deck_input(self, query=""):
        """What the Stream Deck process reads, 20 times a second: the
        fresh holds (with the held time the engine has heartbeats for) and
        the per-group disarms. Its query may carry usb_ms, the deck's
        worst USB key image write of the last minute (2026-10-21), for the
        rack screen; nothing here acts on it."""
        now = self.clock()
        self._deck_seen = now
        self._deck_reads += 1
        self.users_file_check()
        self._deck_usb_ms = deck_usb_ms(query)
        self._deck_usb_fail = deck_usb_fail(query)
        self._deck_layout_default = deck_layout_default(query)
        self._deck_nokey = deck_nokey(query)
        self._deck_hw = deck_hw(query)
        self._deck_arm_ms = deck_arm_ms(query)
        self._deck_rearm = deck_rearm(query) + (now,)
        refused = deck_refused(query)
        taken = deck_taken(query)
        enabled = self.arming_enabled()
        holds = []
        with self._arm_lock:
            for i, h in list(self._holds.items()):
                age = now - h["beat"]
                if age > 10 * BEAT_STALE_S:
                    del self._holds[i]          # long gone; tidy up
                    continue
                holds.append({"group": i, "id": h["id"],
                              "held_s": round(h["beat"] - h["start"], 3),
                              "fresh": age <= BEAT_STALE_S,
                              "who": h["who"], "device": h["device"]})
            # What the deck would not take, kept for the hold's next
            # heartbeat to say (only for holds still here).
            live_ids = {h["id"] for h in self._holds.values()}
            for hid, code in refused:
                if hid in live_ids:
                    self._deck_refused[hid] = code
            for hid in taken:
                if hid in live_ids:
                    self._deck_refused[hid] = "taken"
            for hid in [k for k in self._deck_refused if k not in live_ids]:
                del self._deck_refused[hid]
            disarms = list(self._disarms)
        self._apply_pulse()
        # The open alerts too: the deck relights a key as ACK while one is
        # open, and any key press acknowledges (alerts.py).
        out = {"enabled": enabled, "holds": holds if enabled else [],
               "disarms": disarms, "alerts": self.alerts.view()}
        # The rig check under way (preroll.py): the Start key says so.
        # Display only.
        pv = self.preroll_view()
        out["preroll"] = bool(pv) and pv.get("state") in ("queued",
                                                          "checking")
        # The announcements, for the deck's page 2 keys (display only).
        av = self.announce_view()
        out["announce"] = {
            "configured": bool(av.get("configured")),
            "stale": bool(av.get("stale")),
            "ok": av.get("ok") is not False,
            # Whether a press can play now: through the show audio, or
            # on the announcement's own output (announce.route_check).
            "device_ok": bool(av.get("route_ok")),
            "ready": {i["id"]: bool(i.get("available"))
                      for i in av.get("items") or []},
            "missing": {i["id"]: bool(i.get("missing"))
                        for i in av.get("items") or []},
            "playing": (av.get("playing") or {}).get("id")}
        # The hazers (hazers.py), for the deck's page 3 keys (display only).
        hz = self.hazers
        if hz is not None:
            try:
                names = hz.names()
                if names:
                    out["hazers"] = {"names": names,
                                     "on": sorted(hz.on()),
                                     "down": hz.down(),
                                     "paused": hz.show() == "held"}
            except Exception:                    # noqa: BLE001
                pass
        # Who is signed in on this machine (users.py): the deck shows it
        # and acts as them.
        uv = self.user_view_for_deck()
        if uv is not None:
            out["user"] = uv
        # Whether live flame programming is on, or Programming playback with
        # the show's flame cues following it: the deck counts either as
        # running, so key 0 is Stop (STOP ALL), never FIRE (review of #130,
        # low items, 3).
        # Not known (a view that raises, answers with an error, or does not
        # say "on"): None, which the deck reads as on, so Stop wins (review
        # of #132, 4). On wins over not known.
        live = False
        for view in (self.live_flame_view, self.programming_flames_view):
            try:
                v = view()
            except Exception:                    # noqa: BLE001
                v = None
            if isinstance(v, dict) and v.get("on") is True:
                live = True
            elif not isinstance(v, dict) or "error" in v or \
                    not isinstance(v.get("on"), bool):
                if live is False:
                    live = None
        # The wave flamer cleaning running, paused or being held counts as
        # running too, so the deck's key 0 is Stop (cleaning.py).
        cl = self.cleaning
        if cl is not None:
            try:
                if cl.active():
                    live = True
            except Exception:                    # noqa: BLE001
                live = None if live is False else live
        out["flames_live"] = live
        # The Morning and Shutdown page (morning.py): whether it is open
        # (the deck re-maps itself while it is), the groups the purge
        # selection needs armed, and the locks. The deck arms a purge
        # group only while the engine says purge arming is on.
        m = self.morning
        if m is not None:
            try:
                mv = m.deck_view()
            except Exception as e:                   # noqa: BLE001
                mv = {"open": False, "purge": {"wanted_groups": [],
                                               "selected": 0, "firing": False},
                      "locks": {}, "error": f"{type(e).__name__}: {e}"}
            mv["purge_enabled"] = self.purge_enabled(local=True)
            out["morning"] = mv
        return out

    # -- the Morning and Shutdown page (morning.py) --------------------------
    def purge_enabled(self, local):
        """May a purge be armed from here? purge_arming (default on) for
        the machine itself; a device on the show network also needs
        screen_arming, the switch every other arming from the network has
        (MORNING.md, T9). A broken settings file: off."""
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError):
            return False
        if rs["purge_arming"] is not True:
            return False
        return bool(local) or rs["screen_arming"] is True

    def may_act_from(self, local):
        """May a device UNLOCK a machine or turn a laser test ON from here?
        The show machine itself always; a device on the show network only
        with screen_arming (review of PR #68, P2-4 and P2-5: the two things
        on this page that raise risk from the network are gated like
        arming). Locking stays open to every device: it only lowers risk."""
        if local:
            return True
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError):
            return False
        return rs["screen_arming"] is True

    def _morning_journal(self, text, action="morning", outcome="done",
                         who="", screen="", fault=False, **_kw):
        self._journal(who or "system", screen or "engine", action, outcome,
                      text, fault=fault)

    # -- live flame programming (flameprog.py) ------------------------------
    def live_flame_settings_path(self):
        return os.path.join(self.folder, SETTINGS_FILE)

    def live_flame_blockers(self):
        """Why the engine forbids live flame programming right now (entry
        and every watch pass), in a sentence, or "": a show live or held,
        an Abort standing, the show clock playing, Run pressed, flamesafe
        not reporting, the flame link not sending, a purge selected."""
        why = self.show_live_refusal()
        if why:
            return why
        svc = self.schedule
        m = getattr(svc, "machine", None)
        state = getattr(m, "state", None)
        if state in ("HOLD", "PAUSED"):
            return "a Hold is standing; Resume or Reset first"
        s = getattr(self.control, "session", None)
        if s is not None and getattr(s, "running", False):
            return ("Run is pressed (a show file is running), so the show "
                    "may send flame cues; press Stop first")
        try:
            cs = self.control.state() or {}
        except Exception:                        # noqa: BLE001
            cs = {}
        if cs.get("running") or cs.get("starting"):
            return "Run is pressed (a show file is running); press Stop first"
        fs = self.flame_status
        fl = fs.view() if fs is not None else None
        if not fl or fl.get("stale"):
            return ("flamesafe has not reported in the last second, so its "
                    "link and armed groups are not known")
        lf = self.live_flame
        link = lf.link() if lf is not None and hasattr(lf, "link") else None
        if link is not None:
            try:
                sender = link.sender_state()
            except Exception:                    # noqa: BLE001
                sender = "unknown"
            if sender not in ("running",):
                return (f"the flame link to flamesafe is not sending "
                        f"({sender})")
            if getattr(link, "lock_alarm", ""):
                return f"the flame link has an alarm: {link.lock_alarm}"
            if link.purge_active():
                return "a purge is firing on the Morning and Shutdown page"
        mo = self.morning
        pg = getattr(mo, "purge", None) if mo is not None else None
        if pg is not None and pg.selected:
            return ("machines are selected for a purge on the Morning and "
                    "Shutdown page; clear the purge selection first")
        # The fuel pump (re-review A of #188, N1): at the start only (the
        # mode not yet on; begin() asks this after marking itself
        # starting), never on a watch pass, so a pump can never end a
        # running session (and with it, by its disarm, the pump).
        if lf is None or not getattr(lf, "on", False):
            why = self.live_flame_pump_refusal()
            if why:
                return why
        return ""

    def live_flame_armed_groups(self):
        """The flame groups flamesafe reports armed (or asked for), or None
        when its status is not fresh."""
        fs = self.flame_status
        fl = fs.view() if fs is not None else None
        if not fl or fl.get("stale"):
            return None
        # Armed, or being armed (wanted: the Stream Deck is asking for it,
        # flamesafe's "held" while it waits): review of PR #96, P3-1. A
        # second group is seen before flamesafe can arm it.
        return [g["name"] for g in fl.get("groups") or []
                if g.get("armed") == "armed" or g.get("wanted")]

    def live_flame_arm_refusal(self, gname):
        """With live flame programming on and one group at a time: a hold
        on a second group is refused, named. "" otherwise."""
        lf = self.live_flame
        if lf is None or not lf.on:
            return ""
        s = lf.settings
        if s is None or not s.one_group:
            return ""
        armed = self.live_flame_armed_groups()
        if armed is None:
            return ("Live flame programming is on and flamesafe's armed "
                    "groups are not known, so nothing arms from here.")
        others = [n for n in armed if n != gname]
        if others:
            return (f"Live flame programming is on and {others[0]} is "
                    f"already armed: one flame group at a time. Disarm it "
                    f"first.")
        return ""

    def live_flame_cut(self, reason, who="", screen=""):
        """At an Abort, Hold or Start now press: the program slot shut now
        (LiveFlame.cut), and the rest of ending it (journal, audit, the
        disarm of every group) on a thread of its own. Never raises, never
        waits."""
        lf = self.live_flame
        if lf is None:
            return False
        try:
            if not lf.cut(reason):
                return False
            threading.Thread(target=lf.end, args=(reason,),
                             kwargs={"who": who, "screen": screen,
                                     "system": not who},
                             daemon=True,
                             name="ltcplay-live-flame-end").start()
            return True
        except Exception:                        # noqa: BLE001
            return False

    def live_flame_end(self, reason, who="", screen=""):
        """End live flame programming now, if it is on (D2: every flame
        group disarmed). Never raises."""
        lf = self.live_flame
        if lf is None:
            return False
        try:
            return lf.end(reason, who=who, screen=screen,
                          system=not who)
        except Exception:                        # noqa: BLE001
            return False

    def live_flame_view(self):
        lf = self.live_flame
        if lf is None:
            return {"available": False, "on": False,
                    "why": "Live flame programming needs the Fire & Ice "
                           "engine with a flame link."}
        try:
            v = lf.view()
        except Exception as e:                   # noqa: BLE001
            return {"available": True, "on": False,
                    "error": f"{type(e).__name__}: {e}"}
        v["available"] = True
        return v

    def live_flame_post(self, body, ctx):
        """POST live-flame {"on": true, "confirmed": true} or {"on":
        false}. ON: the show machine itself only, confirmed, and every
        engine check (flameprog.LiveFlame.begin); the role matrix's
        Settings row is checked by web.py before this (users.classify).
        Anything but the JSON word true is OFF, from any surface, signed in
        or not."""
        who = ctx.user or ""
        screen = ctx.screen if ctx.local else ctx.device
        lf = self.live_flame
        on = body.get("on") is True
        if not on:
            if lf is None or not lf.on:
                return 200, {"ok": True, "on": False,
                             "text": "Live flame programming is off."}
            lf.end(f"{who or 'someone signed out'} turned it off on the "
                   f"{screen}", who=who or "unsigned", screen=screen,
                   system=False)
            return 200, {"ok": True, "on": False,
                         "text": "Live flame programming is OFF. Every "
                                 "flame group was sent a disarm."}
        if lf is None:
            return 409, {"error": "Live flame programming needs the Fire & "
                                  "Ice engine with a flame link. Nothing "
                                  "was changed."}
        if not ctx.local:
            self._journal(who or ctx.device, ctx.device, "live flame",
                          "refused",
                          f"Live flame programming ON from {ctx.device} was "
                          f"refused: only on the show machine itself.")
            return 403, {"error": "Live flame programming is turned on on "
                                  "the show machine itself, not from here. "
                                  "Nothing was changed."}
        # The fuel pump (2026-10-30, review B of #188, F3): live flame
        # programming ends with a disarm to every flame group, which turns
        # the pump off too; a Hold or Start now ends it. So it does not
        # start while the pump runs: Hold must never end the pump (Jeff:
        # "Off on Stop, NOT Hold"). This is the early, plain answer; the
        # check that decides is live_flame_blockers, run inside begin()
        # after it is marked starting, and pump_blocker refuses the pump
        # while live flame programming is on OR starting, with the pump's
        # run reserved before that look (re-review A of #188, N1): one of
        # the two always sees the other.
        why = self.live_flame_pump_refusal()
        if why:
            self._journal(who or "unsigned", screen, "live flame", "refused",
                          f"Live flame programming ON on the {screen} was "
                          f"refused: {why}")
            return 409, {"error": why}
        if body.get("confirmed") is not True:
            return 400, {"error": "Confirm it on the page first. Nothing "
                                  "was changed."}
        ok, text = lf.begin(who or "unsigned", screen)
        if not ok:
            return 409, {"error": text}
        return 200, {"ok": True, "on": True, "text": text}

    # -- the show's flame cues in Programming (playhead.py) -----------------
    def programming_flames_follow(self):
        """True when the show's flame cues follow the Programming transport
        here: this engine has a flame link (Fire & Ice) and the running
        show plays from the show audio (playhead.py), so Play from, Back,
        Ahead, Continue and the loop decide when flames fire. Fails
        closed: True when it cannot be told."""
        try:
            if getattr(self.flame_cues, "link", None) is None:
                return False
            ph = self.playhead
            return ph is not None and bool(ph.applies())
        except Exception:                        # noqa: BLE001
            return True

    def programming_flames_view(self):
        """For the status and the deck: "available" with a flame link in
        this engine, "follows" while the running show's flame cues follow
        the Programming transport, "on" while Programming playback is
        playing with them following, "line" the Programming screen's
        sentence. Never raises."""
        out = {"available": False, "follows": False, "on": False,
               "line": PROGRAMMING_FLAMES_LINE}
        try:
            out["available"] = \
                getattr(self.flame_cues, "link", None) is not None
            out["follows"] = bool(out["available"] and
                                  self.programming_flames_follow())
            ph = self.playhead
            out["on"] = bool(out["follows"] and ph is not None and
                             ph.active())
        except Exception as e:                   # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
            out.pop("on", None)
        return out

    # The transport presses that move the position and so decide when
    # flames fire while the show's flame cues follow the transport (Jeff's
    # decision on review of #117, P1-2, kept when the switch went), and
    # those that change what a later move plays (the loop either way, the
    # marks: second review, P2-3). Pause and Follow only stop it.
    PROGRAMMING_MOVES = ("play-from", "jump", "continue", "loop", "mark-a",
                         "mark-b")
    PROGRAMMING_MOVE_REFUSED = ("Flames follow the show here; move the "
                                "position from the show PC or sign in as "
                                "an admin. Nothing was changed.")

    def programming_move_refusal(self, name, body, ctx):
        """While the show's flame cues follow the Programming transport
        (programming_flames_follow), a press that moves the position or
        changes what plays (Play from, Jump, Continue, the loop on or off,
        mark A, mark B) needs the show machine itself or someone signed in
        with the Settings permission; "" when it may go ahead. Fails
        closed."""
        if name not in self.PROGRAMMING_MOVES or \
                getattr(ctx, "local", False):
            return ""
        if not self.programming_flames_follow():
            return ""
        try:
            from . import users as users_mod
            u = self.users
            role = (getattr(ctx, "session", None) or {}).get("role")
            if u is not None and u.active() and role is not None and \
                    users_mod.allowed(u.matrix(), role, "settings"):
                return ""
        except Exception:                        # noqa: BLE001
            pass
        return self.PROGRAMMING_MOVE_REFUSED

    # -- the hazers (hazers.py) ----------------------------------------------
    def hazers_cut(self, reason, who="", screen=""):
        """Abort or Stop: every hazer OFF at the flame link now, the journal
        and audit lines on a thread of their own. Never raises, never
        waits."""
        hz = self.hazers
        if hz is None:
            return False
        try:
            # Which were armed, read before the cut clears them, so the
            # journal line and last_off name them (reviewer B, 1).
            was = hz.cut()
            threading.Thread(target=hz.all_off, args=(reason,),
                             kwargs={"who": who, "screen": screen,
                                     "system": not who, "was": was},
                             daemon=True, name="ltcplay-hazers-off").start()
            return True
        except Exception:                        # noqa: BLE001
            return False

    def hazer_post(self, name, body, ctx):
        """POST hazer {"hazer": name, "on": true|false} (arm or disarm),
        hazers-off, and hazer-level {"hazer": name or "all", "haze": 0 to
        100, "fan": 0 to 100}. Arming and levels are arm presses
        (users.classify, checked by web.py first) and, from the show
        network, need screen_arming (may_act_from); a disarm and Hazers off
        are never refused. (code, body)."""
        hz = self.hazers
        if hz is None:
            return 404, {"error": "There are no hazers in this engine."}
        who, device, _surface = self._actor(ctx, body)
        if name == "hazers-off":
            hz.all_off(f"{who or 'someone'} pressed Hazers off on the "
                       f"{device}", who=who, screen=device, system=False)
            return 200, {"ok": True, "text": "Every hazer is DISARMED."}
        hname = body.get("hazer")
        names = hz.names()
        if isinstance(hname, int) and not isinstance(hname, bool) and \
                0 <= hname < len(names):
            hname = names[hname]
        if not isinstance(hname, str) or (
                hname not in names and
                not (name == "hazer-level" and hname == "all")):
            return 400, {"error": "That is not a hazer in flamesafe's "
                                  "config. Nothing was changed."}
        on = body.get("on") is True or name == "hazer-level"
        if on and not self.may_act_from(ctx.local):
            what = ("Setting hazer levels" if name == "hazer-level"
                    else "Arming a hazer")
            why = (f"{what} from a device on the show network needs "
                   f"screen_arming (on the show machine itself: "
                   f"Programming, Arming from a screen, Turn on). Nothing "
                   f"was changed.")
            self._journal(who, device, name, "refused",
                          f"{who or 'Someone'}'s {name} for {hname} from the "
                          f"{device} was refused. {why}")
            return 403, {"error": why}
        if name == "hazer-level":
            ok, text = hz.set_levels(hname, body.get("haze"),
                                     body.get("fan"), who=who, screen=device)
            if not ok:
                return 400, {"error": text}
            return 200, {"ok": True, "text": text}
        ok, text = hz.set(hname, on, who=who, screen=device)
        if not ok:
            return 409, {"error": text}
        return 200, {"ok": True, "text": text}

    # -- the fuel pump (fuelpump.py) ------------------------------------------
    def pump_cut(self, reason, who="", screen=""):
        """Stop or Abort: the fuel pump OFF at the flame link now, the
        journal and audit lines on a thread of their own. Never raises,
        never waits."""
        pm = self.pump
        if pm is None:
            return False
        try:
            was = pm.cut()
            threading.Thread(target=pm.off, args=(reason,),
                             kwargs={"who": who, "screen": screen,
                                     "system": not who, "was": was},
                             daemon=True, name="ltcplay-pump-off").start()
            return True
        except Exception:                        # noqa: BLE001
            return False

    def pump_settings(self):
        """The fuel pump's run times on this show machine: the screens'
        default (always 5:00, capped by the longest), the longest, the
        shortest and
        the most an admin may set, from ltcplay_remote.json; "error" when
        that file cannot be read (then the pump is not turned on)."""
        out = {"default_s": PUMP_DEFAULT_S, "max_s": PUMP_MAX_DEFAULT_S,
               "min_s": PUMP_MIN_S, "limit_s": PUMP_MAX_LIMIT_S,
               "error": ""}
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError) as e:
            out["error"] = str(e)
            return out
        out["max_s"] = rs["pump_max_s"]
        # Jeff (2026-10-30): 5 minutes unless Andy sets another run time
        # before he turns it on; never a remembered one.
        out["default_s"] = min(PUMP_DEFAULT_S, rs["pump_max_s"])
        return out

    def pump_post(self, body, ctx):
        """POST fuel-pump {"on": true, "seconds": N, "run": "<press id>"}
        or {"on": false}. ON is an arm press (users.classify, checked by
        web.py first) and, from the show network, needs screen_arming
        (may_act_from); anything but the JSON word true turns it OFF, is
        never refused and never starts it. (code, body)."""
        pm = self.pump
        if pm is None:
            return 404, {"error": "There is no fuel pump in this engine."}
        who, device, _surface = self._actor(ctx, body)
        on = body.get("on") is True
        if not on:
            ok, text = pm.set(False, who=who, screen=device)
            return 200, {"ok": True, "text": text}
        if not self.may_act_from(ctx.local):
            why = ("Turning the fuel pump on from a device on the show "
                   "network needs screen_arming (on the show machine "
                   "itself: Programming, Arming from a screen, Turn on). "
                   "Nothing was changed.")
            self._journal(who, device, "fuel-pump", "refused",
                          f"{who or 'Someone'}'s fuel pump ON from the "
                          f"{device} was refused. {why}")
            return 403, {"error": why}
        # The press's own age (final delta review of #189): an ON sent from
        # a status more than ARM_FRESH_S old (or none) is refused, as an
        # arm hold is, so a request that stalled on weak Wi-Fi and lands
        # after a Pump off or a Stop can never start the pump then.
        try:
            seen = float(body.get("seen"))
        except (TypeError, ValueError):
            seen = None
        if seen is None or seen != seen or \
                abs(self.wall() - seen / 1000.0) > ARM_FRESH_S:
            why = ("The fuel pump was not turned on: the press came from a "
                   "screen whose status was more than 1 s old (or said "
                   "nothing of its age), so it may be from before a Pump "
                   "off or a Stop. Wait for the screen to be live, then "
                   "press again.")
            self._journal(who, device, "fuel-pump", "refused",
                          f"{who or 'Someone'}'s fuel pump ON from the "
                          f"{device} was refused. {why}")
            return 409, {"error": why}
        ps = self.pump_settings()
        if ps["error"]:
            return 409, {"error": f"The fuel pump was not turned on: "
                                  f"{ps['error']}"}
        ok, text = pm.set(True, who=who, screen=device,
                          seconds=body.get("seconds"), run=body.get("run"),
                          max_s=ps["max_s"])
        if not ok:
            return 409, {"error": text}
        return 200, {"ok": True, "text": text}

    def pump_view(self, fl):
        """The status's "pump": fuelpump.FuelPump.view with this show
        machine's run times, or None with no pump in this engine. Never
        raises."""
        pm = self.pump
        if pm is None:
            return None
        try:
            v = pm.view(fl)
            ps = self.pump_settings()
            v.update(default_s=ps["default_s"], max_s=ps["max_s"],
                     min_s=ps["min_s"], settings_error=ps["error"])
            return v
        except Exception as e:                   # noqa: BLE001
            return {"available": False,
                    "down": f"{type(e).__name__}: {e}"}

    def hazers_view(self, fl):
        """The status's "hazers": hazers.Hazers.view, and the display only
        "hazers" alert while flamesafe's list and this engine's differ.
        None with no hazers in this engine. Never raises."""
        hz = self.hazers
        if hz is None:
            return None
        try:
            v = hz.view(fl)
        except Exception as e:                   # noqa: BLE001
            return {"available": False, "hazers": [],
                    "down": f"{type(e).__name__}: {e}"}
        try:
            mine = sorted((n, list(sl)) for n, sl in hz.slots().items())
            theirs = hz.flamesafe_list(fl)
            agree = None if theirs is None else sorted(theirs) == mine
            v["agree"] = agree
            self._hazers_alert(agree, mine, theirs)
        except Exception:                        # noqa: BLE001
            pass
        return v

    def hazer_show_state(self):
        """The hazers' gate (hazers.Hazers show_state): "playing" while the
        scheduled show is in SHOW, "held" on Hold (HOLD or PAUSED), else
        "idle" (between shows, the rig check, Programming). Never
        raises."""
        try:
            m = getattr(self.schedule, "machine", None)
            st = getattr(m, "state", None)
        except Exception:                        # noqa: BLE001
            return "idle"
        if st == "SHOW":
            return "playing"
        if st in ("HOLD", "PAUSED"):
            return "held"
        return "idle"

    def hazer_blocker(self):
        """Why no hazer may be armed now beyond hazers.py's own checks, or
        "": live flame programming on (its end sends the Abort's disarm to
        flamesafe, which disarms every hazer; reviewer B, 3)."""
        lf = getattr(self, "live_flame", None)
        if lf is not None and getattr(lf, "on", False):
            return ("live flame programming is on, and turning it off "
                    "disarms every hazer. Turn it off first.")
        return ""

    def pump_night_state(self):
        """The scheduler's state for the fuel pump's end of night
        (fuelpump.NIGHT_CLOSING and NIGHT_OVER), or None. Never raises."""
        try:
            return getattr(getattr(self.schedule, "machine", None), "state",
                           None)
        except Exception:                        # noqa: BLE001
            return None

    def live_flame_pump_refusal(self):
        """Why live flame programming may not start because of the fuel
        pump, or "": the pump is on (its end, or a Hold or Start now ending
        it, would turn the pump off). Fails closed. Never raises."""
        pm = self.pump
        if pm is None:
            return ""
        try:
            on = pm.run is not None or pm.on()
        except Exception:                        # noqa: BLE001
            on = True
        if on:
            return ("The fuel pump is on, and live flame programming ends "
                    "with a disarm that turns the pump off (a Hold would "
                    "end it too). Press Pump off first, or wait for its "
                    "time to end. Nothing was changed.")
        return ""

    def pump_blocker(self):
        """Why the fuel pump may not be turned ON now beyond fuelpump.py's
        own checks, or "": live flame programming on, or being turned on
        (its end sends the Abort's disarm to flamesafe, which turns the
        pump off; re-review A of #188, N1). Asked again by fuelpump.py with
        its run already reserved, so a begin() racing it is always seen by
        one side or the other."""
        lf = getattr(self, "live_flame", None)
        programming = lf is not None and (getattr(lf, "on", False) or
                                          getattr(lf, "_starting", False))
        if programming and not getattr(lf, "on", False):
            return ("live flame programming is starting, and turning it "
                    "off turns the fuel pump off. Turn it off first, or "
                    "wait for it to start and turn it off.")
        if programming:
            return ("live flame programming is on, and turning it off "
                    "turns the fuel pump off. Turn it off first.")
        return ""

    @staticmethod
    def hazers_mismatch_text(mine, theirs, path):
        def words(v):
            return "; ".join(f"{n} {', '.join(str(x) for x in sl)}"
                             for n, sl in v) if v else "none"
        return (f"Hazers do not match: flamesafe has {words(theirs)} and "
                f"LTC Player has {words(mine)} (LTC Player read "
                f"{path or 'flamesafe config'} when it started). Until they "
                f"match no hazer can be switched on here. Check that both "
                f"read the same flamesafe.json, then restart flamesafe and "
                f"LTC Player. Stop, Hold, Abort and Disarm all work as usual "
                f"meanwhile.")

    def _hazers_alert(self, agree, mine, theirs):
        """Raise once per new mismatch sentence; clear the one raised here
        once the lists agree. Not known: nothing changes. Display only."""
        if agree is None:
            return
        with self._hz_lock:
            if agree:
                aid, self._hz_alert_id = self._hz_alert_id, None
                was, self._hz_said = self._hz_said, None
                if was is None:
                    return
                self._journal("system", "engine", "hazers", "match",
                              "Hazers match again: flamesafe and LTC Player "
                              "have the same list.")
                if aid is not None and any(
                        a.id == aid for a in self.alerts.open()):
                    self.alerts.clear(action="Confirm", id=aid,
                                      who="LTC Player", screen="engine")
                return
            src = self.flame_aim
            path = "" if src is None else str(src[1] or "")
            text = self.hazers_mismatch_text(mine, theirs, path)
            if text == self._hz_said:
                return
            self._hz_said = text
            try:
                a = self.alerts.raise_("hazers", text)
                self._hz_alert_id = getattr(a, "id", None)
            except Exception:                    # noqa: BLE001
                self._journal("system", "engine", "hazers", "differ", text,
                              fault=True)

    def morning_live_refusal(self):
        """The Morning page's live_check: show_live_refusal, and while live
        flame programming is on nothing fires from that page (one source
        of fire values at a time)."""
        why = self.show_live_refusal()
        if why:
            return why
        lf = self.live_flame
        if lf is not None and lf.on:
            return ("live flame programming is on; nothing fires from this "
                    "page until it is off")
        return ""

    def show_live_refusal(self):
        """Why nothing may fire from the Morning page right now: a show
        live (SHOW or PAUSED), an Abort standing, or the show clock playing
        a cue. "" when a purge may go (between shows, nothing playing)."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        state = getattr(m, "state", None)
        if state in ("SHOW", "PAUSED"):
            return (f"a scheduled show is live ({state}); nothing fires from "
                    f"this page until it has ended")
        cond = getattr(svc, "conductor", None)
        if getattr(m, "abort_latched", False) or \
                getattr(cond, "latched", False):
            return "the show is aborted; press Reset first"
        s = getattr(self.control, "session", None)
        clk = getattr(s, "clock", None) if s is not None else None
        if clk is not None and getattr(clk, "playing", False):
            return "the show clock is playing a cue; nothing fires from this page meanwhile"
        return ""

    def morning_get(self, name, query, ctx):
        m = self.morning
        if m is None:
            return 404, {"error": "The Startup page is not "
                                  "set up in this engine (it needs the Fire "
                                  "& Ice scheduler)."}
        if name == "morning":
            v = m.full_view(ctx.device)
            v["purge_enabled"] = self.purge_enabled(ctx.local)
            # The timed purge: its numbers here, and this page's own view
            # of it (its start hold is this page load's, by window).
            v["timed_settings"] = self.timed_purge_settings()
            pg = getattr(m, "purge", None)
            if pg is not None and hasattr(pg, "timed_view"):
                v["timed"] = pg.timed_view(self._page_owner(ctx))
            v["checks"] = self.engine_checks()
            v["served_at"] = int(self.wall() * 1000)
            return 200, v
        import urllib.parse
        q = urllib.parse.parse_qs(query or "")
        if name == "checklist-pdf":
            date = (q.get("date") or [m.checklist.show_date()])[0]
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                return 400, {"error": "date has to be YYYY-MM-DD."}
            data = m.checklist.pdf_for_date(date)
            return (200, data, "application/pdf",
                    {"Content-Disposition":
                     f'attachment; filename="checklist_{date}.pdf"'})
        if name == "checklist-export":
            frm = (q.get("from") or [None])[0]
            to = (q.get("to") or [None])[0]
            for d in (frm, to):
                if d is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    return 400, {"error": "from and to have to be "
                                          "YYYY-MM-DD."}
            fname, data = m.checklist.export_zip(frm, to)
            return (200, data, "application/zip",
                    {"Content-Disposition": f'attachment; filename="{fname}"'})
        return 404, {"error": "no such thing here"}

    def open_beyond(self, body, ctx):
        """POST open-beyond: BEYOND's window to the front of the show PC
        (windowfront.py). The show machine itself only, no show live and
        no Abort standing; signed in or not. Moves a window, nothing more:
        nothing is ever sent to BEYOND."""
        who, screen, _surface = self._actor(ctx, body)
        if not ctx.local:
            return 403, {"error": "Open BEYOND works on the show machine "
                                  "itself, where BEYOND's window is. "
                                  "Nothing was done."}
        why = self.show_live_refusal() or self._open_beyond_state() or \
            self.rig_busy()
        if why:
            return 409, {"error": f"Open BEYOND is not allowed now: {why}."}
        fn = self.open_beyond_fn
        if fn is None:
            # On a worker thread with a 3 s limit, one press at a time
            # (review of PR #125, P2-2).
            from . import windowfront
            fn = windowfront.OPENER
            # The supervisor's rack screen keeper leaves BEYOND in front
            # after this press (showwindows.RackKeeper).
            from . import appdata, showwindows
            if appdata.WINDOWS:
                showwindows.stamp_opened(appdata.folder())
        try:
            ok, text = fn(self.beyond_host)
        except Exception as e:                   # noqa: BLE001
            ok, text = False, (f"BEYOND could not be brought forward "
                               f"({type(e).__name__}). Open it from the "
                               f"taskbar.")
        self._journal(who, screen, "open-beyond",
                      "done" if ok else "refused",
                      f"Open BEYOND pressed by {who or 'someone'} on the "
                      f"{screen or 'show machine'}: {text}")
        return (200 if ok else 409), ({"ok": True, "text": text} if ok
                                      else {"error": text})

    def _open_beyond_state(self):
        """Why Open BEYOND may not go in this scheduler state, or "": on
        Hold (or the conductor holding), and between shows (STANDBY), when
        the rack screen has to stay in front (review of PR #125, P2-3)."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        state = getattr(m, "state", None)
        if state == "HOLD" or getattr(getattr(svc, "conductor", None),
                                      "_look", None) == "HELD":
            return "the show is on Hold; the rack screen stays in front"
        if state == "STANDBY":
            return ("the night is between shows (STANDBY); the rack screen "
                    "stays in front until the last show is over")
        return ""

    def rig_busy(self, armed=False):
        """What is going on at the rig that a window in front of the rack
        screen, or a sign-out, must not cut into, as a sentence, or "": a
        screen's hold to arm, a purge selected or firing; with `armed`
        (the sign-out), also a laser calibration test on, any laser armed
        and any flame group flamesafe reports armed or wanted (the Stream
        Deck's holds show there). Open BEYOND asks without `armed`: it is
        meant to be pressed while the calibration test is on. Fails closed: a state that cannot be read is
        busy. Plain reads (review of PR #125, P2-3 and P2-5)."""
        try:
            with self._arm_lock:
                if self._holds:
                    return "a hold to arm a flame group is in progress"
            m = self.morning
            if m is not None:
                if m.purge.fire is not None:
                    return "a purge is firing"
                if m.purge.selected:
                    return "machines are armed for a purge"
                if armed and m.lasers.on:
                    # Not for Open BEYOND: bringing BEYOND forward while
                    # the calibration test is on is the point of it (Jeff,
                    # 2026-10-26). The sign-out waits for it.
                    return "a laser calibration test is on"
                if armed and m.laser_arm.armed_ids():
                    return "a laser is armed"
            fs = self.flame_status
            if armed and fs is not None:
                v = fs.view()
                if not v.get("stale"):
                    for g in v.get("groups") or []:
                        if g.get("armed") == "armed" or g.get("wanted"):
                            return f"flame group {g.get('name')} is armed"
        except Exception as e:                   # noqa: BLE001
            return f"the rig's state could not be read ({type(e).__name__})"
        return ""

    def morning_post(self, name, body, ctx):
        if name == "open-beyond":
            return self.open_beyond(body, ctx)
        m = self.morning
        if m is None:
            return 404, {"error": "The Startup page is not "
                                  "set up in this engine."}
        who, screen, _surface = self._actor(ctx, body)
        device = ctx.device
        try:
            seen = float(body.get("seen"))
            seen_ok = abs(self.wall() - seen / 1000.0) <= 1.0
        except (TypeError, ValueError):
            seen_ok = False
        if name == "morning-open" and not self.purge_enabled(ctx.local):
            # A device that may not purge from here reads the page but does
            # not hold it "open": the deck re-maps itself and the purge
            # selection lives only for devices that may arm one (review of
            # PR #68, P2-8). Its Arrange mode still counts: a lock, an
            # unlock or a laser test from it is held back while it arranges
            # (adversarial review of PR #73, finding 2).
            m.open_beat(device, arranging=body.get("arranging") is True,
                        inst=body.get("inst"), seq=body.get("seq"),
                        mark_open=False)
            return 200, {"ok": True, "open": False, "view_only": True,
                         "arranging": m.device_arranging(device)}
        # A purge fire is gated by what it DOES, never by whether "on" is
        # there: only a body users.purge_fire_wanted() says starts or
        # carries on a press needs the purge arming switch; every other
        # purge-fire body is a release, which morning.py turns into a stop
        # and which therefore goes whatever the switch says.
        timed_start = name == "purge-timed" and body.get("hold") is True
        if m.purge.timed_running() and (timed_start or (
                name == "purge-fire" and users_mod.purge_fire_wanted(body))):
            # A second purge press while a timed purge runs only ends it
            # (Purge.fire_beat, Purge.timed_beat): no switch, deck or
            # settings check may hold back a stop (review of #172).
            # end_only: if the purge ends between this look and the press,
            # the press is refused, never taken as a fresh one past the
            # switches (re-review of #172).
            if name == "purge-fire":
                return m.post(name, body, device, who, screen, seen_ok,
                              ctx.local, end_only=True)
            return m.post(name, body, device, who, screen, seen_ok,
                          ctx.local, owner=self._page_owner(ctx),
                          token=getattr(ctx, "token", "") or "",
                          end_only=True)
        if timed_start or \
                (name == "purge-select" and body.get("on") is not False) or \
                (name == "purge-fire" and users_mod.purge_fire_wanted(body)):
            if not self.purge_enabled(ctx.local):
                return 403, {"error": "Arming a purge from here is switched "
                                      "off (purge_arming, and from the show "
                                      "network screen_arming, in "
                                      "ltcplay_remote.json).",
                             "let_go": True}
        if ((name == "lock" and body.get("on") is False) or
                (name == "laser-test" and body.get("on") is True) or
                (name == "laser-arm" and body.get("on") is True)) and \
                not self.may_act_from(ctx.local):
            return 403, {"error": ("Unlocking" if name == "lock" else
                                   "Arming a laser" if name == "laser-arm"
                                   else "Turning a laser test on") +
                         " from a device on the show network needs "
                         "screen_arming true in ltcplay_remote.json. Do it on "
                         "the show machine itself."}
        if (name == "purge-select" and body.get("on") is not False) or \
                timed_start:
            try:
                deck_alive = self.clock() - (self._deck_seen or -1e9) <= \
                    self.DECK_QUIET_S
            except Exception:
                deck_alive = False
            if not deck_alive:
                return 409, {"error": "The Stream Deck program has not read "
                                      "this engine in the last 2 s, and it "
                                      "is what arms a group for the purge. "
                                      "Nothing was armed.", "cut": True}
        if name == "purge-timed":
            ts = self.timed_purge_settings()
            if timed_start and ts["error"]:
                return 409, {"error": f"No timed purge: {ts['error']}",
                             "cut": True}
            code, out = m.post(name, body, device, who, screen, seen_ok,
                               ctx.local, owner=self._page_owner(ctx),
                               token=getattr(ctx, "token", "") or "",
                               max_s=ts["max_s"])
            if code == 200 and out.get("started") is True:
                # The duration started last is this show PC's default next
                # time. After the start, never before it: a slow disk never
                # delays a purge, and a failed save changes nothing.
                try:
                    if load_settings(self.folder)["purge_timed_s"] != \
                            out.get("seconds"):
                        save_settings(self.folder,
                                      purge_timed_s=out.get("seconds"))
                except (ValueError, OSError, TypeError):
                    pass
            return code, out
        return m.post(name, body, device, who, screen, seen_ok, ctx.local)

    @staticmethod
    def _page_owner(ctx):
        """(device, window): one page load, as the cleaning and the
        Fireball page tell screens apart."""
        return (ctx.device, str(getattr(ctx, "window", "") or "")[:64])

    def timed_purge_settings(self):
        """The timed purge's numbers on this show machine: the presets, the
        page's default (the last started here) and the longest, from
        ltcplay_remote.json; "error" when that file cannot be read (then no
        timed purge starts)."""
        out = {"presets": list(TIMED_PRESETS_S), "default_s": TIMED_DEFAULT_S,
               "max_s": TIMED_MAX_DEFAULT_S, "min_s": TIMED_MIN_S,
               "limit_s": TIMED_MAX_LIMIT_S, "hold_s": SCREEN_HOLD_S,
               "error": ""}
        try:
            rs = load_settings(self.folder)
        except (ValueError, OSError) as e:
            out["error"] = str(e)
            return out
        out["max_s"] = rs["purge_timed_max_s"]
        out["default_s"] = min(rs["purge_timed_s"], rs["purge_timed_max_s"])
        return out

    def timed_purge_blockers(self, t):
        """morning.Purge's engine checks for a timed purge, at its start
        (`t` None) and on every send while it runs: a Hold standing, Stop
        under way, Run pressed or starting, the flame link not sending or
        alarmed, and (running) the starter's sign-in gone. "" or why.
        Plain reads; fails closed."""
        try:
            svc = self.schedule
            state = getattr(getattr(svc, "machine", None), "state", None)
            if state == "HOLD":
                return ("a Hold is standing; Resume or Reset first, then "
                        "start it again.")
            if getattr(svc, "halting", lambda: False)() is True:
                return "Stop is under way."
            if getattr(getattr(self.control, "session", None), "running",
                       False):
                return ("Run is pressed (a show file is running), so the "
                        "show file owns the flame channels; press Stop first.")
            try:
                cs = self.control.state() or {}
            except Exception:                    # noqa: BLE001
                cs = {}
            if cs.get("starting") or cs.get("running"):
                return ("Run is pressed (a show file is running), so the "
                        "show file owns the flame channels; press Stop first.")
            m = self.morning
            link = m._link_get() if m is not None else None
            if link is not None:
                try:
                    sender = link.sender_state()
                except Exception:                # noqa: BLE001
                    sender = "unknown"
                if sender != "running":
                    return (f"the flame link to flamesafe is not sending "
                            f"({sender}).")
                if getattr(link, "lock_alarm", ""):
                    return f"the flame link has an alarm: {link.lock_alarm}"
            if t is not None:
                # The Stream Deck program is what keeps the purge's groups
                # armed and the operator's own disarm path: once it stops
                # reading the engine, a timed purge ends (review of #172,
                # S1). The start has its own check (morning_post).
                seen = self._deck_seen
                if seen is None or self.clock() - seen > self.DECK_QUIET_S:
                    return (f"the Stream Deck program stopped reading this "
                            f"engine ({self.DECK_QUIET_S:g} s with nothing)")
            if t is not None and self.users_active():
                token = t.get("token") or ""
                if not token or self.users.session(token) is None:
                    return (f"{t.get('who') or 'the person who started it'} "
                            f"signed out")
        except Exception as e:                   # noqa: BLE001
            return f"the engine's state could not be read ({type(e).__name__})."
        return ""

    def timed_purge_cut(self, why, who="", screen="", token=None,
                        group=None):
        """End a running timed purge at once (a Hold, Start now, Run, Stop,
        a disarm of one of its groups, its starter signing out): every slot
        it fired back to zero and a journal line saying why. Never raises,
        never waits."""
        m = self.morning
        purge = getattr(m, "purge", None) if m is not None else None
        if purge is None:
            return False
        try:
            return purge.timed_end(why, device=screen or None, token=token,
                                   group=group)
        except Exception:                        # noqa: BLE001
            return False

    def _disarm_fn(self):
        if self._flame_disarm is not None:
            return self._flame_disarm
        cond = getattr(self.schedule, "conductor", None)
        show = getattr(cond, "show", None)
        fn = getattr(show, "flames_disarm_all", None)
        return fn

    def _disarm_now(self, who, screen, what):
        """(ok, sentence) of the flame link's disarm_all, called at once on
        this thread: no lock taken, nothing journaled, no disk touched.
        ok is None when no flame link is connected in this engine."""
        fn = self._disarm_fn()
        if fn is None:
            return None, ("No flame link is connected in this engine, so no "
                          "disarm could be sent from here. Disarm from the "
                          "Stream Deck.")
        label = who or "An unnamed operator"
        try:
            r = fn(f"{label} pressed {what} on the {screen}")
            ok = bool(getattr(r, "ok", False))
            said = str(getattr(r, "sentence", "") or "").strip()
        except Exception as e:
            ok, said = False, f"{type(e).__name__}: {e}"
        if ok:
            return True, (f"Every flame group: {said}" if said else
                          "Every flame group: a disarm was sent.")
        return False, (f"The disarm of every flame group did NOT go out "
                       f"({said}). Disarm from the Stream Deck.")

    def _lasers_disarm_now(self, who, screen, what):
        """Every laser disarmed (morning.Morning.lasers_disarm_now), on this
        thread, at once. Never raises; nothing when there is no Morning
        page (and so no laser arm) in this engine."""
        m = self.morning
        fn = getattr(m, "lasers_disarm_now", None)
        if fn is None:
            return
        label = who or "An unnamed operator"
        if what == "announcement":
            # Said as the announcement it was, not as a Hold press (delta
            # review of #129, 8).
            text = (f"An announcement played by {label} on the {screen} "
                    f"held the schedule")
        else:
            text = (f"{'Abort' if what == 'abort' else 'Hold'} pressed by "
                    f"{label} on the {screen}")
        try:
            fn(text, who, screen)
        except Exception:                        # noqa: BLE001
            pass

    def last_show_over(self):
        """For users.Users.last_show (sign everyone out after the last
        show, Jeff, 2026-10-26): {"night", "ended"} once the night's last
        show has ended, else None.

        The night is the schedule's own (its 02:00 reset, schedule.night_of),
        and "ended" is when the last show of it that really ran ended (a
        slot's ended_at; a restart's assumed end does not count). A show
        started by hand (Start now) counts, but only on a night that had a
        show on the schedule: without one, never (review of PR #125, P2-8).
        A night whose last show ended shortly before the 02:00 reset still
        gets its sign-out after the reset (remembered here).

        None with no schedule, while a show is live, paused or on Hold,
        while a show is still to come or waiting after a Hold, while an
        Abort stands, while the show clock plays, and while anything is
        going on at the rig (rig_busy with armed: a hold to arm, a purge,
        a laser test, an armed laser or flame group): it waits until that
        is clear (P2-5). Plain reads, never a lock."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        if m is None:
            return None
        try:
            if self.show_live_refusal():
                return None
            cond = getattr(svc, "conductor", None)
            if getattr(m, "state", None) in ("SHOW", "PAUSED", "HOLD") or \
                    getattr(cond, "_look", None) == "HELD" or \
                    getattr(m, "running", 0):
                return None
            if self.rig_busy(armed=True):
                return None
            slots = list(getattr(m, "slots", ()) or ())
            ends = [s.ended_at for s in slots
                    if getattr(s, "ended_at", None) is not None]
            scheduled = any(getattr(s, "origin", "rule") != "operator"
                            for s in slots)
            if ends and scheduled:
                self._last_show_seen = {"night": str(m.date),
                                        "ended": max(ends).timestamp()}
                if not m.waiting():
                    return dict(self._last_show_seen)
            seen = self._last_show_seen
            if seen is not None and seen["night"] != str(m.date):
                # That night closed at its reset (nothing of it can still
                # run), so its last show is over whatever tonight holds.
                return dict(seen)
            return None
        except Exception:                        # noqa: BLE001
            return None

    def laser_arm_blockers(self):
        """Why no laser may be armed (nor BEYOND's laser output switched
        on) right now, or "": the Abort latch until Reset, a Hold until
        Resume, the night closing, and no schedule at all (the show state
        unknown). Plain reads, never a lock: the laser gate asks this."""
        svc = self.schedule
        m = getattr(svc, "machine", None)
        if m is None:
            return ("the show state is not known (no schedule is loaded), "
                    "so no laser may be armed")
        cond = getattr(svc, "conductor", None)
        if getattr(m, "abort_latched", False) or \
                getattr(cond, "latched", False):
            return "the show is aborted; press Reset first, then arm"
        state = getattr(m, "state", None)
        if state in ("PAUSED", "HOLD") or \
                getattr(cond, "_look", None) in ("HELD", "DARK"):
            return "the show is on Hold; press Resume first, then arm"
        if state == "CLOSING":
            return "the night is closing"
        return ""

    def _lasers_dark_now(self, what="Abort pressed on a screen"):
        """(ok or None, sentence): the conductor's laser blank, on this
        thread, at once. None when no conductor is attached."""
        cond = getattr(self.schedule, "conductor", None)
        fn = getattr(cond, "lasers_dark_now", None)
        if fn is None:
            return None, ""
        try:
            r = fn(what)
            ok = bool(getattr(r, "ok", False))
        except Exception as e:
            return False, (f"The laser blank did NOT go out at the press "
                           f"({type(e).__name__}: {e}).")
        return ok, ("Lasers blanked at the press." if ok else
                    "The laser blank did NOT go out at the press.")

    def stop_press(self, who, screen):
        """Stop pressed on a screen, the rack screen or an iPad (Jeff,
        2026-10-28): exactly the Stream Deck's Stop. halt() first (every
        flame group and laser disarmed, nothing fired, a live show ended
        and faded to dark, nothing latched), the fade let run, then
        playback stops, so Run is off. (code, answer) in /api/stop's
        shape: {"flames", "lasers", "lasers_blank", "show", "failed",
        "stopped"}, with "error" when a step failed. Never raises."""
        svc = self.schedule
        begin = getattr(svc, "halt_begin", None)
        if begin is not None:
            begin()
        try:
            return self._stop_press(who, screen)
        finally:
            end = getattr(svc, "halt_end", None)
            if end is not None:
                end()

    def _stop_press(self, who, screen):
        h = self.halt(who, screen)
        out = {k: h[k] for k in ("flames", "lasers", "lasers_blank", "show")
               if h.get(k) is not None}
        out["failed"] = list(h.get("failed") or [])
        if h.get("show_ended"):
            svc = self.schedule
            try:
                svc.flush_conductor(STOP_FADE_WAIT_S)
            except Exception:                    # noqa: BLE001
                pass
            cond = getattr(svc, "conductor", None)
            if cond is not None and getattr(cond, "threaded", False):
                try:
                    cond.wait_idle(STOP_FADE_WAIT_S)
                except Exception:                # noqa: BLE001
                    pass
        try:
            out["stopped"] = bool(self.control.stop())
        except Exception as e:                   # noqa: BLE001
            out["stopped"] = False
            out["failed"].append("playback stop")
            out["error"] = f"{type(e).__name__}: {e}"
        try:
            self.control._state_cache = None
        except Exception:                        # noqa: BLE001
            pass
        if out["failed"] and "error" not in out:
            out["error"] = ("Stop did not complete: " +
                            ", ".join(out["failed"]) + " did NOT work.")
        return (500 if "error" in out else 200), out

    def halt(self, who, screen):
        """The Stream Deck's Stop, a complete halt of everything (Jeff,
        2026-10-26), during a scheduled show too: everything a screen's
        Abort takes away, in the Abort's order, WITHOUT the Abort latch.
        Each step runs whatever the one before it did, and none raises:

          1. every flame group disarmed through the flame link (the
             flame half of an Abort, _disarm_now), before anything else;
          2. live flame programming cut, the link's slot shut at once and
             the rest of ending it on a thread of its own, exactly as an
             Abort does;
          3. every laser disarmed (the laser arm, told a press is under
             way first, as for an Abort) and blanked at once
             (_lasers_dark_now, its answer reported as "lasers_blank");
          4. every screen arm hold let go, the purge selection cleared and
             the laser tests ended (what an Abort clears);
          5. a scheduled show that is live ended: the scheduler's Stop
             (STOP_SHOW), faded to dark like an Abort, nothing latched.
             A show that ended by itself meanwhile is not a failure.

        {"flames", "lasers", "lasers_blank", "show", "show_ended",
        "failed"}: each step's
        {"ok"/"disarmed", "text"} (None when there is nothing of it in
        this engine) and the names of the steps that failed. The caller
        (web.py /api/stop) stops playback after it."""
        label = who or "An unnamed operator"
        out = {"flames": None, "lasers": None, "lasers_blank": None,
               "show": None, "show_ended": False, "failed": []}
        # A Start now waiting for the show audio never outlives a Stop
        # (review of #144, F2).
        cancel = getattr(self.schedule, "cancel_start_wait", None)
        if cancel is not None:
            try:
                cancel(f"Stop was pressed on the {screen}")
            except Exception:                    # noqa: BLE001
                pass
        m = self.morning
        arm = getattr(m, "laser_arm", None)
        if arm is not None:
            try:
                arm.press_begin()
            except Exception:                    # noqa: BLE001
                arm = None
        try:
            # 1. The flames first, every time (the Abort's rule).
            try:
                f_ok, f_text = self._disarm_now(who, screen, "Stop")
            except Exception as e:               # noqa: BLE001
                f_ok, f_text = False, f"{type(e).__name__}: {e}"
            if f_ok is not None:
                out["flames"] = {"disarmed": bool(f_ok), "text": f_text}
                if not f_ok:
                    out["failed"].append("flames")
            # 2. Live flame programming, cut as an Abort cuts it (review of
            # #130, second pass, 3).
            why = f"{label} held Stop on the {screen}"
            self.timed_purge_cut(why, who, screen)
            for name in ("live_flame_cut",):
                fn = getattr(self, name, None)
                try:
                    if fn is not None:
                        fn(why, who, screen)
                except Exception:                # noqa: BLE001
                    pass
            # The hazers OFF too (Jeff, 2026-10-29: Stop is a complete
            # halt). The flame link's disarm_all in step 1 did it at the
            # link already; this is the belt, and the journal line.
            self.hazers_cut(why, who, screen)
            # The fuel pump OFF too (Jeff, 2026-10-30: "it should be
            # affected by the stop button"); step 1's disarm_all did it at
            # the link already.
            self.pump_cut(why, who, screen)
            # 3. Every laser: disarmed on the arm, and blanked at once.
            fn = getattr(m, "lasers_disarm_now", None)
            if fn is not None:
                try:
                    r = fn(f"Stop held by {label} on the {screen}", who,
                           screen)
                    if isinstance(r, tuple) and len(r) == 2:
                        l_ok, l_text = r
                    else:
                        l_ok = getattr(r, "ok", True)
                        l_text = getattr(r, "sentence", "")
                    l_ok = bool(l_ok)
                    l_text = str(l_text or "Every laser: a disarm was sent.")
                except Exception as e:           # noqa: BLE001
                    l_ok, l_text = False, (f"The laser disarm did NOT go "
                                           f"out ({type(e).__name__}: {e}).")
                out["lasers"] = {"disarmed": l_ok, "text": l_text}
                if not l_ok:
                    out["failed"].append("lasers")
            try:
                b_ok, b_text = self._lasers_dark_now(
                    f"Stop held on the {screen}")
            except Exception as e:               # noqa: BLE001
                b_ok, b_text = False, (f"The laser blank did NOT go out "
                                       f"({type(e).__name__}: {e}).")
            if b_ok is not None:
                out["lasers_blank"] = {"ok": bool(b_ok), "text": b_text}
                if not b_ok:
                    out["failed"].append("laser blank")
            # 4. What an Abort clears: screen holds, the purge, laser tests.
            try:
                self.cancel_holds(f"{who or 'someone'} held Stop on the "
                                  f"{screen}")
            except Exception:                    # noqa: BLE001
                pass
            for obj, meth, arg in (
                    (getattr(m, "purge", None), "clear",
                     f"Stop held by {who or screen or 'someone'}"),
                    (getattr(m, "lasers", None), "all_off", "Stop held")):
                fn = getattr(obj, meth, None)
                try:
                    if fn is not None:
                        fn(arg)
                except Exception:                # noqa: BLE001
                    pass
            # 5. A live scheduled show ends, with no latch.
            svc = self.schedule
            state = getattr(getattr(svc, "machine", None), "state", None)
            if svc is not None and state in ("SHOW", "PAUSED"):
                try:
                    r = svc.operator_press("stop-show", who, screen,
                                           confirmed=True)
                    s_ok, s_text = bool(r.get("ok")), str(r.get("text")
                                                          or "")
                except Exception as e:           # noqa: BLE001
                    s_ok, s_text = False, f"{type(e).__name__}: {e}"
                now_state = getattr(getattr(svc, "machine", None), "state",
                                    None)
                if not s_ok and now_state not in ("SHOW", "PAUSED"):
                    # The show ended by itself while Stop was on its way
                    # (third review of #130, 8): nothing left to stop.
                    s_ok, s_text = True, ("The show had already ended, so "
                                          "there was no show to stop.")
                else:
                    out["show_ended"] = s_ok
                out["show"] = {"ok": s_ok, "text": s_text}
                if not s_ok:
                    out["failed"].append("show")
        finally:
            if arm is not None:
                try:
                    arm.press_end()
                except Exception:                # noqa: BLE001
                    pass
        parts = [x["text"] for x in (out["flames"], out["lasers"],
                                     out["lasers_blank"]) if x is not None]
        bad = ", ".join(out["failed"])
        self._journal(who, screen, "stop", "fault" if bad else "done",
                      f"{label} held Stop on the {screen}: a complete halt. "
                      + " ".join(parts)
                      + (f" These did NOT work: {bad}." if bad else ""),
                      fault=bool(bad))
        return out

    def abort(self, who, screen, flames, lasers=(None, ""), pressed=None):
        """A screen's Abort (the deck has no Abort key since 2026-10-26).
        `flames` is what the disarm
        sent before anything else did (_disarm_now). Then the scheduler's
        Abort, which stops a live show through the conductor. The answer
        says both, truthfully: a disarm sent with no show to stop is not
        "aborted", and a stopped show whose disarm failed is a fault."""
        f_ok, f_text = flames
        label = who or "An unnamed operator"
        self._journal(who, screen, "abort disarm",
                      "fault" if f_ok is False else
                      ("done" if f_ok else "not connected"),
                      f"{label} pressed Abort on the {screen}. {f_text}",
                      fault=f_ok is False)
        svc = self.schedule
        if svc is None:
            s_ok, s_text = False, ("There is no scheduler running in this "
                                   "engine, so no show was stopped.")
        else:
            try:
                r = svc.operator_press("abort", who, screen, confirmed=True,
                                       pressed=pressed)
                s_ok = bool(r.get("ok"))
                s_text = str(r.get("text") or "")
            except ValueError as e:
                s_ok, s_text = False, str(e)
            if not s_ok:
                s_text = f"No show was stopped: {s_text}"
        text = f"{f_text} {lasers[1]} {s_text}".strip().replace("  ", " ")
        # A disarm that went out is the Abort's safety half done, whatever
        # the scheduler said; one that failed is a fault even if the show
        # stopped. With no flame link at all, the scheduler decides.
        ok = f_ok is True or (f_ok is None and s_ok)
        out = {"ok": ok, "text": text, "disarmed": f_ok, "stopped": s_ok,
               "lasers_blanked": lasers[0]}
        if not ok:
            out["error"] = text
        return (200 if ok else 409), out

    def disarm_all(self, who, screen):
        """Disarm all flame groups: a PLAIN disarm (Jeff, 2026-10-23), the
        same thing a group's own Disarm is, for every group at once: handed
        to the Stream Deck process, which owns the arm link, as an instant
        tap of each group's key (wanted false). flamesafe disarms on the
        next arm frame, and a group is armed again by an ordinary hold:
        no Abort latch and no forced arm cycle (only the show's Abort and
        a fault force one, CONTRACT.md "Disarm every group").

        Safety first, always: never gated by a role, a sign-in, Arrange,
        screen arming or a lockout, never waits on a fresh page or a show,
        and never touches the lasers. When the plain disarm cannot be
        trusted to land, the flame link's disarm_all goes instead (the
        Abort's disarm, which forces a fresh arm cycle) and the page says
        so:
          - the Stream Deck program has not read this engine within
            PLAIN_DECK_FRESH_S, or flamesafe names no groups: at once;
          - flamesafe has not reported every group that was armed or
            wanted at the press as off and not wanted within
            PLAIN_CONFIRM_S: then, journaled as a fault, with an alert."""
        label = who or "An unnamed operator"
        names, watch = self._plain_disarm_groups()
        seen = self._deck_seen
        deck_fresh = (seen is not None and
                      self.clock() - seen <= PLAIN_DECK_FRESH_S)
        if not names or not deck_fresh:
            why = ("flamesafe names no flame groups to this engine"
                   if not names else
                   "the Stream Deck program is not reading this engine, "
                   "and it is what carries a plain disarm")
            return self._disarm_all_forced(who, screen, why)
        self._drop_holds_all()
        with self._arm_lock:
            for i, gname in enumerate(names):
                self._disarm_ids += 1
                self._disarms.append({"id": self._disarm_ids, "group": i,
                                      "name": gname, "who": who,
                                      "device": screen, "all": True})
            self._plain_gen += 1
            gen = self._plain_gen
        text = (f"{label} pressed Disarm all flame groups on the {screen}. "
                f"Every group was sent a plain disarm through the Stream "
                f"Deck, which owns the arm link; flamesafe's lamps show each "
                f"one off. A plain disarm needs no reset: hold a group's Arm "
                f"to arm it again.")
        self._journal(who, screen, "disarm all", "sent", text)
        try:
            self._later(PLAIN_CONFIRM_S,
                        lambda: self.confirm_plain_disarm(gen, who, screen,
                                                          watch))
        except Exception:                        # noqa: BLE001
            # No way to check it later: the forced disarm now, never a
            # disarm left unconfirmed.
            self.confirm_plain_disarm(gen, who, screen, None)
        return 200, {"ok": True, "plain": True, "text": text}

    def _plain_disarm_groups(self):
        """(group names flamesafe reports, the indices to confirm off):
        every group armed or wanted in a fresh status, or every group
        when the status is stale. Never raises."""
        fs = self.flame_status
        try:
            v = fs.view() if fs is not None else None
        except Exception:                        # noqa: BLE001
            v = None
        if not v:
            return [], set()
        groups = v.get("groups") or []
        names = [str(g.get("name", "")) for g in groups]
        if v.get("stale"):
            return names, set(range(len(names)))
        return names, {i for i, g in enumerate(groups)
                       if g.get("armed") != "disarmed" or g.get("wanted")}

    def _drop_holds_all(self):
        with self._arm_lock:
            self._holds.clear()

    @staticmethod
    def _timer(seconds, fn):
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()

    def confirm_plain_disarm(self, gen, who, screen, watch):
        """PLAIN_CONFIRM_S after a plain Disarm all flame groups: every
        group in `watch` must read off and not wanted in a fresh flamesafe
        status, else the flame link's disarm_all goes now (the forced
        disarm, a fresh arm cycle), journaled as a fault with an alert.
        `watch` None: forced at once. A newer Disarm all supersedes this
        one. Never raises."""
        with self._arm_lock:
            if gen != self._plain_gen:
                return None
        left = None
        if watch is not None:
            try:
                v = self.flame_status.view()
                if not v.get("stale"):
                    gs = v.get("groups") or []
                    left = [gs[i]["name"] for i in sorted(watch)
                            if i < len(gs) and
                            (gs[i].get("armed") != "disarmed" or
                             gs[i].get("wanted"))]
            except Exception:                    # noqa: BLE001
                left = None
        if left == []:
            self._journal(who or "system", screen or "engine", "disarm all",
                          "confirmed", "Disarm all flame groups: flamesafe "
                          "reports every group off.")
            return True
        why = (f"flamesafe still reported {', '.join(left)} on "
               f"{PLAIN_CONFIRM_S:g} s after the plain disarm" if left else
               f"flamesafe did not confirm every group off within "
               f"{PLAIN_CONFIRM_S:g} s of the plain disarm")
        code, out = self._disarm_all_forced(who, screen, why)
        if out.get("ok"):
            text = (f"Disarm all flame groups was not confirmed: {why}. "
                    f"Every group was sent the disarm Abort uses, so each "
                    f"needs Arm again (let go, then hold) before it can "
                    f"fire. {out.get('text') or ''}")
        else:
            # Review of PR #97, P2-4: never say off when it may not be.
            text = (f"Disarm all flame groups COULD NOT be confirmed: {why}, "
                    f"and the Abort's disarm did not go out either "
                    f"({out.get('error') or out.get('text') or 'no answer'})."
                    f" A group may still be armed: press Abort, or disarm "
                    f"at the Stream Deck.")
        try:
            self.alerts.raise_("flames", text=text)
        except Exception:                        # noqa: BLE001
            self._journal(who or "system", screen or "engine",
                          "disarm all", "fault", text, fault=True)
        return False

    def _disarm_all_forced(self, who, screen, why):
        """The flame link's disarm_all (the Abort's disarm, which forces a
        fresh arm cycle), for when a plain disarm cannot be trusted."""
        fn = self._disarm_fn()
        label = who or "An unnamed operator"
        if fn is None:
            text = (f"{label} pressed Disarm all flame groups on the "
                    f"{screen}, but {why}, and no flame link is connected "
                    f"in this engine, so nothing could be sent. Disarm from "
                    f"the Stream Deck.")
            self._journal(who, screen, "disarm all", "fault", text,
                          fault=True)
            return 409, {"ok": False, "error": text}
        try:
            r = fn(f"{label} pressed Disarm all flame groups on the "
                   f"{screen} ({why}: disarmed the Abort way)")
            ok = bool(getattr(r, "ok", False))
            said = str(getattr(r, "sentence", "") or "")
        except Exception as e:
            ok, said = False, f"{type(e).__name__}: {e}"
        text = (f"{label} pressed Disarm all flame groups on the {screen}. "
                f"A plain disarm could not go ({why}), so every group was "
                f"disarmed the way Abort does it: each needs Arm again (let "
                f"go, then hold) before it can fire. {said}").strip()
        self._journal(who, screen, "disarm all", "done" if ok else "fault",
                      text, fault=not ok)
        return (200 if ok else 409), {"ok": ok, "plain": False,
                                      "text": text}

    # -- status ------------------------------------------------------------
    DECK_QUIET_S = 2.0

    def cues_fault(self):
        """The flame cues' CUES FAULT for the screens: the refusal now in
        force, else a render older than the layout found at show load
        (FlameCues.faults_now). "" when none. Never raises."""
        c = self.flame_cues
        try:
            now = getattr(c, "faults_now", None)
            return str((now() if now is not None
                        else getattr(c, "fault", "")) or "")
        except Exception:                        # noqa: BLE001
            return ""

    def aim_view(self, fl):
        """The aim slots (2026-10-28), display only: each group of `fl` (a
        FlameStatus view) with aim slots on either side gets "aim": its
        slots, flamesafe's list (None from an older flamesafe), this
        engine's (None without a flame link), "loaded" (both the same and
        not empty), and the values asked for and sent on those slots in
        flamesafe's last fresh frame (None while stale). Returns the whole
        picture, and raises an "aim" alert (display only: alerts.
        DISPLAY_ONLY) while the two lists differ (cleared once they
        agree). Nothing here is sent anywhere, and it
        never touches Stop, Hold, Abort or a disarm. Never raises."""
        try:
            return self._aim_view(fl or {})
        except Exception:                        # noqa: BLE001
            return {"ltcplay": None, "flamesafe": None, "path": "",
                    "agree": None}

    def _aim_view(self, fl):
        src = self.flame_aim
        mine = (None if src is None
                else sorted(int(x) for x in (src[0] or ())))
        path = "" if src is None else str(src[1] or "")
        groups = fl.get("groups") or []
        live = bool(fl.get("connected") and not fl.get("stale"))
        theirs = None
        if live and groups and all(isinstance(g.get("aim_slots"), list)
                                   for g in groups):
            theirs = sorted({int(a) for g in groups for a in g["aim_slots"]})
        for g in groups:
            fire = g.get("fire_slots") or []
            fs = g.get("aim_slots")
            fs = sorted(fs) if isinstance(fs, list) else None
            lt = None if mine is None else [x for x in mine if x in fire]
            slots = sorted(set(fs or ()) | set(lt or ()))
            if not slots:
                continue

            def at(vals):
                if not live or not isinstance(vals, list) or \
                        len(vals) != len(fire):
                    return None
                return [vals[fire.index(x)] for x in slots]
            g["aim"] = {"slots": slots, "flamesafe": fs, "ltcplay": lt,
                        "loaded": bool(fs) and fs == lt,
                        "asked": at(g.get("commanded_fire")),
                        "sent": at(g.get("sent_fire"))}
        agree = None if theirs is None or mine is None else theirs == mine
        self._aim_alert(agree, mine, theirs, path)
        return {"ltcplay": mine, "flamesafe": theirs, "path": path,
                "agree": agree}

    @staticmethod
    def aim_mismatch_text(mine, theirs, path):
        """The alert's sentence while the lists differ: what each has, the
        file this engine read, and what to do."""
        def words(v):
            return ", ".join(str(x) for x in v) if v else "none"
        return (f"Aim slots do not match: flamesafe has {words(theirs)} and "
                f"LTC Player has {words(mine)} (LTC Player read "
                f"{path or 'flamesafe config'} when it started). Until they "
                f"match a wave flamer's angle can stay at 0, its end stop. "
                f"Put the aim list in the flamesafe.json flamesafe uses, "
                f"check that LTC Player reads that same file, then quit LTC "
                f"Player and start it again so both read it afresh. Stop, "
                f"Hold, Abort and Disarm all work as usual meanwhile.")

    def _aim_alert(self, agree, mine, theirs, path):
        """Raise once per new mismatch sentence; clear the one raised here
        once the lists agree. Not known (no fresh frame, an older
        flamesafe, no flame link): nothing changes."""
        if agree is None:
            return
        with self._aim_lock:
            if agree:
                aid, self._aim_alert_id = self._aim_alert_id, None
                was, self._aim_said = self._aim_said, None
                if was is None:
                    return
                self._journal("system", "engine", "aim slots", "match",
                              f"Aim slots match again: flamesafe and LTC "
                              f"Player both have "
                              f"{', '.join(str(x) for x in mine) or 'none'}.")
                # Only this alert's own id: the "aim" kind is raised
                # nowhere else.
                if aid is not None and any(
                        a.id == aid for a in self.alerts.open()):
                    self.alerts.clear(id=aid, action="Confirm",
                                      who="LTC Player", screen="engine")
                return
            text = self.aim_mismatch_text(mine, theirs, path)
            if text == self._aim_said:
                return
            self._aim_said = text
            try:
                a = self.alerts.raise_("aim", text)
                self._aim_alert_id = getattr(a, "id", None)
            except Exception:                    # noqa: BLE001
                self._journal("system", "engine", "aim slots", "differ",
                              text, fault=True)

    def render_fault_alert(self, text):
        """FlameCues.on_render_fault (web.serve): a render older than the
        layout, found at show load, is a flames alert on every screen and
        a booth note, not a journal line nobody reads (soak 2026-10-21).
        FlameCues calls this once for each new sentence."""
        try:
            self.alerts.raise_("flames", text)
        except Exception:                        # noqa: BLE001
            pass

    def show_folder_problem(self):
        """show_folder_mismatch for this engine and showpc.json (read at
        most every 10 s; off the show PC there is none). Journaled once
        for each new sentence. Never raises."""
        try:
            doc = self._settings_json(SHOWPC_FILE, "showpc", 10.0)
            text = show_folder_mismatch(self._show_folder(),
                                        doc.get("show_folder"))
        except Exception:                        # noqa: BLE001
            text = ""
        if text and text != getattr(self, "_folder_said", ""):
            self._journal("system", "engine", "show folder", "mismatch",
                          text, fault=True)
        self._folder_said = text
        return text

    def _time_service(self):
        now = self.clock()
        got = getattr(self, "_w32time", None)
        if got is not None and now - got[1] < TIME_SERVICE_READ_S:
            return got[0]
        st = windows_time_service(getattr(self, "_sc_run", None),
                                  getattr(self, "_sc_windows", None))
        self._w32time = (st, now)
        return st

    def engine_checks(self):
        """The Morning page's engine checks, worked out at most every
        CHECKS_READ_S: [{"id", "level" ("ok", "warn", "bad", "unknown"),
        "title", "text"}]. Display only. Never raises."""
        now = self.clock()
        got = getattr(self, "_checks", None)
        if got is not None and now - got[1] < CHECKS_READ_S:
            return got[0]
        out = []
        if hasattr(self.flame_cues, "faults_now"):
            try:
                from . import fire_ice as fi
                faults = fi.show_folder_render_faults(self._show_folder())
                running = self.cues_fault()
                if running and running not in faults:
                    faults.insert(0, running)
            except Exception as e:               # noqa: BLE001
                faults = [f"The renders could not be checked: "
                          f"{type(e).__name__}: {e}"]
            out.append({"id": "renders", "title": "Flame cue renders",
                        "level": "bad" if faults else "ok",
                        "text": " ".join(faults) if faults else
                        "Every show file's renders are newer than its "
                        "xlights_networks.xml, so their flame cues can "
                        "play."})
        try:
            cc = getattr(self.schedule, "clock_check", None) or {}
            level, text = judge_pc_clock(cc.get("offset_s"),
                                         self._time_service())
        except Exception as e:                   # noqa: BLE001
            level, text = "unknown", f"The clock check failed: {e}"
        out.append({"id": "clock", "title": "This PC's clock",
                    "level": level, "text": text})
        out.append(self._artnet_check())
        folder = self._show_folder()
        problem = self.show_folder_problem()
        out.append({"id": "show_folder", "title": "Show folder served",
                    "level": "bad" if problem else "ok",
                    "text": problem or f"This engine serves {folder}."})
        self._checks = (out, now)
        return out

    def artnet_faults(self):
        """artnodes.folder_faults for the show folder, worked out at most
        every CHECKS_READ_S: ([(level, sentence)], show files looked at).
        Never raises."""
        now = self.clock()
        active = self._active_show_files()
        got = getattr(self, "_artnet", None)
        # A newly loaded show is judged at once (review of PR #97, P2-1),
        # not on the next CHECKS_READ_S.
        if got is not None and now - got[1] < CHECKS_READ_S and \
                got[2] == active:
            return got[0]
        try:
            from . import artnodes
            adapters = getattr(self, "_artnet_adapters",
                               artnodes.adapter_addresses)
            res = artnodes.folder_faults(
                self._show_folder(), adapters(),
                getattr(self, "_artnet_probe", artnodes.bind_probe),
                getattr(self, "_artnet_windows", None), active=active)
        except Exception as e:                   # noqa: BLE001
            res = ([("warn", f"The Art-Net receivers could not be checked: "
                             f"{type(e).__name__}: {e}")], 0)
        self._artnet = (res, now, active)
        return res

    # Written by the first build of #97 at every Run; no longer read: a
    # show last Run is not the selection (second review of #97, P2-a).
    LAST_SHOW_FILE = "ltcplay_last_show.json"

    SELECTED_SHOW_FILE = "ltcplay_selected_show.json"
    SELECTED_READ_S = 2.0

    def _active_show_files(self):
        """The show file in use, for the Art-Net receiver check (Jeff,
        2026-10-23: an unused soak_show.json was a live fault), as a set of
        one name: the one loaded now, else the one last Run in this
        engine, else the one the Programming screen's show picker has
        selected (SELECTED_SHOW_FILE in the settings folder: written by the
        picker and by every Run, so it is the show Run will run), while
        that file is still in the show folder. None when no show is loaded
        or selected: then every show file is checked as a fault, each
        named (second review of PR #97, P2-a: a show last Run is not
        assumed to be tonight's unless the picker says so). The selection
        file is read at most every SELECTED_READ_S (P3-4). Never
        raises."""
        name = None
        try:
            s = getattr(self.control, "session", None)
            p = getattr(s, "timeline_path", None) or getattr(s, "path",
                                                             None)
            name = os.path.basename(str(p)) if p else None
            if not name:
                last = getattr(self.control, "last_timeline", None)
                name = os.path.basename(str(last)) if last else None
        except Exception:                        # noqa: BLE001
            name = None
        if name:
            if name != self._selected_show():
                self._save_selected_show(name)
            return {name}
        sel = self._selected_show()
        return {sel} if sel else None

    def _selected_show(self):
        """The picker's selection (or the last Run), if that show file is
        still in the show folder, else None. Cached SELECTED_READ_S."""
        now = self.clock()
        pend = getattr(self, "_sel_pending", None)
        if pend:
            return pend        # chosen, its write still on its way
        got = getattr(self, "_sel_cache", None)
        if got is not None and now - got[1] < self.SELECTED_READ_S:
            return got[0]
        sel = None
        try:
            with open(os.path.join(self.folder, self.SELECTED_SHOW_FILE),
                      encoding="utf-8") as fh:
                was = os.path.basename(str(json.load(fh).get("show") or ""))
            if was and os.path.isfile(os.path.join(self._show_folder(),
                                                   was)):
                sel = was
        except (OSError, ValueError, AttributeError):
            sel = None
        self._sel_cache = (sel, now)
        return sel

    def _save_selected_show(self, name):
        """`name` is the show in use from now on: known at once, and
        written to SELECTED_SHOW_FILE on a thread of its own, so a stalled
        disk never holds up the request that chose it (Run's reply, a
        status poll, the picker; review of #102, P2-c). Never raises."""
        self._sel_cache = (name, self.clock())
        self._sel_pending = name
        self._sel_idle.clear()
        try:
            threading.Thread(target=self._selection_writer, daemon=True,
                             name="ltcplay-show-select").start()
        except RuntimeError:
            self._selection_writer()

    def _selection_writer(self):
        with self._sel_lock:
            name = self._sel_pending
            if name is not None:
                self._write_selection_file(name)
            if self._sel_pending == name:
                self._sel_pending = None
                self._sel_idle.set()

    def _write_selection_file(self, name):
        """One whole file or none: a temp file beside it, then one
        os.replace. Never raises."""
        path = os.path.join(self.folder, self.SELECTED_SHOW_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"show": name}, fh)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def selection_written(self, timeout=5.0):
        """True once no selection write is waiting (tests)."""
        return self._sel_idle.wait(timeout)

    def select_show(self, body):
        """POST show-select {"file"}: the Programming screen's show picker
        changed (the show machine only). Display and checks only: nothing
        runs until Run."""
        f = os.path.basename(str((body or {}).get("file") or ""))
        try:
            names = [t.get("file") for t in self.control.timelines()]
        except Exception:                        # noqa: BLE001
            names = []
        if not f or f not in names:
            return 400, {"error": "That is not a show file in the show "
                                  "folder. Nothing was changed."}
        self._save_selected_show(f)
        self._artnet = None
        self._checks = None
        return 200, {"ok": True, "selected": f}

    def _artnet_check(self):
        """The Morning page's Art-Net receivers card."""
        faults, seen = self.artnet_faults()
        if self._artnet_unchosen(seen) and \
                any(lv != "note" for lv, _t in faults):
            # No show in use and more than one show file (2026-10-24):
            # what was found is said, amber, and is not a fault.
            return {"id": "artnet", "title": "Art-Net timecode receivers",
                    "level": "warn",
                    "text": self.AMBIGUOUS + " ".join(t for _lv, t in faults)}
        notes = [t for lv, t in faults if lv == "note"]
        faults = [(lv, t) for lv, t in faults if lv != "note"]
        levels = {lv for lv, _t in faults}
        level = "bad" if "bad" in levels else \
            "warn" if levels else "ok"
        if faults:
            text = " ".join(t for _lv, t in faults)
        elif notes:
            text = ("No Art-Net timecode receiver fault in the show in "
                    "use.")
        elif seen:
            text = ("Every Art-Net timecode receiver in the show files has "
                    "its own address, and each one is on this PC.")
        else:
            text = ("No show file in the folder names Art-Net timecode "
                    "receivers (clock.artnet.nodes).")
        if notes:
            text += " Other show files, not checked as faults: " + \
                " ".join(notes)
        return {"id": "artnet", "title": "Art-Net timecode receivers",
                "level": level, "text": text}

    AMBIGUOUS = ("No show is loaded, Run or picked yet, so no show file "
                 "is judged as a fault: what the check found in them is a "
                 "note. Pick the show on the Programming screen (or Run "
                 "it) and that one is checked. ")

    def _artnet_unchosen(self, seen):
        """True when the last check judged no show file in use (none
        loaded, Run or picked) and there is more than one show file. Then
        nothing found is a FAULT or an alert (Jeff, 2026-10-24: after a
        restart an unused soak_show.json was a red FAULT on the rack
        screen): it is one collapsed note on the rack screen
        (artnet_note) and an amber line on the Morning card. The show
        actually being run is loaded, so it is always judged as a fault;
        so is the only show file in the folder."""
        got = getattr(self, "_artnet", None)
        active = got[2] if got is not None else None
        return active is None and seen > 1

    def artnet_note(self):
        """The rack screen's one collapsed note while no show is in use
        (_artnet_unchosen): AMBIGUOUS and what was found, or "". Never
        raises."""
        try:
            faults, seen = self.artnet_faults()
            found = [t for lv, t in faults if lv in ("bad", "warn")]
            if found and self._artnet_unchosen(seen):
                return "Art-Net receivers, a note: " + self.AMBIGUOUS + \
                    " ".join(found)
        except Exception:                        # noqa: BLE001
            pass
        return ""

    def note_run(self, path):
        """Run started `path` (web.py's /api/start, once the show is open):
        it is the show in use from now on, written to SELECTED_SHOW_FILE
        at once, so a restart judges it even if no status was asked for
        between the Run and the restart (2026-10-24). Never raises."""
        try:
            name = os.path.basename(str(path or ""))
            if name:
                self._save_selected_show(name)
                self._artnet = None
        except Exception:                        # noqa: BLE001
            pass

    def artnet_alert(self):
        """A timecode alert on every screen (the rack screen's strip, the
        booth note) for the Art-Net receiver faults ("bad" only: two
        receivers on one address, or a node address not on this PC), once
        for each new sentence. Warnings stay on the Morning card. Never
        raises; never blocks a show."""
        try:
            faults, seen = self.artnet_faults()
            text = " ".join(t for lv, t in faults if lv == "bad")
            if self._artnet_unchosen(seen):
                text = ""         # a note on the rack screen, not an alert
            if text and text != getattr(self, "_artnet_said", ""):
                self.alerts.raise_("timecode", text)
            self._artnet_said = text
        except Exception:                        # noqa: BLE001
            pass

    def beyond_link(self):
        """beyondwatch.BeyondWatch.state() while a BEYOND watch runs in
        this engine (Fire & Ice with BEYOND configured), else None.
        Display only; never raises."""
        w = self.beyond_watch
        if w is None:
            return None
        try:
            return w.state()
        except Exception as e:                   # noqa: BLE001
            return {"link": "unknown", "age_s": None,
                    "why": f"the BEYOND watch failed: {type(e).__name__}"}

    def madmapper_link(self, playing=False):
        """madmapperwatch.MadMapperWatch.state() while a MadMapper watch
        runs in this engine (Fire & Ice with MadMapper configured), with
        "heartbeat" and "heartbeat_why" added when the show file has a
        heartbeat block (madmapperwatch.heartbeat_view), else None.
        `playing`: a show's video should be moving. Display only; never
        raises."""
        w = self.madmapper_watch
        if w is None:
            return None
        try:
            st = dict(w.state())
        except Exception as e:                   # noqa: BLE001
            st = {"link": "unknown", "age_s": None,
                  "why": f"the MadMapper watch failed: {type(e).__name__}"}
        from . import madmapperwatch as mmw
        hb = mmw.heartbeat_view(self.madmapper_heartbeat,
                                self.madmapper_heartbeat_cfg, playing,
                                getattr(self.control, "session", None))
        if hb is not None:
            st["heartbeat"] = hb["heartbeat"]
            st["heartbeat_why"] = hb["why"]
            # What MadMapper's heartbeat track must end at, and what it
            # ends at now once measured (Jeff, 2026-10-28, item 14).
            st["heartbeat_track"] = hb.get("track")
            st["heartbeat_track_wrong"] = hb.get("track_wrong") or False
        return st

    @staticmethod
    def _video_moving(cs, snap):
        """A show is playing and the conductor's look is PLAYING: only then
        is MadMapper's heartbeat expected (silence on Hold, between shows
        and at intermission is normal: madmapper.py, bench B3)."""
        return bool((cs or {}).get("running")) and \
            (snap or {}).get("look") == "PLAYING"

    def madmapper_link_changed(self, link, why):
        """The MadMapper watch's word changed (its own thread): journaled,
        and a lost link is an alert on every screen, once per loss, only
        when MadMapper was seen running and then was not, exactly as
        BEYOND's (beyond_link_changed). Not running from the start is the
        lamp and a journal line, no alert."""
        if link == "up":
            self._madmapper_was_up = True
        if link == "lost" and not getattr(self, "_madmapper_was_up", False):
            self._journal("system", "engine", "video", "link lost",
                          f"MadMapper: {why}. The Video lamp reads "
                          f"MadMapper NOT RUNNING until it is running.",
                          fault=True)
        elif link == "lost":
            self._madmapper_was_up = False
            text = (f"MadMapper was running and is not any more ({why}). "
                    f"Nothing sent to it lands, so the Video lamp reads "
                    f"MadMapper NOT RUNNING until it is back. Start "
                    f"MadMapper and its project again.")
            try:
                self.alerts.raise_("video", text)
            except Exception:                    # noqa: BLE001
                self._journal("system", "engine", "video", "link lost",
                              text, fault=True)
        elif link == "up" and \
                getattr(self, "_madmapper_said", None) == "lost":
            self._journal("system", "engine", "video", "link back",
                          "MadMapper is running again on this PC.")
        self._madmapper_said = link

    def _video_lamp(self, add, out, cond, snap, cs):
        """The Video lamp (Jeff, 2026-10-26): whether MadMapper is running,
        before any show plays, from the MadMapper watch; what was last sent
        to it once a command went through. A link check, never a picture
        check. Display only."""
        applied = (snap or {}).get("applied") or {}
        v = applied.get("video")
        mm = self.madmapper_link(self._video_moving(cs, snap))
        try:
            sent = int(getattr(cond, "_ver", {}).get("video") or 0) > 0
        except Exception:                        # noqa: BLE001
            sent = False
        last = "" if v is None or str(v) == "unknown" else str(v).lower()
        detail = f"Last sent to MadMapper: {last}. " if last else (
            "Nothing has been sent to MadMapper yet. " if not sent else
            "The last command to MadMapper is not confirmed yet. ")
        why = ""
        if cond is None or v is None:
            add("Video", "off", "none")
            return
        if mm is not None and mm["link"] == "lost":
            add("Video", "bad", "MadMapper NOT RUNNING")
        elif mm is not None and mm["link"] == "up":
            hb = mm.get("heartbeat")
            if hb == "silent":
                add("Video", "warn", "MadMapper running, no heartbeat")
            elif mm.get("heartbeat_track_wrong"):
                add("Video", "warn", f"MadMapper running, "
                    f"{mm['heartbeat_track_wrong']}")
            elif not last and sent:
                add("Video", "warn",
                    "MadMapper running, last command not confirmed")
            else:
                word = "MadMapper running"
                if hb == "ok":
                    word += ", heartbeat OK"
                if last:
                    word += f", {last}"
                add("Video", "ok", word)
            if mm.get("heartbeat_why"):
                why = mm["heartbeat_why"][0].upper() + \
                    mm["heartbeat_why"][1:] + ". "
            if mm.get("heartbeat_track"):
                why += mm["heartbeat_track"][0].upper() + \
                    mm["heartbeat_track"][1:] + ". "
        elif last:
            add("Video", "ok", last)
        elif sent:
            add("Video", "warn", "last command not confirmed")
        else:
            add("Video", "off", "not sent yet")
        link_why = ""
        if mm is not None:
            w = str(mm.get("why") or "")
            link_why = (w[:1].upper() + w[1:] + ". ") if w else ""
        up = mm is not None and mm["link"] == "up"
        out[-1]["title"] = link_why + why + detail + (
            MADMAPPER_LAMP_TITLE if up else MADMAPPER_LINK_TITLE)

    def _lasers_ver(self, cond):
        """The conductor's lasers record count (it moves on every laser
        command it records), or None. Display only."""
        try:
            return getattr(cond, "_ver", {}).get("lasers")
        except Exception:                        # noqa: BLE001
            return None

    def _lit_since_loss(self, cond):
        """None when BEYOND has not been lost; False while the conductor
        has recorded no laser command since; True once it has."""
        at = getattr(self, "_beyond_lost_ver", None)
        if at is None:
            return None
        if at[0] == "pending":
            return False                # lost, and not back up yet
        now = self._lasers_ver(cond)
        if now is None or now == at[0]:
            return False
        self._beyond_lost_ver = None
        return True

    def beyond_link_changed(self, link, why):
        """The BEYOND watch's word changed (its own thread): journaled,
        and a lost link is an alert on every screen, once per loss: only
        when BEYOND was seen running and then was not (it exited or
        crashed). Not running from the start is the lamp and a journal
        line, no alert: BEYOND is often started after this engine."""
        if link == "up":
            self._beyond_was_up = True
            at = getattr(self, "_beyond_lost_ver", None)
            if at is not None and at[0] == "pending":
                # Counted from when BEYOND is back (second review of #97,
                # P2-b): a laser command sent while it was gone reached
                # nobody, so only one recorded after this counts.
                cond = getattr(self.schedule, "conductor", None)
                self._beyond_lost_ver = (self._lasers_ver(cond),)
        if link == "lost":
            self._beyond_lost_ver = ("pending",)
        if link == "lost" and not getattr(self, "_beyond_was_up", False):
            self._journal("system", "engine", "lasers", "link lost",
                          f"BEYOND: {why}. The Lasers lamp reads LINK LOST "
                          f"until it is running.", fault=True)
        elif link == "lost":
            # Seen running since the last loss, whatever came between
            # (up, unknown, lost is a loss too: review of PR #97, P3).
            self._beyond_was_up = False
            text = (f"BEYOND was running and is not any more ({why}). "
                    f"Nothing sent to it lands, so the Lasers lamp reads "
                    f"LINK LOST until it is back. Start BEYOND and its show again; the lasers are "
                    f"not lit by this program meanwhile.")
            try:
                self.alerts.raise_("lasers", text)
            except Exception:                    # noqa: BLE001
                self._journal("system", "engine", "lasers", "link lost",
                              text, fault=True)
        elif link == "up" and getattr(self, "_beyond_said", None) == "lost":
            self._journal("system", "engine", "lasers", "link back",
                          "BEYOND is running again on this PC.")
        self._beyond_said = link

    def laser_fault(self):
        """beyondtc.NOT_SET_UP while the conductor's laser blanking has no
        OSC target, else "". Display only; never raises."""
        try:
            cond = getattr(self.schedule, "conductor", None)
            b = getattr(getattr(cond, "devices", None), "beyond", None)
            return str(getattr(b, "fault", "") or "")
        except Exception:
            return ""

    def disk_view(self):
        """{"state": "low" | "critical", "text"} while the supervisor says
        the disk is low (its disk-low.txt), else None. Never raises."""
        now = self.clock()
        cached, at = self._disk
        if at and now - at < DISK_READ_S:
            return cached
        view = None
        try:
            with open(self.disk_file, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            state = (lines[0].strip().lower() if lines else "")
            text = " ".join(l.strip() for l in lines[1:] if l.strip())
            if state in ("low", "critical") and text:
                view = {"state": state, "text": text}
        except OSError:
            view = None
        except Exception:                        # noqa: BLE001
            view = None
        self._disk = (view, now)
        return view

    # -- first run and setup (this machine only) -----------------------------
    def _settings_json(self, name, cache_key, every_s=PROGRAMS_READ_S):
        """A JSON object file in the settings folder, read at most every
        `every_s` seconds; {} when it is not there or not an object. Never
        raises."""
        now = self.clock()
        cache = getattr(self, "_json_cache", None)
        if cache is None:
            cache = self._json_cache = {}
        doc, at = cache.get(cache_key, ({}, 0.0))
        if at and now - at < every_s:
            return doc
        try:
            with open(os.path.join(settings_folder(), name),
                      encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                doc = {}
        except Exception:                        # noqa: BLE001
            doc = {}
        cache[cache_key] = (doc, now)
        return doc

    def programs_view(self):
        """The Windows supervisor's word on its three programs
        (programs.json: {"flamesafe": {"running": bool, "why": ""}, ...},
        written on every change), or {} off the show PC. Never raises."""
        return self._settings_json(PROGRAMS_FILE, "programs")

    def deck_view(self):
        """The Stream Deck program, as this engine sees it, in a sentence
        a person can act on: whether it has been heard (its deck-input
        reads), and if not, why not, from the supervisor's word when there
        is one (Jeff, 2026-10-17: "the Stream Deck never connected")."""
        seen = self._deck_seen
        if seen is not None:
            age = self.clock() - seen
            if age <= self.DECK_QUIET_S:
                usb = self._deck_usb_ms
                fails = self._deck_usb_fail
                if fails:
                    return {"heard": True, "age_s": round(age, 1),
                            "usb_worst_ms": usb, "usb_failed": fails,
                            "text": (f"The Stream Deck did not take {fails} "
                                     f"key image write(s) over USB in the "
                                     f"last minute, so a key may show an "
                                     f"older face until a resend goes "
                                     f"through. A press acts on the face the "
                                     f"deck last really showed. Arming, "
                                     f"disarming and Abort do not wait on "
                                     f"it.")}
                return {"heard": True, "age_s": round(age, 1),
                        "usb_worst_ms": usb,
                        "text": (f"The Stream Deck's keys are slow to "
                                 f"update: writing them over USB took "
                                 f"{usb / 1000.0:.1f} s in the last minute. "
                                 f"Arming, disarming and Abort do not wait "
                                 f"on it. A busy machine (a big file copy, "
                                 f"a USB hub) is the usual cause."
                                 if usb is not None and
                                 usb >= DECK_USB_SLOW_MS else "")}
            return {"heard": False, "age_s": round(age, 1),
                    "text": (f"The Stream Deck program was last heard "
                             f"{age:.0f} s ago; LTC Player starts it again "
                             f"within seconds if it stopped. Its log is "
                             f"deck.log in the settings folder.")}
        pr = (self.programs_view().get("deck") or {})
        why = str(pr.get("why") or "").strip()
        if why:
            text = (f"The Stream Deck program is not running: {why} So "
                    f"nothing has looked for the Stream Deck hardware yet.")
        elif pr.get("running"):
            text = ("The Stream Deck program is running but has not reached "
                    "this engine yet. If this stays, its log (deck.log in "
                    "the settings folder) says what it found.")
        else:
            text = ("The Stream Deck program has never reached this engine. "
                    "On the show PC, LTC Player starts it only when "
                    "flamesafe's config file exists (showpc.json, "
                    "flamesafe_config) and show_mode is fire_ice; its "
                    "supervisor.log says which. The Stream Deck hardware "
                    "itself is looked for by that program, not by this one.")
        return {"heard": False, "age_s": None, "text": text}

    def _show_folder(self):
        return str(getattr(self.control, "folder", "") or "")

    @staticmethod
    def _renders_in(folder):
        """How many .fseq renders are in `folder` (0 when it cannot be
        read): the same list Make a show file uses."""
        from . import timeline as timeline_mod
        return len(timeline_mod.fseq_names(folder))

    def _show_files(self):
        """The show files the Programming screen's Run picker would offer."""
        try:
            tls = self.control.timelines()
        except Exception:                        # noqa: BLE001
            return 0
        return len([t for t in tls if not t.get("error")])

    def _show_file_errors(self):
        """["file: why"] for the show files in the folder that do not load
        (at most 3), for the no-show-file line (review of #113, P2-b)."""
        try:
            tls = self.control.timelines()
        except Exception:                        # noqa: BLE001
            return []
        return [f"{t.get('file') or t.get('name')}: {t.get('error')}"
                for t in tls if t.get("error")][:3]

    def _schedule_path(self):
        """The scheduler's own file, or where one would go (the same name
        schedule_service.RULE_FILE has, spelled here so a GPL engine, which
        never loads the scheduler, does not load it for this)."""
        svc = self.schedule
        p = getattr(svc, "path", None)
        if p:
            return str(p)
        return os.path.join(settings_folder(), "ltcplay_schedule.json")

    def _show_live(self):
        return self._show_live_state() in self.LIVE_STATES

    def _show_live_state(self):
        try:
            return self.schedule.state_view().get("state")
        except Exception:                        # noqa: BLE001
            return None

    def setup_view(self):
        """What a fresh install has and lacks, for the getting-started card
        and the Programming screen: the show folder and its show files,
        the schedule file and whether the scheduler loaded it, flamesafe
        and the Stream Deck as this engine sees them, and which files the
        supervisor wrote as examples at first run. Display only."""
        folder = self._show_folder()
        files = self._show_files()
        svc = self.schedule
        sched_path = self._schedule_path()
        sched_ok = svc is not None and getattr(svc, "rule", None) is not None
        first = self._settings_json(FIRST_RUN_FILE, "first_run", 30.0)
        written = first.get("written") or {}
        try:
            fl = self.flame_status.view() if self.flame_status else None
        except Exception:                        # noqa: BLE001
            fl = None
        fs_connected = bool(fl and fl.get("connected") and not fl.get("stale"))
        return {"show_folder": folder,
                "show_folder_problem": self.show_folder_problem(),
                "show_folder_exists": bool(folder) and os.path.isdir(folder),
                "show_files": files,
                "show_file_errors": self._show_file_errors() if not files
                else [],
                # The renders a show file could be made from (Make a show
                # file), counted only while there is no show file.
                "show_folder_renders": self._renders_in(folder) if not files
                else None,
                "schedule_path": sched_path,
                "schedule_exists": os.path.isfile(sched_path),
                "schedule_ok": bool(sched_ok),
                "schedule_error": str(getattr(svc, "error", "") or ""),
                "schedule_attached": svc is not None,
                "example_schedule": bool(written.get("schedule")),
                "example_flamesafe": bool(written.get("flamesafe")),
                "flamesafe_config": os.environ.get(FLAMESAFE_ENV, ""),
                "flamesafe_connected": fs_connected,
                "deck_heard": self.deck_view()["heard"],
                "settings_folder": settings_folder(),
                "kiosk_closable": bool(appdata.WINDOWS),
                "ready": files > 0 and bool(sched_ok)}

    def schedule_file_view(self):
        """GET schedule-file: the file as it is (or the example's times to
        start from), its path, and the scheduler's word on it."""
        path = self._schedule_path()
        exists = os.path.isfile(path)
        doc, err = None, ""
        if exists:
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
                if not isinstance(doc, dict):
                    doc, err = None, "the file is not a schedule object"
            except (OSError, ValueError) as e:
                doc, err = None, str(e)
        if doc is None:
            doc = self.example_schedule()
        svc_err = str(getattr(self.schedule, "error", "") or "")
        return {"path": path, "exists": exists, "doc": doc,
                "error": err or svc_err}

    @staticmethod
    def example_schedule():
        """ltcplay_schedule.example.json's times, the starting point for a
        new file; a built-in copy if the example is not shipped."""
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, "ltcplay_schedule.example.json"),
                      encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"timezone": "America/Denver",
                "season": {"first_date": "2026-11-14",
                           "last_date": "2027-01-02"},
                "weekly": {"fri": {"first_start": "17:30",
                                   "interval_min": 20, "last_end": "22:00"},
                           "sat": {"first_start": "17:00",
                                   "interval_min": 20, "last_end": "22:00"}},
                "exceptions": {}, "show_len_s": 440, "guard_s": 120,
                "late_grace_s": 15}

    def save_schedule_file(self, body):
        """POST schedule-file {"doc": {...}}: checked by the scheduler's
        own parser first (every refusal is its sentence), written whole,
        then loaded by the running scheduler. Refused while a show is live:
        the night's list is not rebuilt under a running show."""
        doc = body.get("doc")
        if not isinstance(doc, dict):
            return 400, {"error": "Send the schedule as an object."}
        if self.schedule is None:
            # A GPL engine never loads the scheduler, not even to check a
            # file for it.
            return 409, {"error": "There is no scheduler in this engine, so "
                                  "there is no schedule to edit here."}
        # One save at a time, written, loaded and answered whole: two
        # screens saving at once never interleave (review of PR #180).
        with _SCHEDULE_SAVE_LOCK:
            return self._save_schedule_file(doc)

    def _save_schedule_file(self, doc):
        if self._show_live():
            return 409, {"error": "A show is live. Save the schedule between "
                                  "shows. Nothing was changed."}
        try:
            from . import schedule as sch
            rule = sch.parse_rule(doc)
        except Exception as e:                   # RuleError and kin
            problems = getattr(e, "problems", None)
            why = "; ".join(str(p) for p in problems) if problems else str(e)
            return 400, {"error": f"Not saved: {why}"}
        path = self._schedule_path()
        try:
            _write_json(path, doc)
        except OSError as e:
            return 500, {"error": f"Could not write {path}: {e}"}
        svc = self.schedule
        loaded = ""
        if svc is not None and hasattr(svc, "reload"):
            try:
                # Tonight's list is rebuilt at once, even on a night that
                # had closed, and the answer says what it now holds (Jeff,
                # Pico, 2026-10-29: "has loaded it" while nothing was
                # planned).
                svc.reload()
                err = str(getattr(svc, "error", "") or "")
                words = getattr(svc, "tonight_words", None)
                if err:
                    loaded = f" The scheduler could not load it: {err}"
                elif callable(words):
                    loaded = " " + str(words())
                else:
                    loaded = " The scheduler has loaded it."
            except Exception as e:               # noqa: BLE001
                loaded = f" The scheduler could not reload it: {e}"
        cw = self.config_watch
        if cw is not None and svc is not None and hasattr(svc, "reload"):
            # Read again just now: what is on disk is the engine's copy.
            cw.refresh(path)
        # Jeff, 2026-10-30: "Don't restrict the schedule in any way." What
        # used to refuse a save is said here, and the save stands.
        try:
            warns = list(sch.rule_warnings(rule))
        except Exception:                        # noqa: BLE001
            warns = []
        warned = (" Warnings, saved anyway: " + " ".join(warns)) \
            if warns else ""
        self._journal("", "Rack screen", "schedule file", "saved",
                      f"The schedule file {path} was saved from the "
                      f"Programming screen.{loaded}{warned}")
        return 200, {"ok": True, "text": f"Saved to {path}.{loaded}{warned}",
                     "doc": doc, "path": path, "warnings": warns}

    def open_folder(self, body):
        """POST open-folder {"which": "show" | "settings"}: the folder
        opened in the file manager of this machine."""
        which = str(body.get("which") or "")
        if which == "show":
            path = self._show_folder()
        elif which == "settings":
            path = settings_folder()
        else:
            return 400, {"error": "which has to be show or settings."}
        if not path or not os.path.isdir(path):
            return 404, {"error": f"{path or 'that folder'} does not exist "
                                  f"on this machine yet."}
        opener = getattr(self, "_opener", None) or _open_in_file_manager
        try:
            opener(path)
        except Exception as e:                   # noqa: BLE001
            return 500, {"error": f"Could not open {path}: {e}"}
        front = (" in Explorer and brought it to the front. If you do not "
                 "see it, it is on the taskbar." if os.name == "nt" else ".")
        return 200, {"ok": True, "text": f"Opened {path}{front}"}

    def set_show_folder(self, body):
        """POST show-folder {"path": ...}: the show PC's showpc.json
        show_folder, then a restart request the supervisor acts on (and
        refuses during a show). Off the show PC the folder is the engine's
        --folder, so this says how instead of pretending."""
        path = str(body.get("path") or "").strip().strip('"')
        if not path or not os.path.isabs(path):
            return 400, {"error": "Type the folder's full path, like "
                                  "C:\\Users\\show\\Documents\\LTC Shows."}
        if not os.path.isdir(path):
            return 400, {"error": f"{path} is not a folder on this machine. "
                                  f"Make it first, then try again."}
        if self._show_live():
            return 409, {"error": "A show is live. Change the show folder "
                                  "between shows. Nothing was changed."}
        showpc = os.path.join(settings_folder(), SHOWPC_FILE)
        if not os.path.isfile(showpc):
            return 409, {"error": f"On this machine the show folder is given "
                                  f"when ltcplay is started (its --folder), "
                                  f"not here. The engine is using "
                                  f"{self._show_folder()}."}
        try:
            with open(showpc, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                raise ValueError("not a settings object")
            doc["show_folder"] = path
            _write_json(showpc, doc)
            with open(os.path.join(settings_folder(), RESTART_ENGINE_FILE),
                      "w", encoding="utf-8") as fh:
                fh.write(f"show folder {time.time():.3f}\n")
        except (OSError, ValueError) as e:
            return 500, {"error": f"Could not save the show folder: {e}"}
        self._journal("", "Rack screen", "show folder", "saved",
                      f"The show folder was set to {path} from the "
                      f"Programming screen; LTC Player restarts the engine "
                      f"to use it.")
        return 200, {"ok": True, "text": f"Saved. LTC Player restarts the "
                     f"show engine now to use {path} (it refuses while a "
                     f"show is running). This page reconnects by itself."}

    # One new show file at a time: a double press makes one file, not two.
    _new_show_lock = threading.Lock()

    def _new_show_refusal(self):
        """(code, sentence) when a show file may not be made now, else
        None: a scheduled show live, or a show file running or starting."""
        if self._show_live():
            return 409, ("A scheduled show is live. Make the show file "
                         "between shows. Nothing was written.")
        c = self.control
        sess = getattr(c, "session", None)
        if getattr(c, "_starting", False) or \
                (sess is not None and getattr(sess, "running", False)):
            return 409, ("A show file is running. Press Stop first, then "
                         "make the new show file. Nothing was written.")
        return None

    def new_show_file(self, body):
        """POST show-file-new: a starter show file for the renders in the
        folder this engine serves, laid out by the same timeline.starter()
        as `ltc init` (cli.make_show_file): one cue per .fseq, back to back
        from 01:00:00:00. Unlike `ltc init` it writes into the show folder,
        not beside the launcher, and starts from the renders' rate guess
        (cli.renders_rate) rather than a fixed 30 fps. Written as a NEW file where the Show file
        chooser lists show files, never over one, and chosen there.
        Refused while a show file is running or a scheduled show is live,
        asked again just before the write and before the choice. The
        renders are only read."""
        folder = self._show_folder()
        no = self._new_show_refusal()
        if no:
            return no[0], {"error": no[1]}
        if not folder:
            return 409, {"error": "This engine serves no show folder. "
                                  "Nothing was written."}
        from .cli import INIT_START, make_show_file

        class _Refused(Exception):
            pass

        def _still_may():
            got = self._new_show_refusal()
            if got:
                raise _Refused(got)
        if not self._new_show_lock.acquire(blocking=False):
            return 409, {"error": "A show file is being made already. "
                                  "Nothing more was written."}
        try:
            try:
                made = make_show_file(folder, folder,
                                      before_write=_still_may)
            except _Refused as e:
                code, why = e.args[0]
                return code, {"error": why}
            except ValueError as e:
                return 400, {"error": str(e)}
            except OSError as e:
                return 500, {"error": f"Could not write the show file in "
                                      f"{folder}: {e}. Nothing was "
                                      f"written."}
        finally:
            self._new_show_lock.release()
        name = made["file"]
        n = made["cues"]
        text = (f"Made {name} with {n} cue{'' if n == 1 else 's'}, one for "
                f"each render (.fseq) in {made['folder']}, back to back "
                f"from {INIT_START}. The renders were not changed. "
                f"{made['rate_note']} The \"Check the show file\" button "
                f"does not hear the timecode. Read the rate in the "
                f"timecode source's own settings; once a show file is "
                f"running, the Show file card on the Programming screen "
                f"says when the "
                f"timecode coming in runs at another rate than the show "
                f"file's.")
        if made["skipped"]:
            text += (f" Left out because they could not be read: "
                     f"{', '.join(made['skipped'][:5])}"
                     f"{', ...' if len(made['skipped']) > 5 else ''}.")
        if not made["map"]:
            text += (" There is no xlights_networks.xml in the folder yet, "
                     "so Check will say there is no controller map: export "
                     "it from xLights into this folder.")
        nxt = (f"Next: open {name} in the show folder with Notepad and set "
               f"each cue's \"tc\" to the timecode that render starts at "
               f"in your show (the order is alphabetical until you change "
               f"it), and \"fps\" to the timecode's rate. Then press "
               f"Check the show file, then Run in Programming mode to "
               f"rehearse it. Nothing reaches the rig until Run.")
        late = self._new_show_refusal()
        if late:
            # Something started while the file was written: it is kept,
            # and not chosen under a running or live show.
            text += (f" It was not chosen in the Show file chooser: "
                     f"{late[1].split('. ')[0]}.")
        else:
            self._save_selected_show(name)
            self._artnet = None
            self._checks = None
        self._journal("", "Rack screen", "show file", "made",
                      f"{name} was made from the renders in "
                      f"{made['folder']} ({n} cues) on the Programming "
                      f"screen.")
        return 200, {"ok": True, "file": name, "cues": n,
                     "folder": made["folder"], "fps": made["fps"],
                     "chosen": not late, "text": text, "next": nxt}

    def screen(self, body):
        """POST screen {"action": "close"}: a request the Windows
        supervisor acts on, closing the rack screen window it opened. The
        engine, the show and the rig are not touched."""
        if str(body.get("action") or "") != "close":
            return 400, {"error": "action has to be close."}
        try:
            with open(os.path.join(settings_folder(), SCREEN_CLOSE_FILE),
                      "w", encoding="utf-8") as fh:
                fh.write(f"close {time.time():.3f}\n")
        except OSError as e:
            return 500, {"error": f"Could not ask: {e}. Press Alt+F4."}
        return 200, {"ok": True, "text": "Asked LTC Player to close this "
                     "window. The show engine keeps running. If the window "
                     "stays, press Alt+F4."}

    # -- settings changed on disk (configwatch.py, 2026-10-28) -------------
    def config_journal(self, text):
        """The watch's journal line."""
        self._journal("system", "engine", "settings files", "changed", text)

    def programs_now(self):
        """programs.json read afresh (not the status's 3 s cache), for the
        watch's own thread: a restarted program is seen within one check."""
        return self._settings_json(PROGRAMS_FILE, "programs_cfgwatch", 0.0)

    def show_busy_for_reload(self):
        """True while a show is live, or a show file runs or is starting."""
        if self._show_live():
            return True
        c = self.control
        sess = getattr(c, "session", None)
        return bool(getattr(c, "_starting", False) or
                    (sess is not None and getattr(sess, "running", False)))

    def reload_engine_refusal(self):
        """Why the rack screen's Reload engine may not be pressed now, or
        None: only on the show PC (the supervisor restarts the engine), and
        only when restart_refusal() finds nothing."""
        if not os.path.isfile(os.path.join(settings_folder(), SHOWPC_FILE)):
            return ("This engine is not run by LTC Player's supervisor. "
                    "Quit it and start it again.")
        return self.restart_refusal() or None

    def restart_refusal(self):
        """Why this engine may not be restarted now, in a sentence, or "".
        Fails closed (2026-10-28): anything not known refuses. Asked by
        Reload engine, and again by the Windows supervisor right before it
        stops the engine (GET restart-check), for every restart-engine
        request. Never raises."""
        try:
            return self._restart_refusal()
        except Exception as e:                   # noqa: BLE001
            return (f"Whether the engine may restart could not be checked "
                    f"({type(e).__name__}).")

    def _restart_refusal(self):
        if self.show_busy_for_reload():
            return "A show is running. Reload the engine between shows."
        ann = getattr(self, "announce", None)
        if ann is not None and getattr(ann, "playing", None) is not None:
            return "An announcement is playing."
        fs = self.flame_status
        if fs is None:
            return ("flamesafe's status is not connected to this engine, so "
                    "it is not known that every flame group is disarmed.")
        v = fs.view() or {}
        groups = v.get("groups") or []
        if v.get("stale") or not groups:
            return ("flamesafe's status is not seen, so it is not known "
                    "that every flame group is disarmed.")
        if any(g.get("armed") != "disarmed" or g.get("wanted")
               for g in groups):
            return "A flame group is armed. Disarm every group first."
        lf = getattr(self, "live_flame", None)
        if lf is not None and lf.on:
            return "Live flame programming is on. Turn it off first."
        fb = getattr(self, "fireball", None)
        if fb is not None:
            sm = fb.summary() or {}
            if sm.get("on") or sm.get("owner"):
                return "The Fireball page is in use. Press All off first."
        m = getattr(self, "morning", None)
        if m is not None:
            mv = m.status_view() or {}
            if mv.get("purge"):
                return "A purge is selected or firing. Clear it first."
            if mv.get("laser_tests_on"):
                return "A laser test is on. Turn it off first."
            if (mv.get("laser_arm") or {}).get("armed"):
                return "A laser is armed. Disarm the lasers first."
        return ""

    def config_changed_view(self, ctx):
        """The settings notice for the status (configwatch.ConfigWatch.
        view): {"files", "text", "engine_behind"} and, on the rack screen
        while the engine's copy is behind, "reload": {"offered", "why"}.
        Never raises."""
        cw = self.config_watch
        if cw is None:
            return {"files": [], "text": "", "engine_behind": False}
        try:
            v = cw.view()
            if v.get("engine_behind") and ctx.local:
                why = self.reload_engine_refusal()
                # flamesafe's own file: a Reload engine alone does not
                # give it to flamesafe or the deck; the page says so.
                v["reload"] = {"offered": why is None, "why": why or "",
                               "flamesafe": any(
                                   i.get("name") == "flamesafe config"
                                   for i in v.get("files") or [])}
            return v
        except Exception:                        # noqa: BLE001
            return {"files": [], "text": "", "engine_behind": False}

    def reload_engine(self, body):
        """POST reload-engine: the rack screen's Reload engine, offered
        while a settings file changed since the engine read it. A request
        the Windows supervisor acts on (restart-engine, as for a new show
        folder): the engine alone is restarted and reads every settings
        file afresh. flamesafe and the Stream Deck program are not
        touched. Refused unless reload_engine_refusal() is None; the
        supervisor refuses again while a show runs."""
        why = self.reload_engine_refusal()
        if why:
            return 409, {"error": f"Not reloaded: {why} Nothing was "
                                  f"changed."}
        try:
            with open(os.path.join(settings_folder(), RESTART_ENGINE_FILE),
                      "w", encoding="utf-8") as fh:
                fh.write(f"settings changed {time.time():.3f}\n")
        except OSError as e:
            return 500, {"error": f"Could not ask LTC Player: {e}"}
        self._journal("", "Rack screen", "reload engine", "asked",
                      "Reload engine was pressed: LTC Player restarts the "
                      "engine so it reads the changed settings files.")
        return 200, {"ok": True, "text": (
            "LTC Player restarts the engine now so it reads the settings "
            "files again. This page reconnects by itself. flamesafe and the "
            "Stream Deck program keep running with what they read at their "
            "start: to give them a changed flamesafe.json, stop LTC Player "
            "and start it again.")}

    def setup_post(self, name, body):
        if name == "schedule-file":
            return self.save_schedule_file(body)
        if name == "open-folder":
            return self.open_folder(body)
        if name == "show-folder":
            return self.set_show_folder(body)
        if name == "show-select":
            return self.select_show(body)
        if name == "show-file-new":
            return self.new_show_file(body)
        if name == "reload-engine":
            return self.reload_engine(body)
        return self.screen(body)

    def audio_interface_view(self):
        """audiowatch's view, or None without the watch. Never raises."""
        w = getattr(self, "audio_watch", None)
        if w is None:
            return None
        try:
            return w.view()
        except Exception:                        # noqa: BLE001
            return None

    def deck_chip(self):
        """The Deck chip's colour and word from live truth only
        (DECK_ARM_FRESH_MS): ("ok", "ok") only while the Stream Deck
        program was heard within PLAIN_DECK_FRESH_S, says its USB deck is
        present, and sent an arm frame within DECK_ARM_FRESH_MS. "bad"
        with plain words when the deck is unplugged ("missing"), the
        program is not heard, or the deck sends no arm frames ("not
        arming"); "warn" only while its word is late or not known.
        No alert, and no Acknowledge, is read here: a lamp never changes
        colour because someone saw a banner. Never raises."""
        seen = self._deck_seen
        if seen is None:
            # A rig with flamesafe, or a show PC whose supervisor runs the
            # deck program, expects a deck: not hearing it is a fault.
            # Anywhere else nothing is expected, and the chip is grey.
            try:
                expected = self.flame_status is not None or \
                    "deck" in (self.programs_view() or {})
            except Exception:                    # noqa: BLE001
                expected = True
            return ("bad" if expected else "off"), "not heard"
        try:
            age = self.clock() - seen
        except Exception:                        # noqa: BLE001
            return "warn", "not known"
        if age > self.DECK_QUIET_S:
            return "bad", "not heard"
        hw = self._deck_hw
        if hw is False:
            return "bad", "missing"
        if age > PLAIN_DECK_FRESH_S:
            return "warn", f"late {age:.1f} s"
        if hw is not True:
            # An older deck program that does not say: never green.
            return "warn", "USB not known"
        arm = self._deck_arm_ms
        if arm is None or arm > DECK_ARM_FRESH_MS:
            # Heard, the deck here, and yet no arm frame for longer than
            # flamesafe waits (review of #108, P2-3): its main loop is
            # frozen and the arm link silent, so flamesafe has disarmed and
            # nothing arms from the deck. A fault, said plainly.
            return "bad", "not arming"
        return "ok", "ok"

    def subsystems(self, cs):
        """The rack screen's compact strip: one word per subsystem, and
        "ok", "warn", "bad" or "off". Display only, read from what the
        engine already knows; never raises."""
        out = []

        def add(name, state, word):
            out.append({"name": name, "state": state, "word": word})
        state, word = self.deck_chip()
        usb = self._deck_usb_ms
        if state != "ok":
            # Not live, or not all there: said as it is (deck_chip).
            add("Deck", state, word)
        elif getattr(self, "_deck_layout_default", False):
            # ltcplay_deck.json could not be used: the keys are in the
            # default layout, and the deck's journal says why.
            add("Deck", "warn", "default keys: ltcplay_deck.json not used")
        elif self._deck_usb_fail:
            # Key image writes the deck did not take (review of PR #83,
            # P2-2): its keys may show an older face until a resend goes
            # through. Display only, like USB slow.
            add("Deck", "warn",
                f"USB {self._deck_usb_fail} write(s) failed")
        elif usb is None:
            add("Deck", "ok", "ok")
        elif usb >= DECK_USB_SLOW_MS:
            # The deck's keys are behind (USB writes stalling on a busy
            # machine); its arming and Abort are not (they do not wait on
            # the writes). Said here, and journaled by the deck once per
            # slow spell.
            add("Deck", "warn", f"USB slow {usb / 1000.0:.1f} s")
        else:
            add("Deck", "ok", f"ok, USB {usb} ms")
        try:
            fl = self.flame_status.view() if self.flame_status else None
        except Exception:
            fl = None
        link = self._flame_link()
        if link is not None:
            # Flame frame timing (2026-10-29), in plain words: frames to
            # flamesafe late by more than the limit in the last hour.
            try:
                tv = link.timing_view()
                n = tv.get("late_last_hour", 0)
                hour = tv.get("this_hour") or {}
                if n:
                    add("Flame timing", "warn",
                        f"{n} frame(s) late in the last hour, worst "
                        f"{tv.get('late_last_hour_worst_ms', 0):.0f} ms")
                else:
                    add("Flame timing", "ok",
                        f"on time, worst {hour.get('worst_ms', 0):.0f} ms "
                        f"this hour")
            except Exception:                    # noqa: BLE001
                pass
        if not fl or not fl.get("connected"):
            add("Flamesafe", "off", "not connected")
        elif fl.get("stale"):
            add("Flamesafe", "bad", "no status")
        elif fl.get("fault"):
            add("Flamesafe", "bad", "fault")
        else:
            add("Flamesafe", "ok", "linked")
        audio = ((cs or {}).get("clock") or {}).get("audio")
        watch = self.audio_interface_view()
        # "Show audio" is the show's own interface; the announcements have
        # their own lamp beside it, so the two are never read as one check
        # (Jeff's field report, 2026-10-28, item 10).
        if watch and watch.get("missing"):
            # Before Run: the interface the show file names is not on this
            # PC at all (audiowatch.py). The banner says which.
            add("Show audio", "bad", "not attached")
        elif not isinstance(audio, dict):
            add("Show audio", "off", "none")
        elif audio.get("fault") or not audio.get("connected"):
            add("Show audio", "bad",
                "fault" if audio.get("fault") else "lost")
        else:
            add("Show audio", "ok", "ok")
        av = self.announce_view() if self._announce is not None else None
        if av is not None:
            rt = av.get("route") or {}
            if av.get("stale") or av.get("ok") is False:
                add("Announcements", "bad", "not known" if av.get("stale")
                    else "file problem")
            elif rt.get("ok"):
                dl = [i for i in av.get("items") or []
                      if i.get("id") == "delayed"]
                if dl and not dl[0].get("available"):
                    # Advisory (Jeff, 2026-10-30): nothing plays on the PA
                    # by itself, but the operator's Delayed button cannot
                    # play, so the lamp says so. A warning, never a fault.
                    add("Announcements", "warn",
                        "Delayed recording missing" if dl[0].get("missing")
                        else "Delayed recording cannot play")
                else:
                    add("Announcements", "ok", "on show audio"
                        if rt.get("kind") == "show" else "own output")
            elif rt.get("kind") == "show":
                add("Announcements", "bad", "show audio not open")
            elif not av.get("device_set"):
                add("Announcements", "warn", "pick an output")
            elif "matches" in str(av.get("device_reason") or ""):
                add("Announcements", "warn", "pick one output")
            else:
                add("Announcements", "warn", "output not attached")
        disk = self.disk_view()
        if disk:
            add("Disk", "bad" if disk["state"] == "critical" else "warn",
                disk["state"])
        a = self.audit
        if a is not None:
            # Only while it has a fault: lines lost, or the disk refusing
            # (audit.py). The show is never held back by it.
            try:
                st = a.status()
                if st["fault"] or st["waiting_gap"]:
                    add("Audit", "warn", "not writing")
            except Exception:                    # noqa: BLE001
                add("Audit", "warn", "not writing")
        applied = {}
        snap = {}
        cond = getattr(self.schedule, "conductor", None)
        try:
            snap = cond.snapshot() or {}
            applied = snap.get("applied") or {}
        except Exception:
            applied = {}
        laser_fault = self.laser_fault()
        m = self.morning
        if m is not None and getattr(m, "laser_arm", None) is not None:
            # The laser arm (Jeff, 2026-10-22): shown on every screen.
            try:
                la = m.laser_arm.status_view()
                n, total = len(la["armed"]), la["total"]
                if n == 0:
                    add("Laser arm", "off", "disarmed")
                elif la["output_permitted"]:
                    add("Laser arm", "warn", "ARMED")
                else:
                    add("Laser arm", "warn", f"{n} of {total} armed")
            except Exception:                    # noqa: BLE001
                add("Laser arm", "bad", "not known")
        link = self.beyond_link()
        self._video_lamp(add, out, cond, snap, cs)
        mo = getattr(self, "mm_output", None)
        if mo is not None:
            # MadMapper's video output (showwindows.py, Jeff's field
            # report 2026-10-29): its full-screen window on the projector
            # display, read from Windows. Display only.
            try:
                st, why = mo.state()
            except Exception as e:               # noqa: BLE001
                st, why = "unknown", f"not known: {type(e).__name__}"
            add("MadMapper output", {"on": "ok", "off": "bad"}.get(
                st, "warn"), {"on": "on", "off": "OFF"}.get(st, "not known"))
            out[-1]["title"] = why
        for name, key in (("Lasers", "lasers"),):
            v = applied.get(key)
            if key == "lasers" and laser_fault:
                # Never "ok" or "lit" while nothing can blank them.
                add(name, "bad", "blanking not set up")
            elif key == "lasers" and link is not None and \
                    link["link"] == "lost":
                # BEYOND is not running (2026-10-23: the lamp said lit for
                # hours after it exited): whatever was sent last, nothing
                # lands.
                add(name, "bad", "LINK LOST")
            elif key == "lasers" and cond is not None and \
                    str(v) == "lit":
                # BEYOND never answers: "lit" is only what was sent. With
                # its process seen running it is sent and unconfirmed;
                # with no word on the link at all it is a warning. After
                # BEYOND was lost, the new process has had no unblank
                # until the conductor sends lit again (review of PR #97,
                # P2-2): the conductor's lasers record must have moved.
                if link is not None and link["link"] == "up" and \
                        self._lit_since_loss(cond) is False:
                    add(name, "warn", "BEYOND restarted: dark until lit "
                                      "again")
                elif link is not None and link["link"] == "up":
                    add(name, "ok", "lit, unconfirmed")
                else:
                    add(name, "warn", "lit? link unknown")
                out[-1]["title"] = BEYOND_LAMP_TITLE
            elif cond is None or v is None:
                add(name, "off", "none")
            elif str(v) == "unknown":
                add(name, "warn", "not known")
            else:
                add(name, "ok", str(v).lower())
        since = (cs or {}).get("since_ok")
        if not (cs or {}).get("running"):
            add("Pixels", "off", "stopped")
        elif not isinstance(since, (int, float)) or isinstance(since, bool):
            # No good send yet, or no word on it: never "sending".
            add("Pixels", "warn", "not known")
        elif since > 2.0:
            # Nothing has left this machine for 2 s, counted errors or
            # not (2026-10-26: a sender that stops without an error read
            # "sending"); display.py's own rule for the main screen.
            add("Pixels", "bad", "not sending")
        else:
            add("Pixels", "ok", "sending")
        return out

    def version_line(self):
        """Which LTC Player this is, for the rack screen's header: the
        release it was stamped with and its build id, or that it has been
        edited since (version.status()). Worked out once: the build id
        hashes every file the program ships, which is cheap once and not on
        every poll. Never raises."""
        if self._version is None:
            try:
                self._version = version_mod.status()
            except Exception as e:                  # noqa: BLE001
                self._version = f"version unknown ({type(e).__name__})"
        return self._version

    def _rearm_words(self, fl):
        """Each flame group's `rearm_ms` (ms left in the Stream Deck's
        re-arm refractory window, as of this status) and `purge_refused`
        (the deck refused a purge arm for it: "refractory", or ""), from
        the deck's last read while it is fresh. Display only (Jeff,
        2026-10-26: a quick re-arm after a Disarm sat on "arming" with no
        word why)."""
        left, pref, at = self._deck_rearm
        age = None if at is None else self.clock() - at
        fresh = age is not None and 0 <= age <= DECK_REARM_FRESH_S
        nokey = getattr(self, "_deck_nokey", set())
        for i, g in enumerate(fl.get("groups") or []):
            ms = left.get(i, 0) - int(age * 1000) if fresh else 0
            g["rearm_ms"] = max(0, ms)
            g["purge_refused"] = pref.get(i, "") if fresh else ""
            # Whether the Stream Deck has a key for it (review of #126,
            # P1): False only on the deck's fresh word that it has none.
            g["deck_key"] = not (fresh and i in nokey)

    def users_file_check(self):
        """A users file that is broken, or missing after users were set up
        here, locks every control but the stops and disarms (users.py,
        fail closed; review of PR #143, 2). Said loudly, once for each
        reason: an alert on every screen and the deck, and the journal.
        Asked on every status and deck read. Never raises."""
        u = getattr(self, "users", None)
        if u is None:
            return
        try:
            why = u.broken()
        except Exception:                        # noqa: BLE001
            return
        said = getattr(self, "_users_alert_said", None)
        if not why:
            if said:
                # A good file is back: used at once, and the alert goes.
                self._users_alert_said = None
                try:
                    self.alerts.clear(kind="users", action="Confirm",
                                      who="system", screen="engine")
                except Exception:                # noqa: BLE001
                    pass
            return
        if why == said:
            return
        self._users_alert_said = why
        from . import users as users_mod
        locked = ("Every control is locked except Stop, Hold, Abort, "
                  "Acknowledge and every disarm, and nobody can sign in.")
        if getattr(u, "error_kind", "") == "open":
            # There, but held by something: nothing to delete. It is tried
            # again on every read (third review of PR #143, 4).
            text = (f"The users file cannot be opened just now: {why}. "
                    f"{locked} It is tried again all the time and used the "
                    f"moment it opens, with no restart. Another program may "
                    f"be holding it, such as an antivirus scan: if this "
                    f"lasts, check that program, and that {u.folder} can be "
                    f"read.")
        else:
            text = (f"The users file cannot be used: {why}. {locked} If a "
                    f"good {users_mod.USERS_FILE} is put back in "
                    f"{u.folder}, it is used at once, with no restart. To "
                    f"start over with no users instead: close LTC Player, "
                    f"remove {users_mod.USERS_FILE} and "
                    f"{users_mod.USERS_MARK} from that folder, and start "
                    f"LTC Player again; then each person chooses a password "
                    f"at the rack screen.")
        try:
            self.alerts.raise_("users", text)
        except Exception:                        # noqa: BLE001
            self._journal("system", "engine", "users", "fault", text,
                          fault=True)

    def status(self, ctx):
        self.users_file_check()
        out = {"served_at": int(self.wall() * 1000), "fresh_s": FRESH_S,
               "me": self.whoami(ctx), "version": self.version_line()}
        svc = self.schedule
        if svc is None:
            out["schedule"] = {"attached": False}
        else:
            try:
                st = svc.state_view(journal=8)
                tn = svc.tonight_view()
                cv = st.get("conductor") or {}
                state = st.get("state")
                out["schedule"] = {
                    "attached": True, "ok": st.get("ok"),
                    "error": st.get("error"), "state": state,
                    "running": st.get("running"),
                    "held": state in ("HOLD", "PAUSED"),
                    "aborted": bool(cv.get("aborted")),
                    "conductor": cv.get("attached", False),
                    "trouble": cv.get("trouble"),
                    "next": st.get("next"), "delayed": st.get("delayed"),
                    "dry_run": st.get("dry_run"),
                    "start_blocked": st.get("start_blocked"),
                    "run_warning": st.get("run_warning"),
                    "run_warning_urgent": st.get("run_warning_urgent"),
                    "slots": tn.get("slots", [])}
            except Exception as e:
                out["schedule"] = {"attached": True, "ok": False,
                                   "error": f"{type(e).__name__}: {e}"}
        try:
            cs = self.control.state()
            out["show"] = {"running": bool(cs.get("running")),
                           # Run on its way in (web.py _build_state): the
                           # page shows Stop then too (review of #144, F4).
                           "starting": bool(cs.get("starting")),
                           "show": cs.get("show"),
                           "timeline": cs.get("timeline"),
                           "timecode": cs.get("playing") or cs.get("ltc_in"),
                           "state": cs.get("state"),
                           "rate_note": rate_mismatch(cs)}
            cue_now = cs.get("now")
            out["subsystems"] = self.subsystems(cs)
        except Exception as e:
            out["show"] = {"running": False,
                           "error": f"{type(e).__name__}: {e}"}
            cue_now = None
            out["subsystems"] = self.subsystems({})
        out["countdown"] = countdown_view(out["schedule"], out["show"],
                                          cue_now)
        out["audio_interface"] = self.audio_interface_view()
        out["disk"] = self.disk_view()
        out["deck"] = self.deck_view()
        out["programs"] = self.programs_view()
        if ctx.local:
            # The getting-started card: the rack screen's, not the iPad's.
            out["setup"] = self.setup_view()
        try:
            out["transport"] = self._transport_view()
        except Exception as e:
            out["transport"] = {"programming": False,
                                "why": f"{type(e).__name__}: {e}"}
        if self.flame_status is None:
            out["flames"] = {"connected": False, "stale": True, "groups": [],
                             "why": "flamesafe's status is not connected to "
                                    "this engine, so the flame lamps cannot "
                                    "be shown here. Look at the Stream "
                                    "Deck."}
        else:
            out["flames"] = self.flame_status.view()
        out["flames"]["aim"] = self.aim_view(out["flames"])
        self._rearm_words(out["flames"])
        # Review of PR #43, P1-3: the flame cues refused for a reason that
        # is a fault (the render and the layout do not fit, and the like).
        out["flames"]["cues_fault"] = self.cues_fault()
        out["laser_fault"] = self.laser_fault()
        out["live_flame"] = self.live_flame_view()
        out["programming_flames"] = self.programming_flames_view()
        hzv = self.hazers_view(out["flames"])
        if hzv is not None:
            out["hazers"] = hzv
        pmv = self.pump_view(out["flames"])
        if pmv is not None:
            out["pump"] = pmv
        out["beyond_link"] = self.beyond_link()
        try:
            _snap = self.schedule.conductor.snapshot() or {}
        except Exception:                        # noqa: BLE001
            _snap = {}
        out["madmapper_link"] = self.madmapper_link(
            self._video_moving(out["show"], _snap))
        if ctx.local:
            out["selected_show"] = self._selected_show()
        out["show_folder"] = {"served": self._show_folder(),
                              "problem": self.show_folder_problem()}
        out["disarm_connected"] = self._disarm_fn() is not None
        now = self.clock()
        tok = ctx.device
        with self._arm_lock:
            holds = [{"group": i, "who": h["who"], "device": h["device"],
                      "held_s": round(h["beat"] - h["start"], 2),
                      "mine": h["token"] == tok}
                     for i, h in self._holds.items()
                     if now - h["beat"] <= BEAT_STALE_S]
        out["arming"] = {"enabled": self.arming_enabled(),
                         "needs_s": SCREEN_HOLD_S, "fresh_s": ARM_FRESH_S,
                         "holds": holds}
        self.artnet_alert()
        out["artnet_note"] = self.artnet_note()
        out["config_changed"] = self.config_changed_view(ctx)
        self._apply_pulse()
        out["alerts"] = self.alerts.view()
        booth = getattr(self.alerts.booth, "device_name", "") or ""
        out["booth"] = {"configured": bool(booth), "device": booth,
                        "notes": self._booth_notes()}
        try:
            out["alert_hold"] = self.alert_hold.view()
        except Exception as e:                   # noqa: BLE001
            out["alert_hold"] = {"held": False,
                                 "error": f"{type(e).__name__}: {e}"}
        out["preroll"] = self.preroll_view()
        if ctx.local:
            # Operator announcements: the rack screen's block (announce.py).
            out["announce"] = self.announce_view()
        # The lock banner and the purge, on every screen (morning.py).
        m = self.morning
        if m is not None:
            try:
                out["morning"] = m.status_view()
            except Exception as e:                   # noqa: BLE001
                out["morning"] = {"error": f"{type(e).__name__}: {e}"}
            out["morning"]["purge_enabled"] = self.purge_enabled(ctx.local)
            out["morning"]["group_machines"] = self.group_machines(
                m, out["morning"])
            out["laser_arm"] = self.rack_laser_view(m, ctx)
        if self.fuel is not None:
            # The Fuel nav button's colour (fuel.py). Display only.
            try:
                out["fuel"] = self.fuel.summary()
            except Exception as e:                   # noqa: BLE001
                out["fuel"] = {"fault": f"{type(e).__name__}: {e}"}
        if self.cleaning is not None:
            # Wave flamer cleaning: every screen's banner and Stop.
            try:
                out["cleaning"] = self.cleaning.summary()
            except Exception as e:                   # noqa: BLE001
                out["cleaning"] = {"available": False, "state": "unknown",
                                   "text": f"{type(e).__name__}: {e}"}
        if self.fireball is not None:
            # The Fireball nav button, and whether a channel is on.
            try:
                out["fireball"] = self.fireball.summary()
            except Exception as e:                   # noqa: BLE001
                out["fireball"] = {"available": False,
                                   "error": f"{type(e).__name__}: {e}"}
        return out

    @staticmethod
    def group_machines(m, sv=None):
        """Each flame group's machines from the Startup page's machine list,
        for the show screen's "armed n of m machines" line (Jeff,
        2026-10-26): {group: {"machines": n, "locked": k, "lock": how}},
        k of them in the lock table; how is "ok" once flamesafe holds
        exactly that table (sv's flamesafe_confirmed), "pending" before,
        "untrusted" under a lock error (second review of #122, P2-3).
        Display only; None when the config could not be read or the list
        cannot be read (the page then shows what it showed before). Never
        raises."""
        try:
            if getattr(m, "config_error", ""):
                return None
            sv = sv or {}
            how = ("untrusted" if sv.get("lock_error") else
                   "ok" if sv.get("flamesafe_confirmed") is True else
                   "pending")
            locked = m.lockout.machines
            out = {}
            for x in m.cfg.machines:
                g = out.setdefault(str(x.group), {"machines": 0, "locked": 0,
                                                  "lock": how})
                g["machines"] += 1
                if x.id in locked:
                    g["locked"] += 1
            return out
        except Exception:                        # noqa: BLE001
            return None

    def rack_laser_view(self, m, ctx):
        """The show screen's LASERS strip (Jeff, 2026-10-22): every laser's
        arm state, the groups, and whether an arm from this screen would
        be refused before anyone confirms it, in words. Display only: the
        laser-arm route decides every press. Never raises."""
        try:
            v = m.laser_arm.view()
        except Exception as e:                   # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        v.pop("per_laser_osc", None)
        v["arm_here"] = "" if self.may_act_from(ctx.local) else (
            "arming a laser from a device on the show network needs "
            "screen_arming (on the show machine itself: Programming, "
            "Arming from a screen, Turn on)")
        return v
