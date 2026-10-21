"""YAML extraction: keys with their real line numbers, multi-document files,
and the infrastructure the file describes.

Every scalar becomes a `config_key` (dotted path, value, line). Files that
are recognisably infrastructure also get `config` nodes with a `kind`:

  * Docker Compose (`services:`)       -> compose-service (image, build
    context, ports, env file); `depends_on` between services
  * Kubernetes (`apiVersion` + `kind`) -> k8s:<Kind> named by metadata.name
    (container images, configMap/secret refs)
  * GitHub Actions (.github/workflows) -> ci-job (runs-on, needs, the
    `run:` commands); `needs` between jobs
  * GitLab CI (.gitlab-ci.yml)         -> ci-job (script lines, needs)

Cross-file facts (a build context directory, a script a CI step runs)
are kept as attributes and linked to existing files by graph/infra_link.py.
"""
from __future__ import annotations

import re

from ...core.identifiers import content_hash, edge_id, file_node_id, stable_id
from ...core.models import Edge, ExtractionResult, Location, Node
from ...core.provenance import make_evidence

YAML_VERSION = "2"
_GITLAB_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services",
                    "before_script", "after_script", "cache"}


def _scalar(n):
    import yaml
    return n.value if isinstance(n, yaml.ScalarNode) else None


def _to_py(n):
    import yaml
    if isinstance(n, yaml.ScalarNode):
        return n.value
    if isinstance(n, yaml.SequenceNode):
        return [_to_py(x) for x in n.value]
    if isinstance(n, yaml.MappingNode):
        return {str(_to_py(k)): _to_py(v) for k, v in n.value}
    return None


def _get(n, key):
    """(key node, value node) of `key` in a mapping node, else (None, None)."""
    import yaml
    if not isinstance(n, yaml.MappingNode):
        return None, None
    for k, v in n.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None, None


def _items(n):
    import yaml
    return [(k, v) for k, v in n.value if isinstance(k, yaml.ScalarNode)] if isinstance(n, yaml.MappingNode) else []


def _line(n) -> int:
    return (n.start_mark.line + 1) if n is not None and getattr(n, "start_mark", None) else 1


def _end(n) -> int:
    return (n.end_mark.line + 1) if n is not None and getattr(n, "end_mark", None) else _line(n)


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    if isinstance(v, dict):
        return [str(k) for k in v]
    return [str(v)]


