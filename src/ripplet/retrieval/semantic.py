"""Semantic index over graph symbols and documentation (Phase 4 Stages 5-6).

Indexing units are source-grounded texts (qualified name, language, location,
signature, doc comment, bounded relation summary) — never model prose. Raw
code, derived text, embeddings and provenance stay in separate columns/tables.
"""
from __future__ import annotations
import hashlib
import time

from .embeddings import PIPELINE_VERSION
from .vectors import pack, unpack, cosine, EXACT_ROW_THRESHOLD

# Node types worth embedding. External placeholders, files, modules and
# tombstones are excluded unless tests demonstrate value (none do yet).
INDEX_TYPES = {"function", "method", "class", "interface", "type", "enum",
               "doc_section", "claim", "config_key"}

SCHEMA_ADD = """
CREATE TABLE IF NOT EXISTS semantic_units(
  node_id TEXT, file_key TEXT, provider TEXT, model TEXT, dim INT,
  normalize INT, pipeline TEXT, text_hash TEXT, text TEXT, vec BLOB,
  updated_at REAL, PRIMARY KEY(node_id, model));
CREATE INDEX IF NOT EXISTS idx_sem_file ON semantic_units(file_key);
CREATE INDEX IF NOT EXISTS idx_sem_model ON semantic_units(model);
CREATE TABLE IF NOT EXISTS semantic_state(
  model TEXT PRIMARY KEY, provider TEXT, dim INT, normalize INT,
  pipeline TEXT, updated_at REAL, unit_count INT);
"""


def ensure_schema(store) -> None:
    store.cx.executescript(SCHEMA_ADD)


def unit_text(node, store, max_relations: int = 8) -> str | None:
    """Source-grounded index text for one node. None => do not index."""
    ntype = getattr(node, "type", "")
    if ntype not in INDEX_TYPES:
        return None
    if getattr(node, "type", "") == "external_symbol":
        return None
    try:
        if (getattr(node, "attributes", {}) or {}).get("tombstoned"):
            return None
    except Exception:
        pass
    loc = getattr(node, "location", None)
    line = getattr(loc, "start_line", 1) if loc else 1
    attrs = getattr(node, "attributes", {}) or {}
    parts = [
        f"symbol: {getattr(node, 'qualified_name', '') or getattr(node, 'label', '')}",
        f"kind: {ntype}",
        f"language: {getattr(node, 'language', '')}",
        f"defined_at: {getattr(node, 'file_path', '')}:{line}",
    ]
    if attrs.get("signature"):
        parts.append(f"signature: {attrs['signature']}")
    # File-scoped invalidation: any byte change in the owning file retires the
    # unit even when the derived description is unchanged (cheap, correct).
    try:
        frow = store.cx.execute(
            "SELECT content_hash FROM nodes WHERE type='file' AND file_path=? "
            "AND repository_id=? LIMIT 1",
            (getattr(node, "file_path", ""), getattr(node, "repository_id", ""))).fetchone()
        if frow and frow["content_hash"]:
            parts.append(f"source_version: {frow['content_hash'][:24]}")
    except Exception:
        pass
    if attrs.get("doc"):
        parts.append(f"documentation: {str(attrs['doc'])[:600]}")
    if ntype in ("claim", "doc_section"):
        parts.append(f"text: {getattr(node, 'label', '')[:600]}")
    try:
        rels = []
        for e in store.neighbors(node.id, direction="both", limit=40):
            other = e["target"] if e["source"] == node.id else e["source"]
            peer = store.get_node(other)
            label = getattr(peer, "label", other) if peer else other
            rels.append(f"{e['relation']} {label}")
            if len(rels) >= max_relations:
                break
        if rels:
            parts.append("related: " + "; ".join(sorted(set(rels))))
    except Exception:
        pass
    return "\n".join(parts)[:4000]


