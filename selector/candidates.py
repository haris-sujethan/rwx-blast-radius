"""Turn a patch into the set of tests that could observe it, using the dependency map."""

from __future__ import annotations

import ast
import os
import pickle
import re
import zlib

import wm
from analyze import analyze_python
from patchutil import apply, parse_patch, touched_lines

CONST_RE = re.compile(r"_?[A-Z][A-Z0-9_]*")
IGNORED_OPENS = {"pyproject.toml", "setup.cfg", "tests", "tests/dialects"}


def bits(v):
    out = []
    while v:
        low = v & -v
        out.append(low.bit_length() - 1)
        v ^= low
    return out


class MapDB:
    def __init__(self, path, repo):
        m = pickle.loads(zlib.decompress(open(path, "rb").read()))
        self.ids = m["ids"]
        self.sets = m["sets"]
        self.fn = m["fn"]
        self.mods = m["mods"]
        self.deflines = {p: set(v) for p, v in m["deflines"].items()}
        self.opens = m["opens"]
        self.classes = m["classes"]
        self.repo = repo
        self._src = {}
        self._ast = {}
        self._pyfiles = None
        self.req = None
        self.file_tests = {}
        for n, tid in enumerate(self.ids):
            self.file_tests.setdefault(tid.split("::")[0], []).append(n)

    # ---- sources ---------------------------------------------------------
    def source(self, rel):
        s = self._src.get(rel)
        if s is None:
            try:
                with open(os.path.join(self.repo, rel), "rb") as f:
                    s = f.read()
            except OSError:
                s = b""
            self._src[rel] = s
        return s

    def tree(self, rel):
        t = self._ast.get(rel)
        if t is None:
            try:
                t = ast.parse(self.source(rel))
            except SyntaxError:
                t = ast.Module(body=[], type_ignores=[])
            self._ast[rel] = t
        return t

    def pyfiles(self):
        if self._pyfiles is None:
            out = []
            for top in ("sqlglot", "tests"):
                for root, dirs, files in os.walk(os.path.join(self.repo, top)):
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    for f in files:
                        if f.endswith(".py"):
                            out.append(os.path.relpath(os.path.join(root, f), self.repo))
            self._pyfiles = sorted(out)
        return self._pyfiles

    # ---- primitive queries ---------------------------------------------
    def _req_lines(self, rel, lines):
        if self.req is not None and lines:
            self.req["lines"].setdefault(rel, set()).update(lines)

    def line_tests(self, rel, lines):
        d = self.fn.get(rel)
        if not d:
            return 0
        v = 0
        S = self.sets
        hit = []
        for ln in lines:
            i = d.get(ln)
            if i is not None:
                v |= S[i]
                hit.append(ln)
        self._req_lines(rel, hit)
        return v

    def span_tests(self, rel, spans):
        d = self.fn.get(rel)
        if not d:
            return 0
        v = 0
        S = self.sets
        hit = []
        for a, b in spans:
            if b - a > 400:
                for ln, i in d.items():
                    if a <= ln <= b:
                        v |= S[i]
                        hit.append(ln)
            else:
                for ln in range(a, b + 1):
                    i = d.get(ln)
                    if i is not None:
                        v |= S[i]
                        hit.append(ln)
        self._req_lines(rel, hit)
        return v

    def _req_full(self, v):
        if self.req is not None:
            self.req["full"] |= v
        return v

    def importers(self, rel):
        i = self.mods.get(rel)
        return self._req_full(self.sets[i] if i is not None else 0)

    def openers(self, rel):
        if rel in IGNORED_OPENS or (rel.startswith("tests/") and rel.endswith(".py")):
            return 0
        v = 0
        i = self.opens.get(rel)
        if i is not None:
            v |= self.sets[i]
        if self.req is not None and v:
            if rel.startswith("tests/fixtures/"):
                self.req["fixtures"].add(rel)  # resolved per fixture case later
            else:
                self.req["full"] |= v
        return v

    def dir_listers(self, rel):
        d = os.path.dirname(rel)
        v = 0
        while d:
            if d not in IGNORED_OPENS:
                i = self.opens.get(d)
                if i is not None:
                    v |= self.sets[i]
            d = os.path.dirname(d)
        return self._req_full(v)

    def class_users(self, modname, qual):
        i = self.classes.get(f"{modname}:{qual}")
        return self.sets[i] if i is not None else None

    # ---- name references ---------------------------------------------------
    def name_refs(self, names, depth=0, seen=None):
        """Tests that execute code referencing any of `names` (textual, word-boundary)."""
        names = {n for n in names if n and n != "__doc__"}
        if not names:
            return 0
        seen = seen if seen is not None else set()
        names -= seen
        if not names:
            return 0
        seen |= names
        rx = re.compile(rb"\b(?:" + b"|".join(re.escape(n.encode()) for n in sorted(names)) + rb")\b")
        v = 0
        for rel in self.pyfiles():
            src = self.source(rel)
            if not rx.search(src):
                continue
            fnd = self.fn.get(rel, {})
            dl = self.deflines.get(rel, ())
            deflevel = []
            for lineno, line in enumerate(src.split(b"\n"), 1):
                if rx.search(line):
                    i = fnd.get(lineno)
                    if i is not None:
                        v |= self.sets[i]
                        self._req_lines(rel, (lineno,))
                    if lineno in dl:
                        deflevel.append(lineno)
            if deflevel and depth < 3:
                v |= self.deflevel_refs(rel, deflevel, depth, seen)
        return v

    def deflevel_refs(self, rel, lines, depth, seen):
        """A module/class-body line references a changed name: the binding it defines changes."""
        tree = self.tree(rel)
        modname = rel[:-3].replace("/", ".")
        if modname.endswith(".__init__"):
            modname = modname[: -len(".__init__")]
        v = 0
        names = set()
        lines = set(lines)

        def visit(body, qual, cls_span):
            nonlocal v
            for st in body:
                a, b = st.lineno, st.end_lineno or st.lineno
                hit = any(a <= ln <= b for ln in lines)
                if not hit:
                    continue
                if isinstance(st, ast.ClassDef):
                    q = f"{qual}.{st.name}" if qual else st.name
                    # class header line itself (bases) or body statements
                    if any(a <= ln < (st.body[0].lineno if st.body else b + 1) for ln in lines):
                        v |= self.class_change(rel, modname, q, set(), depth + 1, seen,
                                               (a, b))
                    visit(st.body, q, (a, b))
                elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # decorator / default args evaluated at def time
                    if qual:
                        v |= self.class_change(rel, modname, qual, {st.name}, depth + 1, seen,
                                               cls_span)
                    else:
                        names.add(st.name)
                elif qual:
                    tg = set()
                    for node in ast.walk(st):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                            tg.add(node.id)
                    v |= self.class_change(rel, modname, qual, tg, depth + 1, seen, cls_span)
                else:
                    for node in ast.walk(st):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                            names.add(node.id)
                    if isinstance(st, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                        visit(getattr(st, "body", []), qual, cls_span)

        visit(tree.body, "", None)
        if names:
            v |= self.name_refs(names, depth + 1, seen)
        return v

    def class_change(self, rel, modname, qual, attrs, depth, seen, span=None):
        v = 0
        users = self.class_users(modname, qual)
        if users is not None:
            v |= users
        if span is None:
            span = self.class_span(rel, qual)
        if span is not None:
            v |= self.span_tests(rel, [span])
        if users is not None and attrs and all(CONST_RE.fullmatch(a) for a in attrs):
            # a changed class constant only matters to code that reads it by name
            outer = self.req
            if outer is not None:
                self.req = {"lines": {}, "classes": set(), "full": 0, "fixtures": set(),
                            "conj": []}
            try:
                refs = self.name_refs(set(attrs), depth + 1, set(seen))
            finally:
                inner, self.req = self.req, outer
            if outer is not None:
                outer["full"] |= inner["full"] & v
                outer["conj"].append((f"{modname}:{qual}", inner["lines"]))
                outer["conj"].extend(inner["conj"])
                outer["classes"] |= inner["classes"]
                outer["fixtures"] |= inner["fixtures"]
            v &= refs
            return v
        if users is not None and self.req is not None:
            self.req["classes"].add(f"{modname}:{qual}")
        if users is None and depth < 3:
            # not an instantiated class we track: follow references to it / its attributes
            simple = qual.rsplit(".", 1)[-1]
            v |= self.name_refs({simple} | set(attrs), depth + 1, seen)
        return v

    def class_span(self, rel, qual):
        tree = self.tree(rel)
        body = tree.body
        node = None
        for part in qual.split("."):
            node = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == part), None)
            if node is None:
                return None
            body = node.body
        return (node.lineno, node.end_lineno)


