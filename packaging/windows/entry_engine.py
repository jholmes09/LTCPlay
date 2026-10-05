"""ltcplay.exe: the show engine (`ltc serve` and every other ltc command).

The supervisor runs it as `ltcplay.exe serve --folder <show folder> --port
7878 --no-browser`. Run by hand it takes exactly what `./ltc` takes on a Mac.
"""
import multiprocessing
import sys


def _self_check():
    import importlib
    import os
    for mod in ("ltcplay.cli", "ltcplay.web", "ltcplay.showaudio",
                "ltcplay.flamelink", "ltcplay.conductor", "ltcplay.output",
                "ltcplay.schedule_service", "ltcplay.announce",
                "ltcplay.fseq", "ltcplay.clock"):
        importlib.import_module(mod)
    yield "ltcplay modules import"
    import numpy
    yield f"numpy {numpy.__version__}"
    import zstandard
    yield f"zstandard {zstandard.__version__}"
    import sounddevice
    n = len(sounddevice.query_devices())
    yield (f"sounddevice {sounddevice.__version__}, "
           f"{sounddevice.get_portaudio_version()[1]}, {n} audio device(s)")
    from zoneinfo import ZoneInfo
    ZoneInfo("America/New_York")
    yield "time zones (tzdata)"
    from ltcplay import web, brand, version
    page = os.path.join(web.HERE, "web", "index.html")
    if not os.path.isfile(page):
        raise RuntimeError(f"the show page is missing: {page}")
    yield "show page present"
    yield f"brand: {brand.load()['product']}"
    yield f"version: {version.status()}"
    import ltcwin
    if ltcwin.frozen():
        inside = os.path.normcase(os.path.abspath(ltcwin.internal_dir()))
        outside = [p for p in sys.path if p and not os.path.normcase(
            os.path.abspath(p)).startswith(inside)]
        if outside:
            raise RuntimeError(f"Python looks outside the app: {outside}")
        yield "self-contained: Python looks only inside the app"
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=ltcwin.spawn_probe)
    p.start()
    p.join(60)
    if p.exitcode != 0:
        raise RuntimeError(f"a spawned process exited with {p.exitcode}")
    yield "a spawned process (how the show audio runs) starts and exits"
    if ltcwin.WINDOWS:
        got = ltcwin.keep_time()
        bad = [g for g in got if "NOT" in g or "could not" in g]
        if bad:
            raise RuntimeError("Windows timekeeping: " + "; ".join(bad))
        yield "Windows timekeeping: " + ", ".join(got)


def main():
    multiprocessing.freeze_support()
    import ltcwin
    ltcwin.prepare_stdio()
    argv = sys.argv[1:]
    rc = ltcwin.common_flags("ltcplay", argv, _self_check)
    if rc is not None:
        return rc
    ltcwin.clean_stop_on_logoff()
    _bench_fake_audio()
    _bench_send_gaps()
    _bench_stall_probe()
    if argv and argv[0] == "serve":
        ltcwin.say_keep_time("ltcplay")
        if ltcwin.boosted():
            # The flame link's sender at Highest. The show audio's own
            # process raises itself (showaudio.child_main).
            ltcwin.boost_threads(
                ("ltcplay-flame-link",),
                log=lambda t: print(f"ltcplay: {t}", flush=True))
    from ltcplay import cli
    return cli.main(argv)


def _bench_send_gaps():
    """BENCH BUILD ONLY: with LTCPLAY_BENCH_SENDGAPS set to a file path (the
    soak sets it), the engine measures its own output timing where it
    sends: the worst interval between two pixel frames and between two
    timecode packets, per minute, written to that file every 5 s. The soak
    lays it beside its own view, so a starved soak is never mistaken for a
    starved engine (show PC, 2026-10-04). Unset, it does nothing at all."""
    import json
    import os
    import threading
    import time
    path = os.environ.get("LTCPLAY_BENCH_SENDGAPS")
    if not path:
        return
    from ltcplay import clock, output
    meters = {"pixels": {}, "timecode": {}}
    events = []     # (wall time, kind, ms): each interval of 100 ms or more
    last = {}

    def note(kind):
        now = time.perf_counter()
        prev, last[kind] = last.get(kind), now
        if prev is None or now - prev > 5.0:
            return
        m = int(time.time() // 60)
        d = meters[kind]
        ms = (now - prev) * 1000.0
        if ms > d.get(m, 0.0):
            d[m] = ms
        if ms >= 100.0 and len(events) < 500:
            events.append((round(time.time(), 3), kind, round(ms, 1)))
    real_frame = output.Sender.send_frame
    real_tc = clock.TimecodeOut.send

    def send_frame(self, channels):
        try:
            return real_frame(self, channels)
        finally:
            note("pixels")

    def send(self, pkt):
        try:
            return real_tc(self, pkt)
        finally:
            note("timecode")
    output.Sender.send_frame = send_frame
    clock.TimecodeOut.send = send

    def writer():
        while True:
            time.sleep(5.0)
            try:
                doc = {k: {str(m): round(v, 1) for m, v in d.items()}
                       for k, d in meters.items()}
                doc["events"] = list(events)
                with open(path + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
                os.replace(path + ".tmp", path)
            except Exception:
                pass
    threading.Thread(target=writer, daemon=True,
                     name="bench-sendgaps").start()
    print(f"BENCH: the engine times its own sends into {path}", flush=True)


def _bench_stall_probe():
    """BENCH BUILD ONLY: with LTCPLAY_BENCH_STALLS set to a file path (the
    soak sets it), bench_probe's stall probe runs in the engine: what held
    the engine up in each stall (its own thread, paging, or no CPU), and
    each minute's CPU per engine thread. Unset, it does nothing at all."""
    import os
    path = os.environ.get("LTCPLAY_BENCH_STALLS")
    if not path:
        return
    import bench_probe
    import ltcwin
    raise_to = ((lambda nid: ltcwin.thread_priority(nid, 2))
                if ltcwin.boosted() else None)
    bench_probe.StallProbe(path).start(raise_to)
    print(f"BENCH: the engine's stall probe writes to {path}"
          + (" (the probe at Highest)" if raise_to else ""), flush=True)


def _bench_fake_audio():
    """BENCH BUILD ONLY (branch bench-build, never merged): when the soak
    test runs on a machine with no audio interface (a CI runner) it sets
    LTCPLAY_BENCH_FAKE_AUDIO to the show file's device name, and the show
    audio then plays to the test suite's stand-in device
    (showaudio.FakeSoundDevice). Unset, which is always on a show PC, this
    does nothing at all."""
    import os
    name = os.environ.get("LTCPLAY_BENCH_FAKE_AUDIO")
    if not name:
        return
    from ltcplay import showaudio
    real = showaudio.engine_spec

    def fake_spec(acfg, checked, fake=None, platform=None):
        fake = {"devices": [{"name": acfg.device, "hostapi": 0,
                             "max_output_channels": max(2, acfg.channels)}],
                "hostapis": ["ASIO" if sys.platform == "win32"
                             else "Core Audio"]}
        return real(acfg, checked, fake=fake, platform=platform)
    showaudio.engine_spec = fake_spec
    print(f"BENCH: the show audio plays to a FAKE stand-in device named "
          f"{name!r} (LTCPLAY_BENCH_FAKE_AUDIO is set)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
