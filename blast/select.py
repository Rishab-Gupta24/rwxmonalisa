"""From one patch to a bitmask of tests that could observe it."""

from __future__ import annotations

import ast
import os

from .analyze import (
    FUNC, DEFS, apply_patch, bound_names, code_signature, doc_free_dump, func_span, locate,
    module_name, parse_patch, qualname_of, stmt_span, _strip,
)


class Selector:
    def __init__(self, data, index, repo):
        self.repo = repo
        self.ids = data["ids"]
        self.n = len(self.ids)
        self.ALL = (1 << self.n) - 1
        self.masks = data["masks"]
        self.lines = data["lines"]
        self.files = data["files"]
        self.types = data["types"]
        self.collect = {}
        for path, line in data["collect"]:
            self.collect.setdefault(path, set()).add(line)
        self.subproc = data.get("subproc", 0)
        self.refs = index["refs"]
        self.strs = index.get("strs", {})
        self.owners = data.get("owners", {})
        self.from_imports = index["from_imports"]
        # test classes: MRO name -> mask, file -> mask
        self.test_cls = {}
        self.test_file = {}
        cls_of = {}
        for i, tid in enumerate(self.ids):
            path, cls, _ = tid.split("::")
            self.test_file[path] = self.test_file.get(path, 0) | (1 << i)
            cls_of.setdefault(f"{path}::{cls}", 0)
            cls_of[f"{path}::{cls}"] |= 1 << i
        for key, mask in cls_of.items():
            for name in data["test_mro"].get(key, [key.replace("::", ":")]):
                self.test_cls[name] = self.test_cls.get(name, 0) | mask
        self._src = {}
        self._tree = {}

    # ------------------------------------------------------------------ map access
    def cov(self, path, lo, hi):
        by_line = self.lines.get(path)
        if not by_line:
            return 0
        m = 0
        masks = self.masks
        for ln in range(lo, hi + 1):
            i = by_line.get(ln)
            if i is not None:
                m |= masks[i]
        return m

    def file_cov(self, path):
        by_line = self.lines.get(path)
        m = 0
        if by_line:
            for i in set(by_line.values()):
                m |= self.masks[i]
        return m

    def class_users(self, path, qual):
        if path.startswith("tests/"):
            return self.test_cls.get(f"{path}:{qual}", 0)
        key = f"{module_name(path)}:{qual}"
        m = self.types.get(key, 0)
        for owner in self.owners.get(key, ()):
            m |= self.types.get(owner, 0)
        # a nested class (Dialect.Tokenizer) feeds its enclosing class too
        if "." in qual:
            outer = qual.rsplit(".", 1)[0]
            m |= self.types.get(f"{module_name(path)}:{outer}", 0)
        return m

    def attr_readers(self, names):
        """Tests that execute a line reading any of these attribute names, or
        None when some read happens at import time or through a string, where
        line coverage cannot tell who is affected."""
        m = 0
        for name in names:
            if name in self.strs:
                return None
            for rpath, ctx, base in self.refs.get(name, ()):
                kind = ctx[0]
                if kind not in ("body", "header"):
                    return None
                lo, hi = ctx[1], ctx[2]
                col = self.collect.get(rpath)
                if col and any(lo <= ln <= hi for ln in col):
                    return None
                if kind == "header":
                    return None
                m |= self.cov(rpath, lo, hi)
        return m

    def module_users(self, path):
        m = self.file_cov(path)
        if path.startswith("tests/"):
            m |= self.test_file.get(path, 0)
        else:
            prefix = module_name(path) + ":"
            for k, v in self.types.items():
                if k.startswith(prefix):
                    m |= v
        return m

    def import_breaks(self, path):
        """Tests that fail if this module cannot be imported."""
        if path in self.collect:
            if path.startswith("tests/"):
                # every test file that imports it at collection
                m = self.test_file.get(path, 0)
                mod = module_name(path)
                for (base, name), users in self.from_imports.items():
                    if base == mod or base.startswith(mod + "."):
                        for p, _ in users:
                            if p.startswith("tests/"):
                                m |= self.test_file.get(p, 0) | self.file_cov(p)
                return m | self.file_cov(path)
            return self.ALL
        return self.module_users(path)

    # ------------------------------------------------------------------ sources
    def source(self, path):
        s = self._src.get(path)
        if s is None:
            full = os.path.join(self.repo, path)
            try:
                with open(full, encoding="utf-8") as f:
                    s = f.read()
            except (OSError, UnicodeDecodeError):
                s = False
            self._src[path] = s
        return s

    def tree(self, path):
        t = self._tree.get(path)
        if t is None:
            src = self.source(path)
            try:
                t = ast.parse(src) if src else False
            except SyntaxError:
                t = False
            self._tree[path] = t
        return t

    # ------------------------------------------------------------------ references
    def refs_of(self, path, names, depth=0, seen=None):
        """Tests reaching any reference to module-level names of `path`."""
        if seen is None:
            seen = set()
        m = 0
        mod = module_name(path)
        todo = []
        for name in names:
            if (path, name) in seen:
                continue
            seen.add((path, name))
            for rpath, ctx, base in self.refs.get(name, ()):
                if base is None:
                    if rpath != path:
                        continue
                else:
                    # attribute access: only through something naming this module
                    last = mod.rsplit(".", 1)[-1]
                    if last not in base.split(".") and not (mod.startswith("sqlglot.expressions") and base.split(".")[-1] == "exp"):
                        continue
                m |= self._ctx_mask(rpath, ctx, todo)
            for rpath, asname in self.from_imports.get((mod, name), ()):
                todo.append((rpath, (asname,)))
            if mod.startswith("sqlglot.expressions"):
                for rpath, asname in self.from_imports.get(("sqlglot.expressions", name), ()):
                    todo.append((rpath, (asname,)))
        if depth < 4:
            for rpath, nm in todo:
                m |= self.refs_of(rpath, nm, depth + 1, seen)
        else:
            for rpath, nm in todo:
                m |= self.module_users(rpath)
        return m

    def _ctx_mask(self, rpath, ctx, todo):
        kind = ctx[0]
        if kind in ("body", "header"):
            return self.cov(rpath, ctx[1], ctx[2])
        if kind == "class":
            return self.class_users(rpath, ctx[1])
        # module-level statement using the name: follow what it binds
        if ctx[1]:
            todo.append((rpath, ctx[1]))
            return 0
        return self.module_users(rpath)

    def attr_refs(self, path, qual, names):
        """Class attributes copied or read through the class's own name."""
        m = 0
        simple = qual.rsplit(".", 1)[-1]
        todo = []
        for name in names:
            for rpath, ctx, base in self.refs.get(name, ()):
                if base is None:
                    continue
                parts = base.split(".")
                if simple in parts or base.endswith(qual) or base in ("self", "cls") and rpath == path:
                    if base in ("self", "cls"):
                        continue
                    m |= self._ctx_mask(rpath, ctx, todo)
        for rpath, nm in todo:
            m |= self.refs_of(rpath, nm, 1)
        return m

    # ------------------------------------------------------------------ main entry
    def select(self, patch_text):
        notes = []
        mask = 0
        for fp in parse_patch(patch_text):
            m, note = self.file_mask(fp)
            mask |= m
            notes.append(note)
        return mask, notes

    def file_mask(self, fp):
        path = fp.path
        old_path = fp.old_path
        if not path:
            return 0, "nopath"
        opened = self.files.get(old_path or path, 0) | self.files.get(path, 0)
        if not path.endswith(".py"):
            return opened, f"data {path}"
        if not (path.startswith("sqlglot/") or path.startswith("tests/")):
            return opened, f"other {path}"
        if old_path is None:
            # a new module: only matters if something imports it
            return opened, f"new {path}"
        old_src = self.source(old_path)
        if old_src is False:
            return opened | self.ALL, f"unreadable {path}"
        if fp.new_path is None:
            return opened | self.import_breaks(old_path), f"deleted {path}"
        new_src, removed, inserted, added = apply_patch(old_src, fp)
        old_tree = self.tree(old_path)
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            return opened | self.import_breaks(old_path), f"syntax {path}"
        if old_tree is False:
            return opened | self.ALL, f"oldparse {path}"
        if _strip(old_tree) == _strip(new_tree):
            return opened, f"format {path}"
        mask = opened
        if doc_free_dump(ast.parse(old_src)) == doc_free_dump(ast.parse(new_src)):
            # docstring-only edit: readers of the source and doc tests
            mask |= self.file_cov_tests(path, "tests/test_docs.py")
            return mask, f"docstring {path}"

        sig_old = sig_new = None
        regions = []
        for ln in sorted(removed):
            regions.append(("line", ln))
        for k in sorted(inserted):
            regions.append(("insert", k))

        kinds = []
        for kind, ln in regions:
            if kind == "line":
                ctx = locate(old_tree, ln)
            else:
                # an insertion between old k and k+1: the context of the next
                # old line, unless that is shallower than the previous one
                ctx = self._insert_ctx(old_tree, ln)
            m, why = self.ctx_mask(path, old_tree, ctx, kind, new_tree, new_src, ln)
            if why == "body" and sig_old is None:
                sig_old = code_signature(old_src, path)
                sig_new = code_signature(new_src, path)
            if why == "body" and sig_old is not None and sig_new is not None and ctx.func is not None:
                q = self._func_qual(old_tree, ctx.func)
                if q is not None and sig_old.get(q) != sig_new.get(q):
                    m |= self._func_mask(path, ctx.func)
                    why = "body+locals"
            mask |= m
            kinds.append(why)
        if path in self.collect and set(kinds) - {"body", "func", "body+locals", "const"}:
            # import-time behaviour changed: fresh-interpreter tests can see it
            mask |= self.subproc
        return mask, f"{path}: {','.join(sorted(set(kinds)))}"

    def file_cov_tests(self, path, test_path):
        return self.test_file.get(test_path, 0) & self.module_users(path)

    def _func_qual(self, tree, f):
        if isinstance(f, ast.Lambda):
            return None
        return qualname_of(tree, f)

    def _func_mask(self, path, f):
        if isinstance(f, ast.Lambda):
            return self.cov(path, f.lineno, f.end_lineno)
        lo, hi = func_span(f)
        return self.cov(path, lo, hi)

    def _insert_ctx(self, tree, k):
        nxt = locate(tree, k + 1) if k >= 0 else None
        prv = locate(tree, k) if k >= 1 else None
        # prefer the deeper (function body) context
        for c in (prv, nxt):
            if c is not None and c.kind == "body":
                return c
        for c in (prv, nxt):
            if c is not None and c.kind == "header":
                return c
        for c in (prv, nxt):
            if c is not None and c.kind == "class":
                return c
        return nxt or prv

    def ctx_mask(self, path, old_tree, ctx, kind, new_tree, new_src, ln):
        if ctx.kind == "body":
            if kind == "insert" or ctx.stmt is None:
                return self._func_mask(path, ctx.func), "func"
            lo, hi = stmt_span(ctx.stmt)
            return self.cov(path, lo, hi), "body"
        if ctx.kind == "header":
            return self._func_mask(path, ctx.func), "func"
        if ctx.kind == "class":
            qual = ".".join(ctx.classes)
            m = self.class_users(path, qual)
            if ctx.stmt is not None:
                names = bound_names(ctx.stmt)
                if names and isinstance(ctx.stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and not path.startswith("tests/"):
                    readers = self.attr_readers(names)
                    if readers is not None:
                        m &= readers
                if names:
                    m |= self.attr_refs(path, qual, names)
            if path.startswith("tests/"):
                m |= self.cov(path, *func_span(ctx.cls))
            return m, "class"
        # module level
        stmt = ctx.stmt
        if stmt is None:
            return self.module_users(path), "module?"
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            return self.import_breaks(path) | self.refs_of(path, bound_names(stmt)), "import"
        names = bound_names(stmt)
        if isinstance(stmt, ast.ClassDef):
            return self.class_users(path, stmt.name) | self.refs_of(path, names), "classdef"
        if names:
            m = self.refs_of(path, names)
            if path.startswith("tests/"):
                m |= self.test_file.get(path, 0) & self.file_cov(path)
            return m, "global"
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            return 0, "const"
        return self.module_users(path), "module"
