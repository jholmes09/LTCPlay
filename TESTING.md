# How ltcplay is tested

For Jeff and for whoever works on this next. What runs when, how long it
takes, what a failure means, and how to run the whole thing on demand.
Written 2026-10-06, after the audit that simplified it; reworked
2026-10-15 after the second audit (the one that found the suite sound
and the mutation sweep too expensive to run every night).

## The three layers

1. **selftest.py** is the suite: 362 tests, about 3,300 checks of the
   program, from LTC decoding to the show conductor's Abort. It runs in
   about 2.5 minutes on a laptop and 3 to 4 minutes on a CI runner.
   Every PR runs it on ubuntu and on windows (the show PC's OS).
   `python3 selftest.py` runs it by hand; its last line is "all checks
   passed in N s" or a count of failures, each with a sentence, followed
   by "failed tests: ..." naming the test functions they were in.
   `python3 selftest.py --only test_a,test_b` runs just those.

2. **mutate.py** is the proof that the suite is honest. It breaks the
   program on purpose, one line at a time (861 breakages, each a real
   show-night failure: "Abort's flame cut waits for the executor", "a new
   socket for every frame"), and requires the suite to fail under each.
   A mutation the suite does not notice is a guarantee nobody is testing.
   A caught mutation costs one run of the suite plus a second look at the
   tests that failed (seconds), so the whole list is about 35 hours of
   runner time, split 32 ways.

3. **The bench soak** (`packaging/windows/soak.py`, the bench build)
   runs the whole Fire & Ice stack on the show PC for hours with nothing
   dangerous connected, and times every output on the PC's own clock.
   That is where real timing is measured. The suite above proves logic
   and sequence; it does not prove that the show PC meets 25 ms frames,
   because a shared CI runner cannot.

## What runs when

| When | What | How long | Required to merge |
|---|---|---|---|
| Every push to a PR | selftest on ubuntu and windows | 3 to 4 min | yes, through `ci` |
| A PR marked ready for review, and every push to it after | the mutate shards, each running only the mutations this PR can have changed the result of (below); as many shards as that needs, 32 at most, none at all for a doc or UI change | nothing for a doc; 40 min for a change to one file; about 1.5 h for a change to a safety file | yes, through `ci` |
| A push to main | the same, against the previous main | as above | not applicable |
| A release tag (`gpl-...` or a tech build) | every mutation, ubuntu and windows, 32 shards each | 2 to 4 hours | the tag is not cut until it is green |
| Every Sunday, 03:12 Eastern | every mutation on main, ubuntu, plus the Windows-only code on a windows runner (`weekly mutate` workflow) | 2 to 4 hours | a red sweep opens an issue; it is fixed before the next merge |
| On demand | the same, from the Actions tab: `weekly mutate`, Run workflow, pick ubuntu, windows or both | 2 to 4 hours | before any tech build |

**One required check.** Branch protection on main requires the `ci` job
of the selftest workflow and nothing else. `ci` is green only when the
suite passed on both OSes, the `changes` job decided what to run, and
every mutate shard that had to run passed (or none had to). It is red
when any of those failed or was cancelled. The shards are not named in
the protection, so their number can follow the work: a doc change starts
no shard job at all, a one-file change starts one or two, a release tag
starts 64. (Before 2026-10-15 the 32 shard names were each required, so
a doc PR had to spin up 32 jobs that did nothing, and the shard count
could never change.)

Draft PRs run only the selftest. The mutate shards, and `ci`, start when
the PR is marked ready for review.

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
  about 290 of them, whatever else changed;
- the mutation itself is new or edited in mutate.py;
- its line in `mutate_expected_misses.txt` changed;
- the test that caught it in the last weekly sweep was deleted or
  changed. The weekly sweep writes a **catchers file** (which test caught
  each mutation, the `catchers` artifact, kept 90 days) and the `changes`
  job reads the newest one, so a changed test re-runs exactly the
  mutations it guarded, in whatever file they break, and a mutation no
  sweep has recorded a catcher for (a new one) runs too. Without a
  catchers file (none written yet, or expired) a deleted test means the
  whole list runs, and a changed test re-runs only the mutations of the
  files in the diff, as before.

Everything else (docs, launchers, the brand, installers, this file, the
workflow) selects nothing, and no shard job is started: `ci` goes green
on the selftest alone.

The shard count is about one for every 12 selected mutations, so a
shard is about 40 minutes with its baseline, and 32 at most.

## How a mutation is confirmed

The suite is run once with the line broken. If it stays green the
mutation is NOT CAUGHT. If it goes red, the failure has to reproduce
before it counts: the tests that failed are run again on their own
(`selftest.py --only ...`), under the same mutation, and must fail with
a FAIL line in common with the first run (seconds); if they do not, the
whole suite is run again and must fail (minutes), the rule the sweep had
from the start. A test that fails for the runner's reasons passes one of
those and the mutation reads NOT CAUGHT (flaky), with the first failure
printed, never "caught".

## What a failure means

**selftest (ubuntu) or selftest (windows) red.** A check failed; the job's
annotations carry the sentence, and the job summary names the test. Read
the sentence first: it says what the program did and what it should have
done. The suite's real-time tests run on a virtual clock (below), so a
sentence that names a time is from one of the few rate measurements,
and those judge the machine first; a red one twice running is real.

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
- `shard N/M TIMED OUT`: the shard ran out of its 300 minutes. Not a
  miss; re-run the shard.
- `THE TREE IS NOT CLEAN`: a restore did not land. Should never happen;
  the shard's tree is thrown away, but say so.
- `windows only ... skipped on this OS`: not a failure. A mutation of
  Windows-only code (the one-copy lock through msvcrt) that only a
  windows runner can see break; the weekly sweep and every tag run it
  there.

**The weekly sweep red.** The same as a shard, on main. Its `report` job
opens an issue labelled `mutate-sweep` naming the red shards with a link
to each (or adds a comment to the one already open), and every PR's `ci`
job carries a warning while that issue is open. Whoever merges next
fixes it first; nothing is tagged while it is open. The next green sweep
closes the issue by itself.

## Running things by hand

- The suite: `python3 selftest.py`. A few tests: `python3 selftest.py
  --only test_flame_link_unit,test_conductor_abort_cuts_flames_at_once_and_fades_the_rest`.
- One mutation or a few: `python3 mutate.py abort` runs every mutation
  whose name contains "abort".
- What a change would run: `python3 mutate.py --changed origin/main
  --list` (or `--files ltcplay/conductor.py,selftest.py --list` with no
  git).
- The affected mutations of a change, for real: `python3 mutate.py
  --changed origin/main`.
- Everything, locally: `python3 mutate.py` (plan on most of a day; a
  shard of it is `--shard 3/32`). The Windows-only subset, on the show
  PC: `python mutate.py --windows`.
- Everything, in CI: Actions tab, `weekly mutate`, Run workflow. Choose
  `both` before a tech build.

While mutate.py runs, the working tree is deliberately broken (a
`.mutating` file marks it). Do not build, soak or run a show from that
tree until it says "tree restored byte for byte".

## Real time in the suite

The conductor's real-time tests (the review of PR #29: Abort never held
up by a slow BEYOND, a hung laser gate or an announcement's file read; a
press mid fade never lets the video rise; a stalled MadMapper sender is
a fault) run on REAL threads, the conductor's executor, MadMapper's
worker, BEYOND's helpers, and a VIRTUAL clock (`_VClock` in selftest.py):
every thread reads the same fake perf_counter and nobody sleeps on the
wall clock. A thread that sleeps blocks until the test moves the clock
past its deadline, and the test moves it in exactly the steps the
sleepers are waiting for, so a press "30 ms into a fade" lands exactly
there, and what used to be a bound in milliseconds ("the flame cut went
out within 75 ms") is an order of events ("the flame cut went out while
BEYOND's packet was still stuck"), which no runner's scheduling can
blur. The 1 s fades take no real time; those four tests went from 38 s
to about 1 s together, and they cannot flake on a slow runner.

A handful of checks still read the real clock, because what they prove
is a rate or a thread handoff through two real processes: the pixel
thread's frame interval, the Art-Net clock under load, the flame link
end to end against the real flamesafe over loopback UDP. They are
written one way: a stall is a thread really blocked, the bound is half
the stall or wider, and the rate checks judge the machine first, over
the same second, and only apply their bounds on a machine that could
keep a 25 ms sleep at the time. The numbers are always printed; the
ones that matter are the soak's, on the show PC.

flamesafe's own suite (`flamesafe/test_flamesafe.py`, on a fake clock
throughout) runs in its own process, started first thing so its 15 to
20 s of CPU overlap the rest of the run, and is read where it always
was.

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
