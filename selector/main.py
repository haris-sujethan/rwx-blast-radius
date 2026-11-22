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
PROXY_IDS = set() if os.environ.get("BR_NO_PROXY") == "1" else {
    "tests/test_parser.py::TestParser::test_max_nodes"}
EXEC_TPC = {"tests/test_executor.py::TestExecutor::test_execute_tpcds": "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql",
            "tests/test_executor.py::TestExecutor::test_execute_tpch": "tests/fixtures/optimizer/tpc-h/tpc-h.sql"}
STAGES = os.environ.get("BR_STAGES", "/app/stages.json")
MIRROR = os.environ.get("BR_MIRROR", "/tmp/m")
WALL_BUDGET = float(os.environ.get("BR_WALL", "140"))
CPU_SCALE = float(os.environ.get("BR_CPU_SCALE", "2.4"))
CHEAP_MAX = float(os.environ.get("BR_CHEAP_MAX", "1.5"))
RUN_CAP = float(os.environ.get("BR_RUN_CAP", "20"))
CLEAN_SKIP_MIN = float(os.environ.get("BR_CLEAN_SKIP_MIN", "20"))
CLEAN_MIN_EXEC = int(os.environ.get("BR_CLEAN_MIN_EXEC", "50"))
WORKER_MS = 2000
MAX_WORKERS = 8

T0 = time.monotonic()
ID2MAP = {}
STATE = {"commits": [], "printed": set(), "cands": {}, "dur_ms": None, "ntests": 0}


