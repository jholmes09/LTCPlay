"""Write VERSION into a built app folder, so every program in it can say
which release it is.

    python packaging/windows/stamp_version.py "dist/LTC Player" 2026.10.10.1

The build id is worked out by ltcplay.version itself, pointed at the built
folder exactly as it is when the app runs (one above the ltcplay package,
which in the app is PyInstaller's _internal folder), so the app reports
"release X, build Y" and not "MODIFIED SINCE".
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))
sys.path.insert(0, ROOT)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    app, release = argv[0], argv[1]
    notes = " ".join(argv[2:])
    internal = os.path.join(app, "_internal")
    if not os.path.isdir(os.path.join(internal, "ltcplay")):
        print(f"{internal} has no ltcplay folder; is this a built app?")
        return 2
    from ltcplay import version as v
    v.folder = lambda: internal
    v._package_dir = lambda: os.path.join(internal, "ltcplay")
    bid = v.build()[0]
    doc = {"release": release, "build": bid, "show": "",
           "made": time.strftime("%Y-%m-%dT%H:%M:%S"), "notes": notes}
    with open(os.path.join(internal, v.STAMP), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    print(f"stamped {internal}: release {release}, build {bid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
