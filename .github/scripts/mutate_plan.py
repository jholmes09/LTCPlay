#!/usr/bin/env python3
"""When the mutate shards run, how many there are, and how many run at once.

One place for selftest.yml, nightly-mutate.yml and weekly-mutate.yml, so the
three can never disagree. selftest.py (test_mutate_shards_fit_their_time_limit)
imports it and checks every rule below.

    python3 .github/scripts/mutate_plan.py mode EVENT REF BASE_REF
    python3 .github/scripts/mutate_plan.py size N [OSES]
    python3 .github/scripts/mutate_plan.py safety-size N
    python3 .github/scripts/mutate_plan.py safety-targets [--with-core] < changed-files
    python3 .github/scripts/mutate_plan.py nightly-cap N

Each prints `name=value` lines ready for $GITHUB_OUTPUT. Anything it cannot
read (a repository variable that is not a whole number, a selection too big
for one matrix) is an error and a nonzero exit, with an ::error:: line: the
job goes red, it never guesses.

Who runs mutations (2026-11-13, the CI diet; TESTING.md has the why):

  - a PR into main (a release): the mutations the diff can have changed the
    result of, plus every flamesafe, conductor and flame link mutation
    (mutate.py --changed BASE --with-core), on ubuntu;
  - a PR into any other branch: only `mutate-safety`, and only when the
    diff touches a safety file (SAFETY_FILES): that file's own mutations,
    and the whole safety core only when a core file itself changed. Not
    required to merge; at most MUTATE_SAFETY_AT_ONCE shards at once;
  - a push (main, a release tag): none. The release PR already ran them on
    the same code, and the weekly sweep runs the whole list;
  - a gpl-* tag: refused. Releases are tagged ltc-YYYY.MM.DD.N; gpl-* is the
    old name and builds nothing (windows-app.yml), so the selftest run
    fails with the reason instead of passing quietly.

How many shards. About PER_SHARD mutations a shard, at most MUTATE_SHARDS
shards (a repository variable, default 30), but never more than
MOST_PER_SHARD a shard, so a shard ends far inside its 340 minute limit
(measured 2026-11-13 on ubuntu with --jobs 3: about 2.8 minutes of a shard
per mutation, so 45 is about 125 minutes). How many at once: never more than
the account's job slots (CI_JOB_SLOTS, default 20) minus a reserve
(CI_SLOT_RESERVE, default 4) that is always left for PRs' selftest and
installer jobs. When Jeff moves to a plan with more slots, he sets
CI_JOB_SLOTS (Settings, Secrets and variables, Actions, Variables) and the
shards spread out by themselves. A matrix holds at most MATRIX_MAX jobs, so
a selection that would need more is an error, said plainly.

The nightly cap. A night that selects more than MUTATE_NIGHTLY_MAX mutations
(default 1500, about 4,200 runner minutes and 4.5 hours on 16 slots) runs
only the safety core and the touched safety files' own mutations, and leaves
the rest to the weekly sweep, which runs everything.
"""
import json
import math
import os
import sys

PER_SHARD = 25
MOST_PER_SHARD = 45
SHARDS_DEFAULT = 30
SLOTS_DEFAULT = 20
RESERVE_DEFAULT = 4
NIGHTLY_MAX_DEFAULT = 1500
# GitHub runs at most 256 jobs from one matrix.
MATRIX_MAX = 256

# The branch whose PRs are releases. Only those run the full per-PR set.
RELEASE_BASE = "main"

# The safety core: every mutation in these runs on a release PR, and on a
# bench-build PR whenever one of these files itself changed.
SAFETY_CORE = ("flamesafe/", "ltcplay/conductor.py", "ltcplay/flamelink.py")
# Files whose own mutations a PR into bench-build runs (mutate-safety).
# A path ending in "/" is a folder; one ending in "*" a name prefix.
SAFETY_FILES = SAFETY_CORE + (
    "ltcplay/laserarm.py", "ltcplay/lasertest.py", "ltcplay/users.py",
    "ltcplay/fuel*", "ltcplay/fireball*", "ltcplay/flameprog.py",
    "ltcplay/hazers.py", "ltcplay/hazertest.py", "ltcplay/morning.py",
    "ltcplay/web/remote_rules.js")


def _whole(env, name, default, least):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ValueError(f"{name}={raw!r} is not a whole number")
    value = int(raw)
    if value < least:
        raise ValueError(f"{name}={value} is under {least}")
    return value


def limits(env):
    """(most shards, how many at once at most) from the repository
    variables, or the defaults."""
    slots = _whole(env, "CI_JOB_SLOTS", SLOTS_DEFAULT, 2)
    reserve = _whole(env, "CI_SLOT_RESERVE", RESERVE_DEFAULT, 1)
    if reserve >= slots:
        raise ValueError(f"CI_SLOT_RESERVE={reserve} leaves no slot of "
                         f"CI_JOB_SLOTS={slots} for the shards")
    most = _whole(env, "MUTATE_SHARDS", SHARDS_DEFAULT, 1)
    return most, slots - reserve


