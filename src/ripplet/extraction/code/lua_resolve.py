"""Lua chunk-scoped resolution (Phase 41 Wave 3).

Binds ``ts-lua`` placeholders: `require("a.b.c")` specs to repo files
(same-dir-relative first, then root-relative; each tried as `<p>.lua`
then `<p>/init.lua`, first hit wins — the rest of `package.path` is
unmodeled, documented), require-alias member calls to the target's
exported table methods, same-file locals and table methods. Colon
(`self`) calls, dynamic requires, metatable dispatch and ambiguous
names stay unresolved with reasons.

Conventions mirror ``go_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``lua-resolve`` v1, deterministic sites, single targets
only.
"""
from __future__ import annotations

METHOD = "lua-resolve"
VERSION = "5"  # v5: local functions exported as `M.f = f`; v4: unique distinctive colon-method names (inferred); v3: alias of a module that returns a function; stored-module exports; v2: source-root requires, table re-export aliases


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    """Returns ([], [(owner_key, Edge)], diagnostics) for one repo. See
    js_resolve.resolve_repo for the exclude/fresh contract."""
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("lua_collect")
    from ...core.models import Edge as EdgeCls
    from ...core.identifiers import file_node_id, canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf
    diag: dict = {"resolved_imports": 0, "external_imports": 0,
                  "unresolved_imports": 0, "depends_on_derived": 0,
                  "resolved_calls": 0, "ambiguous_calls": 0}

    def owner_rel(key: str) -> str:
        return key.split(":", 1)[-1]

    def mod_id(rel: str) -> str:
        return file_node_id(repo_id, rel + "#module")

    def _attrs(d: dict) -> dict:
        import json as _json
        a = d.get("attributes") or {}
        if isinstance(a, str):
            try:
                a = _json.loads(a)
            except Exception:
                return {}
        return a if isinstance(a, dict) else {}

    lua_files = sorted(f for f in file_set if f.lower().endswith(".lua"))

    # Memoized (perf, identical results): resolver loops looked nodes up per
    # call with one store query each, plus a linear scan of new_results.
    _new_nodes: dict = {}
    for _k, _res in new_results:
        for _m in getattr(_res, "nodes", []):
            _new_nodes.setdefault(getattr(_m, "id", ""), _m)
    _node_cache: dict = {}

    def _node(nid: str):
        if nid in _node_cache:
            return _node_cache[nid]
        n = store.get_node(nid)
        if n is None:
            n = _new_nodes.get(nid)
        _node_cache[nid] = n
        return n

    # ---- collect ts-lua edges
    imports: list[tuple[str, dict]] = []
    unres_calls: list[tuple[str, dict]] = []
    repo_keys = sorted(k for k in store.list_file_keys()
                       if k.startswith(repo_id + ":") and k not in exclude_keys
                       and k.split(":", 1)[-1] not in fresh_rels)
    for key in repo_keys:
        for eid in sorted(store.get_manifest_edges(key)):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?",
                                   (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if not (d["method"] or "").startswith("ts-lua"):
                continue
            if d["relation"] == "imports":
                imports.append((key, d))
            elif d["relation"] == "calls_unresolved":
                unres_calls.append((key, d))
    for key, res in new_results:
        for e in res.edges:
            if not (e.extraction_method or "").startswith("ts-lua"):
                continue
            d = {"id": e.id, "source": e.source, "target": e.target,
                 "relation": e.relation,
                 "method": e.extraction_method,
                 "provenance": list(e.provenance_ids or []),
                 "attributes": dict(e.attributes or {}),
                 "file_path": e.file_path, "start_line": e.start_line}
            if e.relation == "imports":
                imports.append((key, d))
            elif e.relation == "calls_unresolved":
                unres_calls.append((key, d))

    # ---- per-file symbol tables + module tables (stored + new)
    file_syms: dict[str, dict[str, list[str]]] = {}
    file_tables: dict[str, str] = {}  # rel -> exported table name ("M")

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method"):
            return
        file_syms.setdefault(rel, {}).setdefault(
            getattr(node, "label", ""), []).append(node.id)

    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and (n.file_path or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
            if n is not None and getattr(n, "type", "") == "module":
                # stored (unchanged) modules export too: without this an
                # incremental run lost every `alias.member` call into them
                exp = (getattr(n, "attributes", "") or {}).get("exports")
                if exp:
                    file_tables.setdefault(owner_rel(key), exp)
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if (getattr(n, "file_path", "") or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
            if getattr(n, "type", "") == "module":
                exp = (getattr(n, "attributes", "") or {}).get("exports")
                if exp:
                    file_tables[owner_rel(key)] = exp

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    def _resolve_require(from_rel: str, spec: str):
        """(target_rel or None, via). Dotted spec -> path; same-dir
        first, then root; `<p>.lua` before `<p>/init.lua`."""
        import posixpath
        parts = (spec or "").strip().split(".")
        if not parts or any(not p for p in parts):
            return None, ""
        here = _dir_of(from_rel)
        path = "/".join(parts)
        bases = []
        if here:
            bases.append(here + "/" + path)
        bases.append(path)
        for base in bases:
            for cand in (base + ".lua", base + "/init.lua"):
                cand = posixpath.normpath(cand)
                if cand in file_set:
                    via = "relative" if here and base.startswith(
                        here + "/") else "root"
                    return cand, via
        # Source roots (`src/`, `lua/`): `luarocks.util` from
        # src/luarocks/cmd/x.lua is src/luarocks/util.lua. Nearest ancestor
        # of the requiring file first, then a unique suffix match.
        d = here
        while d:
            d = _dir_of(d)
            for cand in ((d + "/" if d else "") + path + ".lua",
                         (d + "/" if d else "") + path + "/init.lua"):
                if cand in file_set:
                    return cand, "source-root"
        hits = sorted(f for f in lua_files
                      if f.endswith("/" + path + ".lua") or f.endswith("/" + path + "/init.lua"))
        if len(hits) == 1:
            return hits[0], "suffix"
        return None, ""

    extra: list[tuple[str, object]] = []

    def add_edge(owner_key: str, src: str, rel: str, tgt: str, src_row: dict,
                 etype: str, cclass: str, score, attrs: dict, site: str = ""):
        from ...graph.builder import carry_occurrences as _carry
        attrs = _carry(attrs, src_row)
        e = EdgeCls(canonical_edge_id(src, rel, tgt, owner_rel(owner_key),
                                      site),
                    src, tgt, rel, etype, cclass, score,
                    list(src_row.get("provenance", [])), METHOD, VERSION,
                    file_path=owner_rel(owner_key),
                    start_line=src_row.get("start_line") or 0,
                    attributes=attrs)
        extra.append((owner_key, e))
        return e

    prof.end("lua_collect")
    prof.begin("lua_bind")
    # (owner_key, alias) -> target rel
    bindings: dict[tuple[str, str], str] = {}
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        alias = a.get("alias", "")
        if not spec:
            continue
        tgt, via = _resolve_require(owner_rel(key), spec)
        if tgt is None:
            diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        if alias:
            bindings.setdefault((key, alias), tgt)
        add_edge(key, mod_id(owner_rel(key)), "depends_on", mod_id(tgt),
                 d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "lua-" + via},
                 site=_sf("luadep", spec, tgt))
        diag["depends_on_derived"] += 1
    prof.end("lua_bind")
    prof.begin("lua_calls")

    def _exported_methods(tgt_rel: str, member: str) -> list[str]:
        """Methods of the target's exported table named member.

        Method qnames are `<mod>.<Table>.<member>`; the table segment
        sits right before the member. Without a known exported table
        every file method counts (documented approximation).
        """
        table = file_tables.get(tgt_rel, "")
        out = []
        for nid in (file_syms.get(tgt_rel, {}) or {}).get(member, []):
            n = _node(nid)
            if n is None or getattr(n, "type", "") != "method":
                continue
            if not table:
                out.append(nid)
                continue
            segs = (getattr(n, "qualified_name", "") or "").split(".")
            if len(segs) >= 2 and segs[-2] == table:
                out.append(nid)
        if not out:
            # `local function add_chunk(..)` exported as `M.add_chunk = add_chunk`
            local = _local_exports(tgt_rel).get(member)
            for nid in (file_syms.get(tgt_rel, {}) or {}).get(local or "", []):
                n = _node(nid)
                if n is not None and getattr(n, "type", "") == "function":
                    out.append(nid)
        return sorted(set(out))

    _locexp_memo: dict = {}

    def _local_exports(rel: str) -> dict:
        """member -> local function name for `T.m = f` lines of the module's
        exported table T."""
        if rel not in _locexp_memo:
            import os
            import re
            table = file_tables.get(rel, "")
            try:
                text = open(os.path.join(repo_root, rel), encoding="utf-8", errors="ignore").read()
            except OSError:
                text = ""
            _locexp_memo[rel] = {m: f for t, m, f in re.findall(
                r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*$", text, re.M)
                if (not table or t == table) and f not in ("nil", "true", "false")}
        return _locexp_memo[rel]

    _reexp_memo: dict = {}

    def _reexports(rel: str) -> dict:
        """member -> (alias, member) for top-level `T.m = alias.m2` lines of
        the module's exported table T."""
        if rel in _reexp_memo:
            return _reexp_memo[rel]
        import os
        import re
        out: dict = {}
        table = file_tables.get(rel, "")
        try:
            text = open(os.path.join(repo_root, rel), encoding="utf-8",
                        errors="ignore").read()
        except OSError:
            text = ""
        for t, m, al, m2 in re.findall(
                r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*$",
                text, re.M):
            if (not table or t == table) and al != t:
                out.setdefault(m, (al, m2))
        _reexp_memo[rel] = out
        return out

    _by_label: dict = {}
    for _rel, _syms in file_syms.items():
        for _lab, _ids in _syms.items():
            _by_label.setdefault(_lab, set()).update(_ids)

    def _unique_method(name: str):
        """The one repo function/method called `name`, when the name is
        distinctive (>= 10 chars, two word breaks): `get_vim_item` yes,
        `get`/`new`/`setup` never."""
        if len(name) < 10 or sum(1 for ch in name if ch.isupper()) + name.count("_") < 2:
            return None
        ids = _by_label.get(name) or set()
        return next(iter(ids)) if len(ids) == 1 else None

    def _local_funcs(here: str, name: str) -> list[str]:
        return sorted(set(
            nid for nid in (file_syms.get(here, {}) or {}).get(name, [])
            if (_node(nid) is not None and
                getattr(_node(nid), "type", "") == "function")))

    def _file_methods(here: str, dotted: str) -> list[str]:
        """Same-file Table.member methods."""
        member = dotted.split(".")[-1]
        out = []
        for nid in (file_syms.get(here, {}) or {}).get(member, []):
            n = _node(nid)
            if n is None or getattr(n, "type", "") != "method":
                continue
            qn = getattr(n, "qualified_name", "") or ""
            if qn.endswith(dotted):
                out.append(nid)
        return sorted(set(out))

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            cands = _local_funcs(here, base)
            tgt = cands[0] if len(cands) == 1 else None
            if tgt is None and len(cands) > 1:
                diag["ambiguous_calls"] += 1
            if tgt is None and not cands:
                # `local git_command = require('gitsigns.git.cmd')` where the
                # module ends `return git_command`: the alias is that function
                tdir = bindings.get((key, base))
                exp = file_tables.get(tdir, "") if tdir else ""
                if exp:
                    ex = _local_funcs(tdir, exp)
                    tgt = ex[0] if len(ex) == 1 else None
        elif kind == "selector":
            base = a.get("base", "")
            member = a.get("member", "")
            if a.get("colon"):
                # `e:get_vim_item(off)` on an untyped receiver: no flow
                # analysis, but a distinctive method name defined exactly
                # once in the repo binds (inferred evidence, hypothetical confidence)
                u = _unique_method(member)
                if u:
                    diag["resolved_calls"] += 1
                    add_edge(key, d["source"], "calls", u, d, "inferred", "hypothetical", 0.6,
                             {"via": "unique-name"},
                             site=_sf("luacall", a.get("base", ""), member))
                continue
            tdir = bindings.get((key, base))
            if tdir is None and base.startswith("__require__"):
                tdir = _resolve_require(here, base[len("__require__"):])[0]
            if tdir is not None:
                cands = _exported_methods(tdir, member)
                hop, seen = (tdir, member), set()
                while not cands and hop not in seen and len(seen) < 3:
                    # `util.warning = core.warning` re-export: follow the
                    # alias into the module `core` is required from.
                    seen.add(hop)
                    al = _reexports(hop[0]).get(hop[1])
                    nxt = bindings.get((repo_id + ":" + hop[0], al[0])) if al else None
                    if nxt is None:
                        break
                    hop = (nxt, al[1])
                    cands = _exported_methods(*hop)
                tgt = cands[0] if len(cands) == 1 else None
                if tgt is None and len(cands) > 1:
                    diag["ambiguous_calls"] += 1
            else:
                cands = _file_methods(here, base + "." + member)
                tgt = cands[0] if len(cands) == 1 else None
                if tgt is None and len(cands) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "lua-resolve"},
                     site=_sf("luacall", a.get("base", ""),
                              a.get("member", "")))
    prof.end("lua_calls")
    prof.begin("lua_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "lua")
    prof.end("lua_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts (same
    contract as go/java/rust/cpp). Deterministic per file pair."""
    from ...core.identifiers import canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf2
    from ...core.models import Edge as EdgeCls
    seen: set[tuple[str, str, str]] = set()
    nodes: dict[str, object] = {}
    fresh = {getattr(m, "id", ""): m for _k, res in new_results
             for m in getattr(res, "nodes", [])}

    def _get(nid: str):
        if nid not in nodes:  # fresh-node scan per lookup was quadratic
            n = store.get_node(nid)
            nodes[nid] = n if n is not None else fresh.get(nid)
        return nodes[nid]

    for owner_key, e in list(extra):
        if e.relation not in ("calls", "inherits", "implements",
                              "instantiates"):
            continue
        s = _get(e.source)
        t = _get(e.target)
        if s is None or t is None:
            continue
        sf = getattr(s, "file_path", "") or ""
        tf = getattr(t, "file_path", "") or ""
        if not sf or not tf or sf == tf:
            continue
        if (owner_key, sf, tf) in seen:
            continue
        seen.add((owner_key, sf, tf))
        eid = canonical_edge_id(mod_id(sf), "depends_on", mod_id(tf),
                                owner_key.split(":", 1)[-1],
                                _sf2(f"{tag}filedep", sf, tf))
        extra.append((owner_key, EdgeCls(
            eid, mod_id(sf), mod_id(tf), "depends_on", "resolved",
            "resolved", 0.9, list(getattr(e, "provenance_ids", []) or []),
            method, version,
            file_path=owner_key.split(":", 1)[-1], start_line=0,
            attributes={"via": f"{tag}-file-fact"})))
        diag["depends_on_derived"] = diag.get("depends_on_derived", 0) + 1
