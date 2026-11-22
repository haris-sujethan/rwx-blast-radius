"""Run sqlglot tests in forked children of a pre-warmed zygote process.

The parent keeps a symlink mirror of the read-only repo in /tmp. A commit's patched files are
written into the mirror, a child is forked, the child runs the requested tests with unittest
and streams per-test results back through a pipe, and the mirror is restored afterwards.
"""

from __future__ import annotations

import importlib
import os
import select
import signal
import sys
import time

DEVNULL = os.open(os.devnull, os.O_RDWR)
CPU_LOG = {}
WM_LOG = {}


class TestTimeout(BaseException):
    pass


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
        """Make sure rel_dir is a real directory in the mirror (fixtures dirs are symlinked)."""
        if not rel_dir:
            return
        d = os.path.join(self.dst, rel_dir)
        if os.path.islink(d):
            self._materialize_dir(os.path.dirname(rel_dir))
            target = os.readlink(d)
            os.unlink(d)
            os.makedirs(d)
            self.changed.append((rel_dir, "dirlink", target))
            for name in os.listdir(target):
                os.symlink(os.path.join(target, name), os.path.join(d, name))
        elif not os.path.exists(d):
            self._materialize_dir(os.path.dirname(rel_dir))
            os.makedirs(d)
            self.changed.append((rel_dir, "newdir", None))

    def apply(self, files):
        """files: rel -> new text or None (delete)."""
        for rel, text in files.items():
            self._materialize_dir(os.path.dirname(rel))
            p = os.path.join(self.dst, rel)
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
                    os.unlink(os.path.join(p, name))
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
            if isinstance(e, TestTimeout):
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


def preload(modules):
    for m in modules:
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            print(f"preload {m} failed: {e!r}", file=sys.stderr)


def loaded_files(root):
    out = set()
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.startswith(root):
            out.add(os.path.relpath(f, root))
    return out


# ---------------------------------------------------------------------------------------------
# child side


def _child_run(tests, wfd, purge, per_test_timeout):
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
            if n is not None:
                out.write(f"B {n}\n")
                signal.setitimer(signal.ITIMER_REAL, per_test_timeout.get(n, 60))

        def stopTest(self, test):
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


GC_THRESHOLD = tuple(int(x) for x in os.environ.get("BR_GC", "50000,50,100").split(","))


def run_tests(tests, purge=False, timeouts=None, hard_budget=None, stop_cb=None):
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
                _child_run(remaining, w, purge, timeouts)
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
