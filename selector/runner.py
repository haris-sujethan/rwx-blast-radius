"""Run sqlglot tests in forked children of a pre-warmed zygote process.

The parent keeps a symlink mirror of the read-only repo in /tmp. A commit's patched files are
written into the mirror, a child is forked, the child runs the requested tests with unittest
and streams per-test results back through a pipe, and the mirror is restored afterwards.
"""

from __future__ import annotations

import importlib
import os
import select
import shutil
import signal
import sys
import time

DEVNULL = os.open(os.devnull, os.O_RDWR)
CPU_LOG = {}
CUR_FILTER = None
FAIL_FAST = os.environ.get("BR_FAIL_FAST", "1") == "1"
WM_LOG = {}


class TestTimeout(BaseException):
    pass


class TestAbort(BaseException):
    """Raised once a test is known to fail: the rest of it is wasted CPU."""


# ---------------------------------------------------------------------------------------------
# mirror of the repository


class Mirror:
    def __init__(self, src, dst):
        self.src = src
        self.dst = dst
        self.changed = []  # (rel, kind) to restore
        os.makedirs(dst, exist_ok=True)
        for top in ("sqlglot", "tests"):
            self._mirror_dir(top)
        for f in os.listdir(src):
            p = os.path.join(src, f)
            if os.path.isfile(p):
                os.symlink(p, os.path.join(dst, f))

    def _mirror_dir(self, rel):
        s = os.path.join(self.src, rel)
        d = os.path.join(self.dst, rel)
        os.makedirs(d, exist_ok=True)
        with os.scandir(s) as it:
            for e in it:
                r = os.path.join(rel, e.name)
                if e.is_dir(follow_symlinks=False):
                    if e.name == "__pycache__" or rel.startswith("tests/fixtures"):
                        os.symlink(e.path, os.path.join(self.dst, r))
                    else:
                        self._mirror_dir(r)
                else:
                    os.symlink(e.path, os.path.join(self.dst, r))

    def _materialize_dir(self, rel_dir):
        """Make every component of rel_dir a real directory inside the mirror (fixture
        directories are mirrored as symlinks to the read-only source)."""
        if not rel_dir:
            return
        cur = ""
        for part in rel_dir.split("/"):
            cur = os.path.join(cur, part) if cur else part
            d = os.path.join(self.dst, cur)
            if os.path.islink(d):
                target = os.readlink(d)
                os.unlink(d)
                os.mkdir(d)
                self.changed.append((cur, "dirlink", target))
                if os.path.isdir(target):
                    for name in os.listdir(target):
                        os.symlink(os.path.join(target, name), os.path.join(d, name))
            elif not os.path.exists(d):
                os.mkdir(d)
                self.changed.append((cur, "newdir", None))

    def apply(self, files):
        """files: rel -> new text or None (delete)."""
        real_dst = os.path.realpath(self.dst)
        for rel, text in files.items():
            rel = os.path.normpath(rel)
            if rel.startswith("..") or os.path.isabs(rel):
                continue
            self._materialize_dir(os.path.dirname(rel))
            p = os.path.join(self.dst, rel)
            if not os.path.realpath(os.path.dirname(p)).startswith(real_dst):
                raise RuntimeError(f"refusing to write outside the mirror: {rel}")
            if os.path.isdir(p) and not os.path.islink(p):
                continue
            existed = os.path.lexists(p)
            target = os.readlink(p) if existed and os.path.islink(p) else None
            if existed:
                os.unlink(p)
            self.changed.append((rel, "file", target))
            if text is not None:
                with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                    f.write(text)

    def restore(self):
        for rel, kind, target in reversed(self.changed):
            p = os.path.join(self.dst, rel)
            if kind == "file":
                if os.path.lexists(p):
                    os.unlink(p)
                if target is not None:
                    os.symlink(target, p)
            elif kind == "dirlink":
                for name in os.listdir(p):
                    q = os.path.join(p, name)
                    if os.path.islink(q) or not os.path.isdir(q):
                        os.unlink(q)
                    else:
                        shutil.rmtree(q, ignore_errors=True)
                os.rmdir(p)
                os.symlink(target, p)
            elif kind == "newdir":
                try:
                    os.rmdir(p)
                except OSError:
                    pass
        self.changed = []


# ---------------------------------------------------------------------------------------------
# zygote preparation


