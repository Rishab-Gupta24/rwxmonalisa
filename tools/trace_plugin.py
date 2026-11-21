"""pytest plugin: records, per test, what it depends on at the pinned sqlglot commit.

For each test it records
  * lines: every (file, line) executed in the repo (sqlglot/ and tests/)
  * files: every repo file opened for reading (fixtures, sources read as text)
  * types: every sqlglot class (MRO-closed) whose __init__/__new__ ran on an
    instance, or which was passed as ``cls`` to a classmethod

Lines executed during collection (imports) are stored under the key "<collect>".
Run with: python -m pytest -p trace_plugin ... (tools/ on PYTHONPATH),
TRACE_OUT=<dir> picks the output directory (one pickle per xdist worker).
"""

import builtins
import io
import os
import pickle
import sys

import pytest

ROOT = os.path.realpath(os.getcwd()) + os.sep
TOOL = 4
mon = sys.monitoring
DISABLE = mon.DISABLE

_state = {"lines": set(), "files": set(), "types": set()}
_records = {}
_code_ok = {}


def _in_repo(filename):
    ok = _code_ok.get(filename)
    if ok is None:
        real = os.path.realpath(filename) if filename and not filename.startswith("<") else ""
        ok = real[len(ROOT):] if real.startswith(ROOT) else False
        if ok and not (ok.startswith("sqlglot" + os.sep) or ok.startswith("tests" + os.sep)):
            ok = False
        _code_ok[filename] = ok
    return ok


def _on_line(code, line):
    rel = _in_repo(code.co_filename)
    if rel:
        _state["lines"].add((rel, line))
    return DISABLE


_INIT_NAMES = {"__init__", "__new__", "__post_init__", "__init_subclass__"}


def _on_start(code, offset):
    if not _in_repo(code.co_filename):
        return DISABLE
    if code.co_argcount == 0:
        return DISABLE
    first = code.co_varnames[0]
    if first == "cls":
        try:
            obj = sys._getframe(1).f_locals.get("cls")
        except Exception:
            return None
        if isinstance(obj, type):
            _state["types"].add(obj)
        return None
    if first == "self" and code.co_name in _INIT_NAMES:
        try:
            obj = sys._getframe(1).f_locals.get("self")
        except Exception:
            return None
        _state["types"].add(type(obj))
        return None
    return DISABLE


_real_open = builtins.open


def _open(file, *args, **kwargs):
    try:
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.fsdecode(os.fspath(file))
            real = os.path.realpath(p)
            if real.startswith(ROOT):
                _state["files"].add(real[len(ROOT):])
    except Exception:
        pass
    return _real_open(file, *args, **kwargs)


def _type_names(types):
    out = set()
    for t in types:
        try:
            mro = t.__mro__
        except Exception:
            continue
        for c in mro:
            mod = getattr(c, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                out.add(f"{mod}:{c.__qualname__}")
    return out


def _flush():
    rec = {
        "lines": _state["lines"],
        "files": _state["files"],
        "types": _type_names(_state["types"]),
    }
    _state["lines"] = set()
    _state["files"] = set()
    _state["types"] = set()
    return rec


class _SyncExecutor:
    """Runs process-pool work in this process so the tracer sees it."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            # a real pool pickles arguments and results (sqlglot's serde)
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [fn(*a) for a in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pytest_configure(config):
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = _SyncExecutor
    builtins.open = _open
    io.open = _open
    mon.use_tool_id(TOOL, "blast_trace")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)


@pytest.hookimpl(hookwrapper=True)
def pytest_collection(session):
    yield
    _records["<collect>"] = _flush()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _flush()
    mon.restart_events()
    yield
    rec = _flush()
    rec["setup"] = _setup.pop(item.nodeid, None)
    _records[item.nodeid] = rec


_setup = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield
    # Everything up to here (setUpClass, setUp, fixtures) is the setup phase.
    rec = _flush()
    _setup[item.nodeid] = rec
    mon.restart_events()


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    out = os.environ.get("TRACE_OUT", "/tmp/trace")
    os.makedirs(out, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with _real_open(os.path.join(out, f"{worker}.pkl"), "wb") as f:
        pickle.dump(_records, f)
