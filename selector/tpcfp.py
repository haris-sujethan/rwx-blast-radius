"""Fingerprints of what tests/test_executor.py's TPC-H / TPC-DS checks depend on, per query.

For query i the executor test compares
    execute(pickle(parse_one(sql)), schema, tables)    (sqlglot's Python engine)
with duckdb running transpile(sql, write="duckdb")[0].
When the executor, planner and Python code generator are untouched, the engine's answer is a
function of the optimized expression it plans, so a query whose optimized expression (as a full
serde dump, types included) and duckdb SQL are both unchanged gives the same comparison result.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sys

TPCH = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
TPCDS = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"

# patches touching these can change the engine's behaviour without changing the fingerprint
UNSAFE_PREFIXES = (
    "sqlglot/executor/", "sqlglot/planner.py", "sqlglot/generator.py", "sqlglot/dialects/dialect.py",
    "sqlglot/expressions/", "sqlglot/helper.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py",
    "sqlglot/__init__.py", "sqlglot/errors.py", "sqlglot/time.py", "sqlglot/trie.py",
    "sqlglot/_typing.py", "tests/",
)


SAFE_FIXTURES = ("tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")


ENGINE = ("sqlglot/executor/", "sqlglot/planner.py")


def safe_for(files, req=None, execlines=None):
    """True when the engine (planner + executor run) cannot behave differently for an
    unchanged fingerprint: engine files untouched and, given the recorded lines the engine
    phase executes, no changed line runs inside it."""
    for f in files:
        if f.startswith(SAFE_FIXTURES):
            continue  # the fingerprint re-reads the (patched) fixture rows
        if not f.startswith("sqlglot/"):
            return False
        if f.startswith(ENGINE):
            return False
        if execlines is None and f.startswith(UNSAFE_PREFIXES):
            return False
    if execlines is not None:
        if req is None or req["full"] or req["classes"] or req.get("conj"):
            return False
        for rel, lines in req["lines"].items():
            el = execlines.get(rel)
            if el and not el.isdisjoint(lines):
                return False
    return True


def fingerprints(rel, idxs=None):
    """{idx: hex} for executable rows of the fixture (all rows when idxs is None)."""
    from sqlglot import parse_one, serde, transpile
    from sqlglot.executor import ensure_tables  # noqa: F401  (import side effects match)
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    loader = getattr(load_sql_fixture_pairs, "__wrapped__", load_sql_fixture_pairs)
    schema = TPCH_SCHEMA if rel == TPCH else TPCDS_SCHEMA
    fname = rel[len("tests/fixtures/"):]
    out = {}
    for i, (meta, sql, _expected) in enumerate(loader(fname)):
        if idxs is not None and i not in idxs:
            continue
        if not meta.get("execute"):
            continue
        h = hashlib.sha1()
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
            s = pickle.loads(pickle.dumps(schema))
            opt = optimize(e, s, leave_tables_isolated=True, dialect=None)
            h.update(json.dumps(serde.dump(opt), sort_keys=True, default=repr).encode())
            h.update(b"\0")
            h.update(transpile(sql, write="duckdb")[0].encode())
        except BaseException as ex:  # noqa: BLE001
            h.update(f"error:{type(ex).__name__}:{ex}".encode()[:500])
        out[i] = h.hexdigest()
    return out


if __name__ == "__main__":
    # build time: python tpcfp.py <repo> <out.json>
    import os

    repo = os.path.abspath(sys.argv[1])
    os.chdir(repo)
    sys.path.insert(0, repo)
    res = {rel: fingerprints(rel) for rel in (TPCH, TPCDS)}
    json.dump(res, open(sys.argv[2], "w"))
    print({k: len(v) for k, v in res.items()}, file=sys.stderr)
