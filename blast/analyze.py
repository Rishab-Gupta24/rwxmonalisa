"""Static side of the selector: from a patch to the set of tests that could see it.

The map (built from running sqlglot's tests at the pinned commit) says which
tests execute each line, open each file and instantiate each class. This module
works out, for every changed region of a patch, which of those facts matter:

* a changed statement inside a function body -> tests that executed it
* a changed function header, or new statements in a function -> tests that
  executed any line of that function
* a changed class body statement (attributes, dict tables) -> tests that used
  that class or a subclass, plus classes that copy from it by name
* a changed module-level binding -> tests that reach every reference to it
* a changed non-Python file -> tests that opened it
* anything we cannot place -> every test that could import the module
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
COMPOUND = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match,
) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


# --------------------------------------------------------------------------- patch


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)  # (old_start, old_len, new_start, new_len, lines)

    @property
    def path(self):
        return self.new_path or self.old_path


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch(None, None)
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None:
                cur = FilePatch(None, None)
                files.append(cur)
            old = line[4:].split("\t")[0]
            new = lines[i + 1][4:].split("\t")[0]
            cur.old_path = None if old == "/dev/null" else old[2:] if old.startswith("a/") else old
            cur.new_path = None if new == "/dev/null" else new[2:] if new.startswith("b/") else new
            i += 2
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            os_, ol, ns, nl = m.groups()
            os_, ns = int(os_), int(ns)
            ol = 1 if ol is None else int(ol)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            o_seen = n_seen = 0
            while i < len(lines) and (o_seen < ol or n_seen < nl):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag == " ":
                    o_seen += 1
                    n_seen += 1
                elif tag == "-":
                    o_seen += 1
                elif tag == "+":
                    n_seen += 1
                else:
                    break
                body.append((tag, l[1:]))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    return files


def apply_patch(old_text: str, fp: FilePatch):
    """Returns (new_text, removed_old_lines, inserted_points, added_new_lines).

    inserted_points are old-file positions k meaning "between old line k and k+1".
    """
    old = old_text.split("\n")
    out: list[str] = []
    pos = 0  # old lines consumed (0-based count)
    removed: set[int] = set()
    inserted: set[int] = set()
    added: set[int] = set()
    for os_, ol, ns, nl, body in fp.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start > pos:
            out.extend(old[pos:start])
            pos = start
        # a run of -/+ lines with at least one "-" replaces those lines; a run
        # of only "+" lines is a pure insertion
        run_has_minus = False
        for j, (tag, text) in enumerate(body):
            if tag == " ":
                run_has_minus = False
                out.append(old[pos] if pos < len(old) else text)
                pos += 1
            elif tag == "-":
                run_has_minus = True
                removed.add(pos + 1)
                pos += 1
            else:
                out.append(text)
                added.add(len(out))
                if not run_has_minus:
                    # look ahead: "+" lines before "-" lines in the same run
                    k = j + 1
                    while k < len(body) and body[k][0] == "+":
                        k += 1
                    if not (k < len(body) and body[k][0] == "-"):
                        inserted.add(pos)
    out.extend(old[pos:])
    return "\n".join(out), removed, inserted, added


# --------------------------------------------------------------------------- AST helpers


def _strip(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def doc_free_dump(tree):
    """AST dump with docstrings removed (to tell docstring-only edits apart)."""
    tree = ast.parse(ast.unparse(tree)) if False else tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return _strip(tree)


def dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(dotted(node.func) + "()")
    return ".".join(reversed(parts))


def bound_names(stmt) -> set[str]:
    names: set[str] = set()

    def targets(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                targets(e)
        elif isinstance(t, ast.Starred):
            targets(t.value)
        elif isinstance(t, ast.Subscript):
            targets(t.value)
        elif isinstance(t, ast.Attribute):
            # X.attr = ... at def-time mutates X
            base = t
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)

    if isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            targets(t)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets(stmt.target)
    elif isinstance(stmt, DEFS):
        names.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        targets(stmt.target)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for it in stmt.items:
            if it.optional_vars is not None:
                targets(it.optional_vars)
    elif isinstance(stmt, ast.Expr):
        # X.update(...), X.append(...), register(...)
        v = stmt.value
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
            base = v.func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)
    if not isinstance(stmt, DEFS):
        for node in ast.walk(stmt):
            if isinstance(node, ast.NamedExpr):
                targets(node.target)
    return names


@dataclass
class Ctx:
    kind: str  # "body" | "header" | "class" | "module"
    func: object = None  # innermost function node (body/header)
    stmt: object = None  # innermost statement (body) / top statement (class/module)
    cls: object = None  # innermost class (class)
    classes: tuple = ()  # enclosing class chain names for qualname


def _children_stmts(node):
    for name in ("body", "orelse", "finalbody", "handlers", "cases"):
        for ch in getattr(node, name, ()) or ():
            yield ch


def locate(tree, line: int) -> Ctx:
    """Finds the innermost execution context of an old-file line."""
    chain = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            lo = getattr(child, "lineno", None)
            if lo is None:
                # match_case / excepthandler wrappers without positions: descend
                if isinstance(child, (ast.match_case,)):
                    visit(child)
                continue
            first = lo
            if isinstance(child, DEFS) and child.decorator_list:
                first = min(first, min(d.lineno for d in child.decorator_list))
            if first <= line <= (child.end_lineno or lo):
                chain.append(child)
                visit(child)
                return

    visit(tree)

    func_i = None
    for i in range(len(chain) - 1, -1, -1):
        n = chain[i]
        if isinstance(n, FUNC):
            func_i = i
            break
    classes = tuple(n.name for n in chain if isinstance(n, ast.ClassDef))

    if func_i is not None:
        f = chain[func_i]
        if isinstance(f, ast.Lambda):
            body_start = f.body.lineno
        else:
            body_start = f.body[0].lineno
            if f.body and isinstance(f.body[0], DEFS) and f.body[0].decorator_list:
                body_start = min(body_start, min(d.lineno for d in f.body[0].decorator_list))
        if line < body_start:
            return Ctx("header", func=f, classes=classes)
        inner = None
        for n in chain[func_i + 1:]:
            if isinstance(n, ast.stmt):
                inner = n
        return Ctx("body", func=f, stmt=inner, classes=classes)

    cls = None
    top = None
    for n in chain:
        if isinstance(n, ast.ClassDef):
            cls = n
            top = None
        elif isinstance(n, ast.stmt) and top is None:
            top = n
    if cls is not None:
        body_start = cls.body[0].lineno if cls.body else cls.end_lineno
        if line < body_start or top is None:
            return Ctx("class", cls=cls, stmt=None, classes=classes)
        return Ctx("class", cls=cls, stmt=top, classes=classes)
    top = chain[0] if chain else None
    return Ctx("module", stmt=top, classes=classes)


def stmt_span(stmt) -> tuple[int, int]:
    """Lines of a statement that belong to it (not to nested bodies)."""
    lo = stmt.lineno
    if isinstance(stmt, COMPOUND):
        firsts = []
        for name in ("body", "handlers", "cases"):
            seq = getattr(stmt, name, None)
            if seq:
                firsts.append(seq[0].lineno if hasattr(seq[0], "lineno") else lo)
        if isinstance(stmt, ast.Match):
            firsts = [stmt.cases[0].pattern.lineno] if stmt.cases else []
        hi = (min(firsts) - 1) if firsts else stmt.end_lineno
        return lo, max(lo, hi)
    return lo, stmt.end_lineno or lo


def func_span(f) -> tuple[int, int]:
    lo = f.lineno
    if isinstance(f, DEFS) and f.decorator_list:
        lo = min(lo, min(d.lineno for d in f.decorator_list))
    return lo, f.end_lineno


def qualname_of(tree, node) -> str:
    """Qualified name of a class/function node (Outer.Inner)."""
    path = []

    def visit(cur, prefix):
        for child in ast.iter_child_nodes(cur):
            if child is node:
                path.extend(prefix + [child.name])
                return True
            if isinstance(child, DEFS):
                if visit(child, prefix + [child.name] + (["<locals>"] if not isinstance(child, ast.ClassDef) else [])):
                    return True
            elif isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                if visit(child, prefix):
                    return True
        return False

    visit(tree, [])
    return ".".join(path)


def find_by_qual(tree, qual: str):
    """The function/class node with this dotted qualname (first match)."""
    parts = qual.split(".")
    nodes = [tree]
    for part in parts:
        if part == "<locals>":
            continue
        found = None
        for cur in nodes:
            for child in ast.walk(cur):
                if isinstance(child, DEFS) and child.name == part and child is not cur:
                    found = child
                    break
            if found is not None:
                break
        if found is None:
            return None
        nodes = [found]
    return nodes[0] if nodes and nodes[0] is not tree else None


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _is_doc(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) and isinstance(stmt.value.value, str)


def _span(node):
    lo = node.lineno
    if isinstance(node, DEFS) and node.decorator_list:
        lo = min(lo, min(d.lineno for d in node.decorator_list))
    return lo, node.end_lineno or lo


def _touch(node, lines):
    lo, hi = _span(node)
    return any(lo <= ln <= hi for ln in lines)


def _header(node):
    if isinstance(node, ast.ClassDef):
        return _strip(ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords, body=[], decorator_list=node.decorator_list, type_params=getattr(node, "type_params", [])))
    return _strip(type(node)(name=node.name, args=node.args, body=[], decorator_list=node.decorator_list, returns=node.returns, type_comment=None, type_params=getattr(node, "type_params", [])))


def diff_kind(old_body, new_body, old_lines, new_lines):
    """Compares two statement lists, looking only at statements that overlap the
    changed lines. Returns "same", "doc" (only docstrings differ) or "code"."""
    o = [i for i, s in enumerate(old_body) if _touch(s, old_lines)]
    n = [i for i, s in enumerate(new_body) if _touch(s, new_lines)]
    if not o and not n:
        # only lines between statements changed (blank lines / comments)
        return "same" if len(old_body) == len(new_body) else "code"
    # untouched statements before the first change and after the last one are
    # textually identical on both sides, so the windows line up
    start = min(([min(o)] if o else []) + ([min(n)] if n else []))
    tail = min(
        len(old_body) - 1 - (max(o) if o else len(old_body) - 1),
        len(new_body) - 1 - (max(n) if n else len(new_body) - 1),
    )
    start = min(start, len(old_body), len(new_body))
    tail = max(0, min(tail, len(old_body) - start, len(new_body) - start))
    wo = old_body[start: len(old_body) - tail]
    wn = new_body[start: len(new_body) - tail]
    if len(wo) != len(wn):
        # a docstring added or removed at the top of the body is "doc"
        a = wo[1:] if start == 0 and wo and _is_doc(wo[0]) else wo
        b = wn[1:] if start == 0 and wn and _is_doc(wn[0]) else wn
        if len(a) == len(b) and all(_strip(x) == _strip(y) for x, y in zip(a, b)):
            return "doc"
        return "code"
    kind = "same"
    for i, (a, b) in enumerate(zip(wo, wn)):
        if type(a) is not type(b):
            return "code"
        if not _touch(a, old_lines) and not _touch(b, new_lines):
            continue
        if isinstance(a, DEFS):
            if _header(a) != _header(b):
                return "code"
            k = diff_kind(a.body, b.body, old_lines, new_lines)
        elif _is_doc(a) and _is_doc(b):
            k = "same" if a.value.value == b.value.value else ("doc" if start + i == 0 else "code")
        else:
            k = "same" if _strip(a) == _strip(b) else "code"
        if k == "code":
            return "code"
        if k == "doc":
            kind = "doc"
    return kind


def local_sig(func):
    """Names local to a function body (its own scope), and whether it yields/awaits.
    Assigning a name anywhere in a function makes it local everywhere in it."""
    names = set()
    flags = set()
    if not isinstance(func, ast.Lambda):
        a = func.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []):
            names.add(arg.arg)
    body = [func.body] if isinstance(func, ast.Lambda) else func.body
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            stack.extend(node.decorator_list)
            continue
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # walrus inside a comprehension binds in this scope
            for sub in ast.walk(node):
                if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                names.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            flags.add((type(node).__name__, tuple(node.names)))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            flags.add("yield")
        elif isinstance(node, ast.Await):
            flags.add("await")
        elif isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        stack.extend(ast.iter_child_nodes(node))
    return frozenset(names), frozenset(flags)


def code_signature(source: str, filename: str):
    """qualname -> (locals, cells, frees, flags) for every function in a module."""
    out = {}
    try:
        code = compile(source, filename, "exec", dont_inherit=True)
    except SyntaxError:
        return None
    stack = [code]
    while stack:
        c = stack.pop()
        key = (c.co_qualname, c.co_firstlineno)
        out.setdefault(c.co_qualname, []).append(
            (tuple(sorted(c.co_varnames)), tuple(sorted(c.co_cellvars)), tuple(sorted(c.co_freevars)), c.co_flags & 0x3A0)
        )
        for k in c.co_consts:
            if hasattr(k, "co_code"):
                stack.append(k)
    return out
