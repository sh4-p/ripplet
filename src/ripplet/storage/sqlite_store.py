from __future__ import annotations
import hashlib, json, sqlite3, time
from pathlib import Path
from ..core.models import Node, Edge, Evidence, Location


def _fts_rid(nid: str) -> int:
    """A node's FTS rowid, derived from its id: lets FTS rows be deleted by
    key (FTS5 cannot index the `id` column; deleting by it scans the table)."""
    return int.from_bytes(hashlib.blake2b(nid.encode("utf-8"), digest_size=8).digest(), "big", signed=True)
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, type TEXT, label TEXT, qualified_name TEXT, project_id TEXT, repository_id TEXT, file_path TEXT, start_line INT, start_col INT, end_line INT, end_col INT, language TEXT, content_hash TEXT, attributes TEXT, provenance TEXT, first_seen REAL, last_seen REAL, valid_from TEXT, valid_until TEXT, schema_version INT);
CREATE INDEX IF NOT EXISTS idx_nodes_qn ON nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY, source TEXT, target TEXT, relation TEXT, evidence_type TEXT, confidence_class TEXT, confidence_score REAL, provenance TEXT, method TEXT, version TEXT, valid_from TEXT, valid_until TEXT, attributes TEXT, file_path TEXT, start_line INT, end_line INT);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_tgt ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(relation);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, source_uri TEXT, file_hash TEXT, start_line INT, end_line INT, method TEXT, version TEXT, backend TEXT, quote TEXT, claim_hash TEXT, status TEXT, ts REAL, supersedes TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, repo_id TEXT, sha256 TEXT, extractor TEXT, extractor_version TEXT, config_hash TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, sha TEXT, message TEXT, created_at REAL, node_count INT, edge_count INT);
CREATE TABLE IF NOT EXISTS history(q TEXT, ts REAL, result_count INT);
CREATE TABLE IF NOT EXISTS file_nodes(file_key TEXT, node_id TEXT, PRIMARY KEY(file_key, node_id));
CREATE INDEX IF NOT EXISTS idx_file_nodes_node ON file_nodes(node_id);
CREATE TABLE IF NOT EXISTS file_edges(file_key TEXT, edge_id TEXT, PRIMARY KEY(file_key, edge_id));
CREATE INDEX IF NOT EXISTS idx_file_edges_edge ON file_edges(edge_id);
CREATE TABLE IF NOT EXISTS superseded_edges(edge_id TEXT PRIMARY KEY, by_edge TEXT);
"""
# calls_unresolved rows whose site a resolved edge now covers (derived each
# run by graph.py_callbind). Kept in `edges` as resolver input; hidden from
# readers so one call is never reported both resolved and unresolved.
NOT_SUPERSEDED = "id NOT IN (SELECT edge_id FROM superseded_edges)"
class SQLiteStore:
    def __init__(self, path: str, busy_timeout_ms: int = 5000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.cx = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.cx.row_factory = sqlite3.Row
        try:
            self.cx.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        except Exception:
            pass
        self.cx.executescript(SCHEMA)
        self.cx.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('schema_version','2')")
        self.cx.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('generation','ready')")
        self.cx.commit()
        # FTS5 best-effort
        try:
            self.cx.execute("CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(id, label, qualified_name, file_path)")
            r = self.cx.execute("SELECT v FROM meta WHERE k='fts_rowid'").fetchone()
            if r is None:
                # one-time: older indexes keyed FTS rows arbitrarily; re-key them
                self.cx.execute("DELETE FROM node_fts")
                self.cx.executemany(
                    "INSERT INTO node_fts(rowid,id,label,qualified_name,file_path) VALUES(?,?,?,?,?)",
                    [(_fts_rid(x["id"]), x["id"], x["label"], x["qualified_name"], x["file_path"])
                     for x in self.cx.execute("SELECT id, label, qualified_name, file_path, attributes FROM nodes")
                     if '"tombstoned": true' not in (x["attributes"] or "")])
                self.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('fts_rowid','1')")
                self.cx.commit()
        except Exception: pass
    def execute(self, *a, **k): return self.cx.execute(*a, **k)
    def upsert_evidence(self, items: list[Evidence]):
        for e in items:
            self.cx.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id,e.source_uri,e.file_hash,e.start_line,e.end_line,e.extraction_method,e.extraction_version,e.backend,e.quote[:500],e.claim_hash,e.status,e.timestamp,json.dumps(e.supersedes)))
    def upsert_nodes(self, nodes: list[Node]):
        # Dedupe by id keeping the last occurrence (matches the old
        # per-node interleaved delete/insert final state exactly).
        seen: dict[str, Node] = {}
        for n in nodes:
            seen[n.id] = n
        rows = []
        for n in seen.values():
            loc = n.location if isinstance(n.location, Location) else Location.from_dict(n.location or {})
            old = self.cx.execute("SELECT provenance, first_seen FROM nodes WHERE id=?", (n.id,)).fetchone()
            if old is not None:
                try:
                    prev = set(json.loads(old["provenance"] or "[]"))
                except Exception:
                    prev = set()
                prov = sorted(prev | set(n.provenance_ids or []))
                first = old["first_seen"] or n.first_seen_at
                last = max(n.last_seen_at, n.first_seen_at)
            else:
                prov = sorted(set(n.provenance_ids or []))
                first, last = n.first_seen_at, n.last_seen_at
            self.cx.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (n.id,n.type,n.label,n.qualified_name,n.project_id,n.repository_id,n.file_path,loc.start_line,loc.start_column,loc.end_line,loc.end_column,n.language,n.content_hash,json.dumps(n.attributes),json.dumps(prov),first,last,n.valid_from,n.valid_until,n.schema_version or 2))
            rows.append(n)
        try:
            # FTS5 tables declared without a UNIQUE constraint ignore REPLACE
            # semantics (every INSERT adds a row), so delete-then-insert.
            # Phase 11: chunked deletes — one full FTS scan per chunk instead
            # of one per node (same resulting rows, O(N) scans fewer).
            self._fts_delete([n.id for n in rows])
            self.cx.executemany(
                "INSERT INTO node_fts(rowid,id,label,qualified_name,file_path) VALUES(?,?,?,?,?)",
                [(_fts_rid(n.id), n.id, n.label, n.qualified_name, n.file_path) for n in rows])
        except Exception: pass
    def upsert_edges(self, edges: list[Edge]):
        # several rows of one id in a batch (a derived pass binding call rows
        # of one spelling): every row's call sites survive, not the last one's
        from ..graph.builder import union_duplicate_occurrences
        union_duplicate_occurrences(edges)
        for e in edges:
            old = self.cx.execute("SELECT provenance FROM edges WHERE id=?", (e.id,)).fetchone()
            if old is not None:
                try:
                    prev = set(json.loads(old["provenance"] or "[]"))
                except Exception:
                    prev = set()
                prov = sorted(prev | set(e.provenance_ids or []))
            else:
                prov = sorted(set(e.provenance_ids or []))
            self.cx.execute("INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id,e.source,e.target,e.relation,e.evidence_type,e.confidence_class,e.confidence_score,json.dumps(prov),e.extraction_method,e.extraction_version,e.valid_from,e.valid_until,json.dumps(e.attributes),e.file_path,e.start_line,e.end_line))
    def commit(self): self.cx.commit()
    def rollback(self): self.cx.rollback()
    def get_node(self, nid: str):
        r = self.cx.execute("SELECT * FROM nodes WHERE id=?",(nid,)).fetchone()
        return _row_node(r) if r else None
    def neighbors(self, nid, direction="both", relations=None, limit=100,
                  offset=0, include_total=False):
        """Neighbor edges (rowid order). Default call shape is unchanged.
        With `offset`, skips that many rows; with `include_total`, returns
        an envelope {results, total, offset, limit, complete, continuation}
        so callers can page without silent truncation."""
        q="SELECT * FROM edges WHERE "; params=[]
        clauses=[]
        if direction in ("out","both"): clauses.append("source=?")
        if direction in ("in","both"): clauses.append("target=?")
        # both => OR
        if direction=="both": q+= "(source=? OR target=?)"; params=[nid,nid]
        elif direction=="out": q+="source=?"; params=[nid]
        else: q+="target=?"; params=[nid]
        if relations: q+= " AND relation IN (%s)" % ",".join("?"*len(relations)); params+=list(relations)
        q += " AND " + NOT_SUPERSEDED
        if not include_total and not offset:
            q+=" LIMIT ?"; params.append(limit)
            return [dict(r) for r in self.cx.execute(q, params).fetchall()]
        total = None
        if include_total:
            cq = q.replace("SELECT * FROM edges", "SELECT COUNT(*) c FROM edges", 1)
            try:
                total = self.cx.execute(cq, params).fetchone()["c"]
            except Exception:
                total = None
        q += " LIMIT ? OFFSET ?"
        params = params + [limit, offset]
        rows = [dict(r) for r in self.cx.execute(q, params).fetchall()]
        if not include_total:
            return rows
        complete = (offset + len(rows) >= total) if total is not None else True
        return {"results": rows, "total": total, "offset": offset,
                "limit": limit, "complete": complete,
                "continuation": None if complete else {
                    "offset": offset + len(rows),
                    "hint": "repeat with offset to list remaining neighbors"}}
    def search_lexical(self, term, limit=20):
        term=term.strip()
        if not term: return []
        out=[]
        try:
            rows=self.cx.execute("SELECT n.* FROM node_fts f JOIN nodes n ON n.id=f.id WHERE node_fts MATCH ? LIMIT ?", (term, limit)).fetchall()
            out=[_row_node(r) for r in rows]
            if out: return out
        except Exception: pass
        like=f"%{term}%"
        rows=self.cx.execute("SELECT * FROM nodes WHERE label LIKE ? OR qualified_name LIKE ? OR file_path LIKE ? LIMIT ?",(like,like,like,limit)).fetchall()
        return [_row_node(r) for r in rows]
    # Phase 12 P2: deterministic relevance order. Rowid (insertion) order made
    # exact matches lose to JSON/config keys indexed earlier, which is the
    # measured exact-P@1 collapse. Ties keep every candidate (ambiguity is
    # never collapsed or filtered) — real code/module symbols simply sort
    # above data blobs and external placeholders, with a fully deterministic
    # tail so repeated queries are stable.
    _SYMBOL_RANK_ORDER = (
        "ORDER BY ((qualified_name=? OR label=?) ) DESC, "
        "(type IN ('function','method','class','interface','type','module','package',"
        "'constant')) DESC, "
        "(type!='external_symbol') DESC, "
        "LENGTH(qualified_name), qualified_name, id"
    )
    def search_symbols(self, name, limit=20, partial=True):
        if partial:
            like=f"%{name}%"
            rows=self.cx.execute("SELECT * FROM nodes WHERE qualified_name LIKE ? OR label LIKE ? "+self._SYMBOL_RANK_ORDER+" LIMIT ?",(like,like,name,name,limit)).fetchall()
        else:
            rows=self.cx.execute("SELECT * FROM nodes WHERE qualified_name=? OR label=? "+self._SYMBOL_RANK_ORDER+" LIMIT ?",(name,name,name,name,limit)).fetchall()
        return [_row_node(r) for r in rows]
    def all_edges(self, limit=100000, include_superseded=False):
        where = "" if include_superseded else " WHERE " + NOT_SUPERSEDED
        return [dict(r) for r in self.cx.execute("SELECT * FROM edges" + where + " LIMIT ?", (limit,)).fetchall()]
    def all_nodes(self, limit=100000):
        return [_row_node(r) for r in self.cx.execute("SELECT * FROM nodes LIMIT ?",(limit,)).fetchall()]
    # ---- targeted read accessors (Phase 24 Faz B): same rows as the
    # full scans, without materializing unrelated facts. Rowid order
    # everywhere, so iteration order matches all_edges()/all_nodes().
    def _in_chunks(self, ids, chunk=500):
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), chunk):
            yield ids[i:i + chunk]
    def nodes_by_ids(self, ids: list) -> dict:
        """id -> node for exactly the requested ids (one query per chunk)."""
        out = {}
        for chunk in self._in_chunks(list(ids)):
            if not chunk:
                continue
            q = ",".join("?" * len(chunk))
            for r in self.cx.execute(
                    f"SELECT * FROM nodes WHERE id IN ({q})", chunk).fetchall():
                try:
                    out[r["id"]] = _row_node(r)
                except Exception:
                    continue
        return out
    def edges_by_targets(self, targets: list, relations=None, limit=200000) -> list:
        """Full edge rows with target in `targets` (rowid order)."""
        targets = list(targets)
        if not targets:
            return []
        out = []
        for chunk in self._in_chunks(targets):
            q = ",".join("?" * len(chunk))
            sql = f"SELECT * FROM edges WHERE target IN ({q}) AND {NOT_SUPERSEDED}"
            params: list = list(chunk)
            if relations:
                sql += " AND relation IN (%s)" % ",".join("?" * len(relations))
                params += list(relations)
            sql += " ORDER BY rowid LIMIT ?"
            params.append(limit)
            out.extend(dict(r) for r in self.cx.execute(sql, params).fetchall())
        return out[:limit]
    def edges_by_sources(self, sources: list, relations=None, limit=200000) -> list:
        """Full edge rows with source in `sources` (rowid order)."""
        sources = list(sources)
        if not sources:
            return []
        out = []
        for chunk in self._in_chunks(sources):
            q = ",".join("?" * len(chunk))
            sql = f"SELECT * FROM edges WHERE source IN ({q}) AND {NOT_SUPERSEDED}"
            params: list = list(chunk)
            if relations:
                sql += " AND relation IN (%s)" % ",".join("?" * len(relations))
                params += list(relations)
            sql += " ORDER BY rowid LIMIT ?"
            params.append(limit)
            out.extend(dict(r) for r in self.cx.execute(sql, params).fetchall())
        return out[:limit]
    def edges_by_relation(self, relation: str, limit=200000) -> list:
        """Full edge rows of one relation (rowid order)."""
        return [dict(r) for r in self.cx.execute(
            "SELECT * FROM edges WHERE relation=? AND " + NOT_SUPERSEDED + " ORDER BY rowid LIMIT ?",
            (relation, limit)).fetchall()]
    def edge_tuples(self, relations=None, limit=200000) -> list:
        """Column-pruned (source, target, relation, id) rows for adjacency
        building without JSON decoding (rowid order)."""
        from collections import namedtuple
        T = namedtuple("EdgeLite", ["source", "target", "relation", "id"])
        if relations:
            q = ",".join("?" * len(relations))
            sql = f"SELECT source, target, relation, id FROM edges WHERE relation IN ({q}) AND {NOT_SUPERSEDED} ORDER BY rowid LIMIT ?"
            params: list = list(relations) + [limit]
        else:
            sql = f"SELECT source, target, relation, id FROM edges WHERE {NOT_SUPERSEDED} ORDER BY rowid LIMIT ?"
            params = [limit]
        return [T(r["source"], r["target"], r["relation"], r["id"])
                for r in self.cx.execute(sql, params).fetchall()]
    def test_candidate_nodes(self, repo_id: str, limit=100000) -> list:
        """Superset of _is_test_node-style nodes (rowid order); callers
        apply the exact predicate. LIKE patterns are slash-anchored with
        ESCAPE so 'latest'/'contest' style names never match."""
        rows = self.cx.execute(
            "SELECT * FROM nodes WHERE repository_id=? AND (type='test' "
            "OR label LIKE 'test\\_%' ESCAPE '\\' "
            "OR file_path LIKE '%/test\\_%' ESCAPE '\\' "
            "OR file_path LIKE '%/tests/%' "
            "OR lower(id) LIKE '%test%') ORDER BY rowid LIMIT ?",
            (repo_id, limit)).fetchall()
        out = []
        for r in rows:
            try:
                out.append(_row_node(r))
            except Exception:
                continue
        return out
    def count(self):
        n=self.cx.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]; e=self.cx.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        return n,e
    def set_file(self, path, repo_id, sha, extractor, ever, chash):
        self.cx.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",(path,repo_id,sha,extractor,ever,chash,time.time()))
    def get_file(self, path):
        r=self.cx.execute("SELECT * FROM files WHERE path=?",(path,)).fetchone()
        return dict(r) if r else None
    # ---- file ownership manifests (Phase 2) ----
    def set_manifest(self, file_key: str, node_ids, edge_ids):
        self.cx.execute("DELETE FROM file_nodes WHERE file_key=?", (file_key,))
        self.cx.execute("DELETE FROM file_edges WHERE file_key=?", (file_key,))
        for nid in set(node_ids):
            self.cx.execute("INSERT OR IGNORE INTO file_nodes VALUES(?,?)", (file_key, nid))
        for eid in set(edge_ids):
            self.cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (file_key, eid))

    def set_manifests_batch(self, items: list[tuple[str, list, list]]) -> None:
        """Batch version of set_manifest for many files (Phase 15).

        Identical final rows: one DELETE per key set, then the same
        INSERT OR IGNORE rows in the same per-key order. Read-only callers
        observe no difference; writers save round-trips.
        """
        if not items:
            return
        keys = [k for k, _, _ in items]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ",".join("?" * len(chunk))
            self.cx.execute(f"DELETE FROM file_nodes WHERE file_key IN ({q})", chunk)
            self.cx.execute(f"DELETE FROM file_edges WHERE file_key IN ({q})", chunk)
        node_rows, edge_rows = [], []
        for key, node_ids, edge_ids in items:
            for nid in set(node_ids):
                node_rows.append((key, nid))
            for eid in set(edge_ids):
                edge_rows.append((key, eid))
        if node_rows:
            self.cx.executemany(
                "INSERT OR IGNORE INTO file_nodes VALUES(?,?)", node_rows)
        if edge_rows:
            self.cx.executemany(
                "INSERT OR IGNORE INTO file_edges VALUES(?,?)", edge_rows)

    def bulk_insert_fresh(self, nodes: list[Node], edges: list[Edge], evidence: list) -> None:
        """Cold-index fast path (Phase 15): plain bulk INSERTs for EMPTY
        tables. Call only when nodes/edges/evidence tables hold zero rows;
        then there is no prior provenance to union, no first_seen to
        preserve, and no FTS rows to delete — the per-row upsert logic
        provably reduces to this. Anything else must use the upserts.
        Duplicate ids replicate upsert semantics exactly: nodes keep the
        last occurrence; edges accumulate provenance across occurrences
        while other columns come from the last one.
        """
        seen: dict[str, Node] = {}
        for n in nodes:
            seen[n.id] = n
        nodes = list(seen.values())
        eprov: dict[str, set] = {}
        elast: dict[str, Edge] = {}
        for e in edges:
            eprov.setdefault(e.id, set()).update(e.provenance_ids or [])
            elast[e.id] = e
        def _nrow(n):
            loc = n.location if isinstance(n.location, Location) else Location.from_dict(n.location or {})
            return (n.id,n.type,n.label,n.qualified_name,n.project_id,n.repository_id,n.file_path,loc.start_line,loc.start_column,loc.end_line,loc.end_column,n.language,n.content_hash,json.dumps(n.attributes),json.dumps(sorted(set(n.provenance_ids or []))),n.first_seen_at,n.last_seen_at,n.valid_from,n.valid_until,n.schema_version or 2)
        self.cx.executemany("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_nrow(n) for n in nodes])
        self.cx.executemany("INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(e.id,e.source,e.target,e.relation,e.evidence_type,e.confidence_class,e.confidence_score,json.dumps(sorted(eprov[eid])),e.extraction_method,e.extraction_version,e.valid_from,e.valid_until,json.dumps(e.attributes),e.file_path,e.start_line,e.end_line) for eid, e in elast.items()])
        self.cx.executemany("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ev.id,ev.source_uri,ev.file_hash,ev.start_line,ev.end_line,ev.extraction_method,ev.extraction_version,ev.backend,ev.quote[:500],ev.claim_hash,ev.status,ev.timestamp,json.dumps(ev.supersedes)) for ev in evidence])
        self._fts_delete([n.id for n in nodes])
        self.cx.executemany(
            "INSERT INTO node_fts(rowid,id,label,qualified_name,file_path) VALUES(?,?,?,?,?)",
            [(_fts_rid(n.id), n.id, n.label, n.qualified_name, n.file_path) for n in nodes])

    def get_manifest_nodes(self, file_key: str) -> set[str]:
        return {r["node_id"] for r in self.cx.execute("SELECT node_id FROM file_nodes WHERE file_key=?", (file_key,)).fetchall()}

    def get_manifest_edges(self, file_key: str) -> set[str]:
        return {r["edge_id"] for r in self.cx.execute("SELECT edge_id FROM file_edges WHERE file_key=?", (file_key,)).fetchall()}

    def list_file_keys(self) -> set[str]:
        return {r["path"] for r in self.cx.execute("SELECT path FROM files").fetchall()}

    def other_owners_of_node(self, node_id: str, exclude_key: str) -> list[str]:
        return [r["file_key"] for r in self.cx.execute(
            "SELECT file_key FROM file_nodes WHERE node_id=? AND file_key!=?", (node_id, exclude_key)).fetchall()]

    def other_owners_of_edge(self, edge_id: str, exclude_key: str) -> list[str]:
        return [r["file_key"] for r in self.cx.execute(
            "SELECT file_key FROM file_edges WHERE edge_id=? AND file_key!=?", (edge_id, exclude_key)).fetchall()]

    def remove_evidence_from_entity(self, table: str, eid: str, ev_ids: set[str]):
        row = self.cx.execute(f"SELECT provenance FROM {table} WHERE id=?", (eid,)).fetchone()
        if row is None:
            return None
        try:
            prov = set(json.loads(row["provenance"] or "[]"))
        except Exception:
            prov = set()
        prov -= ev_ids
        self.cx.execute(f"UPDATE {table} SET provenance=? WHERE id=?", (json.dumps(sorted(prov)), eid))
        return prov

    def _fts_delete(self, ids):
        """Drop FTS rows by their id-derived rowid (a keyed delete, not a scan)."""
        try:
            self.cx.executemany("DELETE FROM node_fts WHERE rowid=?", [(_fts_rid(i),) for i in set(ids)])
        except Exception:
            pass

    def delete_nodes(self, ids):
        ids = list(ids)
        for nid in ids:
            self.cx.execute("DELETE FROM nodes WHERE id=?", (nid,))
        self._fts_delete(ids)

    def delete_edges(self, ids):
        for eid in ids:
            self.cx.execute("DELETE FROM edges WHERE id=?", (eid,))

    def delete_evidence_by_source(self, source_uri: str):
        self.cx.execute("DELETE FROM evidence WHERE source_uri=?", (source_uri,))

    def sweep_orphans(self) -> tuple[int, int, int]:
        """Delete nodes/edges with no manifest owner (stale or legacy identities).

        Tombstoned file nodes (rename history) and manifest-owned facts survive.
        Unreferenced evidence rows are removed too. Returns (nodes, edges, evidence).
        """
        owned_nodes = {r["node_id"] for r in self.cx.execute("SELECT node_id FROM file_nodes").fetchall()}
        owned_edges = {r["edge_id"] for r in self.cx.execute("SELECT edge_id FROM file_edges").fetchall()}
        rn = re_ = 0
        gone: list = []
        for r in self.cx.execute("SELECT id, attributes FROM nodes").fetchall():
            if r["id"] in owned_nodes:
                continue
            try:
                attrs = json.loads(r["attributes"] or "{}")
            except Exception:
                attrs = {}
            if attrs.get("tombstoned"):
                continue
            self.cx.execute("DELETE FROM nodes WHERE id=?", (r["id"],))
            gone.append(r["id"])
            rn += 1
        self._fts_delete(gone)
        for r in self.cx.execute("SELECT id FROM edges").fetchall():
            if r["id"] not in owned_edges:
                self.cx.execute("DELETE FROM edges WHERE id=?", (r["id"],))
                re_ += 1
        used: set[str] = set()
        for r in self.cx.execute("SELECT provenance FROM nodes").fetchall():
            try:
                used |= set(json.loads(r["provenance"] or "[]"))
            except Exception:
                pass
        for r in self.cx.execute("SELECT provenance FROM edges").fetchall():
            try:
                used |= set(json.loads(r["provenance"] or "[]"))
            except Exception:
                pass
        rev = 0
        for r in self.cx.execute("SELECT id FROM evidence").fetchall():
            if r["id"] not in used:
                self.cx.execute("DELETE FROM evidence WHERE id=?", (r["id"],))
                rev += 1
        try:
            alive = {r["id"] for r in self.cx.execute("SELECT id FROM nodes").fetchall()}
            for r in self.cx.execute("SELECT node_id, model FROM semantic_units").fetchall():
                if r["node_id"] not in alive:
                    self.cx.execute("DELETE FROM semantic_units WHERE node_id=? AND model=?",
                                    (r["node_id"], r["model"]))
        except Exception:
            pass  # table absent on pre-semantic databases until first embed
        return rn, re_, rev

    def set_generation(self, state: str):
        self.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('generation',?)", (state,))

    def get_generation(self) -> str:
        r = self.cx.execute("SELECT v FROM meta WHERE k='generation'").fetchone()
        return r["v"] if r else "ready"

    def delete_file_graph(self, rel_path, repo_id):
        # Legacy blind delete kept for compatibility; service now uses
        # ownership-aware reconcile_file_removal instead. Preserved behavior.
        rows=self.cx.execute("SELECT id FROM nodes WHERE file_path=? AND repository_id=?",(rel_path,repo_id)).fetchall()
        ids=[r["id"] for r in rows]
        for nid in ids:
            self.cx.execute("DELETE FROM edges WHERE source=? OR target=?",(nid,nid))
            self.cx.execute("DELETE FROM nodes WHERE id=?",(nid,))
        self._fts_delete(ids)
    def record_snapshot(self, sha, message, nc, ec):
        import hashlib, time as t
        sid=hashlib.sha256(f"{sha}{t.time()}".encode()).hexdigest()[:12]
        self.cx.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",(sid,sha,message,t.time(),nc,ec)); self.cx.commit(); return sid
    def close(self): self.cx.close()
def _row_node(r) -> Node:
    return Node(r["id"],r["type"],r["label"],r["qualified_name"] or "",r["project_id"] or "default",r["repository_id"] or "default",r["file_path"] or "",Location(r["start_line"] or 1,r["start_col"] or 0,r["end_line"] or 1,r["end_col"] or 0),r["language"] or "",r["content_hash"] or "",json.loads(r["attributes"] or "{}"),json.loads(r["provenance"] or "[]"),r["first_seen"] or 0,r["last_seen"] or 0,r["valid_from"] or "",r["valid_until"],r["schema_version"] or 1)
