#!/usr/bin/env python3
"""Make the web pages' icons from the LTC Player app icon.

The browser tab, the iPad home screen and an installed web app all show the
same picture as the Mac and Windows app: the drawings in
"LTC Player.iconset" (Jeff chose it, 2026-10-04). Nothing is redrawn. Each
size comes from the iconset's own drawing where it has one (16 and 32 px are
simplified so "LTC" stays readable) and is shrunk from the next larger
drawing where it does not.

Writes into ltcplay/web/icons/:
  favicon.ico            16, 32 and 48 px, PNG entries
  apple-touch-icon.png   180 px, on the page's near black (iOS fills
                         transparent corners with black anyway)
  icon-192.png           192 px, for the web app manifest
  icon-512.png           512 px, for the web app manifest
  manifest.webmanifest   name, colours and the two icons above

Run it again after the iconset changes, and commit what it writes:
  python3 tools/make_web_icons.py
Needs Pillow (the Stream Deck program already uses it).
"""
import io
import json
import os
import struct
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONSET = os.path.join(ROOT, "LTC Player.iconset")
OUT = os.path.join(ROOT, "ltcplay", "web", "icons")

# The pages' own palette (ltcplay/web/remote.html :root --bg).
BG = "#070605"

# Each output size, from the iconset drawing it is made from.
SOURCE = {16: "icon_16x16.png", 32: "icon_32x32.png",
          48: "icon_32x32@2x.png", 180: "icon_256x256.png",
          192: "icon_256x256.png", 512: "icon_512x512.png"}


def drawing(size):
    im = Image.open(os.path.join(ICONSET, SOURCE[size])).convert("RGBA")
    if im.size != (size, size):
        im = im.resize((size, size), Image.LANCZOS)
    return im


def png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def ico(sizes):
    """A .ico of PNG entries, written by hand as packaging/windows/
    ltcplay.spec does, so each size keeps its own drawing."""
    entries = [(s, png(drawing(s))) for s in sizes]
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for s, data in entries:
        out.write(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                              len(data), offset))
        offset += len(data)
    for _, data in entries:
        out.write(data)
    return out.getvalue()


def on_bg(im):
    tile = Image.new("RGBA", im.size, BG)
    tile.alpha_composite(im)
    return tile.convert("RGB")


MANIFEST = {
    "name": "LTC Player",
    "short_name": "LTC Player",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "theme_color": BG,
    "background_color": BG,
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}


def main():
    os.makedirs(OUT, exist_ok=True)
    files = {
        "favicon.ico": ico((16, 32, 48)),
        "apple-touch-icon.png": png(on_bg(drawing(180))),
        "icon-192.png": png(drawing(192)),
        "icon-512.png": png(drawing(512)),
        "manifest.webmanifest": (json.dumps(MANIFEST, indent=2)
                                 + "\n").encode(),
    }
    for name, data in files.items():
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(data)
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
