"""Python import-resolution benchmark runner (Phase 12 Priority 1).

Evaluates a working tree (inline repo + optional mutation) against
hand-annotated expectations: resolved edges to real local symbols,
unresolved/external/ambiguous handling, file-level depends_on, locations,
and incremental/full-rebuild equivalence for mutation cases. Ground truth
is source-read; the resolver is never consulted to build expectations.
"""
from __future__ import annotations
import json
import shutil
import time
from pathlib import Path


def load_dataset(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_files(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _attrs(e: dict) -> dict:
    a = e.get("attributes", {})
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:
        return {}


def _strip(snap: dict) -> dict:
    """Same documented exclusions as test_phase2_equivalence._strip."""
    import copy
    s = copy.deepcopy(snap)
    for _path, m in s.get("manifests", {}).items():
        if isinstance(m, dict):
            m.pop("cfg", None)
    s["evidence"] = {eid: e for eid, e in s.get("evidence", {}).items()
                     if e.get("method") != "rename-detector"}
    tomb = {nid for nid, n in s.get("nodes", {}).items()
            if (n.get("attrs") or {}).get("tombstoned")}
    if isinstance(s.get("fts"), list):
        s["fts"] = [fid for fid in s["fts"] if fid not in tomb]
    for nid in tomb:
        s.get("nodes", {}).pop(nid, None)
    s["edges"] = {eid: e for eid, e in s.get("edges", {}).items()
                  if e.get("relation") != "renames_to"
                  and e.get("source") not in tomb and e.get("target") not in tomb}
    return s


def run_case(case: dict, tmp: Path) -> dict:
    from ..config import RippletConfig
    from ..service import index_workspace, normalized_snapshot
    from ..storage.sqlite_store import SQLiteStore

    ds_files = run_case.repo_files
    proj = tmp / case["id"]
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    _write_files(proj, ds_files)
    # Databases live OUTSIDE the project: the scanner must never index
    # the graph database, its WAL file, or lock files as source files.
    db = tmp / f"{case['id']}.db"
    mut = case.get("mutate") or {}
    if not mut:
        cfg = RippletConfig([str(proj)], str(db), "bench")
        t0 = time.time()
        index_workspace(cfg)
        dt = round(time.time() - t0, 3)
        st = SQLiteStore(str(db))
        try:
            return _evaluate(case, st, None, dt)
        finally:
            st.close()
    # Mutation case: incremental path vs fresh full rebuild after mutation.
    cfg = RippletConfig([str(proj)], str(db), "bench")
    index_workspace(cfg)
    for rel in mut.get("delete", []) or []:
        (proj / rel).unlink(missing_ok=True)
    for old, new in mut.get("rename", []) or []:
        (proj / new).parent.mkdir(parents=True, exist_ok=True)
        (proj / old).rename(proj / new)
    t0 = time.time()
    index_workspace(cfg)
    dt = round(time.time() - t0, 3)
    st = SQLiteStore(str(db))
    try:
        snap_incr = _strip(normalized_snapshot(st))
        res = _evaluate(case, st, None, dt)
    finally:
        st.close()
    db2 = tmp / f"{case['id']}-full.db"
    cfg2 = RippletConfig([str(proj)], str(db2), "bench")
    index_workspace(cfg2)
    st2 = SQLiteStore(str(db2))
    try:
        snap_full = _strip(normalized_snapshot(st2))
    finally:
        st2.close()
    res["equivalent"] = (snap_incr == snap_full)
    return res


run_case.repo_files = {}


def _node_lookup(st) -> dict:
    out = {}
    for n in st.all_nodes():
        out[n.id] = {"qn": n.qualified_name or "", "file": n.file_path or "",
                     "label": n.label or "", "type": n.type or ""}
    return out


def _evaluate(case: dict, st, _unused, seconds: float) -> dict:
    nodes = _node_lookup(st)
    exp = case.get("expect", {})
    res_edges = [dict(e) for e in st.all_edges()
                 if e["relation"] == "imports" and (e["method"] or "") == "py-import-resolve"
                 and _attrs(e).get("resolution") == "resolved"]
    detail: dict = {"seconds": seconds}
    # --- resolved recall ---
    want = exp.get("resolved", [])
    hits = 0
    for w in want:
        ok = False
        for e in res_edges:
            a = _attrs(e)
            if e["file_path"] != w["file"]:
                continue
            if a.get("imported") != w["imported"]:
                continue
            t = nodes.get(e["target"], {})
            if t.get("qn") == w["target"] and t.get("file") == w["target_file"]:
                if "chain" in w and a.get("via_chain") != w["chain"]:
                    continue
                ok = True
                break
        hits += ok
        detail.setdefault("resolved_detail", {})[w["imported"]] = ok
    detail["resolved_recall"] = round(hits / len(want), 3) if want else 1.0
    # --- precision: no resolved edge may be wrong ---
    bad = 0
    for e in res_edges:
        a = _attrs(e)
        t = nodes.get(e["target"], {})
        if not t or e["target"].startswith("ext:"):
            bad += 1
            continue
        # every resolved edge must be explainable by some expectation
        if not any(a.get("imported") == w.get("imported") and
                   e["file_path"] == w.get("file") for w in want):
            # ...unless it is a legitimately resolved edge outside the
            # annotated set (shared repo!): verify target file is live.
            try:
                live = (st.cx.execute(
                    "SELECT path FROM files WHERE path LIKE ?",
                    (f"%:{t['file']}",)).fetchone() is not None) if t.get("file") else False
            except Exception:
                live = False
            if not live:
                bad += 1
    detail["resolved_total"] = len(res_edges)
    detail["false_local"] = bad
    detail["precision"] = round(1.0 - bad / len(res_edges), 3) if res_edges else 1.0
    # --- unresolved / external / ambiguous ---
    markers = [dict(e) for e in st.all_edges()
               if e["method"] == "py-import-resolve" and
               _attrs(e).get("resolution") in ("unresolved", "ambiguous")]
    for kind in ("unresolved", "external", "ambiguous"):
        for w in exp.get(kind, []):
            if kind == "ambiguous":
                cands = sorted(nodes.get(c, {}).get("qn", "") for c in
                               _cand_ids(st, w))
                detail.setdefault("ambiguous_detail", {})[w["imported"]] = \
                    (cands == sorted(w["candidates"]))
            elif kind == "external":
                # stays an external placeholder: no resolved edge for it
                detail.setdefault("external_detail", {})[w["imported"]] = \
                    not any(_attrs(e).get("imported") == w["imported"] and
                            _attrs(e).get("resolution") == "resolved"
                            for e in res_edges)
            else:
                hit = [e for e in markers
                       if e["file_path"] == w["file"] and
                       _attrs(e).get("imported") == w["imported"] and
                       w["reason"] in (_attrs(e).get("reason") or "")]
                detail.setdefault("unresolved_detail", {})[w["imported"]] = len(hit) == 1
    for key in ("unresolved_detail", "external_detail", "ambiguous_detail"):
        vals = list(detail.get(key, {}).values())
        detail[key.replace("detail", "ok")] = (all(vals) if vals else True)
    # --- depends_on ---
    deps_ok = True
    for w in exp.get("depends", []):
        hit = [e for e in st.all_edges()
               if e["relation"] == "depends_on" and e["method"] == "py-import-resolve"
               and e["file_path"] == w["src"]]
        # target file check via file node ids would need hashing; compare via nodes
        ok = False
        for e in hit:
            t = nodes.get(e["target"], {})
            if t.get("file") == w["dst"]:
                ok = True
        deps_ok &= ok
    detail["depends_ok"] = deps_ok
    # --- no_resolved_from files must not own resolved imports ---
    clean = True
    for rel in exp.get("no_resolved_from", []):
        if any(e["file_path"] == rel for e in res_edges):
            clean = False
    detail["no_resolved_clean"] = clean
    # --- locations: resolved edges carry the import statement line ---
    loc_ok = True
    for e in res_edges:
        if not isinstance(e.get("start_line"), int) or e["start_line"] < 1:
            loc_ok = False
    detail["locations_ok"] = loc_ok
    detail["case_ok"] = all([
        detail["resolved_recall"] == 1.0, detail["false_local"] == 0,
        detail.get("unresolved_ok", True), detail.get("external_ok", True),
        detail.get("ambiguous_ok", True), detail["depends_ok"],
        detail["no_resolved_clean"], detail["locations_ok"]])
    return {"id": case["id"], "split": case.get("split"), **detail}


def _cand_ids(st, w):
    out = []
    for e in st.all_edges():
        if e["method"] != "py-import-resolve":
            continue
        a = _attrs(e)
        if (a.get("resolution") == "ambiguous" and e["file_path"] == w["file"]
                and a.get("imported") == w["imported"]):
            out.append(e["target"])
    return out


def evaluate(dataset_path: str | Path, tmp_root: str | Path,
             split: str = "all") -> dict:
    ds = load_dataset(dataset_path)
    run_case.repo_files = dict(ds.get("repo_files", {}))
    cases = [c for c in ds["cases"]
             if split == "all" or c.get("split") == split]
    out = []
    for c in cases:
        out.append(run_case(c, Path(tmp_root)))
    return {"version": ds.get("version"), "split": split,
            "cases": out, "summary": summarize(out)}


def summarize(cases: list[dict]) -> dict:
    def mean(key):
        vals = [c[key] for c in cases if isinstance(c.get(key), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else 0.0
    bools = {}
    for key in ("unresolved_ok", "external_ok", "ambiguous_ok", "depends_ok",
                "no_resolved_clean", "locations_ok"):
        vals = [c.get(key, True) for c in cases]
        bools[key] = all(vals)
    eq = [c.get("equivalent", True) for c in cases]
    return {"n": len(cases), "resolved_recall": mean("resolved_recall"),
            "precision": mean("precision"),
            "false_local_total": sum(c.get("false_local", 0) for c in cases),
            **bools, "equivalent_all": all(eq)}


def markdown_report(result: dict) -> str:
    s = result.get("summary", {})
    L = [f"# Import-resolution benchmark `{result.get('version')}` "
         f"(split {result.get('split')})", "",
         f"- cases: {s.get('n')}  resolved_recall: {s.get('resolved_recall')}  "
         f"precision: {s.get('precision')}  false_local: {s.get('false_local_total')}",
         f"- unresolved_ok: {s.get('unresolved_ok')}  external_ok: {s.get('external_ok')}  "
         f"ambiguous_ok: {s.get('ambiguous_ok')}  depends_ok: {s.get('depends_ok')}",
         f"- no_resolved_clean: {s.get('no_resolved_clean')}  "
         f"locations_ok: {s.get('locations_ok')}  equivalent_all: {s.get('equivalent_all')}"]
    bad = [c["id"] for c in result.get("cases", []) if not c.get("case_ok")]
    if bad:
        L.append(f"- failing cases: {', '.join(bad)}")
    return "\n".join(L) + "\n"
