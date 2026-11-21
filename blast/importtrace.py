"""Lines of sqlglot/ and tests/ that run while everything is imported.

A function whose body runs at import time (metaclasses, table builders,
module-level calls in test files) cannot be hot-patched: its effects were
computed with the old code before the patch. Run at image build:

    python -m blast.importtrace <repo> <out.pkl>
"""

import os
import pickle
import pkgutil
import sys


def main():
    repo, out = sys.argv[1:3]
    root = os.path.realpath(repo) + os.sep
    lines = set()
    mon = sys.monitoring
    tool = 5
    cache = {}

    def on_line(code, line):
        fn = code.co_filename
        rel = cache.get(fn)
        if rel is None:
            real = os.path.realpath(fn) if not fn.startswith("<") else ""
            rel = cache[fn] = real[len(root):] if real.startswith(root) else ""
        if rel:
            lines.add((rel, line))
        return mon.DISABLE

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, repo)
    os.chdir(repo)
    mon.use_tool_id(tool, "importtrace")
    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    import sqlglot
    import sqlglot.dialects

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            __import__(info.name)
        except Exception:
            pass
    for dirpath, dirnames, filenames in os.walk(os.path.join(repo, "tests")):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".py") and (fn.startswith("test_") or fn == "helpers.py"):
                rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                try:
                    __import__(rel[:-3].replace(os.sep, "."))
                except Exception:
                    pass
    mon.set_events(tool, 0)
    with open(out, "wb") as f:
        pickle.dump(sorted(lines), f)
    print(f"import-time lines: {len(lines)}", file=sys.stderr)


if __name__ == "__main__":
    main()
