"""Build a per-test dependency record by running every test in its own forked process.

For each test we record:
  imp     lines executed before the test started (imports, collection)
  run     lines executed during setup/call/teardown
  opens   repo files/dirs opened or listed (fixtures, sources read as data)
  classes Dialect/Tokenizer/Parser/Generator/Expression classes instantiated (full MRO)
Usage: python build_map.py <repo> <tests.json> <outdir> [jobs]
"""

import json
import os
import sys
import time

REPO = os.path.abspath(sys.argv[1])
TESTS = json.load(open(sys.argv[2]))["tests"]
OUT = os.path.abspath(sys.argv[3])
JOBS = int(sys.argv[4]) if len(sys.argv) > 4 else 8
ONLY = set(int(x) for x in sys.argv[5].split(",")) if len(sys.argv) > 5 else None

os.makedirs(OUT, exist_ok=True)
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ["SKIP_INTEGRATION"] = "0"

import pytest  # noqa: E402  (preloaded in the zygote)
import _pytest.unittest  # noqa: F401,E402
import _pytest.python  # noqa: F401,E402
import unittest  # noqa: F401,E402

PREFIX = REPO + os.sep


def child(test, outpath):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "map")
    state = {"cur": {}}
    imp = state["cur"]

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(PREFIX):
            s = state["cur"].get(fn)
            if s is None:
                s = state["cur"][fn] = set()
            # function-code lines positive, module/class-body lines negative
            s.add(line if code.co_flags & 1 else -line)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)

    opens = set()
    import builtins
    import io

    real_open = builtins.open

    def rec(p):
        try:
            p = os.fspath(p)
        except TypeError:
            return
        if isinstance(p, bytes):
            p = p.decode()
        if not isinstance(p, str):
            return
        ap = os.path.abspath(p)
        if ap.startswith(PREFIX):
            opens.add(os.path.relpath(ap, REPO))

    def my_open(file, *a, **k):
        rec(file)
        return real_open(file, *a, **k)

    builtins.open = my_open
    io.open = my_open
    real_listdir, real_scandir = os.listdir, os.scandir

    def my_listdir(p="."):
        rec(p)
        return real_listdir(p)

    def my_scandir(p="."):
        rec(p)
        return real_scandir(p)

    os.listdir = my_listdir
    os.scandir = my_scandir

    classes = set()
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
            try:
                fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa: BLE001
                f.set_exception(e)
            return f

        def shutdown(self, *a, **k):
            pass

    cf.ProcessPoolExecutor = SyncExecutor
    import sqlglot  # noqa: F401
    from sqlglot.expressions import core
    from sqlglot import tokens, parser, generator
    from sqlglot.dialects import dialect

    def wrap(cls):
        orig = cls.__dict__["__init__"]

        def __init__(self, *a, **k):
            classes.add(type(self))
            return orig(self, *a, **k)

        cls.__init__ = __init__

    for c in (core.Expr, core.Expression, tokens.Tokenizer, parser.Parser, generator.Generator,
              dialect.Dialect):
        if "__init__" in c.__dict__:
            wrap(c)

    class Phase:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            state["cur"] = run
            mon.restart_events()
            yield
            state["cur"] = {}

        def pytest_runtest_logreport(self, report):
            if report.failed:
                outcome["failed"] = True
            if report.when == "call":
                outcome["call"] = report.outcome

    run = {}
    outcome = {}
    t0 = time.time()
    if not os.environ.get("MAPDEBUG"): sys.stdout = open(os.devnull, "w")
    rc = pytest.main([test["id"], "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
                      "-p", "no:randomly", "-W", "ignore"], plugins=[Phase()])
    dt = time.time() - t0
    mon.set_events(tool, 0)
    builtins.open = real_open
    io.open = real_open

    def conv(d):
        return {os.path.relpath(f, REPO): sorted(v) for f, v in d.items()}

    names = set()
    for c in classes:
        for b in c.__mro__:
            names.add(f"{b.__module__}:{b.__qualname__}")
    rec_ = {"n": test["number"], "id": test["id"], "rc": int(rc), "failed": bool(outcome.get("failed")),
            "dur": dt, "imp": conv(imp), "run": conv(run), "opens": sorted(opens),
            "classes": sorted(names)}
    with real_open(outpath, "w") as f:
        json.dump(rec_, f)


def main():
    todo = [t for t in TESTS if ONLY is None or t["number"] in ONLY]
    todo = [t for t in todo if not os.path.exists(os.path.join(OUT, f"{t['number']}.json"))]
    # longest first for better packing
    todo.sort(key=lambda t: -t["seconds"])
    running = {}
    t0 = time.time()
    done = 0
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
        pid, status = os.wait()
        running.pop(pid, None)
        done += 1
        if done % 100 == 0:
            print(f"{done} done, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    print(f"all done {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
