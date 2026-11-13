#!/usr/bin/env bash
# A whole mutation sweep on main is never silent. Red: open the issue
# labelled mutate-sweep (or comment on the open one) with the red shards,
# and fail this step. Green: close that issue, but only when the sweep ran
# on the commit that is main's head right now (a green sweep of a branch,
# or of an older main, proves nothing about main today). Used by
# weekly-mutate.yml (Sundays, and by hand) and by selftest.yml's
# mutate-report after a push to main or a release tag.
#
#   sweep_issue.sh WHAT REPORT_FILE RED_COUNT
#
# WHAT names the sweep; REPORT_FILE is the markdown table of red shards;
# RED_COUNT is how many are red (no shard at all counts as red; empty or
# not a number counts as red). Needs GH_TOKEN, REPO, RUN_URL, REF (the
# run's git ref) and SHA (the commit it swept); issues: write.
#
# A run on any other branch (a manual weekly run on a PR branch) only
# says its result: it neither opens nor closes the issue about main.
set -u
what="$1"; report="$2"; red="$3"
[[ "$red" =~ ^[0-9]+$ ]] || red=1
label=mutate-sweep
case "$REF" in
  refs/heads/main|refs/tags/*) ;;
  *)
    if (( red > 0 )); then
      echo "::error::$what is red (on $REF, not main: the mutate-sweep issue is left as it is)"
      exit 1
    fi
    echo "$what is green (on $REF, not main: the mutate-sweep issue is left as it is)"
    exit 0 ;;
esac
gh label create "$label" --repo "$REPO" --color B60205 \
  --description "a whole mutation sweep on main is red" --force 2>/dev/null || true
if ! open_issue=$(gh issue list --repo "$REPO" --label "$label" --state open --json number --jq '.[0].number // empty'); then
  echo "::error::could not read the mutate-sweep issue; $what: $red red"
  exit 1
fi
if (( red > 0 )); then
  body="$what is red: [run]($RUN_URL).

$(cat "$report")

While this is open, every release with any code in it runs the whole mutation list before it merges (release_gate.py), and nothing should be tagged. Fix the test (or refresh the mutation), merge, and the next green whole sweep of main's head closes this issue. A shard that only timed out: re-run the red shards from the run page."
  if [[ -n "$open_issue" ]]; then
    gh issue comment "$open_issue" --repo "$REPO" --body "$body"
    echo "::error::$what is red; added to issue #$open_issue"
  else
    num=$(gh issue create --repo "$REPO" --title "A whole mutation sweep on main is red" --label "$label" --body "$body")
    echo "::error::$what is red; opened $num"
  fi
  exit 1
fi
if [[ -z "$open_issue" ]]; then
  echo "$what is green"
  exit 0
fi
head=$(gh api "repos/$REPO/commits/main" --jq .sha 2>/dev/null || true)
if [[ "$REF" == refs/heads/main && -n "$head" && "$head" == "$SHA" ]]; then
  gh issue comment "$open_issue" --repo "$REPO" --body "$what is green on main's head ($SHA): [run]($RUN_URL). Closing."
  gh issue close "$open_issue" --repo "$REPO"
  echo "$what is green on main's head; closed issue #$open_issue"
else
  echo "$what is green, but it swept $SHA on $REF and main's head is ${head:-unknown}: issue #$open_issue stays open until a green sweep of main's head"
fi