def size(n, env, oses=1):
    """(shards per OS, at once) for n mutations: 0, 0 when there is
    nothing."""
    if n < 0:
        raise ValueError(f"{n} mutations")
    most, at_once = limits(env)
    if n == 0:
        return 0, 0
    k = min(math.ceil(n / PER_SHARD), most)
    # The time limit wins over the shard count: a big selection gets more
    # shards, run in waves, never a shard that cannot finish.
    k = max(k, math.ceil(n / MOST_PER_SHARD))
    if k * oses > MATRIX_MAX:
        raise ValueError(
            f"{n} mutations need {k} shards of at most {MOST_PER_SHARD}"
            f"{f' on each of {oses} OSes' if oses > 1 else ''}, more than "
            f"the {MATRIX_MAX} jobs one matrix may hold: the list has "
            f"outgrown the shard plan. Raise MOST_PER_SHARD in "
            f".github/scripts/mutate_plan.py only after measuring that a "
            f"bigger shard still ends inside 340 minutes, or cut the list")
    return k, min(k * oses, at_once)


def safety_size(n, env):
    """(shards, at once) for a bench-build PR's mutate-safety shards: the
    same shard size, but never more at once than MUTATE_SAFETY_AT_ONCE
    (default: the reserve, 4), so one PR's safety shards never take the
    slots other PRs' required checks need."""
    k, at_once = size(n, env)
    if k == 0:
        return 0, 0
    reserve = _whole(env, "CI_SLOT_RESERVE", RESERVE_DEFAULT, 1)
    cap = _whole(env, "MUTATE_SAFETY_AT_ONCE", reserve, 1)
    return k, min(k, cap, at_once)


def _matches(path, pattern):
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


def safety_targets(changed):
    """(the --targets list for mutate-safety, why). Empty when the diff
    touches no safety file: docs, wording, the page, other code."""
    # Code only: a CONTRACT.md or an example config proves nothing by
    # changing (the same rule as mutate.py's safety triggers).
    touched = sorted({p for p in changed if p.endswith((".py", ".js"))
                      and any(_matches(p, s) for s in SAFETY_FILES)})
    if not touched:
        return [], "no safety file touched: no mutate-safety shards"
    targets = touched
    why = f"safety files touched: {', '.join(touched)}"
    if any(_matches(p, s) for p in touched for s in SAFETY_CORE):
        targets = sorted(set(touched) | set(SAFETY_CORE))
        why += "; a core file changed, so the whole safety core runs too"
    return targets, why


def nightly_cap(env):
    """The most mutations a night runs in full; over it, the night runs
    the safety core and the touched safety files only."""
    return _whole(env, "MUTATE_NIGHTLY_MAX", NIGHTLY_MAX_DEFAULT, 1)


def mode(event, ref, base_ref):
    """(mode, why) for selftest.yml: "affected" for a PR into main,
    "refuse" for a gpl-* tag, "none" for everything else."""
    if ref.startswith("refs/tags/gpl-"):
        return "refuse", (
            f"{ref[len('refs/tags/'):]} uses the old release tag name: "
            f"releases are tagged ltc-YYYY.MM.DD.N now (for example "
            f"ltc-2026.11.14.1), so no installer is built for this tag. "
            f"Delete it and tag ltc-... instead (packaging/windows/"
            f"INSTALL.md, Make a release).")
    if ref.startswith("refs/tags/"):
        return "none", ("a release tag: selftest and the installer only. The "
                        "release PR into main ran the mutations on this code, "
                        "and the weekly sweep runs the whole list")
    if event == "pull_request":
        if base_ref == RELEASE_BASE:
            return "affected", ("a PR into main (a release): the mutations "
                                "this diff can have changed the result of, "
                                "plus the safety core")
        return "none", (f"a PR into {base_ref or 'another branch'}: no "
                        f"release shards here (mutate-safety runs a touched "
                        f"safety file's own mutations). The nightly sweep "
                        f"runs what bench-build changed, the weekly sweep "
                        f"runs everything")
    return "none", (f"a {event} to {ref}: no mutate shards. The release PR "
                    f"ran them before this merge; the weekly sweep runs "
                    f"everything")


def main(argv):
    env = os.environ
    try:
        if len(argv) == 4 and argv[0] == "mode":
            m, why = mode(*argv[1:])
            print(f"mode={m}")
            print(f"why={why}")
            if m == "refuse":
                print(f"::error::{why}")
                return 1
            return 0
        if len(argv) in (2, 3) and argv[0] == "size":
            oses = int(argv[2]) if len(argv) == 3 else 1
            k, at_once = size(int(argv[1]), env, oses)
            print(f"nshards={k}")
            print(f"parallel={at_once}")
            print(f"shards={json.dumps(list(range(k)))}")
            return 0
        if len(argv) == 2 and argv[0] == "safety-size":
            k, at_once = safety_size(int(argv[1]), env)
            print(f"safety_nshards={k}")
            print(f"safety_parallel={at_once}")
            print(f"safety_shards={json.dumps(list(range(k)))}")
            return 0
        if argv[:1] == ["safety-targets"] and argv[1:] in ([], ["--with-core"]):
            targets, why = safety_targets(
                [l.strip() for l in sys.stdin if l.strip()])
            if argv[1:] and set(SAFETY_CORE) - set(targets):
                targets = sorted(set(targets) | set(SAFETY_CORE))
                why += "; and the whole safety core, always"
            print(f"safety_targets={','.join(targets)}")
            print(f"safety_why={why}")
            return 0
        if len(argv) == 2 and argv[0] == "nightly-cap":
            n, cap = int(argv[1]), nightly_cap(env)
            print(f"cap={cap}")
            print(f"capped={'true' if n > cap else 'false'}")
            return 0
    except ValueError as e:
        print(f"::error::mutate_plan.py: {e}")
        return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
