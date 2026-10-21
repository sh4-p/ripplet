"""fact_pack_v1: token-efficient presentation of query_graph responses.

Presentation format only: the canonical graph, retrieval ranking, and all
evidence are untouched. A pack carries the same facts, evidence rows,
paths, qualifications, and counts as the legacy response with:

- a node table (one row per distinct node, fixed positional layout pinned
  by the version); facts and graph paths reference rows by integer index;
- evidence rows as positional arrays under a shared fields legend;
- the static ranking-weights dict replaced by a tamper-evident
  fingerprint (`weights_fp`); anything else in metadata passes through
  verbatim.

The decoder reconstructs the legacy structure exactly, modulo documented
representational differences (key order; `ranking` dict <-> `weights_fp`
marker — verified by fingerprint recomputation in the equivalence gate).
"""
from __future__ import annotations
import hashlib
import json

FORMAT = "fact_pack_v1"
VERSION = 1

NODE_FIELDS = ["id", "type", "label", "qualified_name", "file", "line",
               "score"]
# Evidence rows keep a dynamic fields legend (sorted union of keys present
# in this response): storage may add columns (e.g. supersedes, ts) and the
# pack must carry them rather than assume a fixed shape. Missing values
# encode as null and decode back as null.


def weights_fp(weights: dict) -> str:
    return hashlib.sha256(
        json.dumps(weights, sort_keys=True).encode()).hexdigest()[:16]


def encode(res: dict) -> dict:
    """Encode a legacy query_graph response dict as fact_pack_v1."""
    if not isinstance(res, dict):
        raise ValueError("fact_pack_v1 encodes a response dict")
    facts = res.get("facts") or []
    paths = res.get("graph_paths") or []
    node_ids: list = []
    seen: set = set()

    def _use(nid):
        if nid not in seen:
            seen.add(nid)
            node_ids.append(nid)

    for f in facts:
        if isinstance(f, dict) and f.get("id"):
            _use(f["id"])
    for path in paths:
        for nid in (path if isinstance(path, list) else []):
            if nid:
                _use(nid)
    by_id: dict[str, dict] = {}
    for f in facts:
        if isinstance(f, dict) and f.get("id") and f["id"] not in by_id:
            by_id[f["id"]] = f
    nodes = []
    node_absent: list = []
    index_of: dict[str, int] = {}
    for i, nid in enumerate(node_ids):
        index_of[nid] = i
        f = by_id.get(nid, {"id": nid})
        row = [f.get("id", ""), f.get("type", ""), f.get("label", ""),
               f.get("qualified_name", ""), f.get("file", ""),
               f.get("line", 1)]
        # Trailing null score is omitted (decoder accepts width 6 or 7);
        # present scores round-trip exactly.
        score = f.get("score")
        row.append(score if isinstance(score, (int, float)) else None)
        if row[-1] is None:
            row.pop()
        # Missing-vs-null preserved exactly (as for evidence rows).
        missing = [NODE_FIELDS[j] for j in range(len(row))
                   if NODE_FIELDS[j] not in f]
        if missing:
            node_absent.append([i, missing])
        nodes.append(row)
    packed_facts = []
    for f in facts:
        if not isinstance(f, dict) or not f.get("id"):
            packed_facts.append({"!": "unidentified-fact", "fact": f})
            continue
        # Non-schema keys (e.g. Phase-19 span_* fields, ledger refs already
        # handled above) travel in an explicit extras envelope so packing
        # can never silently drop caller-visible data.
        extra = {k: v for k, v in f.items()
                 if k not in ("id", "type", "label", "qualified_name",
                              "file", "line", "score")}
        if extra:
            packed_facts.append([index_of[f["id"]], extra])
        else:
            packed_facts.append(index_of[f["id"]])
    packed_paths = []
    for path in paths:
        if not isinstance(path, list):
            packed_paths.append({"!": "unidentified-path", "path": path})
            continue
        packed_paths.append([index_of[n] for n in path if n in index_of])
    ev_rows = []
    efields: list[str] = []
    for e in res.get("evidence") or []:
        if isinstance(e, dict):
            for k in e:
                if k not in efields:
                    efields.append(str(k))
    efields = sorted(efields)
    # Sparse responses carry no legend (decoder needs none without rows).
    # Missing-vs-null is preserved exactly: rows lacking a legend key are
    # listed in "absent" (empty in the uniform case, hence zero overhead
    # for real responses whose rows share one column set).
    absent: list = []
    for i, e in enumerate(res.get("evidence") or []):
        if not isinstance(e, dict):
            ev_rows.append({"!": "unidentified-evidence", "row": e})
            continue
        ev_rows.append([e.get(k) for k in efields])
        missing = [k for k in efields if k not in e]
        if missing:
            absent.append([i, missing])
    meta = dict(res.get("retrieval_metadata") or {})
    if isinstance(meta.get("ranking"), dict):
        meta = dict(meta)
        meta["ranking"] = {"weights_fp": weights_fp(meta["ranking"])}
    # The node legend is implied by the version (NODE_FIELDS is part of
    # the v1 contract) and omitted to avoid taxing sparse responses; the
    # evidence legend stays explicit because row shapes vary per response.
    pack = {"format": FORMAT, "version": VERSION,
            "answer": res.get("answer", ""),
            "limitations": res.get("limitations", []),
            "freshness": res.get("freshness", {}),
            "nodes": nodes,
            "facts": packed_facts,
            "efields": efields,
            "evidence": ev_rows,
            "paths": packed_paths,
            "meta": meta}
    if absent:
        pack["absent"] = absent
    if node_absent:
        pack["nabsent"] = node_absent
    return pack


