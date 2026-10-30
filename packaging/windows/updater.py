"""Check for a newer LTC Player on GitHub Releases, download it, check its
SHA-256 and hand it to the installer: "LTC Player.exe --update" (Start menu,
"Check for an update").

The release workflow (.github/workflows/windows-app.yml) publishes every
build as a release with two assets: "LTC Player Setup <version>.exe" and
its ".sha256". This asks GitHub for the releases, picks the newest one that
carries both, and compares its tag with the version this install was
stamped with (ltcwin.release()). A show PC running a release (a gpl-*
tag, stamped like 2026.10.30.1) is only ever offered full releases, the
one GitHub marks Latest: the bench-build branch's "bench-..." builds are
pre-releases and never reach it. A bench or test build (bench-..., dev-...,
unstamped) is offered the newest of everything, so it moves on to a
release. Newer: download to the settings folder's
"downloads", check the hash against the .sha256, and start the installer,
which stops LTC Player the safe way and refuses while a show is running
(installer.iss). Nothing is installed by this module itself.

A show PC on the show network alone has no GitHub: the check says so in a
sentence and INSTALL.md says what to do instead (download on another
computer, bring it on a USB stick, double-click it).

Pure parts (which release is newest, is it newer than mine, does the hash
match) take plain data, for the self-check.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

REPO = "jholmes09/LTCPlay"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
# The newest full release (never a pre-release or a draft), whatever the
# number of bench pre-releases published after it.
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_RE = re.compile(r"^\d+(\.\d+){1,3}$")
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
INSTALLER_RE = re.compile(r"^LTC[ .]Player[ .]Setup[ .](.+)\.exe$")
TIMEOUT_S = 20

# Exit codes of --update --check-only, for the smoke test and scripts.
UP_TO_DATE, NEWER_AVAILABLE, NO_NETWORK, NO_RELEASES = 0, 10, 20, 21


def _asset_version(name):
    m = INSTALLER_RE.match(name or "")
    return m.group(1) if m else None


def is_release(version):
    """True for a release's version (from a gpl-YYYY.MM.DD.N tag:
    2026.10.30.1), False for bench-..., dev-..., or nothing."""
    return bool(RELEASE_RE.match(str(version or "").strip()))


def pick(releases, prereleases=True):
    """The newest release (by published_at) that carries an installer and
    its .sha256, as {"tag", "version", "exe_url", "exe_name", "sha_url",
    "size", "published", "prerelease", "page"}, or None. With
    prereleases=False a pre-release is never picked."""
    best = None
    for r in releases or []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        if r.get("prerelease") and not prereleases:
            continue
        exe = sha = None
        for a in r.get("assets") or []:
            n = a.get("name") or ""
            if _asset_version(n) and n.lower().endswith(".exe"):
                exe = a
            elif n.lower().endswith(".exe.sha256"):
                sha = a
        if exe is None or sha is None:
            continue
        when = r.get("published_at") or r.get("created_at") or ""
        if best is None or when > best["published"]:
            best = {"tag": r.get("tag_name") or "",
                    "version": _asset_version(exe["name"]),
                    "exe_url": exe.get("browser_download_url"),
                    "exe_name": exe["name"], "size": exe.get("size") or 0,
                    "sha_url": sha.get("browser_download_url"),
                    "published": when,
                    "prerelease": bool(r.get("prerelease")),
                    "page": r.get("html_url") or RELEASES_PAGE}
    return best


def is_newer(candidate, installed):
    """True when `candidate` (a release's version string) is not the one
    installed. The versions are dated strings (bench-2026.10.15-abc1234,
    2026.10.20.1); a different one that was published later is the newer
    one, which pick() already chose by date. Two release numbers are
    compared as numbers, so a release is never "updated" to an older one.
    An unstamped install (no VERSION) counts as older than anything."""
    if not installed:
        return True
    c, i = str(candidate).strip(), str(installed).strip()
    if is_release(c) and is_release(i):
        return tuple(map(int, c.split("."))) > tuple(map(int, i.split(".")))
    return c != i


def sha256_matches(path, sha_text):
    """True when the hex in `sha_text` (a .sha256 file: "<hex> *name") is
    the SHA-256 of the file at `path`."""
    want = (sha_text or "").split()
    if not want:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == want[0].strip().lower()


def _get(url, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, headers={
        "User-Agent": "LTC-Player-updater", "Accept": "application/json"
        if "api.github.com" in url else "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_releases(get=_get, latest=False):
    """The releases list from GitHub, or raises OSError with a sentence.
    latest=True: only the newest full release (GitHub's Latest), as a list
    of one, or an empty list when there is none yet."""
    try:
        if latest:
            doc = json.loads(get(API_LATEST).decode("utf-8", "replace"))
            return [doc] if isinstance(doc, dict) else []
        return json.loads(get(API).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if latest and e.code == 404:
            return []
        raise OSError(f"GitHub answered {e.code} for the releases list") \
            from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OSError(f"GitHub could not be reached ({type(e).__name__}: "
                      f"{e}). This PC may be on the show network only.") \
            from e


def download(url, dest, get=_get, log=None):
    """Download `url` to `dest` (through dest + ".part"), in one read."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    t0 = time.monotonic()
    data = get(url, timeout=600)
    with open(part, "wb") as fh:
        fh.write(data)
    os.replace(part, dest)
    if log:
        log(f"downloaded {len(data) / 1024 ** 2:.1f} MB in "
            f"{time.monotonic() - t0:.0f} s to {dest}")
    return dest


def check(installed, get=_get):
    """(code, sentence, release) for what is newest on GitHub against
    `installed` (this build's version string). Never raises."""
    on_release = is_release(installed)
    try:
        releases = fetch_releases(get, latest=on_release)
    except OSError as e:
        return NO_NETWORK, (f"Could not check for an update: {e} To update "
                            f"anyway, download LTC Player Setup from "
                            f"{RELEASES_PAGE} on another computer, bring it "
                            f"here on a USB stick and double-click it."), None
    rel = pick(releases, prereleases=not on_release)
    if rel is None:
        return NO_RELEASES, (f"No release on {RELEASES_PAGE} carries an "
                             f"installer and its .sha256 yet."), None
    if not is_newer(rel["version"], installed):
        return UP_TO_DATE, (f"This PC has the newest LTC Player, "
                            f"{rel['version']} (published "
                            f"{rel['published'][:10]})."), rel
    return NEWER_AVAILABLE, (
        f"A newer LTC Player is on GitHub: {rel['version']}, published "
        f"{rel['published'][:10]}"
        f"{' (pre-release)' if rel['prerelease'] else ''}, "
        f"{rel['size'] / 1024 ** 2:.0f} MB. This PC has "
        f"{installed or 'an unstamped build'}."), rel


def fetch_installer(rel, downloads_dir, get=_get, log=None):
    """Download the release's installer and its .sha256 into
    `downloads_dir`, check the hash, and return the installer's path.
    Raises OSError with a sentence when the download or the check fails;
    a file that fails its check is removed."""
    dest = os.path.join(downloads_dir, rel["exe_name"])
    if os.path.isfile(dest):
        # Already here: keep it only if it checks out.
        try:
            if sha256_matches(dest, get(rel["sha_url"]).decode(
                    "utf-8", "replace")):
                if log:
                    log(f"{dest} was already downloaded and its SHA-256 "
                        f"matches")
                return dest
        except OSError:
            pass
        os.remove(dest)
    try:
        download(rel["exe_url"], dest, get=get, log=log)
        sha_text = get(rel["sha_url"]).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise OSError(f"The download failed ({type(e).__name__}: {e}). "
                      f"Nothing was installed.") from e
    if not sha256_matches(dest, sha_text):
        try:
            os.remove(dest)
        except OSError:
            pass
        raise OSError(f"The downloaded installer did not match its "
                      f"published SHA-256, so it was deleted. Nothing was "
                      f"installed. Try again, or download it by hand from "
                      f"{rel['page']}.")
    return dest


def self_test():
    """Plain checks with made-up releases and a scratch file."""
    import tempfile
    rels = [
        {"tag_name": "bench-2026.10.08-c1b2e06", "published_at":
         "2026-10-08T04:04:23Z", "prerelease": True, "assets": [
             {"name": "LTC.Player.Setup.BENCH-ONLY-2026.10.08-c1b2e06.exe",
              "browser_download_url": "u1", "size": 100}]},
        {"tag_name": "bench-2026.10.15-abc1234", "published_at":
         "2026-10-15T10:00:00Z", "prerelease": True, "assets": [
             {"name": "LTC.Player.Setup.bench-2026.10.15-abc1234.exe",
              "browser_download_url": "u2", "size": 90 * 1024 ** 2},
             {"name": "LTC.Player.Setup.bench-2026.10.15-abc1234.exe.sha256",
              "browser_download_url": "s2", "size": 100}]},
        {"tag_name": "bench-2026.10.16-draft", "draft": True,
         "published_at": "2026-10-16T00:00:00Z", "assets": [
             {"name": "LTC.Player.Setup.x.exe", "browser_download_url": "u3"},
             {"name": "LTC.Player.Setup.x.exe.sha256",
              "browser_download_url": "s3"}]}]
    rel = pick(rels)
    if not rel or rel["version"] != "bench-2026.10.15-abc1234" or \
            rel["sha_url"] != "s2":
        raise RuntimeError(f"pick chose {rel}")
    yield "the newest release with an installer and its .sha256 is picked " \
          "(no draft, none without a checksum)"
    if is_newer(rel["version"], "bench-2026.10.15-abc1234") or \
            not is_newer(rel["version"], "bench-2026.10.08-c1b2e06") or \
            not is_newer(rel["version"], ""):
        raise RuntimeError("is_newer reads the installed version wrongly")
    yield "an install is up to date only when it is the picked release"
    full = {"tag_name": "gpl-2026.10.30.1", "published_at":
            "2026-10-30T20:00:00Z", "prerelease": False, "assets": [
                {"name": "LTC.Player.Setup.2026.10.30.1.exe",
                 "browser_download_url": "u4", "size": 90 * 1024 ** 2},
                {"name": "LTC.Player.Setup.2026.10.30.1.exe.sha256",
                 "browser_download_url": "s4", "size": 100}]}
    later_bench = {"tag_name": "bench-2026.10.31-def5678", "published_at":
                   "2026-10-31T09:00:00Z", "prerelease": True, "assets": [
                       {"name": "LTC.Player.Setup.bench-2026.10.31-def5678.exe",
                        "browser_download_url": "u5", "size": 100},
                       {"name": "LTC.Player.Setup.bench-2026.10.31-def5678"
                                ".exe.sha256",
                        "browser_download_url": "s5", "size": 100}]}
    if pick(rels + [full, later_bench], prereleases=False)["version"] != \
            "2026.10.30.1" or pick(rels, prereleases=False) is not None:
        raise RuntimeError("a pre-release was picked for a release install")
    if pick(rels + [full])["version"] != "2026.10.30.1":
        raise RuntimeError("a bench install is not offered the release")
    if not is_release("2026.10.30.1") or is_release("bench-2026.10.15-a") \
            or is_release("dev-abc1234") or is_release(""):
        raise RuntimeError("is_release reads a version wrongly")
    if is_newer("2026.10.30.1", "2026.10.30.2") or \
            not is_newer("2026.10.30.10", "2026.10.30.9") or \
            is_newer("2026.10.30.1", "2026.10.30.1"):
        raise RuntimeError("release numbers are not compared as numbers")

    def latest_get(url, timeout=0):
        if url == API_LATEST:
            return json.dumps(full).encode()
        raise urllib.error.URLError("a release install read the whole list")
    code, text, _ = check("2026.10.30.1", get=latest_get)
    if code != UP_TO_DATE:
        raise RuntimeError(f"check (release, up to date): {code} {text}")
    code, text, _ = check("2026.09.15.10", get=latest_get)
    if code != NEWER_AVAILABLE or "2026.10.30.1" not in text or \
            "pre-release" in text:
        raise RuntimeError(f"check (release, newer): {code} {text}")

    def no_latest(url, timeout=0):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    code, text, _ = check("2026.10.30.1", get=no_latest)
    if code != NO_RELEASES:
        raise RuntimeError(f"check (no full release yet): {code} {text}")
    yield "a release install is offered only full releases (GitHub's " \
          "Latest), never a bench pre-release, and never an older number; " \
          "a bench install is offered the release"
    work = tempfile.mkdtemp(prefix="ltcplay-updater-")
    try:
        p = os.path.join(work, "x.exe")
        with open(p, "wb") as fh:
            fh.write(b"installer bytes")
        good = hashlib.sha256(b"installer bytes").hexdigest()
        if not sha256_matches(p, f"{good} *x.exe\n") or \
                sha256_matches(p, "00" * 32 + " *x.exe") or \
                sha256_matches(p, ""):
            raise RuntimeError("the SHA-256 check is wrong")
        yield "a .sha256 file is checked against the downloaded installer"

        def fake_get(url, timeout=0):
            if url == API:
                return json.dumps(rels).encode()
            if url == "u2":
                return b"installer bytes"
            if url == "s2":
                return f"{good} *LTC Player Setup.exe\n".encode()
            raise urllib.error.URLError("no such url")
        code, text, got = check("bench-2026.10.08-c1b2e06", get=fake_get)
        if code != NEWER_AVAILABLE or "bench-2026.10.15-abc1234" not in text:
            raise RuntimeError(f"check: {code} {text}")
        code, text, _ = check("bench-2026.10.15-abc1234", get=fake_get)
        if code != UP_TO_DATE:
            raise RuntimeError(f"check (up to date): {code} {text}")

        def offline(url, timeout=0):
            raise urllib.error.URLError("getaddrinfo failed")
        code, text, _ = check("x", get=offline)
        if code != NO_NETWORK or "USB stick" not in text:
            raise RuntimeError(f"check (offline): {code} {text}")
        yield "the check says newer, up to date, or how to update with no " \
              "network"
        dest = fetch_installer(got, os.path.join(work, "downloads"),
                               get=fake_get)
        if not os.path.isfile(dest) or not dest.endswith(".exe"):
            raise RuntimeError(f"fetch_installer gave {dest}")

        def bad_sha(url, timeout=0):
            if url == "s2":
                return b"00" * 32 + b" *x\n"
            return fake_get(url, timeout)
        os.remove(dest)
        try:
            fetch_installer(got, os.path.join(work, "downloads"), get=bad_sha)
        except OSError as e:
            if "deleted" not in str(e) or os.path.exists(dest):
                raise RuntimeError(f"a bad download was kept: {e}")
        else:
            raise RuntimeError("a bad SHA-256 was accepted")
        yield "a download whose SHA-256 does not match is deleted, never run"
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
