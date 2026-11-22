"""Work out which parts of a Python module a patch can change.

The result is an Impact: old-file line spans whose executing tests are candidates, plus
class-level / module-level changes that are resolved against the dependency map.
"""

from __future__ import annotations

import ast
from difflib import SequenceMatcher

COMPOUND_BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")


class Impact:
    def __init__(self, path, modname):
        self.path = path
        self.modname = modname
        self.spans = []  # (start, end) old-file lines; tests executing function code here
        self.classes = set()  # class qualnames (within this module) whose class-level state changed
        self.class_names = {}  # qualname -> set of attribute names touched
        self.names = set()  # module-level names whose binding changed
        self.importers = False  # fall back to every test that imported the module
        self.syntax_error = False
        self.notes = []
        self.pairs = []  # (old_stmt, new_stmt, header_only) leaf statement changes in functions
        self.exotic = False  # some change is not a leaf statement edit inside a function

    def __repr__(self):
        return (f"Impact({self.path}, spans={self.spans}, classes={sorted(self.classes)}, "
                f"names={sorted(self.names)}, importers={self.importers}, "
                f"syntax_error={self.syntax_error}, notes={self.notes})")


def _dump(node):
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _span(node):
    start = node.lineno
    if getattr(node, "decorator_list", None):
        start = min([start] + [d.lineno for d in node.decorator_list])
    return (start, node.end_lineno or node.lineno)


def _header_fields(node):
    out = []
    for f in node._fields:
        if f in COMPOUND_BLOCKS:
            continue
        out.append((f, getattr(node, f)))
    return out


def _header_dump(node):
    parts = []
    for f, v in _header_fields(node):
        if isinstance(v, list):
            parts.append((f, [(_dump(x) if isinstance(x, ast.AST) else repr(x)) for x in v]))
        elif isinstance(v, ast.AST):
            parts.append((f, _dump(v)))
        else:
            parts.append((f, repr(v)))
    return repr(parts)


def _header_span(node):
    start = _span(node)[0]
    first_body = None
    for f in COMPOUND_BLOCKS:
        blk = getattr(node, f, None)
        if blk:
            ln = blk[0].lineno
            if first_body is None or ln < first_body:
                first_body = ln
    if first_body is None or first_body <= node.lineno:
        return (start, node.lineno if first_body is None else max(node.lineno, start))
    return (start, first_body - 1)


def _targets(stmt):
    names = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
        return names
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _is_noop(stmt):
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    return False


def _lambda_only_diff(o, n):
    """If o and n differ only inside function bodies of lambdas/nested defs, return the old
    spans of those bodies. Otherwise None."""
    if type(o) is not type(n):
        return None
    if isinstance(o, ast.Lambda):
        if _dump(o.args) != _dump(n.args):
            return None
        if _dump(o.body) == _dump(n.body):
            return []
        return [(o.body.lineno, o.body.end_lineno)]
    if isinstance(o, ast.AST):
        spans = []
        for f in o._fields:
            a, b = getattr(o, f, None), getattr(n, f, None)
            if isinstance(a, list):
                if not isinstance(b, list) or len(a) != len(b):
                    return None
                for x, y in zip(a, b):
                    if isinstance(x, ast.AST):
                        r = _lambda_only_diff(x, y)
                        if r is None:
                            return None
                        spans.extend(r)
                    elif x != y:
                        return None
            elif isinstance(a, ast.AST):
                if not isinstance(b, ast.AST):
                    return None
                r = _lambda_only_diff(a, b)
                if r is None:
                    return None
                spans.extend(r)
            else:
                if a != b and f not in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
                    return None
        return spans
    return [] if o == n else None


