# Removing interface 1.0: done

Interface 1.0 (`remote.html`) is removed. Its screens were ported to the
one screen, remote2.html, PR by PR (P1 to P16, Jeff, 2026-10-28 to
2026-11-03): every route 1.0 pressed, with the same body, role and
refusal, is pressed from the one screen, and `/remote1`, `/ui` and the
`ui-default` setting are gone (R1). Where its code was copied word for
word, the copy is still pinned: `tools/pins/remote1_copied.txt` holds the
1.0 text the tests compare it with, and `_UI2_ROUTES` in selftest.py is
the fixed table of the screen's routes and role rows.
