"""Static facts about the pristine sqlglot checkout, computed once (at image build).

refs:  name -> [(path, ctx)] for every load of a Name or Attribute in sqlglot/
       and tests/, where ctx summarises where the reference executes:
         ("body", lo, hi)            runtime statement span
         ("header", lo, hi)          function header -> whole function span
         ("class", qualname, names)  class body statement binding names
         ("module", names)           module-level statement binding names
       Attribute entries also keep the dotted text of their base object.
from_imports: (module, name) -> [(path, asname)]
"""

from __future__ import annotations

import ast
import os
import pickle

from .analyze import FUNC, DEFS, bound_names, dotted, locate, module_name, stmt_span, func_span


def _iter_py(root):
    for top in ("sqlglot", "tests"):
        base = os.path.join(root, top)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    full = os.path.join(dirpath, fn)
                    yield os.path.relpath(full, root).replace(os.sep, "/"), full


def ctx_summary(tree, line):
    c = locate(tree, line)
    if c.kind == "body":
        if c.stmt is None:
            lo, hi = func_span(c.func) if not isinstance(c.func, ast.Lambda) else (c.func.lineno, c.func.end_lineno)
            return ("header", lo, hi)
        lo, hi = stmt_span(c.stmt)
        return ("body", lo, hi)
    if c.kind == "header":
        if isinstance(c.func, ast.Lambda):
            return ("header", c.func.lineno, c.func.end_lineno)
        lo, hi = func_span(c.func)
        return ("header", lo, hi)
    if c.kind == "class":
        names = tuple(sorted(bound_names(c.stmt))) if c.stmt is not None else ()
        return ("class", ".".join(c.classes), names)
    return ("module", tuple(sorted(bound_names(c.stmt))) if c.stmt is not None else ())


def build(root):
    refs: dict[str, list] = {}
    strs: dict[str, list] = {}
    from_imports: dict[tuple, list] = {}
    for rel, full in _iter_py(root):
        try:
            src = open(full, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        mod = module_name(rel)
        cache = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:
                    pkg = mod.split(".")
                    if not rel.endswith("__init__.py"):
                        pkg = pkg[:-1]
                    pkg = pkg[: len(pkg) - node.level + 1]
                    base = ".".join(pkg + ([node.module] if node.module else []))
                for a in node.names:
                    from_imports.setdefault((base, a.name), []).append((rel, a.asname or a.name))
            name = None
            base = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if 0 < len(v) < 80 and v.replace("_", "a").isalnum() and not v[0].isdigit():
                    line = node.lineno
                    summ = cache.get(line)
                    if summ is None:
                        summ = cache[line] = ctx_summary(tree, line)
                    strs.setdefault(v, []).append((rel, summ))
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
                base = dotted(node.value)
            if name is None:
                continue
            line = node.lineno
            summ = cache.get(line)
            if summ is None:
                summ = cache[line] = ctx_summary(tree, line)
            refs.setdefault(name, []).append((rel, summ, base))
    # only names bound at module or class level are ever looked up
    def_names = set()
    for rel, full in _iter_py(root):
        try:
            tree = ast.parse(open(full, encoding="utf-8").read())
        except Exception:
            continue
        stack = [tree]
        while stack:
            node = stack.pop()
            for stmt in node.body:
                def_names |= bound_names(stmt)
                if isinstance(stmt, ast.ClassDef):
                    stack.append(stmt)
                    def_names.add(stmt.name)
                elif isinstance(stmt, (ast.If, ast.Try)):
                    stack.append(stmt)
            for extra in ("orelse", "finalbody"):
                for stmt in getattr(node, extra, []) or []:
                    def_names |= bound_names(stmt)
    refs = {k: v for k, v in refs.items() if k in def_names}
    strs = {k: v for k, v in strs.items() if k in def_names}
    return {"refs": refs, "strs": strs, "from_imports": from_imports}


def save(root, out):
    data = build(root)
    with open(out, "wb") as f:
        pickle.dump(data, f, protocol=5)


if __name__ == "__main__":
    import sys

    save(sys.argv[1], sys.argv[2])
