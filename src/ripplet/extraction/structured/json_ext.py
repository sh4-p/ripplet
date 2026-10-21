from __future__ import annotations
import json
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import file_node_id, stable_id, edge_id, content_hash
from ...core.provenance import make_evidence
def _walk(prefix, obj, cb):
    if isinstance(obj, dict):
        for k,v in obj.items():
            cb(f"{prefix}.{k}" if prefix else str(k), v)
            _walk(f"{prefix}.{k}" if prefix else str(k), v, cb)
    elif isinstance(obj, list):
        for i,v in enumerate(obj):
            _walk(f"{prefix}[{i}]", v, cb)
def extract_json(rel_path, text, repo_id, project_id) -> ExtractionResult:
    res = ExtractionResult(extractor="json", extractor_version="1")
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "json", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid,"file",rel_path,rel_path,project_id,repo_id,rel_path,Location(1,0,1,0),"json",content_hash(raw),{},[ev.id]))
    try: data = json.loads(text)
    except Exception as e:
        res.warnings.append(f"json parse: {e}"); res.partial=True; return res
    def cb(key, val):
        if isinstance(val,(dict,list)): return
        nid = stable_id("cfg", repo_id, rel_path, key)
        res.nodes.append(Node(nid,"config_key",key,key,project_id,repo_id,rel_path,Location(1,0,1,0),"json","",{"value": str(val)[:200]},[ev.id]))
        res.edges.append(Edge(edge_id(fid,"contains",nid),fid,nid,"contains","structural","deterministic",1.0,[ev.id],"json","1",file_path=rel_path))
    _walk("", data, cb)
    return res
