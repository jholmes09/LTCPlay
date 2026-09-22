"""Which prop lives at which channel, so a report can name things.

A sparse range is a pair of numbers. "Channels 27031 to 35280 are missing" is
true and useless; "the four Web Corners on Shade 1 & 2 stay dark" is the same
fact in a form somebody can act on. xLights keeps the answer in two files that
have to be read together: xlights_networks.xml says where each controller
starts in the absolute channel space, and xlights_rgbeffects.xml gives each
model a start channel expressed as "!Controller:index" against it.
"""
import os
import re
import xml.etree.ElementTree as ET

_REF = re.compile(r"^!(.+?):(\d+)$")


class ModelMap:
    def __init__(self, models):
        # (absolute start, name, controller), sorted
        self.models = sorted(models)

    def __len__(self):
        return len(self.models)

    def at(self, channel):
        """The model that owns this 1-based channel, as best the map can say.

        Binary search for the last model that starts at or before it. Model
        lengths are not in the file in a form worth trusting, so this names the
        model a channel falls into, not a guarantee that it is inside it."""
        lo, hi, best = 0, len(self.models) - 1, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.models[mid][0] <= channel:
                best = self.models[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def name_at(self, channel):
        hit = self.at(channel)
        if not hit:
            return None
        return f"{hit[1]} [{hit[2]}]"


def load(show_dir, netmap):
    """Read the model layout. Returns an empty map rather than raising: naming
    props is a courtesy on top of a report that has to work without it."""
    path = os.path.join(show_dir, "xlights_rgbeffects.xml")
    if not os.path.exists(path):
        return ModelMap([])
    starts = {}
    for u in netmap.universes:
        starts.setdefault(u.controller, u.start)
    try:
        root = ET.parse(path).getroot()
        node = root.find("models")
        if node is None:
            return ModelMap([])
    except ET.ParseError:
        return ModelMap([])
    out = []
    for m in node.findall("model"):
        sc = (m.get("StartChannel") or "").strip()
        name = m.get("name") or "?"
        ref = _REF.match(sc)
        if ref:
            ctrl, idx = ref.group(1), int(ref.group(2))
            if ctrl in starts:
                out.append((starts[ctrl] + idx - 1, name, ctrl))
        elif sc.isdigit() and int(sc) > 0:
            out.append((int(sc), name, "absolute"))
    return ModelMap(out)
