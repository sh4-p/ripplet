"""feature_slice: one call that returns everything an agent must imitate to
build a feature "like X" — the files that implement X in full, and every
touch point elsewhere (registrations, DTO fields, mappers, migrations,
models, tests) as short excerpts. Agents otherwise spend dozens of turns
reading these files one by one; the graph knows them already."""
from __future__ import annotations

import re
from pathlib import Path

CODE_EXT = {".cs", ".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".kt", ".go",
            ".rs", ".php", ".rb", ".swift", ".html", ".scss", ".css", ".vue",
            ".svelte", ".sql", ".lua", ".c", ".cpp", ".h", ".hpp"}
_NOISE = re.compile(r"(^|/)(node_modules|dist|bin|obj|vendor|\.git|realworld|fixtures?|"
                    r"__snapshots__)/", re.I)
_GENERATED = re.compile(r"(\.Designer\.cs|ModelSnapshot\.cs|\.min\.js|\.lock)$", re.I)
# a documentation tree (API reference pages built from the code) is output, not a feature to copy
_DOCS = re.compile(r"(^|/)(docs?|documentation|apidocs?|api-docs|javadoc|site|_site)/", re.I)
_MARKUP = {".html", ".css", ".scss"}
# example / documentation code shows how to USE a feature; it is the last place to copy or edit
_AUX = re.compile(r"(^|/)(docs?|documentation[\w-]*|examples?|samples?|snippets|demos?|tutorials?)/", re.I)


def _stems(feature: str) -> list:
    out = []
    for w in re.split(r"[\s,;/|]+", feature.strip()):
        w = re.sub(r"[^A-Za-z0-9_]", "", w).lower()
        for suf in ("ies", "es", "s"):
            if len(w) > 5 and w.endswith(suf):
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        if len(w) > 4 and w.endswith("e"):
            w = w[:-1]
        if len(w) >= 3 and w not in out:
            out.append(w)
    return out


def _layer(path: str) -> tuple:
    p = path.lower()
    test = bool(re.search(r"(test|spec)", p))
    order = ["domain", "entit", "model", "infrastructure", "data", "migration",
             "feature", "service", "controller", "handler", "api", "route",
             "component", "page", "view"]
    rank = next((i for i, k in enumerate(order) if k in p), len(order))
    return (p.split("/")[0], test, rank, p)


def _excerpts(lines: list, hits: list, ctx: int, cap: int) -> list:
    spans = []
    for h in hits:
        a, b = max(0, h - ctx), min(len(lines), h + ctx + 1)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out = []
    for a, b in spans[:cap]:
        out.extend(f"{i + 1:>5}| {lines[i]}" for i in range(a, b))
        out.append("  ...")
    if len(spans) > cap:
        out.append(f"  ({len(spans) - cap} more places in this file)")
    return out


# Detail levels, most to least. Under budget pressure entries step down one
# level at a time, lowest-priority entry first, so every file degrades a
# little before any one file disappears; the last level is "listed by path".
_CORE_LEVELS = (("full", 0, 0), ("excerpts", 3, 8), ("excerpts", 1, 3), ("listed", 0, 0))
_TOUCH_LEVELS = (("excerpts", 3, 6), ("excerpts", 1, 3), ("excerpts", 0, 2), ("listed", 0, 0))


def _render_entry(e: dict, full_lines: int) -> list:
    kind, ctx, cap = e["levels"][e["lv"]]
    lines, hits = e["lines"], e["hits"]
    if kind == "listed":
        return []
    if kind == "full":
        body = [f"{i + 1:>5}| {t}" for i, t in enumerate(lines[:full_lines])]
        if len(lines) > full_lines:
            body.append(f"  ... {len(lines) - full_lines} more lines")
        return body
    body = _excerpts(lines, hits, ctx, cap)
    if e["role"] == "implements":
        body.insert(0, f"  (excerpts around the {len(hits)} mentions; budget)")
    return body


# a line that WRITES / accepts / declares the field: a new sibling field must
# be added there too. Lines that only read it (reports, rollups) are optional.
_WRITE_RX = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|SET\s+\w+\s*=|ADD\s+(COLUMN\s+)?|ALTER\s+TABLE|CREATE\s+TABLE)|"
    r"array_key_exists|Request::(post|input|get)|\$request->|request\.(form|json|get)|req\.body|"
    r"\bname\s*=\s*[\"']|[\"']properties[\"']|\bfillable\b|\bvalidate\b|\brequired\b|"
    r"=>\s*\[\s*[\"']type[\"']|\$data\[|\$updateData\[|\bparams\[\]|\bschema\b", re.I)


def _writes(lines: list, hits: list, root_rx=None) -> bool:
    """Any line about the feature that writes/accepts/declares it. The root word
    counts too: a form field or API parameter often carries another unit or
    shape of it (`estimate_minutes` is entered as `estimate_hours`)."""
    idx = hits if root_rx is None else [i for i, t in enumerate(lines) if root_rx.search(t)]
    return any(_WRITE_RX.search(lines[i]) for i in idx)


