"""Mediator / CQRS dispatch links.

`mediator.Send(new Create.Command(..))` reaches `Create.Handler.Handle` only
through a container at run time, so the static graph stopped at the
controller and every handler looked untested and unused. This pass adds
one `calls` edge from the sending method to the handler method:

  request R   a repo class/record/struct named *Command|*Query|*Request|
              *Notification|*Event|*Message
  handler H   a class whose declaration names R as the generic argument of a
              handler interface (`IRequestHandler<Command, ArticleEnvelope>`),
              else the single sibling `*Handler` declared next to R
  sender M    a method that instantiates R and dispatches (`Send(`,
              `Publish(`, `Dispatch(`, `Execute(` in its body)

Edges are `inferred` (container wiring is not visible), recomputed from the
published tables on every index run, never touching extractor facts.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..core.identifiers import canonical_edge_id
from ..core.models import Edge

METHOD = "dispatch-link"
VERSION = "1"
_REQ = re.compile(r"(Command|Query|Request|Notification|Event|Message)$")
_SEND = re.compile(r"\b(Send|SendAsync|Publish|PublishAsync|Dispatch|DispatchAsync|Execute|ExecuteAsync)\s*[(<]")
_HANDLE = ("Handle", "HandleAsync", "Consume", "Execute", "ExecuteAsync", "Process")


def _read(roots, rel, cache):
    if rel not in cache:
        cache[rel] = None
        for r in roots:
            p = Path(r) / rel
            if p.is_file():
                try:
                    cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
                break
    return cache[rel]


def link_dispatch(store, repo_roots: dict) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    stats = {"requests": 0, "handled": 0, "links": 0}
    roots = list(repo_roots.values())
    cache: dict = {}
    nodes = {}
    for nid, typ, label, qn, fp, sl, el, rid in cx.execute(
            "SELECT id, type, label, qualified_name, file_path, start_line, end_line, repository_id FROM nodes "
            "WHERE type IN ('class','record','struct','method','function','interface')"):
        nodes[nid] = (typ, label or "", qn or "", fp or "", sl or 0, el or sl or 0, rid)
    by_parent = defaultdict(list)   # parent qn -> class ids
    methods_of = defaultdict(list)  # class qn -> method ids
    for nid, (typ, label, qn, fp, sl, el, rid) in nodes.items():
        parent = qn.rsplit(".", 1)[0] if "." in qn else ""
        if typ in ("class", "record", "struct"):
            by_parent[(rid, parent)].append(nid)
        elif typ == "method":
            methods_of[(rid, parent)].append(nid)
    requests = [nid for nid, v in nodes.items() if v[0] in ("class", "record", "struct") and _REQ.search(v[1])]
    stats["requests"] = len(requests)
    handler_of = {}
    for r in requests:
        typ, label, qn, fp, sl, el, rid = nodes[r]
        parent = qn.rsplit(".", 1)[0] if "." in qn else ""
        found = []
        # 1. a handler class whose declaration names this request as a generic argument
        pat = re.compile(r"Handler\s*<\s*(?:[\w.]*\.)?" + re.escape(label) + r"\b")
        for h in by_parent.get((rid, parent), []) + [x for (rr, _p), xs in by_parent.items() if rr == rid
                                                       for x in xs if nodes[x][3] == fp]:
            ht = nodes[h]
            lines = _read(roots, ht[3], cache)
            if not lines or not ht[1].endswith("Handler"):
                continue
            head = " ".join(lines[max(0, ht[4] - 1):min(len(lines), ht[4] + 3)])
            if pat.search(head):
                found.append(h)
        # 2. the single `*Handler` sibling next to the request
        if not found:
            sib = [h for h in by_parent.get((rid, parent), []) if nodes[h][1].endswith("Handler")]
            if len(sib) == 1 and _REQ.search(label):
                found = sib
        found = list(dict.fromkeys(found))
        if len(found) != 1:
            continue
        hq = nodes[found[0]][2]
        hm = [m for m in methods_of.get((rid, hq), []) if nodes[m][1] in _HANDLE]
        if len(hm) == 1:
            handler_of[r] = hm[0]
    stats["handled"] = len(handler_of)
    if not handler_of:
        return stats
    # senders: methods that dispatch, and the requests their text names
    # (`new Edit.Command(..)`, a `Create.Command command` parameter, `Send<Query>`)
    by_label = defaultdict(list)
    for r in handler_of:
        by_label[(nodes[r][6], nodes[r][1])].append(r)
    labels = sorted({lab for (_rid, lab) in by_label}, key=len, reverse=True)
    ref = re.compile(r"((?:[A-Za-z_][A-Za-z0-9_]*\.)*)(" + "|".join(map(re.escape, labels)) + r")\b")

    def resolve(rid, qual, label, sender_qn):
        cands = [r for r in by_label.get((rid, label), [])
                 if not qual or nodes[r][2].endswith("." + qual + label)]
        if len(cands) <= 1:
            return cands[0] if cands else None
        # same-named requests in several features: the one nearest the sender
        def shared(r):
            a, b = nodes[r][2].split("."), sender_qn.split(".")
            k = 0
            while k < min(len(a), len(b)) and a[k] == b[k]:
                k += 1
            return k
        ranked = sorted(cands, key=shared, reverse=True)
        return ranked[0] if shared(ranked[0]) > shared(ranked[1]) else None

    new = []
    seen = set()
    for src, (typ, label, qn, fp, sl, el, rid) in nodes.items():
        if typ not in ("method", "function") or not fp:
            continue
        lines = _read(roots, fp, cache)
        if not lines:
            continue
        body = "\n".join(lines[max(0, sl - 1):max(sl, el)])
        if not _SEND.search(body):
            continue
        for m in ref.finditer(body):
            req = resolve(rid, m.group(1), m.group(2), qn)
            if req is None or req not in handler_of:
                continue
            h = handler_of[req]
            if (src, h) in seen or src == h:
                continue
            seen.add((src, h))
            ln = sl + body.count("\n", 0, m.start())
            e = Edge(canonical_edge_id(src, "calls", h, fp, "dispatch:" + nodes[req][2]),
                     src, h, "calls", "inferred", "inferred", 0.7, [], METHOD, VERSION,
                     file_path=fp, start_line=ln,
                     attributes={"via": "mediator", "request": nodes[req][2]})
            new.append((e, f"{rid}:{fp}"))
    if new:
        store.upsert_edges([e for e, _ in new])
        for e, fk in new:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    stats["links"] = len(new)
    return stats
