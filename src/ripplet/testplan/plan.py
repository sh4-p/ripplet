"""Test plan assembly (Phase 9 Stage 8).

Deterministic plan IDs (diff hash + mode + config); wording reflects evidence
("directly observed test candidate", "statically related test", "inferred
candidate") — never "will fail" / "fully covers". Read-only throughout.
"""
from __future__ import annotations

from ..storage.sqlite_store import NOT_SUPERSEDED
import hashlib
import json
import time

from . import discovery as disc
from . import ranking as rk

CATEGORY_LABEL = {"A": "directly observed test candidate",
                  "B": "statically related test",
                  "C": "inferred candidate"}


def plan_id(change_digest: str, mode: str) -> str:
    return "plan:" + hashlib.sha256(f"{change_digest}\x00{mode}".encode()).hexdigest()[:12]


def build(store, repo_id: str, repo_root: str, changed_files: list,
          mapped: dict, mode: str = "balanced", revision: str = "",
          change_digest: str = "") -> dict:
    """Assemble a plan from mapped changes. `mapped` is changes.map_to_symbols
    output; deleted files are handled here via dangling references."""
    if mode not in rk.MODES:
        raise ValueError(f"unknown mode: {mode}")
    discovered = disc.discover(store, mapped.get("symbols", {}), repo_id)
    if len(discovered) == 3:
        cands, limitations, discovery_diag = discovered
    else:  # backwards compatibility with older discovery tuples
        cands, limitations = discovered
        discovery_diag = {}
    # deleted files: find tests referencing now-dangling placeholders
    deleted_notes = []
    for rel in mapped.get("deleted_files", []):
        refs = _dangling_test_refs(store, repo_id, rel)
        for tid, why in refs:
            node = store.get_node(tid)
            if node is None:
                continue
            if tid not in {c["test_id"] for c in cands}:
                cands.append({"test_id": tid, "test": node.qualified_name,
                              "file": node.file_path, "category": "C",
                              "evidence": {"kind": "deleted_module_reference",
                                           "note": why},
                              "path": [], "depth": 1, "via": "inferred",
                              "run_id": "", "revision_ok": None})
        deleted_notes.append({"file": rel, "referencing_tests": len(refs),
                              "note": "deleted in working tree; candidates inferred "
                                      "from dangling references, not current edges"})
    # renames: surface both identities with uncertainty preserved
    rename_notes = []
    try:
        for r in store.cx.execute(
                "SELECT id, attributes FROM nodes WHERE repository_id=?", (repo_id,)):
            try:
                attrs = json.loads(r["attributes"] or "{}")
            except Exception:
                attrs = {}
            if attrs.get("tombstoned"):
                rename_notes.append({"tombstone": r["id"],
                                     "renames_to": attrs.get("renames_to", "")})
    except Exception:
        pass
    admitted, excluded = rk.rank(cands, mode)
    counts = {c: sum(1 for x in admitted if x["category"] == c) for c in "ABC"}
    unresolved = _unresolved_relationships(store, mapped.get("symbols", {}))
    undiscovered = _undiscovered_tests(store, repo_id, mapped)
    digest_src = change_digest or json.dumps(
        {"files": [(f.rel, f.status) for f in changed_files],
         "symbols": sorted(mapped.get("symbols", {}))}, sort_keys=True)
    return {
        "plan_id": plan_id(digest_src, mode),
        "mode": mode,
        "repository": repo_id,
        "revision": revision,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "changed_files": [{"file": f.rel, "status": f.status,
                           "old": f.old_rel or "",
                           "ranges": f.new_ranges} for f in changed_files],
        "changed_symbols": [
            {"id": sid, **{k: v for k, v in info.items()}}
            for sid, info in sorted(mapped.get("symbols", {}).items())],
        "unmapped_files": mapped.get("unmapped_files", []),
        "deleted": deleted_notes,
        "renames_noted": rename_notes,
        "candidates": [{**c, "category_label": CATEGORY_LABEL.get(c["category"], "?")}
                       for c in admitted],
        "excluded": [{"test": c["test"], "category": c["category"],
                      "reason": c.get("admission", "")} for c in excluded],
        "counts": {"by_category": counts, "admitted": len(admitted),
                   "excluded": len(excluded)},
        "unresolved_relationships": unresolved,
        "undiscovered_tests": undiscovered,
        "budgets": {"max_depth": disc.BUDGETS["max_depth"],
                    "max_candidates": disc.BUDGETS["max_candidates"],
                    "discovery": discovery_diag},
        "limitations": limitations + _standard_limitations(mapped),
        "validation_steps": [
            "run the admitted candidates with the project's own test runner",
            "treat inferred candidates as leads, not verdicts",
            "re-run test-plan after applying fixes; candidates may change",
        ],
    }