def feature_slice(store, cfg, feature: str, max_chars: int = 60000,
                  full_lines: int = 160, focus: str = "", files_only: bool = False) -> str | dict | list:
    stems = _stems(feature)
    if not stems:
        return {"error": "give a feature name, e.g. 'favorite'"}
    rx = re.compile("|".join(map(re.escape, stems)), re.I)
    root = re.split(r"[_\-\s]|(?<=[a-z])(?=[A-Z])", feature.strip())[0]
    root_rx = re.compile(re.escape(root), re.I) if len(root) >= 4 else None
    # a database field (`estimate_minutes`): a sibling field is added where this
    # one is written/accepted; code that only reads it is optional. For a
    # feature (`favorite`), every reader is a touch point of a similar feature.
    field_mode = store.cx.execute("SELECT 1 FROM nodes WHERE type='column' AND lower(label)=lower(?) LIMIT 1",
                                  (feature.strip(),)).fetchone() is not None
    focus_l = (focus or "").replace("\\", "/").lower().strip()
    roots = [Path(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    paths = [r[0] for r in store.cx.execute(
        "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL AND file_path<>''")]
    core, touch, generated = [], [], []
    for fp in paths:
        rel = fp.replace("\\", "/")
        if _NOISE.search(rel) or Path(rel).suffix.lower() not in CODE_EXT:
            continue
        if focus_l and focus_l not in rel.lower():
            continue
        src = next((r / fp for r in roots if (r / fp).is_file()), None)
        if src is None:
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > 400_000 or not rx.search(text):
            continue
        lines = text.splitlines()
        hits = [i for i, t in enumerate(lines) if rx.search(t)]
        if _GENERATED.search(rel) or (_DOCS.search(rel) and Path(rel).suffix.lower() in _MARKUP):
            generated.append(f"{rel} ({len(hits)} mentions)")
        elif rx.search(Path(rel).name) or rx.search(Path(rel).parent.name):
            core.append((rel, lines, hits))
        else:
            touch.append((rel, lines, hits))
    core.sort(key=lambda x: _layer(x[0]))
    touch.sort(key=lambda x: _layer(x[0]))

    http = []
    for r in store.cx.execute(
            "SELECT e.attributes, s.file_path sf, s.start_line sl, t.qualified_name tq, "
            "t.file_path tf, t.start_line tl FROM edges e JOIN nodes s ON s.id=e.source "
            "JOIN nodes t ON t.id=e.target WHERE e.relation='calls_http'"):
        if rx.search(r["attributes"] or "") or rx.search(r["tq"] or ""):
            import json
            a = json.loads(r["attributes"] or "{}")
            http.append(f"{a.get('http_method')} {a.get('url')}  {r['sf']}:{r['sl']} -> "
                        f"{r['tq']} @ {r['tf']}:{r['tl']}")
    http = sorted(set(http))[:40]
    gen_line = ("=== Generated files that mention it (regenerate, do not hand-edit): "
                + "; ".join(generated[:8])
                + (f"; +{len(generated) - 8} more" if len(generated) > 8 else "")) if generated else ""
    # fixed parts (header, HTTP links, generated list, notes) come off the budget first
    max_chars -= 900 + sum(len(h) + 1 for h in http) + len(gen_line)

    def mk(t, role, levels):
        if field_mode and role == "touch point" and not _layer(t[0])[1]:
            role = "touch point" if _writes(t[1], t[2], root_rx) else "reads it"
        return {"file": t[0], "lines": t[1], "hits": t[2], "role": role, "levels": levels, "lv": 0}
    is_test = lambda e: _layer(e["file"])[1]
    entries = [mk(t, "implements", _CORE_LEVELS) for t in core] +         [mk(t, "touch point", _TOUCH_LEVELS) for t in touch]
    # priority: own files, then places that write/accept it, then tests, then
    # code that only reads it (reports, rollups): optional for a sibling field
    entries.sort(key=lambda e: (e["role"] == "reads it", bool(_AUX.search(e["file"])), is_test(e),
                                e["role"] != "implements"))
    if files_only:
        # the ranked file list without rendering any excerpt, so a caller with a small budget still gets it
        kinds = {"implements": "IMPLEMENTS", "touch point": "TOUCH POINT", "reads it": "READS IT"}
        return [(kinds[e["role"]], e["file"]) for e in entries]

    for e in entries:
        e["body"] = _render_entry(e, full_lines)

    def size(e):
        return len(e["file"]) + 24 + sum(len(x) + 1 for x in e["body"])
    total = sum(map(size, entries))
    while total > max_chars:
        moved = False
        for e in reversed(entries):
            if total <= max_chars:
                break
            if e["lv"] < len(e["levels"]) - 1:
                before = size(e)
                e["lv"] += 1
                e["body"] = _render_entry(e, full_lines)
                total += size(e) - before
                moved = True
        if not moved:
            break
    files = [e for e in entries if e["body"]]
    skipped = [f"{e['file']} ({len(e['hits'])} mentions)" for e in entries if not e["body"]]
    shortened = sum(1 for e in files if e["lv"] > 0)

    out = [f"# feature_slice: {feature!r} (matched: {', '.join(stems)})",
           "IMPLEMENTS = the existing feature's own files: copy their structure.",
           "TOUCH POINT = places elsewhere that must change too (registrations, DTO/model",
           "fields, mappers, queries, templates, tests): mirror each one for the new feature.",
           "READS IT = code that only reads it (reports, rollups, exports): change only if the",
           "new feature must appear there too.",
           "These are the real file contents with line numbers; do not re-read them.", ""]
    if focus_l:
        out.insert(1, f"(focus: only paths containing {focus!r})")
    if shortened or skipped:
        out.append(f"Budget: {shortened} file(s) shown as excerpts, {len(skipped)} only listed at the end.")
        out.append("")
    for f in files:
        out.append(f"=== {f['role'].upper()}: {f['file']}")
        out.extend(f["body"])
        out.append("")
    if http:
        out.append("=== HTTP links (frontend call -> backend handler)")
        out.extend(http)
        out.append("")
    if gen_line:
        out.append(gen_line)
    if focus_l and not entries:
        out.append(f"No file whose path contains {focus!r} mentions {feature!r}.")
    if skipped:
        out.append("=== Not shown (budget): " + "; ".join(skipped))
        out.append("To see them: feature_slice(feature=%r, focus='<part of one of these paths>')." % feature)
    return "\n".join(out)
