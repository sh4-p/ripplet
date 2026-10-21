"""Type-argument references (C#, Java, Kotlin, TypeScript).

Frameworks are wired by naming a type, not by calling it:

  services.AddScoped(typeof(IPipelineBehavior<,>), typeof(ValidationPipelineBehavior<,>))
  app.UseMiddleware<ErrorHandlingMiddleware>()
  class ApiFactory : WebApplicationFactory<Program>
  options.Filters.Add(typeof(ValidatorActionFilter))

The framework then instantiates the type and calls its entry points. This
pass adds a `references` edge from the code that names the type to the
repo type (a `Program` with top-level statements has no class: its file's
module node stands in), so dependents and tests flow through registrations.
Recomputed from the published tables on every index run.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..core.identifiers import canonical_edge_id
from ..core.models import Edge

METHOD = "type-refs"
VERSION = "1"
_EXT = {".cs", ".java", ".kt", ".ts", ".tsx"}
_TYPEOF = re.compile(r"\btypeof\s*\(\s*([A-Za-z_][\w.]*)")
_GENERIC = re.compile(r"[A-Za-z_]\w*\s*<\s*([A-Za-z_][\w.\s,<>?\[\]]*?)\s*>\s*(?=[({:]|$|\s*where\b|\s*,)", re.M)
_CLASSOF = re.compile(r"\b([A-Z]\w*)(?:::class(?:\.java)?|\.class)\b")
_TYPES = ("class", "struct", "record", "interface", "object", "enum", "trait")


def link_type_refs(store, repo_roots: dict) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    by_label = defaultdict(list)
    spans = defaultdict(list)          # file -> [(start, end, id, type)]
    module_of = {}
    qn_of = {}
    for nid, t, lab, qn, fp, sl, el, rid in cx.execute(
            "SELECT id, type, label, qualified_name, file_path, start_line, end_line, repository_id FROM nodes"):
        if not fp or Path(fp).suffix not in _EXT:
            continue
        qn_of[nid] = qn or ""
        if t in _TYPES:
            by_label[(rid, lab)].append(nid)
        if t in _TYPES or t in ("method", "function", "test"):
            spans[(rid, fp)].append((sl or 0, el or sl or 0, nid, t))
        elif t == "module":
            module_of[(rid, fp)] = nid
    roots = dict(repo_roots or {})
    new, seen = [], set()
    for (rid, fp) in set(spans) | set(module_of):
        sp = spans.get((rid, fp), [])   # top-level statement files (Program.cs) have none
        root = roots.get(rid)
        if not root:
            continue
        try:
            lines = (Path(root) / fp).read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        sp.sort(key=lambda x: (x[0], -x[1]))
        for i, text in enumerate(lines, 1):
            if "<" not in text and "typeof" not in text and "class" not in text:
                continue
            names = set(_TYPEOF.findall(text)) | set(_CLASSOF.findall(text))
            for m in _GENERIC.finditer(text):
                names.update(x.strip() for x in re.split(r"[,<>\s?\[\]]+", m.group(1)) if x.strip())
            if not names:
                continue
            # innermost enclosing member, else the type, else the module
            owner = None
            for a, b, nid, t in sp:
                if a <= i <= b:
                    owner = nid
            owner = owner or module_of.get((rid, fp))
            if owner is None:
                continue
            for name in names:
                short = name.rsplit(".", 1)[-1]
                cands = by_label.get((rid, short), [])
                if len(cands) > 1 and "." in name:
                    cands = [c for c in cands if qn_of.get(c, "").endswith(name)]
                if len(cands) > 1:
                    here = set(qn_of.get(owner, "").split("."))
                    cands = sorted(cands, key=lambda c: -len(here & set(qn_of.get(c, "").split("."))))[:1]
                tgt = cands[0] if cands else None
                if tgt is None and short == "Program":
                    # top-level statements: no Program class, the file is the program
                    tgt = next((m for (r_, f_), m in module_of.items()
                                if r_ == rid and f_.rsplit("/", 1)[-1] == "Program.cs"), None)
                if not tgt or tgt == owner or (owner, tgt) in seen:
                    continue
                seen.add((owner, tgt))
                new.append((Edge(canonical_edge_id(owner, "references", tgt, fp, "type-arg"), owner, tgt,
                                 "references", "resolved", "resolved", 0.8, [], METHOD, VERSION, file_path=fp,
                                 start_line=i, attributes={"via": "type-argument", "name": name}), f"{rid}:{fp}"))
    if new:
        store.upsert_edges([e for e, _ in new])
        for e, fk in new:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    return {"edges": len(new)}
