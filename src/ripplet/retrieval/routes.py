"""list_routes: every HTTP entry point with its handler and the access checks
it makes, so "which endpoint lets X do Y?" is one call, not a read of every
controller.

For each route (any framework the graph knows: Flask/FastAPI/Django, Express
and friends, Go, Spring, ASP.NET, PHP routers/Laravel) the handler's calls up
to `depth` steps are searched for guard-like methods (`requireRole`,
`authorize`, `can`, `ensure*`, `Gate::`, ...; or the names you pass), and the
handler body for inline role/permission tests. Write routes without any check
come first. A ready `ripplet.arch.yaml` rule closes the loop: once written,
`check_change` and `ripplet review` flag any new route that skips the guard.
"""
from __future__ import annotations

import json
import re
from collections import Counter, deque
from pathlib import Path

_WRITE = {"POST", "PUT", "PATCH", "DELETE", "*"}
# a guard ENFORCES (requireRole, authorize, canEdit, ensureOwner, Gate::allows);
# a getter that only READS the role (memberRole, getRole) is not one, and a
# lookup like canvasOrFail is not `can*`
_GUARD_NAME = re.compile(r"^(require|authori[sz]e|ensure|assert|deny|forbid|guard|must|abort_?(if|unless))(?=[A-Z_]|$)"
                         r"|^(can|cannot|allows|denies)(?=[A-Z_]|$)"
                         r"|^(check|verify)(?=[A-Z_]?(Role|Permission|Access|Owner|Admin|Auth|Can|Csrf|Token|Signature))"
                         r"|Policy|Gate")
_INLINE = re.compile(r"\b(my_role|role|roles|is_?admin|is_?owner|isOwner|isManager|permission|can\(|cannot\(|"
                     r"authorize|Gate::|abort_(if|unless)|403|Forbidden|Unauthorized|login_required)"
                     # annotations start with a non-word char, so outside the \b group
                     r"|@(PreAuthorize|PostAuthorize|Secured|RolesAllowed)\b|\[Authorize\b", re.I)
_CALL_RELS = ("calls", "dispatches", "instantiates")


def _body(roots, rel: str, a: int, b: int, cache: dict) -> list:
    if rel not in cache:
        cache[rel] = []
        for r in roots:
            p = Path(r) / rel
            if p.is_file():
                try:
                    cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
                break
    lines = cache[rel]
    if not lines:
        return []
    # decorators / attributes right above the handler count too, but only its
    # own: stop at the first line that is code (the previous method's body)
    top = max(0, a - 1)
    while top > 0 and a - top < 8:
        s = lines[top - 1].strip()
        if not s.startswith(("@", "[", "#[", "//", "*", "/*", "#")):
            break
        top -= 1
    return lines[top:b]


_WRITE_SQL = re.compile(r"\b(INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+[`\"\[]?(\w+)", re.I)
_OWN = re.compile(r"\b(user_id|owner_id|created_by|author_id|account_id)\b\s*(=|IN\b)", re.I)


def _writes(fn_ids, sql_refs: dict, roots, cache: dict) -> list:
    """`writes: UPDATE pages (shared rows)` / `... (rows of one user_id)` for the
    SQL the handler and its helpers run (from the statement text at each site)."""
    seen, out = set(), []
    for fid in fn_ids:
        for table, efile, lines in sql_refs.get(fid, ()):
            src = _body(roots, efile, 1, 10 ** 7, cache)
            for ln in lines:
                stmt = " ".join(src[max(0, ln - 2):ln + 3])
                for m in _WRITE_SQL.finditer(stmt):
                    if m.group(2).lower() != table.lower():
                        continue
                    op = m.group(1).split()[0].upper()
                    scope = "rows of one user_id/owner" if _OWN.search(stmt[m.start():]) else "shared rows"
                    key = (op, table.lower(), scope)
                    if key not in seen:
                        seen.add(key)
                        out.append(f"writes: {op} {table} ({scope})")
    return out[:3]


