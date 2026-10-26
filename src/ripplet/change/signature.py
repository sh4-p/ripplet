"""change_signature: add or remove a parameter on the declaration and on every call site the graph proves.

Changing a function's parameters costs an agent one edit turn per call site (32 call sites: 80 turns). This
does the declaration (the symbol and its override family) and each call site it can prove in one call, on
exactly those lines and nowhere else. The argument lists are read from the source (strings, comments, nested
brackets and generic type arguments are skipped), so multi-line calls and calls inside calls are handled; a
site it cannot parse is reported, never guessed. Calls on a receiver whose type could not be established are
listed for review, not edited.
"""
from __future__ import annotations

import re

from .plan import _family, _resolve, _sites
from .rename import _IMPORT_LINE, _file

_SELF = ("self", "cls", "this")
_HASH_COMMENT = (".py", ".rb", ".php", ".sh", ".pl")


def _generic_end(text: str, i: int):
    """Index of the `>` closing the type arguments that open at text[i] == '<', else None."""
    if i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_?]"):
        return None
    depth, j = 0, i
    while j < len(text) and j - i < 240:
        c = text[j]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return j
        elif not (c.isalnum() or c in "_.,?&*[] \t\r\n$:"):
            return None
        j += 1
    return None


def scan_args(text: str, open_idx: int, ext: str = ""):
    """The argument spans of the list opening at text[open_idx] == '(': (spans, close_idx) or None.
    Each span is (start, end) of the trimmed argument; an empty list has no spans."""
    n, i, depth, start = len(text), open_idx + 1, 0, open_idx + 1
    raw: list = []
    hash_comment = ext in _HASH_COMMENT
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            if text.startswith(ch * 3, i):
                end = text.find(ch * 3, i + 3)
                i = end + 3 if end >= 0 else n
                continue
            j = i + 1
            while j < n and text[j] != ch:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and ch != "`":
                    break
                j += 1
            i = j + 1
            continue
        if ch == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == "#" and hash_comment and (i == 0 or text[i - 1] in " \t\n"):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                if ch != ")":
                    return None
                raw.append((start, i))
                spans = []
                for a, b in raw:
                    seg = text[a:b]
                    lead = len(seg) - len(seg.lstrip())
                    trail = len(seg) - len(seg.rstrip())
                    if seg.strip():
                        spans.append((a + lead, b - trail))
                return spans, i
            depth -= 1
        elif ch == "<":
            e = _generic_end(text, i)
            if e is not None:
                i = e + 1
                continue
        elif ch == "," and depth == 0:
            raw.append((start, i))
            start = i + 1
        i += 1
    return None


def _open_paren(text: str, name_end: int):
    """Index of the `(` after a callee name (type arguments and whitespace allowed), else None."""
    j = name_end
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j < len(text) and text[j] == "<":
        e = _generic_end(text, j)
        if e is None:
            return None
        j = e + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
    return j if j < len(text) and text[j] == "(" else None


def _param_names_index(params: list, texts: list, name: str):
    """Index of the declared parameter called `name`, counting from the first non-receiver parameter."""
    skip = 1 if texts and texts[0].strip() in _SELF else 0
    word = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    for k, t in enumerate(texts):
        head = re.split(r"\s*=\s*(?!=)", t, maxsplit=1)[0]
        if word.search(head):
            return k - skip if k >= skip else None
    return None


def _comment_line(text: str, idx: int) -> bool:
    """The match at idx sits on a line that is (or has started) a comment: `* {@link f(x)}`, `// f(x)`, `# f(x)`."""
    start = text.rfind("\n", 0, idx) + 1
    head = text[start:idx]
    stripped = head.lstrip()
    return stripped.startswith(("//", "*", "#", "/*", "--")) or "//" in head


