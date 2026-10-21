"""Infrastructure -> repository files.

`config` nodes from YAML (extraction/structured/yaml_ext.py) carry
cross-file facts as attributes; this pass turns the ones that name an
existing file into `references` edges, so "what runs scripts/deploy.sh"
or "which service builds from api/" have answers:

  * compose-service `build_context` -> the context's Dockerfile (or the
    `dockerfile` it names), `env_file` -> those files
  * ci-job `runs` lines -> scripts they execute (`./scripts/x.sh`,
    `bash x.sh`, `python tools/y.py`, `node z.js`, ...)

Only files present in the index are linked. Recomputed every index run.
"""
from __future__ import annotations

import json
import posixpath
import re

from ..core.identifiers import canonical_edge_id, file_node_id
from ..core.models import Edge

METHOD = "infra-link"
VERSION = "1"
_SCRIPT = re.compile(r"(?:^|[\s;&|(])(?:\./|(?:bash|sh|zsh|python3?|node|ruby|php|pwsh|powershell|deno|bun)\s+)"
                     r"((?:\.{0,2}/)?[\w./-]+\.(?:sh|py|js|mjs|ts|rb|php|ps1))\b")


def link_infra(store) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
    files = {(r[0], r[1].replace("\\", "/")) for r in cx.execute(
        "SELECT repository_id, file_path FROM nodes WHERE type='file' AND file_path<>''")}
    new = []

    def link(src, rid, from_rel, target_rel, line, via):
        target_rel = posixpath.normpath(target_rel).lstrip("./") if target_rel else ""
        if (rid, target_rel) not in files:
            return
        tgt = file_node_id(rid, target_rel)
        e = Edge(canonical_edge_id(src, "references", tgt, from_rel, via), src, tgt, "references", "resolved",
                 "resolved", 0.85, [], METHOD, VERSION, file_path=from_rel, start_line=line,
                 attributes={"via": via})
        new.append((e, f"{rid}:{from_rel}"))

    # Terraform blocks are addressed per directory (`aws_s3_bucket.logs` in any .tf of it)
    tf_blocks = {}
    for nid, rid, fp, lab, attrs in cx.execute(
            "SELECT id, repository_id, file_path, label, attributes FROM nodes WHERE type='config' "
            "AND qualified_name LIKE 'tf:%'"):
        tf_blocks[(rid, posixpath.dirname((fp or "").replace("\\", "/")), lab)] = nid
    by_dir = {}
    for rid, f in files:
        by_dir.setdefault((rid, posixpath.dirname(f)), []).append(f)

    def depend(src, rid, from_rel, tgt, line, ref):
        e = Edge(canonical_edge_id(src, "depends_on", tgt, from_rel, ref), src, tgt, "depends_on", "resolved",
                 "resolved", 0.9, [], METHOD, VERSION, file_path=from_rel, start_line=line,
                 attributes={"via": "terraform-reference", "reference": ref})
        new.append((e, f"{rid}:{from_rel}"))

    for nid, rid, fp, sl, attrs in cx.execute(
            "SELECT id, repository_id, file_path, start_line, attributes FROM nodes WHERE type='config'"):
        try:
            a = json.loads(attrs or "{}")
        except ValueError:
            continue
        kind = a.get("kind", "")
        here = posixpath.dirname((fp or "").replace("\\", "/"))
        if kind == "compose-service":
            ctx = a.get("build_context")
            if ctx:
                cdir = posixpath.join(here, ctx)
                link(nid, rid, fp, posixpath.join(cdir, a.get("dockerfile") or "Dockerfile"), sl, "compose-build")
            for env in a.get("env_file") or []:
                link(nid, rid, fp, posixpath.join(here, env), sl, "compose-env-file")
        elif kind.startswith("tf:"):
            for ref in a.get("references") or []:
                tgt = tf_blocks.get((rid, here, ref.get("address")))
                if tgt and tgt != nid:
                    depend(nid, rid, fp, tgt, ref.get("line") or sl, ref.get("address"))
            src_ = a.get("source") or ""
            if kind == "tf:module" and src_.startswith(("./", "../")):
                mdir = posixpath.normpath(posixpath.join(here, src_))
                for f in sorted(by_dir.get((rid, mdir), [])):
                    if f.endswith(".tf"):
                        link(nid, rid, fp, f, sl, "terraform-module")
        elif kind == "ci-job":
            for line in a.get("runs") or []:
                for m in _SCRIPT.finditer(" " + line):
                    link(nid, rid, fp, m.group(1), sl, "ci-runs")
    if new:
        store.upsert_edges([e for e, _ in new])
        for e, fk in new:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    return {"edges": len(new)}
