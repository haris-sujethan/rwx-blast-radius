"""Generate synthetic mutant patches (unified diffs) against the pinned sqlglot checkout.

Kinds: operator flips, bool/constant changes, comment insertions, fixture line corruption,
class-level constant edits, statement deletion. Writes <out>/<id>.patch and <out>/kinds.json.
"""

import ast
import difflib
import json
import os
import random
import sys

REPO = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]
N = int(sys.argv[3])
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 1
rng = random.Random(SEED)
os.makedirs(OUT, exist_ok=True)

py_files = []
for root, dirs, files in os.walk(os.path.join(REPO, "sqlglot")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".py"):
            py_files.append(os.path.relpath(os.path.join(root, f), REPO))
_dirs = os.environ.get("MUT_DIRS")
if _dirs:
    py_files = [f for f in py_files if any(f.startswith(d) for d in _dirs.split(","))]
fixture_files = []
for root, dirs, files in os.walk(os.path.join(REPO, "tests", "fixtures", "optimizer")):
    for f in files:
        if f.endswith(".sql"):
            fixture_files.append(os.path.relpath(os.path.join(root, f), REPO))
fixture_files += ["tests/fixtures/identity.sql", "tests/fixtures/pretty.sql"]


def diff(rel, old, new):
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3)
    body = "".join(d)
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


CMP_SWAP = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", " is not ": " is ",
            " is ": " is not ", " in ": " not in "}


def mutate_line(line):
    """Textual mutation of one code line; returns new line or None."""
    opts = []
    for a, b in CMP_SWAP.items():
        if a in line:
            opts.append((a, b))
    for a, b in ((" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"),
                 ("not ", ""), ("+ 1", "- 1"), ("- 1", "+ 1"), ("[0]", "[1]"), ("[1]", "[0]"),
                 ("[-1]", "[0]"), (", 0)", ", 1)"), (", 1)", ", 2)")):
        if a in line:
            opts.append((a, b))
    if not opts:
        return None
    a, b = rng.choice(opts)
    idxs = [i for i in range(len(line)) if line.startswith(a, i)]
    i = rng.choice(idxs)
    return line[:i] + b + line[i + len(a):]


def code_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    lines = src.split("\n")
    cand = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(node):
                if isinstance(st, ast.stmt) and not isinstance(st, (ast.FunctionDef, ast.ClassDef)):
                    cand.add(st.lineno)
    cand = sorted(cand)
    rng.shuffle(cand)
    for ln in cand[:50]:
        new = mutate_line(lines[ln - 1])
        if new and new != lines[ln - 1]:
            nl = list(lines)
            nl[ln - 1] = new
            ns = "\n".join(nl)
            try:
                ast.parse(ns)
            except SyntaxError:
                continue
            return rel, src, ns
    return None


def class_const_mutant():
    rel = rng.choice([f for f in py_files if "/dialects/" in f or "/parsers/" in f or
                      "/generators/" in f or "/expressions/" in f])
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    lines = src.split("\n")
    cand = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.lineno == st.end_lineno:
                    cand.append(st.lineno)
                elif isinstance(st, ast.Assign) and isinstance(st.value, ast.Dict):
                    cand.extend(range(st.lineno + 1, st.end_lineno))
    rng.shuffle(cand)
    for ln in cand[:50]:
        new = mutate_line(lines[ln - 1])
        if new and new != lines[ln - 1]:
            nl = list(lines)
            nl[ln - 1] = new
            ns = "\n".join(nl)
            try:
                ast.parse(ns)
            except SyntaxError:
                continue
            return rel, src, ns
    return None


def comment_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    lines = src.split("\n")
    for _ in range(50):
        ln = rng.randrange(1, len(lines))
        cur = lines[ln]
        if not cur.strip():
            continue
        indent = cur[: len(cur) - len(cur.lstrip())]
        nl = list(lines)
        nl.insert(ln, indent + rng.choice(["# Cheap check first", "# Kept for backwards compatibility",
                                           "# TODO: revisit", "# noqa"]))
        ns = "\n".join(nl)
        try:
            ast.parse(ns)
        except SyntaxError:
            continue
        return rel, src, ns
    return None


def fixture_mutant():
    rel = rng.choice(fixture_files)
    src = open(os.path.join(REPO, rel)).read()
    lines = src.split("\n")
    for _ in range(50):
        ln = rng.randrange(len(lines))
        cur = lines[ln]
        if not cur.strip() or cur.startswith("#") or cur.startswith("--"):
            continue
        words = cur.split(" ")
        i = rng.randrange(len(words))
        op = rng.choice(["insert", "delete", "dup"])
        if op == "insert":
            words.insert(i, rng.choice(["42", "x", "0", "NULL"]))
        elif op == "delete" and len(words) > 1:
            words.pop(i)
        else:
            words.insert(i, words[i])
        nl = list(lines)
        nl[ln] = " ".join(words)
        return rel, src, "\n".join(nl)
    return None


def delete_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    lines = src.split("\n")
    cand = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef,)):
            for st in node.body[1:]:
                if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign)) and st.lineno == st.end_lineno:
                    cand.append(st.lineno)
    if not cand:
        return None
    ln = rng.choice(cand)
    nl = list(lines)
    del nl[ln - 1]
    ns = "\n".join(nl)
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


