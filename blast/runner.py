"""Run a few sqlglot tests against a patched tree and report which fail.

The patched tree is a symlink farm in /tmp: every path the patch touches is a
real file, everything else links to the pristine checkout (whose bytecode was
compiled at build time). Each verification forks from this process (which has
already imported pytest and, when needed, the heavy third-party modules the
tests use) and runs pytest in the child, so failures mean exactly what they
mean for the labels: failure, error, failing subtest, timeout, import error.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import time

_preloaded = set()


def preload(heavy=False):
    """Import what every verification needs once, in the parent."""
    if "base" not in _preloaded:
        import unittest  # noqa: F401

        import pytest  # noqa: F401
        import _pytest.unittest  # noqa: F401
        import _pytest.subtests  # noqa: F401
        import pytest_timeout  # noqa: F401

        _preloaded.add("base")
    if heavy and "heavy" not in _preloaded:
        for mod in ("numpy", "pandas", "duckdb", "pandas.testing"):
            try:
                __import__(mod)
            except Exception:
                pass
        _preloaded.add("heavy")


def make_tree(repo, ws, changed):
    """changed: {relpath: new_text or None (deleted)}."""
    if os.path.exists(ws):
        shutil.rmtree(ws)
    os.makedirs(ws)
    real_dirs = {""}
    for rel in changed:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))

    def fill(rel_dir):
        src_dir = os.path.join(repo, rel_dir) if rel_dir else repo
        dst_dir = os.path.join(ws, rel_dir) if rel_dir else ws
        os.makedirs(dst_dir, exist_ok=True)
        try:
            entries = os.listdir(src_dir)
        except FileNotFoundError:
            entries = []
        for name in entries:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in changed:
                continue
            if rel in real_dirs:
                fill(rel)
            else:
                os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))

    fill("")
    for rel, text in changed.items():
        if text is None:
            continue
        dst = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)


class _Collector:
    def __init__(self, stop_on_fail):
        self.ran = set()
        self.failed = set()
        self.broken = set()
        self.stop = stop_on_fail
        self.session = None

    def pytest_sessionstart(self, session):
        self.session = session

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.ran.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)
            if self.stop and self.session is not None:
                self.session.shouldstop = "first failure"

    def pytest_collectreport(self, report):
        if report.failed:
            self.broken.add(report.nodeid)
            if self.stop and self.session is not None:
                self.session.shouldstop = "collection failure"


def _sync_pools():
    """Process pools run their work in-process, with the pickle round trip a
    real pool does, so a verification never spawns interpreters."""
    import concurrent.futures as cf
    import pickle

    class SyncExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, *args, **kwargs):
            fut = cf.Future()
            try:
                a, k = pickle.loads(pickle.dumps((args, kwargs)))
                fut.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)
            return fut

        def map(self, fn, *its, timeout=None, chunksize=1):
            return [self.submit(fn, *a).result() for a in zip(*its)]

        def shutdown(self, wait=True, cancel_futures=False):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    cf.ProcessPoolExecutor = SyncExecutor


def _child(ws, ids, timeout, stop_on_fail, wfd):
    import pytest

    os.chdir(ws)
    sys.path.insert(0, ws)
    os.environ["SKIP_INTEGRATION"] = "0"
    _sync_pools()
    col = _Collector(stop_on_fail)
    args = [
        "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--assert=plain",
        "-o", "console_output_style=classic", "--no-header", "--tb=no",
        "--timeout", str(timeout), "--rootdir", ws, "-c", os.devnull,
    ] + list(ids)
    try:
        pytest.main(args, plugins=[col])
    except BaseException:  # noqa: BLE001
        pass
    out = {"failed": sorted(col.failed), "broken": sorted(col.broken), "ran": sorted(col.ran)}
    os.write(wfd, json.dumps(out).encode())
    os.close(wfd)


def run(ws, ids, timeout, budget, stop_on_fail=True):
    """Fork and run ids in ws. Returns {"failed": [...], "broken": [...]} or None."""
    if not ids:
        return {"failed": [], "broken": []}
    r, w = os.pipe()
    deadline = time.time() + budget
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            # stdout carries the answers: anything the tests print goes to stderr
            os.dup2(2, 1)
            _child(ws, ids, timeout, stop_on_fail, w)
        except BaseException:  # noqa: BLE001
            code = 3
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
