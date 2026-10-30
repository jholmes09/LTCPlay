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
    _pacing_self_test()
    yield "the bench deck pacing line adds up"


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
    _bench_deck_pacing(streamdeck)
    return streamdeck.main(argv)


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


class DeckPacing:
    """BENCH BUILD ONLY (show PC, 2026-10-05: Jeff saw the deck's animation
    run slow during MadMapper's cold start): how the deck's main loop is
    keeping pace, one line a minute in deck.log. A pass of the loop is one
    Controller.tick() (the arm link's 20 Hz); a frame is one
    Controller.draw() and the key images written after it. Since look A
    (deck-look-a) the marquee steps on flamesafe's status heartbeat, not on
    passes: frames per second is how smoothly it moves (about 17 when
    healthy, one per 0.06 s step), and it stops when flamesafe does.

    Per minute: passes and frames per second, the worst pass interval, and
    per frame the drawing time, the key image encoding time, and the USB
    (HID) write time with how many keys actually changed."""

    def __init__(self, clock, out=print):
        self.clock = clock
        self.out = out
        self._reset(None)
        self.last_tick = None
        self.frame = None           # the frame being written

    def _reset(self, minute):
        self.minute = minute
        self.passes = 0
        self.worst_pass = 0.0
        self.frames = []            # (draw, encode, hid, keys) seconds

    def _end_frame(self):
        if self.frame is not None:
            self.frames.append(tuple(self.frame))
            self.frame = None

    def tick(self):
        now = self.clock()
        if self.last_tick is not None:
            self.worst_pass = max(self.worst_pass, now - self.last_tick)
        self.last_tick = now
        self.passes += 1
        self._end_frame()

    def drew(self, seconds):
        self._end_frame()
        self.frame = [seconds, 0.0, 0.0, 0]

    def wrote(self, encode_s, total_s, changed):
        if self.frame is None:
            self.frame = [0.0, 0.0, 0.0, 0]
        self.frame[1] += encode_s
        self.frame[2] += max(0.0, total_s - encode_s)
        self.frame[3] += 1 if changed else 0

    def line(self, span_s):
        """One sentence for the minute just gone, then a fresh count."""
        self._end_frame()
        fr = self.frames
        n = len(fr)

        def ms(vals, f=max):
            return f(vals) * 1000.0 if vals else 0.0
        mean = (lambda v: sum(v) / len(v))
        hid = [f[2] for f in fr]
        text = (f"deck pacing: {self.passes / span_s:.1f} passes/s (the "
                f"arm link's pace; target 20), worst pass "
                f"{self.worst_pass * 1000:.0f} ms; {n / span_s:.1f} frames "
                f"drawn/s"
                + (f"; per frame: drawing {ms([f[0] for f in fr], mean):.1f} "
                   f"ms (worst {ms([f[0] for f in fr]):.0f}), key encoding "
                   f"{ms([f[1] for f in fr], mean):.1f} ms, USB writes "
                   f"{ms(hid, mean):.1f} ms (worst {ms(hid):.0f}) for "
                   f"{mean([f[3] for f in fr]):.1f} changed key(s)"
                   if n else ""))
        self._reset(None)
        return text


def _bench_deck_pacing(streamdeck, every_s=60.0):
    """Wraps the deck program's draw, key writes and pass (bench only:
    timing and logging, nothing about what is drawn or when) and prints
    DeckPacing's line every minute."""
    import threading
    import time
    pace = DeckPacing(time.perf_counter)
    lock = threading.Lock()
    real_tick = streamdeck.Controller.tick
    real_draw = streamdeck.Controller.draw
    real_set = streamdeck.Deck.set_key
    real_native = streamdeck.to_native
    enc = threading.local()

    def tick(self, *a, **kw):
        with lock:
            pace.tick()
        return real_tick(self, *a, **kw)

    def draw(self, *a, **kw):
        t = time.perf_counter()
        try:
            return real_draw(self, *a, **kw)
        finally:
            d = time.perf_counter() - t
            with lock:
                pace.drew(d)

    def to_native(*a, **kw):
        t = time.perf_counter()
        try:
            return real_native(*a, **kw)
        finally:
            enc.s = getattr(enc, "s", 0.0) + time.perf_counter() - t

    def set_key(self, Image, key, img):
        before = self.last.get(key)
        enc.s = 0.0
        t = time.perf_counter()
        try:
            return real_set(self, Image, key, img)
        finally:
            total = time.perf_counter() - t
            with lock:
                pace.wrote(enc.s, total, self.last.get(key) is not before)
    streamdeck.Controller.tick = tick
    streamdeck.Controller.draw = draw
    streamdeck.Deck.set_key = set_key
    streamdeck.to_native = to_native

    def logger():
        last = time.perf_counter()
        while True:
            time.sleep(every_s)
            now = time.perf_counter()
            with lock:
                text = pace.line(now - last)
            last = now
            print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, flush=True)
    threading.Thread(target=logger, daemon=True,
                     name="bench-deck-pacing").start()


def _pacing_self_test():
    t = [0.0]
    lines = []
    p = DeckPacing(lambda: t[0], lines.append)
    for i in range(20):
        p.tick()
        p.drew(0.008)
        p.wrote(0.002, 0.012, True)
        t[0] += 0.05
    text = p.line(1.0)
    assert "20.0 passes/s" in text and "20.0 frames" in text and \
        "USB writes 10.0 ms" in text and "1.0 changed" in text, text
    return text


if __name__ == "__main__":
    sys.exit(main())
