from __future__ import annotations
from ..core import schema
from ..core.models import ExtractionResult
def validate_result(r: ExtractionResult) -> list[str]:
    errs = []
    ids = {n.id for n in r.nodes}
    for e in r.edges:
        if e.source not in ids: errs.append(f"edge {e.id} unknown source {e.source}")
        if e.target not in ids and not e.target.startswith("ext:"): errs.append(f"edge {e.id} unknown target {e.target}")
        if not schema.validate_relation(e.relation): errs.append(f"bad relation {e.relation}")
    for n in r.nodes:
        if not schema.validate_node_type(n.type): errs.append(f"bad node type {n.type} ({n.id})")
    return errs
