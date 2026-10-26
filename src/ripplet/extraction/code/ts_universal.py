"""Unified tree-sitter extractor (Phase 38 Wave 0: Go pilot; Wave 1: Rust, Java;
Wave 2: C#, PHP; Wave 3: C++, C, Kotlin, Lua; Wave 4: Swift, Dart, GDScript).

One engine, parameterized by ``language_id``; per-language node tables
drive a shared walk for scopes, symbols, imports, calls and heritage.
The JsTsExtractor(language_id) pattern generalized: cross-file linking
(import targets, call targets) stays unresolved placeholders here, bound
by repo-stage resolvers (``go_resolve``, ``rust_resolve``,
``java_resolve``, ``csharp_resolve``, ``php_resolve``,
``cpp_resolve``, ``kotlin_resolve``, ``lua_resolve``), where the
repository file list is available.
"""
from __future__ import annotations
import re
from . import tree_sitter as ts_adapter
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import (
    file_node_id, canonical_symbol_id, canonical_edge_id, content_hash,
)
from ...core.provenance import make_evidence

UNIV_TS_VERSION = "116"  # v116: Dart `a?.m()` calls, field initializers and constructor initializer lists are calls; v115: C++ constructor initializer lists, default member initializers and `d.m<T>()` template member calls are calls; v114: Kotlin one-argument explicit generic calls `f<T>(x)` / `o.f<T>(x)` (read as comparisons by the grammar) are calls; v113: Swift deinit bodies are walked for calls; v112: Java static/instance initializer blocks and enum constant arguments are walked for calls; v111: Kotlin init blocks, secondary/primary constructor defaults and enum entry arguments are walked for calls; v110: Kotlin default argument values are walked for calls; v109: Rust `f::<T>()`, Go `f[T]()` and Swift `$0 + f()` calls are calls; v108: C# `d?.M()` and PHP `$d?->m()` calls are calls; v107: a call chained on a new line (`d` / `.m()`) is recorded on the line of the called name, not where its receiver starts; v106: Kotlin `!f()` / `!a.f()` calls (the grammar nests the `!` inside the call) are no longer dropped; v105: a generic supertype/type path (`Base<T>`) no longer glues its type arguments into the name (Base.T); v104: Kotlin generic extension receivers (Column<T>. -> Column); v103: Kotlin `where` as a name retried like set/get; v102: PHP docblock @return class when declared mixed; v101: C# awaited Task<T> results, call markers and field qualifiers; v100: Swift chained calls located at the method name; v99: Swift implicit member calls (.m()) never on self, property initializer calls; v98: Swift closure params typed by the callee's callback type, property types, field paths; v97: Lua module-function aliases; v96: Lua functions named a.b:c / a.b.c; v95: Kotlin calls on locals/params marked local_callee; v94: Kotlin object heritage; v93: PHP container calls and (new X)->m() typed; v92: PHP calls inside arrow functions; v91: Ruby multiple-assignment, rescue and for-loop locals known; v90: Ruby bare receivers and class-body bare names as calls; v89: every #elif/#else branch (up to 3) read for calls; v88: attribute macros after class/struct blanked; v87: C/C++ retries judged by bytes under ERROR; v86: C++ params/locals/fields typed through template and qualified types; v85: C++ class members inside #if blocks; v84: C++ class field types recorded; v83: C++ operator definitions kept, qualified calls in macro bodies; v82: C++ class template specializations walked; v81: X_NAMESPACE_BEGIN macros open namespace x when x is a path directory; v80: calls in function-like macro bodies; v79: C `MACRO(name) { }` definitions named by name; v78: every #else call site kept; v77: C/C++ retry also blanks statement and template-parameter macro calls; v76: C++ member templates (template methods in class bodies); v75: directive lines kept when blanking ALL-CAPS macros; v74: C/C++ calls in #else branches kept (second fold); v73: Java final locals typed, new X() chain roots; v72: Java call sites on the method name; v71: Java enhanced-for locals, this.m() owner; v70: undeclared return types inferred from the body; v69: Rust const initializer calls, item macros accepted with a few stray bytes; v68: Rust const/static values typed, for-loops over them, dyn T + bounds; v67: call-site facts also keyed at the name line; v66: Go call sites at the method name, field-of-local markers; v65: Go field-chain and composite-literal receivers; v64: Go locals copied from package vars; v63: Go := locals marked receiver_local; v62: Go //go:build constraints recorded on the module; v61: Go embedded interfaces as inherits; v60: Go package-level variables as typed nodes; v59: Go embedded struct fields as inherits; v58: Go interface method specs as methods, type assertions and method-result markers type locals; v57: Rust `extern crate x as y` recorded as a use; v56: Rust dyn/impl Trait and Box/Rc/Arc params typed; v55: calls inside item-position Rust macros owned by the module; v54: Rust project macros with expression args parsed (per macro name, only if the file does not parse worse); v52: Rust inline `mod m { }` items qualified as <module>.m.<item>; v51: untyped locals marked receiver_local; v50: nullable unions and generic constructors type locals; v49: generic type annotations type locals; v46: module-scope locals typed; v45: locals declared inside closures typed for the enclosing symbol; v44: Kotlin infix get/set after a block; v43: Kotlin star imports recognised (`*` beside the path); v42: Kotlin retry judged by bytes under ERROR; v41: Kotlin when-guards (`is X if c ->`) no longer drop the file; v40: Swift `!x.m()` calls; v39: Swift default params optional; v38: Blade directive args as an array literal; v37: C# generic member calls (x.M<T>()); v36: calls inside Rust std expression macros (assert!, format!, ..); v35: Go package-level var/const initializer calls; self.a.b receiver paths; v34: Laravel Blade views parsed as PHP; v33: PHP locals typed by a function call, promoted ctor properties typed; v32: (located at the name) v31: Lua colon calls on expression receivers; v30: Lua module tables (`m = lazy_require { u = "a.u" }`, m.u.f()); v29: Ruby methods with params after *rest no longer crash the file; v28: Rust impls for non-nominal types (&[u8], tuples, dyn Trait); v27: Lua requires bound inside functions; v26: Java enum methods (enum_body_declarations) and enum constants with bodies; v25: Java anonymous classes in fields (named by the field), their methods and field initializer calls; v24: Ruby; v23: Go struct field types; calls through a receiver field; v22: Go import names drop /vN and .vN suffixes; v21: Lua chunk-level assignment calls, inline require(..).f(); v20: Rust nested use trees and block-scoped use; v19: PHP qualified function calls (\f(), A\f()); v18: C# constructors (<init>) and property/field/operator bodies; v17: every call site kept (occurrences); v16: PHP trait use; v15: multi-line C# qualifiers; v14: C# top-level statements; v13: C# primary-ctor params typed; v12: locals typed inside test methods; v11: C# qualified calls/new typed; v10: Kotlin soft-keyword local names retried; v9: PHP heritage inside base_clause; v8: GoogleTest/Catch2 test macros; v7: Rust same-name methods across impl blocks split; v6: C/C++ preproc fold, ERROR/extern "C"/#if descent; v4: overloads split (#overloadN), per-site unresolved ids, arity/chain/static annotations (v3: receiver typing)

# Language ids served here (registry mirrors ts_adapter.UNIV_LANGUAGE_IDS;
# asserting the pair keeps table and loader from silently diverging).
UNIVERSAL_LANGUAGES = ("go", "rust", "java", "c_sharp", "php",
                       "cpp", "c", "kotlin", "lua",
                       "swift", "dart", "gdscript", "ruby")


def module_qname_go(rel_path: str) -> str:
    """Repo-relative module identity for Go files: strip .go, collapse
    trailing /index, dot-separated (same doctrine as jsts module_qname).
    The Go package clause name is recorded on the module node; directory
    identity is what imports resolve (go.mod module-path mapping is a
    later wave)."""
    return module_qname_univ(rel_path, (".go",))


def module_qname_univ(rel_path: str, exts: tuple[str, ...]) -> str:
    """Dir-based dotted module identity, stripping one known source
    suffix (agent-found inconsistency: Rust/Java qnames kept `.rs`/
    `.java` while Go stripped `.go`)."""
    p = rel_path
    low = p.lower()
    for ext in exts:
        if low.endswith(ext):
            p = p[: -len(ext)]
            break
    if p.endswith("/index"):
        p = p[: -len("/index")]
    return (p.replace("/", ".") or ".")


def _loc(node) -> Location:
    return Location(node.start_point[0] + 1, node.start_point[1],
                    node.end_point[0] + 1, node.end_point[1])


def _text(node, raw: bytes, limit: int = 200) -> str:
    try:
        return node.text.decode("utf-8", "ignore")[:limit]
    except Exception:
        return ""


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"', "`"):
        return s[1:-1]
    return s


def _named(node, raw: bytes) -> str:
    for c in node.children:
        if c.type in ("identifier", "type_identifier", "field_identifier",
                      "package_identifier"):
            return _text(c, raw, 120)
    return ""


class UniversalExtractor:
    """Tree-sitter extractor for universal languages (go/rust/java)."""

    def __init__(self, language_id: str = "go"):
        if language_id not in UNIVERSAL_LANGUAGES:
            raise ValueError(f"unsupported universal language: {language_id}")
        self.language_id = language_id

    # ------------------------------------------------------------------ main
    def extract(self, rel_path: str, text: str, repo_id: str,
                project_id: str) -> ExtractionResult:
        lang = self.language_id
        res = ExtractionResult(extractor=f"ts-{lang}",
                               extractor_version=UNIV_TS_VERSION)
        raw = text.encode("utf-8", errors="ignore")
        ev = make_evidence(rel_path, text[:300], f"ts-{lang}", raw)
        res.evidence.append(ev)
        try:
            parser = ts_adapter.get_univ_parser(lang)
        except ImportError as e:
            res.warnings.append(
                f"ts-{lang} unavailable for {rel_path}: {e}. "
                f"Install with: pip install 'ripplet[treesitter-multilang]'")
            res.partial = True
            return _unavailable_file(res, ev, repo_id, rel_path, project_id,
                                    lang, raw, "none")
        except Exception as e:
            res.warnings.append(f"ts-{lang} init failed for {rel_path}: {e}")
            res.partial = True
            return _unavailable_file(res, ev, repo_id, rel_path, project_id,
                                    lang, raw, "none")
        orig_raw = raw
        if lang in ("c", "cpp"):
            raw = _blank_export_macros(raw)
            raw = _expand_ns_macros(raw, rel_path)
        rust_user_macros: set = set()
        if lang == "rust":
            raw = _RUST_MACRO.sub(lambda m: m.group(1) + b"_", raw)
            # project macros taking expressions (`rgtest!(name, |dir, cmd| {..})`):
            # same rewrite, kept only if the file does not parse worse
            names = sorted({m.group(1) for m in _RUST_ANY_MACRO.finditer(raw)} - {b"macro_rules"})
            if names:
                try:
                    base_err = _error_bytes(parser.parse(raw).root_node)
                    for nm in names[:24]:          # one decision per macro name
                        rx = re.compile(rb"(?<![\w!])(" + re.escape(nm) + rb")!(?=\s*[(\[])")
                        cand, n_inv = rx.subn(lambda m: m.group(1) + b"_", raw)
                        e2 = _error_bytes(parser.parse(cand).root_node)
                        # item-position uses (`rgtest!(..);` at file level) leave a few
                        # stray bytes; anything more means the args are not expressions
                        if e2 <= base_err + 8 * n_inv:
                            raw, base_err = cand, e2
                            rust_user_macros.add(nm.decode("utf-8", "ignore") + "_")
                except Exception:
                    pass
        if lang == "php" and rel_path.lower().endswith(".blade.php"):
            from .blade import blade_to_php
            raw = blade_to_php(raw)
        try:
            tree = parser.parse(raw)
        except Exception as e:
            res.warnings.append(f"ts-{lang} parse crashed on {rel_path}: {e}")
            res.partial = True
            return _unavailable_file(res, ev, repo_id, rel_path, project_id,
                                    lang, raw, "none")
        from . import tree_sitter as _tsa
        _ = _tsa  # keep adapter import explicit for capability parity
        root = tree.root_node
        errors, missing = _count_tree_issues(root)
        folded = caps_used = False
        pre_fold = raw
        if lang in ("c", "cpp") and (errors or missing):
            # tree-sitter parses `#if` blocks as syntax, so one inside an
            # initializer list or a split signature derails the rest of
            # the file. Retry with directives blanked (first live branch
            # kept, byte offsets and lines unchanged); keep the better tree.
            pre_fold = raw
            raw2 = _fold_preproc(raw)
            # Second retry: also blank attribute-like ALL-CAPS macros
            # (`FMT_CONSTEXPR auto f()`, `FMT_BEGIN_NAMESPACE`) that the
            # grammar cannot expand. The tree with fewest errors wins.
            # Third: also blank statement-like / template-parameter macro
            # invocations (`FMT_PRAGMA_CLANG(..)` lines, `FMT_ENABLE_IF(..)>`).
            for caps, cand in ((0, raw2), (1, _cpp_post(raw2, 1)), (2, _cpp_post(raw2, 2))):
                if cand == raw or not (errors or missing):
                    continue
                try:
                    tree2 = parser.parse(cand)
                    e2, m2 = _count_tree_issues(tree2.root_node)
                    # bytes under ERROR first: one ERROR swallowing the whole
                    # file must not beat a few local ones
                    if (_error_bytes(tree2.root_node), e2 + m2) < (_error_bytes(root), errors + missing):
                        tree, root, raw, errors, missing = \
                            tree2, tree2.root_node, cand, e2, m2
                        folded, caps_used = True, caps
                except Exception:
                    pass
        if lang == "kotlin" and (errors or missing):
            # Soft keywords used as local names (`val set = HashSet()`,
            # `set += x`) derail the grammar and the error swallows the
            # enclosing class. Retry with those names renamed in place
            # (same length, so offsets and lines are unchanged).
            cand = _KT_SOFT.sub(lambda m: m.group(1) + m.group(2)[:1] + b"_" + m.group(2)[2:], raw)
            # Kotlin 2.1 when-guards (`is LiteralOp<*> if e.x is Y ->`) are
            # unknown to the grammar and turn the whole file into one ERROR:
            # blank the guard (same length; its calls are lost, the file kept)
            cand = _kt_unguard(cand)
            # infix `} get tester.col` / `) set x`: renamed like the locals
            cand = _KT_INFIX.sub(lambda m: m.group(1) + m.group(2)[:1] + b"_" + m.group(2)[2:], cand)
            if cand != raw:
                try:
                    tree2 = parser.parse(cand)
                    e2, m2 = _count_tree_issues(tree2.root_node)
                    # bytes under ERROR, not the count: one ERROR swallowing the
                    # whole file is worse than two local ones
                    if (_error_bytes(tree2.root_node), e2 + m2) < (_error_bytes(root), errors + missing):
                        tree, root, raw, errors, missing = tree2, tree2.root_node, cand, e2, m2
                        folded = True
                except Exception:
                    pass
        if lang == "go":
            mqp = module_qname_go(rel_path)
        elif lang == "rust":
            mqp = module_qname_univ(rel_path, (".rs",))
        elif lang == "java":
            # Package clause overrides below; dir-based fallback strips .java.
            mqp = module_qname_univ(rel_path, (".java",))
        elif lang == "c_sharp":
            mqp = module_qname_univ(rel_path, (".cs",))
        elif lang == "php":
            # Namespace clause overrides below; dir-based fallback strips .php.
            mqp = module_qname_univ(rel_path, (".php",))
        elif lang == "cpp":
            mqp = module_qname_univ(rel_path, (".cpp", ".cc", ".cxx",
                                              ".hpp", ".hxx", ".ipp"))
        elif lang == "c":
            mqp = module_qname_univ(rel_path, (".c",))
        elif lang == "kotlin":
            # Package clause overrides below; dir-based fallback strips .kt.
            mqp = module_qname_univ(rel_path, (".kt", ".kts"))
        elif lang == "lua":
            mqp = module_qname_univ(rel_path, (".lua",))
        elif lang == "swift":
            mqp = module_qname_univ(rel_path, (".swift",))
        elif lang == "dart":
            mqp = module_qname_univ(rel_path, (".dart",))
        elif lang == "gdscript":
            mqp = module_qname_univ(rel_path, (".gd",))
        elif lang == "ruby":
            mqp = module_qname_univ(rel_path, (".rb", ".rake"))
        else:  # pragma: no cover - registry asserts the twelve for now
            raise ValueError(f"unsupported universal language: {lang}")
        fid = file_node_id(repo_id, rel_path)
        fattrs = {"parser": {"backend": "tree-sitter", "language": lang,
                             "status": "ok" if not (errors or missing)
                             else "partial",
                             "errors": errors, "missing": missing}}
        if folded:
            fattrs["parser"]["preproc_folded"] = True
        if errors or missing:
            res.warnings.append(
                f"{rel_path}: recovered from {errors} syntax errors "
                f"({missing} missing nodes); coverage partial")
            res.partial = True
            fattrs["parse"] = "partial"
        res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id,
                              repo_id, rel_path,
                              Location(1, 0, max(1, len(text.splitlines())), 0),
                              lang, content_hash(orig_raw), fattrs, [ev.id],
                              schema_version=2))
        mid = file_node_id(repo_id, rel_path + "#module")
        res.nodes.append(Node(mid, "module", mqp, mqp, project_id, repo_id,
                              rel_path, Location(1, 0, 1, 0), lang, "", {},
                              [ev.id], schema_version=2))
        res.edges.append(Edge(canonical_edge_id(fid, "contains", mid, rel_path),
                              fid, mid, "contains", "structural",
                              "deterministic", 1.0, [ev.id], f"ts-{lang}",
                              UNIV_TS_VERSION, file_path=rel_path))
        if lang == "go":
            _GoVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                       mid, raw).walk_module(root)
        elif lang == "rust":
            rv = _RustVisitor(self, res, ev, repo_id, project_id, rel_path, mqp, mid, raw)
            rv.macro_calls = rust_user_macros
            rv.walk_module(root)
        elif lang == "java":
            _JavaVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                         mid, raw).walk_module(root)
        elif lang == "c_sharp":
            _CsVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                       mid, raw).walk_module(root)
        elif lang == "php":
            _PhpVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                        mid, raw).walk_module(root)
        elif lang in ("cpp", "c"):
            _CppVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                        mid, raw).walk_module(root)
        elif lang == "kotlin":
            _KotlinVisitor(self, res, ev, repo_id, project_id, rel_path,
                           mqp, mid, raw).walk_module(root)
        elif lang == "lua":
            _LuaVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                        mid, raw).walk_module(root)
        elif lang == "swift":
            _SwiftVisitor(self, res, ev, repo_id, project_id, rel_path,
                          mqp, mid, raw).walk_module(root)
        elif lang == "dart":
            _DartVisitor(self, res, ev, repo_id, project_id, rel_path,
                         mqp, mid, raw).walk_module(root)
        elif lang == "ruby":
            _RubyVisitor(self, res, ev, repo_id, project_id, rel_path, mqp,
                         mid, raw).walk_module(root)
        elif lang == "gdscript":
            _GdscriptVisitor(self, res, ev, repo_id, project_id, rel_path,
                             mqp, mid, raw).walk_module(root)
        else:  # pragma: no cover - registry asserts the twelve for now
            raise ValueError(f"unsupported universal language: {lang}")
        _split_overloads(res, repo_id)
        try:
            from .typed_params import annotate_receivers
            annotate_receivers(lang, root, raw, res)
        except Exception as exc:  # annotation is additive; never fail a parse
            res.warnings.append(f"receiver typing skipped: {type(exc).__name__}")
        if lang in ("c", "cpp") and folded:
            try:
                self._alt_branch_calls(res, parser, pre_fold, raw, caps_used, ev,
                                       repo_id, project_id, rel_path, mqp, mid, lang)
            except Exception as exc:  # additive; never fail a parse
                res.warnings.append(f"alternate #else pass skipped: {type(exc).__name__}")
        _site_ids(res)
        return res

    def _alt_branch_calls(self, res, parser, pre_fold, kept, caps, ev, repo_id,
                          project_id, rel_path, mqp, mid, lang) -> None:
        """Calls written only in the `#else` / `#elif` branches the fold
        blanked: the file is parsed again keeping those branches, and the
        call edges on lines the first fold blanked are added when their
        caller exists in the main result (same ids: same owners)."""
        covered = {i + 1 for i, k in enumerate(kept.splitlines()) if k.strip()}
        top = max(_branch_counts(pre_fold) or [1])
        for k in range(1, min(top, 4)):   # #else, then a second #elif / #else
            alt = _cpp_post(_fold_preproc(pre_fold, alt=k), caps)
            lines = {i + 1 for i, a in enumerate(alt.splitlines()) if a.strip()} - covered
            if lines:
                self._alt_pass(res, parser, alt, lines, ev, repo_id, project_id,
                               rel_path, mqp, mid, lang)
                covered |= lines

    def _alt_pass(self, res, parser, alt, lines, ev, repo_id, project_id,
                  rel_path, mqp, mid, lang) -> None:
        tree = parser.parse(alt)
        tmp = ExtractionResult(extractor=res.extractor,
                               extractor_version=res.extractor_version)
        _CppVisitor(self, tmp, ev, repo_id, project_id, rel_path, mqp,
                    mid, alt).walk_module(tree.root_node)
        _split_overloads(tmp, repo_id)
        from .typed_params import annotate_receivers
        annotate_receivers(lang, tree.root_node, alt, tmp)
        owners = {n.id for n in res.nodes}
        have = {(e.id, e.start_line) for e in res.edges}
        # macros defined only in a blanked branch (`#else\n#define P(h) ..`)
        added = {n.id for n in tmp.nodes if n.type == "constant" and n.id not in owners
                 and n.location.start_line in lines}
        for n in tmp.nodes:
            if n.id in added:
                res.nodes.append(n)
                owners.add(n.id)
        for e in tmp.edges:
            if e.relation == "defines" and e.target in added and e.source in owners:
                res.edges.append(e)
                have.add((e.id, e.start_line))
        for e in tmp.edges:
            if (e.relation in ("calls", "calls_unresolved") and e.start_line in lines
                    and e.source in owners and (e.id, e.start_line) not in have):
                e.attributes["preproc_branch"] = "alt"
                res.edges.append(e)
                have.add((e.id, e.start_line))


_SITE_KEYS = ("callee_kind", "base", "member", "arity", "chain", "chain_root_static",
              "chain_root_type", "chain_root_owner", "receiver_type", "receiver_owner",
              "receiver_static", "implicit_member")


def _site_ids(res) -> None:
    """Per-site identity for unresolved calls (Python parity, Phase 24 Faz C2).

    Ids were (source, placeholder) only, so `f("A")` and `f("A", 1)` -- or a
    typed and an untyped `x.add()` -- in one function merged into one edge
    and the last site's shape overwrote the others. The call *shape* (no
    line numbers) now joins the identity: textually identical twins still
    share one logical edge, different shapes stay distinct."""
    import json as _json
    from ...core.identifiers import site_fingerprint
    for e in res.edges:
        if e.relation != "calls_unresolved":
            continue
        a = e.attributes or {}
        shape = _json.dumps({k: a.get(k) for k in _SITE_KEYS if a.get(k) is not None},
                            sort_keys=True)
        e.id = canonical_edge_id(e.source, e.relation, e.target, e.file_path or "",
                                 site_fingerprint(shape))


def _split_overloads(res, repo_id: str) -> int:
    """Give each overload its own identity.

    Visitors derive a symbol id from (kind, qualified name), so `f()` and
    `f(int)` in one class collapsed into one node: calls to either landed on
    whichever definition came first and `f()` calling `f(1)` read as
    recursion. The first definition keeps its id (stable across releases);
    the n-th later one gets the `#overloadN` signature, the same scheme the
    Python extractor uses. Edges are re-attributed by line span: a `defines`
    edge follows its definition line, a body edge the definition enclosing
    its line. Pure function of this file's result.
    """
    groups: dict[str, list] = {}
    for n in res.nodes:
        if n.type in ("method", "function"):
            groups.setdefault(n.id, []).append(n)
    remap: dict[str, list[tuple[int, int, str]]] = {}
    for nid, ns in groups.items():
        locs = {(getattr(n.location, "start_line", 0), getattr(n.location, "end_line", 0)) for n in ns}
        if len(ns) < 2 or len(locs) < 2:
            continue
        ns = sorted(ns, key=lambda n: (n.location.start_line, n.location.end_line))
        spans = []
        seen = set()
        k = 1
        for n in ns:
            key = (n.location.start_line, n.location.end_line)
            if key in seen:
                continue
            seen.add(key)
            if k > 1:
                n.id = canonical_symbol_id(repo_id, n.language or "", n.type,
                                           n.qualified_name, f"#overload{k}")
                n.attributes = dict(n.attributes or {})
                n.attributes["overload"] = k
            spans.append((key[0], key[1], n.id))
            k += 1
        remap[nid] = spans
    if not remap:
        return 0
    # keep one Node object per (id) — later duplicates of the same span drop
    uniq, ids = [], set()
    for n in res.nodes:
        if n.id in ids and n.type in ("method", "function"):
            continue
        ids.add(n.id)
        uniq.append(n)
    res.nodes[:] = uniq

    def pick(old: str, line: int, exact: bool) -> str:
        spans = remap[old]
        if exact:
            for s, e, nid in spans:
                if s == line:
                    return nid
        inside = [(e - s, nid) for s, e, nid in spans if s <= line <= e]
        return min(inside)[1] if inside else old

    for e in res.edges:
        src, tgt = e.source, e.target
        line = e.start_line or 0
        if e.relation in ("defines", "contains") and tgt in remap:
            tgt = pick(tgt, line, True)
        if src in remap:
            src = pick(src, line, False)
        if (src, tgt) != (e.source, e.target):
            e.source, e.target = src, tgt
            e.id = canonical_edge_id(src, e.relation, tgt, e.file_path or "")
    return len(remap)


def _unavailable_file(res, ev, repo_id, rel_path, project_id, lang, raw,
                      reason: str):
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id,
                          repo_id, rel_path, Location(1, 0, 1, 0), lang,
                          content_hash(raw),
                          {"parser": {"backend": "none", "status": "unavailable",
                                      "reason": reason[:200]}},
                          [ev.id], schema_version=2))
    return res


_EXPORT_MACRO = re.compile(
    rb"(\b(?:class|struct)\s+)([A-Z][A-Z0-9_]{2,})(?=\s+[A-Za-z_]\w*\s*(?:final\b\s*)?[:{])")
_PP_COND = re.compile(rb"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)")


# `set`/`get` as a local name: declared (`val set =`) or used as a
# statement head / receiver (`set += x`, `set.add(x)`, `set[0]`).
# Rust std macros whose arguments are ordinary expressions: `debug_assert!(..)`
# -> `debug_assert_(..)` (same length) so the calls inside parse as calls.
RUST_EXPR_MACROS = ("assert", "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq",
                    "debug_assert_ne", "println", "print", "eprintln", "eprint", "format",
                    "format_args", "write", "writeln", "panic", "vec", "dbg", "todo",
                    "unreachable", "unimplemented", "matches")
_RUST_MACRO = re.compile(rb"\b(" + b"|".join(m.encode() for m in RUST_EXPR_MACROS) + rb")!(?=\s*[(\[])")
RUST_MACRO_CALLS = frozenset(m + "_" for m in RUST_EXPR_MACROS)
_RUST_ANY_MACRO = re.compile(rb"(?<![\w!])([a-z_][a-z0-9_]*)!(?=\s*[(\[])")