def argswap_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and len(n.args) >= 2
             and n.lineno == n.end_lineno and all(a.lineno == n.lineno for a in n.args[:2])]
    rng.shuffle(calls)
    lines = src.split("\n")
    for c in calls[:20]:
        a, b = c.args[0], c.args[1]
        ln = lines[c.lineno - 1].encode()
        ta, tb = ln[a.col_offset:a.end_col_offset], ln[b.col_offset:b.end_col_offset]
        if ta == tb:
            continue
        new = ln[:a.col_offset] + tb + ln[a.end_col_offset:b.col_offset] + ta + ln[b.end_col_offset:]
        nl = list(lines)
        nl[c.lineno - 1] = new.decode()
        ns = "\n".join(nl)
        try:
            ast.parse(ns)
        except SyntaxError:
            continue
        return rel, src, ns
    return None


def strlit_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    consts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and 2 <= len(n.value) <= 30 and n.lineno == n.end_lineno and "\\" not in n.value]
    rng.shuffle(consts)
    lines = src.split("\n")
    for c in consts[:20]:
        ln = lines[c.lineno - 1].encode()
        seg = ln[c.col_offset:c.end_col_offset].decode()
        if len(seg) < 4 or seg[0] not in "'\"":
            continue
        new_seg = seg[0] + seg[1:-1][::-1] + seg[-1] if rng.random() < 0.5 else seg[:-1] + "_x" + seg[-1]
        nl = list(lines)
        nl[c.lineno - 1] = (ln[:c.col_offset] + new_seg.encode() + ln[c.end_col_offset:]).decode()
        ns = "\n".join(nl)
        try:
            ast.parse(ns)
        except SyntaxError:
            continue
        return rel, src, ns
    return None


def ifdel_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    cand = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for i, st in enumerate(fn.body):
                if isinstance(st, ast.If) and i > 0 and i < len(fn.body) - 1 and not st.orelse:
                    cand.append(st)
    if not cand:
        return None
    st = rng.choice(cand)
    lines = src.split("\n")
    nl = lines[: st.lineno - 1] + lines[st.end_lineno:]
    ns = "\n".join(nl)
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


def docstring_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    cand = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)) and fn.body and \
                isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) and \
                isinstance(fn.body[0].value.value, str):
            cand.append(fn.body[0])
    if not cand:
        return None
    st = rng.choice(cand)
    lines = src.split("\n")
    nl = list(lines)
    i = st.lineno - 1
    nl[i] = nl[i].replace('"""', '"""Note: ', 1)
    ns = "\n".join(nl)
    if ns == src:
        return None
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


def whitespace_mutant():
    rel = rng.choice(py_files)
    src = open(os.path.join(REPO, rel)).read()
    lines = src.split("\n")
    ln = rng.randrange(1, len(lines))
    nl = list(lines)
    if rng.random() < 0.5:
        nl.insert(ln, "")
    else:
        nl[ln] = nl[ln] + "  "
    ns = "\n".join(nl)
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


test_files = []
for root, dirs, files in os.walk(os.path.join(REPO, "tests")):
    for f in files:
        if f.startswith("test_") and f.endswith(".py"):
            test_files.append(os.path.relpath(os.path.join(root, f), REPO))


def testfile_mutant():
    rel = rng.choice(test_files)
    src = open(os.path.join(REPO, rel)).read()
    tree = ast.parse(src)
    consts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and 6 <= len(n.value) <= 80 and n.lineno == n.end_lineno]
    if not consts:
        return None
    c = rng.choice(consts)
    lines = src.split("\n")
    ln = lines[c.lineno - 1].encode()
    seg = ln[c.col_offset:c.end_col_offset].decode()
    if seg[0] not in "'\"" or len(seg) < 4:
        return None
    new_seg = seg[:-1] + " " + seg[-1]
    nl = list(lines)
    nl[c.lineno - 1] = (ln[:c.col_offset] + new_seg.encode() + ln[c.end_col_offset:]).decode()
    ns = "\n".join(nl)
    try:
        ast.parse(ns)
    except SyntaxError:
        return None
    return rel, src, ns


kinds = {}
MODE = os.environ.get("MUT_MODE", "1")
if MODE == "1":
    gens = [("code", code_mutant, 5), ("classconst", class_const_mutant, 2), ("comment", comment_mutant, 1),
            ("fixture", fixture_mutant, 2), ("delete", delete_mutant, 1)]
else:
    gens = [("code", code_mutant, 3), ("argswap", argswap_mutant, 2), ("strlit", strlit_mutant, 2),
            ("ifdel", ifdel_mutant, 2), ("docstring", docstring_mutant, 1),
            ("whitespace", whitespace_mutant, 1), ("testfile", testfile_mutant, 2),
            ("classconst", class_const_mutant, 1), ("fixture", fixture_mutant, 1)]
weights = [g[2] for g in gens]
i = 0
while i < N:
    name, fn, _ = rng.choices(gens, weights)[0]
    r = fn()
    if r is None:
        continue
    rel, old, new = r
    if old == new:
        continue
    cid = f"m{SEED}_{i:03d}"
    open(os.path.join(OUT, cid + ".patch"), "w").write(diff(rel, old, new))
    kinds[cid] = {"kind": name, "file": rel}
    i += 1
json.dump(kinds, open(os.path.join(OUT, "kinds.json"), "w"), indent=1)
print(f"wrote {N} mutants", file=sys.stderr)
