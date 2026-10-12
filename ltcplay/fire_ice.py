"""Fire & Ice: the one place `ltc serve` builds a real show conductor.

Imported ONLY when `ltc serve` is given a schedule (`--schedule`), which is
how this codebase already tells Fire & Ice from GPL: the GPL launchers never
pass it, so on the Mac this module, conductor.py, devices.py, madmapper.py
and beyond.py are never loaded (test_ltc_serve_gpl_builds_no_conductor).

What it builds, from conductor.py's own integration notes:

  ShowOutputs   FireIceShow, below: ltcplay's own side.
    music       the running session's clock.AudioMaster: pause(fade_ms)
                for Hold, resume(fade_ms) for Resume, halt(fade_ms) for
                Abort, each with the conductor's own fade (rehearsal: 0).
    frozen      from the clock's real on_pause/on_resume signal, chained
                after the session's own, never from the request
                (AudioMaster.paused is already True during the fade).
    pixels      the existing pixel output path: the player's override, the
                same "blackout" the page's Blackout button sets. It has no
                fade, so "fade to black" is black at once, journaled as such.
    flames      ltcplay's flame link (flamelink.FlameLink, PR #34), built
                here when this config names "flamesafe_config": its key,
                port, universe and frame_stale_ms come from flamesafe's own
                config file (FlameLinkConfig.from_flamesafe_config), so the
                two programs cannot disagree. zero, release and the
                Abort's disarm_all go through it. The cue values are the
                show's own flame universe, read from the frame the pixel
                output is rendering (FlameCues), and only when this config
                names the flame controller in xlights_networks.xml AND that
                controller is Inactive there, so the pixel output never
                sends fire values to the flame node itself, around
                flamesafe. Anything else: all zeros. Without
                "flamesafe_config" there is no flame link: zero and release
                reach nothing, and a screen Abort's disarm FAILS LOUDLY.
  DeviceOutputs conductor.ConductorDevices on web.serve's own MadMapper and
                BEYOND links, built from this config's "madmapper" and
                "beyond" blocks; either may be absent, and the conductor then
                says "not connected" at start.
  laser gate    conductor.laser_gate_for(the scheduler's state).
  hold_gate     schedule_service.Service.hold_for_announcement.
  announcer     announce.AnnounceService.play, when --announce is given.

The config file is ltcplay_fire_ice.json beside the schedule rule file. It
is optional: without it the conductor is built with no lasers or video and
the scheduler stays a dry run. Its one switch that changes what the
scheduler does is "scheduler_performs" (see ShowRunner and BENCH.md):
false, the default, is today's dry run exactly.
"""
import atexit
import json
import os
import threading

from . import conductor as C
from . import showlog as showlog_mod

CONFIG_FILE = "ltcplay_fire_ice.json"
KEYS = frozenset(("scheduler_performs", "auto_start", "show_cue",
                  "madmapper", "beyond", "flamesafe_config",
                  "flame_controller", "notes", "show_name", "venue",
                  "beyond_blank", "beyond_black_hour", "beyond_timecode_ip"))

# "auto_start": the ONE setting that decides whether the scheduler, once it
# performs, starts a scheduled show by itself (an open question for Jeff,
# PR #32 Q5: is "Run pressed" enough to start a show, and light the lasers
# through the conductor, with no other confirmation?).
#   "when_run_pressed"  PR #32's reading, the default: a show that comes due
#                       is started on the show audio once Run has been
#                       pressed on the page.
#   "off"               the scheduler never starts a show by itself: a show
#                       that comes due is refused by the runner and reported
#                       as a failed start (which disarms every flame group);
#                       an operator's Start now still starts one.
# Every start, automatic or Start now, is journaled either way.
AUTO_START = ("when_run_pressed", "off")


class FireIceConfigError(ValueError):
    """The Fire & Ice config is wrong, in a sentence."""


def config_path_for(schedule_path):
    """ltcplay_fire_ice.json in the same folder as the schedule rule file."""
    return os.path.join(os.path.dirname(os.path.abspath(schedule_path)),
                        CONFIG_FILE)


