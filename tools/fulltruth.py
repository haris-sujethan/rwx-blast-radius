"""Run every candidate test of each patch in full (no shortcuts) and record outcome + CPU.

Usage: fulltruth.py <out.json> <patch files...>
out.json: {commit: {test_number: [failed(0/1), cpu_seconds]}}
"""

import json
import os
import shutil
import sys
import time

W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(W, "selector"))
REPO = os.path.join(W, "sqlglot")
os.environ["SKIP_INTEGRATION"] = "0"

import runner  # noqa: E402
from candidates import MapDB, bits, select  # noqa: E402

db = MapDB(os.path.join(W, "map.bin"), REPO)
OUT = sys.argv[1]
res_all = json.load(open(OUT)) if os.path.exists(OUT) else {}
M = os.environ.get("REF_MIRROR", "/private/tmp/claude-501/mfull")
shutil.rmtree(M, ignore_errors=True)
mirror = runner.Mirror(REPO, M)
runner.prepare_base(M)
runner.preload(["numpy", "pandas", "duckdb", "pandas.testing", "sqlglot"])
runner.FAIL_FAST = False


def spec(n):
    parts = db.ids[n].split("::")
    return (n, parts[0][:-3].replace("/", "."), parts[1], parts[-1])


for p in sys.argv[2:]:
    cid = os.path.basename(p)[:-6]
    if cid in res_all:
        continue
    sel = select(db, open(p).read())
    cands = sorted(bits(sel.cands))
    rec = {}
    if cands:
        mirror.apply(dict(sel.files))
        t0 = time.time()
        try:
            loaded = runner.loaded_files(M + os.sep)
            purge = any(f in loaded for f in sel.files)
            runner.CPU_LOG.clear()
            res = runner.run_tests([spec(n) for n in cands], purge=purge,
                                   timeouts={n: 120 for n in cands})
        finally:
            mirror.restore()
        for n in cands:
            rec[n] = [1 if res.get(n, True) else 0, round(runner.CPU_LOG.get(n, 0.0), 4)]
    res_all[cid] = rec
    json.dump(res_all, open(OUT, "w"))
    print(f"{cid}: {len(cands)} cands, {sum(v[0] for v in rec.values())} fail", flush=True)
print("DONE", flush=True)
