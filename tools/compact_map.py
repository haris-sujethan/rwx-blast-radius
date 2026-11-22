"""Compact raw per-test records into the runtime map (pickled, zlib-compressed).

map = {
  "ids":      [test id by number],
  "sets":     [bitset int, ...]          unique test sets
  "fn":       {file: {line: set_idx}}    tests that executed function code on that line
  "mods":     {file: set_idx}            tests whose process imported the module
  "deflines": {file: sorted lines}       module/class-body lines that ever executed
  "opens":    {path: set_idx}            tests that opened / listed this path
  "classes":  {"module:qualname": set_idx}  tests that instantiated (a subclass of) this class
}
"""

import glob
import json
import pickle
import sys
import zlib

raw_dir, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
tests = json.load(open(tests_json))["tests"]
ids = [None] * len(tests)
for t in tests:
    ids[t["number"]] = t["id"]

fn = {}
mods = {}
deflines = {}
opens = {}
classes = {}

for f in glob.glob(f"{raw_dir}/*.json"):
    r = json.load(open(f))
    bit = 1 << r["n"]
    for phase in ("imp", "run"):
        for path, lines in r[phase].items():
            d = fn.setdefault(path, {})
            for ln in lines:
                if ln > 0:
                    d[ln] = d.get(ln, 0) | bit
                else:
                    mods[path] = mods.get(path, 0) | bit
                    deflines.setdefault(path, set()).add(-ln)
    for p in r["opens"]:
        if "__pycache__" in p:
            continue
        opens[p] = opens.get(p, 0) | bit
    for c in r["classes"]:
        classes[c] = classes.get(c, 0) | bit

sets = []
index = {}


def sid(v):
    i = index.get(v)
    if i is None:
        i = index[v] = len(sets)
        sets.append(v)
    return i


m = {
    "ids": ids,
    "fn": {p: {ln: sid(v) for ln, v in d.items()} for p, d in fn.items()},
    "mods": {p: sid(v) for p, v in mods.items()},
    "deflines": {p: sorted(v) for p, v in deflines.items()},
    "opens": {p: sid(v) for p, v in opens.items()},
    "classes": {c: sid(v) for c, v in classes.items()},
}
m["sets"] = sets
blob = zlib.compress(pickle.dumps(m, protocol=5), 9)
open(out, "wb").write(blob)
nlines = sum(len(d) for d in fn.values())
print(f"files={len(fn)} lines={nlines} unique_sets={len(sets)} classes={len(classes)} "
      f"opens={len(opens)} bytes={len(blob)}", file=sys.stderr)
