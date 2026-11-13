#!/usr/bin/env python3
"""Tells a mutate shard that ran out of time apart from a missed mutation.

Run by the step after a red sweep step (selftest.yml, nightly-mutate.yml,
weekly-mutate.yml), with SHARD, N and LOG (the shard's mutate.log) set. A
miss prints NOT CAUGHT or UNEXPECTED MISS and the sweep reaches its
"caught N, missed M" line. A shard that ran out of time never reaches that
line, so this names every mutation of the shard that has no result in the
log. The mutation being run when the clock stopped is one of them: its
result is unknown, not "caught". Exit 1 for a timeout, 0 for anything else
(that failure is said by the sweep's own lines).
"""
import ast, os

shard, total = int(os.environ["SHARD"]), int(os.environ["N"])
log = []
try:
    with open(os.environ["LOG"], encoding="utf-8", errors="replace") as fh:
        log = [l.rstrip("\r\n") for l in fh]
except OSError:
    pass
if any(l.startswith("caught ") and ", missed " in l for l in log):
    print("the sweep finished; any failure is in its own lines above")
    raise SystemExit(0)
if any("The suite FAILS with nothing mutated" in l for l in log):
    print("the baseline failed; that is not a timeout")
    raise SystemExit(0)
if any(l.startswith("INFRASTRUCTURE FAILURE") for l in log):
    print("the unmutated suite did not finish, twice: an infrastructure failure, said above")
    raise SystemExit(0)
if any("nothing to run" in l or "cannot read the diff" in l for l in log):
    print("the shard stopped before any mutation ran; see its own lines above")
    raise SystemExit(0)
tree = ast.parse(open("mutate.py", encoding="utf-8").read(), "mutate.py")
names = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MUTATIONS" for t in node.targets):
        names = [e.elts[0].value for e in node.value.elts]
mine = [n for i, n in enumerate(names) if i % total == shard]
# In an affected run only part of the shard was due; the sweep
# says which ("would run" is --list's word, "to run" the sweep's).
due = None
for l in log:
    if l.startswith("shard ") and " mutations to run" in l:
        due = int(l.split(":")[1].split()[0])
done = set()
for l in log:
    for pre in ("  caught      ", "  NOT CAUGHT  "):
        if l.startswith(pre):
            done.add(l[len(pre):])
    if l.startswith("  SETUP FAIL  "):
        done.add(l[len("  SETUP FAIL  "):].split(" (pattern", 1)[0])
todo = [n for n in mine if n not in done]
if not todo or (due is not None and len(done) >= due):
    print("no mutation is missing a result; the shard stopped some other way")
    raise SystemExit(0)
head = (f"shard {shard}/{total} TIMED OUT, it did not finish: "
        f"{len(done)} of its {due if due is not None else len(mine)} mutations ran to a result. "
        "This is a timeout, not a missed mutation; re-run the shard.")
esc = lambda s: s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
print("::error title=mutate shard timed out::" + esc(head + " Not run: " + " | ".join(todo))[:3500])
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("### " + head + "\n\n" + "\n".join("- " + n for n in todo) + "\n")
print(head)
for n in todo:
    print("  did not run  " + n)
raise SystemExit(1)
