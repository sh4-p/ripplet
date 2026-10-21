"""Architecture rules checked against the graph: invariants a change must keep.

Rules live in the repository (`ripplet.arch.yaml` at a workspace root, or
`.ripplet/arch.yaml`), are reviewed like code, and are evaluated on the
indexed graph, never on text:

    rules:
      - id: admin-needs-auth
        must_call: [requireAdmin, Auth.check]    # any of these names
        for: {path: "app/Controllers/Admin/**", type: [method, function], public: true}
        depth: 2                                  # direct or via N-1 helpers
        severity: error
        message: Admin actions must check the admin role.
      - id: routes-need-auth
        must_call: authenticate
        for: {route: "/api/admin/**"}             # the handlers of these routes
      - id: ui-not-db
        forbid: {from: "src/ui/**", to: "src/db/**"}   # no call/import/reference
        except: ["src/ui/dev/**"]

`must_call` holds when one of the names is reached through calls within
`depth` steps (or, for a route, appears in its middleware attributes).
`forbid` is a layering rule over file-level dependencies. Every violation
names the file:line it is about. A rule that matches nothing is reported as
such, so a typo cannot silently turn a rule off.
"""
from __future__ import annotations

import json
import re
from collections import deque
from fnmatch import fnmatchcase
from pathlib import Path

RULE_FILES = ("ripplet.arch.yaml", "ripplet.arch.yml", ".ripplet/arch.yaml", ".ripplet/arch.yml")
_CALL_RELS = ("calls", "instantiates", "dispatches")
_DEP_RELS = ("calls", "instantiates", "references", "imports", "depends_on", "inherits", "implements",
             "dispatches", "calls_http")
_SEVERITIES = ("error", "warning", "note")


class RuleError(ValueError):
    pass


def _glob(pat: str, path: str) -> bool:
    """`src/ui/**` matches the folder's files at any depth; `*` stays in one folder."""
    pat = pat.strip().lstrip("./")
    if pat.endswith("/**"):
        return path == pat[:-3] or path.startswith(pat[:-2])
    rx = re.escape(pat).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(rx.replace(r"\?", "[^/]"), path) is not None or fnmatchcase(path, pat)


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def find_rules_file(roots) -> Path | None:
    for r in roots or []:
        for name in RULE_FILES:
            p = Path(r) / name
            if p.is_file():
                return p
    return None


def load_rules(roots) -> tuple[list, Path | None]:
    p = find_rules_file(roots)
    if p is None:
        return [], None
    text = p.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 - a broken rules file must say so, not pass
        raise RuleError(f"{p.name}: {e}") from e
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        raise RuleError(f"{p.name}: expected a `rules:` list")
    out = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise RuleError(f"{p.name}: rule #{i + 1} is not a mapping")
        rid = str(r.get("id") or f"rule-{i + 1}")
        kinds = [k for k in ("must_call", "forbid") if k in r]
        if len(kinds) != 1:
            raise RuleError(f"{p.name}: rule {rid} needs exactly one of must_call / forbid")
        sev = str(r.get("severity", "error"))
        if sev not in _SEVERITIES:
            raise RuleError(f"{p.name}: rule {rid}: severity must be one of {', '.join(_SEVERITIES)}")
        out.append(dict(r, id=rid, kind=kinds[0], severity=sev))
    return out, p


_NON_PUBLIC = re.compile(r"\b(private|protected|internal|fileprivate)\b|^\s*(def|func|fn)\s+_")


class _Graph:
    def __init__(self, cx, roots=()):
        self.roots = [str(r) for r in roots or ()]
        self._lines: dict = {}
        self.nodes = {}
        for nid, typ, lab, qn, fp, sl, el, attrs in cx.execute(
                "SELECT id, type, label, qualified_name, file_path, start_line, end_line, attributes FROM nodes"):
            self.nodes[nid] = {"type": typ, "label": lab or "", "qn": qn or "", "file": fp or "",
                               "line": sl or 0, "end": el or 0, "attrs": attrs or ""}
        self.out: dict = {}
        self.inc: dict = {}
        for s, t, rel, fp, ln in cx.execute(
                "SELECT source, target, relation, file_path, start_line FROM edges "
                "WHERE id NOT IN (SELECT edge_id FROM superseded_edges)"):
            self.out.setdefault(s, []).append((t, rel, fp or "", ln or 0))
            self.inc.setdefault(t, []).append((s, rel, fp or "", ln or 0))


    def head(self, n: dict) -> str:
        """The declaration line of a node (for visibility keywords)."""
        f = n["file"]
        if f not in self._lines:
            self._lines[f] = []
            for r in self.roots:
                p = Path(r) / f
                if p.is_file():
                    try:
                        self._lines[f] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        pass
                    break
        ls = self._lines[f]
        return ls[n["line"] - 1] if 1 <= n["line"] <= len(ls) else ""


