"""Static selection quality on the sample set (no local test runs).

usage: python -m tools.eval_sample <map.pkl.gz> <index.pkl> <repo> <sample_dir>
"""

import gzip
import json
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast.pack import pack  # noqa: E402
from blast.select import Selector  # noqa: E402


def main():
    map_path, index_path, repo, sample = sys.argv[1:5]
    t0 = time.process_time()
    data = pickle.load(gzip.open(map_path))
    index = pickle.load(open(index_path, "rb"))
    t1 = time.process_time()
    sel = Selector(data, index, repo)
    tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
    ms = [round(t["seconds"] * 1000) for t in tests]
    answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]
    total = 0
    oracle = 0
    misses = 0
    t2 = time.process_time()
    for cid in sorted(answers):
        patch = open(os.path.join(sample, "commits", cid + ".patch")).read()
        mask, notes = sel.select(patch)
        chosen = [i for i in range(sel.n) if mask >> i & 1]
        failing = answers[cid]["failing"]
        miss = [f for f in failing if not (mask >> f & 1)]
        cost, _ = pack([(i, ms[i]) for i in chosen])
        ocost, _ = pack([(i, ms[i]) for i in failing])
        total += cost + (300000 if miss else 0)
        oracle += ocost
        misses += bool(miss)
        print(
            f"{cid} {answers[cid]['kind']:8s} fail={len(failing):3d} cand={len(chosen):4d} "
            f"sum={sum(ms[i] for i in chosen)/1000:7.2f} cost={cost/1000:6.2f} oracle={ocost/1000:6.2f} "
            f"{'MISS ' + str(miss) if miss else ''} {notes}"
        )
    t3 = time.process_time()
    print(f"total={total/1000:.1f} oracle={oracle/1000:.1f} misses={misses} load={t1-t0:.2f}s select={t3-t2:.2f}s")


if __name__ == "__main__":
    main()
