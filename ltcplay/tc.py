"""Timecode arithmetic, including drop frame.

Three numbers get confused constantly and this module keeps them apart:

  rate    real frames per second: 23.976, 24, 25, 29.97 or 30
  count   the number the frame digit counts to: 24, 25 or 30
  drop    whether two counts are skipped at the top of most minutes

29.97 and 30 share a count of 30, which is why a source and a show file can
disagree about the rate while every number on both looks correct.  0.1% is one
frame every 33 seconds, or nearly two seconds across a half hour set, and it
reads on the floor as the lights sliding behind the music toward the end.
Detecting that is the whole reason the run display prints a measured rate.
"""
import re

RATES = (23.976, 24.0, 25.0, 29.97, 30.0)
_SEP = re.compile(r"^(\d{1,2})[:;](\d{2})[:;](\d{2})([:;.])(\d{1,2})$")


def normalize_rate(v):
    """Accept 30, '30', 29.97, '29.97 df' and return one of RATES."""
    if isinstance(v, str):
        v = v.lower().replace("df", "").replace("ndf", "").replace("fps", "").strip()
    f = float(v)
    for r in RATES:
        if abs(f - r) < 0.01:
            return r
    raise ValueError(f"frame rate {v} is not one of {', '.join(str(r) for r in RATES)}")


def count_for(rate):
    """The frame digit's modulus. 23.976 counts to 24, 29.97 counts to 30."""
    return int(round(rate))


def rate_label(rate, drop=False):
    """Always spell out drop or non-drop at a 30 count.

    "30" beside "29.97 non-drop" invites the eye to treat the difference as
    the drop flag, which is the one thing it is not."""
    txt = f"{rate:g}"
    if count_for(rate) == 30:
        return f"{txt} {'drop' if drop else 'non-drop'}"
    return f"{txt} drop" if drop else txt


def tc_to_frames(h, m, s, f, count, drop=False):
    total = ((h * 60 + m) * 60 + s) * count + f
    if drop:
        mins = h * 60 + m
        total -= 2 * (mins - mins // 10)
    return total


def frames_to_tc(n, count, drop=False):
    """Inverse of tc_to_frames. Returns (h, m, s, f)."""
    n = int(n)
    neg = n < 0
    if neg:
        n = -n
    if drop:
        if count != 30:
            raise ValueError("drop frame is only defined for a 30 count")
        # 10 minutes of drop frame is exactly 17982 frames; 9 of those minutes
        # are 1798 frames and the first is a full 1800.
        d, rem = divmod(n, 17982)
        if rem < 2:
            rem = 2
        n = n + 18 * d + 2 * ((rem - 2) // 1798)
    f = n % count
    rest = n // count
    out = (rest // 3600, (rest // 60) % 60, rest % 60, f)
    return tuple(-x for x in out) if neg else out


def parse_tc(text, rate, drop=None):
    """'01:02:03:04' -> seconds. A ';' before the frames means drop frame."""
    rate = normalize_rate(rate)
    count = count_for(rate)
    m = _SEP.match(str(text).strip())
    if not m:
        raise ValueError(f"{text!r} is not a timecode like 01:00:00:00")
    h, mi, s, sep, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), \
        m.group(4), int(m.group(5))
    if drop is None:
        drop = sep == ";"
    if f >= count:
        raise ValueError(f"{text!r}: frame {f} does not exist at {rate:g} fps")
    if mi > 59 or s > 59 or h > 23:
        raise ValueError(f"{text!r}: out of range")
    if drop and count == 30 and s == 0 and mi % 10 and f < 2:
        raise ValueError(f"{text!r}: frame {f} is dropped at this minute in drop frame")
    return tc_to_frames(h, mi, s, f, count, drop) / rate


def format_tc(seconds, rate, drop=False):
    rate = normalize_rate(rate)
    count = count_for(rate)
    if seconds is None or seconds < 0:
        return "--:--:--:--"
    h, m, s, f = frames_to_tc(int(round(seconds * rate)), count, drop)
    return f"{h:02d}:{m:02d}:{s:02d}{';' if drop else ':'}{f:02d}"


def format_clock(seconds, sign=False):
    """Short mm:ss for a countdown or an elapsed span."""
    if seconds is None:
        return "--:--"
    neg = seconds < 0
    seconds = abs(seconds)
    h = int(seconds) // 3600
    m = (int(seconds) // 60) % 60
    s = int(seconds) % 60
    body = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    if sign:
        return ("-" if neg else "+") + body
    return ("-" + body) if neg else body

def format_seq(seconds, ms=True):
    """Position inside a sequence, the way xLights writes it: M:SS.mmm.

    This is deliberately NOT timecode. A note that says "the bat is wrong at
    01:04:12:15" is useless inside xLights, which knows nothing about the
    show clock; the same moment there is 2:20.433 into MonsterMash. One is
    for the timecode operator, the other for whoever opens the sequence."""
    if seconds is None:
        return "-:--"
    neg = seconds < 0
    seconds = abs(float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    out = f"{m}:{s:06.3f}" if ms else f"{m}:{int(s):02d}"
    return ("-" + out) if neg else out
