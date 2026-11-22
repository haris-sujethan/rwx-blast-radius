"""Weak-mutation instrumentation.

For a leaf edit inside a function we find the smallest expression that differs between the old
and new statement. If both versions are side-effect free we rewrite the new source so that the
expression is evaluated both ways at runtime:

    _brwm_(id, lambda: (<old expr>), lambda: (<new expr>), cond)

The program keeps using the new value; _brwm_ counts executions and "infections" (runs where the
two values differ). A mutation that never infects anything while the cheap tests run is very
likely equivalent, which lets us skip expensive tests that could only fail through it.
"""

from __future__ import annotations

import ast

SAFE_FUNCS = {
    "len", "isinstance", "issubclass", "bool", "int", "str", "float", "type", "getattr",
    "hasattr", "min", "max", "any", "all", "tuple", "list", "set", "frozenset", "sorted",
    "abs", "callable", "seq_get", "repr", "ord", "chr", "round",
}
SAFE_METHODS = {
    "get", "startswith", "endswith", "lower", "upper", "strip", "lstrip", "rstrip", "isdigit",
    "isalpha", "isalnum", "isupper", "islower", "isspace", "keys", "values", "items", "find",
    "index", "count", "split", "join", "replace", "casefold", "is_type", "is_int", "is_number",
    "is_string", "is_star", "args", "text", "name",
}
PURE_NODES = (
    ast.Name, ast.Attribute, ast.Constant, ast.Compare, ast.BoolOp, ast.UnaryOp, ast.BinOp,
    ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.Set, ast.IfExp, ast.Load, ast.Call,
    ast.keyword, ast.boolop, ast.cmpop, ast.unaryop, ast.operator, ast.expr_context,
    ast.Dict, ast.Starred,
)


def _pure(node):
    for x in ast.walk(node):
        if not isinstance(x, PURE_NODES):
            return False
        if isinstance(x, ast.Name) and x.id in ("super", "__class__"):
            return False
        if isinstance(x, (ast.Name, ast.Attribute, ast.Subscript)) and not isinstance(
                x.ctx, ast.Load):
            return False
        if isinstance(x, ast.Call):
            f = x.func
            if isinstance(f, ast.Name):
                if f.id not in SAFE_FUNCS:
                    return False
            elif isinstance(f, ast.Attribute):
                if f.attr not in SAFE_METHODS:
                    return False
            else:
                return False
    return True


def _dump(n):
    return ast.dump(n, include_attributes=False)


HEADER_SKIP = {"body", "orelse", "finalbody", "handlers", "cases"}


def _min_diff(a, b, ctx, out, header_only=False):
    """Collect minimal differing (old_expr, new_expr) pairs; ctx = nearest enclosing expr pair.
    Returns False if a difference cannot be attributed to an expression."""
    if type(a) is not type(b):
        if ctx is None:
            return False
        out.append(ctx)
        return True
    if isinstance(a, ast.expr):
        ctx = (a, b)
    for f in a._fields:
        if header_only and f in HEADER_SKIP:
            continue
        x, y = getattr(a, f, None), getattr(b, f, None)
        if isinstance(x, list):
            if not isinstance(y, list) or len(x) != len(y):
                if ctx is None:
                    return False
                out.append(ctx)
                return True
            for xi, yi in zip(x, y):
                if isinstance(xi, ast.AST):
                    if _dump(xi) != _dump(yi):
                        if not _min_diff(xi, yi, ctx, out):
                            return False
                elif xi != yi:
                    if ctx is None:
                        return False
                    out.append(ctx)
                    return True
        elif isinstance(x, ast.AST):
            if not isinstance(y, ast.AST):
                if ctx is None:
                    return False
                out.append(ctx)
                return True
            if _dump(x) != _dump(y):
                if not _min_diff(x, y, ctx, out):
                    return False
        else:
            if x != y or type(x) is not type(y):
                if ctx is None:
                    return False
                out.append(ctx)
                return True
    return True


