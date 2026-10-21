"""Shared JavaScript/TypeScript tree-sitter extractor (Phase 3).

One core, parameterized by ``language_id`` (``javascript`` | ``typescript`` |
``tsx``); TS-only syntax is guarded, never copied into a parallel module.
Fits the established pipeline: returns :class:`ExtractionResult` with canonical
IDs, per-file evidence, warnings and parser metadata. Cross-file linking
(import targets, re-export chains, call targets) is intentionally left as
unresolved placeholders here and bound by ``js_resolve`` at service stage,
where the repository file list and tsconfig state are available.
"""
from __future__ import annotations
from . import tree_sitter as ts_adapter
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import (
    file_node_id, canonical_symbol_id, canonical_edge_id, content_hash,
    normalize_qualified,
)
from ...core.provenance import make_evidence

JSTS_VERSION = "20"  # v20: undeclared return types inferred from the body; v19: TS field paths from typed locals/params; v18: callback params typed (annotation or callee callback type); v17: untyped locals marked receiver_local; v16: `import {x} from m; export {x}` re-exports x; v15: `T | undefined` and `new C<T>()` type locals; v14: locals of enclosing test blocks type nested calls; v13: generic type annotations type locals (Router<string>); v11: locals of test callbacks type the calls inside them; v10: module-scope locals (and those in its closures) typed; v9: export default {..} members, module.exports.x = y; v8: `export * as ns from`, test names not callable; v7: member-assigned functions, forward same-file calls, require().x; v5: arity/chain/static receiver annotations (v4: typed params)


def module_qname(rel_path: str) -> str:
    """Repo-relative module identity: strip extension, collapse trailing /index,
    dot-separated like the Python extractor (``src/lib/util`` -> ``src.lib.util``)."""
    p = rel_path
    for ext in (".d.ts", ".d.mts", ".d.cts", ".tsx", ".ts", ".mts", ".cts",
                ".jsx", ".js", ".mjs", ".cjs"):
        if p.lower().endswith(ext):
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


def _bindings_index(bindings: dict, spec: str) -> dict:
    """Local-name -> binding record for one import's bindings (central index)."""
    out: dict = {}
    if not isinstance(bindings, dict):
        return out
    if bindings.get("default"):
        out[bindings["default"]] = {"kind": "default", "imported": "default",
                                    "specifier": spec, "type_only": False}
    for nb in bindings.get("named", []) or []:
        if isinstance(nb, dict) and nb.get("local"):
            out[nb["local"]] = {"kind": "named", "imported": nb.get("imported", ""),
                                "specifier": spec, "type_only": False}
    if bindings.get("namespace"):
        out[bindings["namespace"]] = {"kind": "namespace", "imported": "*",
                                      "specifier": spec, "type_only": False}
    return out