class FireIceConfig:
    """The Fire & Ice settings. Defaults are today's behavior: a dry-run
    scheduler, no MadMapper, no BEYOND."""

    def __init__(self, scheduler_performs=False, show_cue=None,
                 madmapper=None, beyond=None, path=None,
                 auto_start="when_run_pressed", flamesafe_config=None,
                 flame_controller=None, show_name=None, venue=None,
                 beyond_blank="timecode", beyond_black_hour=23,
                 beyond_timecode_ip=None):
        # How the lasers are kept dark (beyondtc.py, Jeff 2026-10-04):
        # "timecode" (BEYOND's own timecode to the black zone, the default),
        # "osc" (beyond.py's brightness 0/100) or "both".
        self.beyond_blank = beyond_blank
        self.beyond_black_hour = beyond_black_hour
        self.beyond_timecode_ip = beyond_timecode_ip
        # The show's own name and where it plays, for the screens' title
        # (/api/brand). Here, not in the shared ltcplay_brand.json, so the
        # GPL build keeps its own name; Jeff Holmes Presents stays global.
        self.show_name = show_name
        self.venue = venue
        self.scheduler_performs = scheduler_performs
        self.auto_start = auto_start
        self.show_cue = show_cue
        self.madmapper = madmapper
        self.beyond = beyond
        self.path = path
        self.flamesafe_config = flamesafe_config
        self.flame_controller = flame_controller

    @classmethod
    def parse(cls, doc, where=CONFIG_FILE):
        if not isinstance(doc, dict):
            raise FireIceConfigError(f"{where}: it has to be one JSON "
                                     f"object.")
        unknown = sorted(k for k in doc if k not in KEYS)
        if unknown:
            raise FireIceConfigError(
                f"{where}: {', '.join(repr(k) for k in unknown)} is not a "
                f"setting this file has. It takes: "
                f"{', '.join(sorted(KEYS))}.")
        performs = doc.get("scheduler_performs", False)
        if performs is not True and performs is not False:
            # Only the JSON words true and false. "yes", 1 or "true" are
            # refused: a switch that changes what the scheduler does on show
            # night is never guessed from something that looks like one.
            raise FireIceConfigError(
                f"{where}: 'scheduler_performs' has to be true or false, "
                f"not {performs!r}.")
        cue = doc.get("show_cue")
        if cue is not None and (not isinstance(cue, str) or not cue.strip()):
            raise FireIceConfigError(
                f"{where}: 'show_cue' is the name of the show's cue in the "
                f"show file, or leave it out for the first cue.")
        auto = doc.get("auto_start", "when_run_pressed")
        if auto not in AUTO_START:
            raise FireIceConfigError(
                f"{where}: 'auto_start' has to be one of "
                f"{', '.join(repr(a) for a in AUTO_START)}, not {auto!r}.")
        fs = doc.get("flamesafe_config")
        if fs is not None:
            if not isinstance(fs, str) or not fs.strip():
                raise FireIceConfigError(
                    f"{where}: 'flamesafe_config' is the path of flamesafe's "
                    f"own config file, or leave it out for no flame link.")
            fs = fs.strip()
            if not os.path.isabs(fs):
                base = os.path.dirname(os.path.abspath(where)) \
                    if where != CONFIG_FILE else os.getcwd()
                fs = os.path.join(base, fs)
        fc = doc.get("flame_controller")
        if fc is not None:
            if not isinstance(fc, str) or not fc.strip():
                raise FireIceConfigError(
                    f"{where}: 'flame_controller' is the exact name of the "
                    f"flame controller in xlights_networks.xml.")
            if fs is None:
                raise FireIceConfigError(
                    f"{where}: 'flame_controller' needs 'flamesafe_config': "
                    f"flame cues only ever go to flamesafe.")
            fc = fc.strip()
        titles = {}
        for k in ("show_name", "venue"):
            v = doc.get(k)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                raise FireIceConfigError(
                    f"{where}: {k!r} is words for the screens' title, or "
                    f"leave it out.")
            titles[k] = v.strip() if v else None
        blank = doc.get("beyond_blank", "timecode")
        if blank not in ("timecode", "osc", "both"):
            raise FireIceConfigError(
                f"{where}: 'beyond_blank' is how the lasers are kept dark: "
                f"\"timecode\" (the black zone, the default), \"osc\" or "
                f"\"both\", not {blank!r}.")
        hour = doc.get("beyond_black_hour", 23)
        if isinstance(hour, bool) or not isinstance(hour, int) or \
                not 0 <= hour <= 23:
            raise FireIceConfigError(
                f"{where}: 'beyond_black_hour' is the hour of BEYOND's black "
                f"zone, a whole number from 0 to 23, not {hour!r}.")
        tip = doc.get("beyond_timecode_ip")
        if tip is not None:
            import ipaddress
            try:
                ipaddress.IPv4Address(str(tip))
            except ValueError:
                raise FireIceConfigError(
                    f"{where}: 'beyond_timecode_ip' is BEYOND's address for "
                    f"its timecode, like 127.0.0.2, not {tip!r}.")
        titles.update(beyond_blank=blank, beyond_black_hour=hour,
                      beyond_timecode_ip=tip)
        mm = bey = None
        if "madmapper" in doc:
            from . import madmapper as madmapper_mod
            mm = madmapper_mod.MadMapperConfig.parse(doc["madmapper"], where)
        if "beyond" in doc:
            from . import beyond as beyond_mod
            bey = beyond_mod.BeyondConfig.parse(doc["beyond"], where)
        return cls(performs, cue.strip() if cue else None, mm, bey, where,
                   auto_start=auto, flamesafe_config=fs,
                   flame_controller=fc, **titles)

    @classmethod
    def load(cls, path):
        """The file, or the defaults when there is none. A file that is
        there but wrong raises: serve refuses to start rather than run
        Fire & Ice on settings it could not read."""
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise FireIceConfigError(f"{path}: could not be read: {e}")
        return cls.parse(doc, path)

    def summary(self):
        return ("scheduler performs" + (
                    ", starts shows by itself once Run is pressed"
                    if self.auto_start == "when_run_pressed" else
                    ", never starts a show by itself (auto_start off)")
                if self.scheduler_performs else "scheduler dry run") + \
            (f", flame link from {self.flamesafe_config}"
             + (f", flame cues from controller {self.flame_controller!r}"
                if self.flame_controller else ", flame cues all zero")
             if self.flamesafe_config else ", no flame link") + \
            (f", MadMapper {self.madmapper.summary()}" if self.madmapper
             else ", no MadMapper") + \
            (", BEYOND " + self.beyond.summary() if self.beyond
             else ", no BEYOND")


# ----------------------------------------------------------- ShowOutputs --

NO_FLAME_LINK = (
    "ltcplay has no flame link to flamesafe in this build: it sends no flame "
    "cue frames at all, so the conductor's flame cue commands reach nothing.")
DISARM_SENT = (
    "a disarm was sent to every flame group through the flame link. Sent "
    "is not confirmed: only flamesafe's status frame can say it took it. "
    "The journal says when flamesafe confirms it, or that it has not after "
    "1 s, and the Stream Deck shows whether each group disarmed.")
NO_DISARM = (
    "A screen-initiated Abort cannot disarm the flame groups: flamesafe's "
    "link contract (flamesafe/CONTRACT.md, version 2) has no disarm message "
    "yet. Flame cues from ltcplay are zero. The Stream Deck's own Abort "
    "disarms through its own link; until the contract has a disarm message, "
    "disarm with the Stream Deck or the arm keys.")


