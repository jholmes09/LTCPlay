#!/usr/bin/env bash
# Which jobs of a mutation sweep are red, in the run's summary and, for the
# record sweep, in an issue, so a red sweep is never silent. Used by the
# report job of nightly-mutate.yml and weekly-mutate.yml, which always runs.
#
#   GH_TOKEN REPO RUN_ID RUN_URL  the run
#   WHAT      "nightly mutation sweep" or "weekly mutation sweep"
#   LABEL     the issue label (mutate-nightly, mutate-sweep)
#   OFFICIAL  "true" for the record sweep (scheduled, or by hand on
#             bench-build with no base of its own). Only it opens, adds to
#             or closes the issue: a sweep of another branch, or of a
#             narrower base, can neither raise nor clear bench-build's alarm.
#   SWEPT     what was swept, for the issue ("bench-build at <commit>")
#   PLAN      the plan job's result; anything but success is red (a bad
#             repository variable, mutate.py crashing, a list too big)
#   MODE      the plan's mode: none means nothing to run, green
#   SELFTEST  the selftest job's result (the shards need it green)
#   NSHARDS   how many shards the plan asked for, per OS
#   NOSES     how many OSes (default 1)
#   CAPPED, SELECTED   a nightly over its cap ran only the safety core and
#             the touched safety files: said in the summary and the issue
#   JOB_PREFIX  the shard jobs' name before the matrix, "sweep ("
#
# Red: the plan not green, the selftest job not green, any shard not green,
# or fewer shard jobs than the plan asked for. Exit 1 when red. The issue
# lookup or write failing never turns a red sweep green.
set -uo pipefail
NOSES="${NOSES:-1}"
jobs="$(mktemp)"
if ! gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" \
       --jq '.jobs[] | [.name, (.conclusion // .status), .html_url] | @tsv' > "$jobs"; then
  echo "::error::cannot read this run's jobs; counting the sweep as red"
  : > "$jobs"
  export JOBS_UNREAD=1
fi
report="$(mktemp)"
python3 - "$jobs" "$report" <<'PY'
import os, sys

env = os.environ.get
prefix, what = env("JOB_PREFIX"), env("WHAT")
url = env("RUN_URL")
red, shards, note = [], [], []
if env("PLAN") != "success":
    red.append(("plan", env("PLAN") or "missing", url))
    note.append("The plan job failed, so nothing was swept: a repository "
                "variable that is not a whole number, mutate.py unable to "
                "list its mutations, or a list too big for one matrix. Its "
                "log says which.")
elif env("MODE") == "none":
    pass
else:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3 and parts[0].startswith(prefix):
                shards.append(parts)
    if env("JOBS_UNREAD"):
        red.append(("this run's jobs", "unreadable", url))
    if env("SELFTEST") != "success":
        red.append(("selftest", env("SELFTEST") or "missing", url))
    red += [s for s in shards if s[1] not in ("success", "skipped")]
    try:
        want = int(env("NSHARDS") or 0) * int(env("NOSES") or 1)
    except ValueError:
        want = 0
    if want > len(shards):
        red.append((f"{want - len(shards)} of the {want} shards", "no job", ""))
if env("CAPPED") == "true":
    note.append(f"Capped: {env('SELECTED')} mutations were selected, over "
                f"MUTATE_NIGHTLY_MAX, so this night ran only the safety "
                f"core and the touched safety files; the rest is left to "
                f"the weekly sweep.")
out = []
if not red:
    out.append(f"### {what}: green" + (" (nothing to run)" if env("MODE") == "none"
               else f", selftest and all {len(shards)} shards"))
else:
    out += [f"### {what}: red ({len(red)})", "",
            "| job | result | link |", "|---|---|---|"]
    out += [f"| {n} | {c} | " + (f"[open]({u})" if u else "none") + " |"
            for n, c, u in red]
    out += ["",
            "A red selftest means the shards measured nothing: fix the suite "
            "first. Each red shard's own summary (under its job) says why: "
            "the kind of failure and the mutation or test. Its annotations "
            "say the same, and its full log is the artifact named "
            "`mutate-log-<os>-shard-<n>`, kept 7 days. TESTING.md says what "
            "each kind of failure means and what to do about it."]
if note:
    out += [""] + note
text = "\n".join(out) + "\n"
print(text)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
    fh.write(text)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(text)
PY
if grep -q '^### .*: red (' "$report"; then red=1; else red=0; fi
if [[ "${OFFICIAL:-}" != true ]]; then
  echo "not the record sweep (another branch, or a base of its own): the issue is left alone"
  exit "$red"
fi
gh label create "$LABEL" --repo "$REPO" --color B60205 \
  --description "the $WHAT is red" --force > /dev/null 2>&1 || true
open_issue=$(gh issue list --repo "$REPO" --label "$LABEL" --state open \
               --json number --jq '.[0].number // empty' 2>/dev/null || true)
if (( red )); then
  body="The $WHAT of $SWEPT is red: [run $RUN_ID]($RUN_URL).

$(cat "$report")

Fix the test (or refresh the mutation) and merge; the next green sweep closes this issue. Re-run a timed-out shard from the run page."
  if [[ -n "$open_issue" ]]; then
    gh issue comment "$open_issue" --repo "$REPO" --body "$body" || true
    echo "::error::the $WHAT is red; added to issue #$open_issue"
  else
    num=$(gh issue create --repo "$REPO" --title "The $WHAT is red" --label "$LABEL" --body "$body" || true)
    echo "::error::the $WHAT is red; opened ${num:-an issue (the write failed)}"
  fi
  exit 1
fi
if [[ -n "$open_issue" ]]; then
  gh issue comment "$open_issue" --repo "$REPO" --body "The $WHAT of $SWEPT is green again: [run $RUN_ID]($RUN_URL). Closing." || true
  gh issue close "$open_issue" --repo "$REPO" || true
  echo "the sweep is green; closed issue #$open_issue"
else
  echo "the sweep is green"
fi
