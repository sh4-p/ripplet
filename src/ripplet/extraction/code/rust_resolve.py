"""Rust module-scoped resolution (Phase 39 Wave 1).

Binds ``ts-rust`` placeholders: `mod` declarations to child files
(`foo.rs`, `foo/mod.rs`), `use` paths (crate/super/self roots, renames,
single targets; globs and extern crates stay unresolved-namespace with
reasons), same-file and use-bound calls. Receiver calls (`e.start()`)
and `Type::assoc` through type names stay `calls_unresolved`
(documented: needs type flow, not name matching). `impl Trait for
Type` minted no heritage upstream and none is added here.

Conventions mirror ``go_resolve``: repo-scoped inputs, manifest-owned
outputs, method ``rust-resolve`` v1, deterministic sites, single
targets only.
"""
from __future__ import annotations
import posixpath

METHOD = "rust-resolve"
VERSION = "11"  # v11: bare calls never bind a same-named method; v10: self::/super:: uses name items in module files; v9: workspace member crates (Cargo.toml names) in paths and uses, crate re-exports; v8: integration-test crates (tests/*.rs) resolve crate:: from their own dir; v7: inline `mod m { }` paths; v6: path/use calls prefer free fns, `use .. as alias` followed; v5: `pub use` re-exports followed, child modules of non-root files; v4: distinctive unique method names on receiver calls (inferred); v3: `use crate::mod::item` binds items, not only module files; v2: glob uses, module-relative super::, crate root dir


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("rust_collect")
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

    rs_files = sorted(f for f in file_set if f.lower().endswith(".rs"))
    crate_roots = _workspace_crates(repo_root, file_set)   # `grep_cli` -> crates/cli/src/lib.rs

    def child_of(mod_dir: str, name: str) -> str | None:
        """`mod name;` in mod_dir -> child rel (foo.rs, foo/mod.rs)."""
        cands = [f"{mod_dir}/{name}.rs" if mod_dir else f"{name}.rs",
                 f"{mod_dir}/{name}/mod.rs" if mod_dir else f"{name}/mod.rs"]
        for c in cands:
            if c in file_set:
                return c
        return None

    # ---- collect
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
            if d["relation"] == "imports" and \
                    (d["method"] or "").startswith("ts-rust"):
                imports.append((key, d))
            elif d["relation"] == "calls_unresolved" and \
                    (d["method"] or "").startswith("ts-rust"):
                unres_calls.append((key, d))
    for key, res in new_results:
        for e in res.edges:
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

    # ---- per-file symbol tables (stored + new)
    file_syms: dict[str, dict[str, list[str]]] = {}
    free_fns: set[str] = set()
    qn_of: dict[str, str] = {}

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type"):
            return
        file_syms.setdefault(rel, {}).setdefault(
            getattr(node, "label", ""), []).append(node.id)
        qn_of[node.id] = getattr(node, "qualified_name", "") or ""
        if getattr(node, "type", "") == "function":
            free_fns.add(node.id)

    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and (n.file_path or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if (getattr(n, "file_path", "") or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)

    # label -> method ids, for the distinctive-name fallback on receiver calls
    methods_by_label: dict[str, set] = {}
    for _rel, _syms in file_syms.items():
        for _lab, _ids in _syms.items():
            for _i in _ids:
                if _i not in free_fns and _i.startswith("method:"):
                    methods_by_label.setdefault(_lab, set()).add(_i)

    def _unique_method(name: str):
        """The one repo method called `name` when the name is distinctive
        (>= 10 chars, two word breaks): `find_overlapping_iter` yes,
        `len`/`get`/`as_ref` never. Receiver types are not inferred."""
        if len(name) < 10 or name.count("_") + sum(1 for ch in name if ch.isupper()) < 2:
            return None
        ids = methods_by_label.get(name) or set()
        return next(iter(ids)) if len(ids) == 1 else None

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    def resolve_use(from_rel: str, spec: str):
        """(target_file_rel or None, names|None(glob), via, note).

        names None = glob/unresolvable-namespace. Single file targets only.
        """
        segs = (spec or "").split("::")
        if not segs or not segs[0]:
            return None, None, "", "empty use path"
        root = segs[0]
        here_dir = _dir_of(from_rel)
        if root == "crate":
            tgt = "/".join(segs[1:])
            if not tgt:
                return None, None, "", "bare crate root"
            # The crate root is the dir of the nearest lib.rs/main.rs (often
            # `src/`), not the repository root; then a unique suffix match.
            d = here_dir
            while True:
                if any((f"{d}/{r}" if d else r) in file_set for r in ("lib.rs", "main.rs")):
                    hit = _to_file(f"{d}/{tgt}" if d else tgt, spec)
                    if hit[0]:
                        return hit[0], None, "crate", ""
                    # `crate::expand::call_site_ident`: the tail names items in
                    # module `expand`, not a file; the longest module prefix wins
                    for k in range(len(segs) - 1, 1, -1):
                        mod_p = "/".join(segs[1:k])
                        hit = _to_file(f"{d}/{mod_p}" if d else mod_p, spec)
                        if hit[0]:
                            return hit[0], [segs[k]] if k == len(segs) - 1 else None, "crate", ""
                    root_file = next((f"{d}/{r}" if d else r for r in ("lib.rs", "main.rs")
                                      if (f"{d}/{r}" if d else r) in file_set), None)
                    if root_file and len(segs) == 2:
                        return root_file, [segs[1]], "crate", ""     # `crate::item` at the crate root
                    break
                if not d:
                    break
                d = _dir_of(d)
            # integration tests (`tests/misc.rs` with `use crate::util::Dir`):
            # each test file is a crate root, its modules are its siblings
            a = here_dir
            while True:
                for k in range(len(segs), 1, -1):
                    mod_p = "/".join(segs[1:k])
                    hit = _to_file(f"{a}/{mod_p}" if a else mod_p, spec)
                    if hit[0] and hit[0] != from_rel:
                        return hit[0], ([segs[k]] if k == len(segs) - 1 else None), "crate", ""
                if not a:
                    break
                a = _dir_of(a)
            for f in rs_files:
                noext = f[:-3] if f.lower().endswith(".rs") else f
                if noext == tgt or noext == tgt + "/mod":
                    return f, None, "crate", ""
            hits = [f for f in rs_files if f[:-3].endswith("/" + tgt) or
                    f[:-3].endswith("/" + tgt + "/mod")]
            if len(hits) == 1:
                return hits[0], None, "crate", ""
            return None, None, "", f"crate path missing: {spec}"
        if root == "super":
            ups = 0
            rest = list(segs)
            while rest and rest[0] == "super":
                ups += 1
                rest = rest[1:]
            # Module path of this file: `a/b.rs` is module a/b, while
            # `a/mod.rs` / `lib.rs` / `main.rs` are module a.
            stem = from_rel.rsplit("/", 1)[-1][:-3]
            mod_path = here_dir if stem in ("mod", "lib", "main") else \
                (f"{here_dir}/{stem}" if here_dir else stem)
            parts = mod_path.split("/") if mod_path else []
            if ups > len(parts):
                return None, None, "", f"escapes repository: {spec}"
            base = "/".join(parts[:len(parts) - ups])
            return _mod_prefix(base, rest, spec)
        if root == "self":
            # `self::` is this file's module: `a/b.rs` -> a/b, `a/mod.rs` -> a
            stem = from_rel.rsplit("/", 1)[-1][:-3]
            own = here_dir if stem in ("mod", "lib", "main") else (f"{here_dir}/{stem}" if here_dir else stem)
            return _mod_prefix(own, segs[1:], spec)
        # plain first segment: child mod of this dir, a workspace crate, else extern crate.
        if segs[0] in crate_roots and not child_of(here_dir, segs[0]):
            lib = crate_roots[segs[0]]
            if len(segs) == 1:
                return lib, [segs[0]], "workspace-crate", ""
            for k in range(len(segs), 1, -1):
                mod_p = "/".join(segs[1:k])
                hit = _to_file(f"{_dir_of(lib)}/{mod_p}", spec)
                if hit[0]:
                    return hit[0], ([segs[k]] if k == len(segs) - 1 else None), "workspace-crate", ""
            return lib, [segs[-1]], "workspace-crate", ""
        if len(segs) == 1:
            c = child_of(here_dir, segs[0])
            if c:
                return c, [segs[0]], "child-mod", ""
            return None, None, "external", f"extern crate: {spec}"
        c = child_of(here_dir, segs[0])
        if c:
            return c, [segs[-1]], "child-mod", ""
        return None, None, "external", f"extern crate: {spec}"

    def _mod_prefix(base: str, rest: list, spec: str):
        """`self::parse::parse`: the longest module prefix that is a file;
        the remaining tail names an item in it."""
        for k in range(len(rest), 0, -1):
            tgt = "/".join(([base] if base else []) + rest[:k])
            hit = _to_file(tgt, spec)
            if hit[0]:
                return hit[0], (rest[k:] if k < len(rest) else None), "mod-tree", ""
        return None, None, "", f"module file missing: {spec}"

    def _to_file(tgt: str, spec: str):
        for c in (tgt + ".rs", tgt + "/mod.rs", tgt):
            if c in file_set:
                return c, None, "mod-tree", ""
        return None, None, "", f"module file missing: {spec}"

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

    prof.end("rust_collect")
    prof.begin("rust_bind")
    # (owner_key, local) -> target file rel (package binding for calls)
    bindings: dict[tuple[str, str], str] = {}
    use_orig: dict[tuple[str, str], str] = {}   # (key, alias) -> original item name
    glob_files: dict[str, list[str]] = {}  # owner_key -> glob-imported module files
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        if a.get("glob_import"):
            # `use super::shift::*`: the module's items are all in scope;
            # bare calls fall back to it (unique name only).
            gtgt = resolve_use(owner_rel(key), spec[:-3] if spec.endswith("::*") else spec)[0]
            if gtgt is None:
                diag["unresolved_imports"] += 1
                continue
            glob_files.setdefault(key, []).append(gtgt)
            diag["resolved_imports"] += 1
            continue
        tgt, _names, via, note = resolve_use(owner_rel(key), spec)
        if tgt is None:
            if via == "external":
                diag["external_imports"] += 1
            else:
                diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        add_edge(key, mod_id(owner_rel(key)), "depends_on",
                 mod_id(tgt), d, "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "rust-" + via},
                 site=_sf("rustdep", spec, tgt))
        diag["depends_on_derived"] += 1
        orig_ = spec.rsplit("::", 1)[-1]
        for local in a.get("names") or []:
            if isinstance(local, str) and local:
                bindings.setdefault((key, local), tgt)
                if local != orig_ and orig_ not in ("self", "*"):
                    use_orig.setdefault((key, local), orig_)
    prof.end("rust_bind")
    prof.begin("rust_calls")
    file_key = {owner_rel(k): k for k, _d in imports}

    def item_in(f: str, name: str, depth: int = 0) -> list:
        """Definitions of `name` reachable from module file `f`: its own, else
        through its `pub use` re-exports (`pub use self::serve::serve;` in
        lib.rs, `pub(crate) use self::parse::parse;` in flags/mod.rs)."""
        own = (file_syms.get(f, {}) or {}).get(name, [])
        if own or depth >= 4:
            return list(own)
        k = file_key.get(f)
        if k is None:
            return []
        t = bindings.get((k, name))
        if t and t != f:
            # `pub use complete::fish::generate as generate_complete_fish`
            r = item_in(t, use_orig.get((k, name), name), depth + 1)
            if r:
                return r
        out = []
        for gf in glob_files.get(k, []):
            if gf != f:
                out += item_in(gf, name, depth + 1)
        return out

    for key, d in unres_calls:
        a = _attrs(d)
        kind = a.get("callee_kind", "")
        here = owner_rel(key)
        tgt = None
        if kind == "function":
            base = a.get("base", "")
            # a bare `f()` is never a method (`self.f()` / `Self::f()` are):
            # `interpolate(..)` inside `fn interpolate(&self)` is the imported free fn
            cands = [c for c in (file_syms.get(here, {}) or {}).get(base, []) if c in free_fns]
            tgt = cands[0] if len(cands) == 1 else None
            if tgt is None and len(cands) > 1:
                diag["ambiguous_calls"] += 1
            if tgt is None:
                # Brought into scope by `use` (single targets only).
                tfile = bindings.get((key, base))
                if tfile is not None:
                    ucands = item_in(tfile, use_orig.get((key, base), base))
                    ucands = [c for c in ucands if c in free_fns] or ucands
                    tgt = ucands[0] if len(ucands) == 1 else None
                    if tgt is None and len(ucands) > 1:
                        diag["ambiguous_calls"] += 1
                elif not cands:
                    gc = sorted({nid for gf in glob_files.get(key, [])
                                 for nid in (file_syms.get(gf, {}) or {}).get(base, [])
                                 if nid in free_fns})
                    tgt = gc[0] if len(gc) == 1 else None
                    if len(gc) > 1:
                        diag["ambiguous_calls"] += 1
        elif kind == "path":
            full = a.get("full", "")
            segs = full.split("::") if full else []
            if not segs:
                continue
            tfile = _resolve_path_file(here, segs, file_set)
            if tfile is None and segs[0] in crate_roots:
                # `grep::cli::is_tty()`: crate `grep` (a workspace member), whose
                # lib.rs may re-export another crate (`pub use grep_cli as cli`)
                cur = crate_roots[segs[0]]
                rest = segs[1:-1]
                while rest and cur:
                    nxt = _resolve_path_file(cur, ["self", *rest, segs[-1]], file_set) if rest else cur
                    if nxt and nxt != cur:
                        cur, rest = nxt, []
                        break
                    via = bindings.get((file_key.get(cur), rest[0]))
                    if via is None:
                        cur = None
                        break
                    cur, rest = via, rest[1:]
                tfile = cur
            if not tfile and len(segs) >= 2 and segs[-2][:1].islower():
                # `convert::str(..)` with `mod convert { .. }` inline in this file
                suf = "." + ".".join(segs[-2:])
                inl = [c for c in (file_syms.get(here, {}) or {}).get(segs[-1], [])
                       if qn_of.get(c, "").endswith(suf)]
                if len(inl) == 1:
                    tgt = inl[0]
            if tfile:
                cands = item_in(tfile, segs[-1])
                # `flags::parse()` names a free fn, not `impl Parser { fn parse }`
                cands = [c for c in cands if c in free_fns] or cands
                tgt = cands[0] if len(cands) == 1 else None
                if tgt is None and len(cands) > 1:
                    diag["ambiguous_calls"] += 1
        elif kind == "receiver":
            u = _unique_method(a.get("member", ""))
            if u and u != d["source"]:
                diag["inferred_calls"] = diag.get("inferred_calls", 0) + 1
                add_edge(key, d["source"], "calls", u, d, "inferred", "hypothetical", 0.6,
                         {"via": "rust-unique-method"},
                         site=_sf("rustcall-u", a.get("base", ""), a.get("member", "")))
            continue
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "rust-resolve"},
                     site=_sf("rustcall", a.get("base", ""),
                              a.get("member", "") or a.get("full", "")))
    prof.end("rust_calls")
    prof.begin("rust_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "rust")
    prof.end("rust_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts: a call
    that crosses files IS a file dependency, import statement or not.
    Deterministic per file pair."""
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


def _workspace_crates(repo_root, file_set) -> dict:
    """Workspace member crates: import name (`grep-cli` -> `grep_cli`) -> its src/lib.rs."""
    import os
    import re as _re
    out: dict = {}
    if not repo_root or not os.path.isdir(repo_root):
        return out
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "target", "node_modules", ".ripplet")]
        if "Cargo.toml" not in filenames:
            continue
        try:
            text = open(os.path.join(dirpath, "Cargo.toml"), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m = _re.search(r"^\[package\][^\[]*?^\s*name\s*=\s*[\"']([^\"']+)[\"']", text, _re.M | _re.S)
        if not m:
            continue
        rel = os.path.relpath(dirpath, repo_root).replace("\\", "/")
        rel = "" if rel == "." else rel
        lib = f"{rel}/src/lib.rs" if rel else "src/lib.rs"
        lm = _re.search(r"^\[lib\][^\[]*?^\s*path\s*=\s*[\"']([^\"']+)[\"']", text, _re.M | _re.S)
        if lm:
            lib = (f"{rel}/{lm.group(1)}" if rel else lm.group(1)).replace("\\", "/")
        if lib in file_set:
            out[m.group(1).replace("-", "_")] = lib
    return out


def _resolve_path_file(here, segs, file_set):
    """The .rs file defining the item a call path names, or None.

    `segs` ends with the called item (`crate::expand::f` -> item `f` in
    module `crate::expand`). `crate` is the crate root: the directory of the
    nearest lib.rs/main.rs above `here` (workspace members live in their own
    sub-directories), falling back to the repository root."""
    if len(segs) < 2:
        return None
    root, mods = segs[0], segs[1:-1]
    here_dir = here.rpartition("/")[0]

    def crate_root() -> str:
        d = here_dir
        while True:
            if any((f"{d}/{r}" if d else r) in file_set for r in ("lib.rs", "main.rs")):
                return d
            if not d:
                return ""
            d = d.rpartition("/")[0]

    def module_file(base: str, rest: list) -> str | None:
        if not rest:
            for r in ("lib.rs", "main.rs", "mod.rs"):
                c = f"{base}/{r}" if base else r
                if c in file_set:
                    return c
            return None
        tgt = "/".join(([base] if base else []) + rest)
        for cand in (tgt + ".rs", tgt + "/mod.rs"):
            if cand in file_set:
                return cand
        return None

    if root == "crate":
        hit = module_file(crate_root(), mods)
        if hit is None:
            # integration test crates: `tests/misc.rs` calling `crate::util::f()`
            a = here_dir
            while hit is None:
                hit = module_file(a, mods) if mods else None
                if not a:
                    break
                a = a.rpartition("/")[0]
        return hit
    if root == "super":
        ups = 1
        rest = list(mods)
        while rest and rest[0] == "super":
            ups += 1
            rest = rest[1:]
        stem = here.rsplit("/", 1)[-1][:-3]
        mod_path = here_dir if stem in ("mod", "lib", "main") else (f"{here_dir}/{stem}" if here_dir else stem)
        parts = mod_path.split("/") if mod_path else []
        if ups > len(parts):
            return None
        return module_file("/".join(parts[:len(parts) - ups]), rest)
    if root == "self":
        return module_file(here_dir, mods)
    stem_ = here.rsplit("/", 1)[-1][:-3]
    # children of `a/b.rs` live in `a/b/`; of `a/mod.rs` / `lib.rs` / `main.rs` in `a/`
    own_dir = here_dir if stem_ in ("mod", "lib", "main") else (f"{here_dir}/{stem_}" if here_dir else stem_)
    child = (own_dir + "/" + root) if own_dir else root
    if not ((child + ".rs") in file_set or (child + "/mod.rs") in file_set):
        child = (here_dir + "/" + root) if here_dir else root
    if (child + ".rs") in file_set or (child + "/mod.rs") in file_set:
        return module_file(child, mods) if mods else (child + ".rs" if (child + ".rs") in file_set else child + "/mod.rs")
    return None
