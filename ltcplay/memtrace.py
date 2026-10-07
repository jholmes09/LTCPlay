"""An opt-in memory diagnostic for the engine.

With LTC_TRACEMALLOC=1 in the engine's environment, `ltc serve` starts
Python's tracemalloc and, every 5 minutes, appends to FILENAME in its data
folder the 20 source lines holding the most memory and the 20 that grew
most since tracing began. Unset (or any other value), nothing is imported,
started or written. Added after the show PC soak of c8179eb (2026-10-06),
where the engine grew +271 MB an hour and only a long run on the show PC
could say from where.

Tracing costs CPU on every allocation: for a diagnostic run, never a show.
"""
import os
import threading
import time

ENV = "LTC_TRACEMALLOC"
FILENAME = "ltcplay_tracemalloc.txt"
EVERY_S = 300.0
TOP = 20
MAX_BYTES = 5 * 1024 * 1024     # then the file is moved to .old and begun again


class Tracer:
    def __init__(self, folder, every_s=EVERY_S):
        self.path = os.path.join(folder, FILENAME)
        self.every_s = every_s
        self._stop = threading.Event()
        self._first = None
        self._t0 = time.time()
        self._thread = None

    def write_once(self):
        """One report appended to the file. Never raises."""
        try:
            import tracemalloc
            snap = tracemalloc.take_snapshot().filter_traces((
                tracemalloc.Filter(False, tracemalloc.__file__),
                tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            ))
            if self._first is None:
                self._first = snap
            cur, peak = tracemalloc.get_traced_memory()
            lines = [f"== {time.strftime('%Y-%m-%d %H:%M:%S')} pid "
                     f"{os.getpid()}, {(time.time() - self._t0) / 60:.1f} min "
                     f"traced: {cur / 1e6:.1f} MB now, {peak / 1e6:.1f} MB "
                     f"peak",
                     f"-- top {TOP} by size:"]
            for st in snap.statistics("lineno")[:TOP]:
                lines.append(f"  {st.size / 1024:10.1f} KB {st.count:8d} "
                             f"blocks  {st.traceback[0]}")
            lines.append(f"-- top {TOP} growth since tracing began:")
            for st in snap.compare_to(self._first, "lineno")[:TOP]:
                lines.append(f"  {st.size_diff / 1024:+10.1f} KB "
                             f"{st.count_diff:+8d} blocks  "
                             f"{st.traceback[0]}")
            try:
                if os.path.getsize(self.path) > MAX_BYTES:
                    os.replace(self.path, self.path + ".old")
            except OSError:
                pass
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n\n")
        except Exception:
            pass

    def _run(self):
        self.write_once()               # the baseline, at once
        while not self._stop.wait(self.every_s):
            self.write_once()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ltcplay-tracemalloc")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)


def start(folder, env=None, every_s=EVERY_S):
    """The Tracer when `env` (os.environ) has LTC_TRACEMALLOC=1, else None
    and nothing done at all."""
    env = os.environ if env is None else env
    if env.get(ENV) != "1":
        return None
    import tracemalloc
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    print(f"Memory: LTC_TRACEMALLOC=1, the top {TOP} allocation sites go to "
          f"{os.path.join(folder, FILENAME)} every {every_s / 60:g} min.",
          flush=True)
    return Tracer(folder, every_s).start()
