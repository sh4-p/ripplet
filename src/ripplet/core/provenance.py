from __future__ import annotations
import time
from .models import Evidence
from .identifiers import content_hash, claim_hash, stable_id
def make_evidence(source_uri: str, quote: str = "", method: str = "", file_bytes: bytes = b"", start_line: int = 0, end_line: int = 0, status: str = "exists") -> Evidence:
    # Stable identity per (source, method): the extraction ACT, not a content
    # snapshot (Phase 6 Stage 1). Quote-dependent IDs multiplied rows on every
    # edit and the provenance union could never forget stale contributors,
    # breaking incremental/full equivalence. Content changes still surface via
    # quote/claim_hash/file_hash fields; orphaned rows are swept as unreferenced.
    q = quote[:500]
    eid = stable_id("ev", source_uri, method)
    return Evidence(id=eid, source_uri=source_uri, file_hash=content_hash(file_bytes) if file_bytes else "", start_line=start_line, end_line=end_line, extraction_method=method, quote=q, claim_hash=claim_hash(q), status=status, timestamp=time.time())
