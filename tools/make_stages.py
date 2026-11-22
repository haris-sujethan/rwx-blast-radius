"""Record which repo files each zygote preload stage loads (run in a fresh process)."""
import glob, importlib, json, os, sys, builtins, io
repo = os.path.abspath(sys.argv[1]); out = sys.argv[2]
os.chdir(repo); sys.path.insert(0, repo); os.environ["SKIP_INTEGRATION"] = "0"
opened = set()
ro = builtins.open
def my_open(f, *a, **k):
    try:
        p = os.path.abspath(os.fspath(f))
        if p.startswith(repo + os.sep): opened.add(os.path.relpath(p, repo))
    except TypeError:
        pass
    return ro(f, *a, **k)
builtins.open = my_open; io.open = my_open
import concurrent.futures  # noqa
import numpy, pandas, duckdb, pandas.testing  # noqa
test_mods = sorted(f[:-3].replace("/", ".") for f in glob.glob("tests/**/test_*.py", recursive=True))
test_mods = [m for m in test_mods if m not in ("tests.test_docs", "tests.test_integration_loader")]
stages = [["sqlglot"], ["tests.helpers", "tests.dialects.test_dialect"], test_mods]
res = []
for mods in stages:
    for m in mods:
        try:
            importlib.import_module(m)
        except BaseException as e:
            print("fail", m, e, file=sys.stderr)
    files = set(opened)
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and os.path.abspath(f).startswith(repo + os.sep):
            files.add(os.path.relpath(os.path.abspath(f), repo))
    res.append({"modules": mods, "files": sorted(files)})
    print(len(mods), len(files), file=sys.stderr)
json.dump(res, open(out, "w"))
