"""Simulate the check policy on labeled patch sets (offline).

usage: python sim_policy.py <map> <index> <imports> <cpu.json> <repo> <dir> [<dir> ...]
Each dir holds tests.json, answers.json and commits/. Check CPU is modeled with
the same estimates the policy uses (policy.CPU_SCALE times them, to test
sensitivity).
"""

import gzip
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast import policy  # noqa: E402
from blast.pack import pack  # noqa: E402
from blast.select import Selector  # noqa: E402

TRUE_SCALE = float(os.environ.get("TRUE_SCALE", 1.0))  # real CPU / estimated CPU


def main():
    map_path, index_path, imports_path, cpu_path, repo = sys.argv[1:6]
    dirs = sys.argv[6:]
    sel = Selector(pickle.load(gzip.open(map_path)), pickle.load(open(index_path, "rb")), repo,
                   pickle.load(open(imports_path, "rb")))
    cpu_by_id = json.load(open(cpu_path))
    verbose = os.environ.get("V") == "1"
    G = {"static": 0, "policy": 0, "oracle": 0, "miss": 0, "n": 0, "cpu": 0}
    for d in dirs:
        tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
        secs = [t["seconds"] for t in tests]
        ms = [round(s * 1000) for s in secs]
        ids = [t["id"] for t in tests]
        cpu = [cpu_by_id.get(t["id"], t["seconds"]) for t in tests]
        answers = json.load(open(os.path.join(d, "answers.json")))["commits"]
        T = {"static": 0, "policy": 0, "oracle": 0, "miss": 0, "cpu": 0}
        for cid in sorted(answers):
            text = open(os.path.join(d, "commits", cid + ".patch")).read()
            mask, notes = sel.select(text)
            cand = [i for i in range(len(tests)) if mask >> i & 1]
            fail = set(answers[cid]["failing"])
            static = pack([(i, ms[i]) for i in cand])[0] / 1000 + (300 if fail - set(cand) else 0)
            p = policy.plan(cand, secs, ids, notes, cpu, sel.hot_ok and bool(sel.hot), sel.regions)
            spent = 0.0
            out = cand
            if p:
                base = policy.HOT_CPU if p["mode"] == "hot" else policy.FRESH_CPU + (
                    policy.HEAVY_IMPORT_CPU if any(ids[i].split("::")[0] in policy.HEAVY_IMPORT_FILES for i in p["verify"]) else 0)
                spent = base
                seen_classes = set()
                passed = []
                hit = []
                for i in p["verify"]:
                    c = ids[i].rsplit("::", 1)[0]
                    if c not in seen_classes:
                        seen_classes.add(c)
                        spent += policy.CLASS_SETUP.get(c, 0.0)
                    spent += cpu[i]
                    if i in fail:
                        hit.append(i)
                        if p["stop"]:
                            break
                    else:
                        passed.append(i)
                spent *= TRUE_SCALE
                if not p["stop"]:
                    out = hit + (list(p["heavy"]) if hit else list(p["keep_if_clean"]))
                elif hit:
                    out = [i for i in cand if i not in set(passed)]
                else:
                    out = list(p["keep_if_clean"])
            cost = pack([(i, ms[i]) for i in out])[0] / 1000 + spent
            miss = bool(fail - set(out))
            cost += 300 if miss else 0
            oracle = pack([(i, ms[i]) for i in fail])[0] / 1000
            T["static"] += static
            T["policy"] += cost
            T["oracle"] += oracle
            T["miss"] += miss
            T["cpu"] += spent
            if verbose or miss:
                print(f"{cid} {answers[cid]['kind']:8s} fail={len(fail):3d} cand={len(cand):4d} static={static:6.2f} "
                      f"policy={cost:6.2f} oracle={oracle:6.2f} "
                      f"{(p['mode'] + ('-stop' if p['stop'] else '') + ' cpu=%.2f' % spent) if p else ''} {'MISS' if miss else ''}")
        print(f"== {os.path.basename(d.rstrip('/'))}: n={len(answers)} static={T['static']:.1f} policy={T['policy']:.1f} "
              f"(check cpu {T['cpu']:.1f}) oracle={T['oracle']:.1f} misses={T['miss']}")
        for k in T:
            G[k] += T[k]
        G["n"] += len(answers)
    print(f"== all: n={G['n']} static={G['static']:.1f} policy={G['policy']:.1f} (check cpu {G['cpu']:.1f}) "
          f"oracle={G['oracle']:.1f} misses={G['miss']}")


if __name__ == "__main__":
    main()
