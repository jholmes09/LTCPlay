"""The bench build's test hooks, and the guard that keeps them off a show.

The soak test (packaging/windows/soak.py) switches a few hooks on through
environment variables in the programs it starts itself: a fake show audio
device, a virtual Stream Deck, stall and send gap probes, and the
tracemalloc diagnostic. Any of them left in a show PC's environment would
run a silent show with live flames, or ignore the real deck's Abort. So
(safety audit of bench-build, P1):

  * the LTC Player supervisor never passes any of them on
    (ltcwin.without_bench_hooks, the same list as here);
  * the engine refuses Run while one is set, unless the soak itself started
    this engine (run_refusal()). The soak says so with SOAK_ENV set to its
    own process id, which must be this engine's parent: a value left over
    in the environment names a process that is not.
"""
import os

PREFIX = "LTCPLAY_BENCH_"
SOAK_ENV = "LTCPLAY_BENCH_SOAK"
EXTRA = ("LTC_TRACEMALLOC",)

WHAT = {
    "LTCPLAY_BENCH_FAKE_AUDIO": "the show audio plays to a fake device, so "
                                "the show would be silent",
    "LTCPLAY_BENCH_VIRTUAL_DECK": "the Stream Deck program ignores the real "
                                  "deck, so its Abort key does nothing",
    "LTCPLAY_BENCH_STALLS": "a test probe adds load to the engine",
    "LTCPLAY_BENCH_SENDGAPS": "a test probe adds load to the engine",
    "LTC_TRACEMALLOC": "a memory diagnostic adds load to the engine",
}


def is_hook(name):
    n = str(name).upper()
    return n.startswith(PREFIX) or n in EXTRA


def active(env=None):
    """The names of the bench hooks switched on in `env`, sorted."""
    env = os.environ if env is None else env
    on = []
    for k, v in env.items():
        if not is_hook(k) or k.upper() == SOAK_ENV:
            continue
        if str(v).strip() not in ("", "0"):
            on.append(k.upper())
    return sorted(on)


def soak_started(env=None, ppid=None):
    env = os.environ if env is None else env
    ppid = os.getppid() if ppid is None else ppid
    mark = str(env.get(SOAK_ENV) or "").strip()
    return bool(mark) and mark == str(ppid)


def run_refusal(env=None, ppid=None):
    """None when Run may go ahead, else the sentence for the rack screen."""
    on = active(env)
    if not on or soak_started(env, ppid):
        return None
    why = "; ".join(f"{n} ({WHAT.get(n, 'a bench test setting')})"
                    for n in on)
    return (f"Run refused. This engine was started with bench test "
            f"settings that must never be on in a show: {why}. Remove "
            f"them from the Windows environment variables of the show "
            f"account, then quit and start LTC Player again. Nothing was "
            f"started.")
