"""Go build constraints: which files a default build compiles.

`binding.go` (`//go:build !nomsgpack`) and `binding_nomsgpack.go`
(`//go:build nomsgpack`) define the same names in one package; only one is
compiled. Binding a call to the other one is a wrong edge, and keeping both
makes every such name ambiguous. Files whose constraint is false for a
default build (linux/amd64, cgo on, no custom tags) are not binding targets.
"""
from __future__ import annotations

import json
import re

_DEFAULT_TRUE = {"linux", "unix", "amd64", "cgo", "gc"}


def default_build(expr: str) -> bool:
    """Evaluate a `//go:build` expression for a default build."""
    if not expr:
        return True
    toks = re.findall(r"[A-Za-z0-9_.]+|&&|\|\||!|\(|\)", expr)
    out = []
    for t in toks:
        if t == "&&":
            out.append(" and ")
        elif t == "||":
            out.append(" or ")
        elif t == "!":
            out.append(" not ")
        elif t in "()":
            out.append(t)
        else:
            out.append("True" if (t in _DEFAULT_TRUE or t.startswith("go1.")) else "False")
    try:
        return bool(eval("".join(out), {"__builtins__": {}}, {}))   # tokens are only True/False/and/or/not
    except Exception:
        return True


def inactive_go_files(cx) -> set:
    """File paths whose Go build constraint is false for a default build."""
    out = set()
    for fp, attrs in cx.execute("SELECT file_path, attributes FROM nodes WHERE type='module' "
                                "AND language='go' AND attributes LIKE '%go_build%'"):
        try:
            expr = json.loads(attrs or "{}").get("go_build", "")
        except ValueError:
            continue
        if not default_build(expr):
            out.add((fp or "").replace("\\", "/"))
    return out
