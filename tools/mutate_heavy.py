"""Mutations on lines the heavy tests execute (offline calibration only).

usage: python mutate_heavy.py <repo> <map> <tests.json> <out_dir> <count> [seed]
"""

import gzip
import json
import os
import pickle
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutate import FLIPS, diff  # noqa: E402


def main():
    repo, map_path, tests_json, out, count = sys.argv[1:6]
    count = int(count)
    seed = int(sys.argv[6]) if len(sys.argv) > 6 else 7
    rng = random.Random(seed)
    data = pickle.load(gzip.open(map_path))
    tests = json.load(open(tests_json))["tests"]
    heavy = 0
    for t in tests:
        if t["seconds"] >= 3.0:
            heavy |= 1 << t["number"]
    lines = []
    for path, by_line in data["lines"].items():
        if not path.startswith("sqlglot/"):
            continue
        for ln, mi in by_line.items():
            if data["masks"][mi] & heavy:
                lines.append((path, ln))
    lines.sort()
    os.makedirs(out, exist_ok=True)
    meta = {}
    made = 0
    tries = 0
    while made < count and tries < count * 200:
        tries += 1
        path, ln = rng.choice(lines)
        src = open(os.path.join(repo, path), encoding="utf-8").read()
        src_lines = src.split("\n")
        line = src_lines[ln - 1]
        s = line.strip()
        if not s or s.startswith(("#", '"', "'", "def ", "class ", "@")):
            continue
        flips = list(FLIPS)
        rng.shuffle(flips)
        for pat, rep in flips:
            m = list(re.finditer(pat, line))
            if not m:
                continue
            mm = rng.choice(m)
            if rep is None:
                n = int(mm.group(1))
                rep = str(n + rng.choice([1, -1]) if n > 0 else 1)
            new_line = line[: mm.start()] + rep + line[mm.end():]
            if new_line == line:
                continue
            new = "\n".join(src_lines[: ln - 1] + [new_line] + src_lines[ln:])
            try:
                compile(new, path, "exec")
            except SyntaxError:
                continue
            cid = f"h{seed:02d}{made:03d}"
            open(os.path.join(out, cid + ".patch"), "w", encoding="utf-8").write(diff(path, src, new))
            meta[cid] = "heavy"
            made += 1
            break
    json.dump(meta, open(os.path.join(out, "kinds.json"), "w"), indent=1)
    print("made", made)


if __name__ == "__main__":
    main()