class FireIceShow(C.ShowOutputs):
    """conductor.ShowOutputs for Fire & Ice. `control` is web.Control: the
    running session, its clock and its player are looked up on every call,
    so a Stop and a new Run are followed without rebuilding anything.

    Same rules as every output: returns a conductor Result, never raises,
    returns at once (AudioMaster's pause, resume and halt only send a
    message to the audio process)."""

    def __init__(self, control, journal=None, flame_link=None):
        self.control = control
        self._journal = journal
        self.flame_link = flame_link
        self.flames = C.ZERO        # what the conductor last asked for
        self._lock = threading.Lock()
        self._hooked = None         # the clock whose callbacks are chained
        self._frozen = None         # True/False once hooked, from the clock
        self._pix_prev = None       # the override before ours, if ours
        self._pix_ours = False
        if flame_link is None:
            self._note(NO_FLAME_LINK, fault=True, action="flames",
                       outcome="not_configured")

    def _note(self, text, **fields):
        if self._journal is not None:
            try:
                self._journal(text, **fields)
            except Exception:
                pass

    # -- the session's clock ------------------------------------------------
    def _session(self):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        return s

    def _clock(self):
        s = self._session()
        clk = getattr(s, "clock", None) if s is not None else None
        if clk is None or getattr(clk, "source", None) != "audio_master":
            return None
        self._hook(clk)
        return clk

    def clock_nolock(self):
        """The running session's show audio clock, or None, taking no lock
        and hooking nothing: what the flame link's sender reads every frame
        (PR #43 review, item 9: it used to take this object's lock)."""
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return None
        clk = getattr(s, "clock", None)
        if clk is None or getattr(clk, "source", None) != "audio_master":
            return None
        return clk

    def _hook(self, clk):
        """Chain onto the clock's own on_pause/on_resume, after whatever the
        session set (the player's hard park), once per clock. The moment
        those fire is the moment the timecode froze or moved again."""
        with self._lock:
            if self._hooked is clk:
                return
            self._hooked = clk
            self._frozen = bool(getattr(clk, "_paused", False))
            before_p, before_r = clk.on_pause, clk.on_resume

            def on_pause():
                try:
                    if before_p is not None:
                        before_p()
                finally:
                    if self._hooked is clk:
                        self._frozen = True

            def on_resume():
                try:
                    if before_r is not None:
                        before_r()
                finally:
                    if self._hooked is clk:
                        self._frozen = False
            clk.on_pause, clk.on_resume = on_pause, on_resume

    def _no_clock(self, what):
        return C.failed(f"{what} was not sent: no show is running on this "
                        f"machine's show audio (Run not pressed, or the "
                        f"show file's clock is not \"audio_master\").")

    def playing(self):
        clk = self._clock()
        return bool(clk is not None and clk.playing)

    def _music(self, what, call):
        clk = self._clock()
        if clk is None:
            return self._no_clock(what)
        try:
            call(clk)
        except Exception as e:
            return C.failed(f"{what} failed: {e}")
        return C.done(f"{what}: sent to the show audio.")

    def music_hold(self, fade_s):
        return self._music(f"Music fade out over {fade_s:g} s and freeze",
                           lambda c: c.pause(fade_ms=fade_s * 1000.0))

    def music_resume(self, fade_s):
        return self._music(f"Music back in over {fade_s:g} s",
                           lambda c: c.resume(fade_ms=fade_s * 1000.0))

    def music_halt(self, fade_s):
        return self._music(f"Music fade out over {fade_s:g} s and stop",
                           lambda c: c.halt(fade_ms=fade_s * 1000.0))

    def music_frozen(self):
        """True once the clock has frozen, False once it is moving, None
        when there is no clock to ask (the conductor treats that as not
        confirmed, and says so)."""
        if self._clock() is None:
            return None
        return self._frozen

    # -- pixels: the player's override ---------------------------------------
    def _player(self):
        s = self._session()
        return (s, getattr(s, "player", None)) if s is not None \
            else (None, None)

    def pixels_fade_out(self, seconds):
        s, p = self._player()
        if p is None:
            return C.failed("Pixels to black was not sent: nothing is "
                            "running. Press Run first.")
        with self._lock:
            if not self._pix_ours:
                self._pix_prev = p.override
                self._pix_ours = True
            p.override = "blackout"
        if s.log:
            s.log.event("override", "show conductor set output to blackout")
        if seconds > 0:
            self._note(f"The pixels went black at once, not over "
                       f"{seconds:g} s: the pixel output has no fade.",
                       action="pixels", outcome="black_not_faded")
        return C.done("Pixels black.")

    def pixels_restore(self, seconds):
        s, p = self._player()
        if p is None:
            return C.failed("Pixels back was not sent: nothing is running.")
        left = False
        with self._lock:
            if not self._pix_ours:
                return C.done("Pixels: the show conductor had not taken "
                              "them, so they were left as they are.")
            self._pix_ours = False
            if p.override != "blackout":
                # Someone pressed a look on the page since: theirs stands.
                left, look = True, p.override
            else:
                p.override = self._pix_prev
        if left:
            # Written after the lock is let go: no lock is ever held across
            # a journal line (PR #43 review, item 9).
            self._note(f"Pixels left on {look or 'auto'}: the operator "
                       f"changed the look while the show conductor had "
                       f"them black.", action="pixels", outcome="left")
            return C.done("Pixels left on the operator's look.")
        if s.log:
            s.log.event("override", f"show conductor set output to "
                                    f"{p.override or 'auto'}")
        return C.done("Pixels back.")

    # -- flames ---------------------------------------------------------------
    def flames_zero(self):
        self.flames = C.ZERO
        if self.flame_link is None:
            return C.done("Flame cues zero: ltcplay sends none in this "
                          "build.")
        try:
            ok = self.flame_link.zero()
        except Exception as e:
            return C.failed(f"Flame cues to zero failed: {e}")
        return C.done("Flame cues zero.") if ok is True else \
            C.failed("Flame cues to zero did not go out.")

    def flames_release(self):
        self.flames = C.LIVE
        if self.flame_link is None:
            self._note("Flame cues released by the show conductor, but " +
                       NO_FLAME_LINK,
                       action="flames", outcome="nothing_sent")
            return C.done("Flame cues released; none are sent in this "
                          "build.")
        try:
            ok = self.flame_link.release()
        except Exception as e:
            return C.failed(f"Flame cues release failed: {e}")
        return C.done("Flame cues released.") if ok is True else \
            C.failed("Flame cues release did not go out.")

    def flames_disarm_all(self, reason):
        """With a flame link: its disarm_all (cues to zero, a zero frame,
        then the disarm, repeated past frame_stale_ms), done when it went
        out. Without one, never a quiet success: the cues go to zero, and
        the answer is a failure carrying NO_DISARM, which the conductor
        writes down as a fault, every time."""
        if self.flame_link is not None:
            self.flames = C.ZERO
            try:
                ok = self.flame_link.disarm_all(reason)
            except Exception as e:
                return C.failed(f"{reason}: the disarm failed: {e}")
            return C.done(f"{reason}: {DISARM_SENT}") if ok is True else \
                C.failed(f"{reason}: the disarm could NOT be sent to "
                         f"flamesafe (it is retried every frame). Disarm "
                         f"with the Stream Deck's Abort or its group keys.")
        z = self.flames_zero()
        tail = "" if z.ok else f" Zeroing the cues also failed: {z.sentence}"
        return C.failed(f"{reason}: {NO_DISARM}{tail}")


# ------------------------------------------------------------ flame cues --

class FlameControllerError(ValueError):
    """The flame controller in xlights_networks.xml cannot be used, in a
    sentence."""


class FlameControllerActive(FlameControllerError):
    """The flame controller is Active (or has no ActiveState) in
    xlights_networks.xml: the pixel output would send its channels."""


def flame_channels(networks_xml, name):
    """(first absolute channel, count) of the controller called `name` in
    xlights_networks.xml, walked in the same order netmap.load() and
    xLights use, so the channels are the ones the FSEQ renders for it.
    Refused unless that controller is Inactive: an active one is in the
    pixel output's map, and the pixel output would send its fire values
    straight to the flame node, around flamesafe. Count is at most 512."""
    found, near = [], []
    for c, first, span in _controllers(networks_xml):
        cname = c.attrib.get("Name", "")
        if cname == name:
            found.append((c, first, span))
        elif cname.strip().lower() == str(name).strip().lower():
            near.append(cname)
    if len(found) > 1:
        raise FlameControllerError(
            f"{len(found)} controllers are called {name!r} in "
            f"{networks_xml}, so which one is the flame controller cannot be "
            f"told. Give it a name of its own in xLights and the same name "
            f"as \"flame_controller\" in ltcplay_fire_ice.json.")
    if not found:
        hint = (f" There is one called {near[0]!r}: the name must match "
                f"exactly, capitals and spaces included." if near else "")
        raise FlameControllerError(
            f"There is no controller called {name!r} (exactly that spelling) "
            f"in {networks_xml}, so which channels are the flames cannot be "
            f"told.{hint} Every flame cue is zero.")
    c, first, span = found[0]
    if c.attrib.get("ActiveState", "Active") == "Active":
        raise FlameControllerActive(
            f"The flame controller {name!r} is Active in "
            f"{networks_xml}, so the pixel output would send its "
            f"fire values straight to the flame node, around "
            f"flamesafe. Set it Inactive in xLights (it keeps its "
            f"channels). Until then every flame cue is zero.")
    if span <= 0:
        raise FlameControllerError(
            f"The flame controller {name!r} in {networks_xml} has "
            f"no channels.")
    return first, min(span, 512)