def _public(g: "_Graph", n: dict) -> bool:
    if n["label"].startswith("_") and not n["label"].startswith("__"):
        return False
    a = n["attrs"]
    if '"visibility": "private"' in a or '"visibility": "protected"' in a or '"exported": false' in a:
        return False
    return not _NON_PUBLIC.search(g.head(n))


def _named(n: dict, names: list) -> bool:
    for nm in names:
        if n["label"] == nm or n["qn"] == nm or n["qn"].endswith("." + nm) or \
                n["qn"].endswith("::" + nm) or n["qn"].endswith("\\" + nm):
            return True
    return False


def _subjects(g: _Graph, sel: dict) -> list:
    """Node ids a `for:` selector picks; routes resolve to their handlers."""
    sel = sel or {}
    paths, types = _as_list(sel.get("path")), _as_list(sel.get("type")) or ["function", "method"]
    name_rx = re.compile(sel["name"]) if sel.get("name") else None
    if sel.get("route"):
        pats = _as_list(sel["route"])
        verbs = {v.upper() for v in _as_list(sel.get("method"))}
        out = []
        for nid, n in g.nodes.items():
            if n["type"] != "route":
                continue
            if verbs and n["label"].split(" ", 1)[0].upper() not in verbs:
                continue
            path = n["label"].split(" ", 1)[-1]
            if any(_glob(p.lstrip("/"), path.lstrip("/")) for p in pats):
                out.append(nid)
        return out
    out = []
    for nid, n in g.nodes.items():
        if n["type"] not in types or not n["file"] or nid.startswith("ext:"):
            continue
        if paths and not any(_glob(p, n["file"]) for p in paths):
            continue
        if name_rx and not name_rx.search(n["label"]):
            continue
        if sel.get("public") and not _public(g, n):
            continue
        out.append(nid)
    return out


def _reaches(g: _Graph, start: str, names: list, depth: int) -> bool:
    seen, dq = {start}, deque([(start, 0)])
    while dq:
        x, d = dq.popleft()
        if d >= depth:
            continue
        for t, rel, _f, _l in g.out.get(x, ()):
            if rel not in _CALL_RELS and not (rel == "handles_route" and g.nodes.get(x, {}).get("type") == "route"):
                continue
            n = g.nodes.get(t)
            if n is None:
                continue
            if rel in _CALL_RELS and _named(n, names):
                return True
            if t not in seen:
                seen.add(t)
                # a route's handler is the route itself, one level down: no step spent
                dq.append((t, d if rel == "handles_route" else d + 1))
    return False


def _excepted(pat: str, n: dict) -> bool:
    """`except:` entry vs a node: a path glob, `Class::method`, `Class.method`,
    `file.php:method` or a bare method name."""
    pat = pat.strip()
    cls = n["qn"].replace("\\", ".").rsplit(".", 2)[-2] if n["qn"].count(".") or "\\" in n["qn"] else ""
    m = re.match(r"^(.+?)(?:::|:|\.)(\w+)$", pat)
    if m and n["label"] == m.group(2):
        owner = m.group(1)
        if owner == cls or owner.endswith("/" + n["file"].rsplit("/", 1)[-1]) or owner == n["file"] \
                or n["file"].endswith(owner) or _glob(owner, n["file"]):
            return True
    return pat == n["label"] or _glob(pat, n["file"])


def _body_matches(g: _Graph, n: dict, rx) -> bool:
    if not n["file"] or not n["line"]:
        return False
    g.head(n)                                  # loads the file's lines
    lines = g._lines.get(n["file"]) or []
    end = n.get("end") or (n["line"] + 80)
    return any(rx.search(t) for t in lines[n["line"] - 1:end])


