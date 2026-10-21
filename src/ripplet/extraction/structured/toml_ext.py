from __future__ import annotations
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import file_node_id, stable_id, edge_id, content_hash
from ...core.provenance import make_evidence
def extract_toml(rel_path, text, repo_id, project_id) -> ExtractionResult:
    res = ExtractionResult(extractor="toml", extractor_version="1")
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "toml", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid,"file",rel_path,rel_path,project_id,repo_id,rel_path,Location(1,0,1,0),"toml",content_hash(raw),{},[ev.id]))
    try: import tomllib
    except ImportError: import tomli as tomllib  # type: ignore
    try:
        data = tomllib.loads(text)
    except Exception as e:
        res.warnings.append(f"toml parse fallback: {e}")
        for i,line in enumerate(text.splitlines(),1):
            s=line.strip()
            if "=" in s and not s.startswith("#"):
                k=s.split("=")[0].strip()[:120]
                nid=stable_id("cfg",repo_id,rel_path,k+str(i))
                res.nodes.append(Node(nid,"config_key",k,k,project_id,repo_id,rel_path,Location(i,0,i,len(line)),"toml","",{"value":s.split("=",1)[1].strip()[:200]},[ev.id]))
                res.edges.append(Edge(edge_id(fid,"contains",nid,i),fid,nid,"contains","structural","deterministic",1.0,[ev.id],"toml","1",file_path=rel_path,start_line=i))
        return res
    def walk(prefix, obj):
        if isinstance(obj, dict):
            for k,v in obj.items():
                key=f"{prefix}.{k}" if prefix else str(k)
                if not isinstance(v,(dict,list)):
                    nid=stable_id("cfg",repo_id,rel_path,key)
                    res.nodes.append(Node(nid,"config_key",key,key,project_id,repo_id,rel_path,Location(1,0,1,0),"toml","",{"value":str(v)[:200]},[ev.id]))
                    res.edges.append(Edge(edge_id(fid,"contains",nid),fid,nid,"contains","structural","deterministic",1.0,[ev.id],"toml","1",file_path=rel_path))
                else: walk(key,v)
    walk("", data or {})
    return res