_KT_GUARD = re.compile(rb"(^[ \t]*[^\n]*?[^\s])([ \t]+if[ \t]+(?!\()[^\n]*?)(?=[ \t]*->)"
                       rb"|(^[ \t]*!?(?:is|in)\b[^\n]*?)([ \t]+if[ \t]*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))(?=\s*->)",
                       re.M)


_KT_INFIX = re.compile(rb"([)\]}][ \t]+)(get|set)(?=[ \t]+[\w(])")


def _kt_unguard(raw: bytes) -> bytes:
    """Blank Kotlin when-guards in place (newlines kept)."""
    def rep(m):
        keep, guard = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
        return keep + bytes(c if c in (10, 13) else 32 for c in guard)
    return _KT_GUARD.sub(rep, raw)
_KT_SOFT = re.compile(rb"(\b(?:val|var)\s+|^[ \t]*|[(,=]\s*)\b(set|get|where)\b(?=\s*(?:[+\-*/]?=(?!=)|\.|\?\.|!!|\[|\)|,|:(?!:)|$))",
                      re.M)


def _branch_counts(raw: bytes) -> list[int]:
    """Branch count of every `#if` block, in opening order."""
    counts: list[int] = []
    stack: list[int] = []
    cont = False
    for line in raw.splitlines(keepends=True):
        if cont:
            cont = line.rstrip(b"\r\n").endswith(b"\\")
            continue
        m = _PP_COND.match(line)
        if m:
            word = m.group(1)
            if word in (b"if", b"ifdef", b"ifndef"):
                stack.append(len(counts))
                counts.append(1)
            elif stack and word == b"endif":
                stack.pop()
            elif stack:
                counts[stack[-1]] += 1
            cont = line.rstrip(b"\r\n").endswith(b"\\")
    return counts


def _fold_preproc(raw: bytes, alt: int = 0) -> bytes:
    """Blank `#if`-family directives and every branch but the first live
    one (`#if 0` counts as dead). Each blanked byte becomes a space and
    newlines stay, so offsets and line numbers match the original.
    `alt=k` keeps branch k of every multi-branch block instead (its last
    one when it has fewer), so calls written only there are seen too."""
    def blank(line: bytes) -> bytes:
        return bytes(b if b in (10, 13) else 32 for b in line)
    if alt:
        return _fold_alt(raw, _branch_counts(raw), blank, alt)

    out: list[bytes] = []
    frames: list[list[bool]] = []  # [live, taken]
    cont = False  # inside a backslash-continued directive
    for line in raw.splitlines(keepends=True):
        if cont:
            out.append(blank(line))
            cont = line.rstrip(b"\r\n").endswith(b"\\")
            continue
        m = _PP_COND.match(line)
        if m:
            word, rest = m.group(1), m.group(2).strip()
            if word in (b"if", b"ifdef", b"ifndef"):
                dead = word == b"if" and rest.split(b"//")[0].strip() in (b"0", b"(0)")
                frames.append([not dead, not dead])
            elif frames and word == b"endif":
                frames.pop()
            elif frames and word == b"else":
                frames[-1] = [not frames[-1][1], True]
            elif frames:  # elif family
                frames[-1] = [not frames[-1][1], True]
            out.append(blank(line))
            cont = line.rstrip(b"\r\n").endswith(b"\\")
            continue
        out.append(blank(line) if any(not f[0] for f in frames) else line)
    return b"".join(out)


def _fold_alt(raw: bytes, counts: list[int], blank, k: int = 1) -> bytes:
    out: list[bytes] = []
    frames: list[list[int]] = []  # [keep branch index, current index]
    opened = 0
    cont = False
    for line in raw.splitlines(keepends=True):
        if cont:
            out.append(blank(line))
            cont = line.rstrip(b"\r\n").endswith(b"\\")
            continue
        m = _PP_COND.match(line)
        if m:
            word = m.group(1)
            if word in (b"if", b"ifdef", b"ifndef"):
                n = counts[opened] if opened < len(counts) else 1
                opened += 1
                frames.append([min(k, n - 1), 0])
            elif frames and word == b"endif":
                frames.pop()
            elif frames:
                frames[-1][1] += 1
            out.append(blank(line))
            cont = line.rstrip(b"\r\n").endswith(b"\\")
            continue
        out.append(blank(line) if any(f[0] != f[1] for f in frames) else line)
    return b"".join(out)


# A standalone ALL-CAPS token followed by a word / `~` / `::` / `{` / line end
# (never `(`, `;`, `,`, operators or `:`): declaration-specifier macros.
_CAPS_MACRO = re.compile(
    rb"(?<![\w.>:#])\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b(?=[ \t]*(?:[A-Za-z_~{]|::|\r?\n))")
_KEEP_CAPS = frozenset({b"NULL", b"EOF", b"TRUE", b"FALSE"})


def _blank_caps_macros(raw: bytes) -> bytes:
    """Same-length blanking of attribute-like macros (`FMT_CONSTEXPR`,
    `FMT_BEGIN_NAMESPACE`, `LEVELDB_EXPORT`); requires an underscore so
    plain ALL-CAPS type names (`DWORD x`) are left alone. Directive lines
    are kept (`#define FMT_ARGS_H_` blanked to `#define` is an error)."""
    sub = lambda m: m.group(0) if m.group(1) in _KEEP_CAPS else b" " * len(m.group(1))  # noqa: E731
    return b"".join(line if line.lstrip().startswith(b"#") else _CAPS_MACRO.sub(sub, line)
                    for line in raw.splitlines(keepends=True))


_CAPS_CALL = re.compile(rb"(?<![\w.>:])\b[A-Z][A-Z0-9]*_[A-Z0-9_]+[ \t]*\(")


def _blank_macro_calls(raw: bytes) -> bytes:
    """Same-length blanking of ALL-CAPS function-like macro invocations
    the grammar cannot place: a whole line that is only the invocation
    (`FMT_PRAGMA_CLANG(diagnostic push)`, no `;`), or one inside a
    `template <..>` parameter list (`FMT_ENABLE_IF(!std::is_same<..>)`)."""
    buf = bytearray(raw)
    for m in _CAPS_CALL.finditer(raw):
        i = m.end() - 1
        depth, j = 0, i
        while j < len(raw):
            c = raw[j]
            if c == 40:
                depth += 1
            elif c == 41:
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(raw):
            continue
        ls = raw.rfind(b"\n", 0, m.start()) + 1
        le = raw.find(b"\n", j)
        le = len(raw) if le < 0 else le
        before = raw[ls:m.start()].strip()
        after = raw[j + 1:le].strip()
        if before.startswith(b"#"):
            continue
        prefix = raw[max(0, m.start() - 400):m.start()]
        prev = raw[max(0, m.start() - 200):m.start()].rstrip()[-1:]
        in_tpl = (prev in (b",", b"<") and after[:1] in (b">", b",")
                  and b"template" in prefix[prefix.rfind(b";") + 1:])
        alone = not before and (not after or after.startswith(b"//"))
        # `class FMT_SO_VISIBILITY("default") format_error : ..`
        attr = re.search(rb"\b(class|struct|union)$", before) is not None
        if in_tpl or alone or attr:
            for k in range(m.start(), j + 1):
                if buf[k] not in (10, 13):
                    buf[k] = 32
            if in_tpl and prev == b",":
                k = m.start() - 1           # the separating comma too
                while k > 0 and buf[k] in (32, 9, 10, 13):
                    k -= 1
                if buf[k] == 44:
                    buf[k] = 32
    return bytes(buf)


_NS_MACRO = re.compile(
    rb"^([ \t]*)([A-Z][A-Z0-9]*)_(?:NAMESPACE_(BEGIN|END)|(BEGIN|END)_NAMESPACE)[ \t]*(?=\r?\n|$)", re.M)


def _expand_ns_macros(raw: bytes, rel_path: str) -> bytes:
    """`SPDLOG_NAMESPACE_BEGIN` / `FMT_BEGIN_NAMESPACE` on a line of their
    own open the project namespace named by the prefix: rewritten in place
    (same length) to `namespace spdlog {` / `}` when that name is a
    directory of the file's path, so the symbols keep their namespace."""
    parts = {p.lower() for p in re.split(r"[\\/]", rel_path)[:-1]}

    def sub(m):
        ns = m.group(2).decode("ascii").lower()
        if ns not in parts:
            return m.group(0)
        word = m.group(3) or m.group(4)
        new = (b"namespace " + ns.encode() + b" {") if word == b"BEGIN" else b"}"
        span = len(m.group(0)) - len(m.group(1))
        if len(new) > span:
            return m.group(0)
        return m.group(1) + new + b" " * (span - len(new))
    return _NS_MACRO.sub(sub, raw)


def _cpp_post(raw: bytes, level: int) -> bytes:
    """Macro blanking applied after the `#if` fold, by retry level."""
    if level >= 2:
        raw = _blank_macro_calls(raw)
    if level >= 1:
        raw = _blank_caps_macros(raw)
    return raw


def _blank_export_macros(raw: bytes) -> bytes:
    """`class LEVELDB_EXPORT DB {`: an ALL-CAPS export/visibility macro
    between the keyword and the name parses (cleanly) as the class name."""
    return _EXPORT_MACRO.sub(lambda m: m.group(1) + b" " * len(m.group(2)), raw)


def _error_bytes(root) -> int:
    """Bytes covered by ERROR nodes (outermost only)."""
    total, stack = 0, [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR":
            total += n.end_byte - n.start_byte
            continue
        stack.extend(n.children)
    return total


def _count_tree_issues(root) -> tuple[int, int]:
    errors = 0
    missing = 0
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR":
            errors += 1
        elif n.is_missing:
            missing += 1
        stack.extend(n.children)
    return errors, missing


def _go_default_import_name(path: str) -> str:
    """The name an unaliased Go import binds: the package name, which by
    convention is the last path element without a major-version suffix
    (`github.com/go-chi/chi/v5` -> `chi`, `gopkg.in/yaml.v3` -> `yaml`,
    `github.com/mattn/go-isatty` -> `isatty`)."""
    parts = [p for p in path.rstrip("/").split("/") if p]
    if not parts:
        return ""
    last = parts[-1]
    if re.fullmatch(r"v[0-9]+", last) and len(parts) > 1:
        last = parts[-2]
    last = re.sub(r"\.v[0-9]+$", "", last)
    if last.startswith("go-") and len(last) > 3:
        last = last[3:]
    return re.sub(r"[.-]go$", "", last).replace("-", "_").replace(".", "_")


class _GoVisitor:
    """Two-pass Go walker: pass 1 declares module-scope symbols (functions,
    methods, named types), pass 2 walks bodies for imports already
    recorded plus calls. Same-file locals resolve to `calls`; everything
    else stays `calls_unresolved` with shape attrs for go_resolve."""

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{ext.language_id}"
        # module-scope tables for same-file resolution
        self.funcs: dict[str, str] = {}   # name -> node id
        self.types: dict[str, str] = {}   # type name -> node id
        self.pkg_alias: dict[str, str] = {}  # import local name -> specifier

    def _sym(self, kind: str, qname: str, node, sig: str = "") -> str:
        nid = canonical_symbol_id(self.repo_id, "go", kind, qname, sig)
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1], qname,
                                   self.project_id, self.repo_id, self.rel,
                                   _loc(node), "go", "", {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score,
                                   [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        for stmt in root.children:
            t = stmt.type
            if t == "package_clause":
                bm = re.search(rb"^//go:build ([^\r\n]+)", self.raw[:stmt.start_byte], re.M)
                for c in stmt.children:
                    if c.type == "package_identifier":
                        for n in self.res.nodes:
                            if n.id == self.mid:
                                n.attributes = dict(n.attributes or {})
                                n.attributes["go_package"] = \
                                    _text(c, self.raw, 120)
                                if bm:
                                    # `//go:build !nomsgpack`: build-tag variants of one package
                                    n.attributes["go_build"] = bm.group(1).decode("utf-8", "ignore").strip()
            elif t == "import_declaration":
                self._imports(stmt)
            elif t == "function_declaration":
                self._function(stmt)
            elif t == "method_declaration":
                self._method(stmt)
            elif t == "type_declaration":
                self._types(stmt)
            elif t == "var_declaration":
                self._package_vars(stmt)
        # pass 2: bodies (imports recorded above feed nothing here; calls
        # resolve against the pass-1 tables of THIS file only)
        for stmt in root.children:
            if stmt.type in ("function_declaration", "method_declaration"):
                self._body(stmt)
            elif stmt.type in ("var_declaration", "const_declaration"):
                # package-level `var ErrX = echo.NewHTTPError(..)`: the
                # initializer runs at package init, owned by the module
                self._calls_in(stmt, self.mid)

    def _import_specs(self, node):
        for c in node.children:
            if c.type == "import_spec":
                yield c
            elif c.type == "import_spec_list":
                for g in c.children:
                    if g.type == "import_spec":
                        yield g

    def _imports(self, node):
        for spec in self._import_specs(node):
            name = ""
            blank = False
            dot = False
            lit = ""
            for c in spec.children:
                if c.type == "interpreted_string_literal":
                    lit = _strip_quotes(_text(c, self.raw, 300))
                elif c.type == "blank_identifier":
                    blank = True
                elif c.type == "package_identifier":
                    name = _text(c, self.raw, 120)
                elif c.type == ".":
                    dot = True
            if not lit:
                continue
            if not name and not blank and not dot:
                name = _go_default_import_name(lit)
            if blank:
                attrs: dict = {"specifier": lit, "side_effect_only": True,
                               "dynamic": False}
            elif dot:
                attrs = {"specifier": lit, "dot_import": True,
                         "dynamic": False}
            else:
                attrs = {"specifier": lit, "names": [name],
                         "dynamic": False}
                self.pkg_alias[name] = lit
            self._edge(self.mid, "imports", self._ext(lit), spec,
                       "structural", "deterministic", 1.0, attrs)

    def _function(self, node):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        nid = self._sym("function", f"{self.mod}.{name}", node)
        self.funcs[name] = nid
        self._defines(self.mid, nid, node)

    def _receiver(self, node) -> tuple[str, str]:
        """(receiver var, receiver type name) or ("", "")."""
        for c in node.children:
            if c.type != "parameter_list":
                continue
            for p in c.children:
                if p.type != "parameter_declaration":
                    continue
                var = ""
                typ = ""
                for g in p.children:
                    if g.type == "identifier" and not var:
                        var = _text(g, self.raw, 80)
                    elif g.type in ("type_identifier", "generic_type"):
                        typ = _text(g, self.raw, 80).split("[")[0]
                    elif g.type == "pointer_type":
                        for h in g.children:
                            if h.type == "type_identifier":
                                typ = _text(h, self.raw, 80)
                if typ:
                    return var, typ
        return "", ""

    def _method(self, node):
        name = ""
        for c in node.children:
            if c.type == "field_identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        _, typ = self._receiver(node)
        if not typ:
            # Unparseable receiver: keep the symbol as a plain function
            # rather than inventing an owner (documented).
            nid = self._sym("function", f"{self.mod}.{name}", node)
            self.funcs[name] = nid
            self._defines(self.mid, nid, node)
            return
        nid = self._sym("method", f"{self.mod}.{typ}.{name}", node)
        owner = self.types.get(typ)
        self._defines(owner or self.mid, nid, node)

    def _types(self, node):
        for c in node.children:
            if c.type == "type_spec":
                tname = ""
                kind = "type"
                for g in c.children:
                    if g.type == "type_identifier" and not tname:
                        tname = _text(g, self.raw, 120)
                    elif g.type == "struct_type":
                        kind = "class"
                    elif g.type == "interface_type":
                        kind = "interface"
                if tname:
                    nid = self._sym(kind, f"{self.mod}.{tname}", c)
                    if kind in ("class", "interface"):
                        self.types[tname] = nid
                    self._defines(self.mid, nid, c)
                    if kind == "interface":
                        self._interface_methods(c, tname, nid)
                    elif kind == "class":
                        self._embedded(c, nid)
            elif c.type == "type_alias":
                tname = ""
                for g in c.children:
                    if g.type == "type_identifier" and not tname:
                        tname = _text(g, self.raw, 120)
                if tname:
                    nid = self._sym("type", f"{self.mod}.{tname}", c)
                    self._defines(self.mid, nid, c)

    def _package_vars(self, decl):
        """`var JSON BindingBody = jsonBinding{}` / `var XML = &xmlBinding{}`:
        package-level variables, typed by their declaration or composite
        literal, usable from every file of the package (`JSON.Bind(..)`)."""
        specs = [c for c in decl.children if c.type == "var_spec"]
        for lst in (c for c in decl.children if c.type == "var_spec_list"):
            specs += [g for g in lst.children if g.type == "var_spec"]
        for vs in specs:
            names = [_text(g, self.raw, 120) for g in vs.children if g.type == "identifier"]
            tnode = next((g for g in vs.children if g.type in ("type_identifier", "pointer_type",
                                                               "qualified_type", "generic_type")), None)
            vt = _text(tnode, self.raw, 120) if tnode is not None else ""
            if not vt:
                el = next((g for g in vs.children if g.type == "expression_list"), None)
                v = next((g for g in el.children if g.is_named), None) if el is not None else None
                if v is not None and v.type == "unary_expression":
                    v = next((g for g in v.children if g.type == "composite_literal"), None)
                if v is not None and v.type == "composite_literal":
                    tn = next((g for g in v.children if g.type in ("type_identifier", "qualified_type",
                                                                   "generic_type")), None)
                    vt = _text(tn, self.raw, 120) if tn is not None else ""
            vt = vt.lstrip("*").split("[", 1)[0].rsplit(".", 1)[-1]
            if len(names) != 1 or not vt:
                continue
            nid = self._sym("variable", f"{self.mod}.{names[0]}", vs)
            for n in self.res.nodes:
                if n.id == nid:
                    n.attributes = dict(n.attributes or {})
                    n.attributes["var_type"] = vt
                    break
            self._defines(self.mid, nid, vs)

    def _embedded(self, spec, struct_id: str):
        """`type Engine struct { RouterGroup; *Pool }`: embedded fields promote
        their methods (`engine.Group()` is RouterGroup.Group) -> inherits."""
        st = next((g for g in spec.children if g.type == "struct_type"), None)
        fl = next((g for g in st.children if g.type == "field_declaration_list"), None) if st is not None else None
        for fd in (fl.children if fl is not None else []):
            if fd.type != "field_declaration" or any(g.type == "field_identifier" for g in fd.children):
                continue
            t_ = next((g for g in fd.children if g.type in ("type_identifier", "qualified_type", "generic_type")), None)
            if t_ is None:
                continue
            nm = _text(t_, self.raw, 120).split("[", 1)[0].rsplit(".", 1)[-1]
            if nm:
                self._edge(struct_id, "inherits", self._ext(nm), fd, "structural", "deterministic", 1.0,
                           {"superclass": nm, "embedded": True})

    def _interface_methods(self, spec, tname: str, iface_id: str):
        """`type ResponseWriter interface { Status() int; ... }`: each method
        spec is a method node of the interface (calls through an interface
        value bind to it; dispatch links it to every implementing type).
        Embedded interfaces are kept as `embeds` for the method set."""
        it = next((g for g in spec.children if g.type == "interface_type"), None)
        if it is None:
            return
        embeds = []
        for el in it.children:
            if el.type in ("method_elem", "method_spec"):
                mname = next((_text(g, self.raw, 120) for g in el.children
                              if g.type == "field_identifier"), "")
                if mname:
                    mid_ = self._sym("method", f"{self.mod}.{tname}.{mname}", el)
                    self._defines(iface_id, mid_, el)
            elif el.type in ("type_elem", "constraint_elem"):
                t_ = next((g for g in el.children if g.type in ("type_identifier", "qualified_type")), None)
                if t_ is not None:
                    nm_ = _text(t_, self.raw, 120).rsplit(".", 1)[-1]
                    embeds.append(nm_)
                    # an embedded interface's methods are part of this one
                    self._edge(iface_id, "inherits", self._ext(nm_), el, "structural", "deterministic", 1.0,
                               {"superclass": nm_, "embedded": True})
        if embeds:
            for n in self.res.nodes:
                if n.id == iface_id:
                    n.attributes = dict(n.attributes or {})
                    n.attributes["embeds"] = embeds

    # ------------------------------------------------------------ pass 2
    def _body(self, node):
        t = node.type
        if t == "function_declaration":
            name = ""
            for c in node.children:
                if c.type == "identifier":
                    name = _text(c, self.raw, 120)
                    break
            owner = self.funcs.get(name)
            recv = ("", "")
        elif t == "method_declaration":
            name = ""
            for c in node.children:
                if c.type == "field_identifier":
                    name = _text(c, self.raw, 120)
                    break
            rvar, typ = self._receiver(node)
            recv = (rvar, typ)
            owner = None
            for nid in self.res.nodes:
                if nid.qualified_name == f"{self.mod}.{typ}.{name}":
                    owner = nid.id
                    break
        else:
            return
        if owner is None:
            return
        for c in node.children:
            self._calls_in(c, owner, recv)

    def _calls_in(self, node, owner: str, recv=("", "")):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                self._call(n, owner, recv)
            stack.extend(n.children)

    def _call(self, node, owner: str, recv=("", "")):
        fn = None
        for c in node.children:
            if c.type in ("identifier", "selector_expression", "index_expression"):
                fn = c
                break
        if fn is not None and fn.type == "index_expression":
            # `f[int](x)` / `pkg.F[T](x)`: a generic instantiation, the callee is the operand
            fn = next((g for g in fn.children if g.type in ("identifier", "selector_expression")), None)
        if fn is None:
            return
        arity = 0
        for c in node.children:
            if c.type == "argument_list":
                arity = sum(1 for g in c.children
                            if g.type not in ("(", ")", ","))
        if fn.type == "identifier":
            base = _text(fn, self.raw, 120)
            if base in ("func",):
                return
            tgt = self.funcs.get(base)
            if tgt is not None:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9, {"arity": arity})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            return
        # selector_expression: operand.field
        operand = ""
        member = ""
        via_field = None
        for c in fn.children:
            if c.type == "identifier" and not operand:
                operand = _text(c, self.raw, 120)
            elif c.type == "selector_expression" and not operand and via_field is None:
                # `cmd.parent.handleExitCoder(..)`: receiver is a field of `cmd`
                ids = [g for g in c.children if g.type in ("identifier", "field_identifier")]
                if len(ids) == 2 and ids[0].type == "identifier":
                    via_field = (_text(ids[0], self.raw, 120), _text(ids[1], self.raw, 120))
            elif c.type == "field_identifier" and not member:
                member = _text(c, self.raw, 120)
        if not member:
            return
        attrs = {"callee_kind": "selector", "base": operand,
                 "member": member, "arity": arity,
                 "import_binding": bool(operand in self.pkg_alias)}
        if via_field and via_field[0] == recv[0] and recv[1]:
            # the field's declared type is read from the receiver's struct
            attrs["base"] = f"{via_field[0]}.{via_field[1]}"
            attrs["receiver_field"] = via_field[1]
            attrs["field_owner"] = f"{self.mod}.{recv[1]}"
        opnode = fn.child_by_field_name("operand")
        if not operand and opnode is not None:
            inner = opnode
            if inner.type == "parenthesized_expression":
                inner = next((g for g in inner.children if g.is_named), inner)
            if inner.type == "unary_expression":
                inner = next((g for g in inner.children if g.type == "composite_literal"), inner)
            if inner.type == "composite_literal":
                # `(render.PureJSON{Data: obj}).Render(w)`: the literal's type
                tn = next((g for g in inner.children if g.type in ("type_identifier", "qualified_type",
                                                                   "generic_type")), None)
                if tn is not None:
                    attrs["receiver_type"] = _text(tn, self.raw, 120).split("[", 1)[0].rsplit(".", 1)[-1]
            elif inner.type == "selector_expression" and "receiver_field" not in attrs:
                dotted = _text(inner, self.raw, 200)
                if re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", dotted) and                         dotted.split(".", 1)[0] not in self.pkg_alias:
                    # `c.Writer.Status()`: typed from `c`'s fields later
                    attrs["base"] = dotted
        if operand and operand == recv[0] and recv[1] and                 operand not in self.pkg_alias:
            # `f.X()` inside `func (f *FlagSet) ...`: the receiver's declared
            # type is syntax, not inference; go_resolve binds it package-wide.
            attrs["receiver_type"] = recv[1]
        # the site is where the method's name is (`b.Int64(..).` then
        # `BindError()` on the next line)
        name_node = next((c for c in fn.children if c.type == "field_identifier"), node)
        self._edge(owner, "calls_unresolved", self._ext(operand + "." + member),
                   name_node, "structural", "deterministic", 1.0, attrs)


class _RustVisitor:
    """Two-pass Rust walker. `use` paths (crate/super/self/extern, globs,
    renames) and same-file locals recorded in pass 1; calls shaped in
    pass 2 (`fn`, `Type::assoc`, receiver `x.m`). `impl Trait for Type`
    mints no heritage (traits are not supertypes). Macros, const/static
    values, and struct-literal construction stay out (documented)."""

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{ext.language_id}"
        self.funcs: dict[str, str] = {}
        self.types: dict[str, str] = {}
        self.method_ids: dict[str, str] = {}  # qualified name -> node id
        self.base_mod = mqp
        self.const_bodies: list = []
        self.fn_by_node: dict[int, str] = {}  # function_item start byte -> node id

    def _sym(self, kind: str, qname: str, node, sig: str = "") -> str:
        nid = canonical_symbol_id(self.repo_id, "rust", kind, qname, sig)
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1], qname,
                                   self.project_id, self.repo_id, self.rel,
                                   _loc(node), "rust", "", {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score,
                                   [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        for stmt in root.children:
            t = stmt.type
            if t == "mod_item":
                self._walk_inner(stmt)
            elif t == "use_declaration":
                self._uses(stmt)
            elif t == "extern_crate_declaration":
                # `pub extern crate grep_cli as cli;`: the crate under a local name
                ids = [_text(c, self.raw, 120) for c in stmt.children if c.type == "identifier"]
                if ids:
                    self._emit_use(ids[0], [ids[-1]], stmt)
            elif t == "struct_item":
                self._named_type(stmt, "class")
            elif t == "enum_item":
                self._named_type(stmt, "type")
            elif t == "trait_item":
                self._trait(stmt)
            elif t == "impl_item":
                self._impl(stmt)
            elif t == "function_item":
                self._function(stmt)
            elif t in ("const_item", "static_item"):
                self._const(stmt)
        for val, cid in self.const_bodies:
            self._calls_in(val, cid)
        for stmt in root.children:
            self._bodies(stmt)
            if stmt.type in ("expression_statement", "macro_invocation", "call_expression"):
                # item-position macros (`rgtest!(name, |dir, cmd| { .. })`,
                # rewritten to a call): the function they generate is unknown,
                # their calls belong to the module
                self._calls_in(stmt, self.mid)
        # `fn fmt(..) { use crate::automaton::fmt_state_indicator; .. }`: a
        # block-scoped import binds names used in that block (kept per file)
        top = {id(c) for c in root.children}
        stack = [c for c in root.children if c.type != "use_declaration"]
        while stack:
            n = stack.pop()
            if n.type == "use_declaration" and id(n) not in top:
                self._uses(n)
                continue
            if n.type == "mod_item":
                continue                         # inline modules: walked by _walk_inner
            stack.extend(n.children)

    def _const(self, node):
        """`pub(super) const FLAGS: &[&dyn Flag] = ..`: a typed module value
        (its element type types `for flag in FLAGS.iter()`)."""
        from .typed_params import _rust_elem_type, _simple_type as _st
        name = next((_text(c, self.raw, 120) for c in node.children if c.type == "identifier"), "")
        tnode = node.child_by_field_name("type")
        if not name or tnode is None:
            return
        attrs = {}
        vt = _st(tnode, self.raw)
        et = _rust_elem_type(tnode, self.raw)
        if vt:
            attrs["var_type"] = vt
        if et:
            attrs["elem_type"] = et
        if not attrs:
            return
        nid = self._sym("variable", f"{self.mod}.{name}", node)
        for n in self.res.nodes:
            if n.id == nid:
                n.attributes = dict(n.attributes or {})
                n.attributes.update(attrs)
                break
        self._defines(self.mid, nid, node)
        val = node.child_by_field_name("value")
        if val is not None:
            self.const_bodies.append((val, nid))   # `&[alias("cursor", ..), ..]`: walked in pass 2

    def _mod_name(self, node) -> str:
        return next((_text(c, self.raw, 120) for c in node.children if c.type == "identifier"), "")

    def _walk_inner(self, node):
        # Inline `mod foo { ... }`: its items are named `<module>.foo.<item>`
        # (`convert::str(..)` in defs.rs is `defs.convert.str`, not `defs.str`).
        # The items sit under the mod's declaration_list.
        saved = self.mod
        nm = self._mod_name(node)
        if nm:
            self.mod = f"{saved}.{nm}"
        try:
            self._walk_inner_items(node)
        finally:
            self.mod = saved

    def _walk_inner_items(self, node):
        kids = [g for c in node.children if c.type == "declaration_list"
                for g in c.children] or node.children
        for c in kids:
            if c.type in ("const_item", "static_item"):
                self._const(c)
            if c.type in ("mod_item", "use_declaration", "struct_item",
                          "enum_item", "trait_item", "impl_item",
                          "function_item"):
                if c.type == "mod_item":
                    self._walk_inner(c)
                elif c.type == "use_declaration":
                    self._uses(c)
                elif c.type in ("struct_item", "enum_item"):
                    self._named_type(
                        c, "class" if c.type == "struct_item" else "type")
                elif c.type == "trait_item":
                    self._trait(c)
                elif c.type == "impl_item":
                    self._impl(c)
                elif c.type == "function_item":
                    self._function(c)

    def _type_name(self, node) -> str:
        for c in node.children:
            if c.type == "type_identifier":
                return _text(c, self.raw, 120)
        return ""

    def _named_type(self, node, kind: str):
        name = self._type_name(node)
        if not name:
            return
        nid = self._sym(kind, f"{self.mod}.{name}", node)
        if kind == "class":
            self.types[name] = nid
        self._defines(self.mid, nid, node)

    def _trait(self, node):
        name = self._type_name(node)
        if not name:
            return
        nid = self._sym("interface", f"{self.mod}.{name}", node)
        self._defines(self.mid, nid, node)
        for c in node.children:
            if c.type != "declaration_list":
                continue
            for m in c.children:
                if m.type == "function_item":  # default method with a body
                    fn = next((_text(g, self.raw, 120) for g in m.children
                               if g.type == "identifier"), "")
                    qn = f"{self.mod}.{name}.{fn}"
                    if fn and qn not in self.method_ids:
                        self.method_ids[qn] = self._sym("method", qn, m)
                        self._defines(nid, self.method_ids[qn], m)
                    continue
                if m.type == "function_signature_item":
                    fn = ""
                    for g in m.children:
                        if g.type == "identifier":
                            fn = _text(g, self.raw, 120)
                            break
                    if fn:
                        mid2 = self._sym(
                            "method", f"{self.mod}.{name}.{fn}", m)
                        self._defines(nid, mid2, m)

    def _impl_target(self, node) -> str:
        """Self type of an impl: `impl X`, `impl<T> X<T>`, `impl T for a::X<U>`
        (the grammar's `type` field; generics and paths reduce to the name)."""
        t = node.child_by_field_name("type")
        while t is not None and t.type in ("generic_type", "scoped_type_identifier",
                                           "reference_type"):
            nxt = t.child_by_field_name("type") or t.child_by_field_name("name")
            if nxt is None:
                nxt = next((c for c in t.children if c.type in (
                    "type_identifier", "generic_type", "scoped_type_identifier")), None)
            t = nxt
        if t is not None and t.type == "type_identifier":
            return _text(t, self.raw, 120)
        # no nominal type: `impl Buf for &[u8]`, `impl X for (A, B)`,
        # `impl fmt::Display for dyn Error`, `impl T for [u8; 4]`: name the
        # impl by the type as written (minus references) so its methods exist
        raw = _text(node.child_by_field_name("type"), self.raw, 120) \
            if node.child_by_field_name("type") is not None else ""
        raw = re.sub(r"^(&\s*('\w+\s+)?(mut\s+)?)+", "", raw.strip())
        m = re.match(r"dyn\s+(?:[\w:]+::)?(\w+)", raw)
        if m:
            return m.group(1)
        return re.sub(r"\s+", "", raw).replace(".", "_")

    def _impl(self, node):
        target = self._impl_target(node)
        # `impl Trait for Type`: no heritage edge (documented); methods
        # still attach to the target type when known.
        for c in node.children:
            if c.type != "declaration_list":
                continue
            for m in c.children:
                if m.type != "function_item":
                    continue
                fn = ""
                for g in m.children:
                    if g.type == "identifier":
                        fn = _text(g, self.raw, 120)
                        break
                if not fn or not target:
                    continue
                qn = f"{self.mod}.{target}.{fn}"
                # Same name in another impl block of one type (several
                # `impl From<X> for T`): a second node with the same id, which
                # _split_overloads turns into `#overloadN` with its own body edges.
                nid = self._sym("method", qn, m)
                self.method_ids.setdefault(qn, nid)
                owner = self.types.get(target)
                self._defines(owner or self.mid, nid, m)

    def _function(self, node):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        nid = self._sym("function", f"{self.mod}.{name}", node)
        self.fn_by_node[node.start_byte] = nid
        if self.mod == self.base_mod:
            self.funcs[name] = nid          # same-file bare calls: module level first
        else:
            self.funcs.setdefault(name, nid)
        self._defines(self.mid, nid, node)

    def _use_path_segments(self, node) -> list[str]:
        """Flatten a use path to segments, keeping leading keywords."""
        if node.type == "identifier":
            return [_text(node, self.raw, 120)]
        if node.type in ("crate", "super", "self"):
            return [node.type]
        if node.type == "scoped_identifier":
            out: list[str] = []
            for c in node.children:
                if c.type in ("::",):
                    continue
                out.extend(self._use_path_segments(c))
            return out
        return []

    def _uses(self, node):
        """Every import of a `use` declaration, at any nesting depth:
        `use crate::{automaton::Automaton, util::{a::B, c::{D, E as F}}, self};`"""
        for c in node.children:
            self._use_tree(c, [])

    def _use_tree(self, n, prefix: list):
        t = n.type
        if t in ("identifier", "crate", "super", "self") and not prefix and t != "identifier":
            return                                   # bare keyword: nothing imported
        if t == "identifier":
            nm = _text(n, self.raw, 120)
            self._emit_use("::".join(prefix + [nm]), [nm], n)
        elif t == "self":
            if prefix:                               # `use foo::{self}` imports foo
                self._emit_use("::".join(prefix), [prefix[-1]], n)
        elif t == "scoped_identifier":
            segs = self._use_path_segments(n)
            if segs:
                self._emit_use("::".join(prefix + segs), [segs[-1]], n)
        elif t == "use_as_clause":
            segs: list[str] = []
            al = ""
            seen_as = False
            for g in n.children:
                if g.type == "as":
                    seen_as = True
                elif g.type == "identifier" and seen_as:
                    al = _text(g, self.raw, 120)
                elif g.type in ("identifier", "scoped_identifier", "crate", "super", "self"):
                    segs.extend(self._use_path_segments(g))
            if segs and al and al != "_":
                self._emit_use("::".join(prefix + segs), [al], n)
        elif t == "scoped_use_list":
            scope: list[str] = []
            for g in n.children:
                if g.type == "use_list":
                    self._use_tree(g, prefix + scope)
                elif g.type != "::":
                    scope.extend(self._use_path_segments(g))
        elif t == "use_list":
            for g in n.children:
                if g.type == "*":
                    self._emit_use("::".join(prefix + ["*"]), [], g, glob=True)
                elif g.type not in ("{", "}", ","):
                    self._use_tree(g, prefix)
        elif t == "use_wildcard":                    # `use super::shift::*;`
            segs = []
            for g in n.children:
                segs.extend(self._use_path_segments(g))
            self._emit_use("::".join(prefix + segs + ["*"]), [], n, glob=True)
        elif t == "*":
            self._emit_use("::".join(prefix + ["*"]) if prefix else "*", [], n, glob=True)

    def _emit_use(self, spec: str, names: list, node, glob: bool = False):
        attrs: dict = {"specifier": spec, "dynamic": False}
        if glob:
            attrs["glob_import"] = True
        else:
            attrs["names"] = list(names)
        self._edge(self.mid, "imports", self._ext(spec), node,
                   "structural", "deterministic", 1.0, attrs)

    # ------------------------------------------------------------ pass 2
    def _bodies(self, node):
        if node.type == "function_item":
            name = ""
            for c in node.children:
                if c.type == "identifier":
                    name = _text(c, self.raw, 120)
                    break
            owner = self.fn_by_node.get(node.start_byte) or self.funcs.get(name)
            if owner is None:
                return
            self._calls_in(node, owner)
        elif node.type == "impl_item":
            for c in node.children:
                if c.type == "declaration_list":
                    for m in c.children:
                        if m.type == "function_item":
                            self._method_body(node, m)
        elif node.type == "trait_item":
            name = self._type_name(node)
            for c in node.children:
                if c.type == "declaration_list":
                    for m in c.children:
                        if m.type == "function_item":
                            fn = next((_text(g, self.raw, 120) for g in m.children
                                       if g.type == "identifier"), "")
                            owner = self.method_ids.get(f"{self.mod}.{name}.{fn}")
                            if owner is not None:
                                self._calls_in(m, owner)
        elif node.type == "mod_item":
            saved = self.mod
            nm = self._mod_name(node)
            if nm:
                self.mod = f"{saved}.{nm}"
            try:
                for c in node.children:
                    if c.type == "declaration_list":
                        for g in c.children:
                            self._bodies(g)
            finally:
                self.mod = saved

    def _method_body(self, impl_node, fn_node):
        target = self._impl_target(impl_node)
        fn = ""
        for g in fn_node.children:
            if g.type == "identifier":
                fn = _text(g, self.raw, 120)
                break
        if not fn or not target:
            return
        owner = self.method_ids.get(f"{self.mod}.{target}.{fn}")
        if owner is None:
            return
        self._calls_in(fn_node, owner)

    def _calls_in(self, node, owner: str):
        # Seed with children (the function node itself must not block
        # descent); nested named items are never descended into.
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type == "call_expression":
                self._call(n, owner)
            if n.type != "function_item":
                stack.extend(n.children)

    def _call(self, node, owner: str):
        fn = None
        for c in node.children:
            if c.type in ("identifier", "scoped_identifier",
                          "field_expression", "generic_function"):
                fn = c
                break
        if fn is not None and fn.type == "generic_function":
            # `f::<T>(x)` / `d.m::<T>()`: the callee is the function before the turbofish
            fn = next((g for g in fn.children if g.type in ("identifier", "scoped_identifier", "field_expression")), None)
        if fn is None:
            return
        if fn.type == "identifier":
            base = _text(fn, self.raw, 120)
            if base in RUST_MACRO_CALLS or base in getattr(self, "macro_calls", ()):
                return  # a std macro rewritten for parsing; its args are walked
            tgt = self.funcs.get(base)
            if tgt is not None:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9, {})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base})
            return
        if fn.type == "scoped_identifier":
            segs = self._use_path_segments(fn)
            if not segs:
                return
            self._edge(owner, "calls_unresolved",
                       self._ext("::".join(segs)), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "path", "root": segs[0],
                        "member": segs[-1], "full": "::".join(segs)})
            return
        # field_expression: receiver method. `self` is its own node kind
        # (not an identifier); chained receivers (`f().m()`) keep "".
        operand = ""
        member = ""
        for c in fn.children:
            if c.type == "identifier" and not operand:
                operand = _text(c, self.raw, 120)
            elif c.type == "self" and not operand:
                operand = "self"
            elif c.type == "field_identifier" and not member:
                member = _text(c, self.raw, 120)
        if not member:
            return
        self._edge(owner, "calls_unresolved",
                   self._ext(operand + "." + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "receiver", "base": operand,
                    "member": member})


