# How ltcplay is tested

For Jeff and for whoever works on this next. What runs when, how long it
takes, what a failure means, and how to run the whole thing on demand.
Written 2026-10-06, after the audit that simplified it.

## The three layers

1. **selftest.py** is the suite: 377 checks of the program, from LTC
   decoding to the show conductor's Abort. It runs in about 3.5 minutes on
   a laptop and 4 to 6 minutes on a CI runner. Every PR runs it on ubuntu
   and on windows (the show PC's OS). `python3 selftest.py` runs it by
   hand; its last line is "all checks passed in N s" or a count of
   failures, each with a sentence.

2. **mutate.py** is the proof that the suite is honest. It breaks the
   program on purpose, one line at a time (973 breakages, each a real
   show-night failure: "Abort's flame cut waits for the executor", "a new
   socket for every frame"), and requires the suite to fail under each.
   A mutation the suite does not notice is a guarantee nobody is testing.
   Every mutation costs two full suite runs, so the whole list is about
   100 hours of runner time, split 32 ways.

3. **The bench soak** (`packaging/windows/soak.py`, the bench build)
   runs the whole Fire & Ice stack on the show PC for hours with nothing
   dangerous connected, and times every output on the PC's own clock.
   That is where real timing is measured. The suite above proves logic
   and sequence; it does not prove that the show PC meets 25 ms frames,
   because a shared CI runner cannot.

## What runs when

| When | What | How long | Required to merge |
|---|---|---|---|
| Every push to a PR | selftest on ubuntu and windows | 5 to 7 min | yes (both) |
| A PR marked ready for review, and every push to it after | the 32 mutate shards, each running only the mutations this PR can have changed the result of (below) | seconds for a doc or UI change; 10 to 90 min for a change to a safety file | yes (all 32) |
| A push to main | the same, against the previous main | as above | not applicable |
| A release tag (`gpl-...` or a tech build) | every mutation, ubuntu and windows | 4 to 6 hours | the tag is not cut until it is green |
| Every night, 03:12 Eastern | every mutation on main, ubuntu (`nightly mutate` workflow) | 4 to 6 hours | a red night is fixed before the next merge |
| On demand | the same, from the Actions tab: `nightly mutate`, Run workflow, pick ubuntu, windows or both | 4 to 6 hours | before any tech build |

Draft PRs run only the selftest. The mutate shards start when the PR is
marked ready for review.

## Which mutations a PR runs

`mutate.py --changed BASE` works it out from the diff; the rules are in
`select_affected()` in mutate.py and the `changes` job prints them on
every run. A mutation runs when:

- the file it breaks is in the diff;
- any safety file is in the diff (anything under `flamesafe/`, the
  conductor, the flame link, the Stream Deck, the scheduler, BEYOND,
  MadMapper, the devices, the trigger, the output, the player, the
  session, the clock, the announcements, the show audio, the one-copy
  guard): then every flamesafe, conductor and flame link mutation runs,
  about 300 of them, whatever else changed;
- the mutation itself is new or edited in mutate.py;
- its line in `mutate_expected_misses.txt` changed;
- the test that caught it last time was deleted or changed, when a
  catchers file (`--catchers`) says which test that was. Without one, a
  deleted test means the whole list runs.

A change to only a test's body, with no source file of its own in the
diff, does NOT re-run the mutations of other files. That is the one
place the per-PR gate is deliberately incomplete, and the nightly sweep
exists to close it within a day: a test weakened without being renamed,
or a change in one file that quietly silenced a test of another, shows
up there, and a release tag runs everything regardless.

Everything else (docs, launchers, the brand, installers, this file, the
workflow) selects nothing, and the 32 shards report success at once so
the PR can merge.

## What a failure means

**selftest (ubuntu) or selftest (windows) red.** A check failed; the job's
annotations carry the sentence. Read the sentence first: it says what the
program did and what it should have done. If it names a time ("took 0.34
s") it is one of the few remaining real-time checks; those are written so
that only a wait on the thing they forbid can fail them (the bound is half
the deliberate stall), so a red one is a real wait, not a slow runner. If
it still looks like the runner, re-run once; if it is red twice it is
real.

**Where to read why, without the job log.** Every red job says so three
ways: its annotations (the Checks tab, or the API's check-run
annotations) name the shard, the kind of failure, the mutation or test
and the first lines that matter; its job summary carries a table (shard,
mutations, caught, missed, flaky, baseline, time, result) and the reason;
and its full log is an artifact on the run page, `selftest-log-<os>` or
`mutate-log-<os>-shard-<n>`, kept 7 days. The `mutate-report` job at the
end of the run lists the red shards with a link to each. A shard that
ends with "exit code 1" and no sentence is a bug in mutate.py's own
reporting; `test_mutate_reports_every_failure_it_can_have` in selftest.py
forces every way it can end red and checks the sentence is there.

**A mutate shard red.** One of:

- `NOT CAUGHT` or `UNEXPECTED MISS <name>`: the suite stayed green with
  that line broken. The guarantee in the mutation's name is not being
  tested. Fix the test, not the mutation.
- `NOW CAUGHT, delete it from the list`: a mutation listed in
  `mutate_expected_misses.txt` is now caught. Good news; delete its line.
- `SETUP FAIL`: the mutation's text no longer matches the code. Refresh
  the mutation to the current code (never delete it without replacing
  what it proved).
- `The suite FAILS with nothing mutated`: the baseline is red on that
  runner; see the selftest job, the shard measured nothing.
- `shard N/32 TIMED OUT`: the shard ran out of its 300 minutes. Not a
  miss; re-run the shard.
- `THE TREE IS NOT CLEAN`: a restore did not land. Should never happen;
  the shard's tree is thrown away, but say so.

**The nightly sweep red.** The same as a shard, on main. Whoever merges
next fixes it first; nothing is tagged while it is red.

## Running things by hand

- The suite: `python3 selftest.py`.
- One mutation or a few: `python3 mutate.py abort` runs every mutation
  whose name contains "abort".
- What a change would run: `python3 mutate.py --changed origin/main
  --list` (or `--files ltcplay/conductor.py,selftest.py --list` with no
  git).
- The affected mutations of a change, for real: `python3 mutate.py
  --changed origin/main`.
- Everything, locally: `python3 mutate.py` (plan on most of a day; a
  shard of it is `--shard 3/32`).
- Everything, in CI: Actions tab, `nightly mutate`, Run workflow. Choose
  `both` before a tech build.

While mutate.py runs, the working tree is deliberately broken (a
`.mutating` file marks it). Do not build, soak or run a show from that
tree until it says "tree restored byte for byte".

## Real time in the suite

A handful of checks still read the clock, because what they prove is a
thread handoff: Abort does not wait behind a slow BEYOND, a hung
announcement, a stuck disk. They are written one way: the test stalls
the thing that must not be waited on (300 ms to 3 s, or until the test
lets it go), and the bound is half that stall or less. A wait on the
stall fails by a wide margin; a slow runner cannot reach the bound. The
few that measure a rate (the pixel thread, the flame link's 40 Hz, the
Art-Net clock under load) judge the machine first, over the same second,
and only apply their bounds on a machine that could keep a 25 ms sleep
at the time. The numbers are always printed; the ones that matter are
the soak's, on the show PC.

## The bench soak

`packaging/windows/soak.py` (bench build only): blocks of 1 h 50 min,
a 7:24 show every 15 minutes, MadMapper and BEYOND started cold between
blocks, every output timed on the PC. It is the only test of real timing
on the real machine, and the show PC's hours before 9 October are the
scarce resource. The order that pays, from the audit:

1. One 8 hour block run of the current bench build with containment ON
   (the setting that goes to the show), all programs, arming exercised:
   the configuration the show will actually run.
2. The same with the Stream Deck plugged in and the real operator flow
   (sign in, arm by holding, Hold, Resume, Abort, Reset) at least once
   per block, by hand, with the soak's judge watching flamesafe's sACN.
3. A 24 hour run, containment ON, unattended, for the slow leaks: memory,
   handles, the journal's disk use, the 120-day pruning, MadMapper's
   cold-start stalls on block boundaries.
4. Only then, and only if a change lands: a 1 hour re-run per change
   that touches the conductor, the flame link, flamesafe or the
   scheduler.

Not worth the PC's time: containment OFF runs (the decision is made),
runs on a build that is not the one going to the show, repeated 1 hour
runs of the same build, and anything the suite can prove on a fake
clock.