def _parents(root):
    par = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _is_cond(node, par):
    p = par.get(node)
    if isinstance(p, (ast.If, ast.While, ast.IfExp, ast.Assert)) and p.test is node:
        return True
    if isinstance(p, ast.BoolOp):
        # operand of and/or used as a condition
        return _is_cond(p, par) or True
    if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
        return True
    if isinstance(p, ast.comprehension) and node in p.ifs:
        return True
    return False


def _inside(node, par, types):
    p = par.get(node)
    while p is not None:
        if isinstance(p, types):
            return True
        p = par.get(p)
    return False


def instrument(new_src: bytes, pairs, start_id=0):
    """Returns (instrumented_source_bytes, n_sites, complete). complete is False when some leaf
    edit could not be turned into a monitored site."""
    sites = []
    complete = True
    for o, n, header in pairs:
        out = []
        ok = _min_diff(o, n, None, out, header_only=header)
        if not ok or not out:
            complete = False
            continue
        # dedupe nested pairs: keep outermost if one contains another
        uniq = []
        for oe, ne in out:
            if any(ne is u[1] for u in uniq):
                continue
            uniq.append((oe, ne))
        if len(uniq) != 1:
            complete = False
            continue
        oe, ne = uniq[0]
        par = _parents(n)
        if not (_pure(oe) and _pure(ne)):
            complete = False
            continue
        if _inside(ne, par, (ast.JoinedStr, ast.Lambda)) or isinstance(ne, ast.Starred):
            complete = False
            continue
        p = par.get(ne)
        if isinstance(p, ast.keyword) or isinstance(p, (ast.Starred,)):
            # keyword values are fine to wrap; starred not
            pass
        try:
            old_txt = ast.unparse(oe)
        except Exception:  # noqa: BLE001
            complete = False
            continue
        sites.append((ne, old_txt, _is_cond(ne, par)))
    if not sites:
        return None, 0, complete and not pairs
    lines = new_src.split(b"\n")
    # apply from the end so earlier positions stay valid
    sites.sort(key=lambda s: (s[0].lineno, s[0].col_offset), reverse=True)
    prev_start = None
    n_ok = 0
    for i, (ne, old_txt, cond) in enumerate(sites):
        l0, c0, l1, c1 = ne.lineno - 1, ne.col_offset, ne.end_lineno - 1, ne.end_col_offset
        if prev_start is not None and (l1, c1) > prev_start:
            complete = False  # overlapping sites
            continue
        sid = start_id + i
        head = f"_brwm_({sid}, lambda: ({old_txt}), lambda: (".encode()
        tail = f"), {cond})".encode()
        if l0 == l1:
            ln = lines[l0]
            lines[l0] = ln[:c0] + head + ln[c0:c1] + tail + ln[c1:]
        else:
            lines[l0] = lines[l0][:c0] + head + lines[l0][c0:]
            lines[l1] = lines[l1][:c1] + tail + lines[l1][c1:]
        prev_start = (l0, c0)
        n_ok += 1
    out_src = b"\n".join(lines)
    try:
        compile(out_src, "<wm>", "exec")
    except SyntaxError:
        return None, 0, False
    return out_src, n_ok, complete


RUNTIME = '''
def __br_install():
    import builtins
    stats = [0, 0]  # executions, infections

    def _same(a, b, cond):
        if cond:
            return bool(a) == bool(b)
        if a is b:
            return True
        if type(a) is not type(b):
            return False
        try:
            return bool(a == b)
        except Exception:
            return False

    def _brwm_(sid, old, new, cond):
        try:
            ov = old()
            oerr = None
        except Exception as e:
            ov, oerr = None, e
        try:
            nv = new()
        except BaseException:
            stats[0] += 1
            if oerr is None:
                stats[1] += 1
            raise
        stats[0] += 1
        if oerr is not None or not _same(ov, nv, cond):
            stats[1] += 1
        return nv

    builtins._brwm_ = _brwm_
    return stats
'''


def install():
    ns = {}
    exec(RUNTIME, ns)
    return ns["__br_install"]()
