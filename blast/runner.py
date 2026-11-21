"""Run a few sqlglot tests against a patched tree and report which fail.

The patched tree is a symlink farm in /tmp: every path the patch touches is a
real file, everything else links to the pristine checkout (whose bytecode was
compiled at build time). Tests run with unittest in a forked child, with a
timer per test; a test that errors, fails, has a failing subtest, times out or
whose module cannot be imported counts as failing.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time


def make_tree(repo, ws, changed):
    """changed: {relpath: new_text or None (deleted)}."""
    if os.path.exists(ws):
        shutil.rmtree(ws)
    os.makedirs(ws)
    # directories that need to be real because something inside changes
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
        dst = os.path.join(ws, rel)
        if text is None:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)


class _Timeout(BaseException):
    pass


def _child(ws, ids, timeouts, deadline, wfd):
    import importlib
    import pickle
    import unittest

    os.chdir(ws)
    sys.path.insert(0, ws)
    os.environ["SKIP_INTEGRATION"] = "0"

    # Process pools: run the work here (with a pickle round trip, as a real
    # pool would) so a verification never spawns interpreters.
    import concurrent.futures as cf

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

    failed = set()
    current = {"id": None}

    def on_alarm(signum, frame):
        raise _Timeout(f"timeout {current['id']}")

    signal.signal(signal.SIGALRM, on_alarm)

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.bad = set()

        def _id(self, test):
            return getattr(test, "_blast_id", None)

        def startTest(self, test):
            super().startTest(test)
            tid = self._id(test)
            current["id"] = tid
            left = deadline - time.time()
            t = min(timeouts.get(tid, 10.0), max(0.05, left))
            signal.setitimer(signal.ITIMER_REAL, t)

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_REAL, 0)
            super().stopTest(test)

        def addError(self, test, err):
            tid = self._id(test)
            if tid is None:
                # setUpClass / module fixture: every test of that class fails
                self.bad.add(("fixture", str(test)))
            else:
                self.bad.add(tid)

        def addFailure(self, test, err):
            self.bad.add(self._id(test))

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.bad.add(self._id(test))

        def addUnexpectedSuccess(self, test):
            self.bad.add(self._id(test))

    by_mod = {}
    for tid in ids:
        path, cls, meth = tid.split("::")
        by_mod.setdefault(path, {}).setdefault(cls, []).append((meth, tid))

    for path, classes in by_mod.items():
        modname = path[:-3].replace("/", ".")
        try:
            mod = importlib.import_module(modname)
        except BaseException:  # noqa: BLE001
            for cls, items in classes.items():
                failed.update(t for _, t in items)
            continue
        for cls, items in classes.items():
            if time.time() > deadline:
                failed.update(t for _, t in items)
                continue
            try:
                klass = getattr(mod, cls)
                suite = unittest.TestSuite()
                for meth, tid in items:
                    case = klass(meth)
                    case._blast_id = tid
                    suite.addTest(case)
                result = Result()
                suite.run(result)
                unittest.suite._call_if_exists(result, "_tearDownPreviousClass")
                for b in result.bad:
                    if isinstance(b, tuple) or b is None:
                        failed.update(t for _, t in items)
                    else:
                        failed.add(b)
                for test, _ in result.skipped:
                    pass
            except _Timeout:
                failed.update(t for _, t in items)
            except BaseException:  # noqa: BLE001
                failed.update(t for _, t in items)
    signal.setitimer(signal.ITIMER_REAL, 0)
    os.write(wfd, json.dumps(sorted(failed)).encode())
    os.close(wfd)


def run(ws, ids, timeouts, budget):
    """Fork, run ids in ws, return (failed_ids or None on crash, cpu_seconds)."""
    if not ids:
        return set(), 0.0
    r, w = os.pipe()
    deadline = time.time() + budget
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        # stdout belongs to the answers: send everything the tests print to stderr
        try:
            os.dup2(2, 1)
            _child(ws, ids, timeouts, deadline, w)
        except BaseException:  # noqa: BLE001
            os._exit(3)
        os._exit(0)
    os.close(w)
    chunks = []
    killed = False
    import select

    while True:
        left = deadline + 2.0 - time.time()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            killed = True
            break
        rl, _, _ = select.select([r], [], [], left)
        if rl:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    if killed or not chunks:
        return None
    try:
        return set(json.loads(b"".join(chunks)))
    except ValueError:
        return None
