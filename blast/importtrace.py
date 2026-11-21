"""Lines of sqlglot/ and tests/ that run while everything is imported, and for
each, which modules' imports ran it.

A function whose body runs at import time (metaclasses, table builders,
module-level calls in test files) shapes the classes and tables those modules
build, so an edit there reaches every user of those modules. It also cannot be
hot-patched: its effects were computed with the old code. Run at image build:

    python -m blast.importtrace <repo> <out.pkl>

Output: {"lines": [(path, line)], "mods": {(path, line): (module_path, ...)}}
"""

import os
import pickle
import pkgutil
import sys


def main():
    repo, out = sys.argv[1:3]
    root = os.path.realpath(repo) + os.sep
    mods = {}
    mon = sys.monitoring
    tool = 5
    cache = {}
    stack = []

    def rel_of(fn):
        rel = cache.get(fn)
        if rel is None:
            real = os.path.realpath(fn) if not fn.startswith("<") else ""
            rel = cache[fn] = real[len(root):] if real.startswith(root) else ""
        return rel

    def on_start(code, offset):
        if code.co_name != "<module>":
            return mon.DISABLE
        stack.append(rel_of(code.co_filename))

    def on_end(code, offset, *rest):
        if code.co_name != "<module>":
            return mon.DISABLE
        if stack:
            stack.pop()

    def on_unwind(code, offset, exc):
        # PY_UNWIND cannot be disabled per location
        if code.co_name == "<module>" and stack:
            stack.pop()

    def on_line(code, line):
        # module and class bodies are def-time code the selector already treats
        # as such; only function bodies running at import matter here
        if not code.co_flags & 0x1:
            return mon.DISABLE
        rel = rel_of(code.co_filename)
        if rel:
            key = (rel, line)
            top = stack[-1] if stack else ""
            s = mods.get(key)
            if s is None:
                mods[key] = {top}
            elif top not in s:
                s.add(top)

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, repo)
    os.chdir(repo)
    mon.use_tool_id(tool, "importtrace")
    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.register_callback(tool, mon.events.PY_RETURN, on_end)
    mon.register_callback(tool, mon.events.PY_UNWIND, on_unwind)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START | mon.events.PY_RETURN | mon.events.PY_UNWIND)
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
    data = {
        "lines": sorted(mods),
        "mods": {k: tuple(sorted(m for m in v if m)) for k, v in mods.items()},
    }
    with open(out, "wb") as f:
        pickle.dump(data, f)
    print(f"import-time lines: {len(mods)}", file=sys.stderr)


if __name__ == "__main__":
    main()
