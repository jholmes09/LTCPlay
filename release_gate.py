#!/usr/bin/env python3
"""The release gate's tier: how much of the mutation sweep a PR into main
must pass before it merges, worked out from the diff alone.

Nobody picks the tier. A PR description, a label that asks for less, or an
agent saying "it is only text" changes nothing: this script reads what
git says changed and decides. Anything it cannot work out runs everything.

    tier docs    every changed file is Markdown, none of them is a file
                 mutate.py breaks, and no test reads it (its name appears
                 nowhere in the test code): the selftest only (ubuntu and
                 windows), no mutation runs
    tier scoped  anything else: the mutations whose text lies in or near
                 the changed lines (every mutation of a changed safety
                 file), the mutations last caught by a test whose string
                 literals the change edited, and the RELEASE_CORE below,
                 always. New tests may be added; an existing test may
                 only have its strings and comments changed
    tier full    every mutation, as before. Forced when the change touches
                 what decides or measures the gate itself (FORCE_FULL, the
                 shared code of selftest.py outside its test_ functions,
                 anything under tools/), when an existing test is deleted
                 or changes in more than its strings (or a string becomes
                 empty), when the last whole sweep on main is red (its
                 `mutate-sweep` issue is open) and any code changed, and
                 whenever this script errors or cannot read the diff

The workflow runs the copy of this file (and of mutate.py and its
catchers) that is on the BASE branch, against the PR's diff, so a release
can never lower its own tier by editing the gate. No copy on the base:
full.

The whole list still runs after the fact: on every push to main (each
merged release), on every release tag, every Sunday, and by hand. A red
one opens the `mutate-sweep` issue, and while it is open every code
release runs the whole list before it merges. TESTING.md has the rest.

    python3 release_gate.py --base origin/main             print the tier
    python3 release_gate.py --base REV --names-out F       and the names
    python3 release_gate.py --base REV --github-output     for the workflow
    --sweep-red WHY    the last whole sweep on main is red: any code
                       change runs everything (the workflow passes it)
    --repo DIR         the git work tree to read the diff from (default:
                       this file's folder; the workflow runs a copy of the
                       base's gate from elsewhere)
    --check-removed    exit 1 if a mutation of BASE's mutate.py is gone
                       from HEAD's without a line in mutate_removed.txt
"""
import ast
import hashlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The git work tree the diff is read from (--repo).
REPO = HERE

# A change to any of these forces the whole list: they decide what the gate
# runs (this file, mutate.py and its lists, the workflows), what the suite
# means (the shared fixtures), or the remote's own safety rules.
# tools/ is test support (the UI test rig, its fixtures, fake_engine.js,
# the stress kit): what a test compares against, so any change is full.
FORCE_FULL = ("release_gate.py", "mutate.py", "mutate_catchers.json",
              "mutate_expected_misses.txt", "mutate_removed.txt",
              "test_show_fixtures.py", "ltcplay/web/remote_rules.js",
              ".github/", "tools/")

# Lines of context either side of a change: a mutation whose text starts or
# ends this close to a changed line runs (the same function, nearly always).
CONTEXT = 25

# The always-run safety core: (file, name pattern or None for every one).
# Flame and laser enable gates, Stop and Abort, the interlocks (flamesafe's
# arm input, its link to the node, its service), the Run gate (nothing
# reaches the rig before Run), the remote's own rules, every fireball,
# fuel and flame programming mutation, flamesafe's rules and sACN, the
# deck's arm keys, the remote's Stop and Hold, the morning arm and purge,
# and who may arm (the hazer). About 900 mutations, 2026-11-12. Every rule must still match at least one mutation
# and the core must stay inside CORE_SIZE, or the gate runs everything (a
# renamed file or a broken pattern can never quietly empty it).
_STOP_GATE = (r"(?i)\b(stop|stops|stopped|abort\w*|e-?stop|disarm\w*|arm|"
              r"armed|arming|gate|gates|gated|interlock\w*|enable\w*)\b")
_RUN_GATE = (r"before Run|Run pressed|Run stopped|Run off|for no Run|"
             r"on Run\b|at Run\b|unattended|Run (check|gate)|"
             r"Run is never refused|is not refused|refused during a live show")