def log(*a):
    t = os.times()
    print(f"[{time.monotonic() - T0:6.2f} c{t.user + t.system:5.2f}]", *a, file=sys.stderr, flush=True)


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

        if os.environ.get("BR_NO_UNITS") == "1":
            raise RuntimeError("unit filtering disabled")
        udb = UnitDB(UNITS)
    except Exception as e:  # noqa: BLE001
        log(f"no unit map: {e!r}")
        udb = None
    STATE["udb"] = udb
    # map test ids -> numbers in this tests.json (ids are stable, numbers might not be)
    id2num = {t["id"]: t["number"] for t in tests}
    mapnum2num = [id2num.get(i) for i in db.ids]
    id2mapnum = {i: k for k, i in enumerate(db.ids)}
    ID2MAP.update(id2mapnum)
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
        for n in cands:
            if tid[n] in PROXY_IDS:
                estc[n] = 0.15 * CPU_SCALE
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

    def spec_of(nums, sel=None, E=None):
        """Run order: tests whose file matches a patched module first (most likely to fail, so
        an early failure can stop the run), then cheapest first; one class kept together."""
        toks = set()
        for f in (sel.files if sel is not None else ()):
            b = os.path.basename(f).rsplit(".", 1)[0]
            if b not in ("__init__", "dialect", "parser", "generator", "core"):
                toks.add(b.lower())
        groups = {}
        for n in nums:
            parts = tid[n].split("::")
            key = (parts[0], parts[1])
            groups.setdefault(key, []).append(n)

        def gkey(item):
            (path, cls), ns = item
            aff = any(t in path.lower() for t in toks)
            cost = sum((E(n) if E else 0.0) for n in ns)
            return (0 if aff else 1, cost)

        out = []
        for (path, cls), ns in sorted(groups.items(), key=gkey):
            for n in sorted(ns, key=lambda n: (E(n) if E else 0.0, n)):
                out.append((n, path[:-3].replace("/", "."), cls, tid[n].split("::")[-1]))
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
    tpc_exec = {}
    try:
        tpc_orig = json.load(open(TPCFP))
        import tpcfp as _tf

        tpc_exec = {rel: set(_tf.exec_rows(rel, REPO)) for rel in (_tf.TPCH, _tf.TPCDS)}
        if os.environ.get("BR_NO_TPC") == "1":
            tpc_orig = None
    except OSError:
        tpc_orig = None

    def tpc_check(cid, sel, expensive, fail, purge, E):
        """Executor TPC tests: only queries whose plan input / duckdb SQL changed can differ."""
        import tpcfp

        udb = STATE.get("udb")
        if tpc_orig is None:
            return
        for n in list(expensive):
            rel = EXEC_TPC.get(tid[n])
            if rel is None:
                continue
            ut = udb.tests.get(tid[n]) if udb is not None else None
            if not tpcfp.safe_for(sel.files, sel.req, ut.get("execlines") if ut else None,
                                   sel.primary_classes, 1 << ID2MAP.get(tid[n], 0)):
                continue
            orig = {int(k): v for k, v in tpc_orig.get(rel, {}).items()}
            flt = sel.filters.get(n)
            idxs = set(flt.get(rel, ())) if flt else set(orig)
            idxs &= set(orig)
            base = pack_cost(fail, dur_ms)
            marginal = (pack_cost(fail | {n}, dur_ms) - base) / 1000
            n_exec = sum(1 for i in idxs if i in tpc_exec.get(rel, ()))
            est_fp = (0.06 * n_exec + 0.006 * (len(idxs) - n_exec)) * CPU_SCALE + 0.3
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
        tpc_sql = {"tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"}
        reload_tpc = runner.PRESETUP_DONE and any(f in tpc_sql for f in sel.files)
        purge = any(f in loaded for f in sel.files) or any(
            f.startswith(runner.PRESETUP_PREFIXES) and f not in tpc_sql for f in sel.files
            if runner.PRESETUP_DONE)
        now = time.monotonic()
        if now - T0 > WALL_BUDGET - 3:
            log(f"{cid}: out of time, blind")
            emit(cid, cheap | expensive)
            return
        est_cheap = sum(E(n) for n in cheap)
        blind_all = pack_cost(cheap | expensive, dur_ms) / 1000
        skip_cheap = False
        if est_cheap > min(blind_all + 1.0, RUN_CAP):
            log(f"{cid}: running cheap tests ({est_cheap:.1f}s) costs more than including "
                f"everything ({blind_all:.1f}s); cheap ones blind")
            skip_cheap = True
        deadline = min(T0 + WALL_BUDGET, now + max(15.0, 3 * est_cheap + 10))
        files = dict(sel.files)
        files.update(sel.instr)
        proxies = {n: runner.PROXIES[tid[n]] for n in cheap | expensive if tid[n] in PROXY_IDS}
        mirror.apply(files)
        c0 = os.times()
        try:
            failing = set()
            wm_ex = wm_inf = 0

            import tpcfp

            tpc_maybe = set()
            if tpc_orig is not None:
                udb_ = STATE.get("udb")
                for n in expensive:
                    if tid[n] in EXEC_TPC:
                        ut = udb_.tests.get(tid[n]) if udb_ is not None else None
                        if tpcfp.safe_for(sel.files, sel.req, ut.get("execlines") if ut else None,
                                   sel.primary_classes, 1 << ID2MAP.get(tid[n], 0)):
                            tpc_maybe.add(n)

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
            if skip_cheap:
                # too many to run: still verify the few that are cheap to run but costly to include
                verify = {n for n in cheap if dur_ms[n] >= 1000 and E(n) < 0.5 * dur_ms[n] / 1000}
                unrun = set(cheap) - verify
                if verify:
                    res = runner.run_tests(spec_of(verify, sel, E), purge=purge,
                                           timeouts={n: max(1.5, E(n) * 2 + 1) for n in verify},
                                           deadline=deadline, filters=sel.filters, proxies=proxies,
                                           reload_tpc=reload_tpc)
                    failing |= {n for n, f in res.items() if f} | {n for n in verify if n not in res}
            elif cheap:
                res = runner.run_tests(spec_of(cheap, sel, E), purge=purge, stop_cb=stop_cb,
                                       timeouts={n: max(1.5, E(n) * 2 + 1) for n in cheap},
                                       deadline=deadline, filters=sel.filters, proxies=proxies,
                                       reload_tpc=reload_tpc)
                failing |= {n for n, f in res.items() if f}
                unrun = cheap - set(res)
                for n in cheap:
                    ex, inf = runner.WM_LOG.get(n, (0, 0))
                    wm_ex += ex
                    wm_inf += inf
            clean = (sel.monitorable and sel.sites > 0 and wm_ex >= CLEAN_MIN_EXEC and wm_inf == 0
                     and not failing and not unrun)
            blind = set(unrun)
            run_b = set()
            if clean:
                # clean evidence only ever skips the very expensive tests; the rest still run
                keep = {n for n in expensive if E(n) < CLEAN_SKIP_MIN}
                if keep:
                    res = runner.run_tests(spec_of(keep, sel, E), purge=purge,
                                           timeouts={n: max(1.5, E(n) * 2 + 1) for n in keep},
                                           deadline=deadline, filters=sel.filters, proxies=proxies,
                                       reload_tpc=reload_tpc)
                    failing |= {n for n, f in res.items() if f} | {n for n in keep if n not in res}
                    run_b |= keep
            if expensive and not clean:
                tpc_check(cid, sel, expensive, failing | unrun, purge, E)
            if expensive and not clean:
                b, run_b = plan_expensive(failing | unrun)
                blind |= b
                if run_b:
                    res = runner.run_tests(spec_of(run_b, sel, E), purge=purge,
                                           timeouts={n: max(1.5, E(n) * 2 + 1) for n in run_b},
                                           deadline=deadline, filters=sel.filters, proxies=proxies,
                                       reload_tpc=reload_tpc)
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
            mods = stages[cur_level]["modules"]
            if cur_level == len(stages) - 1:
                # last stage = test modules: only those the remaining commits will run
                need = {modname(n) for lv, _, _, ch, ex in pending if lv >= cur_level + 1
                        for n in ch | ex}
                mods = [m for m in mods if m in need]
            runner.preload(mods)
            cur_level += 1
            if "tests.test_executor" in mods and any(
                    lv >= cur_level and any(modname(n) == "tests.test_executor" for n in ch)
                    for lv, _, _, ch, _ in pending):
                runner.presetup_executor()
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