class SyncExecutor:
    """Stands in for ProcessPoolExecutor: runs in-process, pickling like a real pool would."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
        import pickle

        self._pickle = pickle
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        import concurrent.futures as cf

        pickle = self._pickle
        f = cf.Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, (TestTimeout, TestAbort)):
                raise
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, **k):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass


def prepare_base(mirror_dir):
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = mirror_dir
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.chdir(mirror_dir)
    if mirror_dir not in sys.path:
        sys.path.insert(0, mirror_dir)
    import concurrent.futures as cf
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401

    cf.ProcessPoolExecutor = SyncExecutor
    import concurrent.futures.process as cfp

    cfp.ProcessPoolExecutor = SyncExecutor


def wrap_helpers():
    """Let fixture loops yield only the cases selected for the running test."""
    h = sys.modules.get("tests.helpers")
    if h is None or getattr(h, "_br_wrapped", False):
        return

    def mk(orig):
        def gen(filename, *a, **k):
            flt = CUR_FILTER
            allowed = None
            if flt is not None:
                allowed = flt.get(os.path.join("tests", "fixtures", filename), ())
            for idx, item in enumerate(orig(filename, *a, **k)):
                if allowed is None or idx in allowed:
                    yield item

        gen.__name__ = orig.__name__
        gen.__wrapped__ = orig
        return gen

    h.load_sql_fixture_pairs = mk(h.load_sql_fixture_pairs)
    h.load_sql_fixtures = mk(h.load_sql_fixtures)
    h._br_wrapped = True


def presetup_executor():
    """Run TestExecutor.setUpClass once in the zygote (TPC data loading), with single-threaded
    duckdb connections so forked children can use them; children skip it."""
    te = sys.modules.get("tests.test_executor")
    if te is None or getattr(te.TestExecutor, "_br_presetup", False):
        return
    try:
        import duckdb

        orig_connect = duckdb.connect

        def connect(*a, **k):
            cfg = dict(k.pop("config", None) or {})
            cfg.setdefault("threads", 1)
            return orig_connect(*a, config=cfg, **k)

        te.duckdb.connect = connect
        try:
            te.TestExecutor.setUpClass()
        finally:
            te.duckdb.connect = orig_connect
        te.TestExecutor.setUpClass = classmethod(lambda cls: None)
        te.TestExecutor.tearDownClass = classmethod(lambda cls: None)
        te.TestExecutor._br_presetup = True
    except BaseException as e:  # noqa: BLE001
        print(f"executor presetup failed: {e!r}", file=sys.stderr)


def preload(modules):
    for m in modules:
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            print(f"preload {m} failed: {e!r}", file=sys.stderr)
        if m == "tests.helpers":
            wrap_helpers()


def loaded_files(root):
    out = set()
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.startswith(root):
            out.add(os.path.relpath(f, root))
    return out


# ---------------------------------------------------------------------------------------------
# child side


def _child_run(tests, wfd, purge, per_test_timeout, filters=None):
    """tests: list of (number, module, cls, method, timeout_s)."""
    import unittest

    import wm

    wm_stats = wm.install()

    import gc

    gc.set_threshold(*GC_THRESHOLD)
    os.dup2(DEVNULL, 1)
    os.dup2(DEVNULL, 2)
    out = os.fdopen(wfd, "w", buffering=1)

    if purge:
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or \
                    name.startswith("tests."):
                del sys.modules[name]

    filters = filters or {}
    try:
        importlib.import_module("tests.helpers")
        wrap_helpers()
    except BaseException:  # noqa: BLE001
        pass

    def on_alarm(signum, frame):
        raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)

    class Result(unittest.TestResult):
        def __init__(self, nums):
            super().__init__()
            self.nums = nums
            self.cur_failed = False

        def startTest(self, test):
            super().startTest(test)
            self.cur_failed = False
            self.t0 = time.process_time()
            self.w0 = tuple(wm_stats)
            n = self.nums.get(test._testMethodName)
            global CUR_FILTER
            CUR_FILTER = filters.get(n)
            self.saved = []
            if CUR_FILTER:
                # fixture rows loaded up front in setUpClass (executor TPC-H / TPC-DS)
                cls = type(test)
                for attr, rel in (("tpch_sqls", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"),
                                  ("tpcds_sqls", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")):
                    rows = getattr(cls, attr, None)
                    if isinstance(rows, list):
                        keep = CUR_FILTER.get(rel, ())
                        self.saved.append((cls, attr, rows))
                        setattr(cls, attr, [r for i, r in enumerate(rows) if i in keep])
            if n is not None:
                out.write(f"B {n}\n")
                signal.setitimer(signal.ITIMER_REAL, per_test_timeout.get(n, 60))

        def stopTest(self, test):
            global CUR_FILTER
            CUR_FILTER = None
            for cls, attr, rows in getattr(self, "saved", ()):
                setattr(cls, attr, rows)
            self.saved = []
            signal.setitimer(signal.ITIMER_REAL, 0)
            n = self.nums.get(test._testMethodName)
            if n is not None:
                ex, inf = wm_stats[0] - self.w0[0], wm_stats[1] - self.w0[1]
                if ex:
                    out.write(f"W {n} {ex} {inf}\n")
                out.write(f"E {n} {1 if self.cur_failed else 0} {time.process_time() - self.t0:.4f}\n")
            super().stopTest(test)

        def addError(self, test, err):
            self.cur_failed = True
            if not hasattr(test, "_testMethodName"):
                # class/module level fixture error
                out.write("X\n")

        def addFailure(self, test, err):
            self.cur_failed = True

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.cur_failed = True
                if FAIL_FAST and not issubclass(err[0], (TestAbort, TestTimeout)):
                    raise TestAbort()

        def addUnexpectedSuccess(self, test):
            self.cur_failed = True

    groups = {}
    order = []
    for num, mod, cls, meth in tests:
        key = (mod, cls)
        if key not in groups:
            groups[key] = {}
            order.append(key)
        groups[key][meth] = num

    for mod, clsname in order:
        nums = groups[(mod, clsname)]
        try:
            m = importlib.import_module(mod)
            cls = getattr(m, clsname)
        except BaseException:  # noqa: BLE001
            for n in nums.values():
                out.write(f"E {n} 1\n")
            continue
        suite = unittest.TestSuite()
        for meth in nums:
            try:
                suite.addTest(cls(meth))
            except BaseException:  # noqa: BLE001
                out.write(f"E {nums[meth]} 1\n")
        result = Result(nums)
        try:
            suite.run(result)
        except BaseException:  # noqa: BLE001
            out.write("X\n")
        out.write(f"G {' '.join(str(n) for n in nums.values())}\n")
    out.write("D\n")
    out.flush()


def run_func(func, args, purge=False, timeout=60.0):
    """Run func(*args) in a forked child (sees the patched mirror). Returns its JSON-able result,
    or None if it failed, crashed or timed out."""
    import gc
    import json

    gc.collect()
    gc.freeze()
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            gc.set_threshold(*GC_THRESHOLD)
            os.dup2(DEVNULL, 1)
            os.dup2(DEVNULL, 2)
            if purge:
                for name in list(sys.modules):
                    if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or \
                            name.startswith("tests."):
                        del sys.modules[name]
            try:
                importlib.import_module("tests.helpers")
                wrap_helpers()
            except BaseException:  # noqa: BLE001
                pass
            res = func(*args)
            data = json.dumps(res).encode()
            with os.fdopen(w, "wb") as f:
                f.write(data)
        except BaseException:  # noqa: BLE001
            pass
        finally:
            os._exit(0)
    os.close(w)
    chunks = []
    end = time.monotonic() + timeout
    ok = True
    while True:
        left = end - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            ok = False
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        c = os.read(r, 1 << 16)
        if not c:
            break
        chunks.append(c)
    os.close(r)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    if not ok or not chunks:
        return None
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return None


GC_THRESHOLD = tuple(int(x) for x in os.environ.get("BR_GC", "50000,50,100").split(","))


def run_tests(tests, purge=False, timeouts=None, hard_budget=None, stop_cb=None, deadline=None,
              filters=None):
    """Fork a child of the current process to run tests. Returns {number: failed_bool}.

    tests: list of (number, module, cls, method). Tests whose result never arrives count as
    failed. If a test hangs past its timeout the child is killed, the test is marked failed and
    the remaining tests are run in a fresh child.
    """
    timeouts = timeouts or {}
    results = {}
    remaining = list(tests)
    import gc

    WM_LOG.clear()
    gc.collect()
    gc.freeze()
    while remaining:
        sys.stdout.flush()
        sys.stderr.flush()
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                _child_run(remaining, w, purge, timeouts, filters)
            finally:
                os._exit(0)
        os.close(w)
        buf = b""
        current = None
        current_start = None
        class_error_groups = False
        done = False
        killed = False
        stopped = False
        while not done and not stopped:
            now = time.monotonic()
            if current is not None:
                limit = timeouts.get(current, 60) + 5
                wait = max(0.05, current_start + limit - now)
            else:
                wait = 30
            if deadline is not None:
                if now > deadline:
                    os.kill(pid, signal.SIGKILL)
                    stopped = True
                    break
                wait = min(wait, max(0.05, deadline - now))
            ready, _, _ = select.select([r], [], [], wait)
            if not ready:
                if current is not None and time.monotonic() - current_start > \
                        timeouts.get(current, 60) + 5:
                    os.kill(pid, signal.SIGKILL)
                    killed = True
                    break
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                parts = line.decode().split()
                if not parts:
                    continue
                if parts[0] == "B":
                    current = int(parts[1])
                    current_start = time.monotonic()
                elif parts[0] == "E":
                    results[int(parts[1])] = parts[2] == "1"
                    if len(parts) > 3:
                        CPU_LOG[int(parts[1])] = float(parts[3])
                    current = None
                    if stop_cb is not None and stop_cb(int(parts[1]), parts[2] == "1"):
                        os.kill(pid, signal.SIGKILL)
                        stopped = True
                        break
                elif parts[0] == "W":
                    WM_LOG[int(parts[1])] = (int(parts[2]), int(parts[3]))
                elif parts[0] == "X":
                    class_error_groups = True
                elif parts[0] == "G":
                    if class_error_groups:
                        for n in parts[1:]:
                            results[int(n)] = True
                    class_error_groups = False
                elif parts[0] == "D":
                    done = True
        os.close(r)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        if stopped:
            return results  # caller asked to stop: unrun tests have no result
        if current is not None:
            results[current] = True  # hung or crashed while running
        remaining = [t for t in remaining if t[0] not in results]
        if not done and (killed or current is not None):
            continue  # rerun what is left in a fresh child
        # finished (or died outside a test): anything without a result counts as failed
        for t in remaining:
            results[t[0]] = True
        remaining = []
    return results
