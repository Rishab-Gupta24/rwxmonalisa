"""Blast Radius selector: one JSON line per commit on stdout, logs on stderr."""

from __future__ import annotations

import gzip
import json
import os
import pickle
import sys
import time
import traceback

from .pack import pack
from .select import Selector

DATA = os.environ.get("BLAST_DATA", "/data")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("BLAST_REPO", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", os.path.join(APP, "data", "map.pkl.gz"))
INDEX = os.environ.get("BLAST_INDEX", os.path.join(APP, "data", "index.pkl"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    t0 = time.process_time()
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
        sel = Selector(data, index, REPO)
    except Exception:
        log("map load failed:\n" + traceback.format_exc())

    out = []
    every = [(t["number"], ms[t["number"]]) for t in tests]
    for cid in commits:
        try:
            if sel is None:
                raise RuntimeError("no selector")
            with open(os.path.join(commits_dir, cid + ".patch"), encoding="utf-8", errors="replace") as f:
                patch = f.read()
            mask, notes = sel.select(patch)
            chosen = [(i, ms[i]) for i in range(len(tests)) if mask >> i & 1]
            cost, workers = pack(chosen)
            log(f"{cid}: {len(chosen)} tests, est {cost/1000:.2f}s {notes}")
        except Exception:
            log(f"{cid}: failed, running everything\n" + traceback.format_exc())
            cost, workers = pack(every)
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
