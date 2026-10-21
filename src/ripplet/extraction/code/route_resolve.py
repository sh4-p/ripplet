"""Service-stage route resolution (Phase 24 Faz C).

Composes what single-file extraction cannot: `include_router` prefixes
from one file apply to `@router.*` routes defined in another, and test
URL literals link to the routes they exercise. Read-only inputs;
returns node attribute updates + owned edges for the normal publish
path (manifests keep them sweep-safe). Deterministic: sorted
iteration, canonical ids, no guessing — unknown shapes stay silent.
"""
from __future__ import annotations
import json

METHOD = "route-resolve"
VERSION = "2"  # v2: include prefixes from repo string constants


def _attrs_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _join_prefix(prefix: str | None, raw: str) -> str:
    if not prefix:
        return raw
    return prefix.rstrip("/") + raw


def _normalize_url(url: str) -> str:
    # Strip query/fragment (test literals like "/api/customers?q=x"), then
    # trailing slashes. The original literal is preserved on the edge;
    # this normalized form is only the join key.
    url = (url or "").split("?", 1)[0].split("#", 1)[0]
    if len(url) > 1:
        url = url.rstrip("/")
    return url or "/"


def _is_template_seg(seg: str) -> bool:
    return len(seg) >= 2 and seg.startswith("{") and seg.endswith("}")


def _param_match(url: str, full: str) -> bool:
    """One-to-one segment match where {param} segments are wildcards.

    Both sides may carry templates: harvested f-strings use ``{_}`` while
    routes declare ``{name}``. A template segment on either side matches any
    non-empty concrete segment; two templates match each other.
    """
    us = url.split("/")
    fs = full.split("/")
    if len(us) != len(fs):
        return False
    for u, f in zip(us, fs):
        tu, tf = _is_template_seg(u), _is_template_seg(f)
        if tu or tf:
            if not u or not f:
                return False
            continue
        if u != f:
            return False
    return True


