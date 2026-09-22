"""Whose tool this is.

Read from `ltcplay_brand.json` beside the launcher when it is there, so the
credit travels with a copy of the folder and can be changed without touching
code. Defaults are Jeff's, because this is his.
"""
import json
import os

FILENAME = "ltcplay_brand.json"

DEFAULT = {
    "name": "Jeff Holmes Presents",
    "product": "ltcplay",
    "tagline": "timecode show player",
    "email": "jeff.holmes@hey.com",
    "phone": "",
    "url": "",
    "logo": "brand/logo.png",
}


def folder():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path():
    return os.path.join(folder(), FILENAME)


def load():
    out = dict(DEFAULT)
    try:
        with open(path()) as fh:
            doc = json.load(fh)
    except (ValueError, OSError):
        return out
    if isinstance(doc, dict):
        for k in DEFAULT:
            v = doc.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    return out


def contact_line(b=None):
    """One line of credit, for a terminal banner or a log header."""
    b = b or load()
    bits = [b["name"]]
    if b.get("email"):
        bits.append(b["email"])
    if b.get("phone"):
        bits.append(b["phone"])
    if b.get("url"):
        bits.append(b["url"])
    return "  ".join(bits)
