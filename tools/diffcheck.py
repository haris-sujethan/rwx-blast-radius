"""Differential soundness check of the shortcuts.

For every patch: each candidate test that would run filtered (per fixture case / per query) is
run both filtered and in full; and when the TPC fingerprint says "unchanged", the TPC executor
test is run in full. Any test that fails in full but passes (or is skipped) via a shortcut is
reported as UNSOUND.

Usage: diffcheck.py <patch files...>
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
import tpcfp  # noqa: E402
from candidates import MapDB, bits, select  # noqa: E402
from units import FULL, SKIP, UnitDB  # noqa: E402

db = MapDB(os.path.join(W, "map.bin"), REPO)
udb = UnitDB(os.path.join(W, "selector", "units.bin"))
tpc_orig = json.load(open(os.path.join(W, "tpcfp.json")))
EXEC_TPC = {"tests/test_executor.py::TestExecutor::test_execute_tpcds": tpcfp.TPCDS,
            "tests/test_executor.py::TestExecutor::test_execute_tpch": tpcfp.TPCH}

M = os.environ.get("DIFF_MIRROR", "/private/tmp/claude-501/mdiff")
shutil.rmtree(M, ignore_errors=True)
mirror = runner.Mirror(REPO, M)
runner.prepare_base(M)
runner.preload(["numpy", "pandas", "duckdb", "pandas.testing", "sqlglot"])


def spec(n):
    parts = db.ids[n].split("::")
    return (n, parts[0][:-3].replace("/", "."), parts[1], parts[-1])


bad = []
for p in sys.argv[1:]:
    cid = os.path.basename(p)[:-6]
    sel = select(db, open(p).read())
    cands = list(bits(sel.cands))
    checks = []  # (n, filter or "skip")
    for n in cands:
        plan, _ = udb.plan(db.ids[n], 1 << n, sel.req, sel.fixture_texts)
        if plan == SKIP:
            checks.append((n, "skip"))
        elif plan != FULL:
            checks.append((n, plan))
    tpc = []
    for n in cands:
        rel = EXEC_TPC.get(db.ids[n])
        if rel is None:
            continue
        ut = udb.tests.get(db.ids[n])
        if tpcfp.safe_for(sel.files, sel.req, ut.get("execlines") if ut else None):
            tpc.append((n, rel))
    if not checks and not tpc:
        continue
    files = dict(sel.files)
    mirror.apply(files)
    t0 = time.time()
    try:
        loaded = runner.loaded_files(M + os.sep)
        purge = any(f in loaded for f in files)
        for n, plan in checks:
            to = {n: 120}
            full = runner.run_tests([spec(n)], purge=purge, timeouts=to).get(n, True)
            if plan == "skip":
                filt = False
            else:
                filt = runner.run_tests([spec(n)], purge=purge, timeouts=to,
                                        filters={n: plan}).get(n, True)
            if full and not filt:
                bad.append((cid, db.ids[n], "unit"))
                print(f"UNSOUND {cid} {db.ids[n]} unit plan={plan if plan == 'skip' else {k: sorted(v)[:10] for k, v in plan.items()}}", flush=True)
        for n, rel in tpc:
            orig = {int(k): v for k, v in tpc_orig[rel].items()}
            res = runner.run_func(tpcfp.fingerprints, (rel, sorted(orig)), purge=purge, timeout=120)
            if res is None:
                continue
            changed = {i for i in orig if res.get(str(i)) != orig[i]}
            full = runner.run_tests([spec(n)], purge=purge, timeouts={n: 300}).get(n, True)
            if full and not changed:
                bad.append((cid, db.ids[n], "tpc"))
                print(f"UNSOUND {cid} {db.ids[n]} tpc (fingerprints unchanged but test fails)", flush=True)
            elif full:
                print(f"ok {cid} tpc changed={len(changed)} full_fails", flush=True)
    finally:
        mirror.restore()
    print(f"checked {cid}: {len(checks)} unit, {len(tpc)} tpc, {time.time() - t0:.0f}s", flush=True)
print("UNSOUND TOTAL", len(bad), bad)
