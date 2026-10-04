"""Stream Deck Mini key-artwork demo for Jeff (Fire & Ice 2026), round 2.

A design demo only. It drives the real deck plugged into the Mac, and it is
connected to nothing else: no show, no flames, no lasers.

Two looks, switched by writing a word into sd_demo_cmd.txt:
  a   every key has its own ring of marquee dots, no solid outline; the chase
      runs around the outside of the whole deck, lighting the outer dots
  b   every key has a solid outline; the chase is a bright run travelling
      along the outline edges around the outside of the whole deck
Also: freeze (the safety program has stopped: nothing moves, keys do
nothing), run, quit. Words can be combined, e.g. "b freeze".

Keys (top row): START NOW, HOLD, ABORT.  Bottom row: three arm keys.
- START NOW starts a pretend show; it then reads NOW PLAYING, flashing green.
- HOLD during a show holds it; the key then reads RESUME. Press to resume.
- ABORT works only while a show is playing or held: hold it down for one
  second while the outside of the deck fills with solid red. Let go early and
  nothing happens. Once it fires, every group disarms, the border stays solid
  red, the key reads RESET (flashing), and no other key does anything until
  RESET is pressed.
- Each arm key steps on every press:
  OFF -> ARMED -> CYCLE ARM -> WAIT 3,2,1 -> ARMED -> SHOW LOST -> OFF.
"""
import io
import os
import time

import hid
from PIL import Image, ImageDraw, ImageFont

VID, PID = 0x0FD9, 0x0063
K = 80
COLS, ROWS = 3, 2
W, H = COLS * K, ROWS * K
CMD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sd_demo_cmd.txt")