class Differ:
    def __init__(self, impact: Impact):
        self.im = impact

    # ---- function scope -------------------------------------------------
    def mark(self, node):
        self.im.spans.append(_span(node))
        self.im.exotic = True

    def block_reach(self, old_block):
        self.im.exotic = True
        for s in old_block:
            self.im.spans.append(_span(s))

    def fn_block(self, old, new, parent_span=None):
        od = [_dump(s) for s in old]
        nd = [_dump(s) for s in new]
        if od == nd:
            return
        sm = SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for o, n in zip(old[i1:i2], new[j1:j2]):
                    self.fn_pair(o, n)
                continue
            for o in old[i1:i2]:
                self.mark(o)
            ins = new[j1:j2]
            if ins and not all(_is_noop(s) for s in ins):
                if old:
                    self.block_reach(old)
                elif parent_span:
                    self.im.spans.append(parent_span)

    def fn_pair(self, o, n):
        if type(o) is not type(n):
            self.mark(o)
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if _header_dump(o) != _header_dump(n):
                self.mark(o)
                return
            self.im.spans.append(_header_span(o))  # tests creating the nested def
            self.fn_block(o.body, n.body, _span(o))
            return
        is_compound = any(isinstance(getattr(o, f, None), list) and f in COMPOUND_BLOCKS
                          for f in o._fields)
        if not is_compound:
            self.im.spans.append(_span(o))
            self.im.pairs.append((o, n, False))
            return
        if _header_dump(o) != _header_dump(n):
            self.im.spans.append(_header_span(o))
            self.im.pairs.append((o, n, True))
        for f in COMPOUND_BLOCKS:
            a, b = getattr(o, f, None), getattr(n, f, None)
            if a is None and b is None:
                continue
            a, b = a or [], b or []
            if f in ("handlers", "cases"):
                if len(a) != len(b):
                    self.mark(o)
                    continue
                for x, y in zip(a, b):
                    if _header_dump(x) != _header_dump(y):
                        self.im.spans.append(_header_span(x))
                        self.mark(x)
                        self.im.exotic = True
                    self.fn_block(x.body, y.body, _span(x))
            else:
                self.fn_block(a, b, _span(o))

    # ---- function definitions (module/class scope) ----------------------
    def funcdef(self, o, n, cls_q):
        if _header_dump(o) != _header_dump(n):
            self.im.exotic = True
            # signature/decorators changed: every caller
            self.im.spans.append(_span(o))
            if cls_q:
                self.class_change(cls_q, {o.name})
            else:
                self.im.names.add(o.name)
            return
        self.fn_block(o.body, n.body, _span(o))

    def class_change(self, q, names):
        self.im.exotic = True
        self.im.classes.add(q)
        self.im.class_names.setdefault(q, set()).update(names)

    # ---- class scope ------------------------------------------------------
    def class_block(self, old, new, q):
        od = [_dump(s) for s in old]
        nd = [_dump(s) for s in new]
        if od == nd:
            return
        sm = SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for o, n in zip(old[i1:i2], new[j1:j2]):
                    self.class_pair(o, n, q)
                continue
            for o in old[i1:i2]:
                if _is_noop(o):
                    continue
                if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.mark(o)
                    self.class_change(q, {o.name})
                elif isinstance(o, ast.ClassDef):
                    self.class_change(f"{q}.{o.name}", set())
                    self.class_change(q, {o.name})
                else:
                    self.class_change(q, _targets(o))
            for n in new[j1:j2]:
                if _is_noop(n):
                    continue
                self.class_change(q, _targets(n))

    def class_pair(self, o, n, q):
        if type(o) is not type(n):
            self.class_change(q, _targets(o) | _targets(n))
            if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.mark(o)
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if o.name != n.name:
                self.mark(o)
                self.class_change(q, {o.name, n.name})
                return
            self.funcdef(o, n, q)
            return
        if isinstance(o, ast.ClassDef):
            if o.name != n.name or _header_dump(o) != _header_dump(n):
                self.class_change(f"{q}.{o.name}", set())
                self.class_change(q, {o.name, n.name})
            self.class_block(o.body, n.body, f"{q}.{o.name}")
            return
        spans = _lambda_only_diff(o, n)
        if spans is not None:
            self.im.spans.extend(spans)
            self.im.exotic = True
            return
        self.class_change(q, _targets(o) | _targets(n))

    # ---- module scope -----------------------------------------------------
    def module(self, old, new):
        od = [_dump(s) for s in old]
        nd = [_dump(s) for s in new]
        sm = SequenceMatcher(None, od, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                for o, n in zip(old[i1:i2], new[j1:j2]):
                    self.module_pair(o, n)
                continue
            for o in old[i1:i2]:
                self.module_removed(o)
            for n in new[j1:j2]:
                self.module_added(n)

    def module_removed(self, o):
        if _is_noop(o):
            # module docstring
            self.im.names.add("__doc__")
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.mark(o)
            self.im.names.add(o.name)
        elif isinstance(o, ast.ClassDef):
            self.class_change(o.name, set())
            self.im.names.add(o.name)
        elif isinstance(o, (ast.Import, ast.ImportFrom)):
            self.im.importers = True
            self.im.notes.append("import removed")
        else:
            self.module_stmt_change(o, None)

    def module_added(self, n):
        if _is_noop(n):
            self.im.names.add("__doc__")
            return
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            self.im.importers = True
            self.im.notes.append("import added")
            return
        self.im.names.update(_targets(n))
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign,
                              ast.AnnAssign, ast.AugAssign)):
            # arbitrary module-level code (calls, ifs, loops): may have side effects at import
            self.im.importers = True
            self.im.notes.append(f"module code added: {type(n).__name__}")

    def module_stmt_change(self, o, n):
        nodes = [x for x in (o, n) if x is not None]
        names = set()
        for x in nodes:
            names |= _targets(x)
        if not names or any(
            not isinstance(x, (ast.Assign, ast.AnnAssign, ast.AugAssign)) for x in nodes
        ):
            self.im.importers = True
            self.im.notes.append(f"module stmt: {type(nodes[0]).__name__}")
        self.im.names.update(names)

    def module_pair(self, o, n):
        if type(o) is not type(n):
            self.module_removed(o)
            self.module_added(n)
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if o.name != n.name:
                self.module_removed(o)
                self.module_added(n)
                return
            self.funcdef(o, n, None)
            return
        if isinstance(o, ast.ClassDef):
            if o.name != n.name:
                self.module_removed(o)
                self.module_added(n)
                return
            if _header_dump(o) != _header_dump(n):
                self.class_change(o.name, set())
            self.class_block(o.body, n.body, o.name)
            return
        if isinstance(o, (ast.Import, ast.ImportFrom)):
            self.im.importers = True
            self.im.notes.append("import changed")
            return
        if isinstance(o, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            # module-level compound (e.g. if TYPE_CHECKING): treat conservatively
            if isinstance(o, ast.If) and _dump(o.test) == _dump(n.test) and \
                    "TYPE_CHECKING" in _dump(o.test):
                return
            self.im.importers = True
            self.im.notes.append(f"module compound: {type(o).__name__}")
            return
        spans = _lambda_only_diff(o, n)
        if spans is not None:
            self.im.spans.extend(spans)
            self.im.exotic = True
            return
        self.module_stmt_change(o, n)


def analyze_python(path, modname, old_src, new_src) -> Impact:
    im = Impact(path, modname)
    try:
        old_tree = ast.parse(old_src) if old_src is not None else None
    except SyntaxError:
        old_tree = None
    if new_src is None:
        im.importers = True
        im.notes.append("deleted")
        return im
    try:
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError):
        im.syntax_error = True
        im.importers = True
        return im
    if old_tree is None:
        im.notes.append("new file")
        im.names.add(modname.rsplit(".", 1)[-1])
        return im
    if _dump(old_tree) == _dump(new_tree):
        return im
    Differ(im).module(old_tree.body, new_tree.body)
    if im.classes or im.names or im.importers or im.syntax_error:
        im.exotic = True
    return im
