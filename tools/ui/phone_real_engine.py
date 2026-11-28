"""The phone page against the REAL engine handlers (Remote, Morning, the
confirm watchers), on the selftest's own rig, served for a real browser.
flamesafe and the Stream Deck program are played by this loop."""
import sys, threading, time, json
sys.path.insert(0, sys.argv[1]); import os; os.chdir(sys.argv[1])
import selftest as T
S = T._sched()
R = T._morning_rig(S, T._MO_CONFIG, link=T._FakeTimedLink())
R.m.timed_blockers = R.remote.timed_purge_blockers
taken = []; done = [R.remote._disarm_ids]
def loop():
    last = time.monotonic()
    while True:
        time.sleep(0.05)
        now = time.monotonic(); dt = now - last; last = now
        R.mono[0] += dt; R.wall[0] += dt
        try:
            q = ("taken=" + ",".join(map(str, taken))) if taken else ""
            del taken[:]
            d = R.remote.deck_input(q)
            for e in d["disarms"]:
                if e["id"] > done[0]:
                    done[0] = e["id"]
                    if 0 <= e["group"] < len(R.groups):
                        R.groups[e["group"]].update(armed="disarmed", wanted=False)
            for h in d["holds"]:
                g = R.groups[h["group"]]
                if h["fresh"] and h["held_s"] >= 1.0 and not g["wanted"]:
                    g.update(armed="armed", wanted=True); taken.append(h["id"])
            pv = R.m.purge.view()
            want = set(pv.get("groups_wanted") or [])
            for i, g in enumerate(R.groups):
                if i in want and g["armed"] != "armed":
                    g.update(armed="armed", wanted=True, purge=True)
                elif g.get("purge") and i not in want:
                    g.update(armed="disarmed", wanted=False, purge=False)
            R.feed()
            if R.m.purge.timed_running(): R.m.purge.timed_send()
            R.m.tick()
        except Exception as e:
            print("loop error", type(e).__name__, e, flush=True)
threading.Thread(target=loop, daemon=True).start()
threading.Thread(target=R.httpd.serve_forever, daemon=True).start()
print("PORT", R.httpd.server_address[1], flush=True)
while True: time.sleep(1)
