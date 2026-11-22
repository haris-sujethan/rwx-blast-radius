"""Evaluate a selector run: simulated seconds (exact scorer rules), misses, oracle, and the
estimated board total for an assumed server/local CPU ratio.

Usage: evaluate.py <tests.json> <answers.json> <output.jsonl> <local_cpu_seconds> [ratio]
"""

import json
import sys

tests = {t["number"]: int(round(t["seconds"] * 1000)) for t in json.load(open(sys.argv[1]))["tests"]}
ans = json.load(open(sys.argv[2]))["commits"]
out = {}
for line in open(sys.argv[3]):
    d = json.loads(line)
    out[d["commit"]] = [w for w in d["workers"] if w]
cpu = float(sys.argv[4])
ratio = float(sys.argv[5]) if len(sys.argv) > 5 else 2.5


def cost(workers):
    if not workers:
        return 0
    return max(sum(tests[n] for n in w) for w in workers) + 2000 * len(workers)


def best(nums):
    nums = sorted(nums, key=lambda n: -tests[n])
    if not nums:
        return 0
    b = None
    for k in range(1, min(8, len(nums)) + 1):
        loads = [0] * k
        for n in nums:
            i = loads.index(min(loads))
            loads[i] += tests[n]
        c = max(loads) + 2000 * k
        b = c if b is None else min(b, c)
    return b


sim = oracle = 0
misses = 0
rows = []
for cid, a in ans.items():
    w = out.get(cid, [])
    sel = {n for x in w for n in x}
    f = set(a["failing"])
    miss = bool(f - sel)
    c = cost(w) + (300000 if miss else 0)
    o = best(f)
    sim += c
    oracle += o
    misses += miss
    rows.append((c - o, cid, a.get("kind"), c / 1000, o / 1000, len(f), len(sel), miss))
rows.sort(reverse=True)
print(f"simulated {sim / 1000:.1f}  oracle {oracle / 1000:.1f}  misses {misses}  "
      f"local_cpu {cpu:.1f}  est_total(x{ratio}) {sim / 1000 + cpu * ratio:.1f}")
for r in rows[:12]:
    if r[0] > 0:
        print(f"  {r[1]:8s} {r[2]:10s} cost {r[3]:6.2f} oracle {r[4]:6.2f} fail {r[5]:3d} sel {r[6]:4d}"
              f"{'  MISS' if r[7] else ''}")
