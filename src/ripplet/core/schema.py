from __future__ import annotations
SCHEMA_VERSION = 2
NODE_TYPES = {"file","directory","repository","module","function","method","class","interface","type","variable","import","config","config_key","route","table","column","query","event","consumer","producer","test","doc","doc_section","claim","commit","snapshot","external_symbol","package","api_handler","constant"}
# Every relation here is written by a shipped extractor/resolver or read by
# a shipped tool — locked by tests/integration/test_phase31_edge_coverage_parsers.py.
# Removed 2026-10 (Phase 31, zero writers + zero readers in-tree, zero
# occurrences on ServisPilot's 2495 edges and a mixed-language fixture):
# reads_config, writes_config, queries_table, produces, consumes, tests,
# documents, supersedes, similar_to, changed_in, tested_by.
# Kept but writer-less by design: conflicts_with (read by get_conflicts;
# absence is itself an honest, eval-pinned answer) and imports_from (read
# by discovery/ranking/multihop/executor traversal filters; reserved for a
# future from-import module edge, never silently minted).
RELATIONS = {"contains","defines","imports","calls","calls_unresolved","inherits","implements","uses","handles_route","claims","conflicts_with","depends_on","references","imports_from","renames_to","exports","instantiates","test_covers_symbol","dispatches","calls_http"}
EVIDENCE_TYPES = {"structural","resolved","observed","documented","inferred","conflict"}
CONFIDENCE_CLASSES = {"deterministic","resolved","observed","claimed","hypothetical","conflicted","stale","unavailable"}
def validate_node_type(t: str) -> bool: return t in NODE_TYPES
def validate_relation(r: str) -> bool: return r in RELATIONS
def validate_evidence_type(e: str) -> bool: return e in EVIDENCE_TYPES
