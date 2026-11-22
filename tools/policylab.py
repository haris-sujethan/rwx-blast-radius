"""Offline policy lab on full-truth data.

Usage: policylab.py <fulltruth jsons...> -- <kinds.json> <patch_dir> [pipeline_out.jsonl pipeline_err.log]
"""

import json
import os
import re
import sys
from collections import defaultdict

W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(W, "selector"))
from candidates import MapDB, select  # noqa: E402
from units import FULL, SKIP, UnitDB  # noqa: E402

args = sys.argv[1:]
sep = args.index("--")
ft = {}
for f in args[:sep]:
    ft.update(json.load(open(f)))
kinds = json.load(open(args[sep + 1]))
pdir = args[sep + 2]
pipe_out = args[sep + 3] if len(args) > sep + 3 else None
pipe_err = args[sep + 4] if len(args) > sep + 4 else None
F = 2.37

tests = {t["number"]: t for t in json.load(open(os.path.join(W, "..", "kit", "sample", "tests.json")))["tests"]}
d = {n: t["seconds"] for n, t in tests.items()}
db = MapDB(os.path.join(W, "map.bin"), os.path.join(W, "sqlglot"))
udb = UnitDB(os.path.join(W, "selector", "units.bin"))


def pack(nums):
    nums = sorted(nums, key=lambda n: -d[n])
    if not nums:
        return 0.0
    best = None
    for k in range(1, min(8, len(nums)) + 1):
        loads = [0.0] * k
        for n in nums:
            i = loads.index(min(loads))
            loads[i] += d[n]
        c = max(loads) + 2 * k
        best = c if best is None else min(best, c)
    return best


pipe = {}
if pipe_out:
    for line in open(pipe_out):
        o = json.loads(line)
        pipe[o["commit"]] = {n for w in o["workers"] for n in w}
pcpu = {}
if pipe_err:
    for line in open(pipe_err):
        m = re.search(r"\] (\S+): lvl=.* cpu=([\d.]+)", line)
        if m:
            pcpu[m.group(1)] = float(m.group(2))

tot = defaultdict(float)
bykind = defaultdict(lambda: defaultdict(float))
rows = []
for cid, rec in ft.items():
    k = kinds.get(cid, {}).get("kind", "?")
    C = {int(n) for n in rec}
    Fs = {int(n) for n, v in rec.items() if v[0]}
    sel = select(db, open(os.path.join(pdir, cid + ".patch")).read())
    cpu_full = sum(v[1] for v in rec.values())
    cpu_unit = 0.0
    for n, v in rec.items():
        n = int(n)
        plan, frac = udb.plan(db.ids[n], 1 << n, sel.req, sel.fixture_texts)
        if plan == SKIP:
            continue
        cpu_unit += v[1] * (frac if plan != FULL else 1.0)
    o = pack(Fs)
    b = pack(C)
    r = F * cpu_unit + o
    best = min(b, r)
    pc = pipe.get(cid)
    psim = pack(pc) if pc is not None else 0.0
    if pc is not None and Fs - pc:
        psim += 300
    pcost = psim + F * pcpu.get(cid, 0.0)
    for key, val in (("oracle", o), ("blind", b), ("runall", r), ("best", best),
                     ("pipe_sim", psim), ("pipe_total", pcost)):
        tot[key] += val
        bykind[k][key] += val
    bykind[k]["n"] += 1
    rows.append((pcost - o, cid, k, len(C), len(Fs), o, b, r, psim, pcost))

n = len(rows)
print(f"commits {n}  (per-40 scaling x{40 / n:.3f})")
for key in ("oracle", "blind", "runall", "best", "pipe_sim", "pipe_total"):
    print(f"  {key:10s} {tot[key]:8.1f}   per40 {tot[key] * 40 / n:7.1f}")
print("by kind:")
for k, v in sorted(bykind.items()):
    print(f"  {k:10s} n={int(v['n']):3d} oracle {v['oracle']:6.1f} blind {v['blind']:6.1f} runall {v['runall']:6.1f} "
          f"best {v['best']:6.1f} pipe {v['pipe_total']:6.1f}")
rows.sort(reverse=True)
print("worst pipeline excess:")
for r in rows[:15]:
    print(f"  {r[1]} {r[2]:10s} C={r[3]:4d} F={r[4]:3d} oracle {r[5]:6.2f} blind {r[6]:6.2f} runall {r[7]:6.2f} "
          f"pipe_sim {r[8]:6.2f} pipe_total {r[9]:6.2f}")
