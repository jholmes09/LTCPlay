# PyInstaller spec for the Windows app: ONE folder, five programs.
#
#   LTC Player.exe     the supervisor: starts, watches and safely stops the
#                      other three (no console window)
#   ltcplay.exe        the show engine (ltc serve and every ltc command)
#   flamesafe.exe      the flame safety program (contains no ltcplay code)
#   ltcplay-deck.exe   the Stream Deck program (ltc deck)
#   ltcplay-soak.exe   the bench soak test (Start menu: LTC Player bench
#                      soak test); never run by the show
#
# Build from the repo root:  pyinstaller --noconfirm packaging/windows/ltcplay.spec
# Output: dist/LTC Player/
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(os.path.dirname(HERE))
# collect_submodules imports the packages to list them, so they must be
# importable while this file runs, not only during the analysis.
sys.path[:0] = [ROOT, HERE]

# The app icon, made from the Mac iconset so both share one picture.
# Each size comes from its own drawing (the small ones are simplified so
# "LTC" stays readable), so the .ico is written by hand: PNG entries.
ICON = None
try:
    import io
    import struct
    from PIL import Image
    ICONSET = os.path.join(ROOT, "LTC Player.iconset")
    pngs = {16: "icon_16x16.png", 32: "icon_32x32.png",
            64: "icon_32x32@2x.png", 128: "icon_128x128.png",
            256: "icon_256x256.png"}
    images = {s: Image.open(os.path.join(ICONSET, f)).convert("RGBA")
              for s, f in pngs.items()}
    images[48] = images[64].resize((48, 48), Image.LANCZOS)
    entries = []
    for s in sorted(images):
        buf = io.BytesIO()
        images[s].resize((s, s), Image.LANCZOS).save(buf, "PNG")
        entries.append((s, buf.getvalue()))
    ICON = os.path.join(HERE, "build", "ltcplay.ico")
    os.makedirs(os.path.dirname(ICON), exist_ok=True)
    with open(ICON, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(entries)))
        offset = 6 + 16 * len(entries)
        for s, data in entries:
            f.write(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                                len(data), offset))
            offset += len(data)
        for _, data in entries:
            f.write(data)
except Exception as e:  # an icon is not worth a failed build
    print(f"no icon: {e}")
    ICON = None

LTCPLAY_DATA = [
    (os.path.join(ROOT, "ltcplay", "web"), os.path.join("ltcplay", "web")),
    (os.path.join(ROOT, "ltcplay_brand.json"), "."),
] + collect_data_files("tzdata")

EXCLUDES = ["tkinter", "pydoc_data"]

# Every module of both packages, listed up front: some (flamelink,
# conductor, announce) are only imported lazily or not yet by the engine,
# and a module missing from the app is found by a show, not by the build.
LTCPLAY_MODULES = collect_submodules("ltcplay")
FLAMESAFE_MODULES = collect_submodules("flamesafe")
for _pkg, _mods, _must in (("ltcplay", LTCPLAY_MODULES, "ltcplay.flamelink"),
                           ("flamesafe", FLAMESAFE_MODULES,
                            "flamesafe.service")):
    if _must not in _mods:
        raise SystemExit(f"could not list the {_pkg} package's modules")

common = dict(pathex=[ROOT, HERE], hookspath=[], runtime_hooks=[],
              excludes=EXCLUDES, noarchive=False)

a_engine = Analysis(
    [os.path.join(HERE, "entry_engine.py")],
    datas=LTCPLAY_DATA,
    hiddenimports=LTCPLAY_MODULES + ["ltcwin", "sounddevice",
                                                  "_sounddevice", "tzdata",
                                                  "bench_probe"],
    **common)

a_flame = Analysis(
    [os.path.join(HERE, "entry_flamesafe.py")],
    datas=[],
    hiddenimports=FLAMESAFE_MODULES + ["ltcwin"],
    **dict(common, excludes=EXCLUDES + ["ltcplay", "numpy", "sounddevice",
                                        "PIL", "hid", "zstandard"]))

a_deck = Analysis(
    [os.path.join(HERE, "entry_deck.py")],
    datas=LTCPLAY_DATA,
    hiddenimports=LTCPLAY_MODULES + ["ltcwin", "hid"],
    **common)

a_soak = Analysis(
    [os.path.join(HERE, "soak.py")],
    datas=LTCPLAY_DATA + [(os.path.join(ROOT, "flamesafe",
                                        "flamesafe.example.json"), ".")],
    hiddenimports=LTCPLAY_MODULES + ["ltcwin", "supervisor", "soak_apps",
                                     "soak_exercise", "contain", "bench_probe",
                                     "test_show_fixtures", "psutil", "hid",
                                     "sounddevice", "_sounddevice",
                                     "tzdata"],
    **common)

a_sup = Analysis(
    [os.path.join(HERE, "supervisor.py")],
    datas=[],
    hiddenimports=["ltcwin", "contain"],
    **dict(common, excludes=EXCLUDES + ["ltcplay", "flamesafe", "numpy",
                                        "sounddevice", "PIL", "hid",
                                        "zstandard"]))


def exe(a, name, console):
    pyz = PYZ(a.pure)
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name,
               debug=False, strip=False, upx=False, console=console,
               icon=ICON)


e_engine = exe(a_engine, "ltcplay", True)
e_flame = exe(a_flame, "flamesafe", True)
e_deck = exe(a_deck, "ltcplay-deck", True)
e_sup = exe(a_sup, "LTC Player", False)
e_soak = exe(a_soak, "ltcplay-soak", True)

coll = COLLECT(
    e_sup, a_sup.binaries, a_sup.datas,
    e_engine, a_engine.binaries, a_engine.datas,
    e_flame, a_flame.binaries, a_flame.datas,
    e_deck, a_deck.binaries, a_deck.datas,
    e_soak, a_soak.binaries, a_soak.datas,
    strip=False, upx=False, name="LTC Player")
