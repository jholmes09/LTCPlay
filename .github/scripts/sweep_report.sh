#!/usr/bin/env bash
# Which shards of a mutation sweep are red, in the run's summary and in an
# issue, so a red sweep is never silent. Used by nightly-mutate.yml and
# weekly-mutate.yml (the report job).
#
#   GH_TOKEN REPO RUN_ID RUN_URL  the run
#   WHAT      "nightly mutation sweep" or "weekly mutation sweep"
#   LABEL     the issue label (mutate-nightly, mutate-sweep)
#   SWEPT     what was swept, for the issue ("bench-build at <commit>")
#   SELFTEST  the selftest job's result (the shards need it green)
#   NSHARDS   how many shards the plan asked for, per OS
#   NOSES     how many OSes (default 1)
#   JOB_PREFIX  the shard jobs' name before the matrix, "sweep ("
#
# Red: the selftest job not green, any shard not green, or fewer shard jobs
# than the plan asked for. Red opens an issue with LABEL, or adds to the
# open one, and exits 1. Green closes the open one. The issue lookup or
# write failing never turns a red sweep green: the exit code follows the
# sweep.
set -uo pipefail
NOSES="${NOSES:-1}"
jobs="$(mktemp)"
if ! gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/jobs?per_page=100" \
       --jq '.jobs[] | [.name, (.conclusion // .status), .html_url] | @tsv' > "$jobs"; then
  echo "::error::cannot read this run's jobs; counting the sweep as red"
  : > "$jobs"
fi
report="$(mktemp)"
python3 - "$jobs" "$report" <<'PY'
import os, sys

prefix = os.environ["JOB_PREFIX"]
what = os.environ["WHAT"]
shards = []
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 3 and parts[0].startswith(prefix):
            shards.append(parts)
red = [s for s in shards if s[1] not in ("success", "skipped")]
try:
    want = int(os.environ.get("NSHARDS") or 0) * int(os.environ.get("NOSES") or 1)
except ValueError:
    want = 0
if want > len(shards):
    red.append((f"{want - len(shards)} of the {want} shards", "no job", ""))
selftest = os.environ.get("SELFTEST", "")
if selftest != "success":
    red.insert(0, ("selftest", selftest or "missing", os.environ["RUN_URL"]))
out = []
if not red:
    out.append(f"### {what}: selftest and all {len(shards)} shards green")
else:
    out += [f"### {what}: red ({len(red)})", "",
            "| job | result | link |", "|---|---|---|"]
    out += [f"| {n} | {c} | " + (f"[open]({u})" if u else "none") + " |"
            for n, c, u in red]
    out += ["",
            "A red selftest means the shards measured nothing: fix the "
            "suite first. Each red shard's own summary (under its job) says "
            "why: the kind of failure and the mutation or test. Its "
            "annotations say the same, and its full log is the artifact "
            "named `mutate-log-<os>-shard-<n>`, kept 7 days. TESTING.md says "
            "what each kind of failure means and what to do about it."]
text = "\n".join(out) + "\n"
print(text)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
    fh.write(text)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(text)
PY
if grep -q '^### .*: red (' "$report"; then red=1; else red=0; fi
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
