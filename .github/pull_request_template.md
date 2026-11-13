## What this does

<!-- One thing. A PR never changes show behavior under a title that says something else. -->

## Proof

- [ ] `python3 selftest.py`: every check passes. Last line:
- [ ] CI's `ci` check is green: the suite on ubuntu and windows, and every
      mutate shard this change needed (TESTING.md says which run and why).
- [ ] Touches a safety file (`flamesafe/`, the conductor, the flame link,
      the deck, the scheduler, `trigger.py`, `output.py`, `player.py`,
      `session.py`, `clock.py`...)? The shards ran every flamesafe,
      conductor and flame link mutation against the expected-miss list.

      `mutate.py --expected mutate_expected_misses.txt` is what CI runs: it
      fails if any mutation not on the list is missed, and it fails if any
      mutation on the list is now caught, so `mutate_expected_misses.txt`
      can only shrink, never grow. Mutate runs when the PR is marked ready
      for review, not on every push to a draft. Running
      `LTCPLAY_TEST_SHOW_DIR=<a copy of the show folder> python3 mutate.py`
      locally is recommended before a release, on the show PC, but is no
      longer required here.
