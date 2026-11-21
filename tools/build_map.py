"""Turn the tracer's pickles into the compact map the selector loads.

usage: python build_map.py <trace_dir> <tests.json> <sqlglot_repo> <out.pkl.gz>

Everything in the map comes from running sqlglot's own tests at the pinned
commit: which lines each test executes, which repo files it opens, which
sqlglot classes it instantiates, and the class hierarchy of the test classes.
"""

import gzip
import importlib
import os
import pickle
import sys
from collections import defaultdict


def main():
    trace_dir, tests_json, repo, out = sys.argv[1:5]
    import json

    tests = json.load(open(tests_json))["tests"]
    num = {t["id"]: t["number"] for t in tests}

    records = {}
    collect_lines = set()
    for name in sorted(os.listdir(trace_dir)):
        with open(os.path.join(trace_dir, name), "rb") as f:
            part = pickle.load(f)
        collect = part.pop("<collect>", None)
        if collect:
            collect_lines |= collect["lines"]
        records.update(part)

    missing = [t["id"] for t in tests if t["id"] not in records]
    print(f"records {len(records)}, tests {len(tests)}, missing {len(missing)}", file=sys.stderr)
    for m in missing[:20]:
        print("  missing", m, file=sys.stderr)

    # setUpClass runs inside the setup phase of whichever test of the class
    # ran first on each worker: share every setup phase across the class.
    class_setup = defaultdict(lambda: {"lines": set(), "files": set(), "types": set()})
    for nodeid, rec in records.items():
        setup = rec.get("setup")
        if setup:
            cls = nodeid.rsplit("::", 1)[0]
            for k in ("lines", "files", "types"):
                class_setup[cls][k] |= setup[k]

    lines = defaultdict(lambda: defaultdict(int))  # file -> line -> mask
    files = defaultdict(int)
    types = defaultdict(int)
    for nodeid, rec in records.items():
        n = num.get(nodeid)
        if n is None:
            continue
        bit = 1 << n
        cls = nodeid.rsplit("::", 1)[0]
        extra = class_setup.get(cls)
        all_lines = rec["lines"] | (rec["setup"]["lines"] if rec.get("setup") else set())
        all_files = set(rec["files"])
        all_types = set(rec["types"])
        if extra:
            all_lines |= extra["lines"]
            all_files |= extra["files"]
            all_types |= extra["types"]
        for path, line in all_lines:
            lines[path][line] |= bit
        for path in all_files:
            files[path] |= bit
        for t in all_types:
            types[t] |= bit

    # Deduplicate the masks: many lines share one test set.
    mask_ids = {}
    masks = []
    line_map = {}
    for path, by_line in lines.items():
        out_lines = {}
        for line, mask in by_line.items():
            i = mask_ids.get(mask)
            if i is None:
                i = mask_ids[mask] = len(masks)
                masks.append(mask)
            out_lines[line] = i
        line_map[path] = out_lines

    # Test class hierarchy: nodeid class -> qualified names of its bases.
    sys.path.insert(0, repo)
    os.chdir(repo)
    test_mro = {}
    for t in tests:
        path, cls, _ = t["id"].split("::")
        key = f"{path}::{cls}"
        if key in test_mro:
            continue
        modname = path[:-3].replace("/", ".")
        try:
            mod = importlib.import_module(modname)
            klass = getattr(mod, cls)
            names = []
            for c in klass.__mro__:
                m = getattr(c, "__module__", "")
                if m.startswith("tests"):
                    names.append(f"{m.replace('.', '/')}.py:{c.__qualname__}")
            test_mro[key] = names
        except Exception as e:  # pragma: no cover
            print("mro failed", key, e, file=sys.stderr)
            test_mro[key] = [f"{path}:{cls}"]

    # Tests that start a fresh interpreter (subprocess): the tracer cannot see
    # inside, so they depend on everything that runs when sqlglot is imported.
    import ast

    subproc = 0
    for t in tests:
        path, cls, meth = t["id"].split("::")
        try:
            tree = ast.parse(open(os.path.join(repo, path), encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for f in node.body:
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == meth:
                        src = ast.unparse(f)
                        if "subprocess" in src or "sys.executable" in src:
                            subproc |= 1 << t["number"]
    print(f"subprocess tests: {bin(subproc).count('1')}", file=sys.stderr)

    # Dialect components: a dialect's metaclass copies data out of its
    # tokenizer / parser / generator classes at import, so a change to one of
    # those classes can reach every user of the dialect.
    owners = defaultdict(set)
    import pkgutil

    import sqlglot.dialects as dialects_pkg
    from sqlglot.dialects.dialect import Dialect

    for info in pkgutil.iter_modules(dialects_pkg.__path__):
        try:
            importlib.import_module(f"sqlglot.dialects.{info.name}")
        except Exception as e:
            print("dialect import failed", info.name, e, file=sys.stderr)

    def subclasses(c):
        out = [c]
        for s in c.__subclasses__():
            out.extend(subclasses(s))
        return out

    for d in set(subclasses(Dialect)):
        dname = f"{d.__module__}:{d.__qualname__}"
        for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
            comp = getattr(d, attr, None)
            if not isinstance(comp, type):
                continue
            for c in comp.__mro__:
                mod = getattr(c, "__module__", "") or ""
                if mod.startswith("sqlglot"):
                    owners[f"{mod}:{c.__qualname__}"].add(dname)
    print(f"component classes with owners: {len(owners)}", file=sys.stderr)

    data = {
        "owners": {k: sorted(v) for k, v in owners.items()},
        "subproc": subproc,
        "ids": [t["id"] for t in tests],
        "masks": masks,
        "lines": line_map,
        "files": dict(files),
        "types": dict(types),
        "collect": sorted(collect_lines),
        "test_mro": test_mro,
    }
    with gzip.open(out, "wb", compresslevel=9) as f:
        pickle.dump(data, f, protocol=5)
    print(
        f"masks {len(masks)}, files {len(line_map)}, lines {sum(len(v) for v in line_map.values())}, "
        f"types {len(types)}, opened {len(files)}, size {os.path.getsize(out)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
