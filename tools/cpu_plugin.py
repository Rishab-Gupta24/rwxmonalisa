"""pytest plugin: per-test CPU seconds when run the way blast/runner.py runs them
(process pools in-process). Writes JSON {nodeid: cpu_seconds} to CPU_OUT."""

import json
import os
import resource
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime + c.ru_utime + c.ru_stime


_t = {}
_start = {}
_mod_first = {}


def pytest_configure(config):
    from blast.runner import _sync_pools

    _sync_pools()
    _t["<start>"] = _cpu()


def pytest_collection_finish(session):
    _t["<collect>"] = _cpu() - _t["<start>"]


def pytest_runtest_protocol(item, nextitem):
    _start[item.nodeid] = _cpu()


def pytest_runtest_logfinish(nodeid, location):
    if nodeid in _start:
        _t[nodeid] = _t.get(nodeid, 0) + _cpu() - _start.pop(nodeid)


def pytest_unconfigure(config):
    with open(os.environ.get("CPU_OUT", "/tmp/cpu.json"), "w") as f:
        json.dump(_t, f, indent=0)