def _check_must_call(g: _Graph, rule: dict) -> tuple[list, int]:
    names = _as_list(rule["must_call"])
    depth = int(rule.get("depth", 1))
    subjects = _subjects(g, rule.get("for") or {})
    excepts = _as_list(rule.get("except"))
    inline_rx = re.compile(rule["inline"]) if rule.get("inline") else None
    out = []
    for nid in subjects:
        n = g.nodes[nid]
        # what an exception may name: the file, `Class::method`, `file.php:method`;
        # for a route also its handler (routes live in one routes file)
        targets = [n] + ([g.nodes[t] for t, rel, _f, _l in g.out.get(nid, ())
                          if rel == "handles_route" and t in g.nodes] if n["type"] == "route" else [])
        if any(_excepted(p, x) for p in excepts for x in targets):
            continue
        if n["type"] == "route" and any(nm in n["attrs"] for nm in names):
            continue  # route-level middleware (`->middleware('auth')`, `Depends(auth)`)
        if _named(n, names) or _reaches(g, nid, names, depth):
            continue
        if inline_rx and any(_body_matches(g, x, inline_rx) for x in targets if x["type"] != "route"):
            continue  # an inline role test (`if ($ws['my_role'] ...)`) the rule accepts
        what = f"route {n['label']}" if n["type"] == "route" else f"{n['type']} {n['qn'].rsplit('.', 1)[-1]}"
        handler = next((x for x in targets if x["type"] != "route"), None)
        if handler is not None and handler is not n:
            what += f" ({handler['qn'].replace(chr(92), '.').rsplit('.', 2)[-2]}::{handler['label']}"
            what += f" @ {handler['file']}:{handler['line']})"
        out.append({"rule": rule["id"], "severity": rule["severity"], "file": n["file"], "line": n["line"],
                    "node": nid,
                    "message": ((rule.get("message") or "").strip() + f" [{what}]") if rule.get("message") else
                    f"{what} does not call {' or '.join(names)}"
                    + (f" (within {depth} steps)." if depth > 1 else "."),
                    "detail": f"{what} must call {' or '.join(names)}"})
    return out, len(subjects)


def _check_forbid(g: _Graph, rule: dict) -> tuple[list, int]:
    spec = rule["forbid"] or {}
    src_p, dst_p = _as_list(spec.get("from")), _as_list(spec.get("to"))
    if not src_p or not dst_p:
        raise RuleError(f"rule {rule['id']}: forbid needs `from` and `to`")
    excepts = _as_list(rule.get("except"))
    out, seen, subjects = [], set(), 0
    for s, edges in g.out.items():
        sn = g.nodes.get(s)
        if sn is None or not sn["file"] or not any(_glob(p, sn["file"]) for p in src_p):
            continue
        if any(_glob(p, sn["file"]) for p in excepts):
            continue
        subjects += 1
        for t, rel, fp, ln in edges:
            tn = g.nodes.get(t)
            if rel not in _DEP_RELS or tn is None or not tn["file"] or t.startswith("ext:"):
                continue
            if tn["file"] == sn["file"] or not any(_glob(p, tn["file"]) for p in dst_p):
                continue
            at_f, at_l = (fp or sn["file"]), (ln or sn["line"])
            key = (at_f, at_l, tn["file"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"rule": rule["id"], "severity": rule["severity"], "file": at_f, "line": at_l,
                        "node": s,
                        "message": (rule.get("message") or "").strip() or
                        f"{sn['file']} must not depend on {tn['file']} ({rel} {tn['label']}).",
                        "detail": f"{rel} -> {tn['qn'] or tn['label']}"})
    return out, subjects


def evaluate(cx, rules: list, files: set | None = None, roots=()) -> dict:
    """All violations (or only those located in `files`), plus rules that matched nothing."""
    g = _Graph(cx, roots)
    violations, empty = [], []
    for r in rules:
        fn = _check_must_call if r["kind"] == "must_call" else _check_forbid
        vs, n_subjects = fn(g, r)
        if n_subjects == 0:
            empty.append(r["id"])
        if files is not None:
            vs = [v for v in vs if v["file"] in files]
        violations.extend(vs)
    violations.sort(key=lambda v: (-_SEVERITIES[::-1].index(v["severity"]), v["file"], v["line"]))
    return {"violations": violations, "rules": len(rules), "matched_nothing": empty}


