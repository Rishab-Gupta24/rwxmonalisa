"""Time the local runner on sample patches: python tools/try_runner.py <repo> <sample> <map> <index> [cid...]"""

import gzip
import json
import os
import pickle
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast import runner  # noqa: E402
from blast.analyze import apply_patch, parse_patch  # noqa: E402
from blast.select import Selector  # noqa: E402


def cpu():
    a = resource.getrusage(resource.RUSAGE_SELF)
    b = resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime


def main():
    repo, sample, map_path, index_path = sys.argv[1:5]
    cids = sys.argv[5:]
    tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
    answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]
    sel = Selector(pickle.load(gzip.open(map_path)), pickle.load(open(index_path, "rb")), repo)
    for cid in cids or sorted(answers):
        text = open(os.path.join(sample, "commits", cid + ".patch")).read()
        mask, notes = sel.select(text)
        cand = [i for i in range(len(tests)) if mask >> i & 1]
        light = [i for i in cand if tests[i]["seconds"] < 1.0]
        if not light or sum(tests[i]["seconds"] for i in light) > 3:
            continue
        changed = {}
        for fp in parse_patch(text):
            if fp.new_path is None:
                changed[fp.old_path] = None
                continue
            old = open(os.path.join(repo, fp.old_path)).read() if fp.old_path else ""
            changed[fp.new_path] = apply_patch(old, fp)[0]
        runner.preload(heavy=any("test_optimizer" in tests[i]["id"] or "test_executor" in tests[i]["id"] for i in light))
        c0, w0 = cpu(), time.time()
        runner.make_tree(repo, "/tmp/blast_ws", changed)
        ids = [tests[i]["id"] for i in light]
        res = runner.run("/tmp/blast_ws", ids, 30, 60, stop_on_fail=False)
        c1, w1 = cpu(), time.time()
        exp = {tests[i]["id"] for i in answers[cid]["failing"]} & set(ids)
        got = set(res["failed"]) if res is not None else None
        ok = "OK " if got == exp else "DIFF"
        print(f"{cid} {ok} light={len(light)} sum={sum(tests[i]['seconds'] for i in light):.2f} "
              f"cpu={c1-c0:.2f} wall={w1-w0:.2f} expected={sorted(exp)} got={sorted(got) if got is not None else None}")


if __name__ == "__main__":
    main()
