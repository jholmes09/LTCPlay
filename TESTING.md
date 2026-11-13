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
   (`--jobs 3`), each in its own copy of the tree. In CI a shard trusts the
   green selftest job it waited for (`--trust-baseline`) instead of running
   the whole suite again in every copy: see "Shards and the baseline".

3. **The bench soak** (`packaging/windows/soak.py`, the bench build)
   runs the whole Fire & Ice stack on the show PC for hours with nothing
   dangerous connected, and times every output on the PC's own clock.
   That is where real timing is measured. The suite above proves logic
   and sequence; it does not prove that the show PC meets 25 ms frames,
   because a shared CI runner cannot.

## What runs when

Since the CI diet (2026-11-13). The audit behind it: the mutate shards
were 91 percent of the account's runner time (68,800 of 75,200 runner
minutes in the 7 days to 2026-11-13), half of that in runs a newer push
cancelled, and they held the 20 job slots so the required selftests
queued for up to 105 minutes. In the logs that could still be read
(2026-09-22 to 2026-11-13), every unexpected NOT CAUGHT came from a PR's
own affected run; no whole sweep ever found one.

| When | What | How long | Required to merge |
|---|---|---|---|
| Every push to a PR (drafts too) | selftest on ubuntu and windows, and `changes`, which prints what the mutation sweep would run for this change | 15 to 20 min (ubuntu), 25 to 35 min (windows); limits 45 and 60 | yes (both, and `changes`) |
| A PR into bench-build, or stacked on another PR, ready for review, that touches a safety file (flamesafe, the conductor, the flame link, laserarm, lasertest, users, fuel*, fireball*, flameprog, hazers, hazertest, morning, remote_rules.js) | `mutate-safety`: that file's own mutations (`mutate.py --targets`), and the whole safety core (553) only when a core file itself changed; at most 4 shards at once (the reserve, `MUTATE_SAFETY_AT_ONCE`) | about 25 min to 1 hour for one file; about 6 to 7 hours for the core | no |
| Any other PR into bench-build (docs, wording, the page, other code) | no mutate shards: the nightly sweep runs them | none | not applicable |
| A PR into main (a release), once it is ready for review | the mutate shards: the mutations this release can have changed the result of, plus every flamesafe, conductor and flame link mutation (`--with-core`), ubuntu, about 25 a shard, at most 30 shards (at most 45 a shard), 16 at a time on the free plan | about 2 hours for the core alone; 4 to 8 hours for a big release | yes: `ci`, the one required check on main |
| A push to main, an `ltc-...` release tag | selftest only (a tag also builds the installer, windows-app.yml); no mutation sweep: the release PR ran it on the same code | 15 to 35 min | the tag is cut from a green main |
| A `gpl-...` tag (the old release name) | refused: the `changes` job fails with the reason, nothing is swept or built | seconds | not applicable |
| Sunday to Friday evenings, 19:17 Mountain Standard Time (20:17 in summer; 02:17 UTC) | `nightly mutate`: what bench-build changed since the last green night (`mutate.py --changed <that commit>`), ubuntu, after one selftest. Over `MUTATE_NIGHTLY_MAX` (default 1,500) selected mutations it runs only the whole safety core and the touched safety files, and leaves the rest to the weekly sweep | nothing on a quiet day; at most about 4,200 runner minutes and 4.5 hours on 16 slots (uncapped, the heaviest night of the last week, 4,182 mutations, would have been about 11,700 runner minutes and 12 hours) | a red night opens a `mutate-nightly` issue; fix it before the next release |
| Saturday evenings, 19:12 Mountain Standard Time (02:12 UTC Sunday) | `weekly mutate`: every mutation on bench-build, ubuntu, after one selftest | 12 to 16 hours on the free plan (ends Sunday), about 6 with 40 slots | a red sweep opens a `mutate-sweep` issue; every PR into main warns while it is open |
| On demand | `nightly mutate` or `weekly mutate` from the Actions tab, Run workflow: any branch; weekly can run windows or both (the only whole run on windows, before a tech build) | as above | before any tech build |

Draft PRs run only the selftest and `changes`; a PR into main starts its
shards when it is marked ready for review.

**What a PR into bench-build gives up.** A test gap a PR opens outside the
safety files is no longer found before that PR merges; the nightly sweep
finds it within a day, and the fix is a follow-up PR. A PR that touches a
safety file still gets that file's mutations before the merge
(`mutate-safety`, not required to merge, so read it before asking for
the merge).

**Only the record sweep counts.** The scheduled night (or Run workflow on
bench-build with no base of its own) is the only one that opens or closes
the `mutate-nightly` issue and writes the "green at <commit>" job the next
night compares with. A sweep of another branch, or with a narrower base,
reports in its own summary only. The report always runs: a plan that
failed (a repository variable that is not a number, mutate.py crashing, a
list too big for the 256 jobs one matrix holds) is red like a missed
mutation. A scheduled sweep dropped from the queue before it could report
opens the issue too (`sweep-dropped.yml`).

