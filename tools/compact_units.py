"""Compact per-fixture-case records into units.bin (pickled, zlib).

units = {
  "sets": [bitset over unit positions],
  "tests": {test_id: {
      "units": [(fixture_rel, idx), ...],
      "lines": {file: {line: set_idx}},     # which units execute that function line
      "classes": {cls: set_idx},
      "nl": {file: frozenset(lines)},       # nonunit lines
      "nc": frozenset(classes)}},
  "implines": {file: frozenset(lines)}      # function lines run while importing (any test)
}
"""

import glob
import json
import pickle
import sys
import zlib

units_dir, raw_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]
sets, index = [], {}


def sid(v):
    i = index.get(v)
    if i is None:
        i = index[v] = len(sets)
        sets.append(v)
    return i


tests = {}
for f in glob.glob(f"{units_dir}/*.json"):
    r = json.load(open(f))
    if r["failed"] or not r["units"]:
        continue
    if not any(u[2]["lines"] for u in r["units"]):
        continue  # units carry no work (e.g. fixtures loaded in setUpClass)
    counts = {}
    for u in r["units"]:
        counts[u[0]] = counts.get(u[0], 0) + 1
    r["units"] = [u for u in r["units"] if u[2]["lines"]]
    ulist = [(u[0], u[1]) for u in r["units"]]
    total = r.get("nonunit_time", 0.0) + sum(u[3] if len(u) > 3 else 0.0 for u in r["units"])
    weights = [((u[3] if len(u) > 3 else 0.0) / total) if total > 0 else 1.0 / len(r["units"])
               for u in r["units"]]
    lines, classes = {}, {}
    for pos, u in enumerate(r["units"]):
        bit = 1 << pos
        for fn, ls in u[2]["lines"].items():
            d = lines.setdefault(fn, {})
            for ln in ls:
                d[ln] = d.get(ln, 0) | bit
        for c in u[2]["classes"]:
            classes[c] = classes.get(c, 0) | bit
    tests[r["id"]] = {
        "units": ulist,
        "counts": counts,
        "w": weights,
        "w0": (r.get("nonunit_time", 0.0) / total) if total > 0 else 0.0,
        "lines": {fn: {ln: sid(v) for ln, v in d.items()} for fn, d in lines.items()},
        "classes": {c: sid(v) for c, v in classes.items()},
        "nl": {fn: frozenset(ls) for fn, ls in r["nonunit"]["lines"].items()},
        "nc": frozenset(r["nonunit"]["classes"]),
    }

implines = {}
for f in glob.glob(f"{raw_dir}/*.json"):
    r = json.load(open(f))
    for fn, ls in r["imp"].items():
        if fn.startswith("sqlglot/"):
            implines.setdefault(fn, set()).update(ln for ln in ls if ln > 0)

blob = zlib.compress(pickle.dumps({"sets": sets, "tests": tests,
                                   "implines": {k: frozenset(v) for k, v in implines.items()}},
                                  protocol=5), 9)
open(out, "wb").write(blob)
print(f"tests={len(tests)} sets={len(sets)} bytes={len(blob)}", file=sys.stderr)
