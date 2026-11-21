"""Write unusual hand-made patches to check the analyzer's handling of them.

usage: python odd_patches.py <repo> <out_dir>
"""

import difflib
import os
import sys


def diff(path, old, new, old_path=None, new_path=None):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    op = old_path or path
    np_ = new_path or path
    body = "".join(difflib.unified_diff(a, b, f"a/{op}" if old else "/dev/null", f"b/{np_}" if new else "/dev/null", n=3))
    return f"diff --git a/{op} b/{np_}\n" + body


def sub(repo, path, old, new, count=1):
    src = open(os.path.join(repo, path), encoding="utf-8").read()
    assert old in src, (path, old)
    return diff(path, src, src.replace(old, new, count))


def main():
    repo, out = sys.argv[1:3]
    os.makedirs(out, exist_ok=True)
    p = {}
    p["syntax"] = sub(repo, "sqlglot/dialects/dremio.py", "class Dremio(Dialect):", "class Dremio(Dialect)")
    p["docstring"] = sub(repo, "sqlglot/optimizer/simplify.py", '"""', '"""Note: ', 1)
    p["import"] = sub(repo, "sqlglot/dialects/dremio.py", "from sqlglot import tokens", "from sqlglot import tokens, parser")
    p["testbody"] = sub(repo, "tests/dialects/test_dremio.py", "def test_", "def test_", 1)
    src = open(os.path.join(repo, "tests/dialects/test_dremio.py"), encoding="utf-8").read()
    i = src.index("self.validate_identity(")
    p["testline"] = diff("tests/dialects/test_dremio.py", src, src[:i] + "self.validate_identity('SELECT 1')\n        " + src[i:])
    p["testclassattr"] = sub(repo, "tests/dialects/test_dremio.py", 'dialect = "dremio"', 'dialect = "drill"')
    p["helper"] = sub(repo, "tests/helpers.py", 'FIXTURES_DIR = os.path.join(FILE_DIR, "fixtures")', 'FIXTURES_DIR = os.path.join(FILE_DIR, "fixture")')
    p["newfile"] = diff("sqlglot/dialects/newdialect.py", "", "X = 1\n")
    p["delete"] = diff("sqlglot/dialects/dremio.py", open(os.path.join(repo, "sqlglot/dialects/dremio.py")).read(), "")
    p["readme"] = diff("README.md", open(os.path.join(repo, "README.md")).read(), open(os.path.join(repo, "README.md")).read() + "\nmore\n")
    p["initpy"] = sub(repo, "sqlglot/__init__.py", "logger = logging.getLogger", "logger = logging.getLogger", 1) if "logger = logging.getLogger" in open(os.path.join(repo, "sqlglot/__init__.py")).read() else ""
    p["decorator"] = sub(repo, "sqlglot/expressions/core.py", "    @property\n", "    @property  \n", 1)
    p["moduleconst"] = sub(repo, "sqlglot/optimizer/simplify.py", "FINAL = \"final\"", "FINAL = \"finals\"") if 'FINAL = "final"' in open(os.path.join(repo, "sqlglot/optimizer/simplify.py")).read() else ""
    p["jsonfix"] = ""
    p["enumdialects"] = sub(repo, "sqlglot/dialects/dialect.py", 'TSQL = "tsql"', 'TSQL = "tsqll"') if 'TSQL = "tsql"' in open(os.path.join(repo, "sqlglot/dialects/dialect.py")).read() else ""
    dsrc = open(os.path.join(repo, "sqlglot/expressions/datatypes.py")).read()
    p["enumdtype"] = sub(repo, "sqlglot/expressions/datatypes.py", "    TEXT = auto()", "    TEXT = auto()\n    TEXTX = auto()") if "    TEXT = auto()" in dsrc else ""
    for name, text in p.items():
        if text:
            open(os.path.join(out, name + ".patch"), "w", encoding="utf-8").write(text)
    print("wrote", [k for k, v in p.items() if v])


if __name__ == "__main__":
    main()