def decode(pack: dict) -> dict:
    """Reconstruct the legacy response structure from fact_pack_v1."""
    if not isinstance(pack, dict) or pack.get("format") != FORMAT:
        raise ValueError("not a fact_pack_v1 payload")
    if pack.get("version") != VERSION:
        raise ValueError(f"unsupported fact_pack version: {pack.get('version')!r}")
    efields_raw = pack.get("efields") or []
    if any(not isinstance(x, str) for x in efields_raw):
        raise ValueError("fact_pack_v1 evidence fields mismatch")
    efields = list(efields_raw)
    nodes = pack.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError("fact_pack_v1 nodes must be a list")

    nabsent_map = {}
    for entry in pack.get("nabsent") or []:
        if (isinstance(entry, list) and len(entry) == 2 and
                isinstance(entry[0], int) and isinstance(entry[1], list)):
            nabsent_map[entry[0]] = set(entry[1])
        else:
            raise ValueError("fact_pack_v1 nabsent entry malformed")

    def _node(i):
        row = nodes[i]
        if not isinstance(row, list) or len(row) not in (6, len(NODE_FIELDS)):
            raise ValueError(f"fact_pack_v1 node row {i} malformed")
        d = dict(zip(NODE_FIELDS, row))
        if d.get("score") is None:
            d.pop("score", None)
        for k in nabsent_map.get(i, set()):
            d.pop(k, None)
        return d

    facts = []
    for f in pack.get("facts") or []:
        if isinstance(f, dict) and f.get("!") == "unidentified-fact":
            facts.append(f["fact"])
        elif isinstance(f, list) and len(f) == 2 and isinstance(f[0], int) \
                and 0 <= f[0] < len(nodes) and isinstance(f[1], dict):
            merged = _node(f[0])
            for k, v in f[1].items():
                if k not in NODE_FIELDS:
                    merged[k] = v
            facts.append(merged)
        elif isinstance(f, int) and 0 <= f < len(nodes):
            facts.append(_node(f))
        else:
            raise ValueError("fact_pack_v1 fact entry malformed")
    evidence = []
    absent_map = {}
    for entry in pack.get("absent") or []:
        if (isinstance(entry, list) and len(entry) == 2 and
                isinstance(entry[0], int) and isinstance(entry[1], list)):
            absent_map[entry[0]] = set(entry[1])
        else:
            raise ValueError("fact_pack_v1 absent entry malformed")
    for idx, row in enumerate(pack.get("evidence") or []):
        if isinstance(row, dict) and row.get("!") == "unidentified-evidence":
            evidence.append(row["row"])
        elif isinstance(row, list) and len(row) == len(efields):
            d = dict(zip(efields, row))
            for k in absent_map.get(idx, set()):
                d.pop(k, None)
            evidence.append(d)
        else:
            raise ValueError("fact_pack_v1 evidence row malformed")
    paths = []
    for path in pack.get("paths") or []:
        if isinstance(path, dict) and path.get("!") == "unidentified-path":
            paths.append(path["path"])
        elif isinstance(path, list):
            ids = []
            for i in path:
                if not isinstance(i, int) or not (0 <= i < len(nodes)):
                    raise ValueError("fact_pack_v1 path index malformed")
                ids.append(nodes[i][0])
            paths.append(ids)
        else:
            raise ValueError("fact_pack_v1 path malformed")
    return {"answer": pack.get("answer", ""),
            "facts": facts,
            "evidence": evidence,
            "graph_paths": paths,
            "limitations": pack.get("limitations", []),
            "freshness": pack.get("freshness", {}),
            "retrieval_metadata": pack.get("meta", {})}
