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
    from ltcplay import cli
    return cli.main(argv)


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
