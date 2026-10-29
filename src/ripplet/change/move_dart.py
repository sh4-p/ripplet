"""move_symbol for Dart: move a top-level class, mixin, enum, typedef, function or const/final to another file.

A Dart file is a library; names reach other libraries through `import` and `export` directives, with `show` / `hide` /
`as` modifiers. So moving a declaration means keeping every library that could see the name seeing it:

* the moved code gets the imports of the old file (relative ones re-based) plus `import 'old.dart' show X;` for the
  public names of the old file it uses (private `_names` can't be reached: that is a refusal);
* a library that exported the old file gets `export 'new.dart' show Name;` too (the package API stays the same, and
  the libraries that import that barrel need nothing);
* any other library that saw the name through imports and uses it gets `import 'new.dart' show Name;` (with the same
  prefix when it used `p.Name`);
* the old file imports the new one when it still uses the name.
Unused imports are only a lint warning in Dart, so imports are carried generously; `dart fix --apply` removes them.
It refuses part files, extensions, declarations other than the supported ones, private names still used in the old
file, and conditional imports.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SKIP = {"node_modules", "build", ".dart_tool", ".git", ".pub-cache", ".symlinks", "ephemeral"}
_parser = None
_CLASSES = ("class_definition", "mixin_declaration", "enum_declaration", "type_alias")


def _parse_bytes(src: bytes):
    global _parser
    if _parser is None:
        import tree_sitter as ts
        import tree_sitter_dart as td
        _parser = ts.Parser(ts.Language(td.language()))
    return _parser.parse(src).root_node


def _txt(src: bytes, n) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _dart_files(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("."))
        out += [Path(dirpath) / f for f in sorted(filenames) if f.endswith(".dart")]
    return out


def _strip_comments(s: str) -> str:
    return re.sub(r"(?s)/\*.*?\*/|(?<![:'\"])//[^\n]*", "", s)


def _doc_start(src: bytes, root, first) -> int:
    """Start of the doc comment / annotations directly above a top-level entry."""
    start, prev = first.start_byte, first.prev_sibling
    while prev is not None and prev.type in ("documentation_comment", "comment", "annotation") \
            and src[prev.end_byte:start].count(b"\n") <= 1:
        start, prev = prev.start_byte, prev.prev_sibling
    return start


def _entries(src: bytes, root) -> list:
    """Top-level declarations: [{name, kind, start, end, multi}] (start includes doc comments and annotations)."""
    out, ch, i = [], root.children, 0
    while i < len(ch):
        c = ch[i]
        if c.type in _CLASSES:
            nm = next((x for x in c.children if x.type in ("identifier", "type_identifier")), None)
            if nm is not None:
                out.append({"name": _txt(src, nm), "kind": c.type.split("_")[0], "start": _doc_start(src, root, c),
                            "end": c.end_byte, "multi": False})
        elif c.type == "extension_declaration":
            nm = next((x for x in c.children if x.type == "identifier"), None)
            members = set()
            for x in _walk(c):
                if x.type in ("function_signature", "getter_signature", "setter_signature", "operator_signature"):
                    ids = [y for y in x.children if y.type == "identifier"]
                    if ids:
                        members.add(_txt(src, ids[0]))
            out.append({"name": _txt(src, nm) if nm is not None else "", "kind": "extension",
                        "start": _doc_start(src, root, c), "end": c.end_byte, "multi": False, "members": members})
        elif c.type == "function_signature" and i + 1 < len(ch) and ch[i + 1].type == "function_body":
            ids = [x for x in c.children if x.type == "identifier"]
            if ids:
                out.append({"name": _txt(src, ids[-1] if len(ids) > 1 else ids[0]), "kind": "function",
                            "start": _doc_start(src, root, c), "end": ch[i + 1].end_byte, "multi": False})
            i += 1
        elif c.type in ("const_builtin", "final_builtin"):
            j = i
            while j < len(ch) and ch[j].type != ";":
                j += 1
            lst = next((x for x in ch[i:j] if x.type == "static_final_declaration_list"), None)
            if lst is not None and j < len(ch):
                names = [_txt(src, next(y for y in d.children if y.type == "identifier"))
                         for d in lst.children if d.type == "static_final_declaration"]
                for k, nm in enumerate(names):
                    out.append({"name": nm, "kind": "const", "start": _doc_start(src, root, c), "end": ch[j].end_byte,
                                "multi": len(names) > 1})
            i = j
        i += 1
    return out


_DIR_RX = re.compile(r"^(import|export)\s+(['\"])(.*?)\2\s*(.*?);\s*$", re.S)


def _directives(src: bytes, root) -> list:
    """[{kind, uri, prefix, shows, hides, node, cond}] for import / export directives."""
    out = []
    for d in root.children:
        if d.type != "import_or_export":
            continue
        t = _txt(src, d).strip()
        m = _DIR_RX.match(re.sub(r"\s+", " ", t)) if "\n" not in t else _DIR_RX.match(t)
        if not m:
            m = re.match(r"^(import|export)\s+(['\"])(.*?)\2(.*);\s*$", t, re.S)
        if not m:
            continue
        rest = m.group(4)
        cond = "if (" in t.split(";")[0] or "if(" in t.split(";")[0]
        pre = re.search(r"\bas\s+(\w+)", rest)
        shows = [x.strip() for g in re.findall(r"\bshow\s+([^;]*?)(?=\bhide\b|$)", rest) for x in g.split(",") if x.strip()]
        hides = [x.strip() for g in re.findall(r"\bhide\s+([^;]*?)(?=\bshow\b|$)", rest) for x in g.split(",") if x.strip()]
        out.append({"kind": m.group(1), "uri": m.group(3), "prefix": pre.group(1) if pre else None, "shows": shows,
                    "hides": hides, "node": d, "cond": cond, "text": t})
    return out


def _passes(d: dict, name: str) -> bool:
    if d["shows"]:
        return name in d["shows"]
    if d["hides"]:
        return name not in d["hides"]
    return True


def _packages(root: Path) -> dict:
    """package name -> package dir, from every pubspec.yaml in the workspace."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        if "pubspec.yaml" in filenames:
            m = re.search(r"(?m)^name:\s*(\S+)", (Path(dirpath) / "pubspec.yaml").read_text(encoding="utf-8", errors="replace"))
            if m:
                out[m.group(1).strip("'\"")] = Path(dirpath)
    return out


