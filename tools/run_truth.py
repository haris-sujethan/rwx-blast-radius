"""Ground truth for mutant patches: apply each to a scratch clone and run the full suite.

Usage: run_truth.py <scratch_clone> <patch_dir> <tests.json> <answers_out.json>
Writes answers in the kit format: {"commits": {id: {"failing": [...], "kind": ...}}}
"""

import json
import os
import subprocess
import sys
import time

CLONE, PATCHES, TESTS, OUT = sys.argv[1:5]
HERE = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(TESTS))["tests"]
id2num = {t["id"]: t["number"] for t in tests}
kinds = json.load(open(os.path.join(PATCHES, "kinds.json")))
answers = {"commits": {}}
if os.path.exists(OUT):
    answers = json.load(open(OUT))

env = dict(os.environ)
env["SKIP_INTEGRATION"] = "0"
env["PYTHONPATH"] = CLONE + os.pathsep + HERE
env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env["PATH"]

for f in sorted(os.listdir(PATCHES)):
    if not f.endswith(".patch"):
        continue
    cid = f[:-6]
    if cid in answers["commits"]:
        continue
    subprocess.run(["git", "reset", "--hard", "-q"], cwd=CLONE, check=True)
    subprocess.run(["git", "clean", "-fdq", "-e", "__pycache__"], cwd=CLONE, check=True)
    r = subprocess.run(["git", "apply", os.path.abspath(os.path.join(PATCHES, f))], cwd=CLONE)
    if r.returncode != 0:
        print(f"{cid}: apply failed", file=sys.stderr)
        continue
    failed_file = f"/private/tmp/claude-501/truth_failed_{cid}.txt"
    env["TRUTH_OUT"] = failed_file
    t0 = time.time()
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "8",
                    "-p", "truth_plugin", "--timeout", "120", "tests", "-x" if False else "-q"],
                   cwd=CLONE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failing = set()
    if os.path.exists(failed_file):
        for line in open(failed_file):
            nid = line.strip()
            if nid in id2num:
                failing.add(id2num[nid])
            else:
                # collection error of a whole file: every test in it fails
                for tid, n in id2num.items():
                    if tid.startswith(nid.split("::")[0]) and "::" not in nid:
                        failing.add(n)
        os.remove(failed_file)
    answers["commits"][cid] = {"failing": sorted(failing), "kind": kinds[cid]["kind"]}
    json.dump(answers, open(OUT, "w"), indent=1)
    print(f"{cid}: {kinds[cid]['kind']} {kinds[cid]['file']} failing={len(failing)} "
          f"{time.time() - t0:.0f}s", file=sys.stderr, flush=True)
subprocess.run(["git", "reset", "--hard", "-q"], cwd=CLONE)