def _collect(store, cfg, method: str = "write", path: str = "", guards=None, depth: int = 2) -> list:
    """Routes with handler, access checks and (for unchecked ones) what they write."""
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    sup = {r[0] for r in cx.execute("SELECT edge_id FROM superseded_edges")}
    out_edges: dict = {}
    for eid, s, t, rel in cx.execute("SELECT id, source, target, relation FROM edges WHERE relation IN "
                                     "('calls','dispatches','instantiates')"):
        if eid not in sup:
            out_edges.setdefault(s, []).append(t)
    nodes = {r[0]: r[1:] for r in cx.execute(
        "SELECT id, label, qualified_name, file_path, start_line, end_line, type, attributes FROM nodes "
        "WHERE type IN ('route','function','method','class')")}
    want = {g.strip() for g in (guards or []) if g and g.strip()}
    is_guard = (lambda lab: lab in want) if want else (lambda lab: bool(_GUARD_NAME.search(lab or "")))
    handler_of = {s: t for s, t in cx.execute("SELECT source, target FROM edges WHERE relation='handles_route'")}
    sql_refs: dict = {}
    for s, table, efile, eline, attrs in cx.execute(
            "SELECT e.source, t.label, e.file_path, e.start_line, e.attributes FROM edges e "
            "JOIN nodes t ON t.id=e.target WHERE e.method='sql-link' AND t.type='table'"):
        lines = [o.get("line") for o in (json.loads(attrs or "{}").get("occurrences") or []) if o.get("line")]
        sql_refs.setdefault(s, []).append((table, efile, lines or [eline or 0]))
    mfilter = (method or "write").upper()
    pfilter = (path or "").lower()
    rows, cache = [], {}
    for rid, (label, _qn, fp, sl, _el, typ, attrs) in nodes.items():
        if typ != "route":
            continue
        a = json.loads(attrs or "{}")
        verb = (a.get("http_method") or label.split(" ", 1)[0] or "*").upper()
        rpath = a.get("full_path") or a.get("raw_path") or label.split(" ", 1)[-1]
        if mfilter == "WRITE" and verb not in _WRITE:
            continue
        if mfilter not in ("WRITE", "ALL", "") and verb != mfilter:
            continue
        if pfilter and pfilter not in rpath.lower():
            continue
        h = handler_of.get(rid)
        hn = nodes.get(h) if h else None
        checks, seen, dq = [], {h} if h else set(), deque([(h, 0)] if h else [])
        while dq:
            x, d = dq.popleft()
            if d >= depth:
                continue
            for t in out_edges.get(x, ()):
                tn = nodes.get(t)
                if tn is None or t in seen:
                    continue
                seen.add(t)
                if is_guard(tn[0]):
                    checks.append(tn[0] + (f" (via {nodes[x][0]})" if x != h else ""))
                else:
                    dq.append((t, d + 1))
        inline = []
        if hn is not None and not checks:
            body = _body(roots, hn[2], hn[3] or 0, hn[4] or hn[3] or 0, cache)
            inline = [ln.strip()[:90] for ln in body if _INLINE.search(ln)][:2]
        status = "CALLS" if checks else ("INLINE" if inline else "NONE")
        handler = f"{hn[1]} @ {hn[2]}:{hn[3]}" if hn is not None else f"(inline handler) @ {fp}:{sl}"
        if status != "CALLS":
            # what an unchecked handler changes decides if it may stay open:
            # a row of the caller's own (`... WHERE user_id = ?`) vs a shared one
            inline = inline + _writes(seen, sql_refs, roots, cache)
        rows.append((status, verb, rpath, handler, checks, inline,
                     {"qn": hn[1] if hn is not None else "", "file": hn[2] if hn is not None else fp,
                      "line": (hn[3] if hn is not None else sl) or 0}))
    return rows


def _owner(qn: str) -> str:
    return (qn or "").replace("\\", ".").rsplit(".", 1)[0]


def access_gaps(rows: list, min_guarded: int = 3, ratio: float = 0.6) -> list:
    """Write routes that skip the access check their controller's other write
    routes make, and write shared rows: the inconsistency is the evidence, so
    login/webhook controllers (nothing checks) never qualify.
    -> [{"route", "handler", "file", "line", "guarded", "total", "writes"}]."""
    by_owner: dict = {}
    for r in rows:
        if r[1] in _WRITE and r[6].get("qn"):
            by_owner.setdefault(_owner(r[6]["qn"]), []).append(r)
    out = []
    for owner, rs in by_owner.items():
        guarded = sum(1 for r in rs if r[0] in ("CALLS", "INLINE"))
        if guarded < min_guarded or guarded / len(rs) < ratio:
            continue
        for r in rs:
            shared = [w for w in r[5] if w.startswith("writes:") and "shared rows" in w]
            if r[0] == "NONE" and shared:
                out.append({"route": f"{r[1]} {r[2]}", "handler": r[6]["qn"].replace("\\", "."),
                            "file": r[6]["file"], "line": r[6]["line"], "guarded": guarded,
                            "total": len(rs), "writes": shared})
    return out


