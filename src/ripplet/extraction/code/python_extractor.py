from __future__ import annotations
import ast
import hashlib
from ...core.models import Node, Edge, Location, ExtractionResult
from ...core.identifiers import (
    file_node_id, canonical_symbol_id, fallback_anon_id,
    canonical_edge_id, edge_id, content_hash, normalize_qualified,
)
from ...core.provenance import make_evidence
from .base import CodeExtractor

EXTRACTOR_VERSION = "14"  # 14: a call chained on a new line is recorded on the line of the attribute; 13: every module-level call (scripts, __main__, x = f()) owned by the module; parameter ranges on functions; 11: constant str_value + include prefix refs (10: receiver-aware, order-independent call binding)
CALL_RESOLUTION_VERSION = "7"  # 7: chained-call line is the attribute line; receiver-aware binding: attribute/local/module receivers never bind by short name (was 5)
# Phase 24 Faz C: framework-aware route facts (additive only — no existing
# fact changes shape, so no extractor-version bump). HTTP verbs recognized
# on any receiver (`router_known` records whether it is a file-local
# APIRouter/FastAPI variable); prefix composition is service-stage.
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options",
                "trace"}
# Call spellings whose keyword literals are recorded for route registration
# (bounded allowlist; nothing else gains keyword attributes).
ROUTE_INCLUDE_CALLS = {"include_router", "register_blueprint", "add_url_rule"}


def _str_literal(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value[:200]
    return None


def _url_template(node) -> str | None:
    """Static URL template from a call argument (plain or f-string only).

    Plain ``"/api/items"`` returns verbatim. An f-string like
    ``f"/api/workorders/{wid}/assign"`` returns ``"/api/workorders/{_}/assign"``
    so parameterized test URLs link to ``{param}`` routes via the existing
    segment-wise matcher. Anything dynamic (BinOp, .format(), concatenation,
    format specs, nested quotes) stays silent — no guessing.
    """
    s = _str_literal(node)
    if s is not None:
        return s
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values or []:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                # Format specs (f"{x:.2f}") or conversions are dynamic.
                if getattr(v, "format_spec", None) is not None:
                    return None
                if getattr(v, "conversion", -1) != -1:
                    return None
                parts.append("{_}")
            else:
                return None
        out = "".join(parts)[:200]
        return out or None
    return None


def _is_url_literal(text: str) -> bool:
    if not isinstance(text, str) or len(text) < 2 or len(text) > 200:
        return False
    if not text.startswith("/"):
        return False
    if any(c in text for c in (" ", "\t", "\n", "\\", "'", '"')):
        return False
    return True


def python_module_name(rel_path: str) -> str:
    """Dotted module path for a repo-relative .py path.

    Shared with the service-stage import resolver (py_resolve) so both
    sides derive identical module identities: ``pkg/mod.py`` -> ``pkg.mod``,
    ``pkg/__init__.py`` -> ``pkg``.
    """
    return rel_path.replace("/", ".").removesuffix(".py").removesuffix(".__init__")


def _annotation_type(ann) -> str:
    """Single named type behind a parameter annotation, or "".

    `Context`, `"Context"`, `click.Context`, `Optional[Context]`,
    `Context | None` -> the dotted name. Unions of two real types, generics
    (`list[X]`), callables and anything else -> "" (no receiver claim)."""
    if ann is None:
        return ""
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            ann = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return ""
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        sides = [x for x in (ann.left, ann.right)
                 if not (isinstance(x, ast.Constant) and x.value is None)]
        return _annotation_type(sides[0]) if len(sides) == 1 else ""
    if isinstance(ann, ast.Subscript):
        head = _func_name(ann.value)
        if head.rsplit(".", 1)[-1] == "Optional":
            return _annotation_type(ann.slice)
        return ""
    if isinstance(ann, (ast.Name, ast.Attribute)):
        name = _func_name(ann)
        return "" if name in ("t.Any", "Any", "object", "None") else name
    return ""


def _param_types(node) -> dict:
    a = node.args
    out = {}
    for arg in list(getattr(a, "posonlyargs", []) or []) + list(a.args) + list(a.kwonlyargs):
        tn = _annotation_type(arg.annotation)
        if tn and arg.arg not in ("self", "cls"):
            out[arg.arg] = tn
    return out


def _signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = node.args
    parts = [
        "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
        f"pos={len(a.args)}",
        f"kwonly={len(a.kwonlyargs)}",
        f"vararg={1 if a.vararg else 0}",
        f"kwarg={1 if a.kwarg else 0}",
        f"defaults={len(a.defaults)}",
    ]
    return ",".join(parts)


def _param_range(fn, is_method: bool) -> dict:
    """{params_min, params_max}: required and maximum positional arguments.
    `*args` makes the maximum open (None); keyword-only parameters without a
    default count as required; `self`/`cls` of a method are not passed."""
    a = fn.args
    pos = list(a.posonlyargs) + list(a.args)
    deco = {getattr(d, "id", getattr(d, "attr", "")) for d in fn.decorator_list}
    if pos and "staticmethod" not in deco and (is_method or pos[0].arg in ("self", "cls")):
        pos = pos[1:]
    required = len(pos) - len(a.defaults) + sum(1 for d in a.kw_defaults if d is None)
    most = None if a.vararg is not None else len(pos) + len(a.kwonlyargs)
    return {"params_min": max(0, required), "params_max": most}


class PythonExtractor(CodeExtractor):
    name = "python-ast"; version = EXTRACTOR_VERSION

    def extract(self, rel_path: str, text: str, repo_id: str, project_id: str) -> ExtractionResult:
        res = ExtractionResult(extractor=self.name, extractor_version=self.version)
        raw = text.encode("utf-8", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            res.warnings.append(f"syntax error {rel_path}: {e}")
            res.partial = True
            # Still emit the file node so the file is tracked (not treated as deleted).
            fid = file_node_id(repo_id, rel_path)
            ev = make_evidence(rel_path, text[:300], self.name, raw)
            res.evidence.append(ev)
            res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id,
                                  rel_path, Location(1, 0, 1, 0), "python",
                                  content_hash(raw), {"suffix": ".py", "parse_error": str(e)[:200]},
                                  [ev.id], schema_version=2))
            return res
        mod_qname = python_module_name(rel_path)
        fid = file_node_id(repo_id, rel_path)
        ev = make_evidence(rel_path, text[:300], self.name, raw)
        res.evidence.append(ev)
        fnode = Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                     Location(1, 0, len(text.splitlines()), 0), "python",
                     content_hash(raw), {"suffix": ".py"}, [ev.id], schema_version=2)
        res.nodes.append(fnode)
        mno = Node(file_node_id(repo_id, rel_path + "#module"), "module", mod_qname, mod_qname,
                   project_id, repo_id, rel_path, Location(1, 0, 1, 0), "python", "",
                   {}, [ev.id], schema_version=2)
        res.nodes.append(mno)
        res.edges.append(Edge(canonical_edge_id(fid, "contains", mno.id, rel_path),
                              fid, mno.id, "contains", "structural", "deterministic", 1.0,
                              [ev.id], self.name, self.version, file_path=rel_path))
        visitor = _V(self, res, ev, repo_id, project_id, rel_path, mod_qname, mno.id)
        visitor.visit(tree)
        _bind_pending_calls(visitor)
        for e in res.edges:
            if e.relation == "imports" and e.extraction_method == "python-ast":
                sc = visitor.import_scopes.get(e.target)
                if sc and "" not in sc:
                    # Function/class-local import: the name is bound there
                    # only (py_callbind must not see it file-wide).
                    e.attributes = dict(e.attributes or {})
                    e.attributes["bound_in"] = sorted(sc)[:20]
        if visitor.dunder_all is not None:
            # Explicit __all__ metadata (list of names only; anything else
            # means "semantics not safely established" and stays absent).
            mno.attributes = dict(mno.attributes or {})
            mno.attributes["dunder_all"] = list(visitor.dunder_all)
        if visitor.routers:
            mno.attributes = dict(mno.attributes or {})
            mno.attributes["routers"] = {
                k: dict(v) for k, v in visitor.routers.items()}
        if visitor.http_calls:
            by_id = {}
            for n in res.nodes:
                by_id.setdefault(n.id, n)
            for nid, urls in visitor.http_calls.items():
                n = by_id.get(nid)
                if n is not None and urls:
                    n.attributes = dict(n.attributes or {})
                    n.attributes["http_calls"] = sorted(urls)[:50]
        if visitor.django_paths:
            mno.attributes = dict(mno.attributes or {})
            mno.attributes["django_paths"] = visitor.django_paths[:200]
        return res