def _controllers(networks_xml):
    """(element, first absolute channel, span) for every controller in
    xlights_networks.xml, in netmap.load()'s and xLights' order."""
    import xml.etree.ElementTree as ET
    root = ET.parse(networks_xml).getroot()
    chan = 1
    out = []
    for c in root:
        if c.tag != "Controller":
            continue
        nets = [n for n in c if n.tag == "network"]
        span = sum(max(0, int(n.attrib.get("MaxChannels", "0") or 0))
                   for n in nets)
        out.append((c, chan, span))
        chan += span
    return out


def map_total_channels(networks_xml):
    """Every channel xlights_networks.xml lays out, as netmap.load() counts
    them: what a render made for this map has."""
    return sum(span for _c, _f, span in _controllers(networks_xml))


def flame_destinations(networks_xml, name):
    """(address, universe, protocol) of every network row of the flame
    controller `name` (ComPort, else the controller's IP; BaudRate is the
    universe, as netmap.load() reads them)."""
    from . import netmap
    out = set()
    for c, _f, _s in _controllers(networks_xml):
        if c.attrib.get("Name", "") != name:
            continue
        for n in c:
            if n.tag != "network":
                continue
            na = n.attrib
            proto = netmap.UDP_PROTOCOLS.get(
                (na.get("NetworkType") or "").lower())
            dest = na.get("ComPort", "") or c.attrib.get("IP", "")
            try:
                univ = int(na.get("BaudRate", ""))
            except ValueError:
                continue
            if proto and dest:
                out.add((dest, univ, proto))
    return out


