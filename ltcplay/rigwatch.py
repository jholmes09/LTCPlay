"""Is the rig still out there?

The output layer heals what it can SEE: a socket whose sends raise errors gets
rebuilt. A replugged USB network adapter is mostly invisible to it. To a
broadcast address, or a route macOS accepts and discards, sendto returns
success, so there is no error, no counter and nothing to rebuild, and the page
reports a healthy output while the trees stand dark.

`check` already pings every controller before a show and prints "22 of 22
answer a ping". The running engine never asked again. This asks again, on a
slow beat, in its own thread, and never touches the output path.

Jeff pulled the USB-C cable carrying both his network and his Dante audio on
2026-09-14; plugging it back in did not recover, and nothing on screen said
why.

False alarms are the whole risk here, because a warning nobody trusts is worse
than no warning. Plenty of ArtNet nodes never answer ICMP at all. So this
measures a DELTA: the first sweep records which controllers answer, and only
those are ever reported as having stopped. A rig where nothing answers is
reported as not watchable, once, and then left alone.
"""
import concurrent.futures
import subprocess
import sys
import threading
import time


def ping(ip, timeout_s=1.0):
    """One ping to one address. True only when it answered.

    The Mac flags are the ones this program has always used. Windows ping
    reads the same letters differently (-c is a routing compartment, -t is
    ping forever), and it exits 0 when a router answers "destination host
    unreachable" on the controller's behalf, so there only a reply that
    carries a TTL counts."""
    if sys.platform == "win32":
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", ip],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout_s + 1.5)
        return r.returncode == 0 and b"TTL=" in r.stdout.upper()
    r = subprocess.run(["ping", "-c", "1", "-W", "1000", "-t", "1", ip],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout_s + 1.5)
    return r.returncode == 0


class RigWatch:
    INTERVAL_S = 15.0
    PING_TIMEOUT_S = 1.0
    MAX_PARALLEL = 16

    def __init__(self, ips, log=None, interval_s=None, pinger=None):
        # Ordered, de-duplicated: one controller can own many universes.
        seen, self.ips = set(), []
        for ip in ips:
            if ip and ip not in seen:
                seen.add(ip)
                self.ips.append(ip)
        self.log = log
        self.interval_s = interval_s or self.INTERVAL_S
        self._ping = pinger or self._ping_once
        self.baseline = None      # None until the first sweep completes
        self.answering = set()
        self.missing = []
        self.checked_at = None
        self.sweeps = 0
        self.usable = None        # False when nothing ever answered
        self._running = False
        self._thread = None
        self._told = None

    # -- the sweep --------------------------------------------------------
    def _ping_once(self, ip):
        try:
            return ping(ip, self.PING_TIMEOUT_S)
        except Exception:
            return False

    def sweep(self):
        if not self.ips:
            self.usable = False
            return set()
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.MAX_PARALLEL, len(self.ips))) as ex:
                got = list(ex.map(self._ping, self.ips))
        except Exception:
            # A machine that cannot spawn ping is not a machine with a dead
            # rig. Say nothing rather than something wrong.
            return self.answering
        up = {ip for ip, ok in zip(self.ips, got) if ok}
        self.answering = up
        self.checked_at = time.monotonic()
        self.sweeps += 1
        if self.baseline is None:
            self.baseline = set(up)
            self.usable = bool(up)
            if not up:
                self._note("no controller answered a ping at the start, so "
                           "this show cannot tell you when the rig stops "
                           "receiving")
            else:
                self._note(f"watching {len(up)} of {len(self.ips)} "
                           f"controllers")
            return up
        self.missing = sorted(self.baseline - up)
        self._tell()
        return up

    # -- what it says -----------------------------------------------------
    def _tell(self):
        """Log only on a CHANGE. A line a second is not a warning."""
        state = tuple(self.missing)
        if state == self._told:
            return
        self._told = state
        if not self.missing:
            if self.sweeps > 1:
                self._note("every controller that was answering is answering "
                           "again")
        elif len(self.missing) == len(self.baseline):
            self._note(f"NOTHING on the rig is answering: all "
                       f"{len(self.baseline)} controllers went away at once. "
                       f"That is the network between this Mac and the rig, "
                       f"not the show.")
        else:
            self._note(f"{len(self.missing)} of {len(self.baseline)} "
                       f"controllers stopped answering: "
                       f"{', '.join(self.missing[:6])}"
                       + (" ..." if len(self.missing) > 6 else ""))

    def _note(self, msg):
        if self.log:
            try:
                self.log.event("rig", msg)
            except Exception:
                pass

    @property
    def all_gone(self):
        return bool(self.baseline) and len(self.missing) == len(self.baseline)

    @property
    def seconds_since_check(self):
        if self.checked_at is None:
            return None
        return time.monotonic() - self.checked_at

    # -- lifecycle --------------------------------------------------------
    def _loop(self):
        while self._running:
            try:
                self.sweep()
            except Exception as e:
                self._note(f"the rig check itself failed: {e}")
            # Sleep in slices so stop() does not wait a whole interval.
            waited = 0.0
            while self._running and waited < self.interval_s:
                time.sleep(0.25)
                waited += 0.25

    def start(self):
        if self._running:
            return self
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ltcplay-rigwatch")
        self._thread.start()
        return self

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