**`ci` on main.** Green only for a push or a PR whose base is main, with
selftest, changes and every needed shard green
(`.github/scripts/ci_verdict.sh`, pinned by `test_ci_verdict_rules`). A
run on a PR into another branch is red, so a PR moved to main always needs
a run of its own: main's copy of selftest.yml listens for edits, and an
edit that moves the base runs everything again; an edit of the title or
text runs nothing and copies the last verdict on the commit.

**How many shards, how many at once.** `.github/scripts/mutate_plan.py`
decides, for all three workflows: about 25 mutations a shard, at most
`MUTATE_SHARDS` shards (default 30), but never more than 45 a shard (about
2.8 minutes of a shard a mutation, measured 2026-11-13, so 45 end in about
125 minutes, far inside the 340 minute limit). At most `CI_JOB_SLOTS`
minus `CI_SLOT_RESERVE` run at once (defaults 20 and 4: 16), so 4 slots
are always free for other PRs' selftest and installer. These three are
repository variables (Settings, Secrets and variables, Actions,
Variables): with a plan of 40 slots, set `CI_JOB_SLOTS` to 40 and 36
shards run at once.

**Shards and the baseline.** A shard needs the whole suite green with
nothing mutated before any mutation counts (a red suite makes every
mutation look caught). Before the diet every shard ran the whole suite in
each of its three copies first, 15 to 19 minutes of every shard (11,900
runner minutes in the 7 days to 2026-11-13). The selftest job of the same
run has already proved that on the same commit, so the shard passes
`--trust-baseline 1200` and each copy runs only the tests its mutations
are hinted to, first, nothing mutated (a minute or two). A caught
mutation's verdict still needs every test it stopped in to be proved green
with nothing mutated in that copy; when one is not, that copy runs its
whole baseline after all, and a red one stops the shard as before. Without
hints, or with a red sanity run, the copy runs its whole baseline.

**Release tags are ltc-YYYY.MM.DD.N** (from 2026-11-14; before that they
were gpl-..., and the Dollywood Mac stays at gpl-2026.09.15.10). Only an
ltc-* tag builds the installer (windows-app.yml). A new gpl-* tag builds
nothing, on purpose, so nobody can cut a release under the old name; the
selftest workflow's `changes` job fails for it with a sentence saying to
use ltc- (`mutate_plan.py mode`), so it is never silent. `test_release_tags_are_ltc_only` fails
if a workflow trigger names a gpl- tag again. The release steps are in
packaging/windows/INSTALL.md, "Make a release (Jeff)".

## Which mutations a PR runs

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
place the selection is deliberately incomplete, and the weekly sweep
exists to close it: a test weakened without being renamed, or a change in
one file that quietly silenced a test of another, shows up there.

Everything else (docs, launchers, the brand, installers, this file, the
workflow) selects nothing. The same rules pick what the nightly sweep runs
(against the last green night) and what a release PR runs (against main,
plus the safety core).

## What deserves a mutation (from 2026-11-13)

A mutation costs every sweep about 6 minutes of runner time, more when
the test that catches it comes late in the suite or draws the page in a
browser, and the list was growing by 10 to 40 a PR. From now on, add a
mutation only for **safety logic**:

- arming and disarming, Hold to arm, the deck's arm keys;
- Stop, Hold, Resume, Abort, the Run gate, every interlock and refusal;
- permissions: who may do what (users.py, the page's sign-in);
- what reaches the rig: flame, fireball, laser, hazer and pump output,
  flamesafe and the flame link, blanking and black;
- fuel and haze accounting, the charge record, any arithmetic a show
  decision rests on;
- persistence: atomic writes, the journal, settings and schedule saves
  that must survive a crash.

Safety logic includes what the program tells the operator to do at a
safety moment: an arming refusal and its reason, an alert that a group is
still armed, the sentence that says a Stop did not go, a fuel warning.
Mutate that the message is raised, refused or shown when it must be, and
that it says the thing the operator acts on (armed or not, which group,
what to press); not the exact wording around it.

Not for wording, layout or looks: the phrasing of a sentence, a label, a
title, a colour, a size in px, which card shows on which screen. The
selftest's own checks cover those (an exact-text check is fine there); a
mutation that only proves a string is that string adds time to every
sweep. When in doubt, ask whether a show could fire, fail to stop, lose a
record, or leave the operator not knowing what to do if the line were
wrong. If not, no mutation. The wording and layout mutations already
in the list stay until Jeff approves the list of candidates to move or
remove.


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
  them timing out means slow runners: measure again and lower
  `MOST_PER_SHARD` in `.github/scripts/mutate_plan.py` (one number for
  all three workflows).
- `THE TREE IS NOT CLEAN`: a restore did not land. Should never happen;
  the shard's tree is thrown away, but say so.

**The nightly or weekly sweep red.** The same as a shard, on bench-build,
and an open issue (`mutate-nightly` or `mutate-sweep`) says which. A red
selftest in the sweep means its shards never ran. Fix it before the next
release; a red night is run again every night (it compares with the last
green one) until it is green, which closes its issue.

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
  `both` before a tech build. What changed since a commit:
  `nightly mutate`, Run workflow, with that commit as the base.
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