def flamesafe_destination(path):
    """(address, universe, "e131") flamesafe sends the flame node, read from
    its own config, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return (str(doc["destination"]["ip"]), int(doc["universe"]), "e131")
    except Exception:
        return None


def _show_networks(show_file):
    """xlights_networks.xml of the show this show file plays, or None."""
    from . import timeline as timeline_mod
    try:
        tl = timeline_mod.Timeline.load(show_file)
    except Exception:
        return None
    return os.path.join(getattr(tl, "show_dir", "") or "",
                        "xlights_networks.xml")


def refuse_active_flame_controller(show_file, name, fs_dest=None):
    """Raise FlameControllerError when the show this file plays cannot be
    run safely with flames, from its xlights_networks.xml:
      - the flame controller `name` is Active (or has no ActiveState,
        which xLights reads as Active): the pixel output would send its
        fire values straight to the flame node, around flamesafe (PR #43
        review, finding 3);
      - no controller has exactly that name, or two do, or it has no
        channels (fix round 2, B: a rename or a change of case in xLights
        used to slip past, with the controller Active);
      - any Active controller sends to the flame controller's own address
        and universe, or to flamesafe's destination (fix round 2, B: a
        second controller at the flame node's address did).
    Returns the (address, universe, protocol) set the pixel output must
    never send to. A map that does not parse is left to the session, which
    refuses it itself."""
    path = _show_networks(show_file)
    if not path or not os.path.exists(path):
        return set()
    try:
        _controllers(path)
    except Exception:
        return set()
    flame_channels(path, name)
    blocked = set(flame_destinations(path, name))
    if fs_dest:
        blocked.add(tuple(fs_dest))
    from . import netmap
    try:
        nm = netmap.load(path)
    except Exception:
        return blocked
    for u in nm.universes:
        if (u.ip, u.universe, u.protocol) in blocked:
            raise FlameControllerActive(
                f"Controller {u.controller!r} is Active in {path} and sends "
                f"to {u.ip}, {u.protocol} universe {u.universe}: the flame "
                f"node's own address and universe, so the pixel output would "
                f"send its values straight to the flame node, around "
                f"flamesafe. Set it Inactive, or give it another address, "
                f"in xLights.")
    return blocked


# The Stream Deck's arm keys (streamdeck.GROUP_KEY_LIMIT; not imported, so
# this module never loads the deck).
FLAME_GROUP_LIMIT = 3


def check_flame_groups(folder, cfg):
    """At `ltc serve` startup: the flame groups in flamesafe's config, the
    one place they are decided (Jeff: a settings change at tech, never a
    code change). The deck's and the pages' labels are read from the same
    file. Raises FireIceConfigError, in a sentence, for an edit that would
    not work on the night: more than FLAME_GROUP_LIMIT groups, a group with
    no name or one name twice, one channel (safety or fire) in two groups,
    or a channel the flame controller in a show's layout does not have (a
    head that does not exist)."""
    if not cfg.flamesafe_config:
        return
    path = cfg.flamesafe_config
    try:
        with open(path, encoding="utf-8-sig") as fh:
            groups = json.load(fh).get("groups")
    except (OSError, ValueError, AttributeError) as e:
        raise FireIceConfigError(f"{path}: could not be read: {e}")
    if not isinstance(groups, list) or not groups:
        raise FireIceConfigError(f"{path}: it lists no flame groups.")
    if len(groups) > FLAME_GROUP_LIMIT:
        raise FireIceConfigError(
            f"{path} lists {len(groups)} flame groups; the Stream Deck has "
            f"{FLAME_GROUP_LIMIT} arm keys. Put the heads into at most "
            f"{FLAME_GROUP_LIMIT} groups.")
    owner, names = {}, set()
    for i, g in enumerate(groups):
        name = g.get("name") if isinstance(g, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise FireIceConfigError(f"{path}: group {i + 1} has no name.")
        if name in names:
            raise FireIceConfigError(
                f"{path}: two groups are both called {name!r}.")
        names.add(name)
        slots = [g.get("safety")] + list(g.get("fire") or [])
        for s in slots:
            if not isinstance(s, int) or isinstance(s, bool):
                raise FireIceConfigError(
                    f"{path}: {name} lists {s!r}, which is not a channel "
                    f"number.")
            if s in owner and owner[s] != name:
                raise FireIceConfigError(
                    f"{path}: channel {s} is in both {owner[s]} and {name}. "
                    f"A head belongs to one group only.")
            owner[s] = name
    if not cfg.flame_controller or not folder or not os.path.isdir(folder):
        return
    layouts = [os.path.join(folder, "xlights_networks.xml")]
    for n in sorted(os.listdir(folder)):
        if n.lower().endswith(".json"):
            try:
                layouts.append(_show_networks(os.path.join(folder, n)))
            except Exception:
                pass
    seen = set()
    for xml in layouts:
        if not xml or xml in seen or not os.path.isfile(xml):
            continue
        seen.add(xml)
        try:
            _first, count = flame_channels(xml, cfg.flame_controller)
        except FlameControllerError:
            continue     # check_flame_controllers says why, in its words
        for s, name in sorted(owner.items()):
            if not 1 <= s <= count:
                raise FireIceConfigError(
                    f"{path}: {name} lists channel {s}, but the flame "
                    f"controller {cfg.flame_controller!r} has channels 1 to "
                    f"{count} in {xml}. There is no such head.")


def check_flame_controllers(folder, cfg):
    """At `ltc serve` startup: every show file in the folder, checked with
    refuse_active_flame_controller. Raises FireIceConfigError naming the
    first one that would put fire values on the pixel output."""
    if not cfg.flame_controller or not folder or not os.path.isdir(folder):
        return
    for n in sorted(os.listdir(folder)):
        if not n.lower().endswith(".json"):
            continue
        p = os.path.join(folder, n)
        try:
            with open(p, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "cues" not in doc:
            continue
        try:
            refuse_active_flame_controller(
                p, cfg.flame_controller,
                flamesafe_destination(cfg.flamesafe_config)
                if cfg.flamesafe_config else None)
        except FlameControllerError as e:
            raise FireIceConfigError(f"{n}: {e}")


class FlameCues:
    """flamelink's cue provider: the show's flame universe, read from the
    show's OWN render at the show timecode (PR #43 review, finding 1).

    The timecode FlameLink passes is the frame the show audio last sent
    (clock.AudioMaster: 00:00:00:00 at the top of the cue it is playing).
    The values are that frame of THAT cue's FSEQ, read through a file handle
    of this provider's own (never the pixel output's, whose block cache is
    not shared across threads), at the Inactive flame controller's
    channels. Never the pixel output's buffer: Blackout, Preshow and a look
    override leave that buffer holding a frame that is not the show's.

    None (all zeros) unless every one of these holds: Run pressed and the
    session running; the show audio playing a cue of this show file and
    not paused; the pixel output following the show (no Blackout, Preshow
    or look override, and no GO free run); the timecode is the clock's own
    current frame; the show folder has a usable flame controller; the frame
    is inside the cue's render. Never raises."""

    def __init__(self, control, name, journal=None, background=False):
        self.control = control
        self.name = name
        self._journal = journal
        self._folder = None      # (session, map path, its mtime and size)
        self._span = None
        self._total = None       # the map's channel count, for the render
        self._problem = ""
        self._why = ""
        self._open = {}     # fseq path -> (FSEQ, [(dst, src, length)])
        self.link = None    # the FlameLink (closing it closes the files)
        # background (ltc serve): every file is read, and every file's date
        # looked at, on a thread of its own, never on the flame link's
        # sender, so a stalled drive can never hold up a flame frame (the
        # show PC's SSD stalled on 2026-09-25). The render is read whole
        # into memory. Without it (the selftest), the same work is done in
        # the call, as before.
        self._bg = bool(background)
        self._layout = None      # (session, span, total), background mode
        self._by_path = {}       # fseq path -> (FSEQ, spans) or an error
        self._stop = threading.Event()
        self._thread = None

    def _note(self, text, **f):
        if self._journal is not None:
            try:
                self._journal(text, **f)
            except Exception:
                pass

    def _zero(self, why):
        """All zeros, saying why once each time the reason changes (an
        episode); the cue going out again clears it."""
        if why != self._why:
            self._why = why
            if why:
                self._note(f"Flame cues are zero: {why}", action="flames",
                           outcome="cues_zero")
        return None

    def _locate(self, session):
        """The flame controller's channels, found again for every new
        session and whenever xlights_networks.xml changes (fix round 2, A:
        they were cached per show folder for the life of ltc serve, so a
        layout changed with serve running read fire from the wrong
        channels)."""
        folder = getattr(getattr(session, "tl", None), "show_dir", None)
        path = getattr(session, "nm_path", None) or (
            os.path.join(folder, "xlights_networks.xml") if folder else None)
        try:
            st = os.stat(path) if path else None
            stamp = (st.st_mtime_ns, st.st_size) if st else None
        except OSError:
            stamp = None
        if self._folder is not None and self._folder[0] is session and \
                self._folder[1:] == (path, stamp):
            return self._span
        self._folder = (session, path, stamp)
        self._span, self._total = None, None
        try:
            if not path:
                raise FlameControllerError("The running show has no show "
                                           "folder.")
            self._span = flame_channels(path, self.name)
            self._total = map_total_channels(path)
            self._problem = ""
            self._note(f"Flame cues: from controller {self.name!r}, "
                       f"channels {self._span[0]} to "
                       f"{self._span[0] + self._span[1] - 1} of the show.",
                       action="flames", outcome="cues_found")
        except Exception as e:
            text = str(e)
            if text != self._problem:
                self._problem = text
                self._note(f"Flame cues are zero: {text}", fault=True,
                           action="flames", outcome="cues_refused")
        return self._span

    def _render(self, path):
        try:
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            key = (path, None, None)
        got = self._open.get(key)
        if got is None:
            for old in [k for k in self._open if k[0] == path]:
                try:
                    self._open.pop(old)[0].close()
                except Exception:
                    pass
            import io
            from .fseq import FSEQ
            with open(path, "rb") as fh:
                data = fh.read()
            f = FSEQ(path, fileobj=io.BytesIO(data))
            spans, src = [], 0
            for start0, length in (f.sparse_ranges or
                                   [(0, f.channel_count)]):
                spans.append((start0, src, length))
                src += length
            got = self._open[key] = (f, spans)
        return got

    def _refresh_once(self):
        """Background mode: the channels and every cue's render of the
        running session, read and published for the sender to use."""
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return
        span = self._locate(s)
        self._layout = (s, span, self._total)
        by = {}
        for c in (getattr(getattr(s, "tl", None), "cues", None) or ()):
            path = getattr(c, "path", None)
            if path:
                try:
                    by[path] = self._render(path)
                except Exception as e:
                    by[path] = e
        self._by_path = by

    def _refresher(self):
        while not self._stop.is_set():
            try:
                self._refresh_once()
            except Exception:
                pass
            self._stop.wait(0.25)

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        for f, _spans in self._open.values():
            try:
                f.close()
            except Exception:
                pass
        self._open.clear()

    def __call__(self, tc):
        s = getattr(self.control, "session", None)
        if s is None or not getattr(s, "running", False):
            return self._zero("")
        clk = getattr(s, "clock", None)
        cue = getattr(clk, "_cue", None)
        if getattr(clk, "source", None) != "audio_master" or not cue or \
                getattr(clk, "paused", True):
            return self._zero("")
        p = getattr(s, "player", None)
        if p is None:
            return self._zero("")
        look = getattr(p, "override", None)
        if look is not None:
            return self._zero(f"the pixel output is on {look}, not the "
                              f"show")
        if getattr(p, "freerun_epoch", None) is not None:
            return self._zero("the show was moved by hand (GO), so the "
                              "pixels are not following the show audio")
        last = getattr(clk, "last_sent", None)
        if not tc or not last or tc != (f"{last[0]:02d}:{last[1]:02d}:"
                                         f"{last[2]:02d}:{last[3]:02d}"):
            return self._zero("")
        if self._bg:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(
                    target=self._refresher, daemon=True,
                    name="ltcplay-flame-cues-read")
                self._thread.start()
            lay = self._layout
            if lay is None or lay[0] is not s:
                return self._zero("the flame controller's channels are "
                                  "still being read (off the flame link's "
                                  "sender)")
            span, total = lay[1], lay[2]
        else:
            span = self._locate(s)
            total = self._total
        if span is None:
            return None
        tl = getattr(s, "tl", None)
        label = cue.get("label") if isinstance(cue, dict) else None
        hits = [c for c in (getattr(tl, "cues", None) or ())
                if getattr(c, "name", None) == label]
        if len(hits) != 1:
            return self._zero(f"the show audio is playing {label!r}, which "
                              f"is not one cue of this show file")
        try:
            if self._bg:
                got = self._by_path.get(hits[0].path)
                if got is None:
                    return self._zero("the show's render is still being "
                                      "read into memory (off the flame "
                                      "link's sender)")
                if isinstance(got, Exception):
                    raise got
                f, spans = got
            else:
                f, spans = self._render(hits[0].path)
            # A render whose one range starts at channel 1 is a whole
            # render and must match the map exactly; a truly sparse one
            # must at least lie inside it.
            have = max(s + n for s, _src, n in spans)
            whole = len(spans) == 1 and spans[0][0] == 0
            if total is None or (have != total if whole
                                 else have > total):
                return self._zero(
                    f"the show's render has {have} channels but "
                    f"xlights_networks.xml lays out {total}: they were "
                    f"not made for each other, so which channels are the "
                    f"flames cannot be told. Render the show again for this "
                    f"layout")
            rel = (last[0] * 3600 + last[1] * 60 + last[2]) + last[3] / 30.0
            idx = int(rel * 1000.0 // f.step_time_ms)
            if not 0 <= idx < f.frame_count:
                return self._zero("the timecode is past the end of the "
                                  "show's render")
            data = f.frame(idx)
        except Exception as e:
            return self._zero(f"the show's render could not be read "
                              f"({type(e).__name__}: {e})")
        start, count = span
        first, end = start - 1, start - 1 + count
        out = [0] * 512
        for dst, src, length in spans:
            lo, hi = max(dst, first), min(dst + length, end)
            if lo < hi:
                out[lo - first:hi - first] = data[src + lo - dst:
                                                  src + hi - dst]
        self._why = ""
        return out


class OffThreadJournal:
    """A journal that never makes its caller wait: each line is queued and
    written in order by a thread of its own. Up to MAX lines may wait; past
    that the newest are dropped and counted, and the count is written as a
    fault once the line is moving again. Never raises."""

    MAX = 500

    def __init__(self, journal):
        import collections
        self._journal = journal
        self._q = collections.deque()
        self._cv = threading.Condition()
        self._busy = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-flame-journal")
        self._thread.start()

    def __call__(self, text, **fields):
        with self._cv:
            if len(self._q) >= self.MAX:
                self.dropped += 1
                return None
            self._q.append((text, fields))
            self._cv.notify_all()
        return None

    def _run(self):
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                text, fields = self._q.popleft()
                dropped, self.dropped = self.dropped, 0
                self._busy = True
            try:
                if dropped:
                    self._write(f"Flame link: {dropped} journal line(s) "
                                f"were dropped because the journal was not "
                                f"keeping up.",
                                {"fault": True, "action": "flame_link",
                                 "outcome": "journal_dropped"})
                self._write(text, fields)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _write(self, text, fields):
        try:
            self._journal(text, **fields)
        except Exception:
            pass

    def flush(self, timeout=5.0):
        """Wait until every queued line is written (tests, and closing)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._q and not self._busy,
                                     timeout)


_log_writer = None     # the one BackgroundShowLog writer thread running


@atexit.register
def _drain_show_log():
    """Every queued show log line is written before the program exits."""
    lst = _log_writer
    if lst is not None:
        try:
            lst.stop()
        except Exception:
            pass


class BackgroundShowLog(showlog_mod.ShowLog):
    """The show log for Fire & Ice: showlog.ShowLog, byte for byte the GPL
    one, with its file handler (and the console echo) moved behind a queue
    onto one writer thread of its own. The threads that log, the show
    audio's timecode thread among them, then never write, flush or print a
    line, nor wait on the logging handler's lock while another thread does
    (PR #43 review, finding 9). A new one stops the last one's writer,
    which drains first, as ShowLog replaces the logger's handlers."""

    def __init__(self, path, echo=False, **kw):
        global _log_writer
        import logging
        import logging.handlers
        import queue
        import sys
        super().__init__(path, echo=False, **kw)
        old, _log_writer = _log_writer, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        out = list(self._log.handlers)
        if echo:
            e = logging.StreamHandler(sys.stdout)
            e.setFormatter(logging.Formatter("%(message)s"))
            out.append(e)
        q = queue.SimpleQueue()
        self._writer = logging.handlers.QueueListener(q, *out)
        self._writer.start()
        _log_writer = self._writer
        self._log.handlers[:] = [logging.handlers.QueueHandler(q)]
        self.background = True

    def flush(self):
        """Wait until every line logged so far is written."""
        w = self._writer
        if w is not None and w is _log_writer:
            w.stop()
            w.start()


def flame_link_config(cfg):
    """The FlameLinkConfig read from flamesafe's own config, or None when
    the Fire & Ice config names none. `ltc serve` calls this before it
    binds anything, so a flamesafe config it cannot read stops it in one
    sentence. Raises FireIceConfigError."""
    if not cfg.flamesafe_config:
        return None
    from . import flamelink
    try:
        return flamelink.FlameLinkConfig.from_flamesafe_config(
            cfg.flamesafe_config)
    except flamelink.FlameLinkConfigError as e:
        raise FireIceConfigError(str(e))


def build_flame_link(cfg, control, show, journal=None):
    """The FlameLink for this config, started, or None when the config
    names no flamesafe config. Raises FireIceConfigError for a config file
    that cannot be read, so serve refuses to start rather than run without
    the link it was told to have."""
    if not cfg.flamesafe_config:
        return None
    from . import flamelink
    lcfg = flame_link_config(cfg)
    # The flame link's own sender thread writes its journal lines (a
    # timecode that stopped at a show's end, a seek, a refused flame
    # controller) at the very moments the rest of the show is busiest. A
    # journal line is never written on that thread: it is handed to a line
    # of its own, so nothing the night journal or the console does can
    # hold up a flame frame (CONTRACT.md's 50 ms floor).
    journal = OffThreadJournal(journal) if journal is not None else None
    cues = (FlameCues(control, cfg.flame_controller, journal,
                      background=True)
            if cfg.flame_controller else flamelink.zero_cues)
    link = flamelink.FlameLink(
        lcfg, cues=cues,
        show_state=flamelink.audio_master_state(show.clock_nolock),
        journal=journal)
    link.journal_line = journal
    if journal is not None and not _has_status_mirror(cfg.flamesafe_config):
        journal("Flame link: flamesafe's status frames go to the Stream Deck "
                "program and flamesafe's config has no link.status_mirror_port"
                ", so this program cannot see flamesafe confirm a disarm, or "
                "raise the lock alarm itself; the Stream Deck shows both. Set "
                "status_mirror_port to give this program its own copy.",
                action="flame_link", outcome="no_status")
    # The seek guard (PR #39) counts the show audio's own timecode frames,
    # which are always 30 a second (clock.MASTER_FPS), whatever the show
    # file's rate.
    link.tc_fps = 30.0
    if isinstance(cues, FlameCues):
        cues.link = link
    return link


def _has_status_mirror(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return "status_mirror_port" in (doc.get("link") or {})
    except (OSError, ValueError, AttributeError):
        return False


# ------------------------------------------------------------- the runner --

class ShowRunner:
    """What the scheduler's dry run used to make up, done for real, only
    while Service.dry_run is False (config "scheduler_performs": true).
    schedule.py's "Contract for PR 3":

      START_SHOW     start_show(): the show cue on the running session's
                     clock (and the MadMapper show bank selected first).
                     Only once Run has been pressed: nothing reaches the
                     rig until then (CLAUDE.md), so without a running show
                     the start is refused and reported as SHOW_FAILED.
      SHOW_CONFIRMED the timecode is seen moving after the start.
      SHOW_ENDED     the cue it started has ended on its own (the audio's
                     end), while the scheduler is still in SHOW.
      SHOW_FAILED    the start was refused, or the cue ended before it was
                     ever confirmed.
      CLOSING_DONE   at closing: flame cues zero and lasers blanked by the
                     conductor, pixels black, then reported done. The
                     MadMapper stop is not performed and the journal says so.

    poll() does all the reporting; a thread calls it every POLL_S. Reports
    are made outside the conductor and with no lock of this module held."""

    POLL_S = 0.1

    def __init__(self, svc, control, show, conductor, cfg, journal=None,
                 madmapper=None):
        self.svc = svc
        self.control = control
        self.show = show
        self.conductor = conductor
        self.cfg = cfg
        self.mm = madmapper
        self._journal = journal
        self._lock = threading.Lock()
        self._cue = None    # {"show", "clock", "played", "confirmed",
                            #  "failed"}
        self._closing_reported = False
        self._stop = threading.Event()
        self._thread = None

    def _note(self, text, **fields):
        if self._journal is not None:
            try:
                self._journal(text, **fields)
            except Exception:
                pass

    def start_show(self, n, who="the scheduler"):
        """Called on the scheduler's ordered line of conductor requests,
        off its lock, after tonight is saved; the conductor is told a show
        started only once it is confirmed. Returns a conductor Result at
        once. `who` is "the scheduler" for a show that came due, or the
        operator who pressed Start now."""
        clk = self.show._clock()
        s = self.show._session()
        auto = who == "the scheduler"
        if auto and self.cfg.auto_start == "off":
            why = ("auto_start is off in ltcplay_fire_ice.json, so the "
                   "scheduler does not start a show by itself; press Start "
                   "now to start it")
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}.")
        if s is None or clk is None or self.conductor.latched:
            why = ("Run has not been pressed, so nothing may reach the rig"
                   if s is None else
                   "the running show file's clock is not \"audio_master\""
                   if clk is None else
                   "the show is aborted, and the show conductor stays "
                   "latched until someone presses Reset")
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}.")
        self._clear_look(s, n)
        if self.mm is not None:
            try:
                self.mm.select_bank(self.mm.cfg.show_bank, wait=False)
            except Exception as e:
                self._note(f"Selecting the MadMapper show bank failed: {e}",
                           fault=True, action="video", outcome="failed")
        try:
            pick = s.clock_play(self.cfg.show_cue)
        except Exception as e:
            with self._lock:
                self._cue = {"show": n, "failed": str(e)}
            return C.failed(f"Show {n} was not started: {e}")
        # The music is now playing, whatever the conductor's record says:
        # an Abort or Hold before SHOW_CONFIRMED must stop or freeze it (PR
        # #43 review, finding 4).
        told = getattr(self.conductor, "music_started", None)
        if told is not None:
            told()
        if self.conductor.latched:
            # An Abort landed while the cue was starting, so its music step
            # may have run before the cue began: stop the music here too.
            why = "it was aborted while it was starting"
            self.show.music_halt(C.ABORT_FADE_S)
            with self._lock:
                self._cue = {"show": n, "failed": why}
            return C.failed(f"Show {n} was not started: {why}; the music "
                            f"was stopped.")
        with self._lock:
            self._cue = {"show": n, "clock": clk,
                         "played": clk.cues_played, "confirmed": False,
                         "failed": None}
        return C.done(f"Show {n}: {pick.name} started on the show audio.")

    def _clear_look(self, s, n):
        """A look the page chose before the show (Preshow, Blackout, any
        override) does not stay on through the scheduled show: the 409 gate
        then keeps anyone from clearing it, and the flame cues stay zero
        all show (PR #43 fix round 2, E). The show conductor's own black
        (between shows) is its to lift on SHOW_CONFIRMED; only what was
        under it is forgotten."""
        p = getattr(s, "player", None)
        if p is None:
            return
        show = self.show
        look = getattr(p, "override", None)
        if getattr(show, "_pix_ours", False):
            # The conductor's black: it stays until SHOW_CONFIRMED lifts it,
            # and the look under it is forgotten. A look the page put over
            # it is taken back to that black.
            show._pix_prev = None
            if look == "blackout":
                return
            p.override = "blackout"
        elif look is None:
            return
        else:
            p.override = None
        self._note(f"Show {n}: the page's {look} look was cleared so the "
                   f"show plays as rendered.", action="pixels",
                   outcome="look_cleared")

    def poll(self):
        sch = self.svc.machine
        state = sch.state if sch is not None else None
        with self._lock:
            cue = self._cue
        if state == "CLOSING":
            if not self._closing_reported:
                self._closing_reported = True
                self._close()
            return
        self._closing_reported = False
        if cue is None:
            return
        n = cue["show"]
        if state not in ("SHOW", "PAUSED"):
            # Aborted, or the scheduler moved on: nothing to report on.
            with self._lock:
                if self._cue is cue:
                    self._cue = None
            return
        if cue.get("failed"):
            if state == "SHOW":
                with self._lock:
                    if self._cue is cue:
                        self._cue = None
                self.svc.report("SHOW_FAILED", cue["failed"], show=n)
            return
        clk = cue["clock"]
        mine = clk.playing and clk.cues_played == cue["played"]
        if not cue["confirmed"] and mine and \
                getattr(clk, "_last_frame", None) not in (None, 0):
            cue["confirmed"] = True
            self.svc.report("SHOW_CONFIRMED",
                            "the show timecode is moving", show=n)
            return
        if not mine and state == "SHOW":
            with self._lock:
                if self._cue is cue:
                    self._cue = None
            how = getattr(clk, "last_ended", "") or "the cue stopped"
            self.svc.report("SHOW_ENDED" if cue["confirmed"]
                            else "SHOW_FAILED", how, show=n)

    def _close(self):
        r = self.conductor.intermission("the scheduler", "")
        if not r.ok:
            self._note(f"Closing: {r.sentence}", fault=True,
                       action="closing", outcome="failed")
        if self.show._session() is not None:
            self.show.pixels_fade_out(1.0)
        self._note("Closing: the MadMapper stop is not performed in this "
                   "build.", action="closing", outcome="not performed")
        self.svc.report("CLOSING_DONE",
                        "flame cues zero, lasers blanked, pixels black")

    def _run(self):
        while not self._stop.wait(self.POLL_S):
            try:
                self.poll()
            except Exception as e:
                self._note(f"The show runner hit an error and carried on: "
                           f"{type(e).__name__}: {e}. That is a bug in "
                           f"ltcplay.", fault=True, action="runner",
                           outcome="error")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ltcplay-show-runner")
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2.0)