BLACK = (7, 6, 5)
GOLD = (212, 168, 74)
CHAMPAGNE = (246, 227, 174)
BULB_OFF = (52, 41, 20)
OUTLINE_DIM = (92, 72, 32)
RED = (230, 30, 24)
DIM_TEXT = (72, 66, 58)
def _first_font(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


SERIF = _first_font([
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",   # macOS
    r"C:\Windows\Fonts\georgiab.ttf",                     # Windows
])

ABORT_HOLD_S = 0.5
RUN_FRACTION = 0.16      # share of the outside path lit by each snake


def sans_bold(size):
    for path in ("/System/Library/Fonts/Avenir Next Condensed.ttc",
                 "/System/Library/Fonts/HelveticaNeue.ttc"):
        if not os.path.exists(path):
            continue
        for idx in range(12):
            try:
                f = ImageFont.truetype(path, size, index=idx)
            except Exception:
                break
            name = " ".join(f.getname()).lower()
            if "bold" in name and "italic" not in name and "ultra" not in name:
                return f
    for path in (r"C:\Windows\Fonts\bahnschrift.ttf",      # Windows: condensed-capable
                 r"C:\Windows\Fonts\arialnb.ttf",          # Arial Narrow Bold
                 r"C:\Windows\Fonts\arialbd.ttf"):
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            if path.endswith("bahnschrift.ttf"):
                try:
                    f.set_variation_by_name("Bold SemiCondensed")
                except Exception:
                    pass
            return f
    return ImageFont.truetype(SERIF, size)


_fonts = {}


def font(kind, size):
    if (kind, size) not in _fonts:
        _fonts[(kind, size)] = (ImageFont.truetype(SERIF, size) if kind == "serif"
                                else sans_bold(size))
    return _fonts[(kind, size)]


def spaced_width(d, text, f, sp):
    return sum(d.textlength(c, font=f) for c in text) + sp * max(0, len(text) - 1)


def text_block(d, box, lines, kind, fill, max_size, sp_ratio=0.06):
    """One or two lines, as large as fits, centred on the letters' own ink
    (cap height and actual left/right edges), not on the font's line box."""
    x0, y0, x1, y1 = box
    size = max_size
    while size > 8:
        f = font(kind, size)
        sp = size * sp_ratio
        cap = f.getbbox("H")
        cap_h = cap[3] - cap[1]
        pitch = cap_h * 1.4
        total_h = cap_h + pitch * (len(lines) - 1)
        if all(spaced_width(d, ln, f, sp) <= (x1 - x0) - 2 for ln in lines) \
                and total_h <= (y1 - y0) - 4:
            break
        size -= 1
    f = font(kind, size)
    sp = size * sp_ratio
    cap = f.getbbox("H")
    cap_h = cap[3] - cap[1]
    pitch = cap_h * 1.4
    total_h = cap_h + pitch * (len(lines) - 1)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for i, ln in enumerate(lines):
        w = spaced_width(d, ln, f, sp)
        lead = f.getbbox(ln[0])[0]                                  # left bearing
        trail = d.textlength(ln[-1], font=f) - f.getbbox(ln[-1])[2]  # right bearing
        ink_w = w - lead - trail
        x = cx - ink_w / 2 - lead
        cap_top = cy - total_h / 2 + pitch * i
        y = cap_top - cap[1]
        for c in ln:
            d.text((x, y), c, font=f, fill=fill)
            x += d.textlength(c, font=f) + sp


# ------------------------------------------------------------------ geometry
def key_origin(k):
    return (k % COLS) * K, (k // COLS) * K


def outer_path(inset):
    """Points every 1 px along the outside of the deck, clockwise, at `inset`
    from the deck edge: top edge, right edge, bottom edge, left edge."""
    x0, y0, x1, y1 = inset, inset, W - inset, H - inset
    pts = [(x, y0) for x in range(x0, x1)]
    pts += [(x1, y) for y in range(y0, y1)]
    pts += [(x, y1) for x in range(x1, x0, -1)]
    pts += [(x0, y) for y in range(y1, y0, -1)]
    return pts


def in_gap(x, y, inset):
    """True for points of the outside path that fall in the physical gap
    between two keys (they cannot be shown, so they are skipped)."""
    return ((x % K) < inset or (x % K) > K - inset) and x not in (inset, W - inset) \
        or ((y % K) < inset or (y % K) > K - inset) and y not in (inset, H - inset)


# Look A: a ring of dots round every key
DOT_INSET, DOT_SPACING, DOT_R = 6, 11.0, 2.6


def key_ring(k):
    ox, oy = key_origin(k)
    x0, y0, x1, y1 = ox + DOT_INSET, oy + DOT_INSET, ox + K - DOT_INSET, oy + K - DOT_INSET
    side = x1 - x0
    n = max(1, round(side / DOT_SPACING))
    pts = []
    for i in range(n):
        t = x0 + side * i / n
        pts += [(t, y0)]
    for i in range(n):
        pts += [(x1, y0 + side * i / n)]
    for i in range(n):
        pts += [(x1 - side * i / n, y1)]
    for i in range(n):
        pts += [(x0, y1 - side * i / n)]
    return pts


def outer_dots():
    """The dots that lie on the outside of the deck, in clockwise order."""
    dots = []
    for k in range(COLS * ROWS):
        for (x, y) in key_ring(k):
            on_edge = (abs(y - DOT_INSET) < 0.5 or abs(y - (H - DOT_INSET)) < 0.5
                       or abs(x - DOT_INSET) < 0.5 or abs(x - (W - DOT_INSET)) < 0.5)
            if on_edge:
                dots.append((x, y))
    cx, cy = W / 2, H / 2

    def along(p):          # position along the clockwise outside path
        x, y = p
        if abs(y - DOT_INSET) < 0.5 and x < W - DOT_INSET - 0.5:
            return x
        if abs(x - (W - DOT_INSET)) < 0.5 and y < H - DOT_INSET - 0.5:
            return W + y
        if abs(y - (H - DOT_INSET)) < 0.5 and x > DOT_INSET + 0.5:
            return W + H + (W - x)
        return 2 * W + H + (H - y)
    return sorted(set(dots), key=along)


OUTER_DOTS = outer_dots()
ALL_DOTS = sorted({p for k in range(6) for p in key_ring(k)})


def _clockwise(p):
    import math
    x, y = p
    return (math.atan2(y - H / 2, x - W / 2) + math.pi / 2) % (2 * math.pi)


SWEEP_DOTS = sorted(ALL_DOTS, key=_clockwise)   # every dot, clockwise from 12 o'clock

# Look B: a solid outline round every key
LINE_INSET, LINE_W = 4, 3
PATH_B = [p for p in outer_path(LINE_INSET) if not in_gap(p[0], p[1], LINE_INSET)]


def face_box(k, look):
    ox, oy = key_origin(k)
    m = 12 if look == "a" else 8
    return (ox + m, oy + m, ox + K - m, oy + K - m)


# ---------------------------------------------------------------- drawing
def draw_frame(d, look, chase, abort_frac):
    if look == "a":
        n = len(OUTER_DOTS)
        run = max(1, round(n * RUN_FRACTION))
        lit = {}
        if abort_frac > 0:
            # Abort fills every key's own ring with red, all keys at once,
            # each ring going round clockwise
            for k in range(COLS * ROWS):
                ring = key_ring(k)
                for p in ring[:round(abort_frac * len(ring))]:
                    lit[p] = RED
        else:
            # two snakes chasing the same way, half a lap apart
            for head in (chase, chase + n // 2):
                for j in range(run):
                    lit[OUTER_DOTS[(head - j) % n]] = CHAMPAGNE if j == 0 else GOLD
        for p in ALL_DOTS:
            x, y = p
            c = lit.get(p, BULB_OFF)
            d.ellipse((x - DOT_R, y - DOT_R, x + DOT_R, y + DOT_R), fill=c)
    else:
        for k in range(6):
            ox, oy = key_origin(k)
            d.rounded_rectangle((ox + LINE_INSET, oy + LINE_INSET,
                                 ox + K - LINE_INSET, oy + K - LINE_INSET),
                                radius=7, outline=OUTLINE_DIM, width=LINE_W)
        n = len(PATH_B)
        if abort_frac > 0:
            seg = PATH_B[:round(abort_frac * n)]
            colour = RED
        else:
            run = round(n * RUN_FRACTION)
            start = (chase * 4) % n
            seg = [PATH_B[(start - j) % n] for j in range(run)]
            colour = GOLD
        for (x, y) in seg:
            d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=colour)


def show_key(d, box, lines, text, bg=None, kind="serif", max_size=24):
    if bg:
        d.rounded_rectangle(box, radius=5, fill=bg)
    text_block(d, (box[0] + 2, box[1] + 2, box[2] - 2, box[3] - 2), lines, kind, text, max_size)


GROUPS = ["LEFT", "MIDDLE", "RIGHT"]
ARM_CYCLE = ["OFF", "ARMED", "CYCLE", "WAIT", "ARMED2", "SHOW LOST"]


def arm_key(d, box, group, state, t, wait_left, grey=False):
    x0, y0, x1, y1 = box
    bar = y0 + 17
    text_block(d, (x0, y0 - 1, x1, bar), [group], "sans", DIM_TEXT if grey else CHAMPAGNE, 16)
    body = (x0, bar + 1, x1, y1)
    if grey:
        d.rounded_rectangle(body, radius=4, fill=(26, 24, 21))
        text_block(d, body, ["OFF"], "sans", DIM_TEXT, 24)
    elif state == "OFF":
        d.rounded_rectangle(body, radius=4, fill=(44, 36, 24), outline=(120, 96, 50), width=1)
        text_block(d, body, ["OFF"], "sans", CHAMPAGNE, 24)
    elif state in ("ARMED", "ARMED2"):
        d.rounded_rectangle(body, radius=4, fill=(40, 190, 90))
        text_block(d, body, ["ARMED"], "sans", (6, 30, 12), 22)
    elif state == "CYCLE":
        on = int(t * 2.5) % 2 == 0
        d.rounded_rectangle(body, radius=4, fill=(245, 160, 30) if on else (70, 44, 8))
        text_block(d, body, ["CYCLE", "ARM"], "sans", (40, 20, 0) if on else (245, 160, 30), 20)
    elif state == "WAIT":
        d.rounded_rectangle(body, radius=4, fill=(245, 160, 30))
        text_block(d, body, [str(wait_left)], "sans", (40, 20, 0), 36)
    elif state == "SHOW LOST":
        d.rounded_rectangle(body, radius=4, fill=(44, 36, 24), outline=(120, 96, 50), width=1)
        text_block(d, body, ["SHOW", "LOST"], "sans", CHAMPAGNE, 20)


# ----------------------------------------------------------------- device
def to_native(img):
    img = img.rotate(90).transpose(Image.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        img.save(buf, "BMP")
        return buf.getvalue()


class Deck:
    def __init__(self):
        self.h = hid.device()
        self.h.open(VID, PID)
        self.h.set_nonblocking(1)
        self.h.send_feature_report([0x0B, 0x63] + [0] * 15)
        self.h.send_feature_report([0x05, 0x55, 0xAA, 0xD1, 0x01, 80] + [0] * 11)
        self.last = {}

    def set_key(self, key, img):
        data = to_native(img)
        if self.last.get(key) == data:
            return
        self.last[key] = data
        step = 1024 - 16
        page = sent = 0
        while sent < len(data):
            chunk = data[sent:sent + step]
            last = 1 if sent + len(chunk) >= len(data) else 0
            pkt = bytes([0x02, 0x01, page, 0, last, key + 1] + [0] * 10) + chunk
            self.h.write(pkt + bytes(1024 - len(pkt)))
            sent += len(chunk)
            page += 1

    def keys_down(self):
        latest = None
        while True:
            r = self.h.read(7)
            if not r:
                return latest
            latest = [bool(v) for v in r[1:7]]

    def close(self):
        black = Image.new("RGB", (K, K), (0, 0, 0))
        self.last.clear()
        for k in range(6):
            self.set_key(k, black)
        self.h.close()


def main():
    print("Stream Deck design demo: write a, b, freeze, run or quit into", CMD)
    deck = Deck()
    show, idle_held = "idle", False
    arm = ["OFF"] * 3
    wait_until = [0.0] * 3
    abort_down_at, abort_flash_until = None, 0.0
    latched = False          # after an Abort: everything dead until Reset
    prev = [False] * 6
    chase, last_step = 0, time.monotonic()
    try:
        while True:
            now = time.monotonic()
            try:
                words = open(CMD).read().split()
            except OSError:
                words = []
            if "quit" in words:
                break
            look = "b" if "b" in words else "a"
            frozen = "freeze" in words

            down = deck.keys_down()
            if frozen:
                # The safety program has stopped: nothing on the deck changes,
                # and presses do nothing.
                if down is not None:
                    prev = down
                time.sleep(0.05)
                continue

            if now - last_step >= 0.06:
                chase += 1
                last_step = now

            if down is not None and latched:
                # Aborted: only RESET does anything.
                if down[2] and not prev[2]:
                    latched = False
                prev = down
                down = None
            if down is not None:
                for k in range(6):
                    if down[k] and not prev[k]:
                        if k == 0 and show == "idle" and not idle_held:
                            show = "playing"
                        elif k == 1:
                            if show == "playing":
                                show = "held"
                            elif show == "held":
                                show = "playing"
                            else:
                                idle_held = not idle_held
                        elif k == 2 and show in ("playing", "held"):
                            abort_down_at = now
                        elif k >= 3:
                            g = k - 3
                            i = ARM_CYCLE.index(arm[g])
                            arm[g] = ARM_CYCLE[(i + 1) % len(ARM_CYCLE)]
                            if arm[g] == "WAIT":
                                wait_until[g] = now + 3.0
                    if k == 2 and prev[k] and not down[k]:
                        abort_down_at = None
                prev = down

            abort_frac = 0.0
            if abort_down_at is not None:
                abort_frac = min(1.0, (now - abort_down_at) / ABORT_HOLD_S)
                if abort_frac >= 1.0:
                    show, idle_held, abort_down_at = "idle", False, None
                    arm = ["OFF"] * 3          # Abort disarms every group
                    latched = True
            if latched:
                abort_frac = 1.0               # the border stays fully red
            for g in range(3):
                if arm[g] == "WAIT" and now >= wait_until[g]:
                    arm[g] = "ARMED2"

            canvas = Image.new("RGB", (W, H), BLACK)
            d = ImageDraw.Draw(canvas)
            draw_frame(d, look, chase, abort_frac)
            b = face_box(0, look)
            if latched:
                show_key(d, b, ["START", "NOW"], DIM_TEXT)
            elif show == "playing":
                on = int(now * 2) % 2 == 0
                show_key(d, b, ["NOW", "PLAYING"], (4, 28, 10) if on else (60, 200, 100),
                         bg=(50, 205, 95) if on else (8, 40, 16), kind="sans")
            elif show == "held":
                show_key(d, b, ["PAUSED"], CHAMPAGNE, bg=(44, 36, 24), kind="sans")
            else:
                show_key(d, b, ["START", "NOW"], DIM_TEXT if idle_held else CHAMPAGNE)
            b = face_box(1, look)
            if latched:
                show_key(d, b, ["HOLD"], DIM_TEXT, max_size=28)
            elif show == "held" or idle_held:
                on = int(now * 2) % 2 == 0
                show_key(d, b, ["RESUME"], BLACK if on else GOLD, bg=GOLD if on else None)
            else:
                show_key(d, b, ["HOLD"], CHAMPAGNE, max_size=28)
            b = face_box(2, look)
            if latched:
                on = int(now * 2) % 2 == 0
                show_key(d, b, ["RESET"], BLACK if on else RED, bg=RED if on else None)
            else:
                show_key(d, b, ["ABORT"], RED if show in ("playing", "held") else DIM_TEXT)
            for g in range(3):
                left = max(1, int(wait_until[g] - now) + 1)
                arm_key(d, face_box(3 + g, look), GROUPS[g], arm[g], now, left, grey=latched)
            for k in range(6):
                ox, oy = key_origin(k)
                deck.set_key(k, canvas.crop((ox, oy, ox + K, oy + K)))
            time.sleep(0.02)
    finally:
        deck.close()


if __name__ == "__main__":
    main()