def _unresolved_relationships(store, symbols: dict) -> list[dict]:
    """calls_unresolved / ambiguous edges touching changed symbols.

    Besides direct endpoint matches, an unresolved placeholder whose name
    contains a changed symbol's label is reported (ambiguous-import case):
    the edge cannot verify its target, so it is listed here rather than
    becoming a verified path.
    """
    out: list[dict] = []
    try:
        rows = store.cx.execute(
            "SELECT id, source, target, relation, file_path, start_line, "
            "attributes FROM edges WHERE relation='calls_unresolved' AND "
            + NOT_SUPERSEDED).fetchall()
    except Exception:
        return out
    syms = set(symbols)
    labels: set[str] = set()
    try:
        batch = store.nodes_by_ids(syms)
    except Exception:
        batch = None
    for sid in syms:
        try:
            n = batch.get(sid) if batch is not None \
                else store.get_node(sid)
            if n is not None and n.label:
                labels.add(n.label)
        except Exception:
            continue
    lowered = sorted({lbl.lower() for lbl in labels if lbl})
    for r in rows:
        d = dict(r)
        hit = d["source"] in syms or d["target"] in syms
        if not hit and lowered:
            tgt = d["target"] or ""
            name = tgt.split(":")[-1].split(".")[-1].lower()
            tgt_low = tgt.lower()
            if any(ll in (name, tgt_low) or name in ll for ll in lowered):
                hit = True
                d["note"] = "placeholder name matches a changed symbol label"
        if hit:
            item = {"edge": d["id"], "relation": d["relation"],
                    "source": d["source"], "target": d["target"],
                    "location": [d["file_path"], d["start_line"]]}
            if d.get("note"):
                item["note"] = d["note"]
            try:
                import json as _j
                attrs = _j.loads(d.get("attributes") or "{}")
                if "ambiguous" in str(attrs).lower():
                    item["ambiguous"] = True
            except Exception:
                pass
            out.append(item)
    return out[:50]


def _undiscovered_tests(store, repo_id: str, mapped: dict) -> list[str]:
    """Changed files with no indexed test declarations at all."""
    out: list[str] = []
    files = {v.get("file", "") for v in mapped.get("symbols", {}).values()}
    files |= set(mapped.get("unmapped_files", []))
    for rel in sorted(files):
        if not rel:
            continue
        try:
            rows = store.cx.execute(
                "SELECT COUNT(*) c FROM nodes WHERE repository_id=? AND "
                "file_path=? AND type='test'", (repo_id, rel)).fetchone()
            if (rows["c"] or 0) == 0:
                # any test in the same directory?
                out.append(rel)
        except Exception:
            continue
    return out[:20]


def _dangling_test_refs(store, repo_id: str, deleted_rel: str) -> list[tuple[str, str]]:
    """Tests referencing placeholders named after a deleted file's symbols."""
    out = []
    stem = deleted_rel.rsplit("/", 1)[-1].split(".")[0]
    try:
        rows = store.cx.execute(
            "SELECT * FROM edges WHERE relation IN ('calls_unresolved','imports','uses')"
            " AND " + NOT_SUPERSEDED).fetchall()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        tgt = d["target"] or ""
        if not tgt.startswith("ext:"):
            continue
        name = tgt.split(":", 2)[-1]
        if stem and (stem in name or name.split(".")[0] in deleted_rel):
            src = store.get_node(d["source"])
            if src is not None and (src.type == "test" or "test" in (src.file_path or "")):
                out.append((src.id, f"references placeholder {name!r} from deleted {deleted_rel}"))
    # module-name heuristic: test files importing the deleted module path
    for r in rows:
        d = dict(r)
        try:
            attrs = json.loads(d["attributes"] or "{}")
        except Exception:
            attrs = {}
        spec = attrs.get("specifier", "") or attrs.get("imported", "")
        if spec and (deleted_rel in spec or stem in spec):
            src = store.get_node(d["source"])
            if src is not None and "test" in (src.file_path or ""):
                out.append((src.id, f"imports {spec!r} from deleted {deleted_rel}"))
    seen, deduped = set(), []
    for tid, why in out:
        if tid not in seen:
            seen.add(tid)
            deduped.append((tid, why))
    return deduped


def _standard_limitations(mapped: dict) -> list[str]:
    lims = []
    if mapped.get("unmapped_files"):
        lims.append("files without symbol mapping (reported at file level): " +
                    ", ".join(mapped["unmapped_files"][:10]))
    if mapped.get("deleted_files"):
        lims.append("deleted files have no current symbols; candidates use "
                    "dangling references and historical evidence where available")
    lims.append("a candidate's presence never claims it will fail or that the "
                "set fully covers the change")
    return lims


def to_markdown(plan: dict) -> str:
    L = [f"# Test plan `{plan['plan_id']}` ({plan['mode']})", "",
         f"Repository: `{plan['repository']}`  revision: `{plan['revision'] or 'working tree'}`",
         f"Changed files: {len(plan['changed_files'])}, "
         f"changed symbols: {len(plan['changed_symbols'])}",
         f"Counts: {plan['counts']}", ""]
    if plan["changed_symbols"]:
        L += ["## Changed symbols", ""]
        for s in plan["changed_symbols"][:30]:
            L.append(f"- `{s['id']}` ({s.get('change')}, via {s.get('via')})")
        L.append("")
    L += ["## Candidates (priority order)", ""]
    for c in plan["candidates"]:
        L.append(f"{c['rank']}. `{c['test']}` — *{c['category_label']}* "
                 f"(depth {c.get('depth')}, {c.get('rationale', '')})")
        if c.get("path"):
            L.append(f"   path: {' → '.join(c['path'][:6])}")
        if c.get("run_id"):
            L.append(f"   coverage run: `{c['run_id']}` "
                     f"(revision compatible: {c.get('revision_ok')})")
    if plan["excluded"]:
        L += ["", "## Excluded by mode", ""]
        for c in plan["excluded"][:20]:
            L.append(f"- `{c['test']}` [{c['category']}]: {c['reason']}")
    if plan["limitations"]:
        L += ["", "## Limitations", ""]
        L += [f"- {x}" for x in plan["limitations"]]
    if plan["deleted"]:
        L += ["", "## Deleted files", ""]
        L += [f"- `{d['file']}`: {d['note']}" for d in plan["deleted"]]
    L += ["", "## Validation steps", ""]
    L += [f"- {x}" for x in plan["validation_steps"]]
    return "\n".join(L) + "\n"
