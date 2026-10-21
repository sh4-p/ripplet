"""Versioned test-selection benchmark (Phase 9 §12, Phase 10 Stage 1/13).

Datasets: selection-v1 (14 cases, frozen) and selection-v2 (v1 preserved
verbatim + new Python/JavaScript/TypeScript cases). Annotations are
independent of ranking: `relevant` test substrings come from scenario design.
Metrics per mode: P@k/R@k/MRR, recall, candidate-set reduction, A/B/C
distribution, false-verified rate, evidence-classification correctness,
revision-mismatch handling, latency, per-tag breakdowns. Coverage-aware vs
graph-only is measured by running each case with and without its coverage
import. Plan quality is measured independently from test-execution outcomes.
"""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path


def load_dataset(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_files(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _import_coverage(store, proj: Path, repo_id: str, spec: dict) -> str:
    """Build a minimal coverage.py-style JSON report from the spec.

    Returns run_id or '' when kind == 'none'.
    """
    kind = (spec or {}).get("kind", "none")
    if kind == "none":
        return ""
    from ..coverage import service as cov_svc
    contexts: dict = {}
    line_files: dict[str, dict] = {}
    if kind == "per-test":
        for ctx, locs in ((spec or {}).get("contexts") or {}).items():
            lines: list[int] = []
            for loc in locs:
                rel, ln = loc.rsplit(":", 1)
                lines.append(int(ln))
                line_files.setdefault(rel, {"executed_lines": [], "missing_lines": [],
                                            "summary": {"num_statements": 1, "covered_lines": 1}})
                for n in lines:
                    if n not in line_files[rel]["executed_lines"]:
                        line_files[rel]["executed_lines"].append(n)
            contexts[ctx] = sorted(lines)
        # contexts live under files.*.contexts in coverage.py JSON
        for rel in line_files:
            line_files[rel]["contexts"] = {c: sorted(v) for c, v in contexts.items()}
    else:  # aggregate: executed lines, no contexts -> must yield zero A links
        for loc in ((spec or {}).get("lines") or []):
            rel, ln = loc.rsplit(":", 1)
            line_files.setdefault(rel, {"executed_lines": [int(ln)], "missing_lines": [],
                                        "summary": {"num_statements": 1, "covered_lines": 1}})
    rep = proj / "_bench_cov.json"
    rep.write_text(json.dumps({"meta": {"version": "7.4.0"}, "files": line_files}),
                   encoding="utf-8")
    kwargs: dict = {"label": "bench"}
    if (spec or {}).get("revision"):
        kwargs["revision"] = spec["revision"]
    out = cov_svc.import_report(store, str(rep), str(proj), **kwargs)
    run_id = out["run_id"]
    if (spec or {}).get("make_stale"):
        # Touch a covered source file so freshness recomputes to stale.
        covered = next(iter(line_files), None)
        if covered:
            p = proj / covered
            p.write_text(p.read_text(encoding="utf-8") + "\n# bench-stale-touch\n",
                         encoding="utf-8")
            cov_svc.refresh_repo_coverage(store, repo_id, str(proj))
    return run_id


def _match_ids(candidates: list[dict], substr: str) -> list[str]:
    return [c["test_id"] for c in candidates if substr in (c.get("test") or "")]


def run_case(case: dict, tmp: Path, modes=("conservative", "balanced", "broad"),
             with_coverage: bool = True) -> dict:
    import shutil
    from ..config import RippletConfig
    from ..service import index_workspace
    from ..storage.sqlite_store import SQLiteStore
    from ..testplan import changes as ch, plan as pl

    proj = tmp / case["id"] / ("cov" if with_coverage else "graph")
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    _write_files(proj, case.get("files", {}))
    cfg = RippletConfig([str(proj)], str(proj / "_bench.db"), "bench")
    index_workspace(cfg)
    store = SQLiteStore(str(proj / "_bench.db"))
    try:
        repo_id = "repo-" + hashlib.sha256(
            str(proj.resolve()).encode()).hexdigest()[:10]
        run_id = ""
        if with_coverage:
            run_id = _import_coverage(store, proj, repo_id, case.get("coverage") or {})
        chg = case.get("change", {})
        deleted = bool(chg.get("deleted"))
        if deleted:
            (proj / chg["file"]).unlink(missing_ok=True)
            changed = [ch.ChangedFile(chg["file"], "deleted")]
            mapped = {"symbols": {}, "unmapped_files": [],
                      "deleted_files": [chg["file"]]}
        else:
            changed = [ch.ChangedFile(chg["file"])] if chg.get("file") else []
            mapped = ch.map_to_symbols(store, repo_id, changed)
            if chg.get("symbol_substr"):
                hits = [n for n in store.all_nodes()
                        if chg["symbol_substr"] in (n.qualified_name or "")
                        and getattr(n, "repository_id", "") == repo_id]
                for n in hits[:3]:
                    mapped.setdefault("symbols", {}).setdefault(
                        n.id, {"change": "modified", "via": "explicit",
                               "file": n.file_path, "status": "modified"})
        relevant = set(case.get("relevant", []))
        # Inventory uses the same test heuristic as discovery (type test OR
        # test path/id), so file/module suite nodes count — reduction is
        # measured against what discovery can emit, not a narrower subset.
        def _is_test(n) -> bool:
            if getattr(n, "type", "") == "test":
                return True
            label = (getattr(n, "label", "") or "")
            fp = (getattr(n, "file_path", "") or "").lower().replace("\\", "/")
            return label.startswith("test_") or "/test_" in fp or "/tests/" in fp
        total_tests = sum(
            1 for n in store.all_nodes()
            if _is_test(n) and getattr(n, "repository_id", "") == repo_id)
        per_mode: dict = {}
        for mode in modes:
            t0 = time.time()
            plan = pl.build(store, repo_id, str(proj), changed, mapped,
                            mode=mode, revision="bench",
                            change_digest=f"bench:{case['id']}:{mode}")
            dt = (time.time() - t0) * 1000.0
            ranked_tests = [c["test"] for c in plan["candidates"]]
            ranked_ids = [c["test_id"] for c in plan["candidates"]]

            def _rel_hit(substr: str) -> int | None:
                for i, t in enumerate(ranked_tests):
                    if substr in t:
                        return i + 1
                return None

            positions = {s: _rel_hit(s) for s in relevant}
            mrr = 0.0
            if positions:
                ranks = sorted(p for p in positions.values() if p)
                mrr = 1.0 / ranks[0] if ranks else 0.0
            k = 5
            hits_k = sum(1 for s in relevant if (positions[s] or 99) <= k)
            p_at_k = (hits_k / min(k, len(ranked_tests))) if ranked_tests else (
                1.0 if not relevant else 0.0)
            r_at_k = (hits_k / len(relevant)) if relevant else (
                1.0 if not ranked_tests else 0.0)
            recall = sum(1 for s in relevant if positions[s]) / len(relevant) \
                if relevant else (1.0 if not ranked_tests else 0.0)
            cats = [c["category"] for c in plan["candidates"]]
            a_ids = [c for c in plan["candidates"] if c["category"] == "A"]
            false_verified = sum(
                1 for c in a_ids
                if not any(s in (c.get("test") or "") for s in relevant))
            # relevant hits in rank order with their evidence categories
            hits = [{"test": c["test"], "category": c["category"], "rank": c.get("rank")}
                    for c in plan["candidates"]
                    if any(s in (c.get("test") or "") for s in relevant)]
            per_mode[mode] = {
                "candidates": len(ranked_tests),
                "total_tests": total_tests,
                "reduction": round(1.0 - len(ranked_tests) / total_tests, 3)
                if total_tests else 1.0,
                "p_at_5": round(p_at_k, 3), "r_at_5": round(r_at_k, 3),
                "mrr": round(mrr, 3), "recall": round(recall, 3),
                "by_category": {c: cats.count(c) for c in "ABC"},
                "false_verified": false_verified,
                "revision_flags": sorted({str(c.get("revision_ok"))
                                          for c in plan["candidates"]}),
                "latency_ms": round(dt, 1),
                "ranked": ranked_tests[:10],
                "hits": hits,
                "plan_meta": {
                    "unresolved": len(plan.get("unresolved_relationships", [])),
                    "limitations": len(plan.get("limitations", [])),
                    "excluded": len(plan.get("excluded", [])),
                },
            }
        return {"id": case["id"], "split": case.get("split"), "modes": per_mode,
                "run_id": run_id, "total_tests": total_tests}
    finally:
        try:
            store.close()
        except Exception:
            pass


def check_expectations(case: dict, result: dict,
                       mode: str = "balanced") -> dict:
    """Evidence-classification correctness for one case (balanced/cov).

    Verifies the case's independent annotations against the produced plan:
    expected/forbidden evidence categories, revision compatibility flags,
    unresolved-relationship surfacing, and A<B<C rank ordering of relevant
    hits. Returns {"ok": bool, "checks": {...}}; never raises.
    """
    checks: dict = {}
    try:
        m = result["cov"]["modes"][mode]
    except Exception:
        return {"ok": False, "checks": {"result_missing": False}}
    cands = m.get("ranked", [])
    hits = m.get("hits", [])
    by_cat = m.get("by_category", {})
    if case.get("expect_any_category"):
        want = case["expect_any_category"]
        checks[f"has_{want}"] = any(
            h["category"] == want for h in hits) if hits else by_cat.get(want, 0) > 0
    if case.get("forbid_category"):
        bad = case["forbid_category"]
        checks[f"no_{bad}"] = by_cat.get(bad, 0) == 0
    if "expect_revision_ok" in case:
        want = case["expect_revision_ok"]
        a_flags = [c for c in [m.get("revision_flags", [])]]
        flags = m.get("revision_flags", [])
        if want is False:
            checks["revision_not_current"] = ("False" in flags) or (by_cat.get("A", 0) == 0 and not hits)
        else:
            checks["revision_current"] = ("True" in flags)
    if case.get("expect_unresolved"):
        checks["unresolved_surfaced"] = (m.get("plan_meta", {}).get("unresolved", 0) > 0)
    if case.get("expect_rank_sorted"):
        order = {"A": 0, "B": 1, "C": 2}
        seq = [order.get(h["category"], 9) for h in hits]
        checks["rank_sorted"] = all(b >= a for a, b in zip(seq, seq[1:])) if seq else True
    if "expect_broad_recall" in case:
        try:
            broad = result["cov"]["modes"]["broad"]
            rel = set(case.get("relevant", []))
            got = sum(1 for s in rel
                      if any(s in (t or "") for t in broad.get("ranked", [])))
            want = len(rel) * float(case["expect_broad_recall"])
            checks["broad_recall"] = got >= want if rel else True
        except Exception:
            checks["broad_recall"] = False
    ok = all(checks.values()) if checks else True
    return {"ok": ok, "checks": checks}


def evaluate(dataset_path: str | Path, tmp_root: str | Path,
             modes=("conservative", "balanced", "broad"),
             split: str = "all") -> dict:
    ds = load_dataset(dataset_path)
    wanted = set(ds.get("split", {}).get(split, [])) if split in ("dev", "held") else None
    out_cases = []
    for case in ds["cases"]:
        if wanted is not None and case["id"] not in wanted:
            continue
        both = {}
        for with_cov in (True, False):
            both["cov" if with_cov else "graph"] = run_case(
                case, Path(tmp_root), modes=modes, with_coverage=with_cov)
        entry = {"id": case["id"], "split": case.get("split"),
                 "relevant": case.get("relevant", []),
                 "tags": case.get("tags", []), **both}
        entry["classification"] = check_expectations(case, entry)
        out_cases.append(entry)
    summary = summarize(out_cases, modes, ds)
    return {"version": ds.get("version"), "modes": list(modes), "split": split,
            "cases": out_cases, "summary": summary}


def summarize(cases: list[dict], modes, ds: dict | None = None) -> dict:
    s: dict = {}
    for mode in modes:
        for variant in ("cov", "graph"):
            mrrs, recalls, reds, fvs, lats = [], [], [], 0, []
            for c in cases:
                m = c[variant]["modes"][mode]
                mrrs.append(m["mrr"])
                recalls.append(m["recall"])
                reds.append(m["reduction"])
                fvs += m["false_verified"]
                lats.append(m["latency_ms"])
            key = f"{mode}/{variant}"
            s[key] = {"mean_mrr": round(sum(mrrs) / len(mrrs), 3) if mrrs else 0.0,
                      "mean_recall": round(sum(recalls) / len(recalls), 3),
                      "mean_reduction": round(sum(reds) / len(reds), 3),
                      "false_verified_total": fvs,
                      "p50_latency_ms": round(sorted(lats)[len(lats) // 2], 1)
                      if lats else 0.0, "n": len(mrrs)}
    # coverage-aware lift over graph-only (balanced), reported, never claimed
    # without the numbers beside it.
    try:
        s["coverage_lift_balanced_mrr"] = round(
            s["balanced/cov"]["mean_mrr"] - s["balanced/graph"]["mean_mrr"], 3)
    except Exception:
        pass
    # evidence-classification correctness: fraction of cases whose independent
    # annotations hold on the balanced/cov plan.
    if cases:
        oks = sum(1 for c in cases if (c.get("classification") or {}).get("ok", True))
        s["classification_accuracy"] = round(oks / len(cases), 3)
        s["classification_n"] = len(cases)
        bad = [c["id"] for c in cases if not (c.get("classification") or {}).get("ok", True)]
        s["classification_failures"] = bad[:20]
    # per-tag breakdown (balanced/cov): plan quality by scenario kind.
    tags: dict[str, list] = {}
    for c in cases:
        for t in c.get("tags", []):
            tags.setdefault(t, []).append(c)
    per_tag = {}
    for t, cs in sorted(tags.items()):
        mrrs = [c["cov"]["modes"]["balanced"]["mrr"] for c in cs
                if "balanced" in c["cov"]["modes"]]
        recs = [c["cov"]["modes"]["balanced"]["recall"] for c in cs
                if "balanced" in c["cov"]["modes"]]
        per_tag[t] = {"n": len(cs),
                      "mean_mrr": round(sum(mrrs) / len(mrrs), 3) if mrrs else 0.0,
                      "mean_recall": round(sum(recs) / len(recs), 3) if recs else 0.0}
    if per_tag:
        s["per_tag_balanced_cov"] = per_tag
    return s


def markdown_report(result: dict) -> str:
    L = [f"# Test-selection benchmark `{result.get('version')}`", ""]
    for k, v in sorted(result.get("summary", {}).items()):
        if isinstance(v, dict) and "mean_mrr" in v:
            L.append(f"- {k}: MRR {v['mean_mrr']} recall {v['mean_recall']} "
                     f"reduction {v['mean_reduction']} false_verified "
                     f"{v['false_verified_total']} p50 {v['p50_latency_ms']}ms")
    s = result.get("summary", {})
    if "classification_accuracy" in s:
        L.append(f"- classification_accuracy: {s['classification_accuracy']} "
                 f"(n={s.get('classification_n')})")
        if s.get("classification_failures"):
            L.append(f"  failures: {', '.join(s['classification_failures'])}")
    if "per_tag_balanced_cov" in s:
        L.append("")
        L.append("per-tag (balanced/cov):")
        for t, v in sorted(s["per_tag_balanced_cov"].items()):
            L.append(f"- {t}: n={v['n']} MRR {v['mean_mrr']} recall {v['mean_recall']}")
    L += ["", "Modes are reported independently; more candidates is not "
          "scored as better (reduction and false_verified guard that).",
          "Plan quality is measured independently from test-execution outcomes."]
    return "\n".join(L) + "\n"
