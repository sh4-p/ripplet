from __future__ import annotations
import re
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import file_node_id, stable_id, canonical_edge_id, content_hash
from ...core.provenance import make_evidence

MD_VERSION = "3"
H = re.compile(r"^(#{1,6})\s+(.*)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def extract_markdown(rel_path, text, repo_id, project_id) -> ExtractionResult:
    res = ExtractionResult(extractor="markdown", extractor_version=MD_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "markdown", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                          Location(1, 0, len(text.splitlines()) or 1, 0), "markdown",
                          content_hash(raw), {}, [ev.id], schema_version=2))
    lines = text.splitlines()
    cur_section = fid
    seen_titles: dict[str, int] = {}

    def _edge(src, rel, tgt, line, etype, cclass, score, attrs=None):
        # Stable edge identity: endpoints + relation + owning file (no line).
        # Multiple links between the same pair in one file collapse to one edge;
        # the line is kept as metadata only.
        return Edge(canonical_edge_id(src, rel, tgt, rel_path), src, tgt, rel,
                    etype, cclass, score, [ev.id], "markdown", MD_VERSION,
                    file_path=rel_path, start_line=line, attributes=attrs or {})

    for i, line in enumerate(lines, start=1):
        m = H.match(line)
        if m:
            title = m.group(2).strip()[:200]
            occ = seen_titles.get(title, 0) + 1
            seen_titles[title] = occ
            # Stable section identity: file + heading text + occurrence index.
            # Inserting text above no longer changes the ID; duplicate headings
            # in one file get #2, #3... discriminators instead of line numbers.
            suffix = "" if occ == 1 else f"#{occ}"
            sid = stable_id("docsec", repo_id, rel_path, title + suffix)
            if not any(n.id == sid for n in res.nodes):
                res.nodes.append(Node(sid, "doc_section", title, f"{rel_path}#{title}{suffix}",
                                      project_id, repo_id, rel_path,
                                      Location(i, 0, i, len(line)), "markdown", "",
                                      {}, [ev.id], schema_version=2))
                res.edges.append(_edge(fid, "contains", sid, i, "structural", "deterministic", 1.0))
            cid = stable_id("claim", repo_id, rel_path, title)
            if not any(n.id == cid for n in res.nodes):
                res.nodes.append(Node(cid, "claim", title[:200], title, project_id, repo_id,
                                      rel_path, Location(i, 0, i, len(line)), "markdown", "",
                                      {"kind": "documented_claim"}, [ev.id], schema_version=2))
            if not any(e.id == canonical_edge_id(sid, "claims", cid, rel_path) for e in res.edges):
                res.edges.append(_edge(sid, "claims", cid, i, "documented", "claimed", None))
            cur_section = sid
        for lm in LINK.finditer(line):
            tgt = lm.group(2)[:300]
            eid = stable_id("doclink", repo_id, tgt)
            if not any(n.id == eid for n in res.nodes):
                res.nodes.append(Node(eid, "external_symbol", tgt, tgt, project_id, repo_id,
                                      "", Location(), "", "", {}, [ev.id], schema_version=2))
            eid2 = canonical_edge_id(cur_section, "references", eid, rel_path)
            if not any(e.id == eid2 for e in res.edges):
                res.edges.append(_edge(cur_section, "references", eid, i, "documented",
                                       "claimed", None, {"label": lm.group(1)[:100]}))
    if not any(n.type == "doc_section" for n in res.nodes):
        res.warnings.append("no headings found")
    return res
