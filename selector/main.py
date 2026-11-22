"""Blast Radius selector: pick the tests each sqlglot patch can break, run the cheap ones to see
which really fail, and print a worker split per commit."""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/app/repo")
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "map.bin"))
COSTS = os.environ.get("BR_COSTS", os.path.join(HERE, "costs.json"))
UNITS = os.environ.get("BR_UNITS", os.path.join(HERE, "units.bin"))
TPCFP = os.environ.get("BR_TPCFP", "/app/tpcfp.json")
EXEC_TPC = {"tests/test_executor.py::TestExecutor::test_execute_tpcds": "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
            "tests/test_executor.py::TestExecutor::test_execute_tpch": "tests/fixtures/optimizer/tpc-h/tpc-h.sql"}
STAGES = os.environ.get("BR_STAGES", "/app/stages.json")
MIRROR = os.environ.get("BR_MIRROR", "/tmp/m")
WALL_BUDGET = float(os.environ.get("BR_WALL", "140"))
CPU_SCALE = float(os.environ.get("BR_CPU_SCALE", "1.3"))
CHEAP_MAX = float(os.environ.get("BR_CHEAP_MAX", "1.5"))
RUN_CAP = float(os.environ.get("BR_RUN_CAP", "20"))
WORKER_MS = 2000
MAX_WORKERS = 8

T0 = time.monotonic()
STATE = {"commits": [], "printed": set(), "cands": {}, "dur_ms": None, "ntests": 0}


def log(*a):
    print(f"[{time.monotonic() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# packing


def pack(nums, dur_ms):
    """Split tests over up to 8 workers minimising max load + 2 s per worker."""
    nums = sorted(set(nums), key=lambda n: -dur_ms[n])
    if not nums:
        return []
    total = sum(dur_ms[n] for n in nums)
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        if best is not None and total / k + WORKER_MS * k > best[0] + 1e9:
            break
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in nums:
            i = loads.index(min(loads))
            loads[i] += dur_ms[n]
            bins[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [b for b in bins if b])
    return best[1]


def pack_cost(nums, dur_ms):
    b = pack(nums, dur_ms)
    if not b:
        return 0
    return max(sum(dur_ms[n] for n in w) for w in b) + WORKER_MS * len(b)


# ---------------------------------------------------------------------------------------------


def emit_line(cid, nums):
    if cid in STATE["printed"]:
        return
    workers = pack(nums, STATE["dur_ms"])
    line = json.dumps({"commit": cid, "workers": workers}, separators=(",", ":"))
    STATE["printed"].add(cid)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def fallback():
    """Every commit must get exactly one line, whatever happened."""
    for cid in STATE["commits"]:
        if cid in STATE["printed"]:
            continue
        nums = STATE["cands"].get(cid)
        if nums is None:
            nums = range(STATE["ntests"])
        try:
            emit_line(cid, nums)
        except Exception:  # noqa: BLE001
            line = json.dumps({"commit": cid, "workers": [list(range(STATE["ntests"]))]})
            STATE["printed"].add(cid)
            sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ntests = len(tests)
    dur_ms = [0] * ntests
    tid = [None] * ntests
    for t in tests:
        dur_ms[t["number"]] = int(round(t["seconds"] * 1000))
        tid[t["number"]] = t["id"]
    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))
    STATE["commits"] = commits
    log(f"{len(commits)} commits, {ntests} tests")

    from candidates import MapDB, bits, select

    db = MapDB(MAP, REPO)
    try:
        from units import FULL, SKIP, UnitDB

        udb = UnitDB(UNITS)
    except Exception as e:  # noqa: BLE001
        log(f"no unit map: {e!r}")
        udb = None
    STATE["udb"] = udb
    # map test ids -> numbers in this tests.json (ids are stable, numbers might not be)
    id2num = {t["id"]: t["number"] for t in tests}
    mapnum2num = [id2num.get(i) for i in db.ids]
    id2mapnum = {i: k for k, i in enumerate(db.ids)}
    try:
        costs = json.load(open(COSTS))
    except OSError:
        costs = {}
    est_cpu = [0.0] * ntests
    for t in tests:
        c = costs.get(t["id"])
        est_cpu[t["number"]] = (c if c is not None else t["seconds"]) * CPU_SCALE

    STATE["dur_ms"] = dur_ms
    STATE["ntests"] = ntests
    emit = emit_line

    jobs = []  # (cid, files, run_nums, blind_nums)
    for cid in commits:
        try:
            text = open(os.path.join(commits_dir, cid + ".patch"), encoding="utf-8",
                        errors="surrogateescape").read()
            sel = select(db, text)
            cands = set()
            for mn in bits(sel.cands):
                n = mapnum2num[mn] if mn < len(mapnum2num) else None
                if n is not None:
                    cands.add(n)
        except Exception as e:  # noqa: BLE001
            log(f"{cid}: analysis failed {e!r}; running everything blind")
            emit(cid, list(range(ntests)))
            continue
        STATE["cands"][cid] = cands
        filters, estc = {}, {}
        if udb is not None:
            for n in list(cands):
                mn = id2mapnum.get(tid[n])
                if mn is None:
                    continue
                try:
                    plan, frac = udb.plan(tid[n], 1 << mn, sel.req, sel.fixture_texts)
                except Exception as e:  # noqa: BLE001
                    log(f"{cid}: unit plan failed for {n}: {e!r}")
                    continue
                if plan == SKIP:
                    cands.discard(n)
                elif plan != FULL:
                    filters[n] = plan
                    estc[n] = est_cpu[n] * frac + 0.01 * CPU_SCALE
        sel.filters = filters
        sel.estc = estc

        def E(n):
            return estc.get(n, est_cpu[n])

        cheap = {n for n in cands if E(n) <= CHEAP_MAX}
        expensive = cands - cheap
        log(f"{cid}: cands={len(cands)} cheap={len(cheap)} exp={sorted(expensive)} "
            f"est_cpu={sum(E(n) for n in cheap):.2f} mon={sel.monitorable} "
            f"sites={sel.sites} filtered={len(filters)}")
        if not cands:
            emit(cid, [])
            continue
        jobs.append((cid, sel, cheap, expensive))

    if jobs:
        execute(jobs, tid, est_cpu, dur_ms, emit)
    log("done")


