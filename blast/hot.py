"""Cheap local checks by hot-patching function bodies in a forked child.

The parent imports pristine sqlglot, every dialect and the test modules a
check needs, once. For each patch whose edits all sit inside function bodies
that never run at import time, a forked child compiles the patched files and
swaps the new code objects into the existing function objects (found through
the garbage collector, so closures and functions stored in tables are patched
too), then runs the chosen tests with pytest. Anything unexpected makes the
child report "ineligible" and the caller keeps its static answer.
"""

from __future__ import annotations

import gc
import json
import os
import pkgutil
import select
import signal
import sys
import time
import types

_state = {"ready": False, "repo": None, "modules": set()}


def prepare(repo, test_files, log=lambda *a: None):
    """Import pytest, pristine sqlglot (all dialects) and the given test files."""
    from .runner import _sync_pools

    if not _state["ready"]:
        os.environ["SKIP_INTEGRATION"] = "0"
        if repo not in sys.path:
            sys.path.insert(0, repo)
        _sync_pools()
        import pytest  # noqa: F401
        import _pytest.unittest  # noqa: F401
        import pytest_timeout  # noqa: F401
        import sqlglot
        import sqlglot.dialects

        for info in pkgutil.iter_modules(sqlglot.dialects.__path__):
            try:
                __import__(f"sqlglot.dialects.{info.name}")
            except Exception:
                pass
        _state["ready"] = True
        _state["repo"] = repo
    for path in sorted(test_files):
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        if mod in _state["modules"]:
            continue
        try:
            __import__(mod)
        except Exception as e:  # the check will report the import error itself
            log(f"preimport {mod} failed: {e!r}")
        _state["modules"].add(mod)


def _codes_by_qual(code):
    out = {}
    stack = [code]
    while stack:
        c = stack.pop()
        out.setdefault(c.co_qualname, []).append(c)
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)
    for v in out.values():
        v.sort(key=lambda c: c.co_firstlineno)
    return out


def _apply(repo, changed):
    """changed: {relpath: new_source}. Swaps code objects; raises on mismatch."""
    import importlib

    # a module first imported after the swap would load the old source, so
    # every edited module is imported (pristine) before patching
    for rel in changed:
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        importlib.import_module(mod)
    funcs_by_file = {}
    targets = {os.path.join(repo, rel): rel for rel in changed}
    for obj in gc.get_objects():
        if isinstance(obj, types.FunctionType):
            fn = obj.__code__.co_filename
            if fn in targets:
                funcs_by_file.setdefault(fn, []).append(obj)
    for full, rel in targets.items():
        new_code = compile(changed[rel], full, "exec", dont_inherit=True)
        new_by_q = _codes_by_qual(new_code)
        # the pristine code objects, from the module's compiled form
        with open(full, encoding="utf-8") as f:
            old_code = compile(f.read(), full, "exec", dont_inherit=True)
        old_by_q = _codes_by_qual(old_code)
        if set(new_by_q) != set(old_by_q) or any(len(new_by_q[q]) != len(old_by_q[q]) for q in old_by_q):
            raise RuntimeError("function structure changed")
        # pristine (qualname, firstlineno) -> new code
        mapping = {}
        for q, olds in old_by_q.items():
            for o, n in zip(olds, new_by_q[q]):
                mapping[(q, o.co_firstlineno)] = n
        for f in funcs_by_file.get(full, []):
            c = f.__code__
            n = mapping.get((c.co_qualname, c.co_firstlineno))
            if n is None:
                raise RuntimeError(f"no new code for {c.co_qualname}:{c.co_firstlineno}")
            if n.co_freevars != c.co_freevars:
                raise RuntimeError("closure changed")
            f.__code__ = n


def _child(repo, changed, ids, timeout, wfd, stop_on_fail=False):
    import pytest

    from .runner import _Collector

    result = {"eligible": True, "failed": [], "broken": [], "ran": []}
    t0 = time.process_time()
    try:
        _apply(repo, changed)
    except Exception as e:  # noqa: BLE001
        result = {"eligible": False, "error": repr(e)}
        os.write(wfd, json.dumps(result).encode())
        return
    t1 = time.process_time()
    os.chdir(repo)
    col = _Collector(stop_on_fail)
    args = [
        "-q", "-p", "no:cacheprovider", "--assert=plain", "-o", "console_output_style=classic",
        "--no-header", "--tb=no", "-p", "no:randomly", "--timeout", str(timeout),
        "--rootdir", repo, "-c", os.devnull,
    ] + list(ids)
    try:
        pytest.main(args, plugins=[col])
    except BaseException:  # noqa: BLE001
        pass
    t2 = time.process_time()
    result.update(failed=sorted(col.failed), broken=sorted(col.broken), ran=sorted(col.ran),
                  cpu_apply=round(t1 - t0, 3), cpu_pytest=round(t2 - t1, 3))
    os.write(wfd, json.dumps(result).encode())


def run(changed, ids, timeout, budget, stop_on_fail=False):
    """Fork, hot-patch, run. Returns a result dict or None (crash / no time)."""
    repo = _state["repo"]
    r, w = os.pipe()
    deadline = time.time() + budget
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            os.dup2(2, 1)
            _child(repo, changed, ids, timeout, w, stop_on_fail)
        except BaseException:  # noqa: BLE001
            code = 3
        finally:
            try:
                os.close(w)
            except OSError:
                pass
        os._exit(code)
    os.close(w)
    chunks = []
    killed = False
    while True:
        left = deadline - time.time()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            killed = True
            break
        rl, _, _ = select.select([r], [], [], left)
        if rl:
            b = os.read(r, 1 << 16)
            if not b:
                break
            chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    if killed or not chunks:
        return None
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        return None
