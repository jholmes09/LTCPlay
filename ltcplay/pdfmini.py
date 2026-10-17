"""A very small PDF writer: text, rules and page numbers, nothing else.

For the checklist records (checklist.py): one readable page set per day,
made on the show PC with no browser and no library beyond the standard
one, so it bundles into the Windows installer as it is. Helvetica and
Helvetica-Bold (the PDF standard fonts, WinAnsi), letter pages, content
streams left uncompressed so extract_text() below, and any auditor's
tool, can read the words straight back.
"""
import re

LETTER = (612.0, 792.0)
MARGIN = 54.0
FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold"}
# Helvetica's advance widths for the printable ASCII range, in thousandths
# of the point size (the AFM values), so wrapping and right alignment land
# where they should. Anything outside the range counts as a wide letter.
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
      278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
      584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
      500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
      667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
      278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
      278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def width(text, size):
    """The width of `text` at `size` points in Helvetica."""
    total = 0
    for ch in text:
        o = ord(ch)
        total += _W[o - 32] if 32 <= o <= 126 else 700
    return total * size / 1000.0


def _ascii(text):
    """PDF string text in WinAnsi: anything the font cannot show is '?'."""
    out = []
    for ch in str(text):
        o = ord(ch)
        if ch in "\\()":
            out.append("\\" + ch)
        elif 32 <= o <= 126:
            out.append(ch)
        elif ch == "\n" or ch == "\t":
            out.append(" ")
        elif 160 <= o <= 255:
            out.append("\\%03o" % o)
        else:
            out.append("?")
    return "".join(out)


def wrap(text, size, max_width):
    """Lines of `text` that fit `max_width` at `size`, word by word; a word
    longer than the line is cut."""
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        while width(w, size) > max_width and len(w) > 1:
            cut = max(1, int(len(w) * max_width / width(w, size)) - 1)
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(w[:cut])
            w = w[cut:]
        trial = (cur + " " + w) if cur else w
        if width(trial, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


class Page:
    def __init__(self):
        self.ops = []

    def text(self, x, y, size, text, bold=False, grey=0.0):
        f = "F2" if bold else "F1"
        g = f"{grey:.2f} g " if grey else "0 g "
        self.ops.append(f"BT {g}/{f} {size:g} Tf {x:.1f} {y:.1f} Td "
                        f"({_ascii(text)}) Tj ET")

    def text_right(self, x_right, y, size, text, bold=False, grey=0.0):
        self.text(x_right - width(str(text), size), y, size, text, bold, grey)

    def rule(self, x1, y1, x2, y2, grey=0.6, w=0.6):
        self.ops.append(f"{grey:.2f} G {w:g} w {x1:.1f} {y1:.1f} m "
                        f"{x2:.1f} {y2:.1f} l S")

    def stream(self):
        return "\n".join(self.ops).encode("latin-1")


class Document:
    """Pages are added with new_page(); build() numbers them."""

    def __init__(self, title="", author="LTC Player", size=LETTER):
        self.title = title
        self.author = author
        self.size = size
        self.pages = []

    def new_page(self):
        p = Page()
        self.pages.append(p)
        return p

    def build(self, footer=None):
        """The PDF bytes. `footer(page, n, total)` draws each page's foot;
        the default prints "Page n of total"."""
        w, h = self.size
        total = len(self.pages) or 1
        if not self.pages:
            self.new_page()
        for n, p in enumerate(self.pages, 1):
            if footer is not None:
                footer(p, n, total)
            else:
                p.text_right(w - MARGIN, 28, 8, f"Page {n} of {total}",
                             grey=0.4)
        objs = []          # object bodies, 1-based

        def add(body):
            objs.append(body)
            return len(objs)
        font_ids = {}
        for key, name in FONTS.items():
            font_ids[key] = add(f"<< /Type /Font /Subtype /Type1 /BaseFont "
                                f"/{name} /Encoding /WinAnsiEncoding >>"
                                .encode("latin-1"))
        res = ("<< /Font << " + " ".join(
            f"/{k} {v} 0 R" for k, v in font_ids.items()) + " >> >>")
        pages_id = len(objs) + 1 + 2 * len(self.pages)
        page_ids = []
        for p in self.pages:
            data = p.stream()
            cid = add(f"<< /Length {len(data)} >>\nstream\n".encode("latin-1")
                      + data + b"\nendstream")
            pid = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox "
                      f"[0 0 {w:g} {h:g}] /Resources {res} /Contents "
                      f"{cid} 0 R >>".encode("latin-1"))
            page_ids.append(pid)
        kids = " ".join(f"{i} 0 R" for i in page_ids)
        got = add(f"<< /Type /Pages /Kids [{kids}] /Count "
                  f"{len(page_ids)} >>".encode("latin-1"))
        assert got == pages_id
        info = add(f"<< /Title ({_ascii(self.title)}) /Producer (LTC Player "
                   f"pdfmini) /Author ({_ascii(self.author)}) >>"
                   .encode("latin-1"))
        root = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>"
                   .encode("latin-1"))
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode("latin-1")
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root {root} 0 R /Info "
                f"{info} 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode("latin-1")
        return bytes(out)


_TJ = re.compile(rb"\((.*?)(?<!\\)\)\s*Tj")


def extract_text(data):
    """Every text string in a PDF this module wrote, in order, one per
    line: for the selftest and anyone checking a record without a viewer.
    Reads only uncompressed content streams."""
    out = []
    for m in _TJ.finditer(data):
        s = m.group(1)
        s = re.sub(rb"\\([0-7]{3})", lambda mm: bytes([int(mm.group(1), 8)]),
                   s)
        s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\",
                                                                   b"\\")
        out.append(s.decode("latin-1"))
    return "\n".join(out)


def page_count(data):
    m = re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data)
    return int(m.group(1)) if m else 0