# ---------------------------------------------------------------- wiring --

class Wiring:
    """What attach() built, for web.serve to keep and close."""

    def __init__(self, conductor, show, devices, runner, flame_link=None):
        self.conductor = conductor
        self.show = show
        self.devices = devices
        self.runner = runner
        self.flame_link = flame_link

    def close(self):
        try:
            if self.runner is not None:
                self.runner.close()
            self.conductor.close()
            gate = getattr(getattr(self.devices, "beyond", None), "gate",
                           None)
            if gate is not None:
                gate.close()
        finally:
            # Last: zero frames until the end. flamesafe disarms every
            # group once it stops hearing it.
            fl = self.flame_link
            if fl is not None and hasattr(fl, "stop"):
                try:
                    fl.zero()
                finally:
                    fl.stop()
                    closer = getattr(getattr(fl, "cues", None), "close",
                                     None)
                    if closer is not None:
                        closer()


def attach(svc, control, cfg, madmapper=None, beyond=None, announce=None,
           journal=None, flame_link=None, threaded=True, **conductor_kw):
    """Build the real Conductor and attach it to the scheduler service
    `svc`. `madmapper` is web.serve's (link, watchdog) pair or None,
    `beyond` its Beyond or None. Call before svc.start(), so the first tick
    already reaches the conductor. `threaded` False and `conductor_kw`
    (clock, waiter) are the selftest's: nothing runs on its own then."""
    link = madmapper[0] if madmapper is not None else None
    built_link = None
    # The show log is written on a thread of its own, so the show audio's
    # timecode thread never writes, flushes or prints a line itself, nor
    # waits on the logging lock (PR #43 review, finding 9).
    defaults = dict(getattr(control, "defaults", None) or {})
    defaults["log_factory"] = BackgroundShowLog
    control.defaults = defaults
    if cfg.flame_controller:
        # The flame controller's channels are never sent by the pixel
        # output in Fire & Ice, whatever xlights_networks.xml says, and a
        # show whose flame controller is Active is refused at Run (PR #43
        # review, finding 3).
        defaults = dict(getattr(control, "defaults", None) or {})
        defaults["exclude_controllers"] = (cfg.flame_controller,)
        control.defaults = defaults

        fs_dest = (flamesafe_destination(cfg.flamesafe_config)
                   if cfg.flamesafe_config else None)

        def before_open(show_file, _name=cfg.flame_controller):
            """Refuses a show that cannot run safely with flames, and
            returns what the session must leave out of the pixel output:
            the flame controller by name AND every address and universe
            the flame node is reached at (fix round 2, B)."""
            from .session import SessionError
            try:
                blocked = refuse_active_flame_controller(show_file, _name,
                                                         fs_dest)
            except FlameControllerError as e:
                raise SessionError(f"This show will not start: {e}")
            return {"exclude_destinations": tuple(sorted(blocked))}
        control.before_open = before_open
    elif cfg.flamesafe_config and journal is not None:
        journal("Flame cues: no 'flame_controller' is named in "
                "ltcplay_fire_ice.json, so no flame cue is ever sent: every "
                "flame frame is zero.", fault=True, action="flames",
                outcome="no_controller")
    if flame_link is None and cfg.flamesafe_config:
        # Built before the show outputs so they have it from the start; the
        # clock it reads is looked up through the show on every frame.
        holder = {}
        show = FireIceShow(control, journal=journal,
                           flame_link=_LinkSlot(holder))
        built_link = build_flame_link(cfg, control, show, journal)
        holder["link"] = built_link
        show.flame_link = built_link
        if threaded:
            built_link.start()
        else:
            built_link.open()
    else:
        show = FireIceShow(control, journal=journal, flame_link=flame_link)
    blanking = build_blanking(cfg, beyond, journal, threaded)
    devices = C.ConductorDevices(link, blanking, journal=journal)

    def state():
        m = svc.machine
        return m.state if m is not None else None
    conductor = C.Conductor(
        devices, show, C.laser_gate_for(state),
        hold_gate=svc.hold_for_announcement,
        announcer=announce.play if announce is not None else None,
        journal=journal, threaded=threaded, **conductor_kw)
    svc.conductor = conductor
    runner = None
    if cfg.scheduler_performs:
        runner = ShowRunner(svc, control, show, conductor, cfg,
                            journal=journal, madmapper=link)
        svc.performer = runner
        svc.dry_run = False
        if threaded:
            runner.start()
    if journal is not None:
        journal(f"Fire & Ice show conductor built: {cfg.summary()}.",
                action="fire_ice", outcome="built")
    return Wiring(conductor, show, devices, runner, built_link)


