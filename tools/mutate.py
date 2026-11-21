"""Generate synthetic patches against the pinned sqlglot, for local validation.

usage: python mutate.py <sqlglot_repo> <out_dir> <count> [seed]

Kinds (loosely like the sample set): operator / constant flips in code,
comment insertions, fixture edits, test-file edits, and multi-file mixes.
Only used offline to measure the selector's recall and tune its policy.
"""

import difflib
import json
import os
import random
import re
import sys

COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation",
    "# Keep in sync with the generator",
    "# TODO: revisit",
]

FLIPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "),
    (r" > ", " >= "), (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "),
    (r" is not ", " is "), (r" is None", " == None"), (r" is ", " == "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""),
    (r"\[0\]", "[1]"), (r"\[1\]", "[0]"), (r"\[-1\]", "[0]"), (r" \+ 1\b", " - 1"),
    (r" - 1\b", " + 1"), (r"\bcopy=False", "copy=True"), (r"\bcopy=True", "copy=False"),
    (r"\bin\b", "not in"), (r"\breturn None\b", "return 0"), (r"\bif ", "if not "),
    (r"\b(\d+)\b", None),
]

FIXTURE_EDITS = [" 42", " 0", " NULL", "x ", ".", ")"]


def py_files(repo, top):
    out = []
    for d, ds, fs in os.walk(os.path.join(repo, top)):
        ds.sort()
        for f in sorted(fs):
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), repo).replace(os.sep, "/"))
    return out


def fixture_files(repo):
    out = []
    for d, ds, fs in os.walk(os.path.join(repo, "tests", "fixtures")):
        ds.sort()
        for f in sorted(fs):
            if f.endswith(".sql"):
                out.append(os.path.relpath(os.path.join(d, f), repo).replace(os.sep, "/"))
    return out


def diff(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


def code_flip(rng, repo, files):
    for _ in range(200):
        path = rng.choice(files)
        src = open(os.path.join(repo, path), encoding="utf-8").read()
        lines = src.split("\n")
        idx = rng.randrange(len(lines))
        line = lines[idx]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith('"""') or s.startswith("'") or s.startswith('"'):
            continue
        rng.shuffle(FLIPS)
        for pat, rep in FLIPS:
            m = list(re.finditer(pat, line))
            if not m:
                continue
            mm = rng.choice(m)
            if rep is None:
                n = int(mm.group(1))
                rep = str(n + rng.choice([1, -1]) if n > 0 else 1)
            new_line = line[: mm.start()] + rep + line[mm.end():]
            if new_line == line:
                continue
            new = "\n".join(lines[:idx] + [new_line] + lines[idx + 1:])
            try:
                compile(new, path, "exec")
            except SyntaxError:
                continue
            return path, src, new
    return None


def comment_insert(rng, repo, files):
    path = rng.choice(files)
    src = open(os.path.join(repo, path), encoding="utf-8").read()
    lines = src.split("\n")
    for _ in range(100):
        idx = rng.randrange(1, len(lines))
        nxt = lines[idx]
        if not nxt.strip():
            continue
        indent = nxt[: len(nxt) - len(nxt.lstrip())]
        new = "\n".join(lines[:idx] + [indent + rng.choice(COMMENTS)] + lines[idx:])
        try:
            compile(new, path, "exec")
        except SyntaxError:
            continue
        return path, src, new
    return None


def fixture_edit(rng, repo, files):
    path = rng.choice(files)
    src = open(os.path.join(repo, path), encoding="utf-8").read()
    lines = src.split("\n")
    for _ in range(100):
        idx = rng.randrange(len(lines))
        line = lines[idx]
        if not line.strip() or line.startswith("#") or line.startswith("--"):
            continue
        words = [m.start() for m in re.finditer(r" ", line)]
        if not words:
            continue
        pos = rng.choice(words)
        new_line = line[:pos] + rng.choice(FIXTURE_EDITS) + line[pos:]
        return path, src, "\n".join(lines[:idx] + [new_line] + lines[idx + 1:])
    return None


def main():
    repo, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    rng = random.Random(seed)
    src_files = py_files(repo, "sqlglot")
    test_files = [f for f in py_files(repo, "tests") if os.path.basename(f).startswith("test_")]
    fixtures = fixture_files(repo)
    os.makedirs(out, exist_ok=True)
    meta = {}
    kinds = ["code"] * 12 + ["comment"] * 3 + ["fixture"] * 4 + ["multi"] * 2 + ["test"] * 2
    made = 0
    while made < count:
        kind = rng.choice(kinds)
        parts = []
        if kind == "code":
            parts = [code_flip(rng, repo, src_files)]
        elif kind == "comment":
            parts = [comment_insert(rng, repo, src_files)]
        elif kind == "fixture":
            parts = [fixture_edit(rng, repo, fixtures)]
        elif kind == "test":
            parts = [code_flip(rng, repo, test_files)]
        else:
            gens = [lambda: code_flip(rng, repo, src_files), lambda: comment_insert(rng, repo, src_files),
                    lambda: fixture_edit(rng, repo, fixtures)]
            parts = [rng.choice(gens)() for _ in range(rng.choice([2, 3]))]
        if any(p is None for p in parts):
            continue
        paths = [p[0] for p in parts]
        if len(set(paths)) != len(paths):
            continue
        cid = f"m{seed:02d}{made:03d}"
        text = "".join(diff(*p) for p in sorted(parts))
        open(os.path.join(out, cid + ".patch"), "w", encoding="utf-8").write(text)
        meta[cid] = kind
        made += 1
    json.dump(meta, open(os.path.join(out, "kinds.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
