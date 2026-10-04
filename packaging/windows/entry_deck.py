"""ltcplay-deck.exe: the Stream Deck program (`ltc deck`), in its own process.

Takes what `ltc deck` takes: --flamesafe-config and --ltcplay-url.
"""
import signal
import sys


def _self_check():
    from ltcplay import streamdeck
    yield "ltcplay.streamdeck imports"
    import hid
    n = len(hid.enumerate())
    yield f"hidapi loads, {n} USB HID device(s) visible"
    import PIL
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (72, 72))
    ImageDraw.Draw(img).rectangle((4, 4, 68, 68), outline=(255, 0, 0))
    yield f"Pillow {PIL.__version__} draws"
    streamdeck.Fonts()
    yield "key fonts load"


def main():
    import ltcwin
    ltcwin.prepare_stdio()
    argv = sys.argv[1:]
    rc = ltcwin.common_flags("ltcplay-deck", argv, _self_check)
    if rc is not None:
        return rc
    # The deck stops cleanly on KeyboardInterrupt. Ctrl-Break (what the
    # supervisor sends) has no handler of its own, so it is made to mean
    # the same thing.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    ltcwin.clean_stop_on_logoff()
    # The arm link's keepalive must not be throttled either; the deck
    # keeps its normal priority (it is not the frame sender).
    ltcwin.say_keep_time("ltcplay-deck", above_normal=False)
    from ltcplay import streamdeck
    return streamdeck.main(argv)


if __name__ == "__main__":
    sys.exit(main())