def _clause_types(node, raw: bytes) -> list[str]:
    """Type names anywhere under a heritage clause (superclass may hold
    the identifier directly; super_interfaces nests it in type_list)."""
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "type_identifier":
            try:
                out.append(n.text.decode("utf-8", "ignore")[:120])
            except Exception:
                pass
        else:
            stack.extend(n.children)
    return out


_SWIFT_INFIX = ("additive_expression", "multiplicative_expression", "comparison_expression",
               "equality_expression", "conjunction_expression", "disjunction_expression",
               "nil_coalescing_expression", "range_expression", "infix_expression")
_ARG_NODES = ("arguments", "argument_list", "value_arguments", "call_suffix", "function_call_arguments")
_TYPE_ARG_NODES = ("type_arguments", "type_argument_list", "generic_arguments")


def _call_line(node, rel: str) -> int:
    """Line of the called name. A call chained on a new line (`d` / `  .m()`) starts where its receiver
    starts, but the name is on the member's line: that is the line a reader, a rename and `site_line` need."""
    row = node.start_point[0]
    if rel in ("calls", "calls_unresolved", "instantiates") and node.end_point[0] > row:
        kids = node.children
        for i, c in enumerate(kids):
            if c.type in _ARG_NODES and i > 0:
                j = i - 1
                while j > 0 and kids[j].type in _TYPE_ARG_NODES:
                    j -= 1
                leaf = kids[j]
                while leaf.child_count:
                    leaf = leaf.children[-1]
                return leaf.start_point[0] + 1
    return row + 1


def _dotted(node, raw: bytes) -> str:
    """Flatten a scoped path (a.b.C) to dots; single names unchanged.
    Covers `identifier`/`type_identifier` (most grammars) and PHP's
    `name` nodes."""
    parts: list[str] = []

    def _walk(n):
        if n.type in ("identifier", "type_identifier", "name"):
            parts.append(_text(n, raw, 120))
        elif n.type in ("type_arguments", "type_argument_list", "type_parameters"):
            return      # `Base<Query>`: Query is an argument of Base, not part of its path
        else:
            for c in n.children:
                if c.type not in (".", "::"):
                    _walk(c)

    _walk(node)
    return ".".join(p for p in parts if p)


def _java_members(body):
    """Members of a Java type body; an enum's members after its constants sit
    in `enum_body_declarations` (missed before v26: every enum method)."""
    for m in body.children:
        if m.type == "enum_body_declarations":
            yield from m.children
        else:
            yield m


class _JavaVisitor:
    """Two-pass Java walker. Package clause gives module identity
    (dir-based fallback); imports (single/on-demand/static), classes
    (extends/implements), interfaces, enums (coarse class), annotations
    (interface), methods, constructors (`<init>`), `new X()` as
    `instantiates`. Unqualified/`this` calls shape for java_resolve;
    object-variable calls stay unresolved (no flow analysis). Anonymous
    class bodies walk under the enclosing method (documented)."""

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{ext.language_id}"
        self.pkg = ""
        self.class_stack: list[tuple[str, str]] = []  # (name, node id)
        self.methods: dict[str, str] = {}  # qname -> node id (same file)

    def _mod(self) -> str:
        return self.pkg or self.mod

    def _sym(self, kind: str, qname: str, node, sig: str = "") -> str:
        nid = canonical_symbol_id(self.repo_id, "java", kind, qname, sig)
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1], qname,
                                   self.project_id, self.repo_id, self.rel,
                                   _loc(node), "java", "", {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score,
                                   [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        for stmt in root.children:
            t = stmt.type
            if t == "package_declaration":
                for c in stmt.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        self.pkg = _dotted(c, self.raw)
                        break
                if self.pkg:
                    for n in self.res.nodes:
                        if n.id == self.mid:
                            n.label = self.pkg
                            n.qualified_name = self.pkg
            elif t == "import_declaration":
                self._imports(stmt)
            elif t in ("class_declaration", "interface_declaration",
                       "enum_declaration", "annotation_type_declaration"):
                self._type_decl(stmt, self.mid, outer="")
        for stmt in root.children:
            if stmt.type in ("class_declaration", "interface_declaration",
                             "enum_declaration",
                             "annotation_type_declaration"):
                self._bodies(stmt, [])

    def _imports(self, node):
        static = False
        glob = False
        path = ""
        for c in node.children:
            if c.type == "static":
                static = True
            elif c.type == "asterisk":
                glob = True
            elif c.type in ("scoped_identifier", "identifier"):
                path = _dotted(c, self.raw)
        if not path:
            return
        attrs: dict = {"specifier": path, "dynamic": False}
        if glob:
            attrs["glob_import"] = True
            names: list = []
        elif static:
            attrs["static_import"] = True
            names = [path.rsplit(".", 1)[-1]]
        else:
            names = [path.rsplit(".", 1)[-1]]
        attrs["names"] = names
        self._edge(self.mid, "imports", self._ext(path), node,
                   "structural", "deterministic", 1.0, attrs)

    def _type_decl(self, node, parent: str, outer: str):
        kind = {"class_declaration": "class",
                "interface_declaration": "interface",
                "enum_declaration": "class",
                "annotation_type_declaration": "interface"}.get(node.type,
                                                                "class")
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return None
        qn = f"{self._mod()}.{outer + '.' if outer else ''}{name}"
        nid = self._sym(kind, qn, node)
        self._defines(parent, nid, node)
        # heritage (ext placeholders; java_resolve upgrades same-package
        # and imported supertypes like js step 1b). Type names may nest
        # (type_list under super_interfaces), so walk the clause subtree.
        for c in node.children:
            if c.type == "superclass":
                for nm in _clause_types(c, self.raw):
                    self._edge(nid, "inherits", self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"superclass": nm})
            elif c.type in ("super_interfaces", "extends_interfaces"):
                rel = "inherits" if node.type == \
                    "interface_declaration" else "implements"
                for nm in _clause_types(c, self.raw):
                    self._edge(nid, rel, self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"interface": nm})
        self.class_stack.append((name, nid))
        for c in node.children:
            if c.type in ("class_body", "interface_body", "enum_body",
                          "annotation_type_body", "declaration_list"):
                for m in _java_members(c):
                    if m.type == "enum_constant":
                        self._enum_constant(m, nid, qn)
                    elif m.type in ("class_declaration",
                                  "interface_declaration",
                                  "enum_declaration",
                                  "annotation_type_declaration"):
                        self._type_decl(m, nid, (outer + "." if outer else "")
                                        + name)
                    elif m.type == "method_declaration":
                        self._method(m, nid, qn)
                    elif m.type == "constructor_declaration":
                        self._ctor(m, nid, qn)
                    elif m.type == "field_declaration":
                        self._field_anon(m, nid, qn)
        self.class_stack.pop()
        return nid

    def _anon_bodies(self, decl):
        """`X = new T<..>() { ... }` -> (X, T<..>, class_body) per declarator."""
        for v in decl.children:
            if v.type != "variable_declarator":
                continue
            nm, val = v.child_by_field_name("name"), v.child_by_field_name("value")
            if nm is None or val is None or val.type != "object_creation_expression":
                continue
            body = next((c for c in val.children if c.type == "class_body"), None)
            if body is None:
                continue
            tn = val.child_by_field_name("type")
            yield _text(nm, self.raw, 120), (_text(tn, self.raw, 200) if tn is not None else ""), body

    def _field_anon(self, decl, parent: str, qn: str):
        # `static final TypeAdapter<URI> URI = new TypeAdapter<URI>() {...}`:
        # the anonymous class is a real type named by its field, with methods
        for name, tname, body in self._anon_bodies(decl):
            aqn = f"{qn}.{name}"
            nid = self._sym("class", aqn, decl)
            self._defines(parent, nid, decl)
            base = tname.split("<", 1)[0].strip()
            if base:
                self._edge(nid, "inherits", self._ext(base), decl, "resolved", "resolved", 0.9,
                           {"superclass": base, "anonymous": True})
            for m in body.children:
                if m.type == "method_declaration":
                    self._method(m, nid, aqn)

    def _enum_constant(self, m, parent: str, qn: str):
        # `Data { void read(..) {..} }`: a constant with a body is a subclass
        # of the enum, named by the constant
        body = m.child_by_field_name("body") or next(
            (c for c in m.children if c.type == "class_body"), None)
        nm = m.child_by_field_name("name")
        if body is None or nm is None:
            return
        cqn = f"{qn}.{_text(nm, self.raw, 120)}"
        cid = self._sym("class", cqn, m)
        self._defines(parent, cid, m)
        for mm in body.children:
            if mm.type == "method_declaration":
                self._method(mm, cid, cqn)

    def _type_owner(self, node):
        want = node.start_point[0] + 1
        for n in self.res.nodes:
            loc = getattr(n, "location", None)
            if n.file_path == self.rel and n.type in ("class", "interface", "enum") and \
                    loc is not None and getattr(loc, "start_line", 0) == want:
                return n.id
        return None

    def _modifiers(self, node) -> dict:
        attrs: dict = {}
        for c in node.children:
            if c.type == "modifiers":
                toks = _text(c, self.raw, 60).split()
                if "static" in toks:
                    attrs["static"] = True
                if "abstract" in toks:
                    attrs["abstract"] = True
        return attrs

    def _method(self, node, parent: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        attrs = self._modifiers(node)
        nid = self._sym("method", f"{qn}.{name}", node)
        self.methods[f"{qn}.{name}"] = nid
        self._defines(parent, nid, node)

    def _ctor(self, node, parent: str, qn: str):
        attrs = self._modifiers(node)
        nid = self._sym("method", f"{qn}.<init>", node)
        self.methods[f"{qn}.<init>"] = nid
        self._defines(parent, nid, node)

    # ------------------------------------------------------------ pass 2
    def _bodies(self, node, outer: list):
        for c in node.children:
            if c.type in ("class_body", "interface_body", "enum_body",
                          "annotation_type_body", "declaration_list"):
                for m in _java_members(c):
                    if m.type == "enum_constant":
                        owner_ = self._type_owner(node)
                        if owner_:
                            self._calls_in(m, owner_)       # `A(f())`: the arguments run when the enum loads
                        body = m.child_by_field_name("body") or next(
                            (x for x in m.children if x.type == "class_body"), None)
                        for mm in (body.children if body is not None else ()):
                            if mm.type == "method_declaration":
                                self._method_body(mm, outer)
                        continue
                    if m.type in ("static_initializer", "block"):
                        owner_ = self._type_owner(node)        # `static { f(); }` / `{ f(); }`
                        if owner_:
                            self._calls_in(m, owner_)
                        continue
                    if m.type in ("class_declaration",
                                  "interface_declaration",
                                  "enum_declaration",
                                  "annotation_type_declaration"):
                        nm = ""
                        for g in m.children:
                            if g.type == "identifier":
                                nm = _text(g, self.raw, 120)
                                break
                        self._bodies(m, outer + [nm] if nm else outer)
                    elif m.type in ("method_declaration",
                                    "constructor_declaration"):
                        self._method_body(m, outer)
                    elif m.type == "field_declaration":
                        anon = list(self._anon_bodies(m))
                        for _n, _t, body in anon:
                            for mm in body.children:
                                if mm.type == "method_declaration":
                                    self._method_body(mm, outer)
                        if not anon:
                            # `static final Pattern P = Pattern.compile(..)`:
                            # initializer calls belong to the class
                            owner = self._type_owner(node)
                            if owner:
                                self._calls_in(m, owner)

    def _method_body(self, node, outer: list):
        # Owner match by (file, start line): unique per method, exact.
        owner = None
        want = node.start_point[0] + 1
        for n in self.res.nodes:
            if n.type != "method" or n.file_path != self.rel:
                continue
            loc = getattr(n, "location", None)
            if (getattr(loc, "start_line", 0) if loc else 0) == want:
                owner = n.id
                break
        if owner is None:
            return
        self._calls_in(node, owner)

    def _calls_in(self, node, owner: str):
        # Anonymous classes walk under the enclosing method (documented):
        # their calls keep file-correct, owner-approximate attribution.
        # Named methods/types never descend (they own their own bodies;
        # descending would double-attribute their calls to the outer).
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type == "method_invocation":
                self._invocation(n, owner)
            elif n.type == "object_creation_expression":
                self._creation(n, owner)
            if n.type in ("method_declaration", "constructor_declaration",
                          "class_declaration", "interface_declaration",
                          "enum_declaration", "annotation_type_declaration"):
                # methods of an anonymous class (`new Runnable() { void run() {..} }`)
                # have no node of their own: their calls belong to the enclosing owner
                p = n.parent
                if n.type == "method_declaration" and p is not None and p.type == "class_body" \
                        and p.parent is not None and p.parent.type == "object_creation_expression":
                    stack.extend(n.children)
                continue
            stack.extend(n.children)

    def _invocation(self, node, owner: str):
        obj = node.child_by_field_name("object")
        nm_node = node.child_by_field_name("name")
        name = _text(nm_node, self.raw, 120) if nm_node is not None else ""
        if not name:
            for c in node.children:
                if c.type == "identifier":
                    name = _text(c, self.raw, 120)
                    break
        if not name:
            return
        site = nm_node if nm_node is not None else node   # `.build();` on its own line
        if obj is None:
            self._edge(owner, "calls_unresolved", self._ext(name), site,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "function", "base": name})
            return
        if obj.type == "this":
            self._edge(owner, "calls_unresolved", self._ext(name), site,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "this", "member": name})
            return
        if obj.type == "super":
            self._edge(owner, "calls_unresolved", self._ext(name), site,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "super", "member": name})
            return
        if obj.type == "identifier":
            base = _text(obj, self.raw, 120)
            self._edge(owner, "calls_unresolved",
                       self._ext(base + "." + name), site,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "selector", "base": base,
                        "member": name})
            return
        self._edge(owner, "calls_unresolved", self._ext(name), site,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "complex", "member": name})

    def _creation(self, node, owner: str):
        nm = ""
        for c in node.children:
            if c.type in ("type_identifier", "scoped_type_identifier",
                          "generic_type"):
                nm = _dotted(c, self.raw).split(".")[-1]
                break
        if not nm:
            return
        self._edge(owner, "instantiates", self._ext(nm), node, "resolved",
                   "resolved", 0.9, {"created": nm})


def _cs_decl_name(node, raw: bytes) -> str:
    """Declared name of a C# method/local function. The grammar's `name`
    field, never "first identifier": with a class-typed return value
    (`Task Run()`, `IMediator Build()`) the first identifier is the type."""
    n = node.child_by_field_name("name")
    if n is not None and n.type == "identifier":
        return _text(n, raw, 120)
    ids = [c for c in node.children if c.type == "identifier"]
    params = [c for c in node.children if c.type == "parameter_list"]
    if params:
        before = [c for c in ids if c.end_byte <= params[0].start_byte]
        if before:
            return _text(before[-1], raw, 120)
    return _text(ids[-1], raw, 120) if ids else ""


# C# members whose bodies hold calls but that are not methods themselves.
_CS_MEMBER_BODIES = ("property_declaration", "indexer_declaration", "field_declaration",
                     "event_declaration", "event_field_declaration", "operator_declaration",
                     "conversion_operator_declaration", "destructor_declaration")


