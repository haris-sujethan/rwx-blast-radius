import json, os, sys, time, shutil
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
import runner
REPO = os.path.abspath(sys.argv[1]); tests = json.load(open(sys.argv[2]))["tests"]; out = sys.argv[3]
M = "/private/tmp/claude-501/mirror_measure"
shutil.rmtree(M, ignore_errors=True)
t0 = time.process_time()
mir = runner.Mirror(REPO, M)
print("mirror cpu", time.process_time() - t0, file=sys.stderr)
runner.prepare_base(M)
import numpy, pandas, duckdb  # noqa
runner.preload(["sqlglot", "tests.helpers", "tests.dialects.test_dialect"])
byfile = {}
for t in tests:
    byfile.setdefault(t["file"], []).append(t)
res = {}
for f, ts in byfile.items():
    mod = f[:-3].replace("/", ".")
    spec = [(t["number"], mod, t["id"].split("::")[1], t["id"].split("::")[-1]) for t in ts]
    c0 = os.times()
    r = runner.run_tests(spec, timeouts={t["number"]: 120 for t in ts})
    c1 = os.times()
    child = (c1.children_user + c1.children_system) - (c0.children_user + c0.children_system)
    fails = [n for n, v in r.items() if v]
    print(f"{f}: n={len(ts)} child_cpu={child:.2f} sum_test_cpu={sum(runner.CPU_LOG.get(t['number'],0) for t in ts):.2f} recorded={sum(t['seconds'] for t in ts):.2f} fails={fails}", file=sys.stderr)
    res.update({t["number"]: runner.CPU_LOG.get(t["number"]) for t in ts})
json.dump(res, open(out, "w"))
