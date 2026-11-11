#!/usr/bin/env bash
# A whole mutation sweep on main is never silent. Red: open the issue
# labelled mutate-sweep (or comment on the open one) with the red shards,
# and fail this step. Green: close that issue, since the whole list now
# passes on main. Used by weekly-mutate.yml (Sundays, and by hand) and by
# selftest.yml's mutate-report after a push to main or a release tag.
#
#   sweep_issue.sh WHAT REPORT_FILE RED_COUNT
#
# WHAT names the sweep ("The weekly mutation sweep"); REPORT_FILE is the
# markdown table of red shards; RED_COUNT is how many are red (no shard at
# all counts as red). Needs GH_TOKEN, REPO and RUN_URL; issues: write.
set -u
what="$1"; report="$2"; red="$3"
label=mutate-sweep
gh label create "$label" --repo "$REPO" --color B60205 \
  --description "a whole mutation sweep on main is red" --force 2>/dev/null || true
open_issue=$(gh issue list --repo "$REPO" --label "$label" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true)
if (( red > 0 )); then
  body="$what is red: [run]($RUN_URL).

$(cat "$report")

While this is open, every release with any code in it runs the whole mutation list before it merges (release_gate.py), and nothing should be tagged. Fix the test (or refresh the mutation), merge, and the next green whole sweep closes this issue. A shard that only timed out: re-run the red shards from the run page."
  if [[ -n "$open_issue" ]]; then
    gh issue comment "$open_issue" --repo "$REPO" --body "$body"
    echo "::error::$what is red; added to issue #$open_issue"
  else
    num=$(gh issue create --repo "$REPO" --title "A whole mutation sweep on main is red" --label "$label" --body "$body")
    echo "::error::$what is red; opened $num"
  fi
  exit 1
elif [[ -n "$open_issue" ]]; then
  gh issue comment "$open_issue" --repo "$REPO" --body "$what is green again: [run]($RUN_URL). Closing."
  gh issue close "$open_issue" --repo "$REPO"
  echo "$what is green; closed issue #$open_issue"
else
  echo "$what is green"
fi
