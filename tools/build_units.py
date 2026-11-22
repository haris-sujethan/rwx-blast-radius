"""Per-fixture-case ("unit") dependency records for tests that loop over SQL fixtures.

A unit is one item yielded by tests.helpers.load_sql_fixture_pairs / load_sql_fixtures, keyed by
(fixture path, index). Lines executed while the consumer handles that item (including the pool
submit and, for check_file, the as_completed result phase) belong to the unit; everything else
in the test is "nonunit". Only sqlglot/ function lines and instantiated classes are recorded.

Usage: python build_units.py <repo> <tests.json> <outdir> <jobs> <comma-separated test numbers>
"""

import json
import os
import sys
import time

REPO = os.path.abspath(sys.argv[1])
TESTS = json.load(open(sys.argv[2]))["tests"]
OUT = os.path.abspath(sys.argv[3])
JOBS = int(sys.argv[4])
ONLY = set(int(x) for x in sys.argv[5].split(","))
os.makedirs(OUT, exist_ok=True)
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ["SKIP_INTEGRATION"] = "0"

import pytest  # noqa: E402
import _pytest.unittest  # noqa: F401,E402
import unittest  # noqa: F401,E402

PREFIX = os.path.join(REPO, "sqlglot") + os.sep


def child(test, outpath):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "units")
    nonunit = {"lines": {}, "classes": set()}
    units = {}
    state = {"cur": nonunit, "unit": None, "active": False}

    execlines = {}

    def on_line(code, line):
        if state["active"] and code.co_flags & 1:
            fn = code.co_filename
            if fn.startswith(PREFIX):
                d = state["cur"]["lines"]
                s = d.get(fn)
                if s is None:
                    s = d[fn] = set()
                s.add(line)
                if state.get("exec"):
                    execlines.setdefault(fn, set()).add(line)
        return mon.DISABLE

    def on_start(code, offset):
        return on_line(code, code.co_firstlineno)

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)

    times = {}

    def switch(key):
        prev = state["unit"]
        now = time.process_time()
        times[prev] = times.get(prev, 0.0) + now - state.get("t", now)
        state["t"] = now
        state["unit"] = key
        if key is None:
            state["cur"] = nonunit
        else:
            u = units.get(key)
            if u is None:
                u = units[key] = {"lines": {}, "classes": set()}
            state["cur"] = u
        mon.restart_events()
        return prev

    import concurrent.futures as cf
    import pickle

    class SyncExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *a, **k):
            f = cf.Future()
            f._br_unit = state["unit"]
            try:
                fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa: BLE001
                f.set_exception(e)
            return f

        def shutdown(self, *a, **k):
            pass

    cf.ProcessPoolExecutor = SyncExecutor
    orig_ac = cf.as_completed

    def as_completed(fs, timeout=None):
        for f in orig_ac(fs, timeout):
            prev = switch(getattr(f, "_br_unit", None))
            try:
                yield f
            finally:
                switch(prev)

    cf.as_completed = as_completed

    import sqlglot  # noqa: F401
    from sqlglot.expressions import core
    from sqlglot import tokens, parser, generator
    from sqlglot.dialects import dialect
    import tests.helpers as helpers

    def wrap_gen(orig):
        def gen(filename, *a, **k):
            rel = os.path.join("tests", "fixtures", filename)
            for idx, item in enumerate(orig(filename, *a, **k)):
                prev = switch((rel, idx))
                try:
                    yield item
                finally:
                    switch(prev)
        return gen

    helpers.load_sql_fixture_pairs = wrap_gen(helpers.load_sql_fixture_pairs)
    helpers.load_sql_fixtures = wrap_gen(helpers.load_sql_fixtures)

    class UnitList(list):
        """Fixture rows loaded up front (executor setUpClass): a row is 'in use' from the moment
        it is iterated to / indexed until the next row is."""

        def __init__(self, rel, items):
            super().__init__(items)
            self._rel = rel

        def __iter__(self):
            prev = state["unit"]
            try:
                for idx, item in enumerate(list.__iter__(self)):
                    switch((self._rel, idx))
                    yield item
            finally:
                switch(prev)

        def __getitem__(self, i):
            if isinstance(i, int):
                switch((self._rel, i if i >= 0 else len(self) + i))
            return list.__getitem__(self, i)

    if "test_executor" in test["id"]:
        import tests.test_executor as te
        from sqlglot import planner
        from sqlglot.executor import python as expy

        def phase(fn):
            def wrapper(*a, **k):
                outer = state.get("exec")
                state["exec"] = True
                if not outer:
                    mon.restart_events()
                try:
                    return fn(*a, **k)
                finally:
                    state["exec"] = outer
                    if not outer:
                        mon.restart_events()
            return wrapper

        def unphase(fn):
            def wrapper(*a, **k):
                outer = state.get("exec")
                state["exec"] = False
                if outer:
                    mon.restart_events()
                try:
                    return fn(*a, **k)
                finally:
                    state["exec"] = outer
                    if outer:
                        mon.restart_events()
            return wrapper

        import sqlglot.executor as sx

        # engine phase = everything mp_execute does (find_tables, ensure_tables, schema,
        # planning, execution) except the optimizer call, whose output the fingerprint covers
        te.find_tables = phase(te.find_tables)
        te.execute = phase(te.execute)
        sx.optimize = unphase(sx.optimize)
        expy.PythonExecutor.execute = phase(expy.PythonExecutor.execute)
        planner.Plan.__init__ = phase(planner.Plan.__init__)

        orig_setup = te.TestExecutor.setUpClass.__func__

        def setUpClass(cls):
            orig_setup(cls)
            cls.tpch_sqls = UnitList("tests/fixtures/optimizer/tpc-h/tpc-h.sql", cls.tpch_sqls)
            cls.tpcds_sqls = UnitList("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", cls.tpcds_sqls)

        te.TestExecutor.setUpClass = classmethod(setUpClass)

    def wrap(cls):
        orig = cls.__dict__["__init__"]

        def __init__(self, *a, **k):
            state["cur"]["classes"].add(type(self))
            return orig(self, *a, **k)

        cls.__init__ = __init__

    for c in (core.Expr, core.Expression, tokens.Tokenizer, parser.Parser, generator.Generator,
              dialect.Dialect):
        if "__init__" in c.__dict__:
            wrap(c)

    class Phase:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            state["active"] = True
            times.clear()
            state["t"] = time.process_time()
            mon.restart_events()
            yield
            switch(None)
            state["active"] = False

        def pytest_runtest_logreport(self, report):
            if report.failed:
                outcome["failed"] = True

    outcome = {}
    sys.stdout = open(os.devnull, "w")
    rc = pytest.main([test["id"], "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                      "-p", "no:randomly", "-W", "ignore"], plugins=[Phase()])
    mon.set_events(tool, 0)

    def conv(u):
        names = set()
        for c in u["classes"]:
            for b in c.__mro__:
                names.add(f"{b.__module__}:{b.__qualname__}")
        return {"lines": {os.path.relpath(f, REPO): sorted(v) for f, v in u["lines"].items()},
                "classes": sorted(names)}

    rec = {"n": test["number"], "id": test["id"], "rc": int(rc), "failed": bool(outcome.get("failed")),
           "nonunit": conv(nonunit),
           "units": [[k[0], k[1], conv(v), times.get(k, 0.0)] for k, v in units.items()],
           "nonunit_time": times.get(None, 0.0),
           "execlines": {os.path.relpath(f, REPO): sorted(v) for f, v in execlines.items()}}
    with open(outpath, "w") as f:
        json.dump(rec, f)


def main():
    todo = [t for t in TESTS if t["number"] in ONLY]
    todo = [t for t in todo if not os.path.exists(os.path.join(OUT, f"{t['number']}.json"))]
    todo.sort(key=lambda t: -t["seconds"])
    running = {}
    t0 = time.time()
    while todo or running:
        while todo and len(running) < JOBS:
            t = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t, os.path.join(OUT, f"{t['number']}.json"))
                except BaseException:
                    import traceback
                    traceback.print_exc(file=sys.__stderr__)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, _ = os.wait()
        running.pop(pid, None)
    print(f"done {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