class _CsVisitor:
    """Two-pass C# walker. Namespace (block or file-scoped `;`), usings
    (plain/static/alias), classes (first base entry = superclass by
    language rule, rest = interfaces), interfaces, structs/records
    (coarse class), enums (coarse type), methods (incl. arrow bodies and
    local functions as methods with `local_function` attr), `new X()`
    as `instantiates`. Property/indexer bodies are skipped (documented:
    accessor logic, revisit on evidence). Calls: locals, this/base,
    Type.member, object vars unresolved."""

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{ext.language_id}"
        self.ns = ""
        self.funcs: dict[str, str] = {}

    def _mod(self) -> str:
        return self.ns or self.mod

    def _sym(self, kind: str, qname: str, node, sig: str = "") -> str:
        nid = canonical_symbol_id(self.repo_id, "c_sharp", kind, qname, sig)
        # label without the generic arity: `Policy`1` is labelled Policy
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1].split("`")[0], qname,
                                   self.project_id, self.repo_id, self.rel,
                                   _loc(node), "c_sharp", "", {},
                                   [self.ev.id], schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score,
                                   [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        pending_ns = ""
        for stmt in root.children:
            t = stmt.type
            if t in ("namespace_declaration",
                       "file_scoped_namespace_declaration"):
                ns, body = self._namespace(stmt)
                if body is not None:
                    saved, self.ns = self.ns, ns
                    for m in body:
                        self._member(m)
                    self.ns = saved
                else:
                    pending_ns = ns
            elif t == "using_directive":
                self._using(stmt)
            elif t in ("class_declaration", "interface_declaration",
                       "struct_declaration", "record_declaration",
                       "enum_declaration"):
                if pending_ns:
                    saved, self.ns = self.ns, pending_ns
                    self._type_decl(stmt, self.mid, "")
                    self.ns = saved
                else:
                    self._type_decl(stmt, self.mid, "")
        # file-scoped namespace without body handled above via pending;
        # second pass walks method bodies with resolved owners.
        for stmt in root.children:
            self._bodies(stmt)
            if stmt.type == "global_statement":
                # C# top-level statements (Program.cs): the program's own code
                self._calls_in(stmt, self.mid)

    def _namespace(self, node):
        ns = ""
        body = None
        for c in node.children:
            if c.type == "qualified_name":
                ns = _dotted(c, self.raw)
            elif c.type == "identifier" and not ns:
                # File-scoped single-segment `namespace Shop;`
                ns = _text(c, self.raw, 120)
            elif c.type == "declaration_list":
                body = [m for m in c.children]
        return ns, body

    def _member(self, node):
        if node.type in ("class_declaration", "interface_declaration",
                         "struct_declaration", "record_declaration",
                         "enum_declaration"):
            self._type_decl(node, self.mid, "")

    def _using(self, node):
        static = any(c.type == "static" for c in node.children)
        has_eq = any(c.type == "=" for c in node.children)
        idents = [c for c in node.children if c.type == "identifier"]
        quals = [c for c in node.children if c.type == "qualified_name"]
        if has_eq:
            # Alias form `using IO = System.IO;`: first identifier is
            # the alias, the qualified name the target. Malformed
            # without both sides: never guess.
            if not idents or not quals:
                return
            alias = _text(idents[0], self.raw, 120)
            path = _dotted(quals[0], self.raw)
            if not alias or not path:
                return
            attrs: dict = {"specifier": path, "dynamic": False,
                           "alias": alias, "names": [alias]}
        elif quals:
            path = _dotted(quals[0], self.raw)
            if not path:
                return
            attrs = {"specifier": path, "dynamic": False, "names": []}
            if static:
                attrs["static_import"] = True
        elif idents:
            path = _text(idents[0], self.raw, 120)
            if not path:
                return
            attrs = {"specifier": path, "dynamic": False, "names": []}
            if static:
                attrs["static_import"] = True
        else:
            return
        self._edge(self.mid, "imports", self._ext(path), node,
                   "structural", "deterministic", 1.0, attrs)

    def _type_decl(self, node, parent: str, outer: str):
        kind = {"class_declaration": "class",
                "interface_declaration": "interface",
                "struct_declaration": "class",
                "record_declaration": "class",
                "enum_declaration": "type"}.get(node.type, "class")
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return None
        tps = next((c for c in node.children if c.type == "type_parameter_list"), None)
        if tps is not None:
            # `Policy` and `Policy<TResult>` are two types (CLR `Policy`1`);
            # one qualified name merged their members (wrong overloads).
            name = f"{name}`{sum(1 for g in tps.children if g.type == 'type_parameter')}"
        qn = f"{self._mod()}.{outer + '.' if outer else ''}{name}"
        nid = self._sym(kind, qn, node)
        self._defines(parent, nid, node)
        # base_list: first entry is the superclass by language rule
        # (at most one class, must come first); rest are interfaces.
        seen_base = False
        for c in node.children:
            if c.type != "base_list":
                continue
            for g in c.children:
                if g.type not in ("identifier", "qualified_name",
                                  "generic_name"):
                    continue
                nm = _dotted(g, self.raw).split(".")[-1]
                if not nm:
                    continue
                if not seen_base:
                    seen_base = True
                    self._edge(nid, "inherits", self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"superclass": nm})
                else:
                    self._edge(nid, "implements", self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"interface": nm})
        for c in node.children:
            if c.type == "declaration_list":
                for m in c.children:
                    if m.type in ("class_declaration",
                                  "interface_declaration",
                                  "struct_declaration",
                                  "record_declaration",
                                  "enum_declaration"):
                        self._type_decl(m, nid, (outer + "." if outer
                                                 else "") + name)
                    elif m.type == "method_declaration":
                        self._method(m, nid, qn, False)
                    elif m.type == "constructor_declaration":
                        # Java parity: constructors are `<init>` methods, so the
                        # calls in their bodies have a caller (they were lost)
                        self._ctor(m, nid, qn)
        return nid

    def _ctor(self, node, parent: str, qn: str):
        static = any(c.type == "modifier" and _text(c, self.raw, 20) == "static"
                     for c in node.children)
        name = "<clinit>" if static else "<init>"
        params = next((c for c in node.children if c.type == "parameter_list"), None)
        sig = _text(params, self.raw, 300) if params is not None else ""
        nid = self._sym("method", f"{qn}.{name}", node, sig)
        self.methods = getattr(self, "methods", {})
        self.methods.setdefault(f"{qn}.{name}", nid)
        self._defines(parent, nid, node)

    def _method(self, node, parent: str, qn: str, local: bool):
        name = _cs_decl_name(node, self.raw)
        if not name:
            return
        attrs = {"local_function": True} if local else {}
        if any(c.type == "explicit_interface_specifier" for c in node.children):
            # `void IFoo.Run()`: callable only through IFoo, never via the class
            attrs["explicit_interface"] = True
        nid = self._sym("method", f"{qn}.{name}", node)
        if attrs:
            for n in self.res.nodes:
                if n.id == nid:
                    n.attributes = dict(n.attributes or {})
                    n.attributes.update(attrs)
        self.methods = getattr(self, "methods", {})
        self.methods[f"{qn}.{name}"] = nid
        self._defines(parent, nid, node)

    # ------------------------------------------------------------ pass 2
    def _bodies(self, node):
        if node.type in ("class_declaration", "interface_declaration",
                         "struct_declaration", "record_declaration",
                         "enum_declaration", "namespace_declaration"):
            type_id = self._node_at(node, ("class", "interface", "type")) \
                if node.type != "namespace_declaration" else None
            for c in node.children:
                if c.type == "declaration_list":
                    for m in c.children:
                        if type_id and m.type in _CS_MEMBER_BODIES:
                            # property/indexer accessors, field initializers,
                            # operators: their calls belong to the type
                            self._calls_in(m, type_id, accessors=True)
                            continue
                        self._bodies(m)
                elif c.type in ("class_declaration",
                                "interface_declaration",
                                "struct_declaration",
                                "record_declaration", "enum_declaration",
                                "method_declaration"):
                    self._bodies(c)
            return
        if node.type not in ("method_declaration", "constructor_declaration"):
            for c in node.children:
                self._bodies(c)
            return
        # owner by start line (unique per method)
        owner = self._node_at(node, ("method",))
        if owner is None:
            return
        # local functions declared inside: own method nodes first
        for c in node.children:
            self._declare_locals(c, owner)
        self._calls_in(node, owner)

    def _node_at(self, node, types: tuple):
        """Id of the symbol this declaration produced (matched by start line)."""
        want = node.start_point[0] + 1
        for n in self.res.nodes:
            if n.type not in types or n.file_path != self.rel:
                continue
            loc = getattr(n, "location", None)
            if (getattr(loc, "start_line", 0) if loc else 0) == want:
                return n.id
        return None

    def _declare_locals(self, node, parent: str):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "local_function_statement":
                name = _cs_decl_name(n, self.raw)
                if name:
                    pq = ""
                    for m in self.res.nodes:
                        if m.id == parent:
                            pq = m.qualified_name
                    nid = self._sym("method", f"{pq}.{name}", n)
                    for m in self.res.nodes:
                        if m.id == nid:
                            m.attributes = dict(m.attributes or {})
                            m.attributes["local_function"] = True
                    self._defines(parent, nid, n)
                    self._calls_in(n, nid)
            if n.type not in ("method_declaration", "class_declaration",
                              "interface_declaration", "struct_declaration",
                              "record_declaration", "enum_declaration",
                              "local_function_statement"):
                stack.extend(n.children)

    def _calls_in(self, node, owner: str, accessors: bool = False):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type == "invocation_expression":
                self._invocation(n, owner)
            elif n.type == "object_creation_expression":
                nm = ""
                for c in n.children:
                    if c.type in ("identifier", "qualified_name",
                                  "generic_name"):
                        nm = _dotted(c, self.raw).split(".")[-1]
                        break
                if nm:
                    attrs = {"created": nm}
                    q = next((c for c in n.children if c.type == "qualified_name"), None)
                    if q is not None:
                        attrs["qualified"] = _dotted(q, self.raw).split("<")[0]  # `new Edit.Handler(..)`
                    self._edge(owner, "instantiates", self._ext(nm), n,
                               "resolved", "resolved", 0.9, attrs)
            if n.type not in ("method_declaration", "class_declaration",
                              "interface_declaration", "struct_declaration",
                              "record_declaration", "enum_declaration",
                              "local_function_statement", "constructor_declaration") and (
                    accessors or n.type not in ("accessor_list", "accessor_declaration")):
                stack.extend(n.children)

    def _invocation(self, node, owner: str):
        fn = None
        for c in node.children:
            if c.type in ("identifier", "member_access_expression", "conditional_access_expression"):
                fn = c
                break
        if fn is None:
            return
        if fn.type == "identifier":
            base = _text(fn, self.raw, 120)
            self._edge(owner, "calls_unresolved", self._ext(base), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "function", "base": base})
            return
        if fn.type == "conditional_access_expression":
            # `d?.M()` / `a?.b?.M()`: the receiver is what comes before `?`, the member is the last name
            named = [c for c in fn.children if c.is_named]
            leaf = fn
            while leaf.child_count:
                leaf = leaf.children[-1]
            member = _text(leaf, self.raw, 120).split("<")[0].strip()
            if not named or not member:
                return
            recv = named[0]
            if recv.type in ("this_expression", "base_expression"):
                self._edge(owner, "calls_unresolved", self._ext(member), node, "structural", "deterministic", 1.0,
                           {"callee_kind": "this" if recv.type == "this_expression" else "super", "member": member})
            elif recv.type == "identifier" and len(named) == 2:
                base = _text(recv, self.raw, 120)
                self._edge(owner, "calls_unresolved", self._ext(base + "." + member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": base, "member": member})
            else:
                self._edge(owner, "calls_unresolved", self._ext(member), node, "structural", "deterministic", 1.0,
                           {"callee_kind": "complex", "member": member})
            return
        # member_access_expression. When the receiver is itself an
        # expression (`this._m.Send()`, `GetIt().Send()`, `a.b.C()`), the
        # direct-identifier scan saw only `Send` and the call looked like a
        # bare `Send()` -> bound to the *enclosing* class's Send (wrong).
        # Record it as `complex` with the member; receiver typing reads the
        # receiver text from the call site instead.
        recv = fn.child_by_field_name("expression")
        nm = fn.child_by_field_name("name")
        if recv is not None and nm is not None and recv.type not in (
                "identifier", "this_expression", "base_expression", "this", "base"):
            member = _text(nm, self.raw, 120).split("<")[0].strip()   # `M<T>`
            if member:
                attrs = {"callee_kind": "complex", "member": member}
                qual = re.sub(r"\s+", "", _text(recv, self.raw, 200))   # `context\n    .Articles`
                if recv.type == "member_access_expression" and re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", qual):
                    # `Conduit.Features.Tags.TagCanonical.Canonical(x)`: a
                    # dotted name; the binder decides whether it is a type
                    attrs["qualifier"] = qual
                self._edge(owner, "calls_unresolved", self._ext(member), node,
                           "structural", "deterministic", 1.0, attrs)
            return
        # `s.FromJson<R>()`: the member is a generic_name
        ids = [_text(c, self.raw, 120) if c.type == "identifier"
               else _text(c, self.raw, 120).split("<")[0].strip()
               for c in fn.children if c.type in ("identifier", "generic_name")]
        ops = [c.type for c in fn.children
               if c.type in ("this_expression", "base_expression")]
        if ops:
            kind = "this" if ops[0] == "this_expression" else "super"
            if ids:
                self._edge(owner, "calls_unresolved", self._ext(ids[-1]),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": kind, "member": ids[-1]})
            return
        if not ids:
            return
        if len(ids) == 1:
            self._edge(owner, "calls_unresolved", self._ext(ids[0]), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "function", "base": ids[0]})
            return
        base, member = ids[0], ids[-1]
        self._edge(owner, "calls_unresolved",
                   self._ext(base + "." + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "selector", "base": base,
                    "member": member})


def _php_names(node, raw: bytes) -> list[str]:
    """Name-ish tokens in source order, qualified paths split into
    segments (for `::`-split call paths)."""
    out: list[str] = []
    for c in node.children:
        if c.type in ("name", "identifier", "variable_name"):
            try:
                out.append(c.text.decode("utf-8", "ignore")[:120])
            except Exception:
                pass
        elif c.type == "qualified_name":
            for seg in _dotted(c, raw).replace(".", "\\").split("\\"):
                if seg:
                    out.append(seg)
    return out


class _PhpVisitor:
    """Two-pass PHP walker. Namespace (semicolon or braced form),
    namespace-`use` (single/function/const/group; class-body trait-`use`
    is IGNORED — it is not an import), classes (extends/implements),
    traits (coarse type), interfaces, enums (coarse type), methods
    (incl. `__construct`), top functions. Calls: bare functions,
    `$this->`, `Class::`/`self::`/`static::`/`parent::`, `new X()` as
    `instantiates`; `$var->` stays unresolved. Includes skipped
    (dynamic paths, documented)."""

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{ext.language_id}"
        self.ns = ""

    def _fq(self, name: str) -> str:
        return f"{self.ns}\\{name}" if self.ns else name

    def _sym(self, kind: str, qname: str, node, sig: str = "") -> str:
        # Labels are always the bare member name: type qnames use
        # backslashes, method qnames dots (agent-caught inconsistency).
        nid = canonical_symbol_id(self.repo_id, "php", kind, qname, sig)
        self.res.nodes.append(Node(nid, kind, qname.replace("\\", ".").split(
            ".")[-1], qname, self.project_id, self.repo_id, self.rel,
                                   _loc(node), "php", "", {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score,
                                   [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        pending_ns = ""
        for stmt in root.children:
            t = stmt.type
            if t == "namespace_definition":
                ns, body = self._namespace(stmt)
                if body is not None:
                    saved, self.ns = self.ns, ns
                    for m in body:
                        self._member(m)
                    self.ns = saved
                else:
                    pending_ns = ns
            elif t == "namespace_use_declaration":
                self._uses(stmt)
            elif t in ("class_declaration", "interface_declaration",
                       "trait_declaration", "enum_declaration",
                       "function_definition"):
                if pending_ns:
                    saved, self.ns = self.ns, pending_ns
                    self._member(stmt)
                    self.ns = saved
                else:
                    self._member(stmt)
        for stmt in root.children:
            self._bodies(stmt)
            if stmt.type in ("expression_statement", "echo_statement", "if_statement",
                             "foreach_statement", "for_statement", "while_statement",
                             "return_statement", "compound_statement"):
                # file-level code (views, routes/web.php, config, scripts):
                # its calls belong to the file's module
                self._calls_in(stmt, self.mid)

    def _namespace(self, node):
        ns = ""
        body = None
        for c in node.children:
            if c.type == "namespace_name":
                ns = _dotted(c, self.raw).replace(".", "\\")
            elif c.type == "declaration_list":
                body = [m for m in c.children]
        return ns, body

    def _member(self, node):
        t = node.type
        if t == "class_declaration":
            self._class(node, "class")
        elif t == "interface_declaration":
            self._class(node, "interface")
        elif t == "trait_declaration":
            self._class(node, "type")
        elif t == "enum_declaration":
            self._class(node, "type")
        elif t == "function_definition":
            name = ""
            for c in node.children:
                if c.type == "name":
                    name = _text(c, self.raw, 120)
                    break
            if name:
                nid = self._sym("function", self._fq(name), node)
                self._defines(self.mid, nid, node)

    def _class(self, node, kind: str):
        name = ""
        for c in node.children:
            if c.type == "name":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return None
        nid = self._sym(kind, self._fq(name), node)
        self._defines(self.mid, nid, node)
        # Heritage by token scan (robust to wrapper shapes): `extends X`,
        # then `implements A, B` up to the declaration list.
        mode = None
        # Newer grammars wrap the tokens: `base_clause` / `class_interface_clause`.
        kids = []
        for c in node.children:
            kids.extend(c.children if c.type in ("base_clause", "class_interface_clause") else [c])
        for c in kids:
            txt = ""
            try:
                txt = c.text.decode("utf-8", "ignore")[:20]
            except Exception:
                pass
            if txt == "extends":
                mode = "inherits"
                continue
            if txt == "implements":
                mode = "implements"
                continue
            if c.type == "declaration_list":
                mode = None
                for m in c.children:
                    if m.type == "method_declaration":
                        self._method(m, nid)
                    elif m.type == "use_declaration":
                        # `use CalculateChecksumFromStream;`: trait methods become this class's
                        for tn in m.children:
                            if tn.type in ("name", "qualified_name"):
                                nm = _dotted(tn, self.raw).replace(".", "\\").split("\\")[-1]
                                if nm:
                                    self._edge(nid, "implements", self._ext(nm), tn, "resolved", "resolved",
                                               0.9, {"interface": nm, "trait": True})
                continue
            if mode and c.type in ("name", "qualified_name"):
                nm = _dotted(c, self.raw).replace(".", "\\").split("\\")[-1]
                if nm:
                    self._edge(nid, mode, self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"superclass" if mode == "inherits"
                                else "interface": nm})
        return nid

    def _method(self, node, parent: str):
        name = ""
        for c in node.children:
            if c.type == "name":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        attrs = {"static": True} if any(
            c.type == "static_modifier" for c in node.children) else {}
        # parent qualified name + member
        pq = ""
        for n in self.res.nodes:
            if n.id == parent:
                pq = n.qualified_name
        nid = self._sym("method", f"{pq}.{name}" if pq else name, node)
        if attrs:
            for n in self.res.nodes:
                if n.id == nid:
                    n.attributes = dict(n.attributes or {})
                    n.attributes.update(attrs)
        self._defines(parent, nid, node)

    def _uses(self, node):
        # Top-level namespace-use ONLY (callers pass program children;
        # class-body trait-use is a different node type and never
        # reaches here). Group clauses expand; unparseable shapes keep
        # the specifier with no names (namespace-only, never guessed).
        for c in node.children:
            if c.type == "namespace_use_clause":
                self._use_clause(c)
            elif c.type == "namespace_use_group":
                for g in c.children:
                    if g.type == "namespace_use_clause":
                        self._use_clause(g)

    def _use_clause(self, node):
        kind = "class"
        for c in node.children:
            if c.type in ("function", "const"):
                kind = c.type
        spec = ""
        alias = ""
        seen_as = False
        for c in node.children:
            if c.type == "as":
                seen_as = True
                continue
            if c.type == "qualified_name":
                spec = _dotted(c, self.raw).replace(".", "\\")
            elif c.type == "name":
                if seen_as:
                    alias = _text(c, self.raw, 120)
        if not spec:
            return
        local = alias or spec.split("\\")[-1]
        attrs: dict = {"specifier": spec, "dynamic": False,
                       "use_kind": kind, "names": [local] if local else []}
        self._edge(self.mid, "imports", self._ext(spec), node,
                   "structural", "deterministic", 1.0, attrs)

    # ------------------------------------------------------------ pass 2
    def _bodies(self, node):
        if node.type in ("class_declaration", "interface_declaration",
                         "trait_declaration", "enum_declaration"):
            for c in node.children:
                if c.type == "declaration_list":
                    for m in c.children:
                        if m.type == "method_declaration":
                            self._method_body(m)
                        elif m.type in ("class_declaration",
                                        "interface_declaration"):
                            self._bodies(m)
            return
        if node.type == "function_definition":
            want = node.start_point[0] + 1
            for n in self.res.nodes:
                if n.type != "function" or n.file_path != self.rel:
                    continue
                loc = getattr(n, "location", None)
                if (getattr(loc, "start_line", 0) if loc else 0) == want:
                    self._calls_in(node, n.id)
                    break
            return
        for c in node.children:
            self._bodies(c)

    def _method_body(self, node):
        want = node.start_point[0] + 1
        for n in self.res.nodes:
            if n.type != "method" or n.file_path != self.rel:
                continue
            loc = getattr(n, "location", None)
            if (getattr(loc, "start_line", 0) if loc else 0) == want:
                self._calls_in(node, n.id)
                break

    def _calls_in(self, node, owner: str):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type == "function_call_expression":
                self._call(n, owner)
            elif n.type in ("member_call_expression", "nullsafe_member_call_expression"):
                self._member_call(n, owner)
            elif n.type == "scoped_call_expression":
                self._scoped_call(n, owner)
            elif n.type == "object_creation_expression":
                nm = ""
                for c in n.children:
                    if c.type in ("name", "qualified_name"):
                        nm = _dotted(c, self.raw).replace(".", "\\").split(
                            "\\")[-1]
                        break
                if nm:
                    self._edge(owner, "instantiates", self._ext(nm), n,
                               "resolved", "resolved", 0.9, {"created": nm})
            # arrow functions / closures run as part of their enclosing
            # function: their calls belong to it (`fn ($r) => f($r)`)
            if n.type not in ("method_declaration", "function_definition",
                              "class_declaration"):
                stack.extend(n.children)

    def _call(self, node, owner: str):
        name = ""
        for c in node.children:
            if c.type == "name":
                name = _text(c, self.raw, 120)
                break
            if c.type == "qualified_name":
                # `\mock_function()` (global) / `Util\f()` (namespace-relative):
                # these calls were dropped; keep the spelling, php_resolve
                # applies PHP's lookup rules to it
                name = re.sub(r"\s+", "", _text(c, self.raw, 200))
                break
        if not name:
            return
        self._edge(owner, "calls_unresolved", self._ext(name.lstrip("\\").split("\\")[-1]), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "function", "base": name})

    def _member_call(self, node, owner: str):
        obj = node.child_by_field_name("object")
        if obj is not None and obj.type not in ("variable_name", "name"):
            # `$this->resolver->resolve()`, `foo()->bar()`: the receiver is an
            # expression. These calls were dropped entirely; record the site
            # (receiver typing reads the receiver text from the call site).
            nm = node.child_by_field_name("name")
            member = _text(nm, self.raw, 120) if nm is not None else ""
            if member:
                attrs = {"callee_kind": "complex", "member": member}
                made = _php_container_type(obj, self.raw)
                if made:
                    # `app(SettingService::class)->m()`: the container
                    # returns an instance of the named class
                    attrs["receiver_type"] = made
                self._edge(owner, "calls_unresolved", self._ext(member), node,
                           "structural", "deterministic", 1.0, attrs)
            return
        toks = _php_names(node, self.raw)
        if not toks:
            return
        recv = toks[0]
        member = toks[-1] if len(toks) > 1 else ""
        if not member:
            return
        if recv == "$this":
            self._edge(owner, "calls_unresolved", self._ext(member), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "this", "member": member})
            return
        self._edge(owner, "calls_unresolved",
                   self._ext(recv + "->" + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "var", "base": recv, "member": member})

    def _scoped_call(self, node, owner: str):
        scope = node.child_by_field_name("scope")
        if scope is not None and scope.type == "relative_scope":
            # `self::m()` / `static::m()` / `parent::m()`: the scope keyword
            # is not a name token, so it was read as a bare function call.
            nm = node.child_by_field_name("name")
            member = _text(nm, self.raw, 120) if nm is not None else ""
            root = _text(scope, self.raw, 20).strip().lower()
            if member and root in ("self", "static", "parent"):
                self._edge(owner, "calls_unresolved",
                           self._ext(root + "::" + member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "static", "base": root,
                            "member": member})
            return
        toks = [t for t in _php_names(node, self.raw)]
        # Split on `::` structure: [root, member] (middle dropped, coarse).
        if not toks:
            return
        if len(toks) == 1:
            self._edge(owner, "calls_unresolved", self._ext(toks[0]), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "function", "base": toks[0]})
            return
        root, member = toks[0], toks[-1]
        self._edge(owner, "calls_unresolved",
                   self._ext(root + "::" + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "static", "base": root,
                    "member": member})


_PHP_CONTAINER_FUNCS = frozenset({"app", "resolve"})


def _php_container_type(obj, raw: bytes) -> str:
    """`app(X::class)` / `resolve(X::class)` / `app()->make(X::class)`:
    the class X a Laravel container call returns; '' otherwise."""
    if obj.type == "member_call_expression":
        nm = obj.child_by_field_name("name")
        inner = obj.child_by_field_name("object")
        if nm is None or _text(nm, raw, 20) != "make" or inner is None \
                or inner.type != "function_call_expression":
            return ""
        fn = inner.child_by_field_name("function")
        if fn is None or _text(fn, raw, 20) != "app":
            return ""
    elif obj.type == "function_call_expression":
        fn = obj.child_by_field_name("function")
        if fn is None or _text(fn, raw, 20).lstrip("\\") not in _PHP_CONTAINER_FUNCS:
            return ""
    else:
        return ""
    args = obj.child_by_field_name("arguments")
    first = next((a for a in (args.children if args is not None else []) if a.type == "argument"), None)
    if first is None:
        return ""
    m = re.fullmatch(rb"\s*\\?([A-Za-z_][\w\\]*)::class\s*", raw[first.start_byte:first.end_byte])
    return m.group(1).decode().rsplit("\\", 1)[-1] if m else ""


def _cpp_dotted(node, raw: bytes) -> str:
    """Dotted path for C++ scope syntax (A::B::c). Template arguments,
    qualifiers and operators are dropped; unknown shapes yield ""."""
    parts: list[str] = []

    def _walk(n):
        if n.type in ("identifier", "type_identifier",
                      "namespace_identifier", "field_identifier"):
            t = _text(n, raw, 120).split("[")[0].split("<")[0].strip()
            if t:
                parts.append(t)
        elif n.type != "template_argument_list":  # `A<K, V>::m` is A.m
            for c in n.children:
                if c.type not in (".", "::", ":", "->", "~", "&", "*"):
                    _walk(c)

    _walk(node)
    return ".".join(p for p in parts if p)


_CPP_WRAPPERS = frozenset({"shared_ptr", "unique_ptr", "weak_ptr", "optional",
                           "reference_wrapper", "atomic"})


def _cpp_type_label(node, raw: bytes) -> str:
    """Bare type label of a C/C++ type node: `ns::circular_q<T>` ->
    `circular_q`, `std::shared_ptr<logger>` -> `logger` (smart pointers and
    wrappers unwrapped); '' for std containers and anything else."""
    t = node.type
    if t == "type_identifier":
        return _text(node, raw, 120)
    if t == "qualified_identifier":
        inner = node.child_by_field_name("name")
        scope = node.child_by_field_name("scope")
        if scope is not None and _text(scope, raw, 40) == "std" and inner is not None and not (
                inner.type == "template_type" and _text(inner.child_by_field_name("name"), raw, 40)
                in _CPP_WRAPPERS):
            return ""   # std containers / types
        return _cpp_type_label(inner, raw) if inner is not None else ""
    if t == "template_type":
        name = node.child_by_field_name("name")
        base = _text(name, raw, 120) if name is not None else ""
        if base in _CPP_WRAPPERS:
            args = node.child_by_field_name("arguments")
            for a in (args.children if args is not None else []):
                if a.type == "type_descriptor":
                    for g in a.children:
                        if g.type in ("type_identifier", "qualified_identifier", "template_type"):
                            return _cpp_type_label(g, raw)
            return ""
        return base
    return ""


_PREPROC_BLOCKS = frozenset({"preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif",
                             "preproc_elifdef"})


def _cpp_members(body):
    """Class-body members, `#if`/`#ifdef` blocks flattened (every branch:
    methods written per platform are all real definitions)."""
    for m in body.children:
        if m.type in _PREPROC_BLOCKS:
            yield from _cpp_members(m)
        else:
            yield m


def _cpp_fn_declarator(node):
    """The function_declarator of a C/C++ definition or declaration, also
    when the return type wraps it (`T* f()`, `T& f()`, `T&& f()`)."""
    for c in node.children:
        if c.type == "function_declarator":
            return c
        if c.type in ("pointer_declarator", "reference_declarator"):
            inner = _cpp_fn_declarator(c)
            if inner is not None:
                return inner
    # C grammar: `TEST_IMPL(name) { .. }` parses as a function whose type is
    # the macro and whose declarator is `(name)`: the function is `name`
    kids = node.children
    for a, b in zip(kids, kids[1:]):
        if (a.type == "type_identifier" and b.type == "parenthesized_declarator"
                and a.text and a.text.isupper() and node.type == "function_definition"
                and any(c.type == "identifier" for c in b.children)):
            return b
    return None


# a bare call in a macro body: not after `.`, `->`, `::`, `#` / `##` or a word
_MACRO_CALL = re.compile(rb"(?<![\w.>:#])((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)[ \t]*\(")
_C_COMMENT = re.compile(rb"/\*.*?\*/|//[^\n]*", re.S)
_C_KEYWORDS = frozenset({"if", "while", "for", "switch", "return", "sizeof", "do", "else",
                         "case", "defined", "alignof", "decltype", "typeof", "_Generic",
                         "static_assert", "_Static_assert", "__typeof__", "__attribute__",
                         "offsetof", "va_arg", "noexcept", "throw", "new", "delete"})


class _CppVisitor:
    """Two-pass C/C++ walker (serves `cpp` and `c`; C is a structural
    subset so one visitor covers both).

    Pass 1 declares: namespaces, classes (struct->class, Go doctrine;
    enum->coarse class with enumerator constants, Java doctrine),
    heritage (`base_class_clause` -> `inherits`), functions/methods incl.
    prototypes ({"declared": True} vs {"defined": True} on definitions;
    out-of-line `A::m` binds under class A), ctors (`<init>`, Java
    doctrine) / dtors (`<destroy>`), pure-virtual/static members,
    templates ({"template": True}, never instantiated), macros
    (`#define`, object- and function-like) as constants (never call
    targets — macro calls are textual), `using` aliases as types,
    includes (system `<>` vs local `""`), `using namespace` as external
    imports. Pass 2 walks every collected body once (named nested
    definitions never descended: no double attribution, Java doctrine).

    Calls: same-file locals resolve to `calls`; `Type::member`,
    selector and template calls stay `calls_unresolved` with shape attrs
    for cpp_resolve; `new T` and `T(...)` construction (direct
    argument_list under init_declarator only) shape `instantiates`;
    braced list-init, `this->` (shaped `this` when the grammar exposes
    it, else guarded-skip), operator overloads, function pointers,
    overload sets (single-only binding), virtual dispatch, ADL, macro
    expansion and inactive `#if` branches (over-approximated) are
    documented boundaries — never guessed. Cross-TU identity is NOT
    merged: same-name free functions unify only through a shared
    declaration (same qualified name); a same-file prototype wins lookup
    over a cross-file definition (correct C++ lookup, separate node).
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.lang = ext.language_id
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = f"ts-{self.lang}"
        self.funcs: dict[str, str] = {}     # free-function name -> nid
        self.classes: dict[str, list] = {}  # bare class label -> [nids]
        self.methods: dict[tuple, str] = {}  # (owner_qn, name) -> nid
        self.bodies: list[tuple] = []       # (owner_nid, body_node)

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, self.lang, kind, qname,
                                  "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), self.lang, "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        self._stmts(root.children, (), self.mid)
        for owner, body in self.bodies:
            self._calls_in(body, owner)
        # Namespace-scope variable initializers run at load: attribute
        # their calls to the module (Lua chunk doctrine). Class-member
        # initializers are skipped (owner-ambiguous, Java parity).
        for stmt in root.children:
            self._top_inits(stmt, ())

    _GTEST = ("TEST", "TEST_F", "TEST_P", "TYPED_TEST", "TYPED_TEST_P", "FRIEND_TEST")
    _CATCH = ("TEST_CASE", "SCENARIO", "TEST_CASE_METHOD", "TEMPLATE_TEST_CASE",
              "TEST_CASE_TEMPLATE", "TEST_SUITE")

    def _stmts(self, stmts, ns: tuple, owner: str):
        """Namespace-scope statements; GoogleTest `TEST(Suite, Name) {..}` and
        Catch2/doctest `TEST_CASE("name") {..}` become test nodes (their
        bodies' calls attributed to the test) instead of a function named
        `TEST` or nothing at all."""
        stmts = list(stmts)
        skip = -1
        for i, stmt in enumerate(stmts):
            if i == skip:
                continue
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if self._test_macro(stmt, nxt, ns, owner):
                if stmt.type == "expression_statement":
                    skip = i + 1
                continue
            self._top(stmt, ns, owner)

    def _test_macro(self, node, nxt, ns: tuple, owner: str) -> bool:
        if node.type == "function_definition":
            decl = node.child_by_field_name("declarator")
            body = node.child_by_field_name("body")
            if decl is None or decl.type != "function_declarator" or body is None:
                return False
            fn = decl.child_by_field_name("declarator")
            macro = _text(fn, self.raw, 40) if fn is not None else ""
            if macro not in self._GTEST:
                return False
            params = decl.child_by_field_name("parameters")
            parts = [_text(c, self.raw, 120).strip() for c in (params.children if params else [])
                     if c.is_named]
            if len(parts) < 2:
                return False
            name, at = f"{parts[0]}.{parts[1]}", node
        elif node.type == "expression_statement" and nxt is not None                 and nxt.type == "compound_statement":
            call = node.children[0] if node.children else None
            if call is None or call.type != "call_expression":
                return False
            fn = call.child_by_field_name("function")
            if fn is None or _text(fn, self.raw, 40) not in self._CATCH:
                return False
            args = call.child_by_field_name("arguments")
            lit = next((c for c in (args.children if args else [])
                        if c.type in ("string_literal", "raw_string_literal")), None)
            if lit is None:
                return False
            name = _text(lit, self.raw, 120).strip('"').replace(".", "_").strip() or "test"
            body, at = nxt, nxt
        else:
            return False
        nid = self._sym("test", self._qn(ns, name), at)
        self._defines(owner, nid, at)
        self.bodies.append((nid, body))
        return True

    def _qn(self, ns: tuple, name: str) -> str:
        base = self.mod if not ns else ".".join(ns)
        return f"{base}.{name}" if name else base

    def _top(self, node, ns: tuple, owner: str):
        t = node.type
        if t == "namespace_definition":
            name = ""
            for c in node.children:
                if c.type == "namespace_identifier":
                    name = _text(c, self.raw, 120)
                    break
            for c in node.children:
                if c.type == "declaration_list":
                    # Anonymous namespaces share the enclosing scope
                    # (documented: collisions stay ambiguous downstream).
                    self._stmts(c.children, ns + ((name,) if name else ()), owner)
        elif t in ("preproc_if", "preproc_ifdef", "preproc_else",
                   "preproc_elif", "preproc_elifdef"):
            # Include guards and platform branches wrap whole files; every
            # branch is declared (over-approximation, documented above).
            for c in node.children:
                self._top(c, ns, owner)
        elif t == "ERROR":
            self._salvage(node, ns, owner)
        elif t == "linkage_specification":  # extern "C" { ... } / extern "C" decl
            for c in node.children:
                if c.type == "declaration_list":
                    for m in c.children:
                        self._top(m, ns, owner)
                elif c.type not in ("extern", "string_literal"):
                    self._top(c, ns, owner)
        elif t in ("class_specifier", "struct_specifier",
                   "enum_specifier"):
            self._class(node, ns, owner)
        elif t == "function_definition":
            self._function_def(node, ns, owner, None)
        elif t in ("declaration", "field_declaration"):
            self._proto_or_var(node, ns, owner)
        elif t == "template_declaration":
            self._template(node, ns, owner)
        elif t == "preproc_include":
            self._include(node)
        elif t in ("preproc_def", "preproc_function_def"):
            name = ""
            for c in node.children:
                if c.type == "identifier":
                    name = _text(c, self.raw, 120)
                    break
            if name:
                nid = self._sym("constant", self._qn(ns, name), node)
                self._defines(owner, nid, node)
                if t == "preproc_function_def":
                    self._macro_calls(node, nid)
        elif t == "using_declaration":
            spec = ""
            for c in node.children:
                if c.type in ("identifier", "qualified_identifier",
                              "namespace_identifier"):
                    spec = _cpp_dotted(c, self.raw) or _text(
                        c, self.raw, 120)
            if spec:
                self._edge(self.mid, "imports", self._ext(spec), node,
                           "structural", "deterministic", 1.0,
                           {"specifier": spec, "using": True,
                            "dynamic": False})
        elif t == "alias_declaration":
            name = ""
            for c in node.children:
                if c.type == "type_identifier":
                    name = _text(c, self.raw, 120)
                    break
            if name and "=" not in (name or ""):
                nid = self._sym("type", self._qn(ns, name), node)
                self._defines(owner, nid, node)

    def _salvage(self, node, ns: tuple, owner: str):
        """ERROR region at namespace scope (macro-heavy code such as
        `GUARDED_BY(mu_)` can swallow a whole `namespace x {`): the
        `namespace` / name / `{` tokens survive as loose children, so the
        scope is rebuilt from them and every complete declaration inside
        is dispatched normally. A `}` closes the innermost one."""
        stack = list(ns)
        depth: list[bool] = []  # per opened brace: did it open a namespace
        pending: str | None = None
        for c in node.children:
            ct = c.type
            if ct == "namespace":
                pending = ""
            elif pending is not None and ct in ("identifier",
                                                "namespace_identifier"):
                pending = _text(c, self.raw, 120)
            elif ct == "{":
                if pending is not None:
                    if pending:
                        stack.append(pending)
                    depth.append(bool(pending))
                    pending = None
                else:
                    depth.append(False)
            elif ct == "}":
                if depth and depth.pop():
                    stack.pop()
            else:
                pending = None
                self._top(c, tuple(stack), owner)

    def _template(self, node, ns: tuple, owner: str):
        for c in node.children:
            if c.type == "function_definition":
                self._function_def(node, ns, owner, None, template=node,
                                   inner=c)
                return
            if c.type in ("class_specifier", "struct_specifier"):
                self._class(c, ns, owner, template=True)
                return

    def _class(self, node, ns: tuple, owner: str, template=False):
        name = ""
        for c in node.children:
            if c.type == "type_identifier" and not name:
                name = _text(c, self.raw, 120)
            elif c.type == "template_type" and not name:
                # a specialization (`struct formatter<R, Char, ..> { }`):
                # its members belong to the template's name
                for g in c.children:
                    if g.type == "type_identifier":
                        name = _text(g, self.raw, 120)
                        break
        if not name:
            return
        qn = self._qn(ns, name)
        attrs = {"template": True} if template else {}
        nid = self._sym("class", qn, node, attrs)
        self.classes.setdefault(name, []).append(nid)
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "base_class_clause":
                for g in c.children:
                    if g.type in ("type_identifier",
                                  "qualified_identifier",
                                  "template_type"):
                        nm = _cpp_dotted(g, self.raw)
                        if nm:
                            self._edge(nid, "inherits", self._ext(nm),
                                       c, "resolved", "resolved", 0.9,
                                       {"superclass": nm})
            elif c.type == "enumerator_list":
                for g in c.children:
                    if g.type == "enumerator":
                        en = ""
                        for h in g.children:
                            if h.type == "identifier":
                                en = _text(h, self.raw, 120)
                                break
                        if en:
                            eid = self._sym("constant", f"{qn}.{en}",
                                            g)
                            self._defines(nid, eid, g)
            elif c.type == "field_declaration_list":
                for m in _cpp_members(c):
                    mt = m.type
                    if mt == "function_definition":
                        self._function_def(m, ns, nid, qn)
                    elif mt == "template_declaration":
                        # member templates (`template <class T> void push_back(..) {}`)
                        for g in m.children:
                            if g.type == "function_definition":
                                self._function_def(m, ns, nid, qn, template=m, inner=g)
                                break
                            if g.type in ("declaration", "field_declaration"):
                                self._member(g, ns, nid, qn)
                                break
                    elif mt in ("declaration", "field_declaration"):
                        self._member(m, ns, nid, qn)
                        if mt == "field_declaration" and _cpp_fn_declarator(m) is None:
                            self._field(m, nid)
                            if any(g.type == "=" for g in m.children):
                                self.bodies.append((nid, m))      # `int field = f();` runs when an instance is built

    def _field(self, node, cls_id: str):
        """`circular_q<T> q_;` / `std::shared_ptr<logger> l_;`: the field's
        type label (template args and smart pointers unwrapped) recorded in
        the class's `fields`, so `q_.pop_front()` binds through it."""
        ty = next((c for c in node.children if c.type in (
            "type_identifier", "template_type", "qualified_identifier")), None)
        label = _cpp_type_label(ty, self.raw) if ty is not None else ""
        if not label:
            return
        names = []
        for c in node.children:
            d = c
            while d is not None and d.type in ("pointer_declarator", "reference_declarator"):
                d = next((g for g in d.children if g.type in (
                    "field_identifier", "pointer_declarator", "reference_declarator")), None)
            if d is not None and d.type == "field_identifier":
                names.append(_text(d, self.raw, 120))
        cls = next((n for n in reversed(self.res.nodes) if n.id == cls_id), None)
        if names and cls is not None:
            f = cls.attributes.setdefault("fields", {})
            for nm in names:
                f.setdefault(nm, label)

    def _declarator(self, node):
        """(kind, scope_dotted|None, name) for a function_declarator.

        kind: plain (free/namespace fn or in-class member), qualified
        (A::m), dtor, unknown (operators, function pointers: guarded).
        """
        for c in node.children:
            if c.type == "identifier":
                return "plain", None, _text(c, self.raw, 120)
            if c.type == "field_identifier":
                return "plain", None, _text(c, self.raw, 120)
            if c.type == "qualified_identifier":
                full = _cpp_dotted(c, self.raw)
                if "." in full:
                    scope, _, nm = full.rpartition(".")
                    if nm:
                        return "qualified", scope, nm
                return "unknown", None, ""
            if c.type == "destructor_name":
                return "dtor", None, ""
            if c.type == "operator_name":
                # `operator()` / `operator==`: kept so their bodies' calls
                # have an owner (never a bare-call target: not an identifier)
                return "plain", None, "".join(_text(c, self.raw, 40).split())
            if c.type == "template_function":
                for g in c.children:
                    if g.type == "identifier":
                        return "plain", None, _text(g, self.raw, 120)
                return "unknown", None, ""
        return "unknown", None, ""

    def _is_ctor(self, name: str, owner_qn: str | None) -> bool:
        return bool(owner_qn) and name == owner_qn.split(".")[-1]

    def _macro_calls(self, node, nid: str):
        """Free-function calls written in a function-like macro body
        (`#define PROCESS(h) do { q = uv__queue_head(&e); } while (0)`):
        owned by the macro, bare names only (never members, the macro's
        own parameters or keywords), bound later like any bare call."""
        params, body = set(), None
        for c in node.children:
            if c.type == "preproc_params":
                params = {_text(g, self.raw, 120) for g in c.children if g.type == "identifier"}
                body = c
        if body is None:
            return
        # the whole body (a `/* .. */` inside it ends preproc_arg early),
        # comments blanked in place so line counting stays exact
        end = body.end_byte                 # the grammar may stop at a comment:
        while True:                         # follow the `\` continuations
            nl = self.raw.find(b"\n", end)
            if nl < 0:
                end = len(self.raw)
                break
            if not self.raw[end:nl].rstrip(b"\r \t").endswith(b"\\"):
                end = nl
                break
            end = nl + 1
        text = _C_COMMENT.sub(lambda m: re.sub(rb"[^\n]", b" ", m.group(0)),
                              self.raw[body.end_byte:end])
        line0 = body.end_point[0] + 1
        for m in _MACRO_CALL.finditer(text):
            full = m.group(1).decode("utf-8", "ignore")
            scope, _, name = full.rpartition("::")
            if name in params or name in _C_KEYWORDS or name.isupper() or scope in params:
                continue
            ln = line0 + text.count(b"\n", 0, m.start(1))
            if scope:   # `fmt::detail::ignore_unused(..)`
                dotted = scope.replace("::", ".")
                tgt = self._ext(dotted + "." + name)
                attrs = {"callee_kind": "qualified", "base": dotted, "member": name}
            else:
                tgt = self._ext(name)
                attrs = {"callee_kind": "function", "base": name}
            attrs["in_macro"] = True
            self.res.edges.append(Edge(
                canonical_edge_id(nid, "calls_unresolved", tgt, self.rel),
                nid, tgt, "calls_unresolved", "structural", "deterministic",
                1.0, [self.ev.id], self.method, UNIV_TS_VERSION, file_path=self.rel,
                start_line=ln, attributes=attrs))

    def _function_def(self, node, ns: tuple, owner: str,
                      owner_qn: str | None, template=None, inner=None):
        node = inner or node
        decl = None
        body = None
        for c in node.children:
            if c.type == "compound_statement":
                body = c
        inits = next((c for c in node.children if c.type == "field_initializer_list"), None)
        decl = _cpp_fn_declarator(node)
        if decl is None:
            return
        kind, scope, name = self._declarator(decl)
        if kind == "dtor":
            name = "<destroy>"
        elif kind == "unknown" or not name:
            return
        if kind == "qualified" and scope:
            cls = scope.split(".")[-1]
            cands = self.classes.get(cls, [])
            qn = None
            par = owner
            if len(cands) == 1:
                cls_qn = next((n.qualified_name for n in self.res.nodes
                               if n.id == cands[0]), None)
                # the method lives UNDER the class: `a.A` + `.m`, never the
                # class identity itself (that collided A::m with class A)
                qn = f"{cls_qn}.{name}" if cls_qn else None
                par = cands[0]
            if qn is None:
                # Keep the whole scope: `SkipList<K,C>::Iterator::m` is not
                # the namespace-level `Iterator::m` (distinct classes).
                qn = self._qn(ns, f"{scope}.{name}")
            if self._is_ctor(name, qn.rpartition(".")[0]):
                name = "<init>"
                qn = f"{qn.rpartition('.')[0]}.<init>"
            key = (qn.rpartition(".")[0], name)
            nid = self.methods.get(key)
            if nid is None:
                attrs = {"defined": True}
                if template is not None:
                    attrs["template"] = True
                nid = self._sym("method", qn, node, attrs)
                self.methods[key] = nid
                self._defines(par, nid, node)
            elif body is not None:
                self._mark_defined(nid, node)  # out-of-line `A::m() {..}`
            if body is not None:
                self.bodies.append((nid, body))
                if inits is not None:      # `C() : field(f())`
                    self.bodies.append((nid, inits))
            return
        # plain: free function, or method when inside a class scope.
        if owner_qn:
            if self._is_ctor(name, owner_qn):
                label, qn = "<init>", f"{owner_qn}.<init>"
            else:
                label, qn = name, f"{owner_qn}.{name}"
            key = (owner_qn, label)
            nid = self.methods.get(key)
            if nid is None:
                attrs = {"defined": True}
                if template is not None:
                    attrs["template"] = True
                nid = self._sym("method", qn, node, attrs)
                self.methods[key] = nid
                self._defines(owner, nid, node)
            elif body is not None:
                self._mark_defined(nid, node)
            if body is not None:
                self.bodies.append((nid, body))
                if inits is not None:      # `C() : field(f())`
                    self.bodies.append((nid, inits))
        else:
            qn = self._qn(ns, name)
            if name in self.funcs:
                nid = self.funcs[name]
                self._mark_defined(nid, node)
            else:
                attrs = {"defined": True}
                if template is not None:
                    attrs["template"] = True
                nid = self._sym("function", qn, node, attrs)
                self.funcs[name] = nid
                self._defines(owner, nid, node)
            if body is not None:
                self.bodies.append((nid, body))
                if inits is not None:      # `C() : field(f())`
                    self.bodies.append((nid, inits))

    def _mark_defined(self, nid: str, node):
        """A prototype seen first owns the node; its definition is where the
        symbol lives (location and `defined`), so navigation lands on code."""
        for n in self.res.nodes:
            if n.id == nid:
                if not (n.attributes or {}).get("defined"):
                    n.location = _loc(node)
                    n.attributes = dict(n.attributes or {}, defined=True)
                    n.attributes.pop("declared", None)
                return

    def _proto_or_var(self, node, ns: tuple, owner: str):
        decl = _cpp_fn_declarator(node)
        if decl is None:
            return
        kind, scope, name = self._declarator(decl)
        if kind == "unknown" or not name and kind != "dtor":
            return
        if kind == "dtor":
            name = "<destroy>"
        owner_qn = None
        if owner != self.mid:
            for n in self.res.nodes:
                if n.id == owner:
                    owner_qn = n.qualified_name
                    break
        if owner_qn and self._is_ctor(name, owner_qn):
            name = "<init>"
        qn = f"{owner_qn}.{name}" if owner_qn else self._qn(ns, name)
        key = (owner_qn or ".".join(ns) or self.mod, name)
        nid = self.methods.get(key)
        if nid is None:
            nid = self._sym("method" if owner_qn else "function", qn,
                            node, {"declared": True})
            if owner_qn:
                self.methods[key] = nid
            elif name not in self.funcs:
                self.funcs[name] = nid
            self._defines(owner, nid, node)

    def _member(self, node, ns: tuple, owner: str, owner_qn: str):
        decl = _cpp_fn_declarator(node)
        if decl is None:
            return  # fields skipped (Java parity, documented)
        kind, scope, name = self._declarator(decl)
        if kind == "unknown":
            return
        if kind == "dtor":
            name = "<destroy>"
        elif self._is_ctor(name, owner_qn):
            name = "<init>"
        qn = f"{owner_qn}.{name}"
        key = (owner_qn, name)
        nid = self.methods.get(key)
        if nid is None:
            nid = self._sym("method", qn, node, {"declared": True})
            self.methods[key] = nid
            self._defines(owner, nid, node)

    def _include(self, node):
        spec = ""
        system = False
        for c in node.children:
            if c.type == "system_lib_string":
                spec = _text(c, self.raw, 300).strip()
                if spec.startswith("<") and spec.endswith(">"):
                    spec = spec[1:-1]
                system = True
            elif c.type == "string_literal":
                spec = _strip_quotes(_text(c, self.raw, 300))
        if not spec:
            return
        self._edge(self.mid, "imports", self._ext(spec), node,
                   "structural", "deterministic", 1.0,
                   {"specifier": spec, "system": system,
                    "dynamic": False})

    def _top_inits(self, node, ns: tuple):
        if node.type not in ("declaration", "field_declaration"):
            return
        has_declarator = any(c.type == "function_declarator"
                             for c in node.children)
        if has_declarator:
            return
        self._calls_in(node, self.mid)

    # ------------------------------------------------------------ pass 2
    def _calls_in(self, node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call_expression":
                self._call(n, owner)
            elif t == "new_expression":
                nm = ""
                for c in n.children:
                    if c.type in ("type_identifier", "identifier"):
                        nm = _text(c, self.raw, 120)
                        break
                if nm:
                    self._edge(owner, "instantiates", self._ext(nm), n,
                               "resolved", "resolved", 0.9,
                               {"created": nm})
            elif t == "declaration":
                self._construction(n, owner)
            if t in ("class_specifier", "struct_specifier",
                     "enum_specifier", "union_specifier",
                     "namespace_definition"):
                continue  # named scopes own their bodies (no doubles)
            stack.extend(n.children)

    def _construction(self, node, owner: str):
        init = None
        for c in node.children:
            if c.type == "init_declarator":
                init = c
                break
        if init is None:
            return
        if not any(g.type == "argument_list" for g in init.children):
            return
        typ = ""
        for c in node.children:
            if c.type in ("type_identifier", "identifier"):
                typ = _text(c, self.raw, 120)
                break
        if typ:
            self._edge(owner, "instantiates", self._ext(typ), node,
                       "resolved", "resolved", 0.9, {"created": typ})

    def _call(self, node, owner: str):
        fn = None
        for c in node.children:
            if c.type in ("identifier", "field_expression",
                          "qualified_identifier", "template_function",
                          "destructor_name"):
                fn = c
                break
        if fn is None:
            return
        arity = 0
        for c in node.children:
            if c.type == "argument_list":
                arity = sum(1 for g in c.children
                            if g.is_named and g.type not in ("(", ")",
                                                             ","))
        if fn.type == "identifier":
            base = _text(fn, self.raw, 120)
            tgt = self.funcs.get(base)
            if tgt is not None:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9, {"arity": arity})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            return
        if fn.type == "template_function":
            base = ""
            for g in fn.children:
                if g.type == "identifier":
                    base = _text(g, self.raw, 120)
                    break
            if not base:
                return
            tgt = self.funcs.get(base)
            if tgt is not None:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9,
                           {"arity": arity, "template_call": True})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity, "template_call": True})
            return
        if fn.type == "field_expression":
            operand = ""
            member = ""
            is_this = False
            for c in fn.children:
                if c.type == "identifier" and not operand:
                    operand = _text(c, self.raw, 120)
                elif c.type == "this" and not operand:
                    is_this = True
                elif c.type == "field_identifier" and not member:
                    member = _text(c, self.raw, 120)
                elif c.type == "template_method" and not member:
                    # `d.m<int>()`: the member name sits inside the template_method node
                    member = next((_text(g, self.raw, 120) for g in c.children if g.type == "field_identifier"), "")
            if not member:
                return
            if is_this or operand == "this":
                self._edge(owner, "calls_unresolved",
                           self._ext(member), node, "structural",
                           "deterministic", 1.0,
                           {"callee_kind": "this", "member": member,
                            "arity": arity})
                return
            if not operand:
                return  # guarded: unshaped receiver, never guessed
            self._edge(owner, "calls_unresolved",
                       self._ext(operand + "." + member), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "selector", "base": operand,
                        "member": member, "arity": arity})
            return
        if fn.type == "qualified_identifier":
            full = _cpp_dotted(fn, self.raw)
            if not full or "." not in full:
                return
            scope, _, member = full.rpartition(".")
            self._edge(owner, "calls_unresolved",
                       self._ext(scope + "." + member), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "qualified", "base": scope,
                        "member": member, "arity": arity})
            return


