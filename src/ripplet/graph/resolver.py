from __future__ import annotations
def resolve_unresolved(nodes_by_label: dict[str, list[str]], edges: list, node_types: dict[str,str] | None = None) -> list:
    """Try to bind calls_unresolved edges whose short name matches a qualified symbol. Returns new resolved edges.
    Prefers real code symbols over external_symbol placeholders; never resolves external->external.
    The produced edge inherits the source call site's fingerprint so one edge
    per call site is preserved end-to-end (Phase 4 Stage 1)."""
    from ..core.identifiers import canonical_edge_id, site_fingerprint
    from ..core.models import Edge
    produced = []
    node_types = node_types or {}
    # Phase 15 (exact memoization): the candidate pool is a pure function of
    # (short name, source). Repeated call sites of one callee in one scope
    # share it; per-edge output (ids, occurrences) is still built per edge,
    # so encounter order and produced edges are identical.
    _pool_cache: dict[tuple[str, str], list] = {}
    for e in edges:
        if e.relation != "calls_unresolved": continue
        # Python calls bind receiver/import-aware in the extractor and in
        # graph.py_callbind; a repo-wide unique-label guess here produced
        # wrong `resolved` edges (`sys.exit` -> Context.exit).
        if getattr(e, "extraction_method", "") == "python-call-resolution": continue
        # skip self-loop noise like out.append -> append (receiver-qualified, not a real symbol)
        raw = (e.attributes.get("unresolved_call","") or "")
        if "." in raw and raw.split(".")[0] in ("out","self","os","sys"):
            # still allow self.method resolution? keep conservative: skip out.* noise
            if raw.startswith("out."): continue
        short = raw.split(".")[-1].split("(")[0].strip()
        if not short: continue
        _pkey = (short, e.source)
        pool = _pool_cache.get(_pkey)
        if pool is None:
            cands = [c for c in nodes_by_label.get(short, []) if c != e.source]
            if not cands:
                _pool_cache[_pkey] = []
                continue
            real = [c for c in cands if node_types.get(c, "") in ("function","method","class","module")]
            pool = real or [c for c in cands if not str(c).startswith("ext:")]
            _pool_cache[_pkey] = pool
        if len(pool) == 1:
            tgt = pool[0]
            # avoid resolving to the same external placeholder family
            if str(tgt).startswith("ext:") and str(e.target).startswith("ext:"): continue
            site = site_fingerprint(raw[:200], e.file_path or "")
            attrs = {}
            try:
                occ = (e.attributes or {}).get("occurrences")
                if occ:
                    attrs["occurrences"] = occ
            except Exception:
                pass
            produced.append(Edge(canonical_edge_id(e.source,"calls",tgt,e.file_path or "",site), e.source, tgt, "calls","resolved","resolved",0.6,e.provenance_ids,"name-resolver","2",file_path=e.file_path,start_line=e.start_line,attributes=attrs))
    return produced