def extract_yaml(rel_path, text, repo_id, project_id) -> ExtractionResult:
    res = ExtractionResult(extractor="yaml", extractor_version=YAML_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "yaml", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    fnode = Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path, Location(1, 0, 1, 0), "yaml",
                 content_hash(raw), {}, [ev.id])
    res.nodes.append(fnode)
    try:
        import yaml
    except ImportError:
        res.warnings.append("pyyaml not installed; line-scan fallback")
        for i, line in enumerate(text.splitlines(), 1):
            if ":" in line and not line.strip().startswith("#"):
                k = line.split(":")[0].strip()[:120]
                if k:
                    nid = stable_id("cfg", repo_id, rel_path, k + str(i))
                    res.nodes.append(Node(nid, "config_key", k, k, project_id, repo_id, rel_path,
                                          Location(i, 0, i, len(line)), "yaml", "",
                                          {"value": line.split(":", 1)[1].strip()[:200]}, [ev.id]))
                    res.edges.append(Edge(edge_id(fid, "contains", nid, i), fid, nid, "contains", "structural",
                                          "deterministic", 1.0, [ev.id], "yaml", YAML_VERSION,
                                          file_path=rel_path, start_line=i))
        return res
    try:
        docs = [d for d in yaml.compose_all(text) if d is not None]
    except Exception as e:
        res.warnings.append(f"yaml parse: {str(e)[:200]}")
        res.partial = True
        return res
    seen: set = set()
    multi = len(docs) > 1

    def key_node(key: str, value, line: int):
        nid = stable_id("cfg", repo_id, rel_path, key)
        if nid in seen:
            return
        seen.add(nid)
        res.nodes.append(Node(nid, "config_key", key, key, project_id, repo_id, rel_path, Location(line, 0, line, 0),
                              "yaml", "", {"value": str(value)[:200]}, [ev.id]))
        res.edges.append(Edge(edge_id(fid, "contains", nid), fid, nid, "contains", "structural", "deterministic",
                              1.0, [ev.id], "yaml", YAML_VERSION, file_path=rel_path, start_line=line))

    def walk(prefix: str, n):
        if isinstance(n, yaml.MappingNode):
            for k, v in n.value:
                kk = _to_py(k)
                key = f"{prefix}.{kk}" if prefix else str(kk)
                if isinstance(v, yaml.ScalarNode):
                    key_node(key, v.value, _line(k))
                else:
                    walk(key, v)
        elif isinstance(n, yaml.SequenceNode):
            for i, v in enumerate(n.value):
                if isinstance(v, yaml.ScalarNode):
                    key_node(f"{prefix}[{i}]", v.value, _line(v))
                else:
                    walk(f"{prefix}[{i}]", v)

    for di, d in enumerate(docs):
        walk(f"[{di}]" if multi else "", d)

    infra: dict = {}

    def config(kind: str, name: str, a_node, attrs: dict) -> str:
        nid = stable_id("infra", repo_id, rel_path, f"{kind}:{name}")
        if nid not in seen:
            seen.add(nid)
            res.nodes.append(Node(nid, "config", name, f"{kind}:{name}", project_id, repo_id, rel_path,
                                  Location(_line(a_node), 0, _end(a_node), 0), "yaml", "",
                                  dict({"kind": kind}, **attrs), [ev.id]))
            res.edges.append(Edge(edge_id(fid, "contains", nid), fid, nid, "contains", "structural",
                                  "deterministic", 1.0, [ev.id], "yaml", YAML_VERSION, file_path=rel_path,
                                  start_line=_line(a_node)))
        return nid

    def dep(src: str, kind: str, name: str, line: int):
        tgt = stable_id("infra", repo_id, rel_path, f"{kind}:{name}")
        res.edges.append(Edge(edge_id(src, "depends_on", tgt, line), src, tgt, "depends_on", "structural",
                              "deterministic", 1.0, [ev.id], "yaml", YAML_VERSION, file_path=rel_path,
                              start_line=line))

    low = rel_path.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    for d in docs:
        # ---- Docker Compose
        _k, services = _get(d, "services")
        if services is not None and isinstance(services, yaml.MappingNode) and \
                ("compose" in base or _get(d, "version")[0] is not None or
                 any(_get(v, "image")[0] is not None or _get(v, "build")[0] is not None for _kk, v in _items(services))):
            names = {k.value for k, _v in _items(services)}
            pending = []
            for k, v in _items(services):
                svc = _to_py(v) if isinstance(v, yaml.MappingNode) else {}
                build = svc.get("build")
                ctx = build.get("context") if isinstance(build, dict) else build
                dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
                attrs = {k2: svc[k2] for k2 in ("image", "command", "entrypoint") if isinstance(svc.get(k2), str)}
                if ctx:
                    attrs["build_context"] = str(ctx)
                if dockerfile:
                    attrs["dockerfile"] = str(dockerfile)
                if svc.get("ports"):
                    attrs["ports"] = _as_list(svc.get("ports"))[:10]
                if svc.get("env_file"):
                    attrs["env_file"] = _as_list(svc.get("env_file"))[:5]
                nid = config("compose-service", k.value, k, attrs)
                for other in _as_list(svc.get("depends_on")):
                    if other in names:
                        pending.append((nid, other, _line(k)))
            for nid, other, line in pending:
                dep(nid, "compose-service", other, line)
            infra["compose"] = True
        # ---- Kubernetes
        _ka, api = _get(d, "apiVersion")
        _kk, kind = _get(d, "kind")
        if _scalar(api) and _scalar(kind):
            meta = _to_py(_get(d, "metadata")[1]) or {}
            name = meta.get("name") if isinstance(meta, dict) else None
            if name:
                spec = _to_py(d) or {}
                images = sorted(set(re.findall(r"'image': '([^']+)'", str(spec))))[:10]
                refs = sorted(set(re.findall(r"'(?:configMapRef|secretRef|configMapKeyRef|secretKeyRef)': \{'name': '([^']+)'",
                                             str(spec))))[:10]
                attrs = {"api_version": _scalar(api)}
                if images:
                    attrs["images"] = images
                if refs:
                    attrs["config_refs"] = refs
                config(f"k8s:{_scalar(kind)}", str(name), d, attrs)
                infra["kubernetes"] = True
        # ---- GitHub Actions
        _kj, jobs = _get(d, "jobs")
        if "/.github/workflows/" in "/" + low and isinstance(jobs, yaml.MappingNode):
            wf_name = _scalar(_get(d, "name")[1]) or base
            jnames = {k.value for k, _v in _items(jobs)}
            pending = []
            for k, v in _items(jobs):
                job = _to_py(v) if isinstance(v, yaml.MappingNode) else {}
                runs = []
                for st in job.get("steps") or []:
                    if isinstance(st, dict) and isinstance(st.get("run"), str):
                        runs.extend(x.strip() for x in st["run"].splitlines() if x.strip())
                attrs = {"workflow": wf_name, "runs": runs[:40]}
                if job.get("runs-on"):
                    attrs["runs_on"] = _as_list(job.get("runs-on"))[:3]
                nid = config("ci-job", k.value, k, attrs)
                for other in _as_list(job.get("needs")):
                    if other in jnames:
                        pending.append((nid, other, _line(k)))
            for nid, other, line in pending:
                dep(nid, "ci-job", other, line)
            infra["ci"] = "github-actions"
        # ---- GitLab CI
        if base in (".gitlab-ci.yml", ".gitlab-ci.yaml") and isinstance(d, yaml.MappingNode):
            pending, jnames = [], set()
            for k, v in _items(d):
                if k.value in _GITLAB_RESERVED or k.value.startswith(".") or not isinstance(v, yaml.MappingNode):
                    continue
                job = _to_py(v) or {}
                if "script" not in job:
                    continue
                jnames.add(k.value)
                runs = [str(x) for x in (job.get("script") if isinstance(job.get("script"), list) else [job.get("script")])
                        if x is not None]
                nid = config("ci-job", k.value, k, {"workflow": "gitlab-ci", "runs": runs[:40],
                                                    "stage": job.get("stage") or ""})
                for other in _as_list(job.get("needs")):
                    pending.append((nid, other, _line(k)))
            for nid, other, line in pending:
                if other in jnames:
                    dep(nid, "ci-job", other, line)
            infra["ci"] = "gitlab-ci"
    if infra:
        fnode.attributes = {"infra": infra}
    return res