class _KotlinVisitor:
    """Two-pass Kotlin walker.

    Pass 1 declares: package clause (module identity, Java doctrine),
    imports (single + `.*` glob flag; `as` aliases are NOT rebound —
    the plain name stays unbound, documented miss, never misbound),
    classes (data/interface/object-singleton/enum-class + entries as
    constants; `open`/`abstract`/`sealed` recorded, not interpreted),
    heritage (`delegation_specifiers`: constructor call -> `inherits` +
    `instantiates`, bare type -> `implements`), functions (top-level,
    member, local with `local_function` attr, extension with
    `extension_receiver` attr), properties as variables (initializers
    walked under the property), companion objects (class + `companion`
    attr, methods under it), primary-constructor params skipped
    (documented), secondary constructors/`init` blocks skipped
    (documented). Pass 2 walks every collected body once (named nested
    declarations never descended: no double attribution).

    Calls: file-local functions resolve to `calls`; file-local classes
    called bare (`Mover("x")`) resolve to `instantiates` (a same-named
    local fun wins: redeclaration would not compile); navigation
    `a.b()` shapes (a, b) with `this` kind for `this_expression`;
    chained results shape `complex`; everything else stays
    `calls_unresolved` for kotlin_resolve (same-package, import-bound,
    companion, `this`). Coroutines, delegation `by`, reflection,
    data-class generated members and generic specialization are
    documented non-goals.

    Depth honesty: the 1.1.0 grammar sets a PHANTOM `has_error` bit on
    `class_body` (measured: zero ERROR/missing nodes, complete
    subtrees; broken code still yields real ERROR nodes). The engine
    trusts error/missing COUNTS, never the flag.
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-kotlin"
        self.pkg = ""
        self.funcs: dict[str, str] = {}    # file-local fun name -> nid
        self.classes: dict[str, str] = {}  # file-local class label -> nid
        self.bodies: list[tuple] = []

    def _mod(self) -> str:
        return self.pkg or self.mod

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "kotlin", kind, qname,
                                  "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), "kotlin", "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        saw_package = False
        for stmt in root.children:
            t = stmt.type
            if t == "package_header":
                saw_package = True
                for c in stmt.children:
                    if c.type == "qualified_identifier":
                        self.pkg = _dotted(c, self.raw)
                        break
                if self.pkg:
                    for n in self.res.nodes:
                        if n.id == self.mid:
                            n.label = self.pkg
                            n.qualified_name = self.pkg
            elif t == "import":
                self._import(stmt)
            else:
                self._decl(stmt, self.mid, self._mod(), top=True)
        if not saw_package:
            # Default package: the dir-based module identity would
            # masquerade as a package path (Java strips it to the parent
            # dir instead). Empty is the honest default-package marker.
            for n in self.res.nodes:
                if n.id == self.mid:
                    n.label = ""
                    n.qualified_name = ""
        for owner, body in self.bodies:
            self._calls_in(body, owner)

    def _import(self, node):
        path = ""
        for c in node.children:
            if c.type == "qualified_identifier":
                path = _dotted(c, self.raw)
        if not path:
            return
        # newer grammars put `.*` beside the qualified_identifier, not in it
        glob = path.endswith(".*") or path.endswith("*") or \
            any(c.type in ("*", "wildcard_import") for c in node.children)
        if glob:
            path = path.rstrip("*").rstrip(".")
        self._edge(self.mid, "imports", self._ext(path), node,
                   "structural", "deterministic", 1.0,
                   {"specifier": path, "glob_import": glob,
                    "dynamic": False})

    def _decl(self, node, owner: str, qn: str, top=False):
        t = node.type
        if t == "class_declaration":
            self._class(node, owner, qn)
        elif t == "object_declaration":
            self._object(node, owner, qn)
        elif t == "function_declaration":
            self._function(node, owner, qn, top=top)
        elif t == "property_declaration":
            self._property(node, owner, qn)

    def _class_kind(self, node) -> tuple[str, dict]:
        first = ""
        for c in node.children:
            if c.type in ("class", "interface"):
                first = c.type
                break
        attrs: dict = {}
        for c in node.children:
            if c.type == "modifiers":
                toks = _text(c, self.raw, 60).split()
                if "data" in toks:
                    attrs["data"] = True
                if "enum" in toks:
                    attrs["enum"] = True
                if "open" in toks:
                    attrs["open"] = True
                if "abstract" in toks:
                    attrs["abstract"] = True
                if "sealed" in toks:
                    attrs["sealed"] = True
        if first == "interface":
            return "interface", attrs
        return "class", attrs

    def _class(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        kind, attrs = self._class_kind(node)
        full = f"{qn}.{name}"
        nid = self._sym(kind, full, node, attrs)
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "delegation_specifiers":
                self._heritage(c, nid)
            elif c.type in ("class_body", "enum_class_body"):
                self._members(c, nid, full)
            elif c.type == "primary_constructor" and any(
                    g.type == "=" for p_ in c.children for q_ in p_.children for g in q_.children):
                # `class C(val d: Dep = Dep(f()))`: a default value runs when an instance is built
                self.bodies.append((nid, c))

    def _object(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node, {"singleton": True})
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "delegation_specifiers":
                # `object SchemaUtils : SchemaUtilityApi()`
                self._heritage(c, nid)
            elif c.type == "class_body":
                self._members(c, nid, full)

    def _members(self, body, owner: str, qn: str):
        kids = []
        for m in body.children:
            # One-line `interface S { fun send() }` wraps the body-less member
            # in an ERROR node (grammar quirk): salvage what is inside.
            kids.extend(m.children if m.type == "ERROR" else [m])
        for m in kids:
            t = m.type
            if t == "function_declaration":
                self._function(m, owner, qn)
            elif t == "property_declaration":
                self._property(m, owner, qn)
            elif t == "class_declaration":
                self._class(m, owner, qn)
            elif t == "object_declaration":
                self._object(m, owner, qn)
            elif t == "companion_object":
                self._companion(m, owner, qn)
            elif t in ("anonymous_initializer", "secondary_constructor"):
                # `init { f() }` / `constructor(x: Int) : this(g(x))`: they run when an instance is built
                self.bodies.append((owner, m))
            elif t == "enum_entry":
                en = ""
                for g in m.children:
                    if g.type == "identifier":
                        en = _text(g, self.raw, 120)
                        break
                if en:
                    eid = self._sym("constant", f"{qn}.{en}", m)
                    self._defines(owner, eid, m)
                    self.bodies.append((owner, m))      # `A(f())`: the arguments run when the class loads

    def _companion(self, node, owner: str, qn: str):
        name = "Companion"
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node, {"companion": True})
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "class_body":
                self._members(c, nid, full)

    def _function(self, node, owner: str, qn: str, top=False):
        name = ""
        receiver = ""
        dot_seen_for: str | None = None
        for c in node.children:
            if c.type == "user_type" and not name:
                # `Column<T>.withDefinition`: the receiver type, without its
                # type arguments (dotting them gave `Column.T`)
                dot_seen_for = ".".join(_text(g, self.raw, 120) for g in c.children
                                        if g.type in ("type_identifier", "simple_identifier",
                                                      "identifier")) or _dotted(c, self.raw)
            elif c.type == "." and dot_seen_for and not name:
                receiver = dot_seen_for
            elif c.type == "identifier" and not name:
                name = _text(c, self.raw, 120)
        if not name:
            return
        has_body = any(c.type == "function_body" for c in node.children)
        attrs: dict = {}
        if receiver:
            attrs["extension_receiver"] = receiver
        if not has_body:
            attrs["declared"] = True
        if not top and owner == self.mid:
            attrs["local_function"] = True
        full = f"{qn}.{name}"
        # Top-level module members are functions; class/companion members
        # and nested locals are methods (locals carry the attr, C# parity).
        kind = "function" if (top and owner == self.mid) else "method"
        nid = self._sym(kind, full, node, attrs)
        if top and owner == self.mid and not receiver:
            self.funcs[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "function_body":
                self.bodies.append((nid, c))
            elif c.type == "function_value_parameters" and any(g.type == "=" for g in c.children):
                # `fun f(x: Int = g())`: a default value runs when the function is called
                self.bodies.append((nid, c))

    def _property(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "variable_declaration":
                for g in c.children:
                    if g.type == "identifier" and not name:
                        name = _text(g, self.raw, 120)
                        break
        # value = the last significant child (initializer expression)
        kids = [c for c in node.children
                if c.type not in ("val", "var", "variable_declaration",
                                  "=", ":", ";")]
        # No initializer (`abstract val x: Int`) used to leave `value`
        # unbound and abort the whole file's extraction.
        value = kids[-1] if kids else None
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("variable", full, node)
        self._defines(owner, nid, node)
        if value is not None:
            self.bodies.append((nid, value))

    def _heritage(self, node, owner: str):
        for spec in node.children:
            if spec.type != "delegation_specifier":
                continue
            ctor = None
            plain = None
            for c in spec.children:
                if c.type == "constructor_invocation":
                    ctor = c
                elif c.type == "user_type" and plain is None:
                    plain = c
            if ctor is not None:
                nm = ""
                for c in ctor.children:
                    if c.type == "user_type":
                        nm = _dotted(c, self.raw).split("<")[0]
                        break
                if nm:
                    short = nm.split(".")[-1]
                    self._edge(owner, "inherits", self._ext(nm), spec,
                               "resolved", "resolved", 0.9,
                               {"superclass": short})
                    self._edge(owner, "instantiates",
                               self._ext(nm), spec, "resolved",
                               "resolved", 0.9, {"created": short})
            elif plain is not None:
                nm = _dotted(plain, self.raw).split("<")[0]
                if nm:
                    self._edge(owner, "implements", self._ext(nm), spec,
                               "resolved", "resolved", 0.9,
                               {"interface": nm.split(".")[-1]})

    # ------------------------------------------------------------ pass 2
    def _calls_in(self, node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call_expression":
                self._call(n, owner)
            elif t == "binary_expression":
                shape = self._generic_call_shape(n)
                if shape is not None:
                    self._call(n, owner, *shape)
            if t == "function_declaration" and n is not node:
                # Nested local fun: own symbol under the enclosing
                # function (C# local_function parity), body walked
                # with it; the outer walk skips the subtree.
                qn = ""
                for m in self.res.nodes:
                    if m.id == owner:
                        qn = m.qualified_name or ""
                        break
                self._function(n, owner, qn or self._mod())
                continue
            if t in ("class_declaration", "object_declaration",
                     "companion_object"):
                continue  # named scopes own their bodies (no doubles)
            stack.extend(n.children)

    @staticmethod
    def _generic_call_shape(n):
        """tree-sitter-kotlin reads `f<Int>(x)` / `o.f<Int>(x)` (one argument) as `(f < Int) > (x)`.
        `(a < b) > (c)` is not code anyone writes, so the shape is a call: (callee node, arity)."""
        k = n.children
        if len(k) != 3 or k[1].type != ">" or k[2].type != "parenthesized_expression":
            return None
        inner = k[0]
        if inner.type != "binary_expression" or len(inner.children) != 3 or inner.children[1].type != "<":
            return None
        callee, targ = inner.children[0], inner.children[2]
        if callee.type not in ("identifier", "navigation_expression") or targ.type not in (
                "identifier", "navigation_expression", "user_type"):
            return None
        return callee, (1 if any(g.is_named for g in k[2].children) else 0)

    def _call(self, node, owner: str, _fn=None, _arity=None):
        fn = _fn
        args = None
        for c in (node.children if _fn is None else ()):
            if c.type in ("identifier", "navigation_expression") and \
                    fn is None:
                fn = c
            elif c.type == "unary_expression" and fn is None:
                # tree-sitter-kotlin binds `!` tighter than the call: `!isFor()` parses as
                # call_expression(unary_expression(!, isFor), args). The callee is what `!` negates.
                inner = [g for g in c.children if g.is_named]
                if inner and inner[-1].type in ("identifier", "navigation_expression"):
                    fn = inner[-1]
            elif c.type == "value_arguments":
                args = c
        if fn is None:
            return
        arity = 0
        if args is not None:
            arity = sum(1 for g in args.children
                        if g.type == "value_argument")
        if _arity is not None:
            arity = _arity
        if fn.type == "identifier":
            base = _text(fn, self.raw, 120)
            if not base:
                return
            if base in self.funcs:
                self._edge(owner, "calls", self.funcs[base], node,
                           "resolved", "resolved", 0.9, {"arity": arity})
            elif base in self.classes:
                self._edge(owner, "instantiates", self._ext(base), node,
                           "resolved", "resolved", 0.9,
                           {"created": base})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            return
        # navigation_expression: operand.member (possibly chained)
        kids = [c for c in fn.children if c.is_named or
                c.type in (".", "?.")]
        if len(kids) < 3:
            return
        first, member = kids[0], kids[-1]
        if member.type not in ("identifier",):
            return
        mname = _text(member, self.raw, 120)
        if not mname:
            return
        if first.type == "unary_expression":
            # `!b.isFor()` / `-b.abs()` parse as `(!b).isFor()`: the receiver is what the operator is applied to
            inner = [g for g in first.children if g.is_named]
            if inner:
                first = inner[-1]
        if first.type == "this_expression":
            self._edge(owner, "calls_unresolved", self._ext(mname),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "this", "member": mname,
                        "arity": arity})
            return
        if first.type == "identifier":
            base = _text(first, self.raw, 120)
            if not base:
                return
            self._edge(owner, "calls_unresolved",
                       self._ext(base + "." + mname), node,
                       "structural", "deterministic", 1.0,
                       {"callee_kind": "selector", "base": base,
                        "member": mname, "arity": arity})
            return
        self._edge(owner, "calls_unresolved", self._ext(mname), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "complex", "member": mname,
                    "arity": arity})


class _LuaVisitor:
    """Two-pass Lua walker.

    Pass 1 declares: `require("a.b.c")` specs (paren-less too) with the
    assigned local as alias; module tables (`local M = {}` + chunk
    `return M` marks the exported table); `function M.f` / `M:new` /
    `local function f` / `M.f = function` forms (colon = `self`
    method); nested functions as own symbols (`local_function` attr).
    Pass 2 walks every collected body once plus chunk-level statements
    (owner = module: Lua runs chunk code).

    Calls: file locals resolve to `calls`; `alias.member` (require
    alias recorded) and `table.member` shape selector for lua_resolve;
    colon calls shape selector with `colon` attr (receiver `self`,
    never bound: no flow analysis); dynamic require (non-string arg)
    is skipped (documented); metatable/`__index` dispatch, `_G`
    mutation, `loadstring` and table-constructor function fields are
    documented non-goals — never guessed.
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-lua"
        self.funcs: dict[str, str] = {}    # local/global fun name -> nid
        self.methods: dict[str, str] = {}  # "Table.name" -> nid
        self.aliases: dict[str, str] = {}  # require local -> specifier
        self.member_aliases: dict[str, tuple] = {}  # local -> (specifier, member)
        self.exported = ""                 # chunk `return X` table name
        self.bodies: list[tuple] = []

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "lua", kind, qname, "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), "lua", "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        self.top_calls = []
        self.nested_aliases = self._nested_requires(root)
        self.table_aliases = self._module_tables(root)
        for stmt in root.children:
            self._chunk(stmt, self.mid, top=True)
        # chunk-level calls are walked after pass 1, once every function
        # body is collected (the skip test reads that set)
        for stmt in self.top_calls:
            self._calls_in(stmt, self.mid)
        if self.exported:
            for n in self.res.nodes:
                if n.id == self.mid:
                    n.attributes = dict(n.attributes or {})
                    n.attributes["exports"] = self.exported
        for owner, body in self.bodies:
            self._calls_in(body, owner)

    def _chunk(self, node, owner: str, top=False):
        t = node.type
        if t == "function_declaration":
            self._funcdecl(node, owner)
        elif t == "variable_declaration":
            self._vardecl(node, owner)
            if top:
                # `local colors = { normal = utils.extract(...) }`: calls in a
                # chunk-level value run at load and belong to the module
                self.top_calls.append(node)
        elif t == "assignment_statement":
            self._assign(node, owner)
            if top:
                self.top_calls.append(node)
        elif t == "return_statement":
            # `return M` parses as return_statement > expression_list > identifier
            flat = [g for c in node.children
                    for g in ([c] if c.type != "expression_list" else c.children)]
            values = [g for g in flat if g.type not in ("return", ",", ";")]
            names = [_text(c, self.raw, 120) for c in values if c.type == "identifier"]
            if top and len(names) == 1 and len(values) == 1:
                self.exported = names[0]
            if top:
                self._returned_values(node)
        elif top:
            # Chunk-level statements run at load (loops, pcalls, ...):
            # their calls belong to the module, never dropped.
            self.top_calls.append(node)
            # Named functions declared inside chunk-level callbacks
            # (`describe('x', function() local function f() end end)`).
            self._declare_nested(node, self.mid)

    def _name_toks(self, node) -> list:
        """Identifiers of a (nested) dot / colon index name, in order."""
        out = []
        for g in node.children:
            if g.type == "identifier":
                out.append(_text(g, self.raw, 120))
            elif g.type in ("dot_index_expression", "method_index_expression"):
                out.extend(self._name_toks(g))
        return out

    def _func_name(self, node):
        """(kind, dotted) for a function name prefix: M.f -> method,
        M:f -> colon-method, f -> function."""
        for c in node.children:
            if c.type == "dot_index_expression":
                toks = self._name_toks(c)
                if len(toks) >= 2:
                    return "method", ".".join(toks), {}
                return None, "", {}
            if c.type == "method_index_expression":
                # `histories.History:new`: the table part may itself be dotted
                toks = self._name_toks(c)
                if len(toks) >= 2:
                    return "method", ".".join(toks), \
                        {"method_style": "colon"}
                return None, "", {}
            if c.type == "identifier":
                return "function", _text(c, self.raw, 120), {}
        return None, "", {}

    def _funcdecl(self, node, owner: str, local=False):
        kind, dotted, attrs = self._func_name(node)
        if not dotted:
            return
        is_local = local
        for c in node.children:
            if c.type == "local":
                is_local = True
        if kind == "method":
            qn = f"{self.mod}.{dotted}"
            if is_local:
                attrs = dict(attrs)
                attrs["local_function"] = True
            nid = self._sym("method", qn, node, attrs)
            self.methods[dotted] = nid
            self._defines(owner, nid, node)
        else:
            qn = f"{self.mod}.{dotted}"
            if is_local or owner != self.mid:
                attrs = {"local_function": True}
            nid = self._sym("function", qn, node, attrs)
            if owner == self.mid:
                self.funcs[dotted] = nid
            self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "block":
                self.bodies.append((nid, c))
                self._declare_nested(c, nid)

    def _returned_values(self, node):
        """Module patterns returned at chunk level (common in Lua libraries,
        e.g. busted): `return function(ctx) local function f() ... end end`
        -- the factory body is the module's scope; and `return { f = function
        ... end }` -- each field is a module function. Both were skipped,
        leaving such files without any symbol."""
        stack = [c for c in node.children if c.is_named]
        while stack:
            e = stack.pop(0)
            if e.type == "expression_list":
                stack = [c for c in e.children if c.is_named] + stack
            elif e.type == "function_definition":
                for c in e.children:
                    if c.type == "block":
                        self.bodies.append((self.mid, c))
                        self._declare_nested(c, self.mid)
            elif e.type == "table_constructor":
                for fld in e.children:
                    if fld.type != "field":
                        continue
                    key = fld.child_by_field_name("name")
                    val = fld.child_by_field_name("value")
                    if key is not None and val is not None and \
                            key.type == "identifier":
                        # `k = getfenv or function ... end` keeps the literal
                        fn = val if val.type == "function_definition" else next(
                            (g for g in val.children if g.type == "function_definition"),
                            None) if val.type == "binary_expression" else None
                        if fn is not None:
                            self._func_from_value(_text(key, self.raw, 120), fn,
                                                  self.mid, False)

    def _declare_nested(self, block, owner: str):
        # Function bodies may declare their own locals (idiomatic Lua):
        # each gets its own symbol (C# local_function parity) and body,
        # so the pass-2 walk can skip their subtrees without losing
        # their calls.
        stack = list(block.children)
        while stack:
            n = stack.pop()
            t = n.type
            if t == "function_declaration":
                self._funcdecl(n, owner)
                continue
            if t in ("variable_declaration", "assignment_statement"):
                if t == "variable_declaration":
                    self._vardecl(n, owner)
                else:
                    self._assign(n, owner)
                continue
            stack.extend(n.children)

    def _module_tables(self, root) -> dict:
        """`local modules = lazy_require { utils = 'a.utils', hl = 'a.hl' }`
        (or a bare table): name -> {field: module spec} for string fields."""
        out: dict = {}
        for n in root.children:
            if n.type != "variable_declaration":
                continue
            txt = _text(n, self.raw, 4000)
            m = re.match(r"\s*local\s+(\w+)\s*=\s*[^\n{]*?\{(.*)\}\s*\)?\s*$", txt, re.S)
            if not m:
                continue
            fields = dict(re.findall(r"(\w+)\s*=\s*['\"]([\w.\-/]+)['\"]", m.group(2)))
            if fields:
                out[m.group(1)] = fields
        return out

    def _nested_requires(self, root) -> dict:
        """`local diff = require('a.b')` inside functions / test blocks:
        name -> spec when the name means one module throughout the file."""
        seen: dict = {}
        stack = [c for c in root.children if c.type != "variable_declaration"]
        while stack:
            n = stack.pop()
            if n.type == "variable_declaration":
                a = next((c for c in n.children if c.type == "assignment_statement"), None)
                vl = next((c for c in a.children if c.type == "variable_list"), None) if a else None
                el = next((c for c in a.children if c.type == "expression_list"), None) if a else None
                names = [_text(g, self.raw, 120) for g in vl.children if g.type == "identifier"] if vl else []
                exprs = [e for e in el.children if e.type != ","] if el else []
                if len(names) == 1 and len(exprs) == 1 and exprs[0].type == "function_call":
                    spec = self._require_spec(exprs[0])
                    if spec:
                        seen.setdefault(names[0], set()).add(spec)
            stack.extend(n.children)
        return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}

    def _require_spec(self, node):
        """(specifier, alias-needed) if node is require("...")."""
        fn = None
        args = None
        for c in node.children:
            if c.type == "identifier" and fn is None:
                fn = _text(c, self.raw, 120)
            elif c.type == "arguments":
                args = c
        if fn != "require" or args is None:
            return ""
        for c in args.children:
            if c.type == "string":
                return _strip_quotes(_text(c, self.raw, 300))
            if c.type == "string_content":
                return _text(c, self.raw, 300)
        return ""

    def _vardecl(self, node, owner: str):
        assigns = None
        for c in node.children:
            if c.type == "assignment_statement":
                assigns = c
                break
        if assigns is None:
            return
        names = []
        exprs = []
        for c in assigns.children:
            if c.type == "variable_list":
                names = [_text(g, self.raw, 120) for g in c.children
                         if g.type == "identifier"]
            elif c.type == "expression_list":
                exprs = list(c.children)
        if len(names) != 1 or not exprs:
            return
        local_tok = any(c.type == "local" for c in node.children)
        if len(exprs) == 1 and exprs[0].type == "function_call":
            spec = self._require_spec(exprs[0])
            if spec:
                self._edge(self.mid, "imports", self._ext(spec), node,
                           "structural", "deterministic", 1.0,
                           {"specifier": spec, "alias": names[0],
                            "dynamic": False})
                self.aliases[names[0]] = spec
                return
        if len(exprs) == 1 and exprs[0].type == "dot_index_expression":
            # `local get_status = require("telescope.state").get_status` /
            # `local buf_delete = utils.buf_delete`: a module function alias
            e = exprs[0]
            member = next((_text(g, self.raw, 120) for g in reversed(e.children)
                           if g.type == "identifier"), "")
            head = e.children[0] if e.children else None
            spec = ""
            if head is not None and head.type == "function_call":
                spec = self._require_spec(head)
            elif head is not None and head.type == "identifier" and len(e.children) == 3:
                spec = self.aliases.get(_text(head, self.raw, 120), "")
            if spec and member:
                self.member_aliases[names[0]] = (spec, member)
                return
        for e in exprs:
            if e.type == "function_definition":
                self._func_from_value(names[0], e, owner, local_tok)
                return

    def _assign(self, node, owner: str):
        lhs = ""
        fn = None
        for c in node.children:
            if c.type == "variable_list":
                idx = [g for g in c.children
                       if g.type in ("dot_index_expression",
                                     "method_index_expression",
                                     "identifier")]
                if len(idx) == 1:
                    g = idx[0]
                    if g.type == "identifier":
                        lhs = _text(g, self.raw, 120)
                    else:
                        toks = [_text(h, self.raw, 120)
                                for h in g.children
                                if h.type == "identifier"]
                        if len(toks) >= 2:
                            lhs = ".".join(toks)
            elif c.type == "expression_list":
                for e in c.children:
                    if e.type == "function_definition":
                        fn = e
                        break
        if not lhs or fn is None:
            return
        self._func_from_value(lhs, fn, owner, False)

    def _func_from_value(self, lhs: str, fn, owner: str, local: bool):
        if "." in lhs or ":" in lhs:
            dotted = lhs.replace(":", ".")
            attrs: dict = {}
            if ":" in lhs:
                attrs["method_style"] = "colon"
            if local:
                attrs["local_function"] = True
            nid = self._sym("method", f"{self.mod}.{dotted}", fn,
                            attrs)
            self.methods[dotted] = nid
            self._defines(owner, nid, fn)
        else:
            attrs = {"local_function": True} if local else {}
            nid = self._sym("function", f"{self.mod}.{lhs}", fn, attrs)
            if owner == self.mid:
                self.funcs[lhs] = nid
            self._defines(owner, nid, fn)
        for c in fn.children:
            if c.type == "block":
                self.bodies.append((nid, c))
                self._declare_nested(c, nid)

    # ------------------------------------------------------------ pass 2
    def _calls_in(self, node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "function_call":
                self._call(n, owner)
            if t in ("function_declaration", "function_definition") and \
                    n is not node and (t == "function_declaration" or
                                       self._is_collected(n)):
                # Nested definitions own their bodies (collected in
                # pass 1); the outer walk skips them: no doubles. An
                # anonymous callback (`each(t, function(k) f(k) end)`) has
                # no symbol, so its calls belong to the enclosing function.
                continue
            stack.extend(n.children)

    def _is_collected(self, fn_node) -> bool:
        if getattr(self, "_collected", None) is None:
            self._collected = {(b.parent.start_byte, b.parent.end_byte)
                               for _o, b in self.bodies if b.parent is not None}
            self._collected |= {(b.start_byte, b.end_byte) for _o, b in self.bodies}
        return (fn_node.start_byte, fn_node.end_byte) in self._collected

    def _call(self, node, owner: str):
        prefix = None
        args = None
        for c in node.children:
            if c.type in ("identifier", "dot_index_expression",
                          "method_index_expression",
                          "function_call") and prefix is None:
                prefix = c
            elif c.type == "arguments":
                args = c
        if prefix is None:
            return
        if prefix.type == "identifier" and \
                _text(prefix, self.raw, 120) == "require":
            return  # imports edge already minted at declaration
        arity = 0
        if args is not None:
            arity = sum(1 for g in args.children
                        if g.type not in ("(", ")", ","))
        if prefix.type == "identifier":
            base = _text(prefix, self.raw, 120)
            if not base:
                return
            tgt = self.funcs.get(base)
            if tgt is None and base in self.member_aliases:
                spec, member = self.member_aliases[base]
                self._edge(owner, "calls_unresolved", self._ext(spec + "." + member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": "__require__" + spec, "member": member,
                            "arity": arity, "require_bound": True, "alias_of": base})
                return
            if tgt is not None:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9, {"arity": arity})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            return
        if prefix.type in ("dot_index_expression",
                           "method_index_expression") and prefix.children \
                and prefix.children[0].type == "function_call":
            # `require('gitsigns.diff_int').run_word_diff(x)`: an inline
            # require; bound like `local m = require(..)` + `m.f(x)`
            spec = self._require_spec(prefix.children[0])
            member = next((_text(g, self.raw, 120) for g in reversed(prefix.children)
                           if g.type == "identifier"), "")
            if spec and member:
                alias = "__require__" + spec      # lua_resolve resolves the spec itself
                self._edge(owner, "calls_unresolved", self._ext(spec + "." + member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": alias, "member": member,
                            "arity": arity, "require_bound": True})
                return
        if prefix.type in ("dot_index_expression",
                           "method_index_expression"):
            dotted = _text(prefix, self.raw, 200).replace(":", ".").split(".")
            tbl = getattr(self, "table_aliases", {}).get(dotted[0]) if len(dotted) == 3 else None
            if tbl and dotted[1] in tbl and all(re.fullmatch(r"\w+", p) for p in dotted):
                # `local modules = lazy_require { utils = 'pkg.utils' }` +
                # `modules.utils.f()`: the field names a module (the resolver
                # binds only when the string is a module of this repo)
                spec = tbl[dotted[1]]
                self._edge(owner, "calls_unresolved", self._ext(spec + "." + dotted[2]), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": "__require__" + spec, "member": dotted[2],
                            "arity": arity, "require_bound": True})
                return
            toks = [_text(g, self.raw, 120) for g in prefix.children
                    if g.type == "identifier"]
            if len(toks) < 2 and prefix.type == "method_index_expression" and toks:
                # `async.run(f):raise_on_error()`, `self.source:get_name()`: the
                # receiver is an expression; record the colon call so a
                # distinctive unique method name can still bind (inferred)
                name_node = [g for g in prefix.children if g.type == "identifier"][-1]
                # located at the method name: a chain spanning lines starts elsewhere
                self._edge(owner, "calls_unresolved", self._ext("<expr>." + toks[-1]), name_node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": "<expr>", "member": toks[-1],
                            "arity": arity, "require_bound": False, "colon": True})
                return
            if len(toks) < 2:
                return
            base, member = toks[0], toks[-1]
            if base not in self.aliases and base in getattr(self, "nested_aliases", {})                     and len(toks) == 2:
                # bound by a `local x = require(..)` inside a function
                spec = self.nested_aliases[base]
                self._edge(owner, "calls_unresolved", self._ext(spec + "." + member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "selector", "base": "__require__" + spec, "member": member,
                            "arity": arity, "require_bound": True,
                            **({"colon": True} if prefix.type == "method_index_expression" else {})})
                return
            attrs: dict = {"callee_kind": "selector", "base": base,
                           "member": member, "arity": arity,
                           "require_bound": bool(base in self.aliases)}
            if prefix.type == "method_index_expression":
                attrs["colon"] = True
            self._edge(owner, "calls_unresolved",
                       self._ext(base + "." + member), node,
                       "structural", "deterministic", 1.0, attrs)
            return
        self._edge(owner, "calls_unresolved", self._ext("result"), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "complex", "arity": arity})