def _line_offsets(text: str) -> list:
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def _pname(t: str):
    """The name of one declared parameter as its text spells it, or None when it is not a single plain name."""
    head = re.split(r"\s*=\s*(?!=)", t.strip(), maxsplit=1)[0]
    if head.lstrip().startswith(("*", "...")):
        return None
    if ":" in head:
        head = head.split(":", 1)[0]
    ids = re.findall(r"[A-Za-z_$][\w$]*", head)
    return ids[-1] if ids else None


def _permute(text, spans, perm):
    """(offset, delete_length, insert) putting the items of one list in the order perm (new slot j takes item perm[j]),
    keeping the separators and line breaks between the slots where they were."""
    if list(perm) == sorted(perm):
        return None
    items = [text[a:b] for a, b in spans]
    out = []
    for j, k in enumerate(perm):
        out.append(items[k])
        if j < len(spans) - 1:
            out.append(text[spans[j][1]:spans[j + 1][0]])
    return spans[0][0], spans[-1][1] - spans[0][0], "".join(out)


def _edit_list(text, spans, close, open_idx, op, position):
    """(offset, delete_length, insert) turning one argument/parameter list into the changed one, or None."""
    if op[0] == "add":
        snippet = op[1]
        if position is None or position >= len(spans):
            if not spans:
                return open_idx + 1, 0, snippet
            return spans[-1][1], 0, ", " + snippet
        return spans[position][0], 0, snippet + ", "
    k = op[1]                                             # remove the argument at index k
    if k >= len(spans):
        return None
    if len(spans) == 1:
        return spans[0][0], spans[0][1] - spans[0][0], ""
    if k < len(spans) - 1:
        return spans[k][0], spans[k + 1][0] - spans[k][0], ""
    return spans[k - 1][1], spans[k][1] - spans[k - 1][1], ""