def resolve_routes(repo_id: str, store) -> tuple[list, list, list, dict]:
    """Returns (node_updates, new_nodes, [(owner_key, Edge)], diagnostics).

    new_nodes are     (owner_key, Node) pairs for service-stage synthesized
    routes (Django `path()` today): the caller upserts them and records the
    same manifest ownership as edges, so the orphan sweep treats them like
    extraction facts (re-derivation is deterministic and idempotent).
    """
    from ...core.models import Edge, Node, Location
    from ...core.identifiers import (canonical_edge_id, canonical_symbol_id,
                                     normalize_qualified, site_fingerprint)
    diag = {"routes": 0, "prefixed": 0, "test_links": 0, "ambiguous_urls": 0,
            "django_routes": 0}
    updates: list = []
    new_nodes: list = []
    out: list = []

    routes = []
    modules: dict[str, dict] = {}
    try:
        rows = store.cx.execute(
            "SELECT * FROM nodes WHERE repository_id=? AND "
            "(type='route' OR type='module')", (repo_id,)).fetchall()
    except Exception:
        return updates, new_nodes, out, diag
    for r in rows:
        try:
            from ...storage.sqlite_store import _row_node
            n = _row_node(r)
        except Exception:
            continue
        if n.type == "route":
            routes.append(n)
        elif n.type == "module":
            modules[n.qualified_name] = n
    if not routes:
        pass  # Django-only projects have no decorator routes yet; handled below.
    diag["routes"] = len(routes)

    # ---- Django urlpatterns (module attrs) + file/module maps ----
    django_by_mod: dict[str, list] = {}
    file_of_mod: dict[str, str] = {}
    for qn, n in modules.items():
        fp = getattr(n, "file_path", "") or ""
        if fp:
            file_of_mod[qn] = fp
        attrs = _attrs_dict(getattr(n, "attributes", {}) or {})
        dp = attrs.get("django_paths")
        if isinstance(dp, list) and dp:
            django_by_mod[qn] = [e for e in dp if isinstance(e, dict)]
    if not routes and not django_by_mod:
        return updates, new_nodes, out, diag

    # ---- router variable tables per module (from module attrs) ----
    routers_by_mod: dict[str, dict] = {}
    for qn, n in modules.items():
        attrs = _attrs_dict(getattr(n, "attributes", {}) or {})
        if isinstance(attrs.get("routers"), dict):
            routers_by_mod[qn] = attrs["routers"]

    # ---- include_router registrations (this repo's files only) ----
    try:
        live_keys = {k.split(":", 1)[-1] for k in store.list_file_keys()
                     if k.split(":", 1)[0] == repo_id}
    except Exception:
        live_keys = set()
    includes: list[dict] = []
    _const_memo: dict = {}

    def _const_str(ref: str):
        """`settings.API_V1_STR` -> the unique repo string constant named
        API_V1_STR (`API_V1_STR: str = "/api/v1"`), else None."""
        name = ref.rsplit(".", 1)[-1]
        if name not in _const_memo:
            vals = set()
            for (a_,) in store.cx.execute(
                    "SELECT attributes FROM nodes WHERE type='constant' AND label=? "
                    "AND repository_id=?", (name, repo_id)):
                v = _attrs_dict(a_).get("str_value")
                if isinstance(v, str) and v.startswith("/"):
                    vals.add(v)
            _const_memo[name] = vals.pop() if len(vals) == 1 else None
        return _const_memo[name]

    try:
        irows = store.cx.execute(
            "SELECT source, target, file_path, start_line, attributes FROM edges "
            "WHERE relation IN ('calls','calls_unresolved')").fetchall()
    except Exception:
        irows = []
    src_nodes = {}
    try:
        src_nodes = store.nodes_by_ids(
            {r["source"] for r in irows
             if _attrs_dict(r["attributes"]).get("route_include")})
    except Exception:
        src_nodes = {}
    for r in irows:
        try:
            attrs = _attrs_dict(r["attributes"])
        except Exception:
            attrs = {}
        ri = attrs.get("route_include")
        if not isinstance(ri, dict) or not ri.get("router"):
            continue
        if (r["file_path"] or "") not in live_keys:
            continue
        src = src_nodes.get(r["source"])
        if src is None or getattr(src, "repository_id", "") != repo_id:
            continue
        prefix = ri.get("prefix")
        if prefix is None and ri.get("prefix_ref"):
            prefix = _const_str(ri["prefix_ref"])
        includes.append({"router": ri["router"], "prefix": prefix,
                         "file": getattr(src, "file_path", "") or "",
                         "into": ri.get("into", ""),
                         "module": (getattr(src, "qualified_name", "") or "").split("#")[0]})

    _alias_cache: dict[str, dict] = {}

    def _alias_map(file_rel: str) -> dict[str, str]:
        """Local name -> canonical dotted module for one file's imports."""
        if file_rel in _alias_cache:
            return _alias_cache[file_rel]
        out: dict[str, str] = {}
        if file_rel not in live_keys:
            _alias_cache[file_rel] = out
            return out
        try:
            erows = store.cx.execute(
                "SELECT target, attributes FROM edges WHERE file_path=? AND "
                "relation='imports'", (file_rel,)).fetchall()
        except Exception:
            _alias_cache[file_rel] = out
            return out
        for er in erows:
            a = _attrs_dict(er["attributes"])
            fm = a.get("from_module", "") or ""
            for nm in a.get("names", []) or []:
                local = a.get("alias") or nm
                if fm and nm and local:
                    out[local] = fm + "." + nm
            imp = a.get("imported", "")
            if a.get("alias") and imp and "." in imp and not fm:
                out[a["alias"]] = imp
        _alias_cache[file_rel] = out
        return out

    # ---- effective prefixes per (module, router-var) ----
    prefix_of: dict[tuple[str, str], list[str]] = {}
    parent_of: dict[tuple, list] = {}  # (mod, var) -> [(prefix, file, (into mod, into var))]

    def _module_for(base: str, file_rel: str):
        if "." in base:
            return base if base in modules else None
        amap = _alias_map(file_rel)
        return amap.get(base)


    def _real_module(m: str):
        if m in routers_by_mod or m in modules:
            return m
        hits = [x for x in routers_by_mod if x.endswith("." + m)]
        return hits[0] if len(hits) == 1 else None

    for inc in sorted(includes, key=lambda d: (d["file"], d["router"])):
        base = inc["router"].split(".")[0]
        rest = inc["router"].split(".")[1:]
        mod = _module_for(base, inc["file"])
        if mod is not None and not rest and "." in mod:
            # `from app.api.main import api_router`: alias names the router
            mod, rest = mod.rsplit(".", 1)[0], [mod.rsplit(".", 1)[1]]
        mod = _real_module(mod) if mod else None
        if mod is None or not rest:
            continue
        var = rest[-1]
        # Router var must be a declared router of that module... unless the
        # module itself is the router holder via attribute chain longer than
        # one (a.b.c): only single-var routers supported (documented).
        if len(rest) != 1:
            continue
        rmap = routers_by_mod.get(mod, {})
        if var not in rmap:
            continue
        key = (mod, var)
        entry = (inc["prefix"], inc["file"])
        if key not in prefix_of:
            prefix_of[key] = []
        if entry not in prefix_of[key]:
            prefix_of[key].append(entry)
        parent_of.setdefault(key, []).append((inc["prefix"], inc["file"],
                                              (_real_module(inc["module"]) or inc["module"], inc["into"])))

    def _chain(key, depth=0) -> list:
        """Effective prefixes of a router through nested include_router
        chains (`items` -> `api_router` -> `app` with `/api/v1`)."""
        out = []
        for prefix, f, parent in parent_of.get(key, []):
            ups = [p for p, _f in _chain(parent, depth + 1)] \
                if depth < 5 and parent in parent_of else [None]
            for up in ups:
                joined = _join_prefix(up, prefix or "") if up else prefix
                out.append((joined or None, f))
        return out

    for key in list(prefix_of):
        chained = _chain(key)
        if chained:
            prefix_of[key] = chained

    # ---- rewrite route full paths ----
    for n in sorted(routes, key=lambda x: x.id):
        attrs = _attrs_dict(getattr(n, "attributes", {}) or {})
        if attrs.get("via") in ("django_path",):
            continue  # composed at synthesis below; router tables don't apply.
        raw = attrs.get("raw_path", "")
        if not raw:
            continue
        # qualified is "module#handler#METHOD path"; the module is first.
        mod = (n.qualified_name or "").split("#")[0]
        var = attrs.get("router", "")
        cands: list[str] = []
        same = (routers_by_mod.get(mod, {}).get(var) or {}).get("prefix")
        # The router's own full path: same-file prefix composes under any
        # include prefix ("/api" + "/v1" + "/items"), never beside it. A
        # bare (prefix-less) mount serves the own path; an unmounted
        # router keeps it as its canonical description.
        sub = _join_prefix(same, raw) if same else raw
        # Insertion order is file/router sorted (stable): keep it, but sort
        # defensively with None-safe keys (None prefix = bare mount; sorting
        # None against str crashes on Py3 — E-06 regression).
        raw_incs = prefix_of.get((mod, var), [])
        incs = sorted(raw_incs, key=lambda e: ((e[0] or ""), e[1]))
        bare = any(p is None for p, _ in incs)
        # Prefixed mounts first (canonical = most specific), bare last.
        # Dual mounts serve both; the old single-mount fixtures expect the
        # prefixed path as full_path, so it stays first when present.
        for prefix, _f in incs:
            if prefix:
                cands.append(_join_prefix(prefix, sub))
        if bare or not incs:
            cands.append(sub)
        # Deduplicate preserving order.
        uniq: list[str] = []
        for c in cands:
            if c not in uniq:
                uniq.append(c)
        new_attrs = dict(attrs)
        new_attrs["full_paths"] = uniq
        new_attrs["full_path"] = uniq[0]
        new_attrs["router_resolved"] = True
        if new_attrs.get("full_paths") == attrs.get("full_paths") and \
                new_attrs.get("full_path") == attrs.get("full_path"):
            continue
        n.attributes = new_attrs
        updates.append(n)
        if uniq != [raw]:
            diag["prefixed"] += 1

    # ---- Django urlpatterns synthesis (cross-file by nature) ----
    if django_by_mod:
        from .python_extractor import _flask_rule_to_braces as _brace

        def _re_to_braces(s: str) -> str:
            """Django `re_path` groups to `{name}` params: `(?P<slug>...)`
            -> `{slug}`, any other group -> `{_}`. Leftovers keep their
            regex text (then simply match nothing -- never guessed)."""
            import re as _re
            s = _re.sub(r"\(\?P<([A-Za-z_]\w*)>[^)]*\)", r"{\1}", s)
            s = _re.sub(r"\(\?(?::|=|!)[^)]*\)", "{_}", s)
            return _re.sub(r"\([^)]*\)", "{_}", s)

        def _dj_norm(route: str, is_re: bool) -> str:
            s = (route or "").strip()
            if is_re:
                if s.startswith("^"):
                    s = s[1:]
                if s.endswith("$"):
                    s = s[:-1]
                s = _re_to_braces(s)
            s = _brace(s)
            if not s.startswith("/"):
                s = "/" + s
            return s or "/"

        def _join_chain(chain: str, route: str) -> str:
            c = (chain or "").strip("/")
            r = (route or "").strip("/")
            if c and r:
                return "/" + c + "/" + r
            if c:
                return "/" + c + "/"
            return "/" + r if r else "/"

        # Effective prefix chains per module via include() mounts.
        chains: dict[str, list[str]] = {}
        for _ in range(max(1, len(modules))):
            grown = False
            for mod, entries in django_by_mod.items():
                chains.setdefault(mod, [])
                for e in entries:
                    tgt = e.get("include")
                    if not tgt or tgt not in django_by_mod:
                        # Unknown or route-less target: nothing to compose
                        # under it (never guess across missing modules).
                        continue
                    base = list(chains.get(mod) or [""])
                    if not base:
                        base = [""]
                    for c in base:
                        nxt = _join_chain(c, _dj_norm(e.get("route") or "", False))
                        if nxt not in chains.setdefault(tgt, []):
                            chains[tgt].append(nxt)
                            grown = True
            if not grown:
                break
        # Candidate views: repo-local functions/methods/classes by bare label.
        # (Classes cover `View.as_view()` receivers; the chain stays
        # traversable even though a class is not a function.)
        view_cands: dict[str, list] = {}
        try:
            vrows = store.cx.execute(
                "SELECT id, label, qualified_name, file_path, attributes FROM nodes "
                "WHERE repository_id=? AND type IN ('function','method','class')",
                (repo_id,)).fetchall()
        except Exception:
            vrows = []
        for vr in vrows:
            try:
                view_cands.setdefault(vr["label"] or "", []).append(vr)
            except Exception:
                continue
        for mod in sorted(django_by_mod):
            entries = django_by_mod[mod]
            mod_file = file_of_mod.get(mod) or ""
            owner = repo_id + ":" + mod_file if mod_file else repo_id + ":"
            try:
                proj_id = getattr(modules.get(mod), "project_id", "") or ""
            except Exception:
                proj_id = ""
            base_chains = list(chains.get(mod) or [])
            if not base_chains:
                base_chains = [""]
            for e in entries:
                if e.get("include") or not e.get("view"):
                    continue
                fulls = []
                for c in base_chains:
                    fulls.append(_normalize_url(_join_chain(
                        c, _dj_norm(e.get("route") or "",
                                   bool(e.get("is_re"))))))
                fulls = list(dict.fromkeys(fulls))
                if not fulls:
                    continue
                vshort = str(e["view"]).split(".")[-1]
                cands = view_cands.get(vshort, [])
                if len(cands) != 1:
                    continue  # zero or ambiguous: never guess
                vr = cands[0]
                hqn = vr["qualified_name"] or vshort
                for full in fulls:
                    q = f"{hqn}#GET {full}"
                    rid = canonical_symbol_id(
                        repo_id, "python", "route", q, "")
                    attrs = {"http_method": "GET", "raw_path": full,
                             "router": "django", "router_known": True,
                             "full_path": full, "full_paths": [full],
                             "parameterized": ("{" in full and "}" in full),
                             "via": "django_path",
                             # Django serves every method on a path; the
                             # method slot is a chain placeholder, never a
                             # served-methods claim.
                             "method_note": "django serves all methods; "
                                            "method not modeled"}
                    loc = Location(1, 0, 1, 0)
                    new_nodes.append((owner, Node(
                        rid, "route", f"GET {full}"[:200],
                        normalize_qualified(q), proj_id, repo_id,
                        mod_file, loc, "python", "",
                        attrs, [], schema_version=2)))
                    eid = canonical_edge_id(
                        rid, "handles_route", vr["id"], mod_file)
                    out.append((owner, Edge(
                        eid, rid, vr["id"], "handles_route", "structural",
                        "deterministic", 1.0, [], METHOD, VERSION,
                        file_path=mod_file, start_line=1,
                        attributes={"via": "django_path"})))
                    diag["django_routes"] += 1
        # Synthesized nodes join the test-link lookup below (in-memory; the
        # store still holds pre-composition attributes at this point).
        for _owner, _nn in new_nodes:
            routes.append(_nn)
        diag["routes"] = len(routes)

    # ---- test URL literals -> routes ----
    url_tests: list[tuple] = []
    try:
        trows = store.cx.execute(
            "SELECT id, file_path, attributes FROM nodes WHERE "
            "repository_id=?", (repo_id,)).fetchall()
    except Exception:
        trows = []
    for r in trows:
        a = _attrs_dict(r["attributes"])
        urls = a.get("http_calls")
        if isinstance(urls, list) and urls:
            url_tests.append((r["id"], r["file_path"] or "", urls))
    # Route lookup by normalized full path. Uses the in-memory route
    # nodes (with just-computed full_paths), NOT a DB re-read: the store
    # still holds pre-composition attributes at this point.
    by_path: dict[str, list] = {}
    for n in sorted(routes, key=lambda x: x.id):
        a = _attrs_dict(getattr(n, "attributes", {}) or {})
        fps = a.get("full_paths") or ([a["full_path"]] if a.get("full_path") else [])
        for fp in fps:
            by_path.setdefault(_normalize_url(fp), []).append(n.id)
    for tid, tfile, urls in sorted(url_tests):
        if not tfile:
            continue
        owner = repo_id + ":" + tfile
        for url in sorted(set(urls)):
            norm = _normalize_url(url)
            hits = list(by_path.get(norm, []))
            parameterized = False
            if not hits:
                for fp, rids in by_path.items():
                    if _param_match(norm, fp):
                        hits.extend(rids)
                        parameterized = True
                hits = sorted(set(hits))
            if len(hits) > 1:
                diag["ambiguous_urls"] += 1
            for rid in hits:
                eid = canonical_edge_id(
                    tid, "uses", rid, tfile,
                    site_fingerprint("httpcall", url, rid))
                out.append((owner, Edge(
                    eid, tid, rid, "uses", "resolved", "resolved", 0.9, [],
                    METHOD, VERSION, file_path=tfile, start_line=0,
                    attributes={"kind": "http_call", "url": url,
                                "parameterized": parameterized})))
                diag["test_links"] += 1
    return updates, new_nodes, out, diag