def build_blanking(cfg, beyond, journal=None, threaded=True):
    """BEYOND as the conductor sees it: beyondtc.Blanking, keeping the
    lasers dark by timecode, OSC or both (cfg.beyond_blank). Journals the
    mode. None only when there is no BEYOND at all (OSC mode, no BEYOND
    configured)."""
    from . import beyondtc
    mode = getattr(cfg, "beyond_blank", "timecode")
    if mode == "osc" and beyond is None:
        return None
    gate = None
    if mode in ("timecode", "both"):
        ip = getattr(cfg, "beyond_timecode_ip", None) or (
            beyond.cfg.host if beyond is not None and
            getattr(beyond, "cfg", None) is not None else None)
        gate = beyondtc.TimecodeGate(ip, hour=cfg.beyond_black_hour,
                                     journal=journal)
        if threaded:
            # Only a real serve registers the gate with the show's timecode
            # sender; the selftest's unthreaded attach() starts it itself.
            gate.start()
    if journal is not None:
        where = gate.ip if gate is not None and gate.ip else \
            "the show file's BEYOND node"
        what = {"timecode": f"by timecode: BEYOND's own Art-Net timecode "
                            f"({where}) "
                            f"jumps to the black zone, hour "
                            f"{cfg.beyond_black_hour}, running, whenever the "
                            f"lasers must be dark",
                "osc": "by OSC: BEYOND's brightness 0 or 100",
                "both": f"by timecode (black zone hour "
                        f"{cfg.beyond_black_hour}) AND by OSC brightness"}[mode]
        journal(f"Lasers are blanked {what} (beyond_blank \"{mode}\").",
                action="lasers", outcome="blank_mode")
    return beyondtc.Blanking(mode, gate=gate, osc=beyond, journal=journal)


class _LinkSlot:
    """Stands in for the flame link only while it is being built, so
    FireIceShow does not journal "no flame link" for a link that is about
    to exist."""

    def __init__(self, holder):
        self.holder = holder
