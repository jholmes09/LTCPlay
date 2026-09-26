"""flamesafe: the flame safety program for Fire & Ice 2026.

The only writer of the flame universe.  A separate process from ltcplay,
sharing nothing but the localhost message format in CONTRACT.md.  This
package never imports ltcplay, and ltcplay never imports this package; a
test fails the build if either does.

Run it:  python -m flamesafe <config.json>
"""

__all__ = []