def text_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_index(store, provider, model: str, normalize: bool = True,
                progress=None, cancel=None, limit_nodes: int = 200000) -> dict:
    """Embed changed units only; remove rows for deleted nodes. Deterministic
    node order (sorted ids). Returns stats with cache-hit accounting."""
    import time as _t
    ensure_schema(store)
    t0 = _t.time()
    stats = {"model": model, "provider": provider.provider_id(),
             "units_total": 0, "embedded": 0, "reused": 0, "removed": 0,
             "dim": None, "batches": 0}
    nodes = sorted(store.all_nodes(), key=lambda n: n.id)[:limit_nodes]
    wanted: dict[str, tuple[str, str, str]] = {}  # nid -> (file_key, thash, text)
    for n in nodes:
        # file_key recovery: manifests own the lifecycle mapping
        try:
            owners = [r["file_key"] for r in store.cx.execute(
                "SELECT file_key FROM file_nodes WHERE node_id=?", (n.id,)).fetchall()]
        except Exception:
            owners = []
        text = unit_text(n, store)
        if text is None:
            continue
        stats["units_total"] += 1
        wanted[n.id] = (owners[0] if owners else "", text_fingerprint(text), text)
    # purge rows for vanished nodes (multi-model safe: only this model's rows)
    try:
        stale = [r["node_id"] for r in store.cx.execute(
            "SELECT node_id FROM semantic_units WHERE model=?", (model,)).fetchall()
            if r["node_id"] not in wanted]
        for nid in stale:
            store.cx.execute("DELETE FROM semantic_units WHERE node_id=? AND model=?",
                             (nid, model))
        stats["removed"] = len(stale)
    except Exception:
        pass
    to_embed: list[tuple[str, str]] = []
    for nid, (fkey, thash, text) in sorted(wanted.items()):
        try:
            row = store.cx.execute(
                "SELECT text_hash, dim, normalize, pipeline, provider FROM semantic_units"
                " WHERE node_id=? AND model=?", (nid, model)).fetchone()
        except Exception:
            row = None
        if row is not None and row["text_hash"] == thash and row["pipeline"] == PIPELINE_VERSION \
                and row["provider"] == provider.provider_id() \
                and (row["normalize"] or 0) == (1 if normalize else 0):
            stats["reused"] += 1
            if stats["dim"] is None:
                stats["dim"] = row["dim"]
            continue
        to_embed.append((nid, text))
    prov = provider.provider_id()
    batch: list[str] = []
    batch_ids: list[str] = []
    bs = max(1, getattr(provider, "config", None).batch_size
             if hasattr(provider, "config") else 16)
    if hasattr(provider, "config"):
        try:
            bs = max(1, provider.config.batch_size)
        except Exception:
            pass
    now = _t.time()

    def flush():
        if not batch:
            return
        vecs = provider.embed(batch, cancel=cancel)
        for nid, vec in zip(batch_ids, vecs):
            fkey, thash, text = wanted[nid]
            dim = len(vec)
            if stats["dim"] is None:
                stats["dim"] = dim
            elif dim != stats["dim"]:
                from .embeddings import DimensionMismatch
                raise DimensionMismatch(
                    f"model {model} changed dimensions mid-index: {dim} != {stats['dim']}")
            store.cx.execute(
                "INSERT OR REPLACE INTO semantic_units(node_id,file_key,provider,model,"
                "dim,normalize,pipeline,text_hash,text,vec,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (nid, fkey, prov, model, dim, 1 if normalize else 0, PIPELINE_VERSION,
                 thash, text[:4000], pack([float(v) for v in vec]), now))
            stats["embedded"] += 1
        stats["batches"] += 1
        if progress is not None:
            try:
                progress(stats["embedded"] + stats["reused"], stats["units_total"])
            except Exception:
                pass

    for nid, text in to_embed:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            from .embeddings import EmbeddingError
            raise EmbeddingError("semantic indexing cancelled")
        batch.append(text)
        batch_ids.append(nid)
        if len(batch) >= bs:
            flush()
            batch, batch_ids = [], []
    flush()
    try:
        count = store.cx.execute("SELECT COUNT(*) c FROM semantic_units WHERE model=?",
                                 (model,)).fetchone()["c"]
        store.cx.execute(
            "INSERT OR REPLACE INTO semantic_state(model,provider,dim,normalize,pipeline,"
            "updated_at,unit_count) VALUES(?,?,?,?,?,?,?)",
            (model, prov, stats["dim"] or 0, 1 if normalize else 0, PIPELINE_VERSION,
             _t.time(), count))
    except Exception:
        pass
    stats["seconds"] = round(_t.time() - t0, 2)
    stats["exact_scan"] = True
    stats["exact_row_threshold"] = EXACT_ROW_THRESHOLD
    return stats


def search(store, provider, model: str, query_text: str, limit: int = 20,
           normalize: bool = True) -> dict:
    """Cosine search over rows matching this exact model configuration. Never
    compares vectors across incompatible configurations."""
    ensure_schema(store)
    try:
        state = store.cx.execute("SELECT * FROM semantic_state WHERE model=?",
                                 (model,)).fetchone()
    except Exception:
        state = None
    if state is None:
        return {"available": False, "reason": f"no semantic index for model {model!r}; "
                f"run `ripplet embed --model {model}` first.", "results": []}
    try:
        qvec = provider.embed_query(query_text)
    except Exception as e:
        return {"available": False, "reason": f"provider failed: {e}", "results": []}
    from .vectors import check_vector
    try:
        check_vector(qvec, state["dim"])
    except Exception as e:
        return {"available": False, "reason": f"query vector rejected: {e}", "results": []}
    rows = store.cx.execute(
        "SELECT node_id, vec, dim FROM semantic_units WHERE model=? AND provider=? "
        "AND dim=? AND normalize=? AND pipeline=?",
        (model, provider.provider_id(), state["dim"], 1 if normalize else 0,
         PIPELINE_VERSION)).fetchall()
    if not rows:
        return {"available": False, "reason": "index rows do not match active model "
                "configuration (model changed?)", "results": []}
    scored = []
    for r in rows:
        try:
            v = unpack(r["vec"])
            if len(v) != len(qvec):
                continue
            dot = sum(x * y for x, y in zip(qvec, v))
            scored.append((r["node_id"], dot))  # vectors stored normalized
        except Exception:
            continue
    scored.sort(key=lambda x: (-x[1], x[0]))
    stale = _staleness(store, state)
    return {"available": True, "model": model, "dim": state["dim"],
            "stale": stale, "results": [{"id": nid, "score": round(s, 4)}
                                        for nid, s in scored[:limit]]}


def _staleness(store, state) -> dict:
    try:
        last_file = store.cx.execute("SELECT MAX(updated_at) m FROM files").fetchone()["m"]
        return {"index_updated_at": state["updated_at"],
                "files_updated_at": last_file,
                "stale": bool(last_file and state["updated_at"] and
                              last_file > state["updated_at"])}
    except Exception:
        return {}


def state_summary(store) -> dict:
    try:
        ensure_schema(store)
        return [dict(r) for r in store.cx.execute("SELECT * FROM semantic_state").fetchall()]
    except Exception:
        return []


# Backwards-compatible stub surface (Phase 1-3 callers / tests).
class SemanticIndex:
    available = False

    def __init__(self, *a, **k):
        self.available = False

    def search(self, q, limit=10):
        return {"available": False,
                "reason": "no semantic provider configured (offline mode)",
                "results": []}