def list_routes(store, cfg, method: str = "write", path: str = "", guards=None, depth: int = 2,
                limit: int = 120) -> str:
    rows = _collect(store, cfg, method, path, guards, depth)
    mfilter = (method or "write").upper()
    want = {g.strip() for g in (guards or []) if g and g.strip()}
    gaps = access_gaps(rows)
    if not rows:
        return ("list_routes: no routes match. The graph knows routes of Flask/FastAPI/Django, Express-style "
                "JS/TS, Go, Spring, ASP.NET and PHP routers/Laravel; run `ripplet index` after adding routes.")
    order = {"NONE": 0, "INLINE": 1, "CALLS": 2}
    rows.sort(key=lambda r: (order[r[0]], r[2], r[1]))
    counts = Counter(r[0] for r in rows)
    guard_use = Counter(c.split(" ")[0] for r in rows for c in r[4])
    scope = {"WRITE": "write (POST/PUT/PATCH/DELETE)", "ALL": "all"}.get(mfilter, mfilter)
    out = [f"# list_routes: {len(rows)} {scope} routes"
           + (f" matching {path!r}" if path else "")
           + f" | no check: {counts['NONE']}, inline check only: {counts['INLINE']}, "
             f"calls a guard: {counts['CALLS']}",
           "Guards searched: " + (", ".join(sorted(want)) if want else
                                  "enforcing names: require*/authorize*/ensure*/can*/deny*, check*/verify* of a role/permission, *Policy/Gate")
           + f" (handler and {depth - 1} helper level(s) below it).",
           "Judge each NONE by what it writes: `shared rows` (a page, task, conversation every member "
           "sees) means a member-level check is missing unless the route is public by design (login, "
           "webhooks); `rows of one user_id/owner` is usually the caller's own data. Never add a "
           "shared-row writer to `except` because its name sounds personal.", ""]
    if gaps:
        out.append(f"=== LIKELY GAPS ({len(gaps)}): unchecked, writes shared rows, while its controller's "
                   "other write routes check access")
        for g in gaps:
            out.append(f"  {g['route']}  -> {g['handler']} @ {g['file']}:{g['line']} :: "
                       f"{'; '.join(g['writes'])}; {g['guarded']} of {g['total']} sibling write routes check")
        out.append("")
    for title, st in (("NO ACCESS CHECK FOUND", "NONE"), ("INLINE CHECK ONLY (role/permission test in the body)",
                                                          "INLINE"), ("CALLS A GUARD", "CALLS")):
        part = [r for r in rows if r[0] == st]
        if not part:
            continue
        out.append(f"=== {title} ({len(part)})")
        if st == "CALLS":
            # the guarded majority: counted by guard, not listed one by one
            by_guard = Counter(c.split(" ")[0] for r in part for c in r[4][:1])
            out.append("  by first guard: " + ", ".join(f"{g} x{n}" for g, n in by_guard.most_common(8)))
            out.append("  (list them with method=all/path=... or look a handler up with explain_symbol)")
            out.append("")
            continue
        shown = part
        for _s, verb, rpath, handler, checks, inline, _m in shown:
            extra = (" :: " + "; ".join(checks[:3])) if checks else ((" :: " + " | ".join(inline)) if inline else "")
            out.append(f"  {verb:6s} {rpath}  -> {handler}{extra}")
        if len(shown) < len(part):
            out.append(f"  ... {len(part) - len(shown)} more (pass path=... to narrow)")
        out.append("")
    if guard_use:
        top = [g for g, _n in guard_use.most_common(5)]
        inline_lines = [ln for r in rows if r[0] == "INLINE" for ln in r[5]]
        token = next((t for t in ("my_role", "isOwner", "isManager", "authorize", "Gate::", "can(")
                      if any(t in ln for ln in inline_lines)), "")
        public = [r[3].split(" @ ")[0].replace(chr(92), ".").rsplit(".", 2) for r in rows if r[0] == "NONE"
                  and " @ " in r[3]]
        out += ["=== Keep it enforced: ripplet.arch.yaml (checked by check_change and `ripplet review`)",
                "rules:",
                "  - id: write-routes-check-access",
                f"    must_call: [{', '.join(top)}]",
                "    for: {route: \"/**\", method: [POST, PUT, PATCH, DELETE]}   # narrow the prefix, e.g. \"/w/**\"",
                "    depth: 2"]
        if token:
            out.append(f"    inline: \"{re.escape(token)}\"            # an inline role test also satisfies the rule")
        if public:
            # never pre-filled: copying the list would silence the very gaps it
            # should catch. Uncomment only a handler that is public on purpose.
            out.append("    except: []   # uncomment ONLY handlers public on purpose (login, own settings, webhooks):")
            out += [f"    #  - {p[-2]}::{p[-1]}" for p in public[:20]]
        out.append("    message: Write endpoints must check the caller's role.")
    return "\n".join(out)