def check_rules(cfg, files: set | None = None) -> dict:
    from ..storage.sqlite_store import SQLiteStore
    roots = [str(r) for r in (cfg.workspace_roots or [])]
    try:
        rules, path = load_rules(roots)
    except RuleError as e:
        return {"error": str(e), "violations": [], "rules": 0, "matched_nothing": []}
    st = SQLiteStore(cfg.storage_path)
    try:
        r = evaluate(st.cx, rules, files, roots) if rules else             {"violations": [], "rules": 0, "matched_nothing": []}
        r["builtin"] = _access_consistency(st, cfg, path, files)
    finally:
        st.close()
    r["violations"] = r["violations"] + r["builtin"]
    r["file"] = str(path) if path else None
    return r


def _access_consistency(store, cfg, path, files) -> list:
    """Built-in check, on without a rules file: a write route with no access
    check, writing shared rows, in a controller whose other write routes do
    check. A rule's own `except:` does not silence it (an agent once put a real
    gap there); `access_consistency_except:` does, `access_consistency: false`
    turns it off."""
    cfgd = {}
    if path is not None:
        try:
            import yaml
            cfgd = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - the rules loader already reported a broken file
            cfgd = {}
    if isinstance(cfgd, dict) and cfgd.get("access_consistency") is False:
        return []
    allowed = _as_list(cfgd.get("access_consistency_except")) if isinstance(cfgd, dict) else []
    try:
        from ..retrieval.routes import _collect, access_gaps
        gaps = access_gaps(_collect(store, cfg, "write"))
    except Exception:  # noqa: BLE001 - a graph without routes has nothing to check
        return []
    out = []
    for g in gaps:
        n = {"qn": g["handler"], "label": g["handler"].rsplit(".", 1)[-1], "file": g["file"]}
        if any(_excepted(p, n) for p in allowed):
            continue
        if files is not None and g["file"] not in files:
            continue
        cls = g["handler"].rsplit(".", 2)[-2] if g["handler"].count(".") else ""
        out.append({"rule": "access-consistency", "severity": "warning", "file": g["file"], "line": g["line"],
                    "node": "", "message": f"{g['route']} ({cls}::{g['handler'].rsplit('.', 1)[-1]}) has no "
                    f"access check but {g['guarded']} of {g['total']} write routes of its controller do, and it "
                    f"{'; '.join(w.replace('writes: ', 'writes ') for w in g['writes'])}.",
                    "detail": "built-in check (access_consistency); allow with access_consistency_except"})
    return out


def render(r: dict) -> str:
    if r.get("error"):
        return f"arch: {r['error']}"
    if not r.get("rules"):
        head = ("arch: no rules file (add ripplet.arch.yaml: must_call / forbid rules, "
                "see `ripplet arch --example`).")
        if not r.get("builtin"):
            return head + " Built-in checks: no findings."
        return "\n".join([head, f"Built-in checks: {len(r['builtin'])} finding(s)"] +
                         [f"  {v['severity']:7s} [{v['rule']}] {v['file']}:{v['line']}  {v['message']}"
                          for v in r["builtin"]])
    lines = [f"arch: {r['rules']} rule(s) from {Path(r['file']).name}; {len(r['violations'])} violation(s)"]
    for v in r["violations"][:60]:
        lines.append(f"  {v['severity']:7s} [{v['rule']}] {v['file']}:{v['line']}  {v['message']}")
    if len(r["violations"]) > 60:
        lines.append(f"  ... {len(r['violations']) - 60} more")
    for rid in r.get("matched_nothing") or []:
        lines.append(f"  warning [{rid}] matches nothing in the graph: check its paths/names")
    return "\n".join(lines)


EXAMPLE = """# ripplet.arch.yaml: architecture rules, checked by `ripplet arch`,
# `ripplet review` and check_change on every change
rules:
  - id: admin-needs-auth
    must_call: [requireAdmin]
    for: {path: "src/admin/**", type: [function, method], public: true}
    depth: 2
    severity: error
    message: Admin actions must check the admin role.
  - id: ui-not-db
    forbid: {from: "src/ui/**", to: "src/db/**"}
    severity: error
"""
