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
    # The build id is the SOURCE the app was built from: the ltcplay
    # package and launchers (what a Mac copy hashes), flamesafe, and the
    # Windows packaging. In the app the code is compiled into the programs,
    # so the files on disk below cannot tell two builds apart (2026-10-05:
    # two builds with different code both said 194dbb4c09).
    import hashlib
    h = hashlib.sha256(v.build()[0].encode())
    for sub, exts in (("flamesafe", (".py", ".json")),
                      (os.path.join("packaging", "windows"),
                       (".py", ".spec", ".iss", ".txt"))):
        top = os.path.join(ROOT, sub)
        for d, dirs, names in os.walk(top):
            dirs[:] = sorted(x for x in dirs if x not in
                             ("__pycache__", "build", "dist"))
            for name in sorted(names):
                if not name.endswith(exts):
                    continue
                p = os.path.join(d, name)
                h.update(os.path.relpath(p, ROOT).replace(os.sep, "/")
                         .encode() + b"\0")
                with open(p, "rb") as fh:
                    h.update(fh.read())
    bid = h.hexdigest()[:10]
    # What the app can check on disk for "MODIFIED SINCE" (version.status).
    v.folder = lambda: internal
    v._package_dir = lambda: os.path.join(internal, "ltcplay")
    files = v.build()[0]
    doc = {"release": release, "build": bid, "files": files, "show": "",
           "made": time.strftime("%Y-%m-%dT%H:%M:%S"), "notes": notes}
    with open(os.path.join(internal, v.STAMP), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    print(f"stamped {internal}: release {release}, build {bid} (files on "
          f"disk {files})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