def execute(jobs, tid, est_cpu, dur_ms, emit):
    import runner

    mirror = runner.Mirror(REPO, MIRROR)
    runner.prepare_base(MIRROR)
    try:
        stages = json.load(open(STAGES))
    except OSError:
        stages = [{"modules": ["sqlglot"], "files": None}]

    def modname(n):
        return tid[n].split("::")[0][:-3].replace("/", ".")

    def spec_of(nums):
        out = []
        for n in sorted(nums):
            parts = tid[n].split("::")
            out.append((n, parts[0][:-3].replace("/", "."), parts[1], parts[-1]))
        return out

    need_heavy = any(modname(n) in ("tests.test_optimizer", "tests.test_executor")
                     for _, _, cheap, exp in jobs for n in cheap | exp)
    if need_heavy:
        runner.preload(["numpy", "pandas", "duckdb", "pandas.testing"])

    def level(files):
        k = 0
        for st in stages:
            sf = st.get("files")
            if sf is None or any(f in sf for f in files):
                break
            k += 1
        return k

    pending = sorted(((level(sel.files), cid, sel, cheap, exp) for cid, sel, cheap, exp in jobs),
                     key=lambda j: j[0])
    try:
        tpc_orig = json.load(open(TPCFP))
    except OSError:
        tpc_orig = None

    def tpc_check(cid, sel, expensive, fail, purge, E):
        """Executor TPC tests: only queries whose plan input / duckdb SQL changed can differ."""
        import tpcfp

        udb = STATE.get("udb")
        if tpc_orig is None or not tpcfp.safe_for(sel.files):
            return
        for n in list(expensive):
            rel = EXEC_TPC.get(tid[n])
            if rel is None:
                continue
            orig = {int(k): v for k, v in tpc_orig.get(rel, {}).items()}
            flt = sel.filters.get(n)
            idxs = set(flt.get(rel, ())) if flt else set(orig)
            idxs &= set(orig)
            base = pack_cost(fail, dur_ms)
            marginal = (pack_cost(fail | {n}, dur_ms) - base) / 1000
            est_fp = 0.07 * CPU_SCALE * len(idxs) + 0.3
            if est_fp >= marginal:
                continue
            res = runner.run_func(tpcfp.fingerprints, (rel, sorted(idxs)), purge=purge,
                                  timeout=max(10.0, 4 * est_fp))
            if res is None:
                log(f"{cid}: tpc fingerprint failed for {n}")
                continue
            res = {int(k): v for k, v in res.items()}
            changed = {i for i in idxs if res.get(i) != orig.get(i)}
            log(f"{cid}: tpc {tid[n].split('::')[-1]}: {len(changed)}/{len(idxs)} changed")
            if not changed:
                expensive.discard(n)
                continue
            sel.filters[n] = {rel: changed}
            frac = udb.frac_for(tid[n], rel, changed) if udb is not None else None
            if frac is not None:
                sel.estc[n] = est_cpu[n] * frac + 0.01 * CPU_SCALE

    def serve(cid, sel, cheap, expensive, lvl):
        def E(n):
            return sel.estc.get(n, est_cpu[n])

        loaded = runner.loaded_files(MIRROR + os.sep)
        purge = any(f in loaded for f in sel.files)
        now = time.monotonic()
        if now - T0 > WALL_BUDGET - 3:
            log(f"{cid}: out of time, blind")
            emit(cid, cheap | expensive)
            return
        est_cheap = sum(E(n) for n in cheap)
        blind_all = pack_cost(cheap | expensive, dur_ms) / 1000
        if est_cheap > min(blind_all + 1.0, RUN_CAP):
            log(f"{cid}: running cheap tests ({est_cheap:.1f}s) costs more than including "
                f"everything ({blind_all:.1f}s); blind")
            emit(cid, cheap | expensive)
            return
        deadline = min(T0 + WALL_BUDGET, now + max(15.0, 3 * est_cheap + 10))
        files = dict(sel.files)
        files.update(sel.instr)
        mirror.apply(files)
        c0 = os.times()
        try:
            failing = set()
            wm_ex = wm_inf = 0

            import tpcfp

            tpc_maybe = set()
            if tpc_orig is not None and tpcfp.safe_for(sel.files):
                tpc_maybe = {n for n in expensive if tid[n] in EXEC_TPC}

            def plan_expensive(fail, exclude=()):
                blind_, run_ = set(), set()
                cur = set(fail)
                for n in sorted(expensive, key=lambda n: -dur_ms[n]):
                    if n in exclude:
                        continue
                    base = pack_cost(cur, dur_ms)
                    marginal = (pack_cost(cur | {n}, dur_ms) - base) / 1000
                    if E(n) < marginal:
                        run_.add(n)
                    else:
                        blind_.add(n)
                        cur.add(n)
                return blind_, run_

            seen, seen_fail = set(), set()

            def stop_cb(n, failed):
                seen.add(n)
                if not failed:
                    return False
                seen_fail.add(n)
                rest = cheap - seen
                if not rest:
                    return False
                b, _ = plan_expensive(seen_fail, exclude=tpc_maybe)
                base = seen_fail | b
                marginal = (pack_cost(base | rest, dur_ms) - pack_cost(base, dur_ms)) / 1000
                return marginal <= 0.8 * sum(E(m) for m in rest)

            unrun = set()
            if cheap:
                res = runner.run_tests(spec_of(cheap), purge=purge, stop_cb=stop_cb,
                                       timeouts={n: max(3.0, E(n) * 4 + 2) for n in cheap},
                                       deadline=deadline, filters=sel.filters)
                failing |= {n for n, f in res.items() if f}
                unrun = cheap - set(res)
                for n in cheap:
                    ex, inf = runner.WM_LOG.get(n, (0, 0))
                    wm_ex += ex
                    wm_inf += inf
            clean = (sel.monitorable and sel.sites > 0 and wm_ex > 0 and wm_inf == 0
                     and not failing and not unrun)
            blind = set(unrun)
            run_b = set()
            if expensive and not clean:
                tpc_check(cid, sel, expensive, failing | unrun, purge, E)
            if expensive and not clean:
                b, run_b = plan_expensive(failing | unrun)
                blind |= b
                if run_b:
                    res = runner.run_tests(spec_of(run_b), purge=purge,
                                           timeouts={n: max(3.0, E(n) * 4 + 2) for n in run_b},
                                           deadline=deadline, filters=sel.filters)
                    failing |= {n for n, f in res.items() if f} | {n for n in run_b if n not in res}
        finally:
            mirror.restore()
        c1 = os.times()
        used = (c1.children_user + c1.children_system) - (c0.children_user + c0.children_system)
        log(f"{cid}: lvl={lvl} purge={purge} cheap={len(cheap)} failing={len(failing)} "
            f"wm={wm_ex}/{wm_inf} clean={clean} exp={len(expensive)} run_b={sorted(run_b)} "
            f"blind={sorted(blind)} cpu={used:.2f} testcpu={sum(runner.CPU_LOG.get(n, 0) for n in cheap | run_b):.2f} "
            f"top={sorted(((round(runner.CPU_LOG.get(n, 0), 2), tid[n].split('::')[-1]) for n in cheap | run_b), reverse=True)[:3]}")
        emit(cid, failing | blind)

    cur_level = 0
    for lvl, cid, sel, cheap, expensive in pending:
        while cur_level < lvl:
            runner.preload(stages[cur_level]["modules"])
            cur_level += 1
        try:
            serve(cid, sel, cheap, expensive, lvl)
        except Exception as e:  # noqa: BLE001
            import traceback

            log(f"{cid}: execution failed {e!r}; blind")
            traceback.print_exc(file=sys.stderr)
            try:
                mirror.restore()
            except Exception:  # noqa: BLE001
                pass
            emit(cid, cheap | expensive)


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001
        import traceback

        traceback.print_exc(file=sys.stderr)
    finally:
        try:
            fallback()
        except BaseException:  # noqa: BLE001
            pass
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
