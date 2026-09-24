## What this does

<!-- One thing. A PR never changes show behavior under a title that says something else. -->

## Proof

- [ ] `python3 selftest.py`: every check passes. Last line:
- [ ] CI is green on ubuntu, windows and macos.
- [ ] Touches `trigger.py`, `output.py`, `player.py` or `session.py`? Ran
      `LTCPLAY_TEST_SHOW_DIR=<a copy of the show folder> python3 mutate.py`
      locally: every mutation caught. Paste the summary line:

      CI cannot prove this part. The mutations in `mutate_expected_misses.txt`
      are caught only by tests that need the real show renders, and six of
      them are in `player.py` and `session.py`.
