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
    _bench_virtual_deck(streamdeck)
    _bench_text_cache(streamdeck)
    return streamdeck.main(argv)


def _bench_text_cache(streamdeck, size=256):
    """BENCH BUILD (show PC, 2026-10-04: the deck program grew about 14 MB
    an hour on Windows, steadily): each key's words are drawn once into a
    small transparent tile and pasted from then on, instead of being laid
    out and rasterised again five times a second. If the growth is in the
    font or text path, it stops; the soak's memory line shows which.
    LTCPLAY_DECK_TEXT_CACHE=0 turns it off for comparison."""
    import collections
    import os
    if os.environ.get("LTCPLAY_DECK_TEXT_CACHE", "1") == "0":
        print("BENCH: deck key text drawn every time (cache off)", flush=True)
        return
    real = streamdeck.Fonts.text_block
    cache = collections.OrderedDict()

    def text_block(self, d, box, lines, kind, fill, max_size, sp_ratio=0.06):
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        key = (x1 - x0, y1 - y0, tuple(lines), kind, tuple(fill)
               if isinstance(fill, (list, tuple)) else fill, max_size,
               sp_ratio)
        tile = cache.get(key)
        if tile is None:
            tile = self.Image.new("RGBA", (max(1, x1 - x0), max(1, y1 - y0)),
                                  (0, 0, 0, 0))
            real(self, self.ImageDraw.Draw(tile), (0, 0, x1 - x0, y1 - y0),
                 lines, kind, fill, max_size, sp_ratio)
            cache[key] = tile
            if len(cache) > size:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        d._image.paste(tile, (x0, y0), tile)
    streamdeck.Fonts.text_block = text_block
    print(f"BENCH: deck key text drawn once per face and reused (at most "
          f"{size} faces kept)", flush=True)


class _VirtualDeck:
    """BENCH BUILD ONLY: a Stream Deck with no hardware. No key is ever
    pressed on it and nothing is drawn; the program around it is the real
    one, so the screen's arm holds (remote presses of its keys) go through
    every rule the real deck has."""

    def set_key(self, Image, key, img):
        pass

    def keys_down(self):
        return []

    def close(self):
        pass


def _bench_virtual_deck(streamdeck):
    """When the soak test runs with no Stream Deck plugged in it sets
    LTCPLAY_BENCH_VIRTUAL_DECK, and the deck program then uses _VirtualDeck.
    Unset, which is always on a show PC, this does nothing at all."""
    import os
    if os.environ.get("LTCPLAY_BENCH_VIRTUAL_DECK") != "1":
        return
    real = streamdeck.run_forever

    def run_forever(controller, **kw):
        kw["deck_factory"] = _VirtualDeck
        return real(controller, **kw)
    streamdeck.run_forever = run_forever
    # Nothing is shown on a virtual deck, so a machine with none of the key
    # fonts (a Linux test box) draws the faces without their words.
    real_text = streamdeck.Fonts.text_block

    def text_block(self, *a, **kw):
        try:
            return real_text(self, *a, **kw)
        except Exception:
            return None
    streamdeck.Fonts.text_block = text_block
    print("BENCH: the Stream Deck program runs with a VIRTUAL deck "
          "(LTCPLAY_BENCH_VIRTUAL_DECK is set)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
