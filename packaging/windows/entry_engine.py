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


if __name__ == "__main__":
    sys.exit(main())
