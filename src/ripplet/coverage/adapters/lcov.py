"""LCOV tracefile adapter (line + function + branch records)."""
from __future__ import annotations
from .base import register, check_count, check_line, sanitize_text, report_id, ReportRejected, MAX_RECORDS
from ..models import ParsedReport, CoverageRun, FileObs, LineObs, BranchObs, FuncObs


@register
class LcovTrace:
    name = "lcov"
    version = "1"

    def detect(self, text: str) -> bool:
        head = text[:4000]
        return "SF:" in head and "end_of_record" in text[:200000]

    def parse(self, text, report_bytes, repo_id, label=""):
        run_id, digest = report_id(report_bytes, repo_id, label)
        run = CoverageRun(id=run_id, tool="lcov", format=self.name,
                          format_version="lcov-tracefile", report_hash=digest,
                          label=sanitize_text(label, 120), has_line=True)
        res = ParsedReport(run=run, adapter=self.name, adapter_version=self.version)
        cur: str | None = None
        files: dict[str, dict] = {}
        n = 0

        def fdict():
            return {"lines": {}, "funcs": {}, "branches": [], "has_branch": False,
                    "has_func": False}

        def flush():
            nonlocal n
            if cur is None:
                return
            f = files[cur]
            hit = sum(1 for v in f["lines"].values() if v > 0)
            res.files.append(FileObs(
                cur, lines_hit=hit, lines_missed=len(f["lines"]) - hit,
                funcs_found=len(f["funcs"]) or None,
                funcs_covered=sum(1 for v in f["funcs"].values() if (v[1] or 0) > 0) or None,
                branches_found=len(f["branches"]) or None,
                branches_taken=sum(1 for b in f["branches"] if b[3] > 0) or None))
            for ln, hits in sorted(f["lines"].items()):
                res.lines.append(LineObs(cur, ln, hits))
                n += 1
            for name, (ln, hits) in sorted(f["funcs"].items()):
                res.funcs.append(FuncObs(cur, name, ln, hits or 0))
                n += 1
            for (ln, block, branch, taken) in f["branches"]:
                res.branches.append(BranchObs(cur, ln, block, branch, taken))
                n += 1
            if n > MAX_RECORDS:
                raise ReportRejected(f"record cap exceeded ({MAX_RECORDS})")

        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("SF:"):
                if cur is not None:
                    flush()
                cur = line[3:].strip().replace("\\", "/")
                files.setdefault(cur, fdict())
            elif cur is None:
                continue
            elif line.startswith("DA:"):
                try:
                    num, hits = line[3:].split(",", 1)
                    no = check_line(int(num), f"{cur} DA line")
                    h = check_count(int(hits), f"{cur} DA hits")
                except (ValueError, ReportRejected) as e:
                    res.warnings.append(f"{cur}: {e}")
                    res.partial = True
                    continue
                files[cur]["lines"][no] = h or 0
            elif line.startswith("FN:"):
                try:
                    num, name = line[3:].split(",", 1)
                    no = check_line(int(num), f"{cur} FN line")
                except (ValueError, ReportRejected) as e:
                    res.warnings.append(f"{cur}: {e}")
                    res.partial = True
                    continue
                files[cur]["funcs"].setdefault(sanitize_text(name.strip(), 200), [no, None])
                files[cur]["has_func"] = True
            elif line.startswith("FNDA:"):
                try:
                    hits, name = line[5:].split(",", 1)
                    h = check_count(int(hits), f"{cur} FNDA hits")
                except (ValueError, ReportRejected) as e:
                    res.warnings.append(f"{cur}: {e}")
                    res.partial = True
                    continue
                key = sanitize_text(name.strip(), 200)
                prev = files[cur]["funcs"].get(key, [None, None])
                files[cur]["funcs"][key] = [prev[0], h or 0]
                files[cur]["has_func"] = True
            elif line.startswith("BRDA:"):
                parts = line[5:].split(",")
                if len(parts) != 4:
                    res.warnings.append(f"{cur}: malformed BRDA skipped")
                    res.partial = True
                    continue
                try:
                    no = check_line(int(parts[0]), f"{cur} BRDA line")
                    block = check_count(int(parts[1]), f"{cur} BRDA block")
                    branch = check_count(int(parts[2]), f"{cur} BRDA branch")
                    taken_raw = parts[3].strip()
                    taken = 0 if taken_raw == "-" else check_count(int(taken_raw),
                                                                   f"{cur} BRDA taken")
                except (ValueError, ReportRejected) as e:
                    res.warnings.append(f"{cur}: {e}")
                    res.partial = True
                    continue
                # "-" means explicitly not executed: preserved as taken=0,
                # distinct from a missing record.
                files[cur]["branches"].append((no, block, branch, taken or 0))
                files[cur]["has_branch"] = True
            elif line == "end_of_record":
                flush()
                cur = None
            elif line.startswith(("TN:", "BRF:", "BRH:", "FNF:", "FNH:", "LF:", "LH:")):
                continue
            else:
                res.warnings.append(f"{cur or '?'}: unknown LCOV directive skipped")
                res.partial = True
        if cur is not None:
            flush()
        if any(f["has_branch"] for f in files.values()):
            run.has_branch = True
        if any(f["has_func"] for f in files.values()):
            run.has_func = True
        if not res.files:
            raise ReportRejected("LCOV: no usable file records")
        return res