def change_signature(store, cfg, symbol: str, add_param: dict | None = None, remove_param: str | None = None,
                     apply: bool = True, reorder_params: list | None = None) -> str:
    """add_param: {decl, value?, position?}  decl is the parameter as the declaration spells it (`retries: Int = 0`),
    value what every call site passes (omit it where the default covers them), position a 0-based index (default:
    last). remove_param: the name of the parameter to drop. reorder_params: every parameter name, in the new order."""
    reorder = [str(x).strip() for x in reorder_params] if reorder_params else None
    if bool(add_param) + bool(remove_param) + bool(reorder) != 1:
        return ("change_signature: pass exactly one of add_param={decl, value?, position?}, remove_param=<name> "
                "or reorder_params=[<every parameter name in the new order>].")
    if reorder and len(set(reorder)) != len(reorder):
        return "change_signature: reorder_params lists a name twice."
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    node, _others = _resolve(cx, symbol)
    if node is None:
        return (f"change_signature: no code symbol named {symbol!r} in the index. "
                f"Use search_symbols to find the exact name.")
    name = node["label"]
    if add_param:
        decl = str(add_param.get("decl") or "").strip()
        if not decl:
            return "change_signature: add_param needs `decl`, the parameter as the declaration spells it."
        value = add_param.get("value")
        value = None if value in (None, "") else str(value)
        pos = add_param.get("position")
        pos = None if pos in (None, "", "last", -1) else int(pos)
        op_decl, op_call = ("add", decl), (("add", value) if value is not None else None)
    else:
        op_decl = op_call = None
    fam = _family(cx, node)
    fam_ids = {n["id"] for n in fam}
    named, unnamed = _sites(cx, roots, list(fam_ids), name)
    same = [o for o in _others if o["label"] == name and o["id"] not in fam_ids]
    protect: set = set()
    if same:
        o_named, _ = _sites(cx, roots, [o["id"] for o in same], name)
        protect |= {k for k in o_named if k not in named}
        protect |= {((o["file_path"] or "").replace("\\", "/"), o["start_line"]) for o in same}
    word = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")

    decl_lines: dict = {}
    for n in fam:
        f = (n["file_path"] or "").replace("\\", "/")
        if f and n["start_line"]:
            decl_lines.setdefault(f, set()).add((int(n["start_line"]), int(n["end_line"] or n["start_line"])))
    call_lines: dict = {}
    for (f, ln), v in named.items():
        if v[2] == "imports" or _IMPORT_LINE.match(v[0] or ""):
            continue
        if (f, ln) in protect:
            continue
        call_lines.setdefault(f, set()).add(ln)

    edits_by_file: dict = {}
    report_decl, report_call, skipped = [], [], []
    remove_index = None
    perm = None
    decl_parens: set = set()
    texts = {}
    meta: dict = {}

    def load(rel):
        if rel not in meta:
            p = _file(roots, rel)
            if p is None:
                skipped.append(f"{rel}: file not found on disk (index out of date?)")
                meta[rel] = None
            else:
                text = p.read_bytes().decode("utf-8", errors="surrogateescape")
                texts[rel] = (p, text)
                meta[rel] = (text, p.suffix.lower(), _line_offsets(text))
        return meta[rel]

    # pass 1, declarations: they fix the position of a parameter to remove, whatever file order the calls come in
    for rel in sorted(decl_lines):
        loaded = load(rel)
        if loaded is None:
            continue
        text, ext, offs = loaded
        edits = edits_by_file.setdefault(rel, [])
        for st, en in sorted(decl_lines.get(rel, ())):
            lo = offs[min(st - 1, len(offs) - 1)]
            hi = offs[min(en, len(offs) - 1)] if en < len(offs) else len(text)
            found = None
            for m in word.finditer(text, lo, max(hi, lo + 1) if hi > lo else len(text)):
                if _comment_line(text, m.start()):
                    continue
                o = _open_paren(text, m.end())
                if o is not None:
                    found = (m, o)
                    break
            if found is None:
                skipped.append(f"{rel}:{st}: declaration of {name} has no parameter list I can read")
                continue
            scanned = scan_args(text, found[1], ext)
            if scanned is None:
                skipped.append(f"{rel}:{st}: declaration parameter list could not be parsed")
                continue
            spans, close = scanned
            decl_parens.add((rel, found[1]))
            if reorder:
                skip = 1 if spans and text[spans[0][0]:spans[0][1]].strip() in _SELF else 0
                ptexts = [text[a:b] for a, b in spans][skip:]
                names = [_pname(t) for t in ptexts]
                if None in names or len(set(names)) != len(names):
                    skipped.append(f"{rel}:{st}: declaration has parameters I cannot name one by one "
                                   f"(grouped, variadic or unusual): reorder it by hand")
                    continue
                if sorted(names) != sorted(reorder):
                    skipped.append(f"{rel}:{st}: its parameters are ({', '.join(names)}), not the names given")
                    continue
                this = [names.index(n) for n in reorder]
                if perm is not None and this != perm:
                    skipped.append(f"{rel}:{st}: parameters are in a different order than the first declaration")
                    continue
                if ext == ".py":
                    seen_default, bad = False, False
                    for k in this:
                        t = ptexts[k]
                        has_def = re.search(r"=(?!=)", t) is not None
                        if seen_default and not has_def:
                            bad = True
                        seen_default = seen_default or has_def
                    if bad:
                        skipped.append(f"{rel}:{st}: a parameter without a default would follow one with a default")
                        continue
                perm = this
                e = _permute(text, spans[skip:], perm)
                if e is None:
                    continue
            elif remove_param:
                ptexts = [text[a:b] for a, b in spans]
                k = _param_names_index(spans, ptexts, remove_param)
                if k is None:
                    skipped.append(f"{rel}:{st}: no parameter named {remove_param} in this declaration")
                    continue
                skip = 1 if ptexts and ptexts[0].strip() in _SELF else 0
                remove_index = k if remove_index is None else remove_index
                e = _edit_list(text, spans, close, found[1], ("remove", k + skip), None)
            else:
                skip = 1 if spans and text[spans[0][0]:spans[0][1]].strip() in _SELF else 0
                e = _edit_list(text, spans, close, found[1], op_decl, None if pos is None else pos + skip)
            if e is None:
                skipped.append(f"{rel}:{st}: declaration could not be edited")
                continue
            edits.append(e + ("decl", text.count("\n", 0, e[0]) + 1))
            report_decl.append((rel, text.count("\n", 0, e[0]) + 1))
    # pass 2, call sites (an add with no `value` leaves them alone: the default covers them)
    if reorder and perm is None:
        if not skipped:
            return "change_signature: the order already is the one asked for, nothing to change."
        return "\n".join(["change_signature: no declaration could be reordered; nothing was written."] + skipped[:10])
    for rel in sorted(call_lines) if (op_call is not None or remove_param or reorder) else ():
        loaded = load(rel)
        if loaded is None:
            continue
        text, ext, offs = loaded
        edits = edits_by_file.setdefault(rel, [])
        for ln in sorted(call_lines.get(rel, ())):
            if not (1 <= ln <= len(offs)):
                continue
            lo = offs[ln - 1]
            hi = offs[ln] if ln < len(offs) else len(text)
            for m in word.finditer(text, lo, hi):
                o = _open_paren(text, m.end())
                if o is None or (rel, o) in decl_parens or _comment_line(text, m.start()):
                    continue
                before = text[lo:m.start()]
                if re.search(r"\b(def|fun|func|function|void)\s+$", before):
                    continue
                scanned = scan_args(text, o, ext)
                if scanned is None:
                    skipped.append(f"{rel}:{ln}: call could not be parsed")
                    continue
                spans, close = scanned
                if reorder:
                    if not spans:
                        continue
                    texts_a = [text[a:b] for a, b in spans]
                    named_a = [re.match(r"\s*[A-Za-z_]\w*\s*=(?!=)", t) is not None for t in texts_a]
                    if any(t.lstrip().startswith(("*", "...")) for t in texts_a):
                        skipped.append(f"{rel}:{ln}: call spreads arguments (*, ...): reorder it by hand")
                        continue
                    if all(named_a):
                        continue                       # every argument is named: the order does not matter
                    if any(named_a) or len(spans) != len(perm):
                        npos = named_a.index(True) if any(named_a) else len(spans)
                        if any(named_a[npos:]) and not all(named_a[npos:]):
                            skipped.append(f"{rel}:{ln}: call has a positional argument after a named one: reorder it by hand")
                        elif npos <= len(perm) and set(perm[:npos]) == set(range(npos)):
                            e = _permute(text, spans[:npos], perm[:npos])      # only the ones it passes by position move
                            if e is not None:
                                edits.append(e + ("call", ln))
                                report_call.append((rel, ln))
                        else:
                            skipped.append(f"{rel}:{ln}: call passes {npos} of {len(perm)} arguments by position: reorder it by hand")
                        continue
                    e = _permute(text, spans, perm)
                    if e is None:
                        continue
                elif remove_param:
                    idx = remove_index
                    named_arg = next((k for k, (a, b) in enumerate(spans)
                                      if re.match(r"\s*" + re.escape(remove_param) + r"\s*[:=](?!=)", text[a:b])), None)
                    if named_arg is not None:
                        idx = named_arg
                    if idx is None:
                        skipped.append(f"{rel}:{ln}: the position of {remove_param} is unknown (declaration not found)")
                        continue
                    if idx >= len(spans):
                        continue                       # this call does not pass it (the default covers it): nothing to remove
                    e = _edit_list(text, spans, close, o, ("remove", idx), None)
                else:
                    if pos is not None and pos > len(spans):
                        skipped.append(f"{rel}:{ln}: call has {len(spans)} argument(s); the new one goes at {pos}")
                        continue
                    call_op, call_pos = op_call, pos
                    if ext == ".py" and not re.match(r"\s*\*{0,2}[A-Za-z_]\w*\s*=(?!=)", op_call[1]):
                        # Python: a positional value after a keyword/star argument is a SyntaxError, so name it
                        pname = re.match(r"\s*\*{0,2}\s*([A-Za-z_]\w*)", add_param["decl"])
                        kw = any(re.match(r"\s*(\*|[A-Za-z_]\w*\s*=(?!=))", text[a:b]) for a, b in spans)
                        if pname and (kw or (pos is not None and pos < len(spans))):
                            call_op, call_pos = ("add", f"{pname.group(1)}={op_call[1]}"), None
                    e = _edit_list(text, spans, close, o, call_op, call_pos)
                if e is None:
                    skipped.append(f"{rel}:{ln}: call could not be edited")
                    continue
                edits.append(e + ("call", ln))
                report_call.append((rel, ln))

    # apply: from the end of each file backwards, never two edits on the same characters
    written, dry = {}, []
    for rel, edits in edits_by_file.items():
        if not edits:
            continue
        p, text = texts[rel]
        new, last_lo, done = text, None, 0
        for off, dlen, ins, _kind, ln in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            if last_lo is not None and off + dlen > last_lo:
                skipped.append(f"{rel}:{ln}: overlaps another edit of this call (edit it by hand)")
                continue
            new = new[:off] + ins + new[off + dlen:]
            last_lo = off
            done += 1
            line_no = text.count("\n", 0, off) + 1
            dry.append((rel, line_no, text.splitlines()[line_no - 1].strip()[:110]))
        if apply and new != text:
            p.write_bytes(new.encode("utf-8", errors="surrogateescape"))
        written[rel] = done

    what = (f"add `{add_param['decl']}`" if add_param else f"remove `{remove_param}`" if remove_param
            else f"reorder to ({', '.join(reorder)})")
    verb = "changed" if apply else "would change"
    n_edits = sum(written.values())
    out = [f"# change_signature: {name}: {what}" + ("" if apply else " (dry run, nothing written)"),
           f"Symbol: {node['qualified_name']} ({node['type']}) at {node['file_path']}:{node['start_line']}",
           f"{verb} {len(report_decl)} declaration(s) and {len(report_call)} call site(s) "
           f"in {sum(1 for v in written.values() if v)} file(s) ({n_edits} edit(s)).", ""]
    for rel in sorted(written):
        lines = sorted({ln for (f, ln, _t) in dry if f == rel})
        if lines:
            out.append(f"-- {rel}: " + ", ".join(str(x) for x in lines))
    if not apply:
        out += ["", "Lines that would change:"] + [f"  {f}:{ln}  {t}" for f, ln, t in sorted(set(dry))[:40]]
    if skipped:
        out += ["", f"=== NOT EDITED ({len(skipped)}): edit these by hand"] + [f"  {s}" for s in skipped[:25]]
    if protect:
        out += ["", f"Left alone: {len(protect)} line(s) of a different symbol also named {name}."]
    skip_set = {(f, ln) for f, lns in call_lines.items() for ln in lns} | protect | set(unnamed)
    from ..retrieval.uncertain import render as _render_unv
    from ..retrieval.uncertain import unverified_sites
    unv, unv_total = unverified_sites(cx, roots, fam_ids, name, skip=skip_set, limit=20)
    if unv_total:
        out += [ln.replace("Not counted above", "NOT changed") for ln in _render_unv(unv, unv_total, name)]
    if unnamed:
        out.append(f"{len(unnamed)} caller(s) reach it without writing its name (framework/DI/dispatch): check them.")
    if apply:
        left = len(skipped) + unv_total + len(unnamed)
        if left:
            out += ["", f"Done for every site listed above; do NOT re-edit them. Only the {left} item(s) marked above remain."]
        else:
            out += ["", "Done: every site was changed. Do NOT Read or Edit these files again to redo it."]
    out += ["", "Next: check_change to confirm nothing is left, then run the related tests."]
    return "\n".join(out)
