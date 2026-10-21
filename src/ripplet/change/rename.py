"""rename_symbol: apply a rename on exactly the lines the graph proves.

change_plan lists the lines; an agent then opens every file to edit it (a
rename of a widely used name cost Haiku 30+ Read calls). This applies the
same plan itself: the old name is replaced, as a whole word, on each
declaration (the symbol and its override family), each call site and each
import line that names it -- and nowhere else. Same-named code of another
symbol is never touched. What the graph cannot prove (strings, comments,
unindexed files) is returned for review instead of being edited.
"""
from __future__ import annotations

import re
from pathlib import Path

from .plan import _code_at, _family, _resolve, _sites, _text_mentions

_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")
_IMPORT_LINE = re.compile(r"^\s*(import|from|use|using|require|include|export\s*\{|#include|local\s+\w+\s*=\s*require)\b")


def _file(roots, rel):
    for r in roots:
        p = Path(r) / rel
        if p.is_file():
            return p
    return None


def rename_symbol(store, cfg, symbol: str, new_name: str, apply: bool = True) -> str:
    new_name = (new_name or "").strip()
    if not _IDENT.match(new_name):
        return f"rename_symbol: {new_name!r} is not a valid identifier."
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    node, others = _resolve(cx, symbol)
    if node is None:
        return (f"rename_symbol: no code symbol named {symbol!r} in the index. "
                f"Use search_symbols to find the exact name.")
    name = node["label"]
    if name == new_name:
        return "rename_symbol: the new name equals the old one."
    fam = _family(cx, node)
    fam_ids = {n["id"] for n in fam}
    named, unnamed = _sites(cx, roots, list(fam_ids), name)
    lines: dict[str, set[int]] = {}
    kind: dict[tuple[str, int], str] = {}      # (file, line) -> decl / import / call
    decls = []
    for n in fam:
        f = (n["file_path"] or "").replace("\\", "/")
        if f and n["start_line"]:
            decls.append((f, int(n["start_line"]), int(n["end_line"] or n["start_line"])))
    for (f, ln), v in named.items():
        lines.setdefault(f, set()).add(int(ln))
        kind[(f, int(ln))] = "import" if v[2] == "imports" or _IMPORT_LINE.match(v[0] or "") else "call"
    # lines that belong to a same-named, different symbol stay as they are
    same = [o for o in others if o["label"] == name and o["id"] not in fam_ids]
    protect: set[tuple[str, int]] = set()
    if same:
        o_named, _ = _sites(cx, roots, [o["id"] for o in same], name)
        protect |= {k for k in o_named if k not in named}
        protect |= {((o["file_path"] or "").replace("\\", "/"), o["start_line"]) for o in same}

    word = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    # on a call line only the call is the symbol: `x = this.types.findPetTypes()`
    # may also hold a local named `findPetTypes` that must keep its name
    callish = re.compile(r"(?:(?<=\.)|(?<=::)|(?<=->))" + re.escape(name) + r"(?![\w$])|(?<![\w$])"
                         + re.escape(name) + r"(?=\s*(?:<[^<>()]*>)?\s*\()")
    # a declaration's node may start at its annotations / doc comment: the
    # name is on the first line of its span that has it
    for f, st, en in decls:
        p = _file(roots, f)
        if p is None:
            lines.setdefault(f, set()).add(st)
            continue
        try:
            src_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            src_lines = []
        hit = next((i for i in range(st, min(en, st + 12, len(src_lines)) + 1)
                    if word.search(src_lines[i - 1])), st)
        lines.setdefault(f, set()).add(hit)
        kind[(f, hit)] = "decl"
    edited: dict[str, list[int]] = {}
    missing: list[str] = []
    for rel in sorted(lines):
        p = _file(roots, rel)
        if p is None:
            missing.append(rel)
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="surrogateescape")
        parts = text.splitlines(keepends=True)
        changed = []
        for ln in sorted(lines[rel]):
            if (rel, ln) in protect or not (1 <= ln <= len(parts)):
                continue
            line_ = parts[ln - 1]
            if kind.get((rel, ln)) == "call" and callish.search(line_):
                new = callish.sub(new_name, line_)
            else:
                new = word.sub(new_name, line_)
            if new != parts[ln - 1]:
                parts[ln - 1] = new
                changed.append(ln)
        if changed:
            edited[rel] = changed
            if apply:
                p.write_bytes("".join(parts).encode("utf-8", errors="surrogateescape"))

    n_lines = sum(len(v) for v in edited.values())
    verb = "renamed" if apply else "would rename"
    out = [f"# rename_symbol: {name} -> {new_name}" + ("" if apply else " (dry run, nothing written)"),
           f"Symbol: {node['qualified_name']} ({node['type']}) at {node['file_path']}:{node['start_line']}",
           f"{verb} {n_lines} line(s) in {len(edited)} file(s): {len(fam)} declaration(s), "
           f"call sites and imports the graph proves.", ""]
    for rel, lns in edited.items():
        out.append(f"-- {rel}: " + ", ".join(str(x) for x in lns))
    if missing:
        out.append("")
        out.append("Files not found on disk (index out of date?): " + ", ".join(missing[:10]))
    if protect:
        out.append("")
        out.append(f"Left alone: {len(protect)} line(s) of a different symbol also named {name}.")
    if unnamed:
        out.append(f"{len(unnamed)} caller(s) reach it without writing its name (framework/DI/dispatch): "
                   "no edit needed for a rename.")
    skip = {(f, ln) for f, lns in lines.items() for ln in lns} | protect | set(unnamed)
    from ..retrieval.uncertain import unverified_sites, render as _render_unv
    unv, unv_total = unverified_sites(cx, roots, fam_ids, name, skip=skip, limit=20)
    if unv_total:
        out += [ln.replace("Not counted above", "NOT renamed") for ln in _render_unv(unv, unv_total, name)]
    skip |= {(f, ln) for f, ln, _c, _r in unv}
    mentions, total = _text_mentions(cx, roots, name, skip, 20)
    if mentions:
        out.append("")
        out.append(f"=== NOT EDITED: {total} other mention(s) of `{name}` (strings, comments, docs, dynamic "
                   "calls, unindexed files). Review these; edit only those that mean this symbol:")
        for f, ln, code in mentions:
            out.append(f"  {f}:{ln}  {code}")
        if total > len(mentions):
            out.append(f"  ... {total - len(mentions)} more")
    else:
        out.append("")
        out.append(f"No other mention of `{name}` in the repository.")
    out.append("")
    out.append("Next: check_change to confirm nothing is left, then run the related tests.")
    return "\n".join(out)
