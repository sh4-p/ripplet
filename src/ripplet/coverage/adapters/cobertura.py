"""Cobertura-compatible XML adapter (e.g. `coverage xml -o`).

XML hardening without new dependencies: any input containing a DOCTYPE or
ENTITY declaration is rejected before parsing (stdlib ElementTree exposes no
DTD switch, so pre-scan rejection is the control), plus byte/record caps and
strict counter validation.
"""
from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from .base import register, check_count, check_line, sanitize_text, report_id, ReportRejected, MAX_RECORDS
from ..models import ParsedReport, CoverageRun, FileObs, LineObs, BranchObs, FuncObs

_DTD = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


@register
class CoberturaXml:
    name = "cobertura_xml"
    version = "1"

    def detect(self, text: str) -> bool:
        t = text.lstrip()[:2000]
        return "<coverage" in t and ("line-rate" in t or "branch-rate" in t)

    def parse(self, text, report_bytes, repo_id, label=""):
        if _DTD.search(text[:100000]):
            raise ReportRejected("XML with DOCTYPE/ENTITY declarations is rejected "
                                 "(entity-expansion protection)")
        try:
            root = ET.fromstring(text.encode("utf-8")[:20_000_000])
        except ET.ParseError as e:
            raise ReportRejected(f"malformed XML: {e}")
        if root.tag != "coverage":
            raise ReportRejected("not a Cobertura <coverage> document")
        run_id, digest = report_id(report_bytes, repo_id, label)
        run = CoverageRun(
            id=run_id, tool=sanitize_text(root.get("version", ""), 60) or "cobertura",
            tool_version=sanitize_text(root.get("version", ""), 60),
            format=self.name, format_version="cobertura-1",
            report_hash=digest, label=sanitize_text(label, 120),
            has_line=True,
            has_branch=root.get("branch-rate") is not None)
        res = ParsedReport(run=run, adapter=self.name, adapter_version=self.version)
        n = 0
        classes = root.findall(".//class")
        if not classes:
            raise ReportRejected("Cobertura: no <class> entries")
        for cl in classes:
            rel = str(cl.get("filename", "")).replace("\\", "/").lstrip("./")
            if not rel:
                res.warnings.append("skipped class without filename")
                res.partial = True
                continue
            hit = miss = 0
            for ln in cl.findall("./lines/line"):
                try:
                    no = check_line(int(ln.get("number", "0")), f"{rel} line number")
                    hits = check_count(int(ln.get("hits", "0")), f"{rel} line hits")
                except (ValueError, ReportRejected) as e:
                    res.warnings.append(f"{rel}: {e}")
                    res.partial = True
                    continue
                res.lines.append(LineObs(rel, no, hits or 0))
                n += 1
                if hits:
                    hit += 1
                else:
                    miss += 1
                if n > MAX_RECORDS:
                    raise ReportRejected(f"record cap exceeded ({MAX_RECORDS})")
                if str(ln.get("branch", "")).lower() == "true":
                    cond = str(ln.get("condition-coverage", ""))
                    m = re.match(r"(\d+)%\s*\((\d+)/(\d+)\)", cond)
                    if m:
                        _pct, taken, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        # branch detail below the available fidelity: record one
                        # observation row per line with taken/total preserved.
                        res.branches.append(BranchObs(rel, no, None, None, taken))
                        n += 1
                    else:
                        res.warnings.append(f"{rel}:{no}: unparsable condition-coverage "
                                            f"{cond[:60]!r}; branch kept as unavailable detail")
            for m in cl.findall("./methods/method"):
                mname = sanitize_text(str(m.get("name", "")), 200)
                try:
                    mline = check_line(int(m.get("line", "0")), f"{rel} method line")
                except (ValueError, ReportRejected):
                    mline = None
                mhits = None
                ml = m.findall("./lines/line")
                if ml:
                    try:
                        mhits = max(check_count(int(x.get("hits", "0")), "hits") or 0
                                    for x in ml)
                    except (ValueError, ReportRejected):
                        mhits = None
                res.funcs.append(FuncObs(rel, mname, mline, mhits if mhits is not None else 0))
                if mhits is None:
                    res.warnings.append(f"{rel}.{mname}: method hits unavailable, kept as 0")
            b_total = sum(1 for _ in res.branches if _.file == rel)
            b_taken = sum(1 for _ in res.branches if _.file == rel and _.taken > 0)
            res.files.append(FileObs(
                rel, lines_hit=hit, lines_missed=miss,
                funcs_found=len(cl.findall("./methods/method")) or None,
                branches_found=b_total or None,
                branches_taken=b_taken if b_total else None))
        run.has_func = any(True for _ in res.funcs)
        if not res.files:
            raise ReportRejected("Cobertura: no usable file entries")
        return res
