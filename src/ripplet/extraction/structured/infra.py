from __future__ import annotations
import re
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import file_node_id, stable_id, canonical_edge_id, content_hash
from ...core.provenance import make_evidence

DOCKER_VERSION = "2"
DOCKER = re.compile(r"^\s*(FROM|COPY|RUN|EXPOSE|ENV|CMD|ENTRYPOINT)\b", re.I)


def extract_dockerfile(rel_path, text, repo_id, project_id) -> ExtractionResult:
    res = ExtractionResult(extractor="dockerfile", extractor_version=DOCKER_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "dockerfile", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                          Location(1, 0, 1, 0), "dockerfile", content_hash(raw), {},
                          [ev.id], schema_version=2))
    seen: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = DOCKER.match(line)
        if m:
            key = line.strip()[:150]
            occ = seen.get(key, 0) + 1
            seen[key] = occ
            suffix = "" if occ == 1 else f"#{occ}"
            nid = stable_id("cfg", repo_id, rel_path, key + suffix)
            if not any(n.id == nid for n in res.nodes):
                res.nodes.append(Node(nid, "config_key", key[:120], key[:120], project_id,
                                      repo_id, rel_path, Location(i, 0, i, len(line)),
                                      "dockerfile", "", {}, [ev.id], schema_version=2))
            eid = canonical_edge_id(fid, "contains", nid, rel_path)
            if not any(e.id == eid for e in res.edges):
                res.edges.append(Edge(eid, fid, nid, "contains", "structural", "deterministic",
                                      1.0, [ev.id], "dockerfile", DOCKER_VERSION,
                                      file_path=rel_path, start_line=i))
    return res
