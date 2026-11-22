"""Compare run-all vs blind-all vs per-commit best, given candidate sets and true failures.

Usage: strategy.py <answers.json> <patch_dir> [ratio...]
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
from candidates import MapDB, bits, select  # noqa: E402
from units import FULL, SKIP, UnitDB  # noqa: E402

W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
db = MapDB(os.path.join(W, "map.bin"), os.path.join(W, "sqlglot"))
udb = UnitDB(os.path.join(W, "selector", "units.bin"))
tests = {t["number"]: t for t in json.load(open(os.path.join(W, "..", "kit", "sample", "tests.json")))["tests"]}
costs = json.load(open(os.path.join(W, "selector", "costs.json")))
ans = json.load(open(sys.argv[1]))["commits"]
ratios = [float(x) for x in sys.argv[3:]] or [2.5]


def pack(nums):
    nums = sorted(nums, key=lambda n: -tests[n]["seconds"])
    if not nums:
        return 0.0
    best = None
    for k in range(1, min(8, len(nums)) + 1):
        loads = [0.0] * k
        for n in nums:
            i = loads.index(min(loads))
            loads[i] += tests[n]["seconds"]
        c = max(loads) + 2 * k
        best = c if best is None else min(best, c)
    return best


rows = []
for cid, a in ans.items():
    p = os.path.join(sys.argv[2], cid + ".patch")
    if not os.path.exists(p):
        continue
    sel = select(db, open(p).read())
    cands = set(bits(sel.cands))
    cpu = 0.0
    for n in list(cands):
        plan, frac = udb.plan(db.ids[n], 1 << n, sel.req, sel.fixture_texts)
        if plan == SKIP:
            cands.discard(n)
            continue
        c = costs.get(db.ids[n], tests[n]["seconds"])
        cpu += c * (frac if plan != FULL else 1.0)
    f = set(a["failing"])
    rows.append((cid, a.get("kind"), len(cands), cpu, pack(cands), pack(f), len(f)))

for r in ratios:
    runall = sum(x[3] * r + x[5] for x in rows)
    blind = sum(x[4] for x in rows)
    best = sum(min(x[3] * r + x[5], x[4]) for x in rows)
    oracle = sum(x[5] for x in rows)
    print(f"ratio {r}: commits {len(rows)} oracle {oracle:.1f} run-all {runall:.1f} "
          f"blind-all {blind:.1f} per-commit-best {best:.1f}")
rows.sort(key=lambda x: -(x[3]))
for x in rows[:15]:
    print(f"  {x[0]:8s} {x[1]:10s} cands {x[2]:4d} local_cpu {x[3]:6.2f} blind {x[4]:6.2f} "
          f"oracle {x[5]:6.2f} fail {x[6]}")