class _V(ast.NodeVisitor):
    def __init__(self, ext, res, ev, repo, proj, rel, mod, mod_id):
        self.ext = res
        self.ev = ev
        self.repo = repo
        self.proj = proj
        self.rel = rel
        self.mod = mod
        self.mod_id = mod_id
        self.scope = [mod]
        self.scope_id_stack: list[str] = [mod_id]
        self.class_stack: list[str] = []
        self.func_ids: dict[str, str] = {}
        self.imports_pytest = False
        self.imports_unittest = False
        self.fixtures: dict[str, str] = {}  # fixture name -> node id (same file)
        self.test_classes: set[str] = set()  # qualified names of test suites
        self.scope_ids: dict[str, str] = {}  # qualified -> node id for ALL
        # named scopes (functions, methods, classes, lambdas); drives
        # lexical-scope call attribution in _CallWalk (Stage B).
        self.scope_counts: dict[tuple, int] = {}
        self.lambda_counts: dict[str, int] = {}
        # Module-level __all__ (list of names) when assignable from a plain
        # list/tuple literal; None means "not safely established".
        self.dunder_all: list[str] | None = None
        # File-local router variables: varname -> {"prefix": str|None,
        # "kind": "APIRouter"|"FastAPI"}. Module scope only.
        self.routers: dict[str, dict] = {}
        # Django URL entries from `urlpatterns = [...]` (module scope only):
        # {"route": "items/<int:wid>/", "view": "views.item_list",
        #  "include": "shop.urls"|None, "is_re": bool}. Prefix composition
        # and view linking are service-stage (cross-file by nature).
        self.django_paths: list[dict] = []
        # Test-file URL literals per enclosing function node id.
        self.http_calls: dict[str, set] = {}
        # Receiver-aware call binding (CALL_RESOLUTION v6): calls are
        # collected during the walk and bound once the whole file is known,
        # so binding never depends on definition order.
        self.class_bases: dict[str, list[str]] = {}  # class q -> base spellings
        self.pending_calls: list[dict] = []
        self.param_types: dict[str, dict[str, str]] = {}  # fn q -> param -> type
        self.import_scopes: dict[str, set[str]] = {}  # import target -> scopes

    def _qid(self, name):
        return ".".join(self.scope + [name])

    def _discriminated(self, kind: str, scope_key: tuple, name: str, signature: str) -> str:
        """Occurrence discriminator for overloads / partial re-definitions.

        First definition keeps a bare signature; re-definitions get `#2`, `#3`...
        This distinguishes overloads without using line numbers.
        """
        key = (kind, scope_key, name)
        n = self.scope_counts.get(key, 0) + 1
        self.scope_counts[key] = n
        if n > 1:
            signature = (signature + f"#overload{n}") if signature else f"#overload{n}"
        return signature

    def _add_symbol(self, node, kind, name, ntype, signature: str = ""):
        q = self._qid(name)
        scope_key = tuple(self.scope)
        signature = self._discriminated(kind, scope_key, name, signature)
        nid = canonical_symbol_id(self.repo, "python", kind, q, signature)
        loc = Location(getattr(node, "lineno", 1), getattr(node, "col_offset", 0),
                       getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1,
                       getattr(node, "end_col_offset", 0) or 0)
        enclosing = ".".join(self.scope)
        attrs = {"enclosing_scope": enclosing}
        if signature:
            attrs["signature"] = signature
        if "test" in self.rel.lower() or name.startswith("test_"):
            attrs["is_test"] = True
        n = Node(nid, ntype, name, normalize_qualified(q), self.proj, self.repo,
                 self.rel, loc, "python", "", attrs, [self.ev.id], schema_version=2)
        self.ext.nodes.append(n)
        if kind == "method" and self.class_stack:
            parent = self.class_stack[-1]
        elif kind in ("function", "class") and len(self.scope_id_stack) > 1:
            # Nested function or class: containment belongs to the lexical
            # owner (method, function, or class), not the module. The
            # top-level case still resolves to the module.
            parent = self.scope_id_stack[-1]
        else:
            parent = self.mod_id
        if kind in ("function", "method", "test"):
            self.func_ids[q] = nid
        self.ext.edges.append(Edge(
            canonical_edge_id(parent, "defines", nid, self.rel), parent, nid,
            "defines", "structural", "deterministic", 1.0, [self.ev.id],
            "python-ast", EXTRACTOR_VERSION,
            file_path=self.rel, start_line=getattr(node, "lineno", 1)))
        if kind in ("function", "method", "class", "test"):
            self.scope_ids[q] = nid
        return nid, q

    # -------------------------------------------------- test model (Phase 9)
    def _is_test_file(self) -> bool:
        rel = self.rel.lower().replace("\\", "/")
        return "test" in rel or "conftest" in rel

    def _deco_dots(self, node) -> list[str]:
        out = []
        for dec in getattr(node, "decorator_list", []) or []:
            try:
                target = dec.func if isinstance(dec, ast.Call) else dec
                out.append(_func_name(target))
            except Exception:
                continue
        return out

    def _is_fixture_def(self, node) -> bool:
        return any(d == "pytest.fixture" or d == "fixture" or d.endswith(".fixture")
                   for d in self._deco_dots(node))

    def _parametrize_info(self, node) -> dict | None:
        for dec in getattr(node, "decorator_list", []) or []:
            if not isinstance(dec, ast.Call):
                continue
            try:
                name = _func_name(dec.func)
            except Exception:
                continue
            if name in ("pytest.mark.parametrize", "parametrize") or \
                    name.endswith(".parametrize"):
                argnames = ""
                if dec.args:
                    try:
                        argnames = ast.literal_eval(dec.args[0]) if isinstance(
                            dec.args[0], ast.Constant) else ""
                    except Exception:
                        argnames = ""
                ids: list[str] = []
                for kw in dec.keywords or []:
                    if kw.arg == "ids" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        for elt in kw.value.elts:
                            try:
                                ids.append(str(ast.literal_eval(elt)))
                            except Exception:
                                continue
                return {"argnames": str(argnames)[:200], "ids": ids[:50]}
        return None

    def _test_framework(self, node=None) -> str:
        # Explicit pytest markers win over a mere unittest import: a file can
        # import unittest helpers while its tests are pytest-style.
        if node is not None:
            dots = self._deco_dots(node)
            if any(d == "pytest.fixture" or d == "fixture" or d.endswith(".fixture")
                   or "parametrize" in d for d in dots):
                return "pytest"
        if self.imports_pytest:
            return "pytest"
        if self.imports_unittest:
            return "unittest"
        if self._is_test_file():
            return "pytest"
        return "unknown"

    def _classify_test(self, node, kind: str, name: str) -> tuple[str, str, dict]:
        """Return (kind, ntype, extra_attrs) applying the test model.

        Presence in the graph never claims runnability, discovery, or passing —
        only that the declaration is statically identifiable as a test.
        """
        extra: dict = {}
        if self._is_fixture_def(node):
            extra["role"] = "fixture"
            return kind, "function", extra
        is_test_name = name.startswith("test_") or name.startswith("Test")
        in_test_class = ".".join(self.scope) in self.test_classes or \
            any(".".join(self.scope[:i]) in self.test_classes
                for i in range(1, len(self.scope) + 1))
        is_test_method = (kind == "method" and self.class_stack and
                          (name.startswith("test") or in_test_class))
        if (self._is_test_file() and (is_test_name or is_test_method)) or \
                (kind == "method" and name.startswith("test")):
            fw = self._test_framework(node)
            extra["test_kind"] = "method" if kind == "method" else "function"
            extra["framework"] = fw
            par = self._parametrize_info(node)
            if par is not None:
                extra["parametrized"] = True
                extra["parametrize_argnames"] = par["argnames"]
                if par["ids"]:
                    extra["parametrize_ids"] = par["ids"]
            return "test", "test", extra
        return kind, ("method" if kind == "method" else "function"), extra

    def visit_ClassDef(self, node):
        base_names = []
        for b in node.bases:
            try:
                base_names.append(_func_name(b))
            except Exception:
                continue
        is_unittest = any(b in ("unittest.TestCase", "TestCase") or b.endswith(".TestCase")
                          for b in base_names)
        is_pytest_suite = node.name.startswith("Test") and not any(
            isinstance(s, ast.FunctionDef) and s.name == "__init__" for s in node.body)
        if is_unittest or (is_pytest_suite and (self._is_test_file() or self.imports_pytest)):
            self.test_classes.add(".".join(self.scope + [node.name]))
            nid, q = self._add_symbol(node, "test", node.name, "test")
            try:
                n = self.ext.nodes[-1]
                n.attributes["test_kind"] = "suite"
                n.attributes["framework"] = "unittest" if is_unittest else \
                    self._test_framework(node)
            except Exception:
                pass
        else:
            nid, q = self._add_symbol(node, "class", node.name, "class")
        self.class_bases[q] = base_names
        for b in node.bases:
            bname = ast.unparse(b) if hasattr(ast, "unparse") else getattr(b, "id", "?")
            ext_id = f"ext:{self.repo}:{bname[:150]}"
            if not any(n.id == ext_id for n in self.ext.nodes):
                self.ext.nodes.append(Node(ext_id, "external_symbol", bname[:120], bname[:200],
                                           self.proj, self.repo, "", Location(), "python",
                                           "", {}, [self.ev.id], schema_version=2))
            self.ext.edges.append(Edge(
                canonical_edge_id(nid, "inherits", ext_id, self.rel),
                nid, ext_id, "inherits", "resolved", "resolved", 0.9,
                [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                file_path=self.rel, start_line=node.lineno))
        self.scope.append(node.name)
        self.class_stack.append(nid)
        # Class-body-level calls (assignments, decorators evaluated at class
        # creation) belong to the class symbol; nested scopes are skipped here
        # and get dedicated walks via generic_visit below.
        cbw = _CallWalk(self.ext, self.ev, nid, q, self.repo, self.rel,
                        self.func_ids, self.scope_ids,
                        http_calls=self.http_calls,
                       pending=self.pending_calls)
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            cbw.visit(stmt)
        self.generic_visit(node)
        self.class_stack.pop()
        self.scope.pop()

    def _enclosing_is_class(self) -> bool:
        """True only when the definition sits directly in a class body.

        `class_stack` stays non-empty for the whole class subtree, so testing
        it alone misclassifies `def inner` inside a method (and anything
        deeper) as a method. The direct lexical owner is the scope-stack top:
        it is the class only for direct members.
        """
        return bool(self.class_stack) and bool(self.scope_id_stack) and \
            self.scope_id_stack[-1] == self.class_stack[-1]

    def visit_FunctionDef(self, node):
        self._visit_fn(node, "method" if self._enclosing_is_class() else "function", False)

    def visit_AsyncFunctionDef(self, node):
        self._visit_fn(node, "method" if self._enclosing_is_class() else "function", True)

    def _visit_fn(self, node, kind, is_async):
        sig = _signature_of(node)
        tkind, ntype, textra = self._classify_test(node, kind, node.name)
        nid, q = self._add_symbol(node, tkind, node.name,
                                  ntype if tkind == "test" else
                                  ("method" if kind == "method" else "function"), sig)
        try:
            # positional arity a caller must satisfy (self/cls excluded):
            # check_change compares it before/after to find broken callers
            self.ext.nodes[-1].attributes.update(_param_range(node, kind == "method"))
        except Exception:
            pass
        try:
            if textra:
                self.ext.nodes[-1].attributes.update(textra)
        except Exception:
            pass
        if textra.get("role") == "fixture":
            self.fixtures[node.name] = nid
        if tkind == "test":
            self._link_fixtures(node, nid)
        self._emit_route_decorators(node, nid)
        pt = _param_types(node)
        if pt:
            self.param_types[q] = pt
        for a in node.args.args + node.args.kwonlyargs:
            if a.annotation is not None:
                tname = ast.unparse(a.annotation) if hasattr(ast, "unparse") else "?"
                ext_id = f"ext:{self.repo}:{tname[:150]}"
                if not any(n.id == ext_id for n in self.ext.nodes):
                    self.ext.nodes.append(Node(ext_id, "external_symbol", tname[:120], tname[:200],
                                               self.proj, self.repo, "", Location(), "python",
                                               "", {}, [self.ev.id], schema_version=2))
                self.ext.edges.append(Edge(
                    canonical_edge_id(nid, "uses", ext_id, self.rel),
                    nid, ext_id, "uses", "resolved", "resolved", 0.8,
                    [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                    file_path=self.rel, start_line=node.lineno))
        # Lexical enclosing scope for decorator/default evaluation.
        enclosing_id = self.scope_id_stack[-1] if self.scope_id_stack else self.mod_id
        cw = _CallWalk(self.ext, self.ev, nid, q, self.repo, self.rel, self.func_ids,
                       self.scope_ids, http_calls=self.http_calls,
                       pending=self.pending_calls)
        # Decorator expressions and default values evaluate in the ENCLOSING
        # scope, not inside the function being defined.
        for dec in getattr(node, "decorator_list", []) or []:
            cw.visit_scoped(dec, enclosing_id)
        try:
            for dflt in list(getattr(node.args, "defaults", []) or []) + \
                    list(getattr(node.args, "kw_defaults", []) or []):
                if dflt is not None:
                    cw.visit_scoped(dflt, enclosing_id)
        except Exception:
            pass
        self.scope.append(node.name)
        self.scope_id_stack.append(nid)
        # Body only: decorators/defaults were pre-walked above with the
        # enclosing scope, and nested scopes get their own dedicated walks.
        for stmt in node.body:
            cw.visit(stmt)
        self.generic_visit(node)
        self.scope_id_stack.pop()
        self.scope.pop()

    def visit_Lambda(self, node):
        parent_q = ".".join(self.scope)
        idx = self.lambda_counts.get(parent_q, 0) + 1
        self.lambda_counts[parent_q] = idx
        try:
            body = ast.unparse(node.body) if hasattr(ast, "unparse") else ""
        except Exception:
            body = ""
        bhash = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()[:12]
        nid = fallback_anon_id(self.repo, "python", "lambda", parent_q, idx, bhash)
        loc = Location(getattr(node, "lineno", 1), getattr(node, "col_offset", 0),
                       getattr(node, "end_lineno", 1) or 1, 0)
        self.ext.nodes.append(Node(
            nid, "function", f"<lambda@{idx}>", normalize_qualified(parent_q) + f"#<lambda@{idx}>",
            self.proj, self.repo, self.rel, loc, "python", "",
            {"anonymous": True, "body_hash": bhash, "enclosing_scope": parent_q},
            [self.ev.id], schema_version=2))
        # Lexical owner (function/method/class body), never the module when
        # nested: a lambda inside a function belongs to that function so
        # impact traversal sees the real nesting.
        parent = self.scope_id_stack[-1] if len(self.scope_id_stack) > 1 else self.mod_id
        self.ext.edges.append(Edge(
            canonical_edge_id(parent, "defines", nid, self.rel),
            parent, nid, "defines", "structural", "deterministic", 1.0,
            [self.ev.id], "python-ast", EXTRACTOR_VERSION,
            file_path=self.rel, start_line=getattr(node, "lineno", 1)))
        self.scope_ids[normalize_qualified(parent_q) + f"#<lambda@{idx}>"] = nid
        self.scope.append(f"<lambda@{idx}>")
        self.scope_id_stack.append(nid)
        lmb_q = normalize_qualified(parent_q) + f"#<lambda@{idx}>"
        cw = _CallWalk(self.ext, self.ev, nid, lmb_q, self.repo, self.rel,
                       self.func_ids, self.scope_ids,
                       http_calls=self.http_calls,
                       pending=self.pending_calls)
        cw.visit(node.body)
        self.generic_visit(node)
        self.scope_id_stack.pop()
        self.scope.pop()

    def _link_fixtures(self, node, nid: str) -> None:
        """Statically identifiable fixture references: test args matching a
        same-file fixture definition get a resolved `uses` edge; anything else
        becomes an explicit unresolved placeholder (never guessed)."""
        try:
            argnames = [a.arg for a in list(node.args.args[1:] if node.args.args and
                                            node.args.args[0].arg in ("self", "cls") else
                                            node.args.args) + node.args.kwonlyargs]
        except Exception:
            return
        for a in argnames:
            if a in self.fixtures:
                self.ext.edges.append(Edge(
                    canonical_edge_id(nid, "uses", self.fixtures[a], self.rel),
                    nid, self.fixtures[a], "uses", "structural", "deterministic", 0.9,
                    [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                    file_path=self.rel, start_line=getattr(node, "lineno", 1),
                    attributes={"fixture": a}))
            elif a not in ("self", "cls"):
                eid = f"ext:{self.repo}:fixture:{a[:100]}"
                if not any(n.id == eid for n in self.ext.nodes):
                    self.ext.nodes.append(Node(
                        eid, "external_symbol", a[:100], a[:100], self.proj, self.repo,
                        "", Location(), "python", "", {}, [self.ev.id], schema_version=2))
                self.ext.edges.append(Edge(
                    canonical_edge_id(nid, "uses", eid, self.rel),
                    nid, eid, "uses", "structural", "deterministic", 0.6,
                    [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                    file_path=self.rel, start_line=getattr(node, "lineno", 1),
                    attributes={"fixture": a, "unresolved": True}))

    def _constant_targets(self, node) -> list[str]:
        """Simple assigned names for constant modeling (Phase 14 P4): plain
        Names plus flat tuple/list destructuring. Starred targets,
        attributes, and subscripts are never bindings of a new name."""
        out = []

        def _flat(t):
            if isinstance(t, ast.Name):
                out.append(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in t.elts:
                    if isinstance(e, ast.Name):
                        out.append(e.id)

        for t in node.targets:
            _flat(t)
        seen, uniq = set(), []
        for n in out:
            if n not in seen and n != "__all__":
                seen.add(n)
                uniq.append(n)
        return uniq[:50]

    def _emit_constants(self, node, names: list[str]) -> None:
        # Module scope only (len == 1), or direct class-body scope: deeper
        # (function-local) names are NOT module/class attributes and must
        # never leak there. First binding wins for determinism; later
        # re-assignments (e.g. `X.update(...)` is not a rebind anyway) keep
        # the same identity since it carries no value.
        if len(self.scope) == 1:
            parent_kind = "module"
        elif len(self.scope) == 2 and self.class_stack:
            parent_kind = "class"
        else:
            return
        if not hasattr(self, "_constant_seen"):
            self._constant_seen = set()
        for name in names:
            if not name or not name.isidentifier():
                continue
            key = (parent_kind, name)
            if key in self._constant_seen:
                continue
            self._constant_seen.add(key)
            q = ".".join(self.scope) + "." + name
            nid = canonical_symbol_id(self.repo, "python", "constant", q, "")
            loc = Location(getattr(node, "lineno", 1),
                           getattr(node, "col_offset", 0),
                           getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1,
                           getattr(node, "end_col_offset", 0) or 0)
            attrs = {"enclosing_scope": ".".join(self.scope), "constant": True}
            val = getattr(node, "value", None)
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and len(val.value) <= 200:
                attrs["str_value"] = val.value  # `API_V1_STR = "/api/v1"`: route prefixes
            n = Node(nid, "constant", name[:120], normalize_qualified(q),
                     self.proj, self.repo, self.rel, loc, "python", "", attrs,
                     [self.ev.id], schema_version=2)
            self.ext.nodes.append(n)
            if parent_kind == "class" and self.class_stack:
                parent = self.class_stack[-1]
            else:
                parent = self.mod_id
            self.ext.edges.append(Edge(
                canonical_edge_id(parent, "defines", nid, self.rel), parent, nid,
                "defines", "structural", "deterministic", 1.0, [self.ev.id],
                "python-ast", EXTRACTOR_VERSION,
                file_path=self.rel, start_line=getattr(node, "lineno", 1)))

    def _emit_route_decorators(self, node, handler_nid: str) -> None:
        """`@router.get("/path")`-style decorators become `route` nodes +
        `handles_route` edges to the handler. Receiver-agnostic in shape
        (any `@X.METHOD(literal)`), but only file-local APIRouter/FastAPI
        variables are marked known; anything else records the receiver
        verbatim with `router_known: False` for the service pass to judge.
        Non-literal paths and unknown shapes stay silent."""
        for dec in getattr(node, "decorator_list", []) or []:
            if not isinstance(dec, ast.Call):
                continue
            try:
                dotted = _func_name(dec.func)
            except Exception:
                continue
            parts = dotted.split(".")
            if len(parts) < 2:
                continue
            meth, receiver = parts[-1].lower(), ".".join(parts[:-1])
            methods: list[str] = []
            if meth in HTTP_METHODS:
                methods = [meth.upper()]
            elif meth == "route":
                for kw in dec.keywords or []:
                    if kw.arg == "methods" and isinstance(kw.value,
                                                          (ast.List, ast.Tuple)):
                        for e in kw.value.elts:
                            s = _str_literal(e)
                            if s and s.lower() in HTTP_METHODS:
                                methods.append(s.upper())
                if not methods:
                    # Flask default: @app.route("/x") without methods serves
                    # GET (Starlette/FastAPI `.route` without methods is
                    # invalid usage; defaulting is harmless there).
                    methods = ["GET"]
            else:
                continue
            path = None
            if dec.args:
                path = _str_literal(dec.args[0])
            if path is None:
                for kw in dec.keywords or []:
                    if kw.arg == "path":
                        path = _str_literal(kw.value)
                        break
            if not path or not path.startswith("/"):
                continue
            known = receiver in self.routers
            prefix = (self.routers.get(receiver) or {}).get("prefix") or ""
            full = (prefix.rstrip("/") + path) if prefix else path
            for m in sorted(set(methods)):
                label = f"{m} {path}"
                # Endpoint identity includes the handler: duplicate
                # registrations of one endpoint share the route node
                # (same endpoint) while keeping distinct handles_route
                # edges per handler.
                q = f"{'.'.join(self.scope)}#{node.name}#{m} {path}"
                nid = canonical_symbol_id(self.repo, "python", "route", q, "")
                loc = Location(getattr(node, "lineno", 1),
                               getattr(node, "col_offset", 0),
                               getattr(node, "end_lineno", 1) or 1, 0)
                self.ext.nodes.append(Node(
                    nid, "route", label[:200], normalize_qualified(q),
                    self.proj, self.repo, self.rel, loc, "python", "",
                    {"http_method": m, "raw_path": path,
                     "router": receiver[:120], "router_known": known,
                     "full_path": full,
                     "parameterized": ("{" in path and "}" in path)},
                    [self.ev.id], schema_version=2))
                self.ext.edges.append(Edge(
                    canonical_edge_id(nid, "handles_route", handler_nid,
                                      self.rel),
                    nid, handler_nid, "handles_route", "structural",
                    "deterministic", 1.0, [self.ev.id],
                    "python-ast", EXTRACTOR_VERSION,
                    file_path=self.rel, start_line=getattr(node, "lineno", 1)))

    def _record_router_assign(self, node) -> None:
        """`name = APIRouter(prefix=...)` / `name = FastAPI()` at module
        scope: record the router variable for route resolution. Non-literal
        prefixes, aliased constructors, and conditional definitions stay
        absent (last plain assignment wins, as at runtime).

        Flask parity: `bp = Blueprint("x", url_prefix="/api")` records the
        blueprint variable with its prefix (composition is service-stage and
        shared); `app = Flask(...)` records the app variable (no prefix).
        """
        if len(self.scope) != 1:
            return
        if not isinstance(node.value, ast.Call):
            return
        try:
            callee = _func_name(node.value.func)
        except Exception:
            return
        short = callee.split(".")[-1]
        if short == "Blueprint":
            prefix = None
            for kw in node.value.keywords or []:
                if kw.arg == "url_prefix":
                    prefix = _str_literal(kw.value)
                    break
            kind = "Blueprint"
        elif short in ("APIRouter", "FastAPI"):
            prefix = None
            for kw in node.value.keywords or []:
                if kw.arg == "prefix":
                    prefix = _str_literal(kw.value)
                    break
            kind = short
        elif short == "Flask":
            prefix, kind = None, "Flask"
        else:
            return
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.isidentifier():
                self.routers[t.id] = {"prefix": prefix, "kind": kind}

    def _harvest_django_paths(self, node) -> None:
        """Collect `path()` / `re_path()` / `url()` entries under a
        module-level `urlpatterns = [...]` assignment (the Django convention;
        anything else stays silent). Views may be names, dotted attributes,
        or `View.as_view()` calls; `include("app.urls")` marks a mount whose
        prefix composes service-stage. Non-literal routes/views stay absent.
        """
        try:
            if not any(isinstance(t, ast.Name) and t.id == "urlpatterns"
                       for t in node.targets):
                return
            val = node.value
            if not isinstance(val, (ast.List, ast.Tuple)):
                return
            for elt in val.elts:
                if not isinstance(elt, ast.Call):
                    continue
                try:
                    callee = _func_name(elt.func)
                except Exception:
                    continue
                short = callee.split(".")[-1]
                if short not in ("path", "re_path", "url"):
                    continue
                if not elt.args:
                    continue
                route = _str_literal(elt.args[0])
                if route is None:
                    continue
                entry: dict = {"route": route[:200],
                               "is_re": short == "re_path"}
                if len(elt.args) >= 2:
                    second = elt.args[1]
                    if isinstance(second, ast.Call):
                        # include("app.urls") or View.as_view()
                        try:
                            inner = _func_name(second.func)
                        except Exception:
                            inner = ""
                        if inner.split(".")[-1] == "include" and second.args:
                            mod = _str_literal(second.args[0])
                            if mod is not None:
                                entry["include"] = mod[:200]
                        else:
                            recv = inner.rsplit(".", 1)
                            if len(recv) == 2 and recv[1] == "as_view":
                                entry["view"] = recv[0][:200]
                    elif isinstance(second, ast.Name):
                        entry["view"] = second.id[:200]
                    elif isinstance(second, ast.Attribute):
                        try:
                            entry["view"] = _func_name(second)[:200]
                        except Exception:
                            pass
                if "view" in entry or "include" in entry:
                    self.django_paths.append(entry)
        except Exception:
            pass

    def visit_Expr(self, node):
        # Module-level calls are otherwise unwalked (call attribution owns
        # named scopes only). Route registrations are the exception: Flask's
        # `app.add_url_rule(...)` / `app.register_blueprint(...)` canonically
        # live at module level, and losing them loses the route. A
        # module-scoped walk emits the factual call edge plus any route
        # synthesis; view_func resolution sees defs above this line
        # (forward refs stay silent, the documented rule).
        # Strictly module scope: function/class bodies already own dedicated
        # walks (their trailing generic_visit would otherwise double-count).
        if len(self.scope) != 1:
            self.generic_visit(node)
            return
        # Every other module-level call (scripts, `if __name__ == "__main__":`,
        # registrations, `x = build()`) is owned by the module the same way.
        self._module_walk(node)

    def _module_walk(self, *exprs):
        """Calls in module-scope code, owned by the module. Nested def/class
        bodies are skipped by _CallWalk (they own their walks)."""
        try:
            mw = _CallWalk(self.ext, self.ev, self.mod_id, self.mod,
                           self.repo, self.rel, self.func_ids,
                           self.scope_ids, http_calls=self.http_calls,
                           pending=self.pending_calls)
            for e in exprs:
                if e is not None:
                    mw.visit(e)
        except Exception:
            pass

    def _module_compound(self, node, *exprs):
        if len(self.scope) == 1:
            self._module_walk(*exprs)
        self.generic_visit(node)       # simple statements inside walk themselves

    def visit_If(self, node):
        self._module_compound(node, node.test)

    def visit_For(self, node):
        self._module_compound(node, node.iter)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self._module_compound(node, node.test)

    def visit_With(self, node):
        self._module_compound(node, *[i.context_expr for i in node.items])

    visit_AsyncWith = visit_With

    def visit_AugAssign(self, node):
        if len(self.scope) == 1:
            self._module_walk(node.value)
        self.generic_visit(node)


    def visit_Assign(self, node):
        # Module-level __all__ = ["a", ...] only (last assignment wins, as at
        # runtime). Anything dynamic (BinOp, calls, .append, non-literals)
        # leaves dunder_all absent: semantics not safely established.
        if len(self.scope) == 1:
            self._module_walk(node.value)      # `app = create_app()`
        try:
            if len(self.scope) == 1:
                self._record_router_assign(node)
                self._harvest_django_paths(node)
                if any(isinstance(t, ast.Name) and t.id == "__all__"
                       for t in node.targets):
                    if isinstance(node.value, (ast.List, ast.Tuple)) and all(
                            isinstance(e, ast.Constant) and isinstance(e.value, str)
                            for e in node.value.elts):
                        self.dunder_all = [e.value[:120] for e in node.value.elts][:500]
                    else:
                        self.dunder_all = None
            self._emit_constants(node, self._constant_targets(node))
        finally:
            self.generic_visit(node)

    def visit_AnnAssign(self, node):
        # `X: T = value` binds; bare `X: T` does not (no assignment).
        # Either way the annotation names a type the scope references: emit a
        # `uses` edge so `x: SomeType` links to `SomeType` (function-arg
        # annotations already do; assignments were silently link-less).
        if len(self.scope) == 1:
            self._module_walk(node.value)
        try:
            ann = getattr(node, "annotation", None)
            if ann is not None:
                try:
                    tname = ast.unparse(ann) if hasattr(ast, "unparse") else "?"
                except Exception:
                    tname = "?"
                if tname and tname != "?":
                    ext_id = f"ext:{self.repo}:{tname[:150]}"
                    if not any(n.id == ext_id for n in self.ext.nodes):
                        self.ext.nodes.append(Node(
                            ext_id, "external_symbol", tname[:120], tname[:200],
                            self.proj, self.repo, "", Location(), "python",
                            "", {}, [self.ev.id], schema_version=2))
                    src = self.scope_id_stack[-1] if self.scope_id_stack \
                        else self.mod_id
                    self.ext.edges.append(Edge(
                        canonical_edge_id(src, "uses", ext_id, self.rel),
                        src, ext_id, "uses", "resolved", "resolved", 0.8,
                        [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                        file_path=self.rel, start_line=node.lineno))
            if node.value is None:
                return
            if isinstance(node.target, ast.Name):
                if node.target.id == "__all__":
                    if len(self.scope) == 1 and \
                            isinstance(node.value, (ast.List, ast.Tuple)) and all(
                                isinstance(e, ast.Constant) and isinstance(e.value, str)
                                for e in node.value.elts):
                        self.dunder_all = [e.value[:120] for e in node.value.elts][:500]
                    return
                self._emit_constants(node, [node.target.id])
        finally:
            self.generic_visit(node)

    def _note_import_scope(self, eid: str) -> None:
        """Where an import binds its name: "" = module scope (visible file-
        wide); otherwise the enclosing function/class qualified name."""
        self.import_scopes.setdefault(eid, set()).add(
            "" if len(self.scope) == 1 else ".".join(self.scope))

    def visit_Import(self, node):
        for a in node.names:
            if a.name in ("pytest",) or a.name.startswith("pytest."):
                self.imports_pytest = True
            if a.name in ("unittest",) or a.name.startswith("unittest."):
                self.imports_unittest = True
            eid = f"ext:{self.repo}:{a.name[:150]}"
            self._note_import_scope(eid)
            if not any(n.id == eid for n in self.ext.nodes):
                self.ext.nodes.append(Node(eid, "external_symbol", a.name[:120], a.name[:200],
                                           self.proj, self.repo, "", Location(), "python",
                                           "", {}, [self.ev.id], schema_version=2))
            self.ext.edges.append(Edge(
                canonical_edge_id(self.mod_id, "imports", eid, self.rel),
                self.mod_id, eid, "imports", "structural", "deterministic", 1.0,
                [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                file_path=self.rel, start_line=node.lineno,
                attributes={"imported": a.name[:150], "alias": a.asname or "",
                            "level": 0, "from_module": "",
                            "names": [a.name[:150]]}))

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        if mod in ("pytest", "unittest") or mod.startswith("pytest.") or \
                mod.startswith("unittest."):
            if mod.startswith("pytest"):
                self.imports_pytest = True
            else:
                self.imports_unittest = True
        for a in node.names:
            full = f"{mod}.{a.name}" if mod else a.name
            eid = f"ext:{self.repo}:{full[:150]}"
            self._note_import_scope(eid)
            if not any(n.id == eid for n in self.ext.nodes):
                self.ext.nodes.append(Node(eid, "external_symbol", full[:120], full[:200],
                                           self.proj, self.repo, "", Location(), "python",
                                           "", {}, [self.ev.id], schema_version=2))
            self.ext.edges.append(Edge(
                canonical_edge_id(self.mod_id, "imports", eid, self.rel),
                self.mod_id, eid, "imports", "structural", "deterministic", 1.0,
                [self.ev.id], "python-ast", EXTRACTOR_VERSION,
                file_path=self.rel, start_line=node.lineno,
                attributes={"imported": full[:150], "alias": a.asname or "",
                            "level": int(getattr(node, "level", 0) or 0),
                            "from_module": mod[:150],
                            "names": [a.name[:150]]}))


def _flask_rule_to_braces(rule: str) -> str:
    """Normalize Flask converters to `{name}` route params.

    `/items/<int:wid>` -> `/items/{wid}`; `/x/<name>` -> `/x/{name}`.
    Unbalanced or empty brackets stay verbatim (no guessing).
    """
    import re as _re
    def _one(m):
        inner = m.group(1)
        if not inner or "<" in inner or ">" in inner:
            return m.group(0)
        name = inner.split(":")[-1].strip()
        if not name.replace("_", "").isalnum():
            return m.group(0)
        return "{" + name + "}"
    return _re.sub(r"<([^/]*)>", _one, rule)[:200]


class _CallWalk(ast.NodeVisitor):
    """Lexical-scope call attribution (Phase 6 Stage B).

    Each walk owns exactly one scope: nested ``def``/``class``/``lambda``
    subtrees are SKIPPED here because they receive dedicated walks with their
    own source symbol (via ``_V.generic_visit``). ``visit_scoped`` covers
    decorator/default subtrees, which evaluate in the enclosing scope.
    Comprehension bodies have no symbol of their own and stay with the
    enclosing scope (documented limitation).
    """

    def __init__(self, res, ev, src_id, src_q, repo, rel, func_ids, scope_ids=None,
                 http_calls=None, pending=None):
        self.pending = pending
        self.res = res
        self.ev = ev
        self.src = src_id
        self.src_q = src_q
        self.repo = repo
        self.rel = rel
        self.func_ids = dict(func_ids)
        self.scope_ids = dict(scope_ids or {})
        # Shared per-file URL harvest (test files only): func node id ->
        # set of URL-like string literals in direct call arguments.
        self.http_calls = http_calls if http_calls is not None else {}
        rel_low = (rel or "").lower().replace("\\", "/")
        self._harvest_urls = "test" in rel_low or "conftest" in rel_low

    def visit_scoped(self, node, src_id):
        prev, self.src = self.src, src_id
        try:
            self.visit(node)
        finally:
            self.src = prev

    def visit_FunctionDef(self, node):
        return None  # dedicated walk owns this scope

    def visit_AsyncFunctionDef(self, node):
        return None  # dedicated walk owns this scope

    def visit_ClassDef(self, node):
        return None  # dedicated walk owns this scope

    def visit_Lambda(self, node):
        return None  # dedicated walk owns this scope

    def _emit_add_url_rule(self, node, dotted: str, route_kw: dict) -> None:
        """Synthesize `route` + `handles_route` for one `add_url_rule` call.

        Links only when `view_func` names exactly one same-file function
        (first-win suffix match, the same rule as call binding); zero or
        several candidates stay silent. The receiver (app/blueprint var) is
        recorded so the service pass composes declared prefixes.
        """
        raw = route_kw.get("rule") or ""
        if not raw.startswith("/"):
            return
        rule = _flask_rule_to_braces(raw)
        vf = str(route_kw.get("view_func") or "")
        vshort = vf.split(".")[-1]
        match = None
        for q, nid in self.func_ids.items():
            if q.endswith("." + vshort) or q == vshort:
                if match is not None:
                    return  # ambiguous: never guess
                match = (q, nid)
        if match is None:
            return
        hq, handler_nid = match
        receiver = dotted.rsplit(".", 1)[0] if "." in dotted else ""
        methods = [m for m in (route_kw.get("methods") or ["GET"])
                   if isinstance(m, str)][:10] or ["GET"]
        for m in sorted(set(methods)):
            q = f"{hq}#{m} {rule}"
            nid = canonical_symbol_id(self.repo, "python", "route", q, "")
            loc = Location(getattr(node, "lineno", 1),
                           getattr(node, "col_offset", 0),
                           getattr(node, "end_lineno", 1) or 1, 0)
            self.res.nodes.append(Node(
                nid, "route", f"{m} {rule}"[:200], normalize_qualified(q),
                self.res.nodes[0].project_id if self.res.nodes else "default",
                self.repo, self.rel, loc, "python", "",
                {"http_method": m, "raw_path": rule,
                 "router": receiver[:120], "router_known": False,
                 "full_path": rule,
                 "parameterized": ("{" in rule and "}" in rule),
                 "via": "add_url_rule"},
                [self.ev.id], schema_version=2))
            self.res.edges.append(Edge(
                canonical_edge_id(nid, "handles_route", handler_nid, self.rel),
                nid, handler_nid, "handles_route", "structural",
                "deterministic", 1.0, [self.ev.id],
                "python-ast", EXTRACTOR_VERSION,
                file_path=self.rel, start_line=getattr(node, "lineno", 1)))

    def visit_Call(self, node):
        name = _func_name(node.func)
        short = name.split(".")[-1][:120] if name else "?"
        # Route registration keywords (bounded allowlist only) + test URL
        # harvest. Neither affects call binding.
        route_kw: dict = {}
        if short in ROUTE_INCLUDE_CALLS:
            try:
                if short == "add_url_rule":
                    # Flask: app.add_url_rule("/rule", endpoint=..,
                    #   view_func=fn, methods=[...]). The first arg is the
                    # URL rule itself (not a router); the handler arrives
                    # via view_func. The service pass synthesizes the route
                    # node once view_func resolves unambiguously.
                    if node.args:
                        rule = _str_literal(node.args[0])
                        if rule is not None:
                            route_kw["rule"] = rule[:200]
                    for kw in node.keywords or []:
                        if kw.arg == "view_func":
                            try:
                                route_kw["view_func"] = \
                                    _func_name(kw.value)[:150]
                            except Exception:
                                pass
                        elif kw.arg == "methods" and isinstance(
                                kw.value, (ast.List, ast.Tuple)):
                            ms = []
                            for e in kw.value.elts:
                                s = _str_literal(e)
                                if s and s.lower() in HTTP_METHODS:
                                    ms.append(s.upper())
                            if ms:
                                route_kw["methods"] = sorted(set(ms))[:10]
                    if "methods" not in route_kw:
                        route_kw["methods"] = ["GET"]
                else:
                    try:
                        recv = _func_name(node.func)
                    except Exception:
                        recv = ""
                    if "." in recv:
                        # the router this one is mounted into (nested prefixes)
                        route_kw["into"] = recv.rsplit(".", 1)[0][:150]
                    if node.args and len(node.args) >= 1:
                        first = node.args[0]
                        if isinstance(first, ast.Name):
                            route_kw["router"] = first.id[:150]
                        elif isinstance(first, ast.Attribute):
                            route_kw["router"] = _func_name(first)[:150]
                    for kw in node.keywords or []:
                        if kw.arg in ("prefix", "url_prefix") and \
                                _str_literal(kw.value) is not None:
                            route_kw["prefix"] = _str_literal(kw.value)
                        elif kw.arg in ("prefix", "url_prefix") and \
                                isinstance(kw.value, (ast.Name, ast.Attribute)):
                            # `prefix=settings.API_V1_STR`: resolved service-stage
                            route_kw["prefix_ref"] = _func_name(kw.value)[:150]
                        elif kw.arg == "view_func":
                            try:
                                route_kw["view_func"] = \
                                    _func_name(kw.value)[:150]
                            except Exception:
                                pass
            except Exception:
                route_kw = {}
        if short == "add_url_rule" and route_kw.get("rule") \
                and route_kw.get("view_func"):
            # Flask programmatic registration: synthesize the route node now
            # (same-file view only; imported views stay silent). Prefix
            # composition reuses the normal service pass via `router`.
            self._emit_add_url_rule(node, name, route_kw)
        if self._harvest_urls:
            try:
                for a in node.args or []:
                    s = _url_template(a)
                    if s is not None and _is_url_literal(s):
                        self.http_calls.setdefault(self.src, set()).add(s)
            except Exception:
                pass
        # Per-call-site identity (Phase 4 Stage 1, occurrences Phase 5 Stage 1):
        # callee spelling + arity + sorted keyword names. No line numbers:
        # inserting lines above keeps the ID, editing the call changes it.
        # Textually identical twins share one logical edge whose `occurrences`
        # list keeps every span; distinct sites never collapse into each other.
        # Route registrations additionally fold router/prefix into the site so
        # two include_router calls with the same call shape but different
        # routers stay distinct (ServisPilot E-06: web vs api collapsed to one
        # edge and the /api prefix was lost).
        site = _call_site(node, name)
        if route_kw:
            from ...core.identifiers import site_fingerprint as _sf
            site = _sf(site, str(route_kw.get("router", "")),
                       str(route_kw.get("prefix", "")),
                       str(route_kw.get("view_func", "")))
        # a call chained on a new line (`d` / `.m()`) starts at its receiver; its name is on the attribute's line
        name_line = getattr(node, "lineno", 1)
        if isinstance(node.func, ast.Attribute):
            name_line = getattr(node.func, "end_lineno", name_line) or name_line
        occ = [{"line": name_line,
                "end_line": getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1,
                "col": getattr(node, "col_offset", 0)}]
        call = {"walk": self, "src": self.src, "src_q": self.src_q, "name": name,
                "short": short, "site": site, "occ": occ, "route_kw": route_kw,
                "line": name_line}
        if self.pending is not None:
            self.pending.append(call)
        else:  # standalone walk (no file context): never guess a binding
            self._emit_call(call, None)
        self.generic_visit(node)

    def _emit_call(self, call: dict, target: str | None) -> None:
        name, short, site = call["name"], call["short"], call["site"]
        occ, route_kw = call["occ"], call["route_kw"]
        line = call["line"]
        src = call["src"]
        if target:
            attrs = {"call_spelling": name[:200], "occurrences": occ}
            if route_kw:
                attrs["route_include"] = route_kw
            self.res.edges.append(Edge(
                canonical_edge_id(src, "calls", target, self.rel, site),
                src, target, "calls", "resolved", "resolved", 0.85,
                [self.ev.id], "python-call-resolution", CALL_RESOLUTION_VERSION,
                file_path=self.rel, start_line=line,
                attributes=attrs))
        else:
            eid = f"ext:{self.repo}:{name[:120]}"
            if not any(n.id == eid for n in self.res.nodes):
                proj = self.res.nodes[0].project_id if self.res.nodes else "default"
                self.res.nodes.append(Node(eid, "external_symbol", short, name[:200],
                                           proj, self.repo, "", Location(), "python",
                                           "", {}, [self.ev.id], schema_version=2))
            attrs = {"unresolved_call": name[:200], "occurrences": occ}
            if call.get("receiver_type"):
                attrs["receiver_type"] = call["receiver_type"][:200]
            if route_kw:
                attrs["route_include"] = route_kw
            self.res.edges.append(Edge(
                canonical_edge_id(src, "calls_unresolved", eid, self.rel,
                                  site),
                src, eid, "calls_unresolved", "structural", "deterministic", 1.0,
                [self.ev.id], "python-call-resolution", CALL_RESOLUTION_VERSION,
                file_path=self.rel, start_line=line,
                attributes=attrs))


_MRO_LIMIT = 16


def _bind_pending_calls(v: "_V") -> None:
    """Bind each collected call to a same-file definition, receiver-aware.

    Rules (anything else stays `calls_unresolved` for the service pass):
      f()            lexical lookup: enclosing function scopes, then module.
                     Class scopes are skipped (Python semantics: a method body
                     does not see class attributes by bare name).
      self.m/cls.m   nearest enclosing class, then its same-file bases (MRO
                     order, depth-first, cycle-guarded).
      super().m      same-file bases of the enclosing class only — never the
                     calling method itself.
      C.m / C().m    C names a same-file class reachable lexically.
    Receivers that are attributes, locals, parameters or modules
    (`self._stream.write`, `sys.exit`, `runner.invoke`) never bind by name.
    """
    funcs = v.func_ids
    classes = v.class_bases
    mod = v.mod

    def lexical(src_q: str, name: str, pool) -> str | None:
        parts = src_q.split(".")
        mod_len = len(mod.split("."))
        for i in range(len(parts), mod_len - 1, -1):
            prefix = ".".join(parts[:i])
            if prefix in classes and prefix != src_q:
                continue
            q = f"{prefix}.{name}"
            if q in pool:
                return q
        return None

    def enclosing_class(src_q: str) -> str | None:
        parts = src_q.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in classes:
                return prefix
        return None

    def mro(cls_q: str, include_self: bool) -> list[str]:
        out, seen, stack = [], set(), [(cls_q, include_self)]
        while stack and len(out) < _MRO_LIMIT:
            c, inc = stack.pop(0)
            if c in seen:
                continue
            seen.add(c)
            if inc:
                out.append(c)
            parent = c.rsplit(".", 1)[0]
            bases = []
            for b in classes.get(c, []):
                if "." in b or not b:
                    continue  # qualified/external base: service pass territory
                bq = lexical(parent, b, classes)
                if bq:
                    bases.append((bq, True))
            stack = bases + stack
        return out

    def annotated(src_q: str, var: str) -> str:
        """Declared type of `var` as a parameter of src or an enclosing
        function (closures see outer parameters), innermost first."""
        parts = src_q.split(".")
        for i in range(len(parts), 0, -1):
            pt = v.param_types.get(".".join(parts[:i]))
            if pt and var in pt:
                return pt[var]
        return ""

    def method_in(cls_list, m):
        for c in cls_list:
            q = f"{c}.{m}"
            if q in funcs:
                return funcs[q]
        return None

    for call in v.pending_calls:
        name, src_q = call["name"] or "", call["src_q"] or mod
        parts = name.split(".")
        target = None
        if len(parts) == 1 and name not in ("", "?"):
            q = lexical(src_q, name, funcs)
            target = funcs.get(q) if q else None
        elif len(parts) == 2:
            base, m = parts
            if base in ("self", "cls"):
                c = enclosing_class(src_q)
                if c:
                    target = method_in(mro(c, True), m)
            elif base == "super":
                c = enclosing_class(src_q)
                if c:
                    target = method_in(mro(c, False), m)
            else:
                cq = lexical(src_q, base, classes)
                ann = annotated(src_q, base) if not cq else ""
                if cq:
                    target = method_in(mro(cq, True), m)
                elif ann:
                    # Parameter declared `base: T`: the receiver type is
                    # syntax. Same-file T binds here; imported T is left
                    # for py_callbind via the `receiver_type` attribute.
                    tq = lexical(src_q, ann, classes) if "." not in ann else None
                    if tq:
                        target = method_in(mro(tq, True), m)
                    else:
                        call["receiver_type"] = ann
        call["walk"]._emit_call(call, target)
    v.pending_calls.clear()


def _call_site(node, spelling: str) -> str:
    """Stable fingerprint for one Python call site (no positions).

    Combines the callee spelling with pure-syntax call-shape facts: positional
    arity, sorted keyword-argument names, and starred-argument presence. This is
    deliberately Python-shaped (ast keyword nodes) rather than a copy of the
    JavaScript site fingerprint, whose import-binding fields do not exist here.
    """
    from ...core.identifiers import site_fingerprint
    kwnames: list[str] = []
    starred = False
    try:
        for a in node.args:
            if isinstance(a, ast.Starred):
                starred = True
        for kw in node.keywords:
            if kw.arg is None:
                starred = True
            else:
                kwnames.append(kw.arg[:60])
    except Exception:
        pass
    try:
        arity = len(node.args)
    except Exception:
        arity = 0
    return site_fingerprint(spelling or "", str(arity), ",".join(sorted(kwnames)),
                            "star" if starred else "")


def _func_name(fn) -> str:
    """Clean dotted name without call args: store.cx.execute(...).fetchone -> store.cx.execute.fetchone."""
    import ast as _a
    if isinstance(fn, _a.Name):
        return fn.id[:200]
    if isinstance(fn, _a.Attribute):
        base = _func_name(fn.value)
        return f"{base}.{fn.attr}"[:200] if base else fn.attr[:200]
    if isinstance(fn, _a.Call):
        return _func_name(fn.func)
    if isinstance(fn, _a.Subscript):
        return _func_name(fn.value)
    try:
        return (_a.unparse(fn) if hasattr(_a, "unparse") else "?").split("(")[0][:200]
    except Exception:
        return "?"


open_dummy = ""
