"""Reference check of a full selector run: every candidate test the selector left out of a
commit's output is run in full on the patched tree. Any failure is a miss the selector made.

Usage: refcheck.py <pipeline_output.jsonl> <patch files...>
Prints one line per finding: MISS <commit> <test id>, and per commit stats.
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
out = {}
for line in open(sys.argv[1]):
    d = json.loads(line)
    out[d["commit"]] = {n for w in d["workers"] for n in w}

M = os.environ.get("REF_MIRROR", "/private/tmp/claude-501/mref")
shutil.rmtree(M, ignore_errors=True)
mirror = runner.Mirror(REPO, M)
runner.prepare_base(M)
runner.preload(["numpy", "pandas", "duckdb", "pandas.testing", "sqlglot"])
runner.FAIL_FAST = True


def spec(n):
    parts = db.ids[n].split("::")
    return (n, parts[0][:-3].replace("/", "."), parts[1], parts[-1])


misses = []
for p in sys.argv[2:]:
    cid = os.path.basename(p)[:-6]
    sel = select(db, open(p).read())
    cands = set(bits(sel.cands))
    chosen = out.get(cid, set())
    excluded = sorted(cands - chosen)
    if not excluded:
        print(f"checked {cid}: 0 excluded", flush=True)
        continue
    mirror.apply(dict(sel.files))
    t0 = time.time()
    try:
        loaded = runner.loaded_files(M + os.sep)
        purge = any(f in loaded for f in sel.files)
        res = runner.run_tests([spec(n) for n in excluded], purge=purge,
                               timeouts={n: 300 for n in excluded})
    finally:
        mirror.restore()
    bad = [n for n in excluded if res.get(n, True)]
    for n in bad:
        misses.append((cid, db.ids[n]))
        print(f"MISS {cid} {db.ids[n]}", flush=True)
    print(f"checked {cid}: {len(excluded)} excluded, {len(bad)} fail, {time.time() - t0:.0f}s",
          flush=True)
print("MISS TOTAL", len(misses), misses, flush=True)