def _swift_dotted(node, raw: bytes) -> str:
    """Dotted path for Swift type syntax (skips generics/optionals)."""
    parts: list[str] = []

    def _walk(n):
        if n.type in ("simple_identifier", "type_identifier",
                      "identifier"):
            t = _text(n, raw, 120).split("<")[0].split("[")[0].strip()
            if t:
                parts.append(t)
        else:
            for c in n.children:
                if c.type not in (".", "?", "!", "->", ":"):
                    _walk(c)

    _walk(node)
    return ".".join(p for p in parts if p)


class _SwiftVisitor:
    """Two-pass Swift walker.

    Pass 1 declares: imports (module specifiers; `import struct M.X`
    access-level forms recorded coarsely), classes/structs (class, Go
    doctrine)/enums (coarse class + case entries as constants)/
    protocols (interface + declared members)/extensions (methods
    attributed to the extended type when it is a bare file-local type,
    else a synthetic `extension.<Name>` scope — documented),
    heritage (first `inheritance_specifier` -> `inherits`, rest ->
    `implements`, C# positional doctrine), ctors (`<init>`, Java
    doctrine), static/class members, properties as variables (plain,
    computed and observer bodies all walked under the property),
    top-level functions and chunk-level statements (owner = module,
    Lua doctrine). Pass 2 walks every collected body once (named nested
    declarations declared on encounter, Kotlin parity).

    Calls: file-local functions resolve to `calls`; file-local classes
    called bare (`Mover(...)`) resolve to `instantiates`; `Type.member`
    navigation, `this`/`super` receivers (Java doctrine),
    optional/force chains (`m?.f()`, `m!.f`) shape on the named member;
    the rest stays `calls_unresolved` for swift_resolve (same-dir,
    module-suffix and `this`/`super` binding). Subscripts, operators,
    `where`-clause generics and macro expansions are documented
    non-goals — never guessed.
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-swift"
        self.funcs: dict[str, str] = {}
        self.classes: dict[str, str] = {}
        self.bodies: list[tuple] = []

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "swift", kind, qname,
                                  "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), "swift", "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        for stmt in root.children:
            t = stmt.type
            if t == "import_declaration":
                self._import(stmt)
            elif t in ("class_declaration", "protocol_declaration"):
                self._type(stmt, self.mid, self.mod)
            elif t == "function_declaration":
                self._function(stmt, self.mid, self.mod, top=True)
            elif t == "property_declaration":
                self._property(stmt, self.mid, self.mod)
            else:
                # Top-level statements (main.swift scripts): chunk code
                # belongs to the module, never dropped.
                self._calls_in(stmt, self.mid)
        for owner, body in self.bodies:
            self._calls_in(body, owner)

    def _import(self, node):
        spec = ""
        for c in node.children:
            if c.type in ("identifier", "user_type"):
                spec = _swift_dotted(c, self.raw)
                break
        if not spec:
            for c in node.children:
                if c.type == "simple_identifier":
                    spec = _text(c, self.raw, 120)
                    break
        if not spec:
            return
        self._edge(self.mid, "imports", self._ext(spec), node,
                   "structural", "deterministic", 1.0,
                   {"specifier": spec, "dynamic": False})

    def _type_kind(self, node) -> tuple[str, str | None]:
        """(node-kind, extension-target or None)."""
        first = ""
        for c in node.children:
            if c.type in ("class", "struct", "enum", "extension",
                          "protocol"):
                first = c.type
                break
        if node.type == "protocol_declaration" or first == "protocol":
            return "interface", None
        if first == "extension":
            for c in node.children:
                if c.type == "user_type":
                    return "class", _swift_dotted(c, self.raw)
            return "class", ""
        return "class", None

    def _type(self, node, owner: str, qn: str):
        kind, ext_target = self._type_kind(node)
        if ext_target is not None:
            # Extension declarations carry no type_identifier of their
            # own (measured): the extended name IS the identity.
            name = ext_target.split(".")[-1] if ext_target else ""
            if not name:
                return
            # Attach to the extended type when it is a bare file-local
            # name; else a synthetic scope (documented).
            if "." not in ext_target and ext_target in self.classes:
                full = next((n.qualified_name for n in self.res.nodes
                             if n.id == self.classes[ext_target]),
                            f"{qn}.{ext_target}")
            else:
                full = f"{qn}.extension.{ext_target}" if ext_target \
                    else f"{qn}.extension"
            owner_nid = self.classes.get(ext_target.split(".")[-1],
                                        owner)
            for c in node.children:
                if c.type in ("class_body", "enum_class_body",
                              "protocol_body"):
                    self._members(c, owner_nid, full)
            return
        name = ""
        for c in node.children:
            if c.type == "type_identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym(kind, full, node)
        if kind == "class":
            self.classes[name] = nid
        self._defines(owner, nid, node)
        owner_nid = nid
        for c in node.children:
            if c.type == "inheritance_specifier" and not ext_target:
                for g in c.children:
                    if g.type == "user_type":
                        nm = _swift_dotted(g, self.raw)
                        if nm:
                            first = not any(
                                e.relation == "inherits"
                                for e in self.res.edges
                                if e.source == owner_nid)
                            rel = "inherits" if first else "implements"
                            key = "superclass" if first else "interface"
                            self._edge(owner_nid, rel,
                                       self._ext(nm), c, "resolved",
                                       "resolved", 0.9, {key: nm})
            elif c.type in ("class_body", "enum_class_body",
                            "protocol_body"):
                self._members(c, owner_nid, full)

    def _members(self, body, owner: str, qn: str):
        for m in body.children:
            t = m.type
            if t == "function_declaration":
                self._function(m, owner, qn)
            elif t == "init_declaration":
                self._init(m, owner, qn)
            elif t == "deinit_declaration":
                self.bodies.append((owner, m))      # `deinit { f() }` runs when the instance goes away
            elif t == "property_declaration":
                self._property(m, owner, qn)
            elif t == "protocol_function_declaration":
                nm = ""
                for g in m.children:
                    if g.type == "simple_identifier":
                        nm = _text(g, self.raw, 120)
                        break
                if nm:
                    nid = self._sym("method", f"{qn}.{nm}", m,
                                    {"declared": True})
                    self._defines(owner, nid, m)
            elif t in ("class_declaration", "protocol_declaration"):
                self._type(m, owner, qn)
            elif t == "enum_entry":
                for g in m.children:
                    if g.type == "simple_identifier":
                        eid = self._sym(
                            "constant",
                            f"{qn}.{_text(g, self.raw, 120)}", g)
                        self._defines(owner, eid, g)

    def _func_name(self, node) -> str:
        for c in node.children:
            if c.type == "simple_identifier":
                return _text(c, self.raw, 120)
        return ""

    def _function(self, node, owner: str, qn: str, top=False):
        name = self._func_name(node)
        if not name:
            return
        attrs: dict = {}
        for c in node.children:
            if c.type == "modifiers" and "static" in _text(
                    c, self.raw, 60).split():
                attrs["static"] = True
        full = f"{qn}.{name}"
        kind = "function" if (top and owner == self.mid) else "method"
        nid = self._sym(kind, full, node, attrs)
        if top and owner == self.mid:
            self.funcs[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "function_body":
                self.bodies.append((nid, c))

    def _init(self, node, owner: str, qn: str):
        nid = self._sym("method", f"{qn}.<init>", node)
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "function_body":
                self.bodies.append((nid, c))

    def _property(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "pattern":
                for g in c.children:
                    if g.type == "simple_identifier":
                        name = _text(g, self.raw, 120)
                        break
            elif c.type == "simple_identifier" and not name:
                name = _text(c, self.raw, 120)
        if not name:
            return
        attrs = None
        ta = next((c for c in node.children if c.type == "type_annotation"), None)
        if ta is not None:
            # `var fileio: FileIO { .. }` (often in an extension): the type,
            # so `app.fileio.streamFile()` can walk through it
            from .typed_params import _simple_type
            ut = next((c for c in ta.children if c.is_named), None)
            t = _simple_type(ut, self.raw) if ut is not None else ""
            if t:
                attrs = {"var_type": t}
        nid = self._sym("variable", f"{qn}.{name}", node, attrs)
        self._defines(owner, nid, node)
        seen_eq = False
        for c in node.children:
            if c.type in ("function_body", "computed_property"):
                self.bodies.append((nid, c))
            elif c.type == "=":
                seen_eq = True
            elif seen_eq and c.is_named:
                # `static let description = RuleDescription(..).skip()`: the
                # initializer's calls belong to the property
                self.bodies.append((nid, c))
                seen_eq = False

    # ------------------------------------------------------------ pass 2
    def _calls_in(self, node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call_expression":
                self._call(n, owner)
            if t == "function_declaration" and n is not node:
                qn = ""
                for m in self.res.nodes:
                    if m.id == owner:
                        qn = m.qualified_name or ""
                        break
                self._function(n, owner, qn or self.mod)
                continue
            if t in ("class_declaration", "protocol_declaration"):
                continue
            stack.extend(n.children)

    def _call(self, node, owner: str):
        fn = None
        suffix = None
        for c in node.children:
            if c.type == "prefix_expression" and fn is None and c.children \
                    and c.children[0].type == "." and len(c.children) == 2 \
                    and c.children[1].type == "simple_identifier":
                # `.allExcept(X.self)` / `.inMemory(..)`: an implicit member of
                # the contextual type, never a call on self
                member = _text(c.children[1], self.raw, 120)
                sfx = next((g for g in node.children if g.type == "call_suffix"), None)
                va = next((g for g in sfx.children if g.type == "value_arguments"), None) \
                    if sfx is not None else None
                ar = sum(1 for h in va.children if h.type == "value_argument") if va is not None else 0
                self._edge(owner, "calls_unresolved", self._ext(member), node,
                           "structural", "deterministic", 1.0,
                           {"callee_kind": "complex", "member": member, "arity": ar,
                            "dot_member": True})
                return
            if c.type in _SWIFT_INFIX and fn is None:
                # `$0 + f()` / `a == b.m()` parse as (`$0 + f`)(): the call belongs to the last operand
                while c.type in _SWIFT_INFIX and c.named_child_count:
                    c = c.named_children[-1]
            if c.type == "prefix_expression" and fn is None:
                # `!node.modifiers.m()` parses as (!node.modifiers.m)(): the
                # call belongs to the operand
                c = next((g for g in c.children if g.type in (
                    "simple_identifier", "navigation_expression")), None) or c
            if c.type in ("simple_identifier",
                          "navigation_expression") and fn is None:
                fn = c
            elif c.type == "call_suffix":
                suffix = c
        if fn is None:
            return
        arity = 0
        if suffix is not None:
            for g in suffix.children:
                if g.type == "value_arguments":
                    arity = sum(1 for h in g.children
                                if h.type == "value_argument")
                    break
        if fn.type == "simple_identifier":
            base = _text(fn, self.raw, 120)
            if not base:
                return
            if base in self.funcs:
                self._edge(owner, "calls", self.funcs[base], node,
                           "resolved", "resolved", 0.9, {"arity": arity})
            elif base in self.classes:
                self._edge(owner, "instantiates", self._ext(base), node,
                           "resolved", "resolved", 0.9,
                           {"created": base})
            else:
                self._edge(owner, "calls_unresolved", self._ext(base),
                           node, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            return
        kids = [c for c in fn.children]
        first = None
        member = ""
        for c in kids:
            if c.type in ("simple_identifier", "self_expression",
                          "super_expression") and first is None:
                first = c
            elif c.type == "navigation_suffix":
                for g in c.children:
                    if g.type == "simple_identifier":
                        member = _text(g, self.raw, 120)
                        member_node = g
        if not member:
            return
        if first is None:
            # `RuleDescription(.. ]).skipWrappingInCommentTests()`: a chain
            # spanning lines is located at the method name
            self._edge(owner, "calls_unresolved", self._ext(member),
                       member_node, "structural", "deterministic", 1.0,
                       {"callee_kind": "complex", "member": member,
                        "arity": arity})
            return
        if first.type == "self_expression":
            self._edge(owner, "calls_unresolved", self._ext(member),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "this", "member": member,
                        "arity": arity})
            return
        if first.type == "super_expression":
            self._edge(owner, "calls_unresolved", self._ext(member),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "super", "member": member,
                        "arity": arity})
            return
        base = _text(first, self.raw, 120)
        if not base:
            self._edge(owner, "calls_unresolved", self._ext(member),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "complex", "member": member,
                        "arity": arity})
            return
        self._edge(owner, "calls_unresolved",
                   self._ext(base + "." + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "selector", "base": base,
                    "member": member, "arity": arity})


class _DartVisitor:
    """Two-pass Dart walker (0.1.0 experimental grammar — strict shapes).

    The grammar emits NO `method_invocation` node: calls are FLAT SIBLING
    sequences (`identifier`, `.member`, `(args)` as adjacent children of
    the statement/value node). Only the strict measured patterns shape
    calls; cascades (`..`), conditionals (`?.`), and longer exotic runs
    stay unresolved (documented): a member piece must be a `.`-led
    selector immediately followed by an argument selector, a free call a
    bare identifier immediately followed by one.

    Pass 1 declares: imports (`package:`/`dart:`/relative URIs, `as`
    aliases, `show`/`hide` combinators recorded but never bound —
    documented conservative), `part` directives (library-scope merge
    keys for dart_resolve), classes (ctors `<init>`, named ctors,
    factories as methods; fields as variables with initializers walked),
    mixins (class + `mixin` attr), extensions (class + `extension` attr;
    methods under the extension name — attaching to primitives would
    pollute), enums (coarse class + constants), `extends`->inherits,
    `with`/`implements`->implements, top-level functions (split
    `function_signature` + `function_body` siblings paired by adjacency).
    `super(...)` constructor calls shape `instantiates` on the file-local
    superclass; getters/setters and `external`/`covariant`/`late`
    subtleties are documented non-goals.
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-dart"
        self.funcs: dict[str, str] = {}
        self.classes: dict[str, str] = {}
        self.bodies: list[tuple] = []

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "dart", kind, qname, "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), "dart", "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        for stmt in root.children:
            t = stmt.type
            if t == "import_or_export":
                self._import(stmt)
            elif t == "part_directive":
                uri = ""
                for c in stmt.children:
                    if c.type == "uri":
                        uri = _strip_quotes(_text(c, self.raw, 300))
                        break
                if uri:
                    self._edge(self.mid, "imports", self._ext(uri),
                               stmt, "structural", "deterministic", 1.0,
                               {"specifier": uri, "part": True,
                                "dynamic": False})
            elif t == "class_definition":
                self._class(stmt, self.mid, self.mod)
            elif t == "mixin_declaration":
                self._mixin(stmt, self.mid, self.mod)
            elif t == "extension_declaration":
                self._extension(stmt, self.mid, self.mod)
            elif t == "enum_declaration":
                self._enum(stmt, self.mid, self.mod)
        self._pair_top_level(root)
        for owner, body in self.bodies:
            self._calls_in(body, owner)

    def _uri_text(self, node) -> str:
        for c in node.children:
            if c.type == "configurable_uri":
                for g in c.children:
                    if g.type == "uri":
                        return _strip_quotes(_text(g, self.raw, 300))
            elif c.type == "uri":
                return _strip_quotes(_text(c, self.raw, 300))
        return ""

    def _import(self, node):
        for c in node.children:
            if c.type != "library_import":
                continue
            spec = ""
            alias = ""
            names: list[str] = []
            filt = ""
            for g in c.children:
                if g.type == "import_specification":
                    for h in g.children:
                        if h.type == "configurable_uri":
                            spec = self._uri_text(g)
                        elif h.type == "combinator":
                            for k in h.children:
                                if k.type in ("show", "hide") and not filt:
                                    filt = k.type
                                elif k.type == "identifier":
                                    names.append(
                                        _text(k, self.raw, 120))
                        elif h.type == "as":
                            pass
                        elif h.type == "identifier" and not alias:
                            # `as` prefix comes as bare identifier after
                            # the `as` keyword; combinator names are
                            # nested, so a direct identifier is the alias.
                            alias = _text(h, self.raw, 120)
            if not spec:
                continue
            attrs: dict = {"specifier": spec, "dynamic": False}
            if alias:
                attrs["alias"] = alias
            if names:
                attrs["names"] = names
            if filt:
                attrs["show_hide"] = filt
            self._edge(self.mid, "imports", self._ext(spec), node,
                       "structural", "deterministic", 1.0, attrs)

    def _heritage(self, node, owner: str):
        """superclass children: extends type / with mixins."""
        for c in node.children:
            if c.type == "type_identifier":
                nm = _text(c, self.raw, 120)
                # `extends A` vs bare mixin list: the `extends` keyword
                # sibling decides (measured shapes).
                is_extends = any(g.type == "extends"
                                 for g in node.children)
                if is_extends:
                    self._edge(owner, "inherits", self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"superclass": nm})
                else:
                    self._edge(owner, "implements", self._ext(nm), c,
                               "resolved", "resolved", 0.9,
                               {"interface": nm})
            elif c.type == "mixins":
                for g in c.children:
                    if g.type == "type_identifier":
                        nm = _text(g, self.raw, 120)
                        self._edge(owner, "implements",
                                   self._ext(nm), g, "resolved",
                                   "resolved", 0.9, {"interface": nm})

    def _class(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node)
        self.classes[name] = nid
        self._defines(owner, nid, node)
        kids = list(node.children)
        for i, c in enumerate(kids):
            if c.type == "superclass":
                self._heritage(c, nid)
            elif c.type == "class_body":
                self._class_body(c, nid, full)

    def _mixin(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node, {"mixin": True})
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "class_body":
                self._class_body(c, nid, full)

    def _extension(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node, {"extension": True})
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "extension_body":
                self._paired_members(list(c.children), nid, full)

    def _enum(self, node, owner: str, qn: str):
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("class", full, node, {"enum": True})
        self.classes[name] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "enum_body":
                for g in c.children:
                    if g.type == "enum_constant":
                        for h in g.children:
                            if h.type == "identifier":
                                eid = self._sym(
                                    "constant",
                                    f"{full}.{_text(h, self.raw, 120)}",
                                    h)
                                self._defines(nid, eid, h)
                                break

    def _class_body(self, body, owner: str, qn: str):
        self._paired_members(list(body.children), owner, qn)

    def _paired_members(self, kids: list, owner: str, qn: str):
        """Adjacent signature+body pairing (split siblings)."""
        i = 0
        while i < len(kids):
            c = kids[i]
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            if c.type == "method_signature":
                self._method_sig(c, nxt if nxt is not None and
                                 nxt.type == "function_body" else None,
                                 owner, qn)
                i += 2 if nxt is not None and nxt.type == \
                    "function_body" else 1
            elif c.type == "declaration":
                self._decl_member(c, owner, qn)
                if self._runs_code(c):
                    self.bodies.append((owner, c))      # `int f = g();` / `C() : f = g();` run on construction
                i += 1
            else:
                i += 1

    @staticmethod
    def _runs_code(decl) -> bool:
        """A class member declaration whose initializer (field value or constructor initializer list) calls code."""
        for g in decl.children:
            if g.type == "initializers":
                return True
            if g.type == "initialized_identifier_list" and any(
                    h.type == "=" for i_ in g.children for h in i_.children):
                return True
        return False

    def _sig_name(self, sig) -> tuple[str, dict]:
        """(name, attrs) for method/constructor/factory signatures.

        Accepts the signature node itself: method_signature (wrapping a
        function/constructor/factory signature) or a bare
        constructor_signature (declaration members hand it directly).
        """
        if sig.type == "constructor_signature":
            return self._ctor_name(sig)
        if sig.type == "factory_constructor_signature":
            ids = [_text(g, self.raw, 120) for g in sig.children
                   if g.type == "identifier"]
            if len(ids) >= 2:
                return ids[-1], {"factory": True}
            return (ids[-1] if ids else ""), {"factory": True}
        for c in sig.children:
            if c.type == "factory_constructor_signature":
                ids = [_text(g, self.raw, 120) for g in c.children
                       if g.type == "identifier"]
                if len(ids) >= 2:
                    return ids[-1], {"factory": True}
                return (ids[-1] if ids else ""), {"factory": True}
            if c.type == "constructor_signature":
                return self._ctor_name(c)
        for c in sig.children:
            if c.type == "function_signature":
                for g in c.children:
                    if g.type == "identifier":
                        return _text(g, self.raw, 120), {}
        return "", {}

    def _ctor_name(self, sig) -> tuple[str, dict]:
        ids = [_text(g, self.raw, 120) for g in sig.children
               if g.type == "identifier"]
        dots = any(g.type == "." for g in sig.children)
        if dots and len(ids) >= 2:
            return ids[-1], {"constructor": True}
        return "<init>", {"constructor": True}

    def _method_sig(self, sig, body, owner: str, qn: str):
        name, attrs = self._sig_name(sig)
        if not name:
            return
        full = f"{qn}.{name}"
        nid = self._sym("method", full, sig, attrs)
        self._defines(owner, nid, sig)
        if body is not None:
            self.bodies.append((nid, body))

    def _decl_member(self, node, owner: str, qn: str):
        has_ctor = any(c.type == "constructor_signature"
                       for c in node.children)
        if has_ctor:
            for c in node.children:
                if c.type == "constructor_signature":
                    name, attrs = self._sig_name(c)
                    nid = self._sym("method", f"{qn}.{name}", c,
                                    attrs)
                    self._defines(owner, nid, c)
            return
        name = ""
        for c in node.children:
            if c.type == "identifier":
                name = _text(c, self.raw, 120)
                break
            if c.type == "initialized_identifier":
                for g in c.children:
                    if g.type == "identifier":
                        name = _text(g, self.raw, 120)
                        break
        if not name:
            return
        nid = self._sym("variable", f"{qn}.{name}", node)
        self._defines(owner, nid, node)
        self.bodies.append((nid, node))

    def _pair_top_level(self, root):
        kids = list(root.children)
        i = 0
        while i < len(kids):
            c = kids[i]
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            if c.type == "function_signature":
                name = ""
                for g in c.children:
                    if g.type == "identifier":
                        name = _text(g, self.raw, 120)
                        break
                if name:
                    full = f"{self.mod}.{name}"
                    nid = self._sym("function", full, c)
                    self.funcs[name] = nid
                    self._defines(self.mid, nid, c)
                    if nxt is not None and nxt.type == "function_body":
                        self.bodies.append((nid, nxt))
                        i += 2
                        continue
            i += 1

    # ------------------------------------------------------------ pass 2
    def _is_member_piece(self, n) -> str:
        """Member name if n is a `.member` selector piece, else "".

        The 0.1.0 grammar is inconsistent: `this.x` wraps the piece in
        `selector`, `super.x` leaves `unconditional_assignable_selector`
        bare. Both shapes accepted (measured).
        """
        kids = None
        # `a.b` is an unconditional selector, `a?.b` a conditional one
        member_sels = ("unconditional_assignable_selector", "conditional_assignable_selector")
        if n.type == "selector":
            kids = list(n.children)
            if len(kids) == 1 and kids[0].type in member_sels:
                kids = list(kids[0].children)
        elif n.type in member_sels:
            kids = list(n.children)
        else:
            return ""
        if len(kids) == 2 and kids[0].type in (".", "?.") and \
                kids[1].type == "identifier":
            return _text(kids[1], self.raw, 120)
        return ""

    def _is_args_piece(self, n) -> bool:
        if n.type != "selector":
            return False
        for c in n.children:
            if c.type == "argument_part":
                return True
        return False

    def _args_arity(self, n) -> int:
        for c in n.children:
            if c.type == "argument_part":
                for g in c.children:
                    if g.type == "arguments":
                        return sum(1 for h in g.children
                                   if h.type == "argument")
        return 0

    def _calls_in(self, node, owner: str):
        # Flat sibling sequences: scan each node's children positionally.
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "function_signature" and n is not node:
                # Nested local function: own symbol (Kotlin parity),
                # paired body via the parent; subtree skipped below.
                self._nested_sig(n, owner)
                continue
            self._seq_calls(list(n.children), owner, n is node)
            for c in n.children:
                if c.type in ("class_definition", "mixin_declaration",
                              "extension_declaration", "enum_declaration",
                              "function_signature", "method_signature",
                              "constructor_signature",
                              "factory_constructor_signature"):
                    continue  # own scopes/bodies handled separately
                stack.append(c)

    def _nested_sig(self, node, owner: str):
        name = ""
        for g in node.children:
            if g.type == "identifier":
                name = _text(g, self.raw, 120)
                break
        if not name:
            return
        qn = ""
        for m in self.res.nodes:
            if m.id == owner:
                qn = m.qualified_name or ""
                break
        nid = self._sym("function", f"{qn or self.mod}.{name}", node,
                        {"local_function": True})
        self._defines(owner, nid, node)
        parent = node.parent
        if parent is not None:
            sibs = list(parent.children)
            for idx, s in enumerate(sibs):
                if s.id == node.id and s.start_point == node.start_point \
                        and s.end_point == node.end_point:
                    if idx + 1 < len(sibs) and \
                            sibs[idx + 1].type == "function_body":
                        self.bodies.append((nid, sibs[idx + 1]))
                    break

    def _seq_calls(self, kids: list, owner: str, is_root: bool):
        i = 0
        pending_base = ""
        pending_kind = ""  # identifier | this | super | ""
        while i < len(kids):
            c = kids[i]
            if c.type == "identifier" and not self._is_args_piece(c) \
                    and not self._is_member_piece(c):
                pending_base = _text(c, self.raw, 120)
                pending_kind = "identifier"
                i += 1
                continue
            if c.type in ("this", "super"):
                pending_base = c.type
                pending_kind = c.type
                i += 1
                continue
            member = self._is_member_piece(c)
            if member and i + 1 < len(kids) and \
                    self._is_args_piece(kids[i + 1]):
                arity = self._args_arity(kids[i + 1])
                if pending_kind == "identifier" and pending_base:
                    self._edge(owner, "calls_unresolved",
                               self._ext(pending_base + "." + member),
                               c, "structural", "deterministic", 1.0,
                               {"callee_kind": "selector",
                                "base": pending_base, "member": member,
                                "arity": arity})
                elif pending_kind in ("this", "super"):
                    self._edge(owner, "calls_unresolved",
                               self._ext(member), c, "structural",
                               "deterministic", 1.0,
                               {"callee_kind": pending_kind,
                                "member": member, "arity": arity})
                else:
                    self._edge(owner, "calls_unresolved",
                               self._ext(member), c, "structural",
                               "deterministic", 1.0,
                               {"callee_kind": "complex",
                                "member": member, "arity": arity})
                pending_base = ""
                pending_kind = ""
                i += 2
                continue
            if self._is_args_piece(c):
                arity = self._args_arity(c)
                if pending_kind == "identifier" and pending_base:
                    base = pending_base
                    if base in self.funcs:
                        self._edge(owner, "calls", self.funcs[base], c,
                                   "resolved", "resolved", 0.9,
                                   {"arity": arity})
                    elif base in self.classes:
                        self._edge(owner, "instantiates",
                                   self._ext(base), c, "resolved",
                                   "resolved", 0.9, {"created": base})
                    else:
                        self._edge(owner, "calls_unresolved",
                                   self._ext(base), c, "structural",
                                   "deterministic", 1.0,
                                   {"callee_kind": "function",
                                    "base": base, "arity": arity})
                elif pending_kind == "super":
                    # `super(0)` in an initializer list: the superclass
                    # constructor (resolver upgrades `super` via heritage).
                    self._edge(owner, "instantiates",
                               self._ext("super"), c, "resolved",
                               "resolved", 0.9, {"created": "super"})
                # bare args without a base (`f()()` tail, `this()`):
                # unshaped, guarded skip.
                pending_base = ""
                pending_kind = ""
                i += 1
                continue
            # Anything else resets the run (cascade `..`, `?.`, operators:
            # documented boundaries — the pieces must be strictly adjacent).
            pending_base = ""
            pending_kind = ""
            i += 1


