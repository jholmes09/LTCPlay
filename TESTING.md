# How ltcplay is tested

For Jeff and for whoever works on this next. What runs when, how long it
takes, what a failure means, and how to run the whole thing on demand.
Written 2026-10-06, after the audit that simplified it.

## The three layers

1. **selftest.py** is the suite: 377 checks of the program, from LTC
   decoding to the show conductor's Stop. On a CI runner it takes about
   15 to 20 minutes on ubuntu and 25 to 35 on windows (2026-11-03; the
   job limits are 45 and 60 minutes). Every PR runs it on ubuntu
   and on windows (the show PC's OS). `python3 selftest.py` runs it by
   hand; its last line is "all checks passed in N s" or a count of
   failures, each with a sentence.

2. **mutate.py** is the proof that the suite is honest. It breaks the
   program on purpose, one line at a time (973 breakages, each a real
   show-night failure: "Stop's flame cut waits for the executor", "a new
   socket for every frame"), and requires the suite to fail under each.
   A mutation the suite does not notice is a guarantee nobody is testing.
   A mutation is caught only when the suite fails under it twice. The
   baseline (nothing mutated) runs every test in file order; a mutated run
   stops after its first failing test, and runs first the test that caught
   that mutation last time (`mutate_catchers.json`), so most mutations
   take seconds, not two whole suites. Ubuntu runs three mutations at once
   (`--jobs 3`), each in its own copy of the tree with its own baseline.

3. **The bench soak** (`packaging/windows/soak.py`, the bench build)
   runs the whole Fire & Ice stack on the show PC for hours with nothing
   dangerous connected, and times every output on the PC's own clock.
   That is where real timing is measured. The suite above proves logic
   and sequence; it does not prove that the show PC meets 25 ms frames,
   because a shared CI runner cannot.

## What runs when

| When | What | How long | Required to merge |
|---|---|---|---|
| Every push to a PR | selftest on ubuntu and windows | 15 to 20 min (ubuntu), 25 to 35 min (windows); limits 45 and 60 | yes, through `ci` |
| A PR into bench-build marked ready for review, and every push to it after | the mutate shards, each running only the mutations this PR can have changed the result of (below); about 25 a shard, 96 shards at most, 16 at a time, none at all for a doc or UI change | nothing for a doc; 10 to 90 min for a change to a safety file | no |
| A release (a PR into main) marked ready, and every push to it after | the release gate's tier (below): docs, scoped or full | docs: the selftest only; scoped: about 3 hours (the safety core alone is about 900 mutations, one wave of 16 shards of about 57); full: 13 to 17 hours | yes, through `ci` |
| A push to main (a release just merged) | every mutation, ubuntu, after the fact; red opens the `mutate-sweep` issue | 11 to 14 hours | not applicable: it runs after the merge |
| A release tag (`gpl-...` or a tech build) | every mutation, ubuntu and windows, 96 shards each, 16 at a time | 11 to 14 hours on ubuntu; windows longer | the tag is not cut until it is green |
| Every Sunday, 03:12 Eastern | every mutation on main, ubuntu (`weekly mutate` workflow), 96 shards, 16 at a time | 11 to 14 hours | a red sweep opens an issue; it is fixed before the next merge |
| On demand | the same, from the Actions tab: `weekly mutate`, Run workflow, pick ubuntu, windows or both | 11 to 14 hours (ubuntu) | before any tech build |

**One required check.** Branch protection on main requires the `ci` job
of the selftest workflow and nothing else. `ci` is green only when the
suite passed on both OSes, the `changes` job decided what to run, and
every mutate shard that had to run passed (or none had to). It is red
when any of those failed or was cancelled. The shards are not named in
the protection, so their number can follow the work: a doc change starts
no shard job at all.

Draft PRs run only the selftest. The mutate shards, and `ci`, start when
the PR is marked ready for review.

**Why 96 shards, 16 at a time.** The whole list is about 4300 mutations.
On ubuntu with `--jobs 3` a shard spends 15 to 19 minutes on its
baseline and then 2.2 to 2.5 minutes a mutation (run 42577562836,
2026-11-09). With 32 shards of 135 that was 307 to 340 minutes, and 8 of
the first 14 ran out at their 340 minute limit with 128 to 132 done. With 96
shards of about 45 a shard ends in 115 to 135 minutes, so the 340 minute
limit is a backstop, not a target. The account runs about 20 jobs at
once; the shards take at most 16 of them (`max-parallel`), so 4 are always
left for other PRs' selftest and installer. 96 shards in waves of 16 is 6
waves: 11 to 14 hours for a whole ubuntu sweep.

## The release gate (a PR into main)

`release_gate.py` reads the diff between the release branch and main (from
their merge base) and picks one of three tiers. No person and no agent
picks it, and nothing in a PR's text changes it. The workflow runs the
copy of `release_gate.py`, `mutate.py` and `mutate_catchers.json` that is
on main, against the PR's diff (`--repo`), so a release can never lower its
own tier by editing them; with no copy on main, the tier is full.
`selftest.yml` itself comes from the PR on a `pull_request` run, so it is
protected by a different rule: any change under `.github/` is full, and
`.github/CODEOWNERS` names the owner for the gate's files. That needs
"Require review from Code Owners" turned on in main's branch protection
(Jeff, in Settings).

`ci` passes only for a PR into main whose run computed the tier for main.
On any other base it is red on purpose (it is not required there), so a
green `ci` cannot follow a PR that is moved to main afterwards: a base
change starts no run. Close and reopen the PR (or push) after moving it.

- **docs**: every changed file is Markdown, none is a file mutate.py
  breaks, and no test reads it (its name appears nowhere in selftest.py,
  a `test_*.py` or `tools/`: MORNING.md, CONTRACT.md and others are test
  input). The selftest on both OSes only.
- **scoped**: any other change. The mutations whose text lies within 25
  lines of a changed line (every mutation of a changed safety file), the
  mutations whose last catcher (`mutate_catchers.json`) is a test whose
  strings the change edited, and the safety core, always (`RELEASE_CORE`,
  about 900): every `remote_rules.js`, flamesafe arm input, link,
  service, rules and sACN, laser arm, fireball, fireball charge and cues,
  flame programming, fuel and fuel pump mutation; the conductor's, flame
  link's and composer's Stop, Abort, arm, gate and interlock mutations;
  the Run gate's; the deck's `arm:` keys; the remote's Stop, Hold and
  Abort; the morning arm and purge; who may arm (the hazer). One wave of
  16 shards. New tests may be added freely; an existing test may change
  only in its string literals and comments.
- **full**: every mutation. Forced when the change touches `mutate.py`,
  `mutate_catchers.json`, `mutate_expected_misses.txt`,
  `mutate_removed.txt`, `release_gate.py`, anything under `.github/` or
  `tools/` (the UI test rig and its fixtures), `test_show_fixtures.py`,
  `remote_rules.js`, or selftest.py outside its `test_` functions (the
  runner, `check()`, a shared rig); when an existing test is deleted or
  changes in anything but its strings, or a string in it becomes empty
  (a `return` at its top would gut it with nothing recorded as caught);
  when the
  `mutate-sweep` issue is open and any code changed; and whenever the
  gate errors, cannot read the diff, or a shard's own recomputed list
  differs from the one the changes job sized (that shard is red).

**What the scoped tier accepts.** It does not re-prove a mutation far
from the change whose behavior the change altered indirectly, a test
weakened only through a changed string (an expected value swapped for
another non-empty one), a mutation with no recorded catcher whose test's
strings the change edited, or two commits that only interact. Those are the cases the whole
sweep after the merge exists for: every push to main runs every mutation,
and so does every Sunday and every release tag. A red one opens the
`mutate-sweep` issue, every PR's `ci` warns, and the next release with any
code runs the full tier until a whole sweep is green again (which closes
the issue). So a gap can ship for at most the time one whole sweep takes
after the merge, and is never carried into the release after it.

`python3 release_gate.py --base origin/main` prints the tier and why.

**Mutations only leave on purpose.** A mutation that disappears from
mutate.py (deleted or renamed) fails the `changes` job of any PR unless
`mutate_removed.txt` has a `name | why` line for it
(`release_gate.py --check-removed`, the base branch's copy when it has
one).

## Which mutations a PR into bench-build runs

`mutate.py --changed BASE` works it out from the diff; the rules are in
`select_affected()` in mutate.py and the `changes` job prints them on
every run. A mutation runs when:

- the file it breaks is in the diff;
- any safety file is in the diff (anything under `flamesafe/`, the
  conductor, the flame link, the Stream Deck, the scheduler, BEYOND, the laser arm,
  MadMapper, the devices, the trigger, the output, the player, the
  session, the clock, the announcements, the show audio, the one-copy
  guard, the rig check before a show): then every flamesafe, conductor and flame link mutation runs,
  about 300 of them, whatever else changed;
- the mutation itself is new or edited in mutate.py;
- its line in `mutate_expected_misses.txt` changed;
- the test that caught it last time was deleted or changed, when a
  catchers file (`--catchers`) says which test that was. Without one, a
  deleted test means the whole list runs.

A change to only a test's body, with no source file of its own in the
diff, does NOT re-run the mutations of other files. That is the one
place the per-PR gate is deliberately incomplete, and the weekly sweep
exists to close it within the week: a test weakened without being renamed,
or a change in one file that quietly silenced a test of another, shows
up there, and a release tag runs everything regardless.

Everything else (docs, launchers, the brand, installers, this file, the
workflow) selects nothing, and no shard job is started at all, so the PR
can merge.

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
- `The suite FAILS with nothing mutated`: the baseline was red twice on
  that runner, the second time run alone (with `--jobs 3` the first
  baselines share one runner, its CPU and its loopback ports; the retry
  waits until the other copies' baselines are done). The log says whether
  both runs failed the same way (the code) or differently (a flaky test).
  Either way the shard measured nothing; see the selftest job.
- `FLAKY BASELINE` (a warning, the shard stays green): red once with
  nothing mutated, green when run again alone. The test it names is
  flaky; fix it, but this shard's verdicts stand.
- `INFRASTRUCTURE FAILURE`: the suite with nothing mutated did not finish
  inside 90 minutes, twice (it is run once more by itself after the first
  time). The runner, not the code: no mutation ran and none was missed;
  re-run the shard. A mutant's own suite is stopped at three times that
  shard's measured baseline, never under 300 s or over 90 minutes; a
  mutant stopped there counts as caught (it made the suite hang), and the
  job summary's "baseline time / mutant limit" column gives both numbers.
- `shard I/N TIMED OUT`: the shard ran out of its 340 minutes (the
  step's limit; the job's is 355). Not a miss; re-run the shard. A
  shard of a whole sweep normally takes 115 to 135 minutes, so a run of
  them timing out means slow runners: measure again and raise the
  shard count (`FULL_SHARDS` in selftest.yml and `NSHARDS` in
  weekly-mutate.yml, the same number).
- `THE TREE IS NOT CLEAN`: a restore did not land. Should never happen;
  the shard's tree is thrown away, but say so.

**A whole sweep on main red** (the weekly one, or the one after a push to
main or a release tag). The same as a shard, on main. Its report job
opens an issue labelled `mutate-sweep` naming the red shards with a link
to each (or adds a comment to the one already open) and goes red itself,
and every PR's `ci` job carries a warning while that issue is open. A
release with any code in it runs the full tier while it is open. Whoever
merges next fixes it first; nothing is tagged while it is open. Only a
green whole sweep of the commit that is main's head right now closes the
issue (`.github/scripts/sweep_issue.sh`); a green sweep of a branch or of
an older main leaves it open. A red suite after a push (so no shard ran),
a cancelled sweep, a missing shard or a report that could not be made
counts as red. A manual weekly run never cancels the Sunday one (they
are in separate concurrency groups), and a run on a branch only reports
its result: it neither opens nor closes the issue about main.

## Running things by hand

- The suite: `python3 selftest.py`.
- One mutation or a few: `python3 mutate.py stop` runs every mutation
  whose name contains "stop".
- What a change would run: `python3 mutate.py --changed origin/main
  --list` (or `--files ltcplay/conductor.py,selftest.py --list` with no
  git).
- The affected mutations of a change, for real: `python3 mutate.py
  --changed origin/main`.
- Everything, locally: `python3 mutate.py` (plan on most of a day; a
  shard of it is `--shard 3/96`).
- Everything, in CI: Actions tab, `weekly mutate`, Run workflow. Choose
  `both` before a tech build.
- The catcher hints: `python3 mutate.py --write-catchers
  mutate_catchers.json` adds the test each caught mutation stopped in. A
  stale or missing hint costs time, never a verdict: a hinted run counts
  only when it failed in that test, after that test passed run first with
  nothing mutated; anything else runs again in file order. A hinted catch
  is always confirmed by a second run in plain file order with no hint;
  green there is NOT CAUGHT (flaky), never caught. Refresh the file
  when a sweep gets slow.
- The rack screen drawn in a real browser (2026-10-24): the suite finds
  Chromium, Chrome or Edge by itself (set `LTCPLAY_CHROME` to one to
  choose), draws remote2.html headless at several window sizes with five
  banners and measures every flame group's Hold to arm and Disarm. With
  no browser it says so and skips; on the ubuntu CI runner a missing
  browser is a failure.

While mutate.py runs, the working tree is deliberately broken (a
`.mutating` file marks it). Do not build, soak or run a show from that
tree until it says "tree restored byte for byte".

## Real time in the suite

A handful of checks still read the clock, because what they prove is a
thread handoff: Stop does not wait behind a slow BEYOND, a hung
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
   (sign in, arm by holding, Hold, Resume, Stop) at least once
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