RELEASE_CORE = (
    ("ltcplay/web/remote_rules.js", None),
    ("flamesafe/arminput.py", None),
    ("flamesafe/link.py", None),
    ("flamesafe/service.py", None),
    ("ltcplay/laserarm.py", None),
    ("ltcplay/conductor.py", _STOP_GATE),
    ("ltcplay/flamelink.py", _STOP_GATE),
    ("flamesafe/composer.py", _STOP_GATE),
    ("flamesafe/config.py", _STOP_GATE),
    ("ltcplay/schedule_service.py", _RUN_GATE),
    ("ltcplay/fire_ice.py", _RUN_GATE),
    ("ltcplay/web.py", _RUN_GATE),
    ("ltcplay/remote.py", _RUN_GATE),
    ("ltcplay/fireball.py", None),
    ("ltcplay/fireball_charge.py", None),
    ("ltcplay/fireball_cues.py", None),
    ("ltcplay/flameprog.py", None),
    ("ltcplay/fuel.py", None),
    ("ltcplay/fuelpump.py", None),
    ("flamesafe/rules.py", None),
    ("flamesafe/sacn.py", None),
    ("ltcplay/streamdeck.py", r"(?i)^arm\b"),
    ("ltcplay/web/remote2.html", r"(?i)\b(stop|hold|abort)\b"),
    ("ltcplay/morning.py", r"(?i)\b(arm\w*|disarm\w*|purge\w*)\b"),
    ("ltcplay/users.py", r"(?i)hazer|\barm"),
)
CORE_SIZE = (600, 1200)


class GateError(Exception):
    """Anything the gate cannot work out. It always means: run everything."""


def core_names(mutations):
    """The RELEASE_CORE's mutation names. Raises GateError when a rule
    matches nothing or the total leaves CORE_SIZE."""
    out, hits = set(), {}
    for name, rel, _old, _new in mutations:
        for i, (path, rx) in enumerate(RELEASE_CORE):
            if rel == path and (rx is None or re.search(rx, name)):
                out.add(name)
                hits[i] = hits.get(i, 0) + 1
                break
    dead = [RELEASE_CORE[i][0] for i in range(len(RELEASE_CORE))
            if i not in hits]
    if dead:
        raise GateError(f"the safety core matches no mutation in "
                        f"{', '.join(dead)}")
    if not CORE_SIZE[0] <= len(out) <= CORE_SIZE[1]:
        raise GateError(f"the safety core is {len(out)} mutations, outside "
                        f"{CORE_SIZE[0]} to {CORE_SIZE[1]}")
    return out


def _forced(path):
    return any(path == f or (f.endswith("/") and path.startswith(f))
               for f in FORCE_FULL)


def _test_spans(tree):
    """{name: (first line, last line)} of every top-level test_ function,
    decorators included."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            out[n.name] = (first, n.end_lineno)
    return out


def suite_split(text):
    """(the suite's shared code, {test name: its source}) of a selftest.py
    text. The shared code is every line outside the test_ functions: the
    runner, check(), the rigs and helpers every test leans on."""
    tree = ast.parse(text)
    lines = text.splitlines()
    spans = _test_spans(tree)
    inside = set()
    for a, b in spans.values():
        inside.update(range(a, b + 1))
    # Blank lines are not code: the two between a new test and the last
    # one are not a change to the shared code.
    shared = [l for i, l in enumerate(lines, 1)
              if i not in inside and l.strip()]
    tests = {k: "\n".join(lines[a - 1:b]) for k, (a, b) in spans.items()}
    return shared, tests


def _is_test_py(p):
    """A test file other than selftest.py: test_*.py anywhere."""
    b = os.path.basename(p)
    return b.startswith("test_") and b.endswith(".py")


def strings_only(old_src, new_src):
    """None when new_src differs from old_src only in the contents of
    string literals, comments and line breaks; else why not. A string
    that becomes empty counts as more than a string edit: `want = ""` is
    how a check is emptied without touching its code."""
    import io
    import tokenize
    skip = {tokenize.COMMENT, tokenize.NL}
    text_kinds = {tokenize.STRING}
    for k in ("FSTRING_MIDDLE", "TSTRING_MIDDLE"):
        if hasattr(tokenize, k):
            text_kinds.add(getattr(tokenize, k))

    def toks(src):
        out = []
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type in skip:
                continue
            out.append((t.type, None if t.type in text_kinds else t.string,
                        t.string))
        return out
    try:
        a, b = toks(old_src), toks(new_src)
    except (tokenize.TokenError, SyntaxError) as e:
        return f"it could not be read as Python ({e})"
    if [x[:2] for x in a] != [x[:2] for x in b]:
        return "its code changed, not only its strings"
    for x, y in zip(a, b):
        if x[1] is None and x[2] != y[2] and \
                y[2].lstrip("rRbBuUfF").strip("\"'") == "":
            return f"a string became empty ({x[2][:40]} -> {y[2]})"
    return None


def mutations_in(text):
    """The names in a mutate.py text's MUTATIONS list, parsed, never run."""
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MUTATIONS"
                for t in n.targets):
            return {e.elts[0].value for e in n.value.elts}
    raise GateError("no MUTATIONS list in mutate.py")


