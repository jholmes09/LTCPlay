## What this does

<!-- One thing. A PR never changes show behavior under a title that says something else. -->

## Proof

- [ ] `python3 selftest.py`: every check passes. Last line:
- [ ] A PR into bench-build: selftest (ubuntu and windows), `changes` and
      (for packaging) `installer` are green. `ci` is red on such a PR by
      design; it judges only PRs into main.
- [ ] A PR into main (a release): CI's `ci` check is green: the suite on
      ubuntu and windows, and every mutate shard this change needed plus
      the safety core (TESTING.md says which run and why).
- [ ] Touches a safety file (`flamesafe/`, the conductor, the flame link,
      the deck, the scheduler, users, fuel, `trigger.py`, `output.py`,
      `player.py`, `session.py`, `clock.py`...)? On bench-build,
      `mutate-safety` ran that file's mutations against the expected-miss
      list (not required: read it before asking for the merge); on main,
      the shards ran them with every flamesafe, conductor and flame link
      mutation.

      `mutate.py --expected mutate_expected_misses.txt` is what CI runs: it
      fails if any mutation not on the list is missed, and it fails if any
      mutation on the list is now caught, so `mutate_expected_misses.txt`
      can only shrink, never grow. Mutate runs when the PR is marked ready
      for review, not on every push to a draft. Running
      `LTCPLAY_TEST_SHOW_DIR=<a copy of the show folder> python3 mutate.py`
      locally is recommended before a release, on the show PC, but is no
      longer required here.
