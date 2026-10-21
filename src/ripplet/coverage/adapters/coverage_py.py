"""coverage.py JSON adapter (`coverage json -o`)."""
from __future__ import annotations
from .base import register, strict_json, check_count, check_line, sanitize_text, report_id, ReportRejected, MAX_RECORDS
from ..models import ParsedReport, CoverageRun, FileObs, LineObs, CtxObs


@register
class CoveragePyJson:
    name = "coverage_py_json"
    version = "1"

    def detect(self, text: str) -> bool:
        try:
            d = strict_json(text)
        except Exception:
            return False
        return isinstance(d, dict) and isinstance(d.get("files"), dict) and "meta" in d

    def parse(self, text, report_bytes, repo_id, label=""):
        d = strict_json(text)
        if not isinstance(d, dict) or not isinstance(d.get("files"), dict):
            raise ReportRejected("coverage.py JSON: missing 'files' mapping")
        meta = d.get("meta", {}) if isinstance(d.get("meta"), dict) else {}
        run_id, digest = report_id(report_bytes, repo_id, label)
        run = CoverageRun(id=run_id, tool="coverage.py",
                          tool_version=sanitize_text(str(meta.get("version", "")), 60),
                          format=self.name, format_version=sanitize_text(str(meta.get("version", "")), 60),
                          report_hash=digest, label=sanitize_text(label, 120),
                          has_line=True)
        res = ParsedReport(run=run, adapter=self.name, adapter_version=self.version)
        n = 0
        for path, f in d["files"].items():
            if not isinstance(f, dict):
                res.warnings.append(f"skipped non-object file entry: {str(path)[:120]}")
                res.partial = True
                continue
            rel = str(path).replace("\\", "/").lstrip("./")
            executed = f.get("executed_lines", [])
            missing = f.get("missing_lines", [])
            if not isinstance(executed, list) or not isinstance(missing, list):
                res.warnings.append(f"skipped malformed line lists: {rel[:120]}")
                res.partial = True
                continue
            sm = f.get("summary", {}) if isinstance(f.get("summary"), dict) else {}
            lines: list[int] = []
            for ln in executed:
                try:
                    lines.append((check_line(ln, f"{rel} executed line"), 1))
                except ReportRejected as e:
                    res.warnings.append(str(e)); res.partial = True
            missed: list[int] = []
            for ln in missing:
                try:
                    missed.append(check_line(ln, f"{rel} missing line"))
                except ReportRejected as e:
                    res.warnings.append(str(e)); res.partial = True
            for ln, hits in lines + [(m, 0) for m in missed]:
                res.lines.append(LineObs(rel, ln, hits))
                n += 1
                if n > MAX_RECORDS:
                    raise ReportRejected(f"record cap exceeded ({MAX_RECORDS})")
            res.files.append(FileObs(
                rel,
                statements_found=check_count(sm.get("num_statements"), f"{rel} num_statements"),
                statements_covered=check_count(sm.get("covered_lines"), f"{rel} covered_lines"),
                lines_hit=len(lines), lines_missed=len(missed)))
            ctx = f.get("contexts")
            if isinstance(ctx, dict) and ctx:
                run.has_contexts = True
                for cname, clines in ctx.items():
                    # Context labels stored verbatim; per-test identity is NOT
                    # inferred (aggregate contexts may name suites, files, ...).
                    if not isinstance(clines, list):
                        continue
                    for ln in clines:
                        try:
                            res.contexts.append(CtxObs(sanitize_text(str(cname), 200), rel,
                                                       check_line(ln, f"{rel} context line"), 1))
                        except ReportRejected:
                            continue
        if not res.files:
            raise ReportRejected("coverage.py JSON: no usable file entries")
        return res
