"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr."""

from __future__ import annotations

import gzip
import json
import os
import pickle
import sys
import time
import traceback

from . import hot, policy, runner
from .analyze import apply_patch, parse_patch
from .pack import pack
from .select import Selector

DATA = os.environ.get("BLAST_DATA", "/data")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("BLAST_REPO", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", os.path.join(APP, "data", "map.pkl.gz"))
INDEX = os.environ.get("BLAST_INDEX", os.path.join(APP, "data", "index.pkl"))
IMPORTS = os.environ.get("BLAST_IMPORTS", os.path.join(APP, "data", "imports.pkl"))
CPU = os.environ.get("BLAST_CPU", os.path.join(APP, "data", "cpu.json"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


VERIFY = os.environ.get("BLAST_VERIFY", "1") == "1"
WS = "/tmp/blast_ws"
SECONDS_PER_COMMIT = 6.0  # the run gets 3 minutes for 30 commits


def verify_all(plans, patches, answers, ids, secs, n_commits, t_wall):
    """Run the planned local checks, best expected saving first, inside the
    wall-clock budget; a check that cannot finish leaves the static answer."""
    budget = 0.55 * SECONDS_PER_COMMIT * n_commits
    hot_plans = {c: p for c, p in plans.items() if p["mode"] == "hot"}
    fresh_plans = {c: p for c, p in plans.items() if p["mode"] == "fresh"}
    t_prep = time.process_time()
    try:
        if hot_plans:
            files = {ids[i].split("::")[0] for p in hot_plans.values() for i in p["verify"]}
            files |= {rel for p in hot_plans.values() for rel in p["hot_files"]}
            hot.prepare(REPO, files, log)
        if fresh_plans:
            runner.preload(heavy=any(
                ids[i].split("::")[0] in policy.HEAVY_IMPORT_FILES for p in fresh_plans.values() for i in p["verify"]))
    except Exception:
        log("preload failed:\n" + traceback.format_exc())
        return
    log(f"prepare cpu {time.process_time() - t_prep:.2f}s")
    for cid in sorted(plans, key=lambda c: -plans[c]["gain"]):
        left = budget - (time.time() - t_wall)
        if left < 5:
            log("verification budget used up")
            break
        p = plans[cid]
        run_ids = [ids[i] for i in p["verify"]]
        try:
            if p["mode"] == "hot":
                res = hot.run(p["hot_files"], run_ids, timeout=10, budget=min(left, 60), stop_on_fail=p["stop"])
                if res is not None and not res.get("eligible", True):
                    log(f"{cid}: not hot-patchable ({res.get('error')})")
                    continue
            else:
                changed = {}
                for fp in parse_patch(patches[cid]):
                    if fp.old_path and fp.new_path != fp.old_path:
                        changed[fp.old_path] = None
                    if fp.new_path is None:
                        continue
                    old = ""
                    if fp.old_path:
                        with open(os.path.join(REPO, fp.old_path), encoding="utf-8") as f:
                            old = f.read()
                    changed[fp.new_path] = apply_patch(old, fp)[0]
                runner.make_tree(REPO, WS, changed)
                res = runner.run(WS, run_ids, timeout=10, budget=min(left, 60))
        except Exception:
            log(f"{cid}: check failed\n" + traceback.format_exc())
            continue
        if res is None:
            log(f"{cid}: check did not finish")
            continue
        failed = set(res["failed"])
        broken = res["broken"]
        ran = set(res["ran"]) | failed
        if broken:
            log(f"{cid}: collection broke {broken}, keeping static answer")
            continue
        if p["mode"] == "hot" and not p["stop"]:
            if not set(run_ids) <= ran:
                log(f"{cid}: check skipped {len(set(run_ids) - ran)} tests, keeping static answer")
                continue
            if failed:
                answers[cid] = [i for i in p["verify"] if ids[i] in failed] + list(p["heavy"])
            else:
                answers[cid] = list(p["keep_if_clean"])
        else:
            if failed:
                passed = set(res["ran"]) - failed
                answers[cid] = [i for i in answers[cid] if ids[i] not in passed]
            else:
                if not set(run_ids) <= ran:
                    log(f"{cid}: check skipped {len(set(run_ids) - ran)} tests, keeping static answer")
                    continue
                answers[cid] = list(p["keep_if_clean"])
        log(f"{cid}: {p['mode']} check, {len(failed)} failed, printing {len(answers[cid])} "
            f"(apply {res.get('cpu_apply')} pytest {res.get('cpu_pytest')} est {p['cpu']:.2f})")


def main():
    t0 = time.process_time()
    t_wall = time.time()
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    ms = [round(t["seconds"] * 1000) for t in tests]
    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(fn[:-6] for fn in os.listdir(commits_dir) if fn.endswith(".patch"))

    sel = None
    try:
        with gzip.open(MAP, "rb") as f:
            data = pickle.load(f)
        with open(INDEX, "rb") as f:
            index = pickle.load(f)
        if data["ids"] != [t["id"] for t in tests]:
            log("tests.json differs from the map's test list; remapping by id")
            data = remap(data, tests)
        import_lines = None
        try:
            with open(IMPORTS, "rb") as f:
                import_lines = pickle.load(f)
        except Exception:
            log("no import-time line trace: hot checks disabled")
        sel = Selector(data, index, REPO, import_lines)
    except Exception:
        log("map load failed:\n" + traceback.format_exc())

    every = [(t["number"], ms[t["number"]]) for t in tests]
    secs = [t["seconds"] for t in tests]
    ids = [t["id"] for t in tests]
    answers = {}
    plans = {}
    patches = {}
    try:
        with open(CPU) as f:
            cpu_by_id = json.load(f)
        cpu = [cpu_by_id.get(t["id"], t["seconds"]) for t in tests]
    except Exception:
        cpu = [t["seconds"] * 1.5 for t in tests]
    for cid in commits:
        try:
            if sel is None:
                raise RuntimeError("no selector")
            with open(os.path.join(commits_dir, cid + ".patch"), encoding="utf-8", errors="replace") as f:
                patch = f.read()
            patches[cid] = patch
            mask, notes = sel.select(patch)
            cand = [i for i in range(len(tests)) if mask >> i & 1]
            answers[cid] = cand
            if VERIFY:
                p = policy.plan(cand, secs, ids, notes, cpu, sel.hot_ok and bool(sel.hot), sel.regions)
                if p:
                    p["hot_files"] = dict(sel.hot)
                    plans[cid] = p
            log(f"{cid}: {len(cand)} tests {notes}{' verify ' + str(len(plans[cid]['verify'])) if cid in plans else ''}")
        except Exception:
            log(f"{cid}: failed, running everything\n" + traceback.format_exc())
            answers[cid] = None

    if plans:
        verify_all(plans, patches, answers, ids, secs, len(commits), t_wall)

    out = []
    for cid in commits:
        chosen = answers.get(cid)
        if chosen is None:
            cost, workers = pack(every)
        else:
            cost, workers = pack([(i, ms[i]) for i in chosen])
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"cpu {time.process_time() - t0:.2f}s")


def remap(data, tests):
    """Map built for a different numbering: translate masks through test ids."""
    pos = {tid: i for i, tid in enumerate(data["ids"])}
    new_num = [pos.get(t["id"]) for t in tests]
    unknown = [t["number"] for t, p in zip(tests, new_num) if p is None]

    def tr(mask):
        m = 0
        for new, old in enumerate(new_num):
            if old is not None and mask >> old & 1:
                m |= 1 << new
        for u in unknown:  # tests the map never saw: always run them
            m |= 1 << u
        return m

    data = dict(data)
    data["masks"] = [tr(m) for m in data["masks"]]
    data["files"] = {k: tr(v) for k, v in data["files"].items()}
    data["types"] = {k: tr(v) for k, v in data["types"].items()}
    data["ids"] = [t["id"] for t in tests]
    return data


if __name__ == "__main__":
    main()
