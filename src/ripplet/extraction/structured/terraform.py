"""Terraform (HCL) extraction: blocks and the references between them.

  resource "aws_s3_bucket" "logs" { ... }   -> config  tf:resource  aws_s3_bucket.logs
  data "aws_ami" "base" { ... }             -> config  tf:data      data.aws_ami.base
  module "net" { source = "./modules/net" } -> config  tf:module    module.net (+ source)
  variable "region" / output "url" / locals -> config  tf:variable / tf:output

Inside a block, `aws_s3_bucket.logs.arn`, `module.net.id`, `var.region`,
`data.aws_ami.base.id` and `depends_on = [...]` are recorded and become
`depends_on` edges to the referenced block (graph/infra_link.py). A local module `source` is kept as an attribute
(graph/infra_link.py links it to the module directory's files). Line-based,
brace-balanced scan: no HCL dependency; comments and strings respected.
"""
from __future__ import annotations

import re

from ...core.identifiers import content_hash, edge_id, file_node_id, stable_id
from ...core.models import Edge, ExtractionResult, Location, Node
from ...core.provenance import make_evidence

AVAILABLE = True
TF_VERSION = "2"
_BLOCK = re.compile(r'^[ \t]*(resource|data|module|variable|output|provider)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{', re.M)
_LOCALS = re.compile(r"^[ \t]*locals\s*\{", re.M)
_REF = re.compile(r"\b(?:(var|local|module)\.([A-Za-z_][\w-]*)|(data)\.([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)|"
                  r"([a-z][a-z0-9]*_[a-z0-9_]+)\.([A-Za-z_][\w-]*))")
_SOURCE = re.compile(r'^[ \t]*source\s*=\s*"([^"]+)"', re.M)


def _strip(text: str) -> str:
    """Comments blanked (offsets kept)."""
    out = list(text)
    i, n, q = 0, len(text), False
    while i < n:
        c = text[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                q = False
        elif c == '"':
            q = True
        elif c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _block_end(text: str, open_at: int) -> int:
    depth, q, i = 0, False, open_at
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                q = False
        elif c == '"':
            q = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


def extract_terraform(rel_path: str, text: str, repo_id: str, project_id: str) -> ExtractionResult:
    res = ExtractionResult(extractor="terraform", extractor_version=TF_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "terraform", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path, Location(1, 0, 1, 0),
                          "terraform", content_hash(raw),
                          {"parser": {"backend": "hcl-scan", "status": "ok", "language": "terraform"}},
                          [ev.id], schema_version=2))
    clean = _strip(text)
    starts = [0] + [i + 1 for i, ch in enumerate(clean) if ch == "\n"]

    def line_of(off: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    blocks = []          # (address, kind, node id, body text, body offset)

    def node_id(addr: str) -> str:
        # module-level identity: a block is addressed the same from every file of the directory
        d = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        return stable_id("tf", repo_id, d, addr)

    for m in _BLOCK.finditer(clean):
        kind, a, b = m.group(1), m.group(2), m.group(3)
        if kind == "resource" and b:
            addr = f"{a}.{b}"
        elif kind == "data" and b:
            addr = f"data.{a}.{b}"
        elif kind == "module":
            addr = f"module.{a}"
        elif kind == "variable":
            addr = f"var.{a}"
        elif kind == "output":
            addr = f"output.{a}"
        else:
            addr = f"provider.{a}"
        end = _block_end(clean, m.end() - 1)
        body = clean[m.end():end]
        attrs = {"kind": f"tf:{kind}"}
        if kind in ("resource", "data"):
            attrs["resource_type"] = a
        if kind == "module":
            sm = _SOURCE.search(body)
            if sm:
                attrs["source"] = sm.group(1)
        nid = node_id(addr)
        res.nodes.append(Node(nid, "config", addr, f"tf:{addr}", project_id, repo_id, rel_path,
                              Location(line_of(m.start()), 0, line_of(end), 0), "terraform", "", attrs, [ev.id],
                              schema_version=2))
        res.edges.append(Edge(edge_id(fid, "contains", nid), fid, nid, "contains", "structural", "deterministic",
                              1.0, [ev.id], "terraform", TF_VERSION, file_path=rel_path,
                              start_line=line_of(m.start())))
        blocks.append((addr, kind, nid, body, m.end()))
    for lm in _LOCALS.finditer(clean):
        end = _block_end(clean, lm.end() - 1)
        for km in re.finditer(r"^\s*([A-Za-z_][\w-]*)\s*=", clean[lm.end():end], re.M):
            addr = f"local.{km.group(1)}"
            nid = node_id(addr)
            off = lm.end() + km.start(1)
            res.nodes.append(Node(nid, "config", addr, f"tf:{addr}", project_id, repo_id, rel_path,
                                  Location(line_of(off), 0, line_of(off), 0), "terraform", "",
                                  {"kind": "tf:local"}, [ev.id], schema_version=2))
    # references inside block bodies: kept on the node; graph/infra_link.py
    # mints `depends_on` once the referenced block is known to exist
    by_id = {n.id: n for n in res.nodes}
    for addr, kind, nid, body, boff in blocks:
        refs = {}
        for r in _REF.finditer(body):
            if r.group(1):
                tgt_addr = r.group(1) + "." + r.group(2)
            elif r.group(3):
                tgt_addr = f"data.{r.group(4)}.{r.group(5)}"
            else:
                tgt_addr = f"{r.group(6)}.{r.group(7)}"
            if tgt_addr != addr:
                refs.setdefault(tgt_addr, line_of(boff + r.start()))
        if refs:
            by_id[nid].attributes = dict(by_id[nid].attributes, references=[{"address": k, "line": v}
                                                                            for k, v in sorted(refs.items())])
    return res