def _merge_bindings(old: dict, new: dict) -> dict:
    """Deterministic union of two binding maps for one specifier (first wins)."""
    old = dict(old or {})
    new = new or {}
    if new.get("default") and not old.get("default"):
        old["default"] = new["default"]
    if new.get("namespace") and not old.get("namespace"):
        old["namespace"] = new["namespace"]
    seen = {(nb.get("imported"), nb.get("local")) for nb in old.get("named", [])
            if isinstance(nb, dict)}
    for nb in new.get("named", []) or []:
        if isinstance(nb, dict) and (nb.get("imported"), nb.get("local")) not in seen:
            old.setdefault("named", []).append(nb)
            seen.add((nb.get("imported"), nb.get("local")))
    return old


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"', "`"):
        return s[1:-1]
    return s


class JsTsExtractor:
    name = "jsts"
    version = JSTS_VERSION

    def __init__(self, language_id: str = "javascript"):
        if language_id not in ("javascript", "typescript", "tsx"):
            raise ValueError(f"unsupported language: {language_id}")
        self.language_id = language_id
        self.is_ts = language_id in ("typescript", "tsx")

    # ------------------------------------------------------------------ main
    def extract(self, rel_path: str, text: str, repo_id: str, project_id: str) -> ExtractionResult:
        lang = self.language_id
        res = ExtractionResult(extractor=f"jsts-{lang}", extractor_version=self.version)
        raw = text.encode("utf-8", errors="ignore")
        ev = make_evidence(rel_path, text[:300], f"jsts-{lang}", raw)
        res.evidence.append(ev)
        parsed = ts_adapter.parse(lang, raw)
        if not parsed.ok:
            # Controlled error (missing package / init failure / oversize): a
            # partial file-node result, never a false deep-success.
            res.warnings.append(f"jsts {lang} unavailable for {rel_path}: {parsed.reason}")
            res.partial = True
            fid = file_node_id(repo_id, rel_path)
            res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id,
                                  rel_path, Location(1, 0, 1, 0), lang, content_hash(raw),
                                  {"parser": {"backend": "none", "status": "unavailable",
                                              "reason": parsed.reason[:200]}},
                                  [ev.id], schema_version=2))
            return res
        mqp = module_qname(rel_path)
        fid = file_node_id(repo_id, rel_path)
        is_dts = rel_path.lower().endswith((".d.ts", ".d.mts", ".d.cts"))
        fattrs = {"parser": {"backend": "tree-sitter", "language": lang,
                             "status": parsed.status, "errors": parsed.error_count,
                             "missing": parsed.missing_count}}
        if is_dts:
            fattrs["declaration_file"] = True
        if parsed.status == "partial":
            res.warnings.append(
                f"{rel_path}: recovered from {parsed.error_count} syntax errors "
                f"({parsed.missing_count} missing nodes); coverage partial")
            res.partial = True
            fattrs["parse"] = "partial"
            fattrs["parser_error_samples"] = parsed.error_nodes[:5]
        res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                              Location(1, 0, max(1, len(text.splitlines())), 0), lang,
                              content_hash(raw), fattrs, [ev.id], schema_version=2))
        mid = file_node_id(repo_id, rel_path + "#module")
        res.nodes.append(Node(mid, "module", mqp, mqp, project_id, repo_id, rel_path,
                              Location(1, 0, 1, 0), lang, "", {}, [ev.id], schema_version=2))
        res.edges.append(Edge(canonical_edge_id(fid, "contains", mid, rel_path),
                              fid, mid, "contains", "structural", "deterministic", 1.0,
                              [ev.id], f"jsts-{lang}", self.version, file_path=rel_path))
        visitor = _V(self, res, ev, repo_id, project_id, rel_path, mqp, mid, raw)
        visitor.walk_module(parsed.tree.root_node)
        try:
            from .typed_params import annotate_receivers
            annotate_receivers(lang, parsed.tree.root_node, raw, res)
        except Exception as exc:  # annotation is additive; never fail a parse
            res.warnings.append(f"receiver typing skipped: {type(exc).__name__}")
        return res


class _V:
    """Single-pass declaration walk with a lexical scope stack.

    Qualified names mirror the Python extractor convention:
    ``<module_qname>.<scope...>.<name>`` so identity semantics stay uniform.
    Every ``_stmt``-family handler returns the directly-declared node ids so
    ``export`` links exactly the declared symbols (never their members).
    """

    def __init__(self, ext, res, ev, repo, proj, rel, mod_qname, mod_id, raw: bytes):
        self.ext = ext
        self.res = res
        self.ev = ev
        self.repo = repo
        self.proj = proj
        self.rel = rel
        self.mod = mod_qname
        self.mod_id = mod_id
        self.raw = raw
        self.lang = ext.language_id
        self.is_ts = ext.is_ts
        self.scope: list[str] = [mod_qname]
        self.class_stack: list[str] = []
        self.scope_counts: dict[tuple, int] = {}
        self.local_funcs: dict[str, str] = {}  # qualified -> node id (same-module calls)
        self.local_symbols: dict[str, str] = {}  # qualified -> node id, all kinds
        self.import_bindings: dict[str, dict] = {}  # local name -> binding record
        self.test_frameworks: set[str] = set()  # jest | vitest | mocha (import-observed)
        self.describe_stack: list[str] = []  # enclosing suite node ids
        self._anon_test = 0
        # Bare calls with no declaration seen *yet*: retried after the walk
        # so hoisted / later-defined same-file functions still bind.
        self.pending_local: list[tuple] = []

    # ------------------------------------------------------------- helpers
    def _qid(self, name: str) -> str:
        return ".".join(self.scope + [name])

    def _named(self, node, field: str = "name"):
        n = node.child_by_field_name(field)
        if n is None:
            for c in node.children:
                if c.type in ("identifier", "type_identifier", "property_identifier"):
                    return _text(c, self.raw, 120)
            return ""
        return _text(n, self.raw, 120)

    def _jsdoc(self, node) -> str:
        parent = node.parent
        if parent is None:
            return ""
        try:
            idx = parent.children.index(node)
        except ValueError:
            return ""
        if idx > 0:
            prev = parent.children[idx - 1]
            if prev.type == "comment":
                t = _text(prev, self.raw, 400)
                if t.startswith("/**"):
                    return t
        return ""

    def _disc(self, kind: str, name: str, sig: str) -> str:
        key = (kind, tuple(self.scope), name)
        n = self.scope_counts.get(key, 0) + 1
        self.scope_counts[key] = n
        if n > 1:
            sig = (sig + f"#overload{n}") if sig else f"#overload{n}"
        return sig

    def _add(self, node, kind: str, ntype: str, name: str, sig: str = "",
             extra: dict | None = None, parent: str | None = None) -> tuple[str, str]:
        q = self._qid(name)
        sig = self._disc(kind, name, sig)
        nid = canonical_symbol_id(self.repo, self.lang, kind, q, sig)
        doc = self._jsdoc(node)
        attrs = {"enclosing_scope": ".".join(self.scope)}
        if sig:
            attrs["signature"] = sig
        if doc:
            attrs["doc"] = doc
        if extra:
            attrs.update(extra)
        self.res.nodes.append(Node(
            nid, ntype, name, normalize_qualified(q), self.proj, self.repo, self.rel,
            _loc(node), self.lang, "", attrs, [self.ev.id], schema_version=2))
        if parent is None:
            parent = self.class_stack[-1] if self.class_stack and kind == "method" else self.mod_id
        self.res.edges.append(Edge(
            canonical_edge_id(parent, "defines", nid, self.rel), parent, nid,
            "defines", "structural", "deterministic", 1.0, [self.ev.id],
            f"jsts-{self.lang}", JSTS_VERSION,
            file_path=self.rel, start_line=node.start_point[0] + 1))
        if kind in ("function", "method"):
            self.local_funcs[q] = nid
        self.local_symbols[q] = nid
        return nid, q

    def _ext(self, name: str, short: str | None = None) -> str:
        eid = f"ext:{self.repo}:{name[:150]}"
        if not any(n.id == eid for n in self.res.nodes):
            self.res.nodes.append(Node(eid, "external_symbol", (short or name)[:120],
                                       name[:200], self.proj, self.repo, "", Location(),
                                       self.lang, "", {}, [self.ev.id], schema_version=2))
        return eid

    def _edge(self, src: str, rel: str, tgt: str, node, etype: str, cclass: str,
              score, attrs: dict | None = None, method: str | None = None):
        self.res.edges.append(Edge(
            canonical_edge_id(src, rel, tgt, self.rel), src, tgt, rel, etype, cclass,
            score, [self.ev.id], method or f"jsts-{self.lang}", JSTS_VERSION,
            file_path=self.rel, start_line=node.start_point[0] + 1,
            attributes=attrs or {}))

    # ------------------------------------------------------- module walk
    def walk_module(self, root):
        for stmt in root.children:
            self._stmt(stmt, top=True)
        self._bind_pending()

    def _bind_pending(self):
        """Upgrade forward-referenced bare calls to same-file edges."""
        if not self.pending_local:
            return
        for idx, scope, member, owner_id, line in self.pending_local:
            tgt = None
            for i in range(len(scope), 0, -1):
                cand = ".".join(scope[:i] + (member,))
                if cand in self.local_funcs:
                    tgt = self.local_funcs[cand]
                    break
            if tgt is None or tgt == owner_id:
                continue
            old = self.res.edges[idx]
            self.res.edges[idx] = Edge(
                canonical_edge_id(owner_id, "calls", tgt, self.rel), owner_id, tgt,
                "calls", "resolved", "resolved", 0.9, list(old.provenance_ids),
                f"jsts-{self.lang}-local", JSTS_VERSION,
                file_path=self.rel, start_line=line,
                attributes={"occurrences": (old.attributes or {}).get("occurrences", [])})
        used = {e.target for e in self.res.edges}
        self.res.nodes = [n for n in self.res.nodes
                          if not (n.type == "external_symbol" and n.id not in used)]

    @staticmethod
    def _is_fn_member(p) -> bool:
        if p.type == "method_definition":
            return True
        if p.type == "pair":
            k, v = p.child_by_field_name("key"), p.child_by_field_name("value")
            return k is not None and v is not None and k.type == "property_identifier" \
                and v.type in ("function_expression", "arrow_function")
        return False

    def _object_members(self, obj: str, objnode) -> None:
        """Methods of an object literal bound to a name (`module.exports =
        { get(f) {..}, has: function () {..} }`): `this.get()` inside binds
        through the same object-method table as `obj.m = function`."""
        for p in objnode.children:
            if p.type == "method_definition":
                m = self._prop_name(p)
                if m:
                    self._member_fn(p, obj, m, p, f"{obj}.{m}")
            elif p.type == "pair":
                k = p.child_by_field_name("key")
                v = p.child_by_field_name("value")
                if k is not None and v is not None and k.type == "property_identifier" \
                        and v.type in ("function_expression", "arrow_function"):
                    m = _text(k, self.raw, 120)
                    self._member_fn(p, obj, m, v, f"{obj}.{m}")

    def _member_fn(self, node, obj: str, member: str, fn, assigned: str):
        """`obj.m = function(){}` / `X.prototype.m = ...` / `exports.m = ...`.

        Declared as a method of `obj` (qualified `mod.obj.m`) so `this.m()`
        binds through the class-method table; kept out of the bare-name table
        so a body calling an imported `m()` can never bind to itself. With no
        obj (`exports.m`) it is a module-level function."""
        saved = self.scope
        self.scope = [self.mod] + ([obj] if obj else [])
        try:
            is_async = any(k.type == "async" for k in fn.children)
            kind = "method" if obj else "function"
            sig = f"params={self._param_count(fn)},async={int(is_async)},member=1"
            had = ".".join(self.scope + [member]) in self.local_funcs
            nid, q = self._add(node, kind, kind, member, sig,
                               {"assigned_member": assigned}, parent=self.mod_id)
            if not had:
                self.local_funcs.pop(q, None)
            self.scope.append(member)
            body = fn.child_by_field_name("body")
            if body is not None and body.type == "statement_block":
                self._body(body, nid, q)
            else:
                self._calls_in(fn, nid, q)
            self.scope.pop()
        finally:
            self.scope = saved
        return nid

    def _stmt(self, node, top: bool = False) -> list[str]:
        """Handle one statement; return directly-declared node ids."""
        t = node.type
        if t in ("function_declaration", "generator_function_declaration"):
            nid, _ = self._fn(node, "method" if self.class_stack else "function")
            return [nid]
        if t == "function_signature" and self.is_ts:
            nid, _ = self._fn_sig(node)
            return [nid]
        if t == "class_declaration":
            return [self._class(node, abstract=False)]
        if t == "abstract_class_declaration" and self.is_ts:
            return [self._class(node, abstract=True)]
        if t == "interface_declaration" and self.is_ts:
            return [self._interface(node)]
        if t == "type_alias_declaration" and self.is_ts:
            return [self._type_alias(node)]
        if t == "enum_declaration" and self.is_ts:
            return [self._enum(node)]
        if t == "internal_module" and self.is_ts:
            return [self._namespace(node)]
        if t in ("lexical_declaration", "variable_declaration"):
            owner = self.mod_id if top else (self.class_stack[-1] if self.class_stack else None)
            return self._vars(node, top, owner)
        if t == "export_statement":
            self._export(node)
            return []
        if t == "import_statement":
            self._import(node)
            return []
        if t == "expression_statement":
            # Inside a suite body the owner is the suite, not the module.
            if self.describe_stack:
                self._top_expr(node, owner_id=self.describe_stack[-1],
                               owner_q=".".join(self.scope))
            else:
                self._top_expr(node)
            return []
        if t == "ambient_declaration" and self.is_ts:
            out: list[str] = []
            for c in node.children:
                if c.type not in ("declare", ";", "{", "}"):
                    out.extend(self._stmt(c, top))
            return out
        if t in (";", "{", "}", "comment", "hash_bang_line"):
            return []
        if t == "ERROR" or t == "MISSING":
            # Error recovery: salvage declarations nested in error regions.
            return self._recover(node, top)
        self._calls_in(node, self.mod_id if top else self._owner(), self.mod)
        return []

    def _recover(self, node, top: bool) -> list[str]:
        """Descend into ERROR/MISSING subtrees and dispatch anything
        declaration-shaped. Returns salvaged node ids."""
        out: list[str] = []
        for c in node.children:
            ct = c.type
            if ct in ("ERROR", "MISSING"):
                out.extend(self._recover(c, top))
            elif ct in ("function_declaration", "generator_function_declaration",
                        "class_declaration", "lexical_declaration",
                        "variable_declaration", "export_statement", "import_statement",
                        "interface_declaration", "type_alias_declaration",
                        "enum_declaration", "internal_module", "ambient_declaration",
                        "abstract_class_declaration", "function_signature"):
                out.extend(self._stmt(c, top))
            elif ct == "function_expression":
                nm = self._named(c)
                if nm:
                    nid, q = self._fn_named_expr(c, nm)
                    out.append(nid)
                else:
                    out.extend(self._recover(c, top))
            else:
                # opaque error content: scan for calls, salvaging named functions
                self._calls_in(c, self.mod_id if top else self._owner(), self.mod,
                               recover=True)
        return out

    def _fn_named_expr(self, node, name: str):
        sig = f"params={self._param_count(node)},recovered=1"
        nid, q = self._add(node, "function", "function", name, sig,
                           {"recovered_from_error": True})
        self.scope.append(name)
        body = next((c for c in node.children if c.type == "statement_block"), None)
        if body is not None:
            self._body(body, nid, q)
        self.scope.pop()
        return nid, q

    def _owner(self) -> str:
        if self.class_stack:
            return self.class_stack[-1]
        return self.mod_id

    # ------------------------------------------------------------ functions
    def _param_count(self, node) -> int:
        for c in node.children:
            if c.type == "formal_parameters":
                return sum(1 for p in c.children
                           if p.type not in ("(", ")", ",", "comment"))
        return 0

    def _fn(self, node, kind: str):
        name = self._named(node)
        is_gen = node.type == "generator_function_declaration"
        is_async = any(c.type == "async" for c in node.children)
        sig = f"params={self._param_count(node)},async={int(is_async)},gen={int(is_gen)}"
        if not name:
            name = "<default>"
            sig += ",default"
        nid, q = self._add(node, kind,
                           "method" if kind == "method" else "function", name, sig,
                           {"async": is_async, "generator": is_gen} if (is_async or is_gen) else None)
        self.scope.append(name)
        body = next((c for c in node.children if c.type == "statement_block"), None)
        if body is not None:
            self._body(body, nid, q)
        self.scope.pop()
        return nid, q

    def _fn_sig(self, node):
        name = self._named(node) or "<anonymous>"
        sig = f"params={self._param_count(node)},ambient=1"
        return self._add(node, "function", "function", name, sig, {"ambient": True})

    def _body(self, body, owner_id: str, owner_q: str):
        self._param_defaults(body.parent, owner_id, owner_q)
        for stmt in body.children:
            st = stmt.type
            if st in ("ERROR", "MISSING"):
                self._recover(stmt, top=False)
            elif st in ("function_declaration", "generator_function_declaration"):
                self._fn(stmt, "function")
            elif st == "class_declaration":
                self._class(stmt)
            elif st in ("lexical_declaration", "variable_declaration"):
                self._vars(stmt, top=False, owner=owner_id)
            elif st in ("export_statement", "import_statement"):
                self._stmt(stmt)
            else:
                self._calls_in(stmt, owner_id, owner_q)

    def _param_defaults(self, fn, owner_id: str, owner_q: str):
        """Default parameter values run on every call: `(a, t = getArchtype(a)) =>`."""
        params = fn.child_by_field_name("parameters") if fn is not None else None
        for p in (params.children if params is not None else ()):
            dv = p.child_by_field_name("value") if p.type != "assignment_pattern" \
                else p.child_by_field_name("right")
            if dv is not None:
                self._calls_in(dv, owner_id, owner_q)

    # --------------------------------------------------------------- classes
    def _class(self, node, abstract: bool = False):
        name = self._named(node)
        if not name:
            name = "<default>" if any(c.type == "default" for c in node.children) else "<anonymous>"
        nid, q = self._add(node, "class", "class", name, "",
                           {"abstract": True} if abstract else None)
        for c in node.children:
            if c.type == "class_heritage":
                self._heritage(c, nid, node)
        body = next((c for c in node.children if c.type == "class_body"), None)
        if body is None:
            return nid
        self.scope.append(name)
        self.class_stack.append(nid)
        for m in body.children:
            mt = m.type
            if mt == "method_definition":
                self._method(m)
            elif mt in ("field_definition", "public_field_definition"):
                self._field(m, nid)
            elif mt == "abstract_method_signature" and self.is_ts:
                mn = self._prop_name(m) or "<anonymous>"
                self._add(m, "method", "method", mn, "abstract=1", {"abstract": True}, parent=nid)
            elif mt in ("ERROR", "MISSING"):
                self._recover(m, top=False)
            elif mt in ("function_declaration", "generator_function_declaration",
                        "class_declaration", "export_statement", "import_statement",
                        "lexical_declaration", "variable_declaration"):
                # Only reachable inside error-recovered or mis-nested bodies:
                # salvage at module scope, not as class members.
                saved_scope, saved_stack = self.scope, self.class_stack
                self.scope, self.class_stack = [self.mod], []
                try:
                    self._stmt(m, top=True)
                finally:
                    self.scope, self.class_stack = saved_scope, saved_stack
            elif mt not in ("{", "}", ";", "comment", "decorator"):
                self._calls_in(m, nid, q)
        self.class_stack.pop()
        self.scope.pop()
        return nid

    def _prop_name(self, node) -> str:
        for c in node.children:
            if c.type in ("property_identifier", "private_property_identifier", "identifier"):
                return _text(c, self.raw, 120)
        return ""

    def _method(self, node):
        name = self._prop_name(node)
        extra: dict = {}
        prefix = [c.type for c in node.children[:3]]
        if name == "constructor":
            extra["constructor"] = True
        if "get" in prefix:
            extra["accessor"] = "get"
        if "set" in prefix:
            extra["accessor"] = "set"
        if "async" in prefix:
            extra["async"] = True
        if "*" in [_text(c, self.raw, 2) for c in node.children[:4]]:
            extra["generator"] = True
        if not name:
            name = "<computed>"
        sig = f"params={self._param_count(node)}"
        nid, q = self._add(node, "method", "method", name or "<anonymous>", sig,
                           extra or None, parent=self.class_stack[-1])
        self.scope.append(name)
        body = next((c for c in node.children if c.type == "statement_block"), None)
        if body is not None:
            self._body(body, nid, q)
        self.scope.pop()
        return nid

    def _field(self, node, class_id: str):
        name = self._prop_name(node)
        if not name:
            return
        static = any(c.type == "static" for c in node.children)
        init_fn = None
        for c in node.children:
            if c.type in ("function_expression", "arrow_function"):
                init_fn = c
                break
        if init_fn is not None:
            sig = f"params={self._param_count(init_fn)},field=1"
            nid, q = self._add(node, "method", "method", name, sig,
                               {"field_function": True, "static": static}, parent=class_id)
            self.scope.append(name)
            body = init_fn.child_by_field_name("body")
            if body is not None and body.type == "statement_block":
                self._body(body, nid, q)
            else:
                self._param_defaults(init_fn, nid, q)
                self._calls_in(init_fn, nid, q)
            self.scope.pop()
            return
        nid, _ = self._add(node, "variable", "variable", name, "",
                           {"class_field": True, "static": static}, parent=class_id)
        for c in node.children:
            if c.type == "decorator":
                for d in c.children:
                    if d.type == "identifier":
                        dn = _text(d, self.raw, 120)
                        self._edge(nid, "uses", self._ext(dn), node, "structural",
                                   "deterministic", 1.0, {"decorator": dn})
        self._type_refs_of(node, nid)

    def _heritage(self, node, class_id: str, class_node):
        # class_heritage shapes differ by grammar: some wrap the superclass
        # in an `extends_clause` node, others (tree-sitter-javascript 0.25)
        # place it directly under class_heritage next to the `extends`
        # keyword (Phase 35: Puppy/Store emitted nothing). Accept both;
        # per-class dedup keeps hypothetical dual shapes to one edge.
        seen: set[str] = set()

        def _emit(nm: str, attrs: dict):
            if nm and nm not in seen:
                seen.add(nm)
                self._edge(class_id, "inherits", self._ext(nm), class_node,
                           "resolved", "resolved", 0.9, attrs)

        for c in node.children:
            if c.type == "extends_clause":
                for e in c.children:
                    if e.type in ("identifier", "type_identifier", "member_expression",
                                  "generic_type"):
                        nm = _text(e, self.raw, 150)
                        _emit(nm, {"superclass": nm})
            elif c.type in ("identifier", "type_identifier",
                            "member_expression", "generic_type"):
                nm = _text(c, self.raw, 150)
                _emit(nm, {"superclass": nm})
            elif c.type == "implements_clause" and self.is_ts:
                for e in c.children:
                    if e.type in ("type_identifier", "identifier", "generic_type"):
                        nm = _text(e, self.raw, 150)
                        self._edge(class_id, "implements", self._ext(nm), class_node,
                                   "resolved", "resolved", 0.9, {"interface": nm})

    # -------------------------------------------------- TS-only declarations
    def _interface(self, node):
        name = self._named(node) or "<anonymous>"
        nid, q = self._add(node, "interface", "interface", name)
        body = next((c for c in node.children if c.type == "interface_body"), None)
        if body is None:
            return nid
        self.scope.append(name)
        self.class_stack.append(nid)
        for m in body.children:
            if m.type == "method_signature":
                mn = self._prop_name(m) or "<anonymous>"
                self._add(m, "method", "method", mn, "signature=1",
                          {"signature_only": True}, parent=nid)
            elif m.type == "property_signature":
                mn = self._prop_name(m) or "<anonymous>"
                self._add(m, "variable", "variable", mn, "",
                          {"interface_property": True}, parent=nid)
                self._type_refs_of(m, nid)
            elif m.type == "extends_type_clause":
                for e in m.children:
                    if e.type in ("type_identifier", "identifier"):
                        nm = _text(e, self.raw, 150)
                        self._edge(nid, "inherits", self._ext(nm), node,
                                   "resolved", "resolved", 0.9, {"interface_extends": nm})
            elif m.type in ("ERROR", "MISSING"):
                self._recover(m, top=False)
            elif m.type in ("export_statement", "function_declaration",
                            "generator_function_declaration", "class_declaration",
                            "lexical_declaration", "variable_declaration"):
                # Mis-nested declarations land here only when the enclosing
                # interface is itself inside an error region: salvage them at
                # module scope (like _recover) instead of as interface members.
                saved_scope, saved_stack = self.scope, self.class_stack
                self.scope, self.class_stack = [self.mod], []
                try:
                    self._stmt(m, top=True)
                finally:
                    self.scope, self.class_stack = saved_scope, saved_stack
        self.class_stack.pop()
        self.scope.pop()
        return nid

    def _type_alias(self, node):
        name = self._named(node) or "<anonymous>"
        tparams = 0
        for c in node.children:
            if c.type == "type_parameters":
                tparams = sum(1 for cc in c.children if cc.type == "type_parameter")
        nid, _ = self._add(node, "type", "type", name, "",
                           {"type_params": tparams} if tparams else None)
        self._type_refs_of(node, nid, skip_first=True)
        return nid

    def _enum(self, node):
        name = self._named(node) or "<anonymous>"
        nid, q = self._add(node, "enum", "class", name, "", {"enum": True})
        body = next((c for c in node.children if c.type == "enum_body"), None)
        if body is None:
            return nid
        self.scope.append(name)
        for m in body.children:
            if m.type == "enum_assignment":
                mn = self._prop_name(m) or _text(m, self.raw, 60)
                self._add(m, "variable", "variable", mn, "", {"enum_member": True}, parent=nid)
        self.scope.pop()
        return nid

    def _namespace(self, node):
        name = self._named(node) or "<anonymous>"
        nid, q = self._add(node, "namespace", "module", name, "", {"namespace": True})
        block = next((c for c in node.children if c.type == "statement_block"), None)
        if block is None:
            return nid
        self.scope.append(name)
        saved, self.class_stack = self.class_stack, []
        old_mod = self.mod_id
        self.mod_id = nid
        for s in block.children:
            self._stmt(s, top=True)
        self.mod_id = old_mod
        self.class_stack = saved
        self.scope.pop()
        return nid

    def _type_refs_of(self, node, owner_id: str, skip_first: bool = False):
        """Type annotation references: `references` edges, never calls."""
        if not self.is_ts:
            return
        seen: set[str] = set()
        skipped = not skip_first
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "type_identifier" or (
                    n.type == "identifier" and n.parent is not None
                    and n.parent.type in ("type_annotation", "generic_type", "type_arguments")):
                if not skipped:
                    skipped = True
                else:
                    nm = _text(n, self.raw, 120)
                    if nm and nm not in seen and nm not in ("string", "number", "boolean",
                                                            "void", "any", "unknown", "never",
                                                            "object", "null", "undefined", "this"):
                        seen.add(nm)
                        self._edge(owner_id, "references", self._ext(nm), node,
                                   "resolved", "resolved", 0.7, {"type_reference": nm})
            stack.extend(n.children)

    # --------------------------------------------------------------- variables
    def _bound_names(self, pat) -> list[str]:
        out: list[str] = []
        t = pat.type
        if t == "identifier":
            out.append(_text(pat, self.raw, 120))
        elif t in ("object_pattern", "array_pattern"):
            for c in pat.children:
                if c.type in ("shorthand_property_identifier_pattern", "identifier"):
                    out.append(_text(c, self.raw, 120))
                elif c.type == "pair_pattern":
                    for cc in c.children:
                        if cc.type == "identifier":
                            out.append(_text(cc, self.raw, 120))
                elif c.type in ("object_pattern", "array_pattern", "rest_pattern",
                                "assignment_pattern"):
                    out.extend(self._bound_names(c))
        elif t in ("rest_pattern", "assignment_pattern"):
            for c in pat.children:
                out.extend(self._bound_names(c))
        return [n for n in out if n and n != "..."]

    def _is_require(self, value_node) -> str | None:
        if value_node is not None and value_node.type == "call_expression":
            kids = [c for c in value_node.children if c.type not in ("(", ")", ",", "comment")]
            if kids and _text(kids[0], self.raw, 20) == "require":
                args = next((k for k in value_node.children if k.type == "arguments"), None)
                if args is not None:
                    for a in args.children:
                        if a.type == "string":
                            return _strip_quotes(_text(a, self.raw, 300))
        return None

    def _vars(self, node, top: bool, owner: str | None = None) -> list[str]:
        declared: list[str] = []
        owner = owner or self.mod_id
        for c in node.children:
            if c.type != "variable_declarator":
                continue
            name_node = c.child_by_field_name("name")
            value_node = c.child_by_field_name("value")
            if name_node is None:
                continue
            req_spec = self._is_require(value_node)
            if req_spec is None and value_node is not None and \
                    value_node.type == "member_expression" and name_node.type == "identifier":
                # `var f = require('./m').f` reads like a named import
                prop = value_node.child_by_field_name("property")
                spec2 = self._is_require(value_node.child_by_field_name("object"))
                if spec2 is not None and prop is not None:
                    nm = _text(name_node, self.raw, 120)
                    imp = _text(prop, self.raw, 120)
                    nid, _ = self._add(c, "variable", "variable", nm, "",
                                       {"cjs_require": spec2, "imported": imp}, parent=owner)
                    declared.append(nid)
                    self.import_bindings[nm] = {"kind": "require-named", "imported": imp,
                                                "specifier": spec2, "type_only": False}
                    self._record_import(node, spec2,
                                        {"named": [{"imported": imp, "local": nm}]}, False, False)
                    continue
            if req_spec is not None:
                bound = self._bound_names(name_node)
                for nm in bound:
                    self.import_bindings[nm] = {"kind": "require", "imported": "*",
                                                "specifier": req_spec, "type_only": False}
                    nid, _ = self._add(c, "variable", "variable", nm, "",
                                       {"cjs_require": req_spec}, parent=owner)
                    declared.append(nid)
                # Destructured require behaves like named imports; a bare name
                # acts namespace-like. Both forms are recorded for the resolver.
                bindings: dict = {"namespace": bound[0]} if len(bound) == 1 else {}
                if len(bound) != 1:
                    bindings = {"named": [{"imported": nm, "local": nm} for nm in bound]}
                else:
                    # single name: could be either; record both readings
                    bindings = {"namespace": bound[0],
                                "named": [{"imported": bound[0], "local": bound[0]}]}
                for nm in bound:
                    self.import_bindings[nm] = {"kind": "require-named", "imported": nm,
                                                "specifier": req_spec, "type_only": False}
                self._record_import(node, req_spec, bindings, False, False)
                continue
            init = None
            if value_node is not None and value_node.type in ("function_expression",
                                                              "arrow_function",
                                                              "generator_function"):
                init = value_node
            if init is not None:
                for nm in self._bound_names(name_node)[:1]:
                    is_async = any(k.type == "async" for k in init.children)
                    sig = f"params={self._param_count(init)},async={int(is_async)},assigned=1"
                    kind = "method" if self.class_stack else "function"
                    nid, q = self._add(c, kind,
                                       "method" if kind == "method" else "function", nm, sig,
                                       {"assigned_to": nm}, parent=owner)
                    declared.append(nid)
                    self.scope.append(nm)
                    body = init.child_by_field_name("body")
                    if body is not None and body.type == "statement_block":
                        self._body(body, nid, q)
                    else:
                        self._param_defaults(init, nid, q)
                        self._calls_in(init, nid, q)
                    self.scope.pop()
            elif top:
                for nm in self._bound_names(name_node):
                    nid, _ = self._add(c, "variable", "variable", nm, "", parent=owner)
                    declared.append(nid)
                    if value_node is not None:
                        self._calls_in(value_node, owner, self.mod)
            elif value_node is not None:
                # Local temporaries: no node (noise rule), but scan for calls
                # attributed to the enclosing owner, never the module.
                self._calls_in(value_node, owner, ".".join(self.scope))
        return declared

    # ----------------------------------------------------------------- imports
    def _import(self, node):
        spec = ""
        for c in node.children:
            if c.type == "string":
                spec = _strip_quotes(_text(c, self.raw, 300))
                break
        if not spec:
            for c in node.children:
                if c.type == "import_require_clause":
                    for cc in c.children:
                        if cc.type == "string":
                            spec = _strip_quotes(_text(cc, self.raw, 300))
                    alias = next((_text(cc, self.raw, 120) for cc in c.children
                                  if cc.type == "identifier"), "")
                    if spec:
                        bindings: dict = {}
                        if alias:
                            bindings["default"] = alias
                            self.import_bindings[alias] = {"kind": "require", "imported": "*",
                                                           "specifier": spec, "type_only": False}
                        self._record_import(node, spec, bindings, False, not bindings)
                    return
            return
        type_only = any(c.type == "type" for c in node.children[:3])
        bindings = {}
        side_effect = True
        for c in node.children:
            if c.type == "import_clause":
                side_effect = False
                for cc in c.children:
                    if cc.type == "identifier":
                        local = _text(cc, self.raw, 120)
                        bindings["default"] = local
                    elif cc.type == "named_imports":
                        for s in cc.children:
                            if s.type == "import_specifier":
                                orig = alias = ""
                                for ss in s.children:
                                    if ss.type == "identifier":
                                        if not orig:
                                            orig = _text(ss, self.raw, 120)
                                        else:
                                            alias = _text(ss, self.raw, 120)
                                if orig:
                                    local = alias or orig
                                    bindings.setdefault("named", []).append(
                                        {"imported": orig, "local": local})
                    elif cc.type == "namespace_import":
                        for s in cc.children:
                            if s.type == "identifier":
                                local = _text(s, self.raw, 120)
                                bindings["namespace"] = local
        # import_bindings index is maintained centrally in _record_import.
        self._record_import(node, spec, bindings, type_only, side_effect)

    def _record_import(self, node, spec: str, bindings: dict, type_only: bool,
                       side_effect: bool, dynamic: bool = False,
                       extra: dict | None = None):
        fw = self.TEST_FRAMEWORK_SPECS.get(spec.split("/")[0]
                                           if not spec.startswith("@") else
                                           "/".join(spec.split("/")[:2]))
        if fw:
            self.test_frameworks.add(fw)
        target = self._ext(f"import:{spec}", spec)
        attrs = {"specifier": spec, "bindings": bindings, "type_only": type_only,
                 "side_effect_only": side_effect and not bindings,
                 "dynamic": dynamic}
        if extra:
            attrs.update(extra)
        # One record per specifier per file: further imports of the same module
        # merge into the existing edge (deterministic statement order) instead
        # of creating twins with racy line numbers / flags.
        eid = canonical_edge_id(self.mod_id, "imports", target, self.rel)
        for e in self.res.edges:
            if e.id == eid:
                old = dict(e.attributes or {})
                old["bindings"] = _merge_bindings(old.get("bindings", {}), bindings)
                old["type_only"] = old.get("type_only", True) and type_only
                old["side_effect_only"] = old.get("side_effect_only", True) and \
                    (side_effect and not bindings)
                old["dynamic"] = old.get("dynamic", False) or dynamic
                for flag in ("export_star", "re_export"):
                    if attrs.get(flag):
                        old[flag] = True
                for a_ in attrs.get("ns_exports", []) or []:
                    if a_ not in old.setdefault("ns_exports", []):
                        old["ns_exports"].append(a_)
                if attrs.get("alias") and not old.get("alias"):
                    old["alias"] = attrs["alias"]
                old_names = {(n.get("imported"), n.get("exported")) for n in
                             old.get("names", []) if isinstance(n, dict)}
                for n in attrs.get("names", []) or []:
                    if isinstance(n, dict) and (n.get("imported"), n.get("exported")) \
                            not in old_names:
                        old.setdefault("names", []).append(n)
                        old_names.add((n.get("imported"), n.get("exported")))
                e.attributes = old
                for local, rec in _bindings_index(old["bindings"], spec).items():
                    self.import_bindings.setdefault(local, rec)
                return
        self._edge(self.mod_id, "imports", target, node, "structural", "deterministic",
                   1.0, attrs)
        for local, rec in _bindings_index(bindings, spec).items():
            self.import_bindings.setdefault(local, rec)

    # ----------------------------------------------------------------- exports
    def _export(self, node):
        kids = [c for c in node.children if c.type not in ("export", ";", "comment")]
        if not kids:
            return
        ns = [c for c in kids if c.type == "namespace_export"]
        if ns and not any(c.type == "*" for c in kids):
            # `export * as util from "./util.js"` exports ONE name (a namespace
            # object), not every name of the module: keep it off export_star
            spec = next((_strip_quotes(_text(c, self.raw, 300)) for c in kids
                         if c.type == "string"), "")
            alias = next((_text(cc, self.raw, 120) for cc in ns[0].children
                          if cc.type in ("identifier", "string")), "")
            if spec and alias:
                self._record_import(node, spec, {}, False, False, dynamic=False,
                                    extra={"re_export": True,
                                           "ns_exports": [_strip_quotes(alias)]})
            return
        if any(c.type == "*" for c in kids):
            spec = ""
            alias = ""
            for c in kids:
                if c.type == "string":
                    spec = _strip_quotes(_text(c, self.raw, 300))
                if c.type == "namespace_export":
                    for cc in c.children:
                        if cc.type == "identifier":
                            alias = _text(cc, self.raw, 120)
            if spec:
                self._record_import(node, spec, {}, False, False, dynamic=False,
                                    extra={"export_star": True, "alias": alias})
            return
        from_spec = ""
        for c in kids:
            if c.type == "string":
                from_spec = _strip_quotes(_text(c, self.raw, 300))
        if from_spec:
            target = self._ext(f"import:{from_spec}", from_spec)
            names = []
            for c in kids:
                if c.type == "export_clause":
                    for s in c.children:
                        if s.type == "export_specifier":
                            orig = alias = ""
                            for ss in s.children:
                                if ss.type == "identifier":
                                    if not orig:
                                        orig = _text(ss, self.raw, 120)
                                    else:
                                        alias = _text(ss, self.raw, 120)
                            if orig:
                                names.append({"imported": orig, "exported": alias or orig})
            self._record_import(node, from_spec, {}, type_only=any(
                k.type == "type" for k in kids[:2]), side_effect=False, dynamic=False,
                extra={"re_export": True, "names": names})
            for nm in names:
                ph = self._ext(nm["imported"])
                self._edge(self.mod_id, "exports", ph, node, "documented", "claimed", None,
                           {"exported_as": nm["exported"], "re_export": True, "from": from_spec})
            return
        if any(c.type == "=" for c in kids):
            for c in kids:
                if c.type == "identifier":
                    nm = _text(c, self.raw, 120)
                    self._edge(self.mod_id, "exports", self._ext(nm), node, "documented",
                               "claimed", None, {"export_assign": True, "exported_as": "default"})
            return
        is_default = any(c.type == "default" for c in kids)
        is_type_only = any(c.type == "type" for c in kids[:3])
        if is_default:
            # `export default { a, b: c }`: a utils object; `import utils from` +
            # `utils.a()` reaches `a` through the member `default.a`
            for c in kids:
                if c.type != "object":
                    continue
                for prop in c.children:
                    key = val = ""
                    if prop.type == "shorthand_property_identifier":
                        key = val = _text(prop, self.raw, 120)
                    elif prop.type == "pair":
                        k = prop.child_by_field_name("key")
                        v = prop.child_by_field_name("value")
                        if k is not None and v is not None and v.type == "identifier":
                            key, val = _text(k, self.raw, 120).strip("'\""), _text(v, self.raw, 120)
                    if key and val:
                        self._cjs_export(node, val, "default." + key)
        for c in kids:
            if c.type == "export_clause":
                for s in c.children:
                    if s.type == "export_specifier":
                        orig = alias = ""
                        for ss in s.children:
                            if ss.type == "identifier":
                                if not orig:
                                    orig = _text(ss, self.raw, 120)
                                else:
                                    alias = _text(ss, self.raw, 120)
                        b = self.import_bindings.get(orig) if orig else None
                        if b and b.get("specifier") and b.get("kind") in ("named", "default"):
                            # `import { raw } from '../utils/html'; export { raw }`:
                            # a re-export of the imported binding
                            imp = b.get("imported") or orig
                            imp = "default" if b.get("kind") == "default" else imp
                            self._record_import(node, b["specifier"], {}, type_only=is_type_only,
                                                side_effect=False, dynamic=False,
                                                extra={"re_export": True,
                                                       "names": [{"imported": imp, "exported": alias or orig}]})
                            self._edge(self.mod_id, "exports", self._ext(imp), node, "documented",
                                       "claimed", None, {"exported_as": alias or orig, "re_export": True,
                                                         "from": b["specifier"]})
                        elif orig:
                            self._edge(self.mod_id, "exports", self._ext(orig), node,
                                       "documented", "claimed", None,
                                       {"exported_as": alias or orig, "type_only": is_type_only})
            elif c.type in ("function_declaration", "generator_function_declaration",
                            "function_signature",
                            "class_declaration", "lexical_declaration",
                            "variable_declaration", "abstract_class_declaration",
                            "interface_declaration", "type_alias_declaration",
                            "enum_declaration", "internal_module", "ambient_declaration",
                            "function_expression", "arrow_function"):
                if c.type in ("lexical_declaration", "variable_declaration"):
                    ids = self._vars(c, top=True, owner=self.mod_id)
                else:
                    ids = self._stmt(c, top=True)
                by_id = {n.id: n for n in self.res.nodes}
                if is_default and ids:
                    self._edge(self.mod_id, "exports", ids[0], node, "documented",
                               "claimed", None, {"exported_as": "default"})
                else:
                    for i in ids:
                        n = by_id.get(i)
                        self._edge(self.mod_id, "exports", i, node, "documented",
                                   "claimed", None,
                                   {"exported_as": n.label if n else i,
                                    "type_only": is_type_only})

    def _local_by_name(self, nm: str) -> str | None:
        """Same-module declaration lookup for export targets (scope-aware)."""
        for i in range(len(self.scope), 0, -1):
            cand = ".".join(self.scope[:i] + [nm])
            if cand in self.local_symbols:
                return self.local_symbols[cand]
        return None

    def _cjs_export(self, node, nm: str, exported_as: str):
        # Prefer the real local declaration; placeholder only if unknown.
        tgt = self._local_by_name(nm) or self._ext(nm)
        self._edge(self.mod_id, "exports", tgt, node, "documented", "claimed", None,
                   {"exported_as": exported_as, "cjs": True})

    def _top_expr(self, node, owner_id: str | None = None, owner_q: str | None = None):
        owner_id = owner_id or self.mod_id
        owner_q = owner_q or self.mod
        for c in node.children:
            if c.type == "assignment_expression":
                left = c.child_by_field_name("left")
                right = c.child_by_field_name("right")
                lname = _text(left, self.raw, 150) if left is not None else ""
                if right is not None and left is not None and \
                        left.type == "member_expression" and \
                        right.type in ("function_expression", "arrow_function",
                                       "generator_function"):
                    prop = left.child_by_field_name("property")
                    obj = left.child_by_field_name("object")
                    m = _text(prop, self.raw, 120) if prop is not None else ""
                    o = _text(obj, self.raw, 150) if obj is not None else ""
                    if o.endswith(".prototype"):
                        o = o[: -len(".prototype")]
                    if m and prop.type == "property_identifier":
                        if o in ("exports", "module.exports"):
                            nid = self._member_fn(c, "", m, right, lname)
                            self._edge(self.mod_id, "exports", nid, node, "documented",
                                       "claimed", None, {"exported_as": m, "cjs": True})
                            return
                        if o.isidentifier() and o not in ("this", "module", "window",
                                                          "globalThis", "self"):
                            self._member_fn(c, o, m, right, lname)
                            return
                if lname in ("module.exports", "exports") or lname.startswith(("exports.", "module.exports.")):
                    if right is not None:
                        if right.type == "object" and lname in ("module.exports", "exports"):
                            self._object_members("exports", right)
                        if right.type == "object":
                            for prop in right.children:
                                if prop.type == "shorthand_property_identifier":
                                    nm = _text(prop, self.raw, 120)
                                    self._cjs_export(node, nm, nm)
                                elif prop.type == "pair":
                                    for pc in prop.children:
                                        if pc.type == "property_identifier":
                                            k = _text(pc, self.raw, 120)
                                            self._cjs_export(node, k, k)
                        elif right.type == "identifier":
                            nm = _text(right, self.raw, 120)
                            exported = "default" if lname in ("module.exports", "exports") \
                                else lname.split(".")[-1]
                            self._cjs_export(node, nm, exported)
                        if right.type == "object" and lname in ("module.exports", "exports"):
                            # declared members walked their own bodies already
                            for p in right.children:
                                if not self._is_fn_member(p):
                                    self._calls_in(p, owner_id, owner_q)
                        else:
                            self._calls_in(right, owner_id, owner_q)
                    return
                if right is not None:
                    self._calls_in(right, owner_id, owner_q)
                return
        self._calls_in(node, owner_id, owner_q)

    # ------------------------------------------------------------------- calls
    def _calls_in(self, node, owner_id: str, owner_q: str, recover: bool = False):
        """Conservative call walk: syntax shapes only, no name-guessing.

        Same-module targets bind immediately via the declaration table; imports
        stay unresolved with binding hints for the service-stage resolver.
        With recover=True (inside ERROR regions), named function expressions are
        salvaged as declarations instead of skipped.
        """
        stack = [node]
        while stack:
            n = stack.pop()
            t = n.type
            if t in ("call_expression", "new_expression"):
                # Consumed calls (test declarations, require/dynamic imports)
                # own their subtrees; re-walking children would duplicate them
                # under synthetic scopes.
                if not self._call(n, owner_id, t == "new_expression"):
                    stack.extend(n.children)
            elif t in ("ERROR", "MISSING") and recover:
                self._recover(n, top=False)
            elif t in ("function_declaration", "function_expression", "arrow_function",
                       "generator_function_declaration"):
                if recover and t == "function_expression":
                    nm = self._named(n)
                    if nm:
                        self._fn_named_expr(n, nm)
                        continue
                if t in ("arrow_function", "function_expression",
                         "generator_function_declaration"):
                    # Callbacks passed as arguments: walk the body for calls
                    # attributed to the current owner (never the callee).
                    self._callback_body(n, owner_id)
                    continue
                continue  # nested scopes belong to the declaration walk
            elif t in ("string", "comment", "jsx_text", "regex"):
                continue  # never treat strings/comments as code
            elif t == "template_string":
                for c in n.children:
                    if c.type == "template_substitution":
                        stack.extend(c.children)
            elif t in ("jsx_element", "jsx_self_closing_element"):
                self._jsx(n, owner_id)
                stack.extend(n.children)
            else:
                stack.extend(n.children)

    def _callback_body(self, fn_node, owner_id: str):
        """Walk a callback argument's body for calls (owner = enclosing scope).

        Nested declarations inside callbacks are declared under a deterministic
        `<callback#n>` scope so identities stay stable without collapsing.
        """
        body = fn_node.child_by_field_name("body")
        if body is None:
            return
        key = (tuple(self.scope), owner_id)
        n = self.scope_counts.get(("callback",) + key, 0) + 1
        self.scope_counts[("callback",) + key] = n
        self.scope.append(f"<callback#{n}>")
        try:
            if body.type == "statement_block":
                for stmt in body.children:
                    st = stmt.type
                    if st in ("function_declaration", "generator_function_declaration"):
                        self._fn(stmt, "function")
                    elif st == "class_declaration":
                        self._class(stmt)
                    elif st in ("lexical_declaration", "variable_declaration"):
                        self._vars(stmt, top=False, owner=owner_id)
                    elif st in ("ERROR", "MISSING"):
                        self._recover(stmt, top=False)
                    else:
                        self._calls_in(stmt, owner_id, ".".join(self.scope))
            else:
                self._calls_in(body, owner_id, ".".join(self.scope))
        finally:
            self.scope.pop()

    def _callee_shape(self, fn):
        """Return (kind, base, member) without ever reading string contents."""
        if fn is None:
            return ("unknown", "", "")
        t = fn.type
        if t == "identifier":
            return ("identifier", "", _text(fn, self.raw, 120))
        if t == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            member = _text(prop, self.raw, 120) if prop is not None else ""
            chain: list[str] = []
            cur = obj
            while cur is not None and len(chain) < 4:
                if cur.type == "identifier":
                    chain.append(_text(cur, self.raw, 80))
                    break
                if cur.type == "this":
                    chain.append("this")
                    break
                if cur.type == "super":
                    chain.append("super")
                    break
                if cur.type == "member_expression":
                    p = cur.child_by_field_name("property")
                    if p is not None:
                        chain.append(_text(p, self.raw, 80))
                    cur = cur.child_by_field_name("object")
                    continue
                if cur.type == "call_expression":
                    chain.append("<call>")
                    break
                break
            chain.reverse()
            return ("member", ".".join(chain), member)
        if t == "this":
            return ("this", "this", "")
        if t == "super":
            return ("super", "super", "")
        if t == "import":
            return ("dynamic_import", "", "")
        if t == "call_expression":
            return ("chained", "<call>", "")
        return ("unknown", "", _text(fn, self.raw, 60))

    def _local_target(self, kind: str, base: str, member: str) -> str | None:
        """Same-module binding through the declaration table (scope-aware).

        Tries innermost scope outward: `member`, then `this.member` against the
        enclosing class. Never consults other files here.
        """
        if kind == "identifier" and member:
            for i in range(len(self.scope), 0, -1):
                cand = ".".join(self.scope[:i] + [member])
                if cand in self.local_funcs:
                    return self.local_funcs[cand]
            return None
        if kind == "member" and base == "this" and member:
            for i in range(len(self.scope), 0, -1):
                cand = ".".join(self.scope[:i] + [member])
                if cand in self.local_funcs:
                    return self.local_funcs[cand]
            # fall back to the enclosing class scope
            for cid in reversed(self.class_stack):
                for q, nid in self.local_funcs.items():
                    if q.startswith(cid + ".") and q.rsplit(".", 1)[-1] == member:
                        return nid
            return None
        return None

    # ------------------------------------------------- test model (Phase 9)
    TEST_ROOTS = frozenset({"describe", "test", "it", "context", "specify",
                            "xdescribe", "xtest", "xit", "describe.only", "test.only",
                            "it.only"})
    TEST_SETUP = frozenset({"beforeEach", "afterEach", "beforeAll", "afterAll"})
    TEST_FRAMEWORK_SPECS = {"jest": "jest", "@jest/globals": "jest",
                            "vitest": "vitest", "mocha": "mocha",
                            "@vitest/coverage-v8": "vitest"}

    def _is_test_file(self) -> bool:
        rel = (self.rel or "").lower().replace("\\", "/")
        return "/test_" in rel or "/tests/" in rel or "__tests__" in rel or \
            "test." in rel.split("/")[-1] or "spec." in rel.split("/")[-1]

    def _test_root(self, kind: str, base: str, member: str) -> str | None:
        if kind == "identifier" and member in self.TEST_ROOTS:
            return member.split(".")[0]
        if kind == "member" and base.split(".")[0] in self.TEST_ROOTS:
            return base.split(".")[0]
        return None

    def _framework_known(self) -> str:
        if self.test_frameworks:
            return sorted(self.test_frameworks)[0]
        if self.describe_stack:
            return "suite-context"
        if self._is_test_file():
            return "test-file"
        return ""

    def _string_args(self, args) -> list[str]:
        out = []
        if args is not None:
            for a in args.children:
                if a.type == "string":
                    out.append(_strip_quotes(_text(a, self.raw, 200)))
        return out

    def _fn_args(self, args) -> list:
        if args is None:
            return []
        return [a for a in args.children
                if a.type in ("function_expression", "arrow_function",
                              "function", "generator_function")]

    def _run_test_body(self, fn_node, owner_id: str, owner_q: str) -> None:
        body = fn_node.child_by_field_name("body")
        if body is None:
            for c in fn_node.children:
                if c.type == "statement_block":
                    body = c
                    break
        if body is None:
            return
        if body.type == "statement_block":
            for stmt in body.children:
                self._stmt(stmt, top=False)
        else:
            self._calls_in(body, owner_id, owner_q)

    def _maybe_test_decl(self, node, kind: str, base: str, member: str,
                         args, owner_id: str, owner_q: str) -> bool:
        """Handle test-framework declarations. Returns True when consumed.

        A bare `test_`-like call is NOT a test without framework evidence
        (import, test file, or enclosing suite) — it falls through to the
        ordinary call path. Presence never claims runnability or passing.
        """
        # A bare `.each(table)` provider call is not a test by itself; only
        # the chained call carrying the title declares a test case.
        if kind == "member" and member == "each":
            return False
        root = self._test_root(kind, base, member)
        if root is None and kind == "chained":
            # Chained providers (`it.each(rows)(title, fn)`) resolve through
            # the inner framework root, checked before giving up.
            root = self._inner_test_root(node)
        if root is None:
            return False
        fw = self._framework_known()
        if not fw:
            return False
        if root in ("beforeEach", "afterEach", "beforeAll", "afterAll") or \
                (kind == "member" and member in self.TEST_SETUP) or \
                (kind == "identifier" and member in self.TEST_SETUP):
            # Fixture/setup hook: run the callback under the current owner so
            # its calls attribute to the suite/module, then consume the call.
            for fn in self._fn_args(args):
                self._run_test_body(fn, owner_id, owner_q)
            return True
        is_suite = root in ("describe", "context", "xdescribe")
        strings = self._string_args(args)
        fns = self._fn_args(args)
        # test.each/table-driven: chained callee wrapping a test root.
        parametrized: dict | None = None
        saved_scope = None
        if kind == "chained" and root in ("test", "it", "describe", "specify", "context"):
            parametrized = {"each": True}
            strings = self._string_args(args) or strings
            fns = self._fn_args(args) or fns
            # The chain may sit inside a callback scope from the outer call
            # walk — synthetic segments never belong in a test identity.
            # Restored below so the caller's push/pop balance survives.
            saved_scope = self.scope
            self.scope = [s for s in self.scope
                          if not (s.startswith("<callback#") and s.endswith(">"))]
        name = strings[0] if strings else ""
        if not name:
            self._anon_test += 1
            name = f"<anonymous#{self._anon_test}>"
        parent = self.describe_stack[-1] if (not is_suite and self.describe_stack) \
            else self.mod_id
        sig = "suite" if is_suite else "case"
        if parametrized:
            sig += ",parametrized"
        nid, q = self._add(node, "test", "test", name, sig,
                           {"test_kind": "suite" if is_suite else "case",
                            "framework": fw if fw != "suite-context" else "unknown",
                            "test_name": strings[0] if strings else "",
                            **({"parametrized": True} if parametrized else {})},
                           parent=parent)
        # a test's name is a string (`test("parsedType", ...)`), never a callable
        # identifier: registering it would capture calls to the real function
        try:
            if is_suite:
                self.describe_stack.append(nid)
                self.scope.append(name)
                try:
                    for fn in fns:
                        self._run_test_body(fn, nid, q)
                    # non-function statements directly under describe()
                    if args is not None:
                        for a in args.children:
                            if a.type not in ("(", ")", ",", "comment", "string",
                                              "function_expression", "arrow_function",
                                              "function", "generator_function"):
                                self._calls_in(a, nid, q)
                finally:
                    self.scope.pop()
                    self.describe_stack.pop()
            else:
                for fn in fns:
                    self.scope.append(name)
                    try:
                        self._run_test_body(fn, nid, q)
                    finally:
                        self.scope.pop()
        finally:
            if saved_scope is not None:
                self.scope = saved_scope
        return True

    def _inner_test_root(self, node) -> str | None:
        """Unwrap `test.each(...)(...)`-style chains to the framework root."""
        seen = set()
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if id(n) in seen:
                continue
            seen.add(id(n))
            if n.type == "call_expression":
                kids = [c for c in n.children
                        if c.type not in ("(", ")", ",", "comment", "arguments")]
                if kids:
                    k, b, m = self._callee_shape(kids[0])
                    r = self._test_root(k, b, m)
                    if r is not None:
                        return r
                stack.extend(n.children)
            elif n.type == "member_expression":
                k, b, m = self._callee_shape(n)
                r = self._test_root(k, b, m)
                if r is not None:
                    return r
                stack.extend(n.children)
        return None

    def _call(self, node, owner_id: str, is_new: bool) -> bool:
        """Process one call site. Returns True when consumed (test declaration,
        require/dynamic import): the subtree belongs to a dedicated handler
        and the caller must not re-walk its children."""
        kids = [c for c in node.children
                if c.type not in ("new", "(", ")", ",", "comment")]
        fn = kids[0] if kids else None
        args = next((c for c in node.children if c.type == "arguments"), None)
        kind, base, member = self._callee_shape(fn)
        if self._maybe_test_decl(node, kind, base, member, args, owner_id,
                                 ".".join(self.scope)):
            return True
        if kind == "dynamic_import":
            spec = ""
            if args is not None:
                for a in args.children:
                    if a.type == "string":
                        spec = _strip_quotes(_text(a, self.raw, 300))
                        break
            if spec:
                self._record_import(node, spec, {}, False, True, dynamic=True)
            return True
        if kind == "identifier" and member == "require" and not is_new:
            if args is not None:
                for a in args.children:
                    if a.type == "string":
                        spec = _strip_quotes(_text(a, self.raw, 300))
                        self._record_import(node, spec, {}, False, True)
            return True
        # same-module fast path (declaration table, scope-aware)
        local = self._local_target(kind, base, member)
        occ = [{"line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1,
                "col": node.start_point[1]}]
        if local is not None and not is_new:
            self.res.edges.append(Edge(
                canonical_edge_id(owner_id, "calls", local, self.rel), owner_id, local,
                "calls", "resolved", "resolved", 0.9, [self.ev.id],
                f"jsts-{self.lang}-local", JSTS_VERSION,
                file_path=self.rel, start_line=node.start_point[0] + 1,
                attributes={"occurrences": occ}))
            return False
        display = member if kind == "member" else (member or base or "?")
        if kind == "member" and base:
            display = f"{base}.{member}" if member else base
        target = self._ext(display, member or display)
        attrs: dict = {"callee_kind": kind, "base": base, "member": member,
                       "constructs": is_new, "arity": self._arity(args),
                       "occurrences": [{"line": node.start_point[0] + 1,
                                        "end_line": node.end_point[0] + 1,
                                        "col": node.start_point[1]}]}
        if base in ("this", "super"):
            attrs["scoped_receiver"] = base
        binding = None
        if kind == "identifier" and member in self.import_bindings:
            binding = self.import_bindings[member]
            attrs["import_binding"] = binding
        elif kind == "member" and base.split(".")[0] in self.import_bindings:
            binding = self.import_bindings[base.split(".")[0]]
            attrs["import_binding"] = binding
            attrs["namespace_member"] = member
        rel = "instantiates" if is_new else "calls_unresolved"
        etype, cclass, score = ("structural", "deterministic", 1.0) \
            if not is_new else ("resolved", "resolved", 0.85)
        if kind == "identifier" and member and not is_new and binding is None:
            self.pending_local.append((len(self.res.edges), tuple(self.scope), member,
                                       owner_id, node.start_point[0] + 1))
        self._edge(owner_id, rel, target, node, etype, cclass, score, attrs)
        return False

    def _arity(self, args) -> int:
        if args is None:
            return 0
        return sum(1 for c in args.children if c.type not in ("(", ")", ",", "comment"))

    def _jsx(self, node, owner_id: str):
        # Capitalized JSX tags reference components: a real dependency, modeled
        # as `uses` (not a call — mounting is not invocation evidence).
        tag = ""
        if node.type == "jsx_self_closing_element":
            for cc in node.children:
                if cc.type == "identifier":
                    tag = _text(cc, self.raw, 120)
                    break
            # nested identifiers like <UI.Card>: use the full dotted name
            if not tag:
                for cc in node.children:
                    if cc.type == "nested_identifier":
                        tag = _text(cc, self.raw, 120)
                        break
            tag = (tag.split(".")[-1] if tag else "")
        else:
            for c in node.children:
                if c.type in ("jsx_opening_element", "jsx_self_closing_element"):
                    for cc in c.children:
                        if cc.type == "identifier":
                            tag = _text(cc, self.raw, 120)
                            break
                    break
        if tag and tag[:1].isupper():
            # Same-module components bind to the real definition (like calls);
            # anything else stays an explicit placeholder.
            local = self._local_target("identifier", "", tag)
            tgt = local if local is not None else self._ext(tag)
            self._edge(owner_id, "uses", tgt, node, "structural",
                       "deterministic", 1.0, {"jsx_component": tag,
                                              "resolved": local is not None})