class _GdscriptVisitor:
    """Two-pass GDScript walker (6.1.0 grammar).

    Pass 1 declares: `extends` (bare class/native name, or `"res://"`
    path), `class_name` globals, signals as events (schema `event`;
    stringly `connect`/`emit_signal` are never bound — documented),
    const/var (initializers walked under the symbol), inner classes,
    static methods, enums (coarse class + enumerator constants).
    Every file IS a class: the class symbol is the `class_name`, else
    the file stem (documented rule — anonymous scripts take the stem as
    identity); all functions are methods defined under it. Pass 2 walks
    every collected body once plus top-level statements (owner = the
    file class).

    Calls: receivers survive via `attribute > base + attribute_call`,
    so `(base, member)` shapes for the resolver; bare calls consult
    file-local functions; `Mover.new()` shapes `instantiates`
    (resolver binds class_name/file classes); `preload("res://...")`
    shapes `imports`, never a call; lambdas descend owner-approximately
    (Java anonymous-class doctrine). `self`/`super` receivers shaped
    for the resolver. Metatable-free by language design; `_G` mutation,
    `load`/`load_threaded_request` dynamic paths and `set`/`get`
    stringly access stay unresolved (documented).
    """

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp,
                 mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-gdscript"
        self.funcs: dict[str, str] = {}
        self.file_class = ""
        self.file_class_nid = ""
        self.bodies: list[tuple] = []

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "gdscript", kind,
                                  qname, "")
        self.res.nodes.append(Node(nid, kind, qname.split(".")[-1],
                                   qname, self.project_id, self.repo_id,
                                   self.rel, _loc(node), "gdscript", "",
                                   attrs or {}, [self.ev.id],
                                   schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str,
              cclass: str, score, attrs: dict | None = None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass,
                                   score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel,
                                   start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _defines(self, parent: str, nid: str, node):
        self._edge(parent, "defines", nid, node, "structural",
                   "deterministic", 1.0)

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    # ------------------------------------------------------------ pass 1
    def walk_module(self, root):
        named = ""
        for stmt in root.children:
            if stmt.type == "class_name_statement":
                for c in stmt.children:
                    if c.type == "name":
                        named = _text(c, self.raw, 120)
                        break
        if not named:
            named = self.rel.rpartition("/")[2].rsplit(".", 1)[0]
        self.file_class = named
        qn = f"{self.mod}.{named}" if self.mod != named else named
        self.file_class_nid = self._sym("class", qn, root,
                                        {"file_class": True})
        self._defines(self.mid, self.file_class_nid, root)
        for stmt in root.children:
            t = stmt.type
            if t == "extends_statement":
                self._extends(stmt)
            elif t == "signal_statement":
                nm = ""
                for c in stmt.children:
                    if c.type == "name":
                        nm = _text(c, self.raw, 120)
                        break
                if nm:
                    eid = self._sym("event", f"{qn}.{nm}", stmt)
                    self._defines(self.file_class_nid, eid, stmt)
            elif t == "const_statement":
                self._const_var(stmt, "constant")
            elif t == "variable_statement":
                self._const_var(stmt, "variable")
            elif t == "function_definition":
                self._function(stmt, self.file_class_nid, qn)
            elif t == "class_definition":
                self._inner_class(stmt, self.file_class_nid, qn)
            elif t == "enum_definition":
                self._enum(stmt, self.file_class_nid, qn)
            else:
                self._calls_in(stmt, self.file_class_nid)
        for owner, body in self.bodies:
            self._calls_in(body, owner)

    def _extends(self, node):
        for c in node.children:
            if c.type == "type":
                for g in c.children:
                    if g.type == "identifier":
                        nm = _text(g, self.raw, 120)
                        self._edge(self.file_class_nid, "inherits",
                                   self._ext(nm), node, "resolved",
                                   "resolved", 0.9, {"superclass": nm})
                        return
            elif c.type == "string":
                p = _strip_quotes(_text(c, self.raw, 300))
                self._edge(self.file_class_nid, "inherits",
                           self._ext(p), node, "resolved", "resolved",
                           0.9, {"superclass": p, "path": True})
                return

    def _const_var(self, node, kind: str):
        nm = ""
        for c in node.children:
            if c.type == "name":
                nm = _text(c, self.raw, 120)
                break
        if not nm:
            return
        qn = ""
        for n in self.res.nodes:
            if n.id == self.file_class_nid:
                qn = n.qualified_name or ""
                break
        nid = self._sym(kind, f"{qn}.{nm}", node)
        if kind == "variable":
            self.funcs[nm] = nid
        self._defines(self.file_class_nid, nid, node)
        kids = list(node.children)
        for i, c in enumerate(kids):
            if c.type == "=" and i + 1 < len(kids):
                value = kids[i + 1]
                if self._preload_import(value, nid, nm):
                    break
                self.bodies.append((nid, value))
                break

    def _preload_import(self, value, src: str, alias: str) -> bool:
        """`const D = preload("res://...")`: imports edge sourced at the
        const symbol (so the resolver binds the alias), and the value
        needs no body walk (a string literal holds no calls)."""
        if value.type != "call":
            return False
        base = ""
        for c in value.children:
            if c.type == "identifier" and not base:
                base = _text(c, self.raw, 120)
        if base != "preload":
            return False
        spec = self._string_arg(value)
        if not spec:
            return False
        self._edge(src, "imports", self._ext(spec), value,
                   "structural", "deterministic", 1.0,
                   {"specifier": spec, "preload": True, "alias": alias,
                    "dynamic": False})
        return True

    def _function(self, node, owner: str, qn: str):
        nm = ""
        for c in node.children:
            if c.type == "name":
                nm = _text(c, self.raw, 120)
                break
        if not nm:
            return
        attrs: dict = {}
        for c in node.children:
            if c.type == "static_keyword":
                attrs["static"] = True
        nid = self._sym("method", f"{qn}.{nm}", node, attrs)
        if owner == self.file_class_nid:
            # File-level methods are bare-callable; inner-class methods
            # are not (no funcs pollution: `aid()` must not bind to
            # `Helper.aid`).
            self.funcs[nm] = nid
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "body":
                self.bodies.append((nid, c))

    def _inner_class(self, node, owner: str, qn: str):
        nm = ""
        for c in node.children:
            if c.type == "name":
                nm = _text(c, self.raw, 120)
                break
        if not nm:
            return
        full = f"{qn}.{nm}"
        nid = self._sym("class", full, node)
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "class_body":
                for m in c.children:
                    if m.type == "function_definition":
                        self._function(m, nid, full)

    def _enum(self, node, owner: str, qn: str):
        nm = ""
        for c in node.children:
            if c.type == "name":
                nm = _text(c, self.raw, 120)
                break
        if not nm:
            return
        full = f"{qn}.{nm}"
        nid = self._sym("class", full, node, {"enum": True})
        self._defines(owner, nid, node)
        for c in node.children:
            if c.type == "enumerator_list":
                for g in c.children:
                    if g.type == "enumerator":
                        for h in g.children:
                            if h.type == "identifier":
                                eid = self._sym(
                                    "constant",
                                    f"{full}.{_text(h, self.raw, 120)}",
                                    h)
                                self._defines(nid, eid, h)
                                break

    # ------------------------------------------------------------ pass 2
    def _calls_in(self, node, owner: str):
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call":
                self._call(n, owner)
            elif t == "attribute":
                if not self._attribute(n, owner):
                    stack.extend(n.children)
                else:
                    # Shaped: only the argument subtrees still need a
                    # walk (nested calls like m.f(g(1)) live there).
                    for c in n.children:
                        if c.type == "attribute_call":
                            for g in c.children:
                                if g.type == "arguments":
                                    stack.append(g)
                continue
            elif t == "lambda":
                pass  # owner-approximate descent (documented)
            elif t in ("function_definition", "class_definition") and \
                    n is not node:
                # Pass-1 collects only file-level bodies; an inner
                # definition here would lose its calls if skipped, so
                # descend owner-approximately (documented; Godot code
                # keeps inner classes at file level, where they ARE
                # collected with their own owners).
                pass
            stack.extend(n.children)

    def _string_arg(self, node) -> str:
        for c in node.children:
            if c.type == "arguments":
                strs = [g for g in c.children if g.type == "string"]
                if len(strs) == 1:
                    return _strip_quotes(_text(strs[0], self.raw, 300))
        return ""

    def _call(self, node, owner: str):
        base = ""
        args = None
        for c in node.children:
            if c.type == "identifier" and not base:
                base = _text(c, self.raw, 120)
            elif c.type == "arguments":
                args = c
        if not base:
            return
        if base == "preload":
            spec = self._string_arg(node)
            if spec:
                self._edge(self.mid, "imports", self._ext(spec), node,
                           "structural", "deterministic", 1.0,
                           {"specifier": spec, "preload": True,
                            "dynamic": False})
            return
        arity = 0
        if args is not None:
            arity = sum(1 for g in args.children
                        if g.type not in ("(", ")", ","))
        if base in self.funcs:
            tgt = self.funcs[base]
            n = next((m for m in self.res.nodes if m.id == tgt), None)
            if n is not None and n.type == "variable":
                self._edge(owner, "calls_unresolved",
                           self._ext(base), node, "structural",
                           "deterministic", 1.0,
                           {"callee_kind": "function", "base": base,
                            "arity": arity})
            else:
                self._edge(owner, "calls", tgt, node, "resolved",
                           "resolved", 0.9, {"arity": arity})
        else:
            self._edge(owner, "calls_unresolved", self._ext(base),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "function", "base": base,
                        "arity": arity})

    def _attribute(self, node, owner: str) -> bool:
        """(base, member) from attribute nodes. Returns True when the
        call was shaped (children skipped); nested attributes fall
        through as complex."""
        base = ""
        call = None
        nested = False
        for c in node.children:
            if c.type == "identifier" and not base:
                base = _text(c, self.raw, 120)
            elif c.type == "attribute_call" and call is None:
                call = c
            elif c.type == "attribute":
                nested = True
        if call is None:
            return False
        member = ""
        args = None
        for c in call.children:
            if c.type == "identifier" and not member:
                member = _text(c, self.raw, 120)
            elif c.type == "arguments":
                args = c
        if not member:
            return False
        arity = 0
        if args is not None:
            arity = sum(1 for g in args.children
                        if g.type not in ("(", ")", ","))
        if nested or not base:
            self._edge(owner, "calls_unresolved", self._ext(member),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "complex", "member": member,
                        "arity": arity})
            return True
        if member == "new":
            self._edge(owner, "instantiates", self._ext(base), node,
                       "resolved", "resolved", 0.9, {"created": base})
            return True
        if base in ("self", "super"):
            self._edge(owner, "calls_unresolved", self._ext(member),
                       node, "structural", "deterministic", 1.0,
                       {"callee_kind": "this" if base == "self"
                        else "super", "member": member, "arity": arity})
            return True
        self._edge(owner, "calls_unresolved",
                   self._ext(base + "." + member), node,
                   "structural", "deterministic", 1.0,
                   {"callee_kind": "selector", "base": base,
                    "member": member, "arity": arity})
        return True


