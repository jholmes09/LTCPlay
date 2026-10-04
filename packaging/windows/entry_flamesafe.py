"""flamesafe.exe: the flame safety program, in its own process.

Runs exactly `python -m flamesafe <config.json>`. Stop it with Ctrl-C or
Ctrl-Break (the supervisor sends Ctrl-Break), never End task: only a clean
stop sends the safe zeros (flamesafe/CONTRACT.md). This file never imports
ltcplay, and the built flamesafe.exe does not contain it.
"""
import sys


def _self_check():
    import importlib
    import os
    for mod in ("flamesafe.config", "flamesafe.service", "flamesafe.sacn",
                "flamesafe.composer", "flamesafe.arminput",
                "flamesafe.journal", "flamesafe.link", "flamesafe.rules"):
        importlib.import_module(mod)
    yield "flamesafe modules import"
    if "ltcplay" in sys.modules:
        raise RuntimeError("flamesafe.exe loaded ltcplay; it must not")
    yield "ltcplay is not loaded"
    import ltcwin
    from flamesafe import config
    ex = os.path.join(ltcwin.app_dir(), "flamesafe.example.json")
    if os.path.isfile(ex):
        config.load(ex)
        yield "the example config loads"
    if ltcwin.WINDOWS:
        got = ltcwin.keep_time()
        bad = [g for g in got if "NOT" in g or "could not" in g]
        if bad:
            raise RuntimeError("Windows timekeeping: " + "; ".join(bad))
        yield "Windows timekeeping: " + ", ".join(got)


def main():
    import ltcwin
    ltcwin.prepare_stdio()
    argv = sys.argv[1:]
    rc = ltcwin.common_flags("flamesafe", argv, _self_check)
    if rc is not None:
        return rc
    ltcwin.clean_stop_on_logoff()
    ltcwin.say_keep_time("flamesafe")
    from flamesafe.__main__ import main as flamesafe_main
    return flamesafe_main(argv)


if __name__ == "__main__":
    sys.exit(main())
