"""For labeled sets: when a heavy test is a candidate, how often does it fail,
and does some light candidate fail with it?

usage: python heavy_stats.py <map> <index> <repo> <dir> [<dir> ...]
"""

import gzip
import json
import os
import pickle
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast.select import Selector  # noqa: E402

HEAVY = float(os.environ.get("HEAVY", 3.0))


def main():
    map_path, index_path, repo = sys.argv[1:4]
    sel = Selector(pickle.load(gzip.open(map_path)), pickle.load(open(index_path, "rb")), repo)
    stats = defaultdict(lambda: [0, 0, 0])  # test -> [in cand, failed, failed with no light failing]
    for d in sys.argv[4:]:
        tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
        secs = [t["seconds"] for t in tests]
        answers = json.load(open(os.path.join(d, "answers.json")))["commits"]
        for cid, a in sorted(answers.items()):
            text = open(os.path.join(d, "commits", cid + ".patch")).read()
            mask, notes = sel.select(text)
            cand = [i for i in range(len(tests)) if mask >> i & 1]
            fail = set(a["failing"])
            light_fail = [i for i in fail if secs[i] < HEAVY]
            for i in cand:
                if secs[i] >= HEAVY:
                    s = stats[tests[i]["id"]]
                    s[0] += 1
                    if i in fail:
                        s[1] += 1
                        if not light_fail:
                            s[2] += 1
                            print(f"  ALONE {cid} {tests[i]['id']} {notes}")
    for tid, (n, f, alone) in sorted(stats.items(), key=lambda x: -x[1][0]):
        print(f"{tid}: candidate {n}, failed {f}, failed with no light failure {alone}")


if __name__ == "__main__":
    main()