class _RubyVisitor:
    """Ruby walker (one pass; Ruby resolves names at run time, so binding
    is left to graph/typed_callbind.py and ruby_resolve.py).

    Symbols: `class`/`module` (modules are `class` nodes with `module`
    attr: mixins), `def m` -> method, `def self.m` -> method with
    `singleton`, top-level `def` -> function. Qualified names follow the
    lexical nesting (`Admin::UsersController#index` -> `Admin.UsersController.index`),
    not the file: Ruby's open classes reopened in several files share one
    class identity.

    Calls (only `call` nodes; a bare `foo` is ambiguous with a local and
    is not guessed):
      `foo(..)` / `self.foo` inside a class -> receiver_owner = the class
      `Const.m(..)` / `A::B.m(..)`          -> receiver_static / qualifier
      `Const.new(..)`                       -> instantiates Const
      `x.m(..)` with `x = Const.new` earlier in the method -> receiver_type
      `require` / `require_relative` / `load` -> imports
      `include` / `extend` / `prepend` Const in a class -> implements (mixin)
      `class A < B` -> inherits B
    """

    _DSL = {"attr_accessor", "attr_reader", "attr_writer", "private", "protected", "public",
            "module_function", "private_constant", "alias_method", "define_method", "raise",
            "puts", "p", "pp", "print", "lambda", "proc", "loop", "catch", "throw", "block_given?",
            "require", "require_relative", "load", "include", "extend", "prepend", "using"}

    def __init__(self, ext, res, ev, repo_id, project_id, rel_path, mqp, mid, raw):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo_id = repo_id
        self.project_id = project_id
        self.rel = rel_path
        self.mod = mqp
        self.mid = mid
        self.raw = raw
        self.method = "ts-ruby"
        self.seen_nodes: set = set()

    def _sym(self, kind: str, qname: str, node, attrs=None) -> str:
        nid = canonical_symbol_id(self.repo_id, "ruby", kind, qname, "")
        if nid not in self.seen_nodes:
            self.seen_nodes.add(nid)
            self.res.nodes.append(Node(nid, kind, qname.split(".")[-1], qname, self.project_id, self.repo_id,
                                       self.rel, _loc(node), "ruby", "", dict(attrs or {}), [self.ev.id],
                                       schema_version=2))
        return nid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str, cclass: str, score, attrs=None):
        eid = canonical_edge_id(src, rel, tgt, self.rel)
        self.res.edges.append(Edge(eid, src, tgt, rel, etype, cclass, score, [self.ev.id], self.method,
                                   UNIV_TS_VERSION, file_path=self.rel, start_line=_call_line(node, rel),
                                   attributes=attrs or {}))
        return eid

    def _ext(self, name: str) -> str:
        return f"ext:{self.repo_id}:import:{name}"

    def _const(self, node) -> str:
        """`Foo` / `A::B` / `::A::B` -> dotted `A.B`."""
        if node is None:
            return ""
        if node.type == "constant":
            return _text(node, self.raw, 120)
        if node.type == "scope_resolution":
            parts = [_text(c, self.raw, 120) for c in node.children if c.type == "constant"]
            inner = next((c for c in node.children if c.type == "scope_resolution"), None)
            prefix = self._const(inner) if inner is not None else ""
            return ".".join(x for x in ([prefix] if prefix else []) + parts if x)
        return ""

    # ------------------------------------------------------------ walk
    def walk_module(self, root):
        self._walk(root, [], self.mid, None, {})

    def _walk(self, node, scope: list, owner: str, cls_q, locals_: dict):
        for c in node.children:
            t = c.type
            if t in ("class", "module"):
                name = self._const(c.child_by_field_name("name"))
                if not name:
                    continue
                qn = ".".join(scope + [name])
                nid = self._sym("class", qn, c, {"module": True} if t == "module" else None)
                self._edge(self.mid if not scope else owner, "defines", nid, c, "structural", "deterministic", 1.0)
                sup = c.child_by_field_name("superclass")
                if sup is not None:
                    base = next((g for g in sup.children if g.type in ("constant", "scope_resolution")), None)
                    bname = self._const(base)
                    if bname:
                        self._edge(nid, "inherits", self._ext(bname), sup, "resolved", "resolved", 0.9,
                                   {"superclass": bname.split(".")[-1], "qualified": bname})
                body = c.child_by_field_name("body")
                if body is not None:
                    # class-body bare names are calls too (`skip_authentication`)
                    self._walk(body, scope + name.split("."), nid, qn, {"__known__": set()})
            elif t in ("method", "singleton_method"):
                nm = c.child_by_field_name("name")
                name = _text(nm, self.raw, 120) if nm is not None else ""
                if not name:
                    continue
                single = t == "singleton_method"
                if cls_q:
                    qn = f"{cls_q}.{name}"
                    kind = "method"
                else:
                    qn = f"{self.mod}.{name}"
                    kind = "function"
                params = c.child_by_field_name("parameters")
                pmin = pmax = 0
                if params is not None:
                    for pp in params.children:
                        # `*rest` makes the maximum unbounded (None) for good:
                        # parameters after it (`def f(*a, b)`) only raise the minimum
                        if pp.type == "identifier":
                            pmin += 1
                            pmax = None if pmax is None else pmax + 1
                        elif pp.type in ("optional_parameter", "keyword_parameter"):
                            pmax = None if pmax is None else pmax + 1
                        elif pp.type in ("splat_parameter", "hash_splat_parameter"):
                            pmax = None
                attrs = {"params_min": pmin, "params_max": pmax}
                if single:
                    attrs["singleton"] = True
                mid = self._sym(kind, qn, c, attrs)
                self._edge(owner, "defines", mid, c, "structural", "deterministic", 1.0)
                known = set()
                if params is not None:
                    for pp in params.children:
                        nm_ = pp if pp.type == "identifier" else pp.child_by_field_name("name")
                        if nm_ is not None:
                            known.add(_text(nm_, self.raw, 120))
                self._walk(c, scope, mid, cls_q, {"__known__": known})
            elif t == "call":
                self._call(c, owner, cls_q, locals_)
                self._walk(c, scope, owner, cls_q, locals_)
            elif t == "identifier" and cls_q is not None and "__known__" in locals_ \
                    and node.type in self._VALUE_PARENTS:
                # `save_record` / `total = total_amount`: a bare name that is
                # neither a parameter nor assigned in the method is a call
                nm_ = _text(c, self.raw, 120)
                if nm_ not in locals_["__known__"] and nm_ not in self._DSL and nm_ not in ("nil", "true", "false"):
                    self._edge(owner, "calls_unresolved", self._ext(nm_), c, "structural", "deterministic", 1.0,
                               {"callee_kind": "function", "base": nm_, "member": nm_, "arity": 0,
                                "receiver_owner": cls_q, "bare": True})
            elif t in ("block_parameters", "lambda_parameters") and "__known__" in locals_:
                for pp in c.children:
                    if pp.type == "identifier":
                        locals_["__known__"].add(_text(pp, self.raw, 120))
            elif t in ("exception_variable", "for") and "__known__" in locals_:
                # `rescue => e` / `for x in xs`: locals, not calls
                pat = c if t == "exception_variable" else c.child_by_field_name("pattern")
                for g in (pat.children if pat is not None else []):
                    if g.type == "identifier":
                        locals_["__known__"].add(_text(g, self.raw, 120))
                if pat is not None and pat.type == "identifier":
                    locals_["__known__"].add(_text(pat, self.raw, 120))
                self._walk(c, scope, owner, cls_q, locals_)
            elif t in ("assignment", "operator_assignment"):
                left, right = c.child_by_field_name("left"), c.child_by_field_name("right")
                if left is not None and left.type == "identifier" and "__known__" in locals_:
                    locals_["__known__"].add(_text(left, self.raw, 120))
                if left is not None and left.type == "left_assignment_list" and "__known__" in locals_:
                    # `a, b = pair`
                    for g in left.children:
                        nm_ = g if g.type == "identifier" else next(
                            (x for x in g.children if x.type == "identifier"), None) \
                            if g.type == "rest_assignment" else None
                        if nm_ is not None:
                            locals_["__known__"].add(_text(nm_, self.raw, 120))
                if right is not None:
                    self._walk_value(right, scope, owner, cls_q, locals_)
                if left is not None and left.type == "identifier" and right is not None and right.type == "call":
                    recv = right.child_by_field_name("receiver")
                    meth = right.child_by_field_name("method")
                    if meth is not None and _text(meth, self.raw, 20) == "new" and \
                            recv is not None and recv.type in ("constant", "scope_resolution"):
                        locals_[_text(left, self.raw, 120)] = self._const(recv)
                    else:
                        locals_.pop(_text(left, self.raw, 120), None)
                if right is None:
                    self._walk(c, scope, owner, cls_q, locals_)
            else:
                self._walk(c, scope, owner, cls_q, locals_)

    # parents under which a bare identifier is evaluated as a value
    _VALUE_PARENTS = {"body_statement", "parenthesized_statements", "then", "else", "elsif", "begin",
                      "binary", "argument_list", "return", "if_modifier", "unless_modifier", "if",
                      "unless", "while", "until", "array", "pair", "interpolation", "conditional",
                      "ensure", "rescue_modifier", "do_block", "block", "block_body", "element_reference",
                      "unary", "parenthesized_expression"}

    def _walk_value(self, node, scope, owner, cls_q, locals_):
        """The right side of an assignment: itself may be a bare call."""
        if node.type == "identifier" and cls_q is not None and "__known__" in locals_:
            wrapper = type("W", (), {"children": [node], "type": "body_statement"})()
            self._walk(wrapper, scope, owner, cls_q, locals_)
        else:
            self._walk(type("W", (), {"children": [node], "type": "assignment"})(), scope, owner, cls_q, locals_)

    def _call(self, node, owner: str, cls_q, locals_: dict):
        recv = node.child_by_field_name("receiver")
        meth = node.child_by_field_name("method")
        if meth is None:
            return
        name = _text(meth, self.raw, 120)
        args = node.child_by_field_name("arguments")
        arity = sum(1 for g in args.children if g.is_named) if args is not None else 0
        if recv is None:
            if name in ("require", "require_relative", "load") and args is not None:
                s = next((g for g in args.children if g.type == "string"), None)
                spec = "".join(_text(x, self.raw, 300) for x in s.children if x.type == "string_content") \
                    if s is not None else ""
                if spec:
                    self._edge(self.mid, "imports", self._ext(spec), node, "structural", "deterministic", 1.0,
                               {"specifier": spec, "relative": name == "require_relative", "dynamic": False})
                return
            if name in ("include", "extend", "prepend") and cls_q and args is not None:
                for g in args.children:
                    cname = self._const(g)
                    if cname:
                        cid = canonical_symbol_id(self.repo_id, "ruby", "class", cls_q, "")
                        self._edge(cid, "implements", self._ext(cname), node, "resolved", "resolved", 0.9,
                                   {"interface": cname.split(".")[-1], "qualified": cname, "mixin": name})
                return
            if name in self._DSL:
                return
            attrs = {"callee_kind": "function", "base": name, "member": name, "arity": arity}
            if cls_q:
                attrs["receiver_owner"] = cls_q          # implicit self
            self._edge(owner, "calls_unresolved", self._ext(name), node, "structural", "deterministic", 1.0, attrs)
            return
        attrs = {"callee_kind": "selector", "member": name, "arity": arity}
        if recv.type == "self":
            if cls_q:
                attrs["receiver_owner"] = cls_q
            attrs["base"] = "self"
        elif recv.type in ("constant", "scope_resolution"):
            cname = self._const(recv)
            if name == "new":
                self._edge(owner, "instantiates", self._ext(cname.split(".")[-1]), node, "resolved", "resolved",
                           0.9, {"created": cname.split(".")[-1], "qualified": cname})
                return
            attrs["base"] = cname
            if "." in cname:
                attrs["qualifier"] = cname
            else:
                attrs["receiver_static"] = cname
        elif recv.type == "identifier":
            v = _text(recv, self.raw, 120)
            attrs["base"] = v
            if v in locals_:
                attrs["receiver_type"] = locals_[v].split(".")[-1]
            elif cls_q is not None and "__known__" in locals_ and v not in locals_["__known__"]                     and v not in self._DSL and v not in ("nil", "true", "false"):
                # `to_account.entries` / `csv_rows.map`: the receiver, neither a
                # parameter nor assigned, is itself a call on self
                self._edge(owner, "calls_unresolved", self._ext(v), recv, "structural", "deterministic", 1.0,
                           {"callee_kind": "function", "base": v, "member": v, "arity": 0,
                            "receiver_owner": cls_q, "bare": True})
        else:
            attrs["callee_kind"] = "complex"
        self._edge(owner, "calls_unresolved", self._ext(name), node, "structural", "deterministic", 1.0, attrs)
