"""Implicit dispatch edges (all languages).

Static call binding stops where the runtime picks the target:

  * a class is used (instantiated, entered with `with`, iterated, called),
    and its constructor / protocol methods run without a call site;
  * a call bound to `Base.m` runs `Sub.m` when the receiver is a subclass.

This pass adds `dispatches` edges so that "what depends on X" and "which
tests reach X" follow those paths:

  class C   -> C.__init__ / C.<init> / C.C / __enter__ / __exit__ / __call__ /
              __iter__ / __next__ / __getitem__ / ... (implicitly invoked)
  Base.m    -> Sub.m  for every repo subclass (resolved inherits/implements,
              transitively) that defines a member named m

Edges are recomputed from the published tables on every index run.
"""
from __future__ import annotations

from collections import defaultdict

from ..core.identifiers import canonical_edge_id
from ..core.models import Edge

METHOD = "dispatch-impl"
VERSION = "1"
_IMPLICIT = {"__init__", "__new__", "__post_init__", "__enter__", "__exit__", "__aenter__", "__aexit__",
             "__call__", "__iter__", "__next__", "__aiter__", "__anext__", "__getitem__", "__setitem__",
             "__len__", "__contains__", "__eq__", "__hash__", "__str__", "__repr__", "__bool__",
             "__getattr__", "__get__", "__set__", "__del__", "<init>", "<clinit>", "constructor", "init",
             "Dispose", "DisposeAsync", "close", "Close",
             # framework entry points: called by the host once the type is registered
             "Invoke", "InvokeAsync", "Handle", "HandleAsync", "OnActionExecuting", "OnActionExecuted",
             "OnActionExecutionAsync", "OnResultExecuting", "OnResultExecuted", "OnException", "OnExceptionAsync",
             "OnAuthorization", "OnAuthorizationAsync", "OnModelCreating", "OnConfiguring", "Configure",
             "ConfigureServices", "ExecuteAsync", "StartAsync", "StopAsync", "BindModelAsync",
             "HandleRequirementAsync", "Process", "ProcessAsync", "Consume"}
_TYPES = ("class", "struct", "record", "object", "interface", "trait", "enum", "test")
_MEMBERS = ("method", "function", "test")


def link_dispatch_impl(store) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    ntype, label, nfile, nline, nrepo = {}, {}, {}, {}, {}
    for nid, t, lab, fp, sl, rid in cx.execute(
            "SELECT id, type, label, file_path, start_line, repository_id FROM nodes"):
        ntype[nid], label[nid], nfile[nid], nline[nid], nrepo[nid] = t, lab or "", fp or "", sl or 0, rid
    qn_of, by_qn, by_label = {}, {}, defaultdict(list)
    for nid, qn in cx.execute("SELECT id, qualified_name FROM nodes"):
        qn_of[nid] = qn or ""
        if ntype.get(nid) in _TYPES:
            by_qn[(nrepo[nid], qn or "")] = nid
            by_label[(nrepo[nid], label[nid])].append(nid)
    members = defaultdict(dict)   # class id -> {name: [member ids]}
    for s, t in cx.execute("SELECT source, target FROM edges WHERE relation IN ('defines','contains')"):
        if ntype.get(s) in _TYPES and ntype.get(t) in _MEMBERS:
            members[s].setdefault(label[t], []).append(t)
    # languages whose extractor ties members to the class by name only (Python)
    for nid, t in ntype.items():
        if t in _MEMBERS and "." in qn_of.get(nid, ""):
            cls = by_qn.get((nrepo[nid], qn_of[nid].rsplit(".", 1)[0]))
            if cls and nid not in members[cls].get(label[nid], []):
                members[cls].setdefault(label[nid], []).append(nid)
    bases = defaultdict(set)      # class -> direct repo bases
    for s, t in cx.execute("SELECT source, target FROM edges WHERE relation IN ('inherits','implements')"):
        if ntype.get(s) not in _TYPES or s == t:
            continue
        if ntype.get(t) in _TYPES:
            bases[s].add(t)
        elif t.startswith("ext:"):
            # unresolved base spelling (`ParamType[uuid.UUID]`, `click.Group`):
            # the repo's only type of that name
            sp = t.split(":", 2)[-1].split("[", 1)[0].split("<", 1)[0].rsplit(".", 1)[-1].strip()
            cands = by_label.get((nrepo.get(s), sp), [])
            if len(cands) == 1 and cands[0] != s:
                bases[s].add(cands[0])
    new = []
    seen = set()

    def add(src, tgt, why):
        if (src, tgt) in seen or src == tgt:
            return
        seen.add((src, tgt))
        fp = nfile.get(tgt) or nfile.get(src) or ""
        new.append((Edge(canonical_edge_id(src, "dispatches", tgt, fp, why), src, tgt, "dispatches", "inferred",
                         "inferred", 0.7, [], METHOD, VERSION, file_path=fp, start_line=nline.get(tgt, 0),
                         attributes={"via": why}), f"{nrepo.get(tgt)}:{fp}"))

    # nested functions run when their enclosing function runs (returned
    # closures, decorators' inner wrappers, local helpers)
    fn_by_qn = {(nrepo[n], qn_of[n]): n for n, t in ntype.items() if t in _MEMBERS}
    for nid, t in ntype.items():
        if t in _MEMBERS and "." in qn_of.get(nid, ""):
            parent = fn_by_qn.get((nrepo[nid], qn_of[nid].rsplit(".", 1)[0]))
            if parent:
                add(parent, nid, "nested")

    for cls, by_name in members.items():
        cname = label.get(cls, "")
        for name, ids in by_name.items():
            if name in _IMPLICIT or name == cname:   # Java/C#/Kotlin constructors carry the class name
                for m in ids:
                    add(cls, m, "implicit")
        # overrides: every ancestor's same-named member may dispatch here
        anc, stack = set(), list(bases.get(cls, ()))
        while stack and len(anc) < 64:
            b = stack.pop()
            if b in anc:
                continue
            anc.add(b)
            stack.extend(bases.get(b, ()))
        for b in anc:
            for name, ids in by_name.items():
                if name in _IMPLICIT:
                    continue
                for bm in members.get(b, {}).get(name, ()):
                    for m in ids:
                        add(bm, m, "override")
    if new:
        store.upsert_edges([e for e, _ in new])
        for e, fk in new:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    return {"edges": len(new)}
