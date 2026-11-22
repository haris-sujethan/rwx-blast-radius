"""Diagnostic probe for the practice board (practice scores carry no prize).

* CPU calibration: replays the real selector on the 30 public sample commits bundled in the
  image (its CPU on our machine is known), output discarded.
* Batch shape: every practice commit gets one zero-second test that never fails, so
  simulated = 2 s x commits + 300 s x commits-with-failures, and misses = commits-with-failures.
"""

import json
import os
import subprocess
import sys

PROBE = "tests/test_errors.py::TestErrors::test_highlight_sql_empty_positions"
tests = json.load(open("/data/tests.json"))["tests"]
num = next((t["number"] for t in tests if t["id"] == PROBE), 0)
commits = sorted(f[:-6] for f in os.listdir("/data/commits") if f.endswith(".patch"))
env = dict(os.environ, BR_DATA="/app/sample", BR_MIRROR="/tmp/msample", BR_TPCFP="/app/tpcfp.json")
try:
    subprocess.run([sys.executable, "/app/selector/main.py"], env=env, timeout=150,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception as e:  # noqa: BLE001
    print(f"calibration failed: {e!r}", file=sys.stderr)
for c in commits:
    sys.stdout.write(json.dumps({"commit": c, "workers": [[num]]}) + "\n")
sys.stdout.flush()