def removed_without_note(old_mutate, new_mutate, notes):
    """Mutation names in the old mutate.py text that are not in the new
    one and have no `name | why` line in the notes text (a renamed one
    counts as removed: say so)."""
    old = mutations_in(old_mutate) if old_mutate else set()
    new = mutations_in(new_mutate) if new_mutate else set()
    noted = set()
    for line in (notes or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "|" in line:
            noted.add(line.split("|", 1)[0].strip())
    return sorted(old - new - noted)


def anchor_lines(text, old):
    """(first line, last line) of `old` in `text`, or None unless it is
    there exactly once (a mutation that would SETUP FAIL)."""
    if text is None or text.count(old) != 1:
        return None
    at = text.index(old)
    first = text.count("\n", 0, at) + 1
    return first, first + old.count("\n")


def classify(changed, new_text, old_text, hunks, mutations, catchers,
             safety_files=lambda p: False, sweep_red=None,
             tests_read=lambda p: False):
    """(tier, names or None for everything, reasons).

    changed: every path the diff touches, both sides of a rename.
    new_text(p) / old_text(p): the file after and before (None: absent).
    hunks(p): [(first, last)] changed line ranges in the new file.
    mutations: mutate.MUTATIONS. catchers: {mutation: test that caught it}.
    safety_files(p): True for a file whose every mutation runs when it
    changes. sweep_red: why to run everything when any code changed (the
    last whole sweep on main is red); a docs-only release cannot fix or
    worsen that, so it stays docs. tests_read(p): True when some test
    reads the file (its name appears in the test code), so a Markdown
    file is test input, not docs."""
    reasons = []

    def full(why):
        reasons.append(f"FULL: {why}")
        return "full", None, reasons

    targets = {}
    for name, rel, old, _new in mutations:
        targets.setdefault(rel, []).append((name, old))
    changed = sorted(set(changed))
    if not changed:
        reasons.append("no file differs from the base")
        return "docs", set(), reasons
    if all(p.lower().endswith(".md") and p not in targets
           and not tests_read(p) for p in changed):
        reasons.append(f"docs only ({len(changed)} Markdown files, none "
                       f"mutated): the selftest only")
        return "docs", set(), reasons
    if sweep_red:
        return full(sweep_red)
    forced = [p for p in changed if _forced(p)]
    if forced:
        return full(f"the gate's own files changed: {', '.join(forced)}")

    chosen = set()
    if "selftest.py" in changed:
        new_s, old_s = new_text("selftest.py"), old_text("selftest.py")
        if new_s is None or old_s is None:
            return full("selftest.py was added or removed")
        try:
            shared_new, tests_new = suite_split(new_s)
            shared_old, tests_old = suite_split(old_s)
        except SyntaxError as e:
            return full(f"selftest.py could not be parsed ({e})")
        if shared_new != shared_old:
            return full("selftest.py changed outside its test_ functions "
                        "(the runner, check() or a shared rig)")
        gone = sorted(set(tests_old) - set(tests_new))
        if gone:
            return full(f"existing tests deleted or renamed: "
                        f"{', '.join(gone[:5])}")
        touched = {n for n in tests_old if tests_new.get(n) != tests_old[n]}
        for n in sorted(touched):
            why = strings_only(tests_old[n], tests_new[n])
            if why:
                return full(f"an existing test, {n}, changed: {why}. A test "
                            f"can be weakened that way with nothing of its "
                            f"own recorded as caught")
        if not catchers and touched:
            return full("tests changed and there is no catchers file to "
                        "say what they guarded")
        dep = {n for n, *_ in mutations
               if set((catchers.get(n) or "").split(",")) & touched}
        chosen |= dep
        reasons.append(f"selftest.py: {len(touched)} test(s) edited or "
                       f"deleted, {len(dep)} mutations they caught last time")
    for p in changed:
        if p == "selftest.py" or not _is_test_py(p):
            continue
        before, after = old_text(p), new_text(p)
        if before is not None:
            why = "it was deleted" if after is None else \
                strings_only(before, after)
            if why:
                return full(f"the test file {p} changed: {why}")
        if p == "flamesafe/test_flamesafe.py":
            dep = {n for n, *_ in mutations
                   if "test_flamesafe_in_its_own_process"
                   in (catchers.get(n) or "").split(",")}
            chosen |= dep
            reasons.append(f"flamesafe's own tests (strings only): "
                           f"{len(dep)} mutations they caught last time")

    for p in changed:
        if p not in targets:
            continue
        if safety_files(p):
            names = {n for n, _o in targets[p]}
            chosen |= names
            reasons.append(f"{p}: a safety file, all {len(names)} of its "
                           f"mutations")
            continue
        text = new_text(p)
        spans = [(a - CONTEXT, b + CONTEXT) for a, b in hunks(p)]
        near, lost = set(), set()
        for n, old in targets[p]:
            at = anchor_lines(text, old)
            if at is None:
                lost.add(n)
            elif any(a <= at[1] and at[0] <= b for a, b in spans):
                near.add(n)
        chosen |= near | lost
        reasons.append(f"{p}: {len(near)} of its {len(targets[p])} mutations "
                       f"lie within {CONTEXT} lines of a change"
                       + (f", and {len(lost)} whose text is no longer there "
                          f"exactly once (they will SETUP FAIL)"
                          if lost else ""))
    core = core_names(mutations)
    reasons.append(f"the safety core: {len(core - chosen)} more "
                   f"({len(core)} in all)")
    chosen |= core
    return "scoped", chosen, reasons


# --------------------------------------------------------------- git side --

def _git(*args):
    r = subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise GateError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _show(rev, path):
    try:
        return _git("show", f"{rev}:{path}")
    except GateError:
        return None


def _hunks(point, path):
    """Changed line ranges of `path` in HEAD against `point`. A pure
    deletion is the line it was cut after."""
    out = []
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@",
                         _git("diff", "-U0", "--no-renames", point, "HEAD",
                              "--", path), re.M):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        out.append((start, start + max(count, 1) - 1))
    return out


