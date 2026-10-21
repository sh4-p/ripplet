from __future__ import annotations
import re

# Configurable, inspectable ranking weights (Phase 4 Stage 3). Every weight is
# reported in retrieval_metadata["ranking"] and its effect is measured by
# `ripplet benchmark` — no hidden constants.
RERANK_WEIGHTS = {
    "exact_match": 100,      # query equals label or qualified name
    "dotted_exact": 50,      # dotted query equals qualified name
    "identifier_exact": 40,  # an extracted identifier equals label/qualified
    "substring_label": 20,   # raw query contained in label
    "substring_qualified": 30,
    "token_overlap": 10,     # per shared token between query and names
    "code_type": 15,         # function/method/class/interface/type bonus
    "scope_match": 15,       # node lives under the query's dotted scope
    "external_penalty": -50,
    "tombstone_penalty": -100,
}

_TOKEN = re.compile(r"[A-Za-z_][\w]*")


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(s or "")}


def rerank(nodes, query: str):
    from . import terms as termex
    q = query.lower().strip("`'\"")
    tex = termex.extract_terms(query)
    idents = {i.lower() for i in tex["identifiers"]}
    scope = (tex.get("scope") or "").lower()
    # Keyword-derived scopes (e.g. the word "API") are retrieval hints, not
    # identity claims: only identifier-derived scopes earn the match bonus.
    scope_ok = bool(scope) and tex.get("scope_from") == "ident"
    W = RERANK_WEIGHTS

    def score(n):
        label = (getattr(n, "label", "") or "").lower()
        qn = (getattr(n, "qualified_name", "") or "").lower()
        ntype = getattr(n, "type", "")
        s = 0
        if q == label or q == qn:
            s += W["exact_match"]
        if "." in q and q == qn:
            s += W["dotted_exact"]
        if label in idents or qn in idents:
            s += W["identifier_exact"]
        if q in label:
            s += W["substring_label"]
        if q in qn:
            s += W["substring_qualified"]
        shared = (_tokens(label) | _tokens(qn)) & (_tokens(q) | idents)
        s += W["token_overlap"] * len(shared)
        if scope_ok and (qn == scope or qn.startswith(scope + ".") or qn.startswith(scope + "/")):
            s += W["scope_match"]
        if ntype == "external_symbol":
            s += W["external_penalty"]
        try:
            if ((getattr(n, "attributes", {}) or {}).get("tombstoned")):
                s += W["tombstone_penalty"]
        except Exception:
            pass
        if ntype in ("function", "method", "class", "interface", "type"):
            s += W["code_type"]
        return -s
    return sorted(nodes, key=score)
