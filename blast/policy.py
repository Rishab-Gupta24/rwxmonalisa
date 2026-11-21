"""When to check a selection locally, and what to output after the check.

A check runs the cheap ("light") candidate tests on the patched code:

* hot mode (edits only inside function bodies): every light test runs; the
  output is the light tests that failed plus the heavy candidates kept.
* fresh mode (anything else): tests run in a fresh import of the patched tree,
  stopping at the first failure; a failure prints the static selection minus
  the tests seen passing, a clean run prints only the heavy candidates kept.

Checks cost CPU seconds (charged to the score), so one is made only when its
expected saving over printing the static selection is positive.
"""

from __future__ import annotations

import os

from .pack import pack


def _f(name, default):
    return float(os.environ.get(f"BLAST_{name}", default))


HEAVY = _f("HEAVY", 3.0)  # seconds: never run locally
HOT_CPU = _f("HOT_CPU", 0.45)  # fork + compile + pytest start in hot mode
FRESH_CPU = _f("FRESH_CPU", 0.8)  # fork + pytest + fresh sqlglot import
HEAVY_IMPORT_CPU = _f("HEAVY_IMPORT_CPU", 3.0)  # numpy / pandas / duckdb in fresh mode
P_CLEAN = _f("P_CLEAN", 0.5)  # chance a code change breaks no light test
MAX_CHECK_CPU = _f("MAX_CHECK_CPU", 12.0)  # estimated CPU one check may use
MARGIN = _f("MARGIN", 0.3)
DROP_HEAVY = os.environ.get("BLAST_DROP_HEAVY", "1") == "1"
MIN_SUPPORT = int(_f("MIN_SUPPORT", 3))  # light tests that must share every changed region with a dropped heavy test

HEAVY_IMPORT_FILES = {"tests/test_optimizer.py", "tests/test_executor.py", "tests/test_lineage.py"}
# setUpClass work a check pays again for each class it touches (seconds)
CLASS_SETUP = {
    "tests/test_optimizer.py::TestOptimizer": _f("SETUP_OPTIMIZER", 3.0),
    "tests/test_executor.py::TestExecutor": _f("SETUP_EXECUTOR", 5.0),
    "tests/test_lineage.py::TestLineage": 0.3,
}


def _setup_cpu(tests, ids):
    classes = {ids[i].rsplit("::", 1)[0] for i in tests}
    return sum(CLASS_SETUP.get(c, 0.0) for c in classes)


def _cost(tests, secs):
    return pack([(i, round(secs[i] * 1000)) for i in tests])[0] / 1000


def _order(light, secs, ids, notes):
    """Tests named after a changed module first (likeliest to fail), then cheap."""
    words = set()
    for n in notes:
        path = n.split(":")[0].split(" ")[-1]
        base = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if base and base != "__init__":
            words.add(base.lower())

    def key(i):
        return (0 if any(w in ids[i].lower() for w in words) else 1, secs[i])

    return sorted(light, key=key)


def _keep_heavy(heavy, light, regions):
    """Heavy tests that stay even when every light test passes: those that reach
    some changed region few light tests reach (nothing vouches for them)."""
    light_mask = 0
    for i in light:
        light_mask |= 1 << i
    keep = []
    for h in heavy:
        bit = 1 << h
        for r in regions:
            if r & bit and bin(r & light_mask).count("1") < MIN_SUPPORT:
                keep.append(h)
                break
    return keep


def plan(cand, secs, ids, notes, cpu, hot_ok, regions=()):
    """cpu: estimated local CPU seconds per test number; regions: candidate
    mask of each changed region."""
    if not cand:
        return None
    code = any(not n.startswith(("data ", "format ", "docstring ", "other ", "new ")) for n in notes)
    if not code:
        return None
    heavy = [i for i in cand if secs[i] >= HEAVY]
    light = [i for i in cand if secs[i] < HEAVY]
    if not light:
        return None
    keep = _keep_heavy(heavy, light, regions) if DROP_HEAVY else heavy
    static = _cost(cand, secs)
    keep_cost = _cost(keep, secs)
    order = _order(light, secs, ids, notes)
    run_cpu = sum(cpu[i] for i in light) + _setup_cpu(light, ids)
    first = order[0]
    first_cpu = cpu[first] + _setup_cpu([first], ids)
    if hot_ok:
        base = HOT_CPU
    else:
        base = FRESH_CPU + (HEAVY_IMPORT_CPU if any(ids[i].split("::")[0] in HEAVY_IMPORT_FILES for i in light) else 0.0)
    check_cpu = base + run_cpu
    stop_cpu = base + first_cpu
    if hot_ok and not heavy:
        # run everything; print only the failures
        mode = "hot"
        stop = False
        fail_cost = _cost([first], secs)
        gain = static - (1 - P_CLEAN) * fail_cost - check_cpu
    else:
        # stop at the first failure; then print the static set minus tests seen passing
        mode = "hot" if hot_ok else "fresh"
        stop = True
        gain = P_CLEAN * (static - keep_cost - check_cpu) - (1 - P_CLEAN) * stop_cpu
    if check_cpu > MAX_CHECK_CPU or gain <= MARGIN:
        return None
    return {
        "mode": mode,
        "stop": stop,
        "verify": order,
        "keep_if_clean": keep,
        "heavy": heavy,
        "gain": gain,
        "cpu": check_cpu,
    }