def _tests_read():
    """tests_read() for classify(): True when the file's name appears in
    the test code at HEAD (selftest.py, every test_*.py, tools/)."""
    corpus = []
    for p in _git("ls-tree", "-r", "--name-only", "HEAD").splitlines():
        if p == "selftest.py" or _is_test_py(p) or (
                p.startswith("tools/") and p.endswith((".py", ".js"))):
            corpus.append(_show("HEAD", p) or "")
    text = "\n".join(corpus)
    return lambda p: os.path.basename(p) in text


def decide(base, sweep_red=None):
    """classify() on the diff from the merge base of BASE and HEAD to HEAD.
    Never raises: anything that goes wrong is tier full, with why."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import mutate
        point = _git("merge-base", base, "HEAD").strip()
        if not point:
            raise GateError(f"no merge base with {base}")
        changed = [p for p in _git("diff", "--name-only", "--no-renames",
                                   "-z", point, "HEAD").split("\0") if p]
        catchers = mutate._load_catchers(os.path.join(HERE,
                                                      "mutate_catchers.json"))

        def safety(p):
            return p.startswith(mutate.SAFETY_TRIGGERS) and p.endswith(".py")
        tier, names, reasons = classify(
            changed, lambda p: _show("HEAD", p), lambda p: _show(point, p),
            lambda p: _hunks(point, p), mutate.MUTATIONS, catchers,
            safety, sweep_red, _tests_read())
        return tier, names, [f"against {point[:10]}, {len(changed)} files"] \
            + reasons
    except Exception as e:                       # noqa: BLE001 fail closed
        return "full", None, [f"FULL: the gate could not decide "
                              f"({type(e).__name__}: {e}), so it runs "
                              f"everything"]


def digest(names):
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]


def main(argv):
    global REPO
    base, out, gh, red, check_removed = None, None, False, None, False
    it = iter(argv)
    for a in it:
        if a == "--base":
            base = next(it)
        elif a == "--names-out":
            out = next(it)
        elif a == "--github-output":
            gh = True
        elif a == "--sweep-red":
            red = next(it)
        elif a == "--repo":
            REPO = os.path.abspath(next(it))
        elif a == "--check-removed":
            check_removed = True
        else:
            print(f"unknown option {a}")
            return 2
    if not base:
        print("usage: release_gate.py --base REV [--names-out FILE] "
              "[--github-output] [--sweep-red WHY]")
        return 2
    if check_removed:
        try:
            point = _git("merge-base", base, "HEAD").strip()
            gone = removed_without_note(_show(point, "mutate.py"),
                                        _show("HEAD", "mutate.py"),
                                        _show("HEAD", "mutate_removed.txt"))
        except Exception as e:                   # noqa: BLE001
            print(f"::error::cannot check for removed mutations: {e}")
            return 1
        if gone:
            print(f"::error title=mutations removed::{len(gone)} mutations "
                  f"are gone from mutate.py with no `name | why` line in "
                  f"mutate_removed.txt: {', '.join(gone[:10])}")
            return 1
        print("no mutation was removed without a note")
        return 0
    tier, names, reasons = decide(base, red)
    n = len(names) if names is not None else 0
    print(f"release gate: tier {tier}"
          + (f", {n} mutations" if tier == "scoped" else ""))
    for r in reasons:
        print(f"  -> {r}")
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{x}\n" for x in sorted(names or ())))
    if gh:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"tier={tier}\ncount={n}\n"
                     f"digest={digest(names or ())}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
