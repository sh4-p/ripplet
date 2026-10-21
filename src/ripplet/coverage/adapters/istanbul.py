"""Istanbul/NYC JSON adapter (`coverage-final.json`).

Extracts statement, function and branch counters with source maps. The
standard artifact is aggregate: it carries NO per-test identities, so no
per-test links are ever synthesized from it (documented, tested).
"""
from __future__ import annotations
from .base import register, strict_json, check_count, check_line, sanitize_text, report_id, ReportRejected, MAX_RECORDS
from ..models import ParsedReport, CoverageRun, FileObs, LineObs, BranchObs, FuncObs


@register
class IstanbulJson:
    name = "istanbul_json"
    version = "1"

    def detect(self, text: str) -> bool:
        try:
            d = strict_json(text[:200000])
        except Exception:
            return False
        if not isinstance(d, dict) or not d:
            return False
        first = next(iter(d.values()))
        return isinstance(first, dict) and "statementMap" in first and "s" in first

    def _loc_line(self, loc, what: str):
        try:
            return check_line(int(loc["start"]["line"]), what)
        except (KeyError, TypeError, ValueError, ReportRejected) as e:
            raise ReportRejected(f"{what}: bad location ({e})")

    def parse(self, text, report_bytes, repo_id, label=""):
        d = strict_json(text)
        if not isinstance(d, dict) or not d:
            raise ReportRejected("Istanbul JSON: empty document")
        run_id, digest = report_id(report_bytes, repo_id, label)
        run = CoverageRun(id=run_id, tool="istanbul", format=self.name,
                          format_version="nyc-json", report_hash=digest,
                          label=sanitize_text(label, 120),
                          has_line=True, has_func=True, has_branch=True)
        res = ParsedReport(run=run, adapter=self.name, adapter_version=self.version)
        n = 0
        for path, f in d.items():
            if not isinstance(f, dict):
                res.warnings.append(f"skipped non-object entry: {str(path)[:120]}")
                res.partial = True
                continue
            rel = str(path).replace("\\", "/").lstrip("./")
            smap = f.get("statementMap", {}) if isinstance(f.get("statementMap"), dict) else {}
            scnt = f.get("s", {}) if isinstance(f.get("s"), dict) else {}
            fmap = f.get("fnMap", {}) if isinstance(f.get("fnMap"), dict) else {}
            fcnt = f.get("f", {}) if isinstance(f.get("f"), dict) else {}
            bmap = f.get("branchMap", {}) if isinstance(f.get("branchMap"), dict) else {}
            bcnt = f.get("b", {}) if isinstance(f.get("b"), dict) else {}
            if not smap and not fmap and not bmap:
                res.warnings.append(f"{rel}: no statement/function/branch maps; skipped")
                res.partial = True
                continue
            by_line: dict[int, int] = {}
            for sid, loc in smap.items():
                try:
                    ln = self._loc_line(loc, f"{rel} statement")
                    hits = check_count(scnt.get(sid, 0), f"{rel} statement hits")
                except ReportRejected as e:
                    res.warnings.append(str(e))
                    res.partial = True
                    continue
                by_line[ln] = max(by_line.get(ln, 0), hits or 0)
                n += 1
            for ln, hits in sorted(by_line.items()):
                res.lines.append(LineObs(rel, ln, hits))
            for fid, fdef in fmap.items():
                if not isinstance(fdef, dict):
                    continue
                name = sanitize_text(str(fdef.get("name", "")), 200)
                try:
                    decl = fdef.get("decl", fdef.get("loc", {}))
                    ln = self._loc_line(decl, f"{rel} function") if isinstance(decl, dict) else None
                except ReportRejected:
                    ln = None
                try:
                    hits = check_count(fcnt.get(fid, 0), f"{rel} function hits")
                except ReportRejected as e:
                    res.warnings.append(str(e))
                    res.partial = True
                    hits = 0
                res.funcs.append(FuncObs(rel, name, ln, hits or 0))
                n += 1
            branches = 0
            taken = 0
            for bid, bdef in bmap.items():
                if not isinstance(bdef, dict):
                    continue
                counts = bcnt.get(bid, [])
                if not isinstance(counts, list):
                    res.warnings.append(f"{rel}: branch counts malformed; skipped")
                    res.partial = True
                    continue
                try:
                    ln = self._loc_line(bdef.get("loc", {}), f"{rel} branch")
                except ReportRejected as e:
                    res.warnings.append(str(e))
                    res.partial = True
                    continue
                for bi, c in enumerate(counts):
                    try:
                        t = check_count(c, f"{rel} branch taken")
                    except ReportRejected as e:
                        res.warnings.append(str(e))
                        res.partial = True
                        continue
                    res.branches.append(BranchObs(rel, ln, None, bi, t or 0))
                    branches += 1
                    taken += 1 if (t or 0) > 0 else 0
                    n += 1
                if n > MAX_RECORDS:
                    raise ReportRejected(f"record cap exceeded ({MAX_RECORDS})")
            res.files.append(FileObs(
                rel, lines_hit=sum(1 for v in by_line.values() if v > 0),
                lines_missed=sum(1 for v in by_line.values() if v == 0),
                funcs_found=len(fmap) or None,
                funcs_covered=sum(1 for fid in fmap
                                  if (fcnt.get(fid, 0) or 0) > 0) or None,
                branches_found=branches or None, branches_taken=taken or None))
        if not res.files:
            raise ReportRejected("Istanbul JSON: no usable file entries")
        return res