def modname_of(rel):
    m = rel[:-3].replace("/", ".")
    if m.endswith(".__init__"):
        m = m[: -len(".__init__")]
    return m


class Selection:
    def __init__(self):
        self.cands = 0
        self.notes = []
        self.files = {}  # rel -> new text (None = deleted)
        self.instr = {}  # rel -> weak-mutation instrumented new text
        self.monitorable = True  # every change is a monitored leaf expression edit
        self.sites = 0
        self.req = {"lines": {}, "classes": set(), "full": 0, "fixtures": set(), "conj": []}
        self.fixture_texts = {}  # rel -> (old, new)


def select(db: MapDB, patch_text: str) -> Selection:
    sel = Selection()
    db.req = sel.req
    try:
        return _select(db, sel, patch_text)
    finally:
        db.req = None


def _select(db, sel, patch_text):
    fps = parse_patch(patch_text)
    for fp in fps:
        rels = {p for p in (fp.old_path, fp.new_path) if p}
        old_rel = fp.old_path
        old = None
        if old_rel is not None:
            raw = db.source(old_rel)
            old = raw.decode("utf-8", "surrogateescape")
        if fp.binary:
            sel.monitorable = False
            sel.notes.append(f"binary {fp.path}")
            for r in rels:
                sel.cands |= db.openers(r) | db.dir_listers(r) | db.importers(r)
            continue
        try:
            new = apply(old, fp)
        except Exception as e:  # noqa: BLE001
            sel.notes.append(f"apply failed {fp.path}: {e}")
            new = None
        if fp.new_path:
            sel.files[fp.new_path] = new
        if fp.old_path and fp.old_path == fp.new_path and fp.old_path.startswith("tests/fixtures/"):
            sel.fixture_texts[fp.old_path] = (old, new)
        if fp.old_path and fp.old_path != fp.new_path:
            sel.files[fp.old_path] = None
        v = 0
        for r in rels:
            v |= db.openers(r)
        if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            for r in rels:
                v |= db.dir_listers(r)
        rel = fp.new_path or fp.old_path
        if not (rel.endswith(".py") and fp.old_path == fp.new_path):
            sel.monitorable = False
        if rel.endswith(".py") and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
                v |= db.importers(fp.old_path) | db.name_refs({modname_of(fp.old_path).rsplit(".", 1)[-1]})
            ot, nt = touched_lines(fp)
            im = analyze_python(rel, modname_of(rel),
                                old.encode("utf-8", "surrogateescape") if old is not None else None,
                                new.encode("utf-8", "surrogateescape") if new is not None else None,
                                ot, nt)
            sel.notes.append(repr(im))
            if im.exotic or v:
                # exotic change, or the file is also read as data by some test
                sel.monitorable = False
            elif im.pairs and new is not None:
                src, nsites, complete = wm.instrument(
                    new.encode("utf-8", "surrogateescape"), im.pairs, sel.sites)
                if src is not None:
                    sel.instr[rel] = src.decode("utf-8", "surrogateescape")
                    sel.sites += nsites
                if not complete or src is None:
                    sel.monitorable = False
            if im.importers:
                v |= db.importers(fp.old_path or rel)
                if fp.old_path is None:
                    v |= db.name_refs({modname_of(rel).rsplit(".", 1)[-1]})
            if im.spans:
                v |= db.span_tests(rel, im.spans)
            for q in im.classes:
                v |= db.class_change(rel, im.modname, q, im.class_names.get(q, set()), 0, set())
            if im.names:
                v |= db.name_refs(im.names)
                # names defined at module level are also visible as module attributes of
                # importers' module-level code; tests importing it that reference via attr
        elif rel.endswith(".py"):
            # python outside sqlglot/tests (setup.py, sqlglotc/, benchmarks): importers/openers
            v |= db.importers(rel)
            sel.monitorable = False
        else:
            sel.monitorable = False
        sel.cands |= v
    return sel
