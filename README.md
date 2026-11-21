# Blast Radius selector

Picks the sqlglot tests a patch can break, for the RWX Blast Radius track.

## How it works

1. **Dependency map** (`data/map.pkl.gz`, built once by `tools/`): sqlglot's test
   suite run at the pinned commit under a `sys.monitoring` tracer
   (`tools/trace_plugin.py`). For every test it records the lines it executes,
   the repo files it opens (fixtures), and the sqlglot classes it instantiates.
   Process-pool work is run in-process (with a pickle round trip) so nothing
   escapes the tracer.
2. **Static index** (built in the Docker image from the pristine sources):
   where every name and attribute is referenced, and in what execution context.
3. **Per patch** (`blast/select.py`): the patch is applied to the pristine file
   and each changed region is placed:
   - statement in a function body -> tests that executed that statement
   - function header, inserted statements, or changed local-variable set ->
     tests that executed the function
   - class-body statement -> tests that used the class (or a subclass, or a
     dialect built from it), narrowed to tests that execute a line reading the
     attribute when no read happens at import time or by string
   - module-level binding -> tests reaching any reference to it, following
     imports
   - comment / formatting-only edits (identical AST) -> only tests that read the
     file as data
   - syntax errors, deleted modules, import changes -> every test that imports it
   - non-Python files -> tests that opened them
4. **Packing** (`blast/pack.py`): longest-first onto 1..8 workers, keeping the
   worker count with the lowest busiest-worker time + 2 s per worker.

## Rebuilding the map

```sh
SKIP_INTEGRATION=0 TRACE_OUT=trace PYTHONPATH=tools \
  python -m pytest -p trace_plugin -n 3 tests        # in the sqlglot checkout
python tools/build_map.py trace tests.json <sqlglot> data/map.pkl.gz
```
