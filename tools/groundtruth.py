"""Label synthetic patches by running sqlglot's tests on them (offline only).

usage: python groundtruth.py <repo> <tests.json> <map> <index> <patch_dir> <workers>

For each patch: every test except the heaviest ones, plus any heavy test the
selector picks (a heavy test outside the selection is assumed unaffected).
Writes <patch_dir>/answers.json in the sample's format.
"""

import gzip
import json
import os
import pickle
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from blast.analyze import apply_patch, parse_patch  # noqa: E402
from blast.select import Selector  # noqa: E402

PLUGIN = '''
import json, os
import concurrent.futures as _cf
_PPE = _cf.ProcessPoolExecutor
class _SmallPool(_PPE):
    def __init__(self, max_workers=None, *a, **k):
        super().__init__(2 if max_workers is None else min(max_workers, 2), *a, **k)
_cf.ProcessPoolExecutor = _SmallPool
FAILED = set()
BROKEN = set()
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        BROKEN.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["GT_OUT"], "w") as f:
        json.dump({"failed": sorted(FAILED), "broken": sorted(BROKEN)}, f)
'''

HEAVY_SECONDS = 3.0


def main():
    repo, tests_json, map_path, index_path, pdir, workers = sys.argv[1:7]
    workers = int(workers)
    tests = json.load(open(tests_json))["tests"]
    ids = [t["id"] for t in tests]
    heavy = {t["number"] for t in tests if t["seconds"] >= HEAVY_SECONDS}
    sel = Selector(pickle.load(gzip.open(map_path)), pickle.load(open(index_path, "rb")), repo)
    patches = sorted(f[:-6] for f in os.listdir(pdir) if f.endswith(".patch"))
    out_path = os.path.join(pdir, "answers.json")
    answers = json.load(open(out_path))["commits"] if os.path.exists(out_path) else {}
    kinds = json.load(open(os.path.join(pdir, "kinds.json"))) if os.path.exists(os.path.join(pdir, "kinds.json")) else {}
    todo = [p for p in patches if p not in answers]
    lock = threading.Lock()
    base = os.path.join(os.path.dirname(pdir.rstrip("/")), "gt_work")
    os.makedirs(base, exist_ok=True)
    plugin_dir = os.path.join(base, "plugin")
    os.makedirs(plugin_dir, exist_ok=True)
    open(os.path.join(plugin_dir, "gt_plugin.py"), "w").write(PLUGIN)

    slots = list(range(workers))
    for w in slots:
        wd = os.path.join(base, f"w{w}")
        if not os.path.exists(wd):
            shutil.copytree(repo, wd, ignore=shutil.ignore_patterns(
                "__pycache__", ".git", "sqlglot-integration-tests", "benchmarks", "posts", "docs"))
    free = list(slots)

    def run_one(cid):
        with lock:
            w = free.pop()
        wd = os.path.join(base, f"w{w}")
        try:
            text = open(os.path.join(pdir, cid + ".patch"), encoding="utf-8").read()
            mask, _ = sel.select(text)
            if os.environ.get("GT_CAND_ONLY") == "1":
                run = [i for i in range(len(ids)) if mask >> i & 1]
            else:
                run = [i for i in range(len(ids)) if i not in heavy or mask >> i & 1]
            if not run:
                with lock:
                    answers[cid] = {"failing": [], "kind": kinds.get(cid, "?")}
                return
            originals = {}
            for fp in parse_patch(text):
                rel = fp.old_path or fp.new_path
                full = os.path.join(wd, rel)
                old = open(full, encoding="utf-8").read() if fp.old_path and os.path.exists(full) else None
                originals[(fp.new_path or rel)] = old
                if fp.new_path is None:
                    os.remove(full)
                    continue
                new, *_ = apply_patch(old or "", fp)
                os.makedirs(os.path.dirname(os.path.join(wd, fp.new_path)), exist_ok=True)
                open(os.path.join(wd, fp.new_path), "w", encoding="utf-8").write(new)
            argfile = os.path.join(base, f"args{w}.txt")
            open(argfile, "w").write("\n".join(ids[i] for i in run))
            out = os.path.join(base, f"out{w}.json")
            env = dict(os.environ, SKIP_INTEGRATION="0", GT_OUT=out, PYTHONPATH=plugin_dir,
                       PYTHONDONTWRITEBYTECODE="1",
                       PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
            subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "gt_plugin",
                 "--timeout", "120", "-x" if False else "-q", f"@{argfile}"],
                cwd=wd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800,
            )
            res = json.load(open(out))
            failed = set()
            for nid in res["failed"]:
                if nid in ids:
                    failed.add(ids.index(nid))
            for nid in res["broken"]:
                for i, tid in enumerate(ids):
                    if tid.startswith(nid.split("::")[0] + "::") and i in run:
                        failed.add(i)
            # restore
            for rel, old in originals.items():
                full = os.path.join(wd, rel)
                if old is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    open(full, "w", encoding="utf-8").write(old)
            for fp in parse_patch(text):
                if fp.new_path is None and fp.old_path:
                    shutil.copy(os.path.join(repo, fp.old_path), os.path.join(wd, fp.old_path))
            with lock:
                answers[cid] = {"failing": sorted(failed), "kind": kinds.get(cid, "?")}
                json.dump({"commits": answers}, open(out_path, "w"), indent=1)
                print(cid, len(failed), flush=True)
        finally:
            with lock:
                free.append(w)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(run_one, todo))


if __name__ == "__main__":
    main()
