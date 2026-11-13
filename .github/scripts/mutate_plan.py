#!/usr/bin/env python3
"""When the mutate shards run, how many there are, and how many run at once.

One place for selftest.yml, nightly-mutate.yml and weekly-mutate.yml, so the
three can never disagree. selftest.py (test_mutate_shards_fit_their_time_limit)
imports it and checks every rule below.

    python3 .github/scripts/mutate_plan.py mode EVENT REF BASE_REF
    python3 .github/scripts/mutate_plan.py size N

`mode` prints `mode=...` and `why=...`; `size` prints `nshards=`,
`parallel=` and `shards=` (a JSON list), ready for $GITHUB_OUTPUT. Anything
it cannot read (a repository variable that is not a whole number, say) is an
error and a nonzero exit: the job goes red, it never guesses.

Who runs mutations (2026-11-13, the CI diet; TESTING.md has the why):

  - a PR into main (a release): the mutations the diff can have changed the
    result of, plus every flamesafe, conductor and flame link mutation
    (mutate.py --changed BASE --with-core), on ubuntu;
  - every other PR (into bench-build, or stacked on another PR): none. The
    nightly sweep runs what bench-build changed in the last day;
  - a push (main, a release tag): none. The release PR already ran them on
    the same code, and the weekly sweep runs the whole list.

How many shards. About PER_SHARD mutations a shard, at most MUTATE_SHARDS
shards (a repository variable, default 30), but never more than
MOST_PER_SHARD a shard, so a shard ends far inside its 340 minute limit
(measured 2026-11-13 on ubuntu with --jobs 3: about 2.8 minutes of a shard
per mutation, so 45 is about 125 minutes). How many at once: never more than
the account's job slots (CI_JOB_SLOTS, default 20) minus a reserve
(CI_SLOT_RESERVE, default 4) that is always left for PRs' selftest and
installer jobs. When Jeff moves to a plan with more slots, he sets
CI_JOB_SLOTS (Settings, Secrets and variables, Actions, Variables) and the
shards spread out by themselves.
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

# The branch whose PRs are releases. Only those run mutations per PR.
RELEASE_BASE = "main"


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


def size(n, env):
    """(shards, at once) for n mutations: 0, 0 when there is nothing."""
    if n < 0:
        raise ValueError(f"{n} mutations")
    if n == 0:
        return 0, 0
    most, at_once = limits(env)
    k = min(math.ceil(n / PER_SHARD), most)
    # The time limit wins over the shard count: a big selection gets more
    # shards, run in waves, never a shard that cannot finish.
    k = max(k, math.ceil(n / MOST_PER_SHARD))
    return k, min(k, at_once)


def mode(event, ref, base_ref):
    """(mode, why) for selftest.yml: "affected" for a PR into main, "none"
    for everything else."""
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
                        f"mutate shards here. The nightly sweep runs what "
                        f"bench-build changed in the last day, the weekly "
                        f"sweep runs everything")
    return "none", (f"a {event} to {ref}: no mutate shards. The release PR "
                    f"ran them before this merge; the weekly sweep runs "
                    f"everything")


def main(argv):
    if len(argv) == 4 and argv[0] == "mode":
        m, why = mode(*argv[1:])
        print(f"mode={m}")
        print(f"why={why}")
        return 0
    if len(argv) == 2 and argv[0] == "size":
        k, at_once = size(int(argv[1]), os.environ)
        print(f"nshards={k}")
        print(f"parallel={at_once}")
        print(f"shards={json.dumps(list(range(k)))}")
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