def _pkg_of(path: Path, pkgs: dict):
    best = None
    for nm, d in pkgs.items():
        try:
            path.resolve().relative_to(d.resolve())
        except ValueError:
            continue
        if best is None or len(d.parts) > len(pkgs[best].parts):
            best = nm
    return best


def move(store, cfg, symbol: str, to_file: str, apply: bool, resolve, file_of) -> str:
    cx = store.cx
    roots = [Path(str(r)) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    if "," in symbol:
        return "move_symbol: Dart declarations move one at a time; pass one symbol."
    node, _o = resolve(cx, symbol.strip())
    if node is None:                       # a const is a 'variable' node (or none): path::name finds it in the file
        where, _s, nm0 = symbol.strip().rpartition("::")
        rows = [r for r in cx.execute("SELECT * FROM nodes WHERE type='variable' AND label=?", (nm0.split(".")[-1],)).fetchall()
                if (r["file_path"] or "").lower().endswith(".dart")
                and (not where or (r["file_path"] or "").replace("\\", "/").endswith(where.replace("\\", "/")))]
        if len({r["file_path"] for r in rows}) == 1:
            node = rows[0]
        elif where.endswith(".dart"):
            node = {"file_path": where, "label": nm0}
    if node is None:
        return f"move_symbol: no code symbol named {symbol!r} in the index. Use search_symbols to find the exact name."
    rel_a = (node["file_path"] or "").replace("\\", "/")
    path_a = file_of([str(r) for r in roots], rel_a)
    if path_a is None:
        return f"move_symbol: {rel_a} is not on disk (index out of date?)."
    path_a = path_a.resolve()
    root = next((r.resolve() for r in roots if str(path_a).startswith(str(r.resolve()))), roots[0].resolve())
    path_b = Path(to_file)
    if not path_b.is_absolute():
        path_b = root / to_file
    path_b = path_b.resolve()
    if path_b.suffix != ".dart":
        return "move_symbol: to_file must be a .dart file."
    try:
        path_b.relative_to(root)
    except ValueError:
        return "move_symbol: to_file must be inside the workspace."
    if path_b == path_a:
        return "move_symbol: the symbol is already in that file."

    src_a = path_a.read_bytes()
    tree_a = _parse_bytes(src_a)
    name = node["label"].split(".")[-1]
    if any(c.type in ("part_directive", "part_of_directive") for c in tree_a.children):
        return f"move_symbol: {rel_a} is part of a multi-file library (part / part of); nothing was written."
    ents = _entries(src_a, tree_a)
    hit = [e for e in ents if e["name"] == name]
    if not hit:
        return f"move_symbol: {name} is not a top-level class, mixin, enum, typedef, function or const of {rel_a}; nothing was written."
    if len(hit) > 1 or hit[0]["multi"]:
        return f"move_symbol: {name} is declared with other names in one statement or twice; nothing was written."
    ent = hit[0]
    if ent["kind"] == "extension":
        return "move_symbol: extensions are not supported (their users never name them); nothing was written."
    if any(d["cond"] for d in _directives(src_a, tree_a)):
        return f"move_symbol: {rel_a} has conditional imports; nothing was written."
    eol = "\r\n" if b"\r\n" in src_a else "\n"
    text_a = src_a.decode("utf-8", errors="surrogateescape")
    # the entry bytes
    moved = src_a[ent["start"]:ent["end"]]
    moved_code = _strip_comments(moved.decode("utf-8", errors="replace"))
    pkgs = _packages(root)

    names_a = {e["name"] for e in ents}
    remaining_a = src_a[:ent["start"]] + src_a[ent["end"]:]
    rem_code = _strip_comments(remaining_a.decode("utf-8", errors="replace"))
    if name.startswith("_") and re.search(r"(?<![\w$.])" + re.escape(name) + r"\b", rem_code):
        return f"move_symbol: {name} is private and still used in {rel_a}; nothing was written."
    priv = sorted(n for n in names_a - {name} if n.startswith("_")
                  and re.search(r"(?<![\w$.])" + re.escape(n) + r"\b", moved_code))
    if priv:
        return (f"move_symbol: {name} uses {', '.join(priv[:4])}, private to {rel_a}; they can't be reached from another "
                f"library. Nothing was written.")
    need_a = sorted(n for n in names_a - {name} if not n.startswith("_")
                    and re.search(r"(?<![\w$.])" + re.escape(n) + r"\b", moved_code))
    for e in ents:                       # extensions of the old file: users never name them, only call their members
        if e["kind"] != "extension" or not any(re.search(r"\.\s*" + re.escape(m) + r"\b", moved_code) for m in e["members"]):
            continue
        if not e["name"] or e["name"].startswith("_"):
            return (f"move_symbol: {name} calls members of a private or unnamed extension of {rel_a}, which another library "
                    f"can't see. Nothing was written.")
        if e["name"] not in need_a:
            need_a.append(e["name"])

    # --- the graph of the workspace
    files = {}
    for p in _dart_files(root):
        s = src_a if p == path_a else p.read_bytes()
        t = tree_a if p == path_a else _parse_bytes(s)
        files[p] = (s, t, _directives(s, t), {e["name"] for e in _entries(s, t)})
    src_b0 = path_b.read_bytes() if path_b.exists() else None
    if src_b0 is not None and path_b not in files:
        t = _parse_bytes(src_b0)
        files[path_b] = (src_b0, t, _directives(src_b0, t), {e["name"] for e in _entries(src_b0, t)})
    if src_b0 is not None and name in files[path_b][3]:
        return f"move_symbol: {path_b.name} already declares {name}; nothing was written."
    if src_b0 is not None and any(c.type in ("part_directive", "part_of_directive") for c in files[path_b][1].children):
        return f"move_symbol: {path_b.name} is part of a multi-file library; nothing was written."
    # a conditional import / export elsewhere may name the old file as one of its alternatives
    for f, (s, _t, ds, _n) in files.items():
        for d in ds:
            if d["cond"]:
                for u in re.findall(r"['\"]([^'\"]+\.dart)['\"]", d["text"]):
                    if (f.parent / u).resolve() == path_a if not u.startswith("package:") else False:
                        return (f"move_symbol: {f.relative_to(root).as_posix()} reaches {rel_a} through a conditional "
                                f"import; nothing was written.")
    # a `part of` file sees the imports of its owner library and its names count for the owner
    owner: dict = {}
    for f, (s, t, _ds, _n) in files.items():
        if any(c.type == "part_of_directive" for c in t.children):
            m = re.search(r"part\s+of\s+['\"]([^'\"]+)['\"]", s.decode("utf-8", errors="replace"))
            if m and (f.parent / m.group(1)).resolve() in files:
                owner[f] = (f.parent / m.group(1)).resolve()
    for f, o in owner.items():
        files[o] = (files[o][0], files[o][1], files[o][2], files[o][3] | files[f][3])
    parts_of: dict = {}
    for f, o in owner.items():
        parts_of.setdefault(o, []).append(f)

    def target(f: Path, uri: str):
        if uri.startswith("dart:"):
            return None
        if uri.startswith("package:"):
            pk, _s, rest = uri[len("package:"):].partition("/")
            d = pkgs.get(pk)
            return (d / "lib" / rest).resolve() if d is not None else None
        if "://" in uri:
            return None
        return (f.parent / uri).resolve()

    def decl(f: Path, after: bool) -> set:
        base = set(files[f][3]) if f in files else set()
        if after and f == path_a:
            base.discard(name)
        if after and f == path_b:
            base.add(name)
        return base

    def exposes(f: Path, n: str, after: bool, extra: dict, seen: set) -> bool:
        if f in seen or f not in files:
            return False
        seen.add(f)
        if n in decl(f, after):
            return True
        exps = [(d["uri"], d) for d in files[f][2] if d["kind"] == "export"]
        for uri, d in exps:
            t = target(f, uri)
            if t is not None and _passes(d, n) and exposes(t, n, after, extra, seen):
                return True
        if after:
            for t in extra.get(f, []):
                if exposes(t, n, after, extra, seen):
                    return True
        return False

    def sees(f: Path, n: str, after: bool, extra: dict):
        """None, or the prefix ('' for a plain import) through which f sees n."""
        if f in files and n in decl(f, after):
            return ""
        for d in files[f][2] if f in files else []:
            if d["kind"] != "import":
                continue
            t = target(f, d["uri"])
            if t is not None and _passes(d, n) and exposes(t, n, after, extra, set()):
                return d["prefix"] or ""
        return None

    # libraries that re-export the old file need the export of the new one too (API stays the same)
    extra_exports: dict = {}
    for f, (_s, _t, ds, _n) in files.items():
        for d in ds:
            if d["kind"] == "export" and f not in (path_a, path_b):
                tgt = target(f, d["uri"])
                if tgt == path_a and _passes(d, name):
                    extra_exports.setdefault(f, []).append(path_b)

    def uri_for(f: Path, tgt: Path, hint: str | None) -> str:
        pk = _pkg_of(tgt, pkgs)
        in_lib = pk is not None and (pkgs[pk] / "lib") in tgt.parents
        fpk = _pkg_of(f, pkgs)
        f_in_lib = fpk is not None and (pkgs[fpk] / "lib") in f.resolve().parents
        if in_lib and (hint == "package" or fpk != pk or not f_in_lib):
            return f"package:{pk}/{tgt.relative_to(pkgs[pk] / 'lib').as_posix()}"
        return Path(os.path.relpath(tgt, f.parent)).as_posix()

    def style_of(f: Path, tgt: Path):
        for d in files[f][2] if f in files else []:
            if target(f, d["uri"]) == tgt:
                return "package" if d["uri"].startswith("package:") else "relative"
        return None

    edits: dict = {}                 # file -> [(start, end, bytes)]
    adds: dict = {}                  # file -> [directive line]
    word = lambda n: re.compile(r"(?<![\w$.])" + re.escape(n) + r"\b")

    # --- other files
    updated = []
    for f, tgts in extra_exports.items():
        ds = files[f][2]
        for d in ds:
            if d["kind"] == "export" and target(f, d["uri"]) == path_a and _passes(d, name):
                hint = "package" if d["uri"].startswith("package:") else "relative"
                adds.setdefault(f, []).append(f"export '{uri_for(f, path_b, hint)}' show {name};")
                updated.append(f)
                break
    for f, (s, t, ds, _n) in files.items():
        if f in (path_a, path_b) or f in owner:
            continue
        before = sees(f, name, False, {})
        if before is None or sees(f, name, True, extra_exports) is not None:
            continue
        code = _strip_comments("\n".join(files[x][0].decode("utf-8", errors="replace") for x in [f, *parts_of.get(f, [])]))
        if before:
            if not re.search(r"(?<![\w$.])" + re.escape(before) + r"\." + re.escape(name) + r"\b", code):
                continue
            adds.setdefault(f, []).append(f"import '{uri_for(f, path_b, style_of(f, path_a))}' as {before} show {name};")
        else:
            if not word(name).search(code):
                continue
            adds.setdefault(f, []).append(f"import '{uri_for(f, path_b, style_of(f, path_a))}' show {name};")
        updated.append(f)
    # barrels that also re-export (plain exports of the old file inside the old file's own graph are covered above)

    # --- the old file
    a_uses = word(name).search(rem_code) is not None
    if a_uses and sees(path_a, name, True, extra_exports) is None:
        adds.setdefault(path_a, []).append(f"import '{uri_for(path_a, path_b, None)}' show {name};")
    edits.setdefault(path_a, []).append((ent["start"], ent["end"], b""))

    # --- the new file
    carry = []
    code_b = moved_code
    for d in files[path_a][2]:
        if d["kind"] != "import":
            continue
        if d["prefix"] and not re.search(r"(?<![\w$.])" + re.escape(d["prefix"]) + r"\.", code_b):
            continue
        if d["shows"] and not any(word(x).search(code_b) for x in d["shows"]):
            continue
        uri = d["uri"]
        t = target(path_a, uri)
        if t is not None and not uri.startswith("package:"):
            uri = Path(os.path.relpath(t, path_b.parent)).as_posix()
        if t == path_b:
            continue
        line = d["text"]
        line = line.replace(d["uri"], uri, 1)
        carry.append(re.sub(r"\s+", " ", line) if "\n" in line else line)
    if need_a:
        missing = [n for n in need_a if src_b0 is None or sees(path_b, n, True, extra_exports) is None]
        if missing:
            carry.append(f"import '{uri_for(path_b, path_a, None)}' show {', '.join(missing)};")

    rel = lambda p: str(Path(p).resolve().relative_to(root)).replace("\\", "/")
    out_files: dict = {}
    for f, es in list(edits.items()) + [(f, []) for f in adds if f not in edits]:
        s = files[f][0]
        body = s
        for a, b, rep in sorted(es, key=lambda e: e[0], reverse=True):
            body = body[:a] + rep + body[b:]
        if es:
            body = re.sub(rb"(\r?\n){3,}", (eol * 2).encode(), body)
        if f in adds:
            t = _parse_bytes(body)
            dirs = [c for c in t.children if c.type == "import_or_export"]
            lines = [(l + eol).encode() for l in adds[f] if l.encode() not in body]
            if dirs:
                pos = dirs[-1].end_byte
                body = body[:pos] + eol.encode() + eol.join(l.decode().rstrip() for l in lines).encode() + body[pos:]
            else:
                lib = next((c for c in t.children if c.type == "library_name"), None)
                pos = lib.end_byte if lib is not None else 0
                head = eol.encode() if lib is not None else b""
                body = body[:pos] + head + b"".join(lines) + (eol.encode() if lines else b"") + body[pos:]
        out_files[f] = body
    entry_text = moved.decode("utf-8", errors="surrogateescape")
    if src_b0 is None:
        head = (eol.join(carry) + eol + eol) if carry else ""
        out_files[path_b] = (head + entry_text.rstrip() + eol).encode("utf-8", errors="surrogateescape")
    else:
        t = _parse_bytes(src_b0)
        dirs = [c for c in t.children if c.type == "import_or_export"]
        body = src_b0
        new_lines = [l for l in carry if l.encode() not in body]
        if new_lines:
            if dirs:
                pos = dirs[-1].end_byte
                body = body[:pos] + eol.encode() + eol.join(new_lines).encode() + body[pos:]
            else:
                lib = next((c for c in t.children if c.type == "library_name"), None)
                pos = lib.end_byte if lib is not None else 0
                body = body[:pos] + (eol.encode() if lib is not None else b"") + eol.join(new_lines).encode() + eol.encode() + eol.encode() + body[pos:]
        if not body.endswith(b"\n"):
            body += eol.encode()
        out_files[path_b] = body + eol.encode() + entry_text.encode("utf-8", errors="surrogateescape").rstrip() + eol.encode()

    if apply:
        for p, data in out_files.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    verb = "moved" if apply else "would move"
    lines = [f"# move_symbol: {name}" + ("" if apply else " (dry run, nothing written)"),
             f"{verb} {name} ({ent['kind']}) {rel_a} -> {rel(path_b)}"]
    if carry:
        lines.append(f"imports carried to the new file: {len(carry)} (unused ones are lint warnings only; `dart fix --apply` removes them)")
    others = [f for f in out_files if f not in (path_a, path_b)]
    if others:
        lines.append(f"files updated: {len(others)}")
        lines += [f"  {rel(p)}" for p in others[:12]]
        if len(others) > 12:
            lines.append(f"  ... {len(others) - 12} more")
    if extra_exports:
        lines.append("re-exports added (the package API keeps the name): " + ", ".join(rel(f) for f in extra_exports))
    if apply:
        lines += ["", "Done: the declaration, its imports and the references are already changed. "
                      "Do NOT Read or Edit these files again to redo it.",
                  "Next: check_change, then dart analyze and the related tests."]
    return "\n".join(lines)
