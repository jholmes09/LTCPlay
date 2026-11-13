#!/usr/bin/env bash
# The verdict of `ci`, the one required check on main (selftest.yml's ci
# job). Kept in a file of its own so selftest.py can run it against
# made-up events (test_ci_verdict_rules) and nobody can weaken it quietly.
#
#   EVENT      github.event_name          ACTION   github.event.action
#   BASE_REF   github.base_ref            BASE_CHANGED  "true" when this
#              (the PR's base branch)               edit moved the base
#   SELFTEST, CHANGES, MUTATE   the needed jobs' results
#   MODE       the changes job's mode output (empty when it failed)
#   GH_TOKEN REPO RUN_ID HEAD_SHA   for an edit that did not move the base
#
# Green only when:
#   - it is a push, or a PR whose base is main (a green from a run on a PR
#     into another branch, or from before a retarget, can never count:
#     such a run is red here);
#   - and selftest and changes succeeded, the mode is known, and the shards
#     either had nothing to run (mode none, mutate skipped) or all passed;
#   - or, for an edit of a PR into main that did not move its base (its
#     title or text), the latest other `ci` verdict on the same commit was
#     green. The edit runs nothing else, and copies that verdict, so an edit
#     can neither hide a red result nor cost a run.
# Anything else, a lookup that fails included, is red.
set -uo pipefail
say_red() { echo "::error::$1"; bad=1; }
bad=0
echo "event: ${EVENT:-?} ${ACTION:-}, base: ${BASE_REF:-none}; selftest: ${SELFTEST:-?}, changes: ${CHANGES:-?}, mutate: ${MUTATE:-?} (mode ${MODE:-none given})"

case "${EVENT:-}" in
  push) ;;
  pull_request)
    if [[ "${BASE_REF:-}" != main ]]; then
      echo "::error::ci judges only PRs into main and pushes: this PR's base is '${BASE_REF:-none}'. A PR moved to main needs a new run (push, or close and reopen)."
      exit 1
    fi
    if [[ "${ACTION:-}" == edited && "${BASE_CHANGED:-}" != true ]]; then
      # Only the title or text changed: copy the newest other ci verdict
      # on this commit.
      verdict=$(gh api "repos/$REPO/commits/$HEAD_SHA/check-runs?check_name=ci&filter=all&per_page=100" \
                  --jq "[.check_runs[] | select(.status == \"completed\") | select((.details_url // \"\") | contains(\"/runs/$RUN_ID/\") | not)] | sort_by(.completed_at) | last | .conclusion // empty" 2>/dev/null) || verdict=""
      if [[ "$verdict" == success ]]; then
        echo "an edit of the PR's text: the last ci verdict on this commit was green"
        exit 0
      fi
      echo "::error::an edit of the PR's text: the last ci verdict on this commit is '${verdict:-not found}', not green. Push, or close and reopen, for a new run."
      exit 1
    fi
    ;;
  *) echo "::error::ci does not judge a '${EVENT:-}' event"; exit 1 ;;
esac

[[ "${SELFTEST:-}" == success ]] || say_red "selftest is not green (${SELFTEST:-missing})"
[[ "${CHANGES:-}" == success ]] || say_red "the changes job is not green (${CHANGES:-missing}): it could not say what this change needs, so nothing is assumed"
case "${MODE:-}" in
  none)
    [[ "${MUTATE:-}" == skipped ]] || say_red "mutate ran with nothing to run (${MUTATE:-missing})"
    echo "nothing this change can have broken is mutated: no shard ran" ;;
  affected|full)
    [[ "${MUTATE:-}" == success ]] || say_red "the mutate shards are not green (${MUTATE:-missing}); the mutate-report job lists the red ones" ;;
  *)
    say_red "the changes job gave no mode ('${MODE:-}'): nothing is assumed" ;;
esac
if (( bad )); then exit 1; fi
echo "ci: green"
