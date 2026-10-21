from __future__ import annotations
import re
from pathlib import Path
from ..core.models import ExtractionResult
from ..core.provenance import make_evidence
from ..core.identifiers import file_node_id, canonical_edge_id, canonical_symbol_id, content_hash
from ..core.models import Node, Edge, Location
from .code.python_extractor import PythonExtractor, EXTRACTOR_VERSION as PY_VERSION
from .code import tree_sitter as ts_adapter
from .code.jsts import JsTsExtractor, JSTS_VERSION
from .code.ts_universal import UNIV_TS_VERSION
from .documents.markdown import extract_markdown, MD_VERSION
from .structured.json_ext import extract_json
from .structured.yaml_ext import extract_yaml, YAML_VERSION
from .structured.toml_ext import extract_toml
from .structured.infra import extract_dockerfile, DOCKER_VERSION
from .structured.sql import extract_sql, SQL_VERSION
from .structured.terraform import extract_terraform, TF_VERSION

GENERIC_VERSION = "6"
_PY = PythonExtractor()

# Single source of truth for suffix -> extractor routing (drift lock:
# tests/integration/test_phase25_lang_perfection.py asserts this matrix and
# the real dispatch agree, so docs and code can never silently diverge).
# depth: deep = full AST facts; structured = format-aware facts;
#        fallback = coarse symbol scan, reported partial (never deep);
#        file-only = tracked, no symbols.
LANGUAGE_MATRIX: dict[str, dict[str, str]] = {
    ".py": {"extractor": "python-ast", "depth": "deep"},
    ".js": {"extractor": "jsts", "depth": "deep"},
    ".mjs": {"extractor": "jsts", "depth": "deep"},
    ".cjs": {"extractor": "jsts", "depth": "deep"},
    ".jsx": {"extractor": "jsts", "depth": "deep"},
    ".ts": {"extractor": "jsts", "depth": "deep"},
    ".mts": {"extractor": "jsts", "depth": "deep"},
    ".cts": {"extractor": "jsts", "depth": "deep"},
    ".tsx": {"extractor": "jsts", "depth": "deep"},
    ".md": {"extractor": "markdown", "depth": "structured"},
    ".markdown": {"extractor": "markdown", "depth": "structured"},
    ".txt": {"extractor": "markdown", "depth": "structured"},
    ".rst": {"extractor": "markdown", "depth": "structured"},
    ".json": {"extractor": "json", "depth": "structured"},
    ".yml": {"extractor": "yaml", "depth": "structured"},
    ".yaml": {"extractor": "yaml", "depth": "structured"},
    ".toml": {"extractor": "toml", "depth": "structured"},
    ".go": {"extractor": "ts-go", "depth": "deep"},
    ".rs": {"extractor": "ts-rust", "depth": "deep"},
    ".java": {"extractor": "ts-java", "depth": "deep"},
    ".c": {"extractor": "ts-c", "depth": "deep"},
    # Phase 41 Wave 3: C++ goes deep on unambiguous suffixes only.
    # `.h` is shared by C, C++ and Objective-C, so each header is sniffed
    # (`_header_language`): ObjC markers keep the generic fallback (the
    # C++ grammar yields ERROR garbage on `@interface`, measured), C++
    # markers parse as cpp, the rest as c. language_overrides still wins.
    ".h": {"extractor": "ts-cpp", "depth": "deep"},
    ".cpp": {"extractor": "ts-cpp", "depth": "deep"},
    # Phase 32 (evidence: docs/architecture/PHASE32_LANG_EVIDENCE.md):
    # C++ family completion (.cc/.hpp/.hxx/.ipp/.cxx — same yield class as
    # .cpp/.h) + Dart/Swift/GDScript (real-project usage, non-zero scanner
    # yield). Fallback depth only: coarse scan, partial, warned, never deep.
    # Phase 41 Wave 3: the C++-unambiguous suffixes below go ts-cpp deep;
    # Dart/Swift/GDScript stay fallback (Wave 4 spike decides).
    # Phase 42 Wave 4: the risk spike measured call-carrying on all three
    # grammars (Swift clean; Dart strict sibling-sequences; GDScript
    # attribute receivers) — no depth downgrade, all three go deep.
    ".cc": {"extractor": "ts-cpp", "depth": "deep"},
    ".hpp": {"extractor": "ts-cpp", "depth": "deep"},
    ".hxx": {"extractor": "ts-cpp", "depth": "deep"},
    ".ipp": {"extractor": "ts-cpp", "depth": "deep"},
    ".cxx": {"extractor": "ts-cpp", "depth": "deep"},
    ".dart": {"extractor": "ts-dart", "depth": "deep"},
    ".swift": {"extractor": "ts-swift", "depth": "deep"},
    ".gd": {"extractor": "ts-gdscript", "depth": "deep"},
    # Phase 34 (evidence: local Kotlin usage .kt 2 + .kts 7 across 2
    # projects; `fun`/`object`/`data class` yield measured non-zero).
    # Phase 41 Wave 3: Kotlin goes ts-kotlin deep (phantom has_error bit
    # measured harmless; engine trusts error/missing counts).
    ".kt": {"extractor": "ts-kotlin", "depth": "deep"},
    ".kts": {"extractor": "ts-kotlin", "depth": "deep"},
    # Phase 37 (comprehensive pass — evidence table in
    # docs/architecture/PHASE32_LANG_EVIDENCE.md section 7): every
    # candidate suffix measured; non-zero scanner yield joins the
    # fallback at honest depth, zero-yield/grammar-only stays out
    # (shell, Erlang, Vue, markup/config). Scanner v6 carries the
    # matching machinery (case-insensitive, @-forms, local/sub/defp/
    # proc/data/program/procedure/implementation, Lisp/R forms).
    # Phase 41 Wave 3: Lua goes ts-lua deep (chunk module pattern +
    # require alias binding measured on the 0.5.0 grammar).
    ".lua": {"extractor": "ts-lua", "depth": "deep"},
    ".zig": {"extractor": "generic-text", "depth": "fallback"},
    ".scala": {"extractor": "generic-text", "depth": "fallback"},
    ".groovy": {"extractor": "generic-text", "depth": "fallback"},
    ".jl": {"extractor": "generic-text", "depth": "fallback"},
    ".ex": {"extractor": "generic-text", "depth": "fallback"},
    ".exs": {"extractor": "generic-text", "depth": "fallback"},
    ".f": {"extractor": "generic-text", "depth": "fallback"},
    ".for": {"extractor": "generic-text", "depth": "fallback"},
    ".f90": {"extractor": "generic-text", "depth": "fallback"},
    ".f95": {"extractor": "generic-text", "depth": "fallback"},
    ".m": {"extractor": "generic-text", "depth": "fallback"},
    ".mm": {"extractor": "generic-text", "depth": "fallback"},
    ".ps1": {"extractor": "generic-text", "depth": "fallback"},
    ".psm1": {"extractor": "generic-text", "depth": "fallback"},
    ".pl": {"extractor": "generic-text", "depth": "fallback"},
    ".pm": {"extractor": "generic-text", "depth": "fallback"},
    ".t": {"extractor": "generic-text", "depth": "fallback"},
    ".hs": {"extractor": "generic-text", "depth": "fallback"},
    ".r": {"extractor": "generic-text", "depth": "fallback"},
    ".tcl": {"extractor": "generic-text", "depth": "fallback"},
    ".vb": {"extractor": "generic-text", "depth": "fallback"},
    ".vbs": {"extractor": "generic-text", "depth": "fallback"},
    ".pas": {"extractor": "generic-text", "depth": "fallback"},
    ".dpr": {"extractor": "generic-text", "depth": "fallback"},
    ".scm": {"extractor": "generic-text", "depth": "fallback"},
    ".ss": {"extractor": "generic-text", "depth": "fallback"},
    ".clj": {"extractor": "generic-text", "depth": "fallback"},
    ".cljs": {"extractor": "generic-text", "depth": "fallback"},
    ".el": {"extractor": "generic-text", "depth": "fallback"},
    ".awk": {"extractor": "generic-text", "depth": "fallback"},
    ".cs": {"extractor": "ts-c_sharp", "depth": "deep"},
    ".php": {"extractor": "ts-php", "depth": "deep"},
    ".rb": {"extractor": "ts-ruby", "depth": "deep"},
    ".rake": {"extractor": "ts-ruby", "depth": "deep"},
    ".sql": {"extractor": "sql", "depth": "structured"},
    ".tf": {"extractor": "terraform", "depth": "structured"},
}


def normalize_overrides(raw) -> dict:
    """Local alias: `{'.ext': 'language'}` or {} (never raises)."""
    try:
        from ..config import normalize_language_overrides as _norm
        return _norm(raw)
    except Exception:
        if not isinstance(raw, dict):
            return {}
        return {}


# Override value -> extractor cache key (must match _ext_name_for in service).
_OVERRIDE_EXT_KEYS = {"python": "python-ast", "javascript": "jsts",
                      "typescript": "jsts", "tsx": "jsts",
                      "generic-text": "generic-text", "file-only": "file-only",
                      "markdown": "markdown", "json": "json", "yaml": "yaml",
                      "toml": "toml",
                      # Phase 41 Wave 3: ambiguity hatch for shared
                      # suffixes (`.h` C/C++/ObjC). Opt-in, explicit.
                      "cpp": "ts-cpp", "c": "ts-c",
                      "kotlin": "ts-kotlin", "lua": "ts-lua", "ruby": "ts-ruby"}


def ext_name_for(rel_path: str, language_overrides=None) -> str:
    """Extractor cache key for a repo-relative path, override-aware.

    Shared with service._ext_name_for so cache lookup and actual dispatch
    can never disagree (a file re-parsed under a different language never
    reuses the old entry).
    """
    from pathlib import Path as _P
    ov = normalize_overrides(language_overrides)
    hit = ov.get(_P(rel_path).suffix.lower())
    if hit:
        return _OVERRIDE_EXT_KEYS.get(hit, "generic-text")
    s = _P(rel_path).suffix.lower()
    entry = LANGUAGE_MATRIX.get(s)
    if entry is not None:
        return entry["extractor"]
    return "generic-text"


def _dispatch_override(abs_path: str, rel_path: str, repo_id: str,
                       project_id: str, lang: str):
    """Route one file to a declared language. Returns None to fall through
    to default dispatch (should not happen for allowlisted values)."""
    if lang == "python":
        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            r = ExtractionResult(extractor="io", extractor_version="1", partial=True)
            r.warnings.append(str(e))
            return r
        return _PY.extract(rel_path, text, repo_id, project_id)
    if lang in ("javascript", "typescript", "tsx"):
        caps = ts_adapter.capabilities()
        if caps.get("languages", {}).get(lang, {}).get("initialized"):
            try:
                return JsTsExtractor(lang).extract(rel_path, Path(abs_path).read_text(
                    encoding="utf-8", errors="ignore"), repo_id, project_id)
            except Exception as e:
                res = ExtractionResult(extractor=f"jsts-{lang}",
                                       extractor_version=JSTS_VERSION, partial=True)
                res.warnings.append(f"jsts {lang} crashed on {rel_path}: {e}")
                return res
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        res = _generic_text(rel_path, text, repo_id, project_id,
                            Path(rel_path).suffix.lower())
        res.warnings.append(
            f"{rel_path}: tree-sitter {lang} unavailable; "
            f"generic fallback used (not deep analysis)")
        res.partial = True
        for n in res.nodes:
            if n.type == "file":
                n.attributes = dict(n.attributes or {})
                n.attributes["parser"] = {"backend": "generic-fallback",
                                          "status": "fallback",
                                          "language": lang}
        return res
    if lang in ts_adapter.UNIV_LANGUAGE_IDS:
        # Phase 41: ambiguity hatch (e.g. {".h": "cpp"}). Grammar present
        # -> deep; absent -> honest fallback (jsts-override parity).
        from .code.ts_universal import UniversalExtractor, UNIV_TS_VERSION
        caps = ts_adapter.univ_capabilities()
        if caps.get("languages", {}).get(lang, {}).get("initialized"):
            try:
                return UniversalExtractor(lang).extract(
                    rel_path, Path(abs_path).read_text(
                        encoding="utf-8", errors="ignore"),
                    repo_id, project_id)
            except Exception as e:
                res = ExtractionResult(extractor=f"ts-{lang}",
                                       extractor_version=UNIV_TS_VERSION,
                                       partial=True)
                res.warnings.append(f"ts-{lang} crashed on {rel_path}: {e}")
                return res
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        res = _generic_text(rel_path, text, repo_id, project_id,
                            Path(rel_path).suffix.lower())
        res.warnings.append(
            f"{rel_path}: tree-sitter {lang} unavailable; "
            f"generic fallback used (not deep analysis)")
        res.partial = True
        for n in res.nodes:
            if n.type == "file":
                n.attributes = dict(n.attributes or {})
                n.attributes["parser"] = {"backend": "generic-fallback",
                                          "status": "fallback",
                                          "language": lang}
        return res
    if lang == "generic-text":
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        return _generic_text(rel_path, text, repo_id, project_id,
                             Path(rel_path).suffix.lower())
    if lang == "file-only":
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        raw = text.encode("utf-8", errors="ignore")
        ev = make_evidence(rel_path, text[:200], "file-only", raw)
        res = ExtractionResult(extractor="file-only", extractor_version="1")
        res.evidence.append(ev)
        fid = file_node_id(repo_id, rel_path)
        res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id,
                              rel_path, Location(1, 0, 1, 0), "text",
                              content_hash(raw), {}, [ev.id], schema_version=2))
        return res
    if lang == "markdown":
        return extract_markdown(rel_path, Path(abs_path).read_text(
            encoding="utf-8", errors="ignore"), repo_id, project_id)
    if lang == "json":
        return extract_json(rel_path, Path(abs_path).read_text(
            encoding="utf-8", errors="ignore"), repo_id, project_id)
    if lang == "yaml":
        return extract_yaml(rel_path, Path(abs_path).read_text(
            encoding="utf-8", errors="ignore"), repo_id, project_id)
    if lang == "toml":
        return extract_toml(rel_path, Path(abs_path).read_text(
            encoding="utf-8", errors="ignore"), repo_id, project_id)
    return None

# Suffixes that only get the coarse generic-text symbol scan (no imports,
# calls, or cross-file facts). Derived from LANGUAGE_MATRIX (single source);
# the fallback branch below reports them as fallback, never deep analysis.
GENERIC_FALLBACK_SUFFIXES = tuple(
    s for s, e in LANGUAGE_MATRIX.items() if e["depth"] == "fallback")


_OBJC_MARK = re.compile(r"^\s*(@interface|@implementation|@protocol|@end|@property|#import)\b", re.M)
_CPP_MARK = re.compile(r"^\s*(class|namespace|template\s*<|using\s+namespace|public:|private:|protected:)"
                       r"|::|\bvirtual\b|\bconstexpr\b|\bnoexcept\b", re.M)


def _header_language(text: str) -> str | None:
    """`.h` -> "cpp" | "c", or None for Objective-C (kept on the fallback)."""
    if _OBJC_MARK.search(text):
        return None
    return "cpp" if _CPP_MARK.search(text) else "c"


def _crash_fallback(rel_path, text, repo_id, project_id, suffix, extractor, version, lang, exc):
    """A parser crash must not make the file vanish from the graph (an
    empty result left it with no file node at all): keep the generic text
    facts, keyed to the deep extractor so a fixed version re-parses it, and
    say on the file that deep analysis failed."""
    res = _generic_text(rel_path, text, repo_id, project_id, suffix)
    res.extractor, res.extractor_version, res.partial = extractor, version, True
    res.warnings.append(f"{extractor} crashed on {rel_path}: {type(exc).__name__}: {exc}; "
                        f"generic fallback used (not deep analysis)")
    for n in res.nodes:
        if n.type == "file":
            n.attributes = dict(n.attributes or {})
            n.attributes["parser"] = {"backend": "generic-fallback", "status": "crashed",
                                      "language": lang, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return res


def extract_file(abs_path: str, rel_path: str, repo_id: str, project_id: str,
                 language_overrides=None) -> ExtractionResult:
    res = _extract_file(abs_path, rel_path, repo_id, project_id, language_overrides)
    # schema stated in code (framework migrations, Prisma): tables/columns/drops
    from .structured.schema_code import augment, is_schema_path
    if is_schema_path(rel_path):
        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
            augment(res, rel_path, text, repo_id, project_id)
        except Exception as e:  # noqa: BLE001 - schema facts are additive, never fatal
            res.warnings.append(f"schema-code on {rel_path}: {type(e).__name__}: {e}")
    return res


def _extract_file(abs_path: str, rel_path: str, repo_id: str, project_id: str,
                  language_overrides=None) -> ExtractionResult:
    suffix = Path(rel_path).suffix.lower()
    name = Path(rel_path).name
    ov = normalize_overrides(language_overrides).get(suffix)
    if ov:
        try:
            res = _dispatch_override(abs_path, rel_path, repo_id, project_id, ov)
        except OSError as e:
            r = ExtractionResult(extractor="io", extractor_version="1", partial=True)
            r.warnings.append(str(e))
            return r
        if res is not None:
            res.warnings.append(
                f"{rel_path}: language override {suffix}->{ov} "
                f"(declared, not detected)")
            for n in res.nodes:
                if n.type == "file":
                    n.attributes = dict(n.attributes or {})
                    n.attributes["language_override"] = {"from": suffix, "to": ov}
            return res
    try:
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        r = ExtractionResult(extractor="io", extractor_version="1", partial=True)
        r.warnings.append(str(e))
        return r
    if suffix == ".py":
        return _PY.extract(rel_path, text, repo_id, project_id)
    lang = ts_adapter.language_for_extension(suffix)
    if lang is not None:
        caps = ts_adapter.capabilities()
        if caps.get("languages", {}).get(lang, {}).get("initialized"):
            try:
                return JsTsExtractor(lang).extract(rel_path, text, repo_id, project_id)
            except Exception as e:
                return _crash_fallback(rel_path, text, repo_id, project_id, suffix,
                                       f"jsts-{lang}", JSTS_VERSION, lang, e)
        res = _generic_text(rel_path, text, repo_id, project_id, suffix)
        res.warnings.append(
            f"{rel_path}: tree-sitter {lang} unavailable "
            f"({caps.get('reason', caps.get('languages', {}).get(lang, {}).get('reason', 'missing package'))}); "
            f"generic fallback used (not deep analysis)")
        res.partial = True
        for n in res.nodes:
            if n.type == "file":
                n.attributes = dict(n.attributes or {})
                n.attributes["parser"] = {"backend": "generic-fallback", "status": "fallback",
                                          "language": lang}
        return res
    ulang = ts_adapter.univ_language_for_extension(suffix)
    if suffix == ".h":
        ulang = _header_language(text)
        if ulang is None:
            res = _generic_text(rel_path, text, repo_id, project_id, suffix)
            res.warnings.append(f"{rel_path}: Objective-C header; generic "
                                f"fallback used (not deep analysis)")
            res.partial = True
            return res
    if ulang is not None:
        caps = ts_adapter.univ_capabilities()
        if caps.get("languages", {}).get(ulang, {}).get("initialized"):
            try:
                from .code.ts_universal import UniversalExtractor
                return UniversalExtractor(ulang).extract(rel_path, text,
                                                         repo_id, project_id)
            except Exception as e:
                from .code.ts_universal import UNIV_TS_VERSION
                return _crash_fallback(rel_path, text, repo_id, project_id, suffix,
                                       f"ts-{ulang}", UNIV_TS_VERSION, ulang, e)
        res = _generic_text(rel_path, text, repo_id, project_id, suffix)
        res.warnings.append(
            f"{rel_path}: tree-sitter {ulang} unavailable "
            f"({caps.get('reason', caps.get('languages', {}).get(ulang, {}).get('reason', 'missing package'))}); "
            f"generic fallback used (not deep analysis)")
        res.partial = True
        for n in res.nodes:
            if n.type == "file":
                n.attributes = dict(n.attributes or {})
                n.attributes["parser"] = {"backend": "generic-fallback", "status": "fallback",
                                          "language": ulang}
        return res
    if suffix == ".md" or suffix == ".markdown" or suffix == ".txt" or suffix == ".rst":
        return extract_markdown(rel_path, text, repo_id, project_id)
    if suffix == ".json":
        return extract_json(rel_path, text, repo_id, project_id)
    if suffix in (".yml", ".yaml"):
        return extract_yaml(rel_path, text, repo_id, project_id)
    if suffix == ".toml":
        return extract_toml(rel_path, text, repo_id, project_id)
    if suffix == ".sql":
        return extract_sql(rel_path, text, repo_id, project_id)
    if suffix == ".tf":
        return extract_terraform(rel_path, text, repo_id, project_id)
    if name.lower() in ("dockerfile",) or rel_path.lower().endswith("dockerfile") or suffix in (".cfg", ".ini", ".env"):
        return extract_dockerfile(rel_path, text, repo_id, project_id)
    if suffix in GENERIC_FALLBACK_SUFFIXES:
        res = _generic_text(rel_path, text, repo_id, project_id, suffix)
        res.warnings.append(
            f"{rel_path}: {suffix} has no deep extractor; "
            f"generic fallback used (coarse symbol scan only: no imports, "
            f"calls, or cross-file facts)")
        res.partial = True
        for n in res.nodes:
            if n.type == "file":
                n.attributes = dict(n.attributes or {})
                n.attributes["parser"] = {"backend": "generic-fallback",
                                          "status": "fallback",
                                          "language": suffix.lstrip(".")}
        return res
    # default: file node only
    res = ExtractionResult(extractor="file-only", extractor_version="1")
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:200], "file-only", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                          Location(1, 0, 1, 0), "text", content_hash(raw), {}, [ev.id],
                          schema_version=2))
    return res


import re as _re

# Coarse declaration scanner (fallback depth only). Leading modifiers never
# block a keyword; container keywords broaden the catch. Case-insensitive
# (VB/Fortran-upcase idioms); `@`-prefixed declarations (ObjC) accepted.
# See _generic_text for the deliberate boundaries.
_GENERIC_MODIFIERS = ("public", "private", "protected", "internal",
                      "static", "final", "abstract", "sealed", "readonly",
                      "async", "export", "default", "override", "virtual",
                      "extern", "partial", "data", "local", "unsafe",
                      r"pub(?:\([^)]*\))?")
# NOTE: `data` is a modifier ONLY, never a keyword: Go struct fields
# (`data map[string]string`) collide with Haskell `data` declarations,
# and real-world field noise beats zero-local-usage recall (agent-proven,
# Phase 37). Haskell `data` decls stay missed by design.
_GENERIC_KEYWORDS = ("def", "class", "function", "func", "fn", "fun",
                     "namespace", "interface", "trait", "struct", "enum",
                     "module", "type", "object", "mixin", "extension",
                     "record", "sub", "subroutine", "defp", "proc",
                     "program", "procedure", "implementation", "unit",
                     "mod", "crate")
_GENERIC_DECL_RE = _re.compile(
    r"\s*@?(?:(?:" + "|".join(_GENERIC_MODIFIERS) + r")\s+)*("
    + "|".join(_GENERIC_KEYWORDS) + r")\s+([A-Za-z_][\w]*)",
    _re.IGNORECASE)
# Lisp parenthesized definitions: `(defn name`, `(def name`, `(ns name`,
# `(define (name`, `(defun name`. Only lisps open a line this way.
_GENERIC_LISP_RE = _re.compile(
    r"\(\s*(define|defun|defn|def|ns)\s+\(?([A-Za-z_][\w]*)")
# R assignment-form definitions: `name <- function`.
_GENERIC_R_RE = _re.compile(r"([A-Za-z_][\w.]*)\s*<-\s*function\b")
# Tcl `namespace eval Name` declares Name (the bare `namespace` rule
# would mint the `eval` subcommand, which the reserved-name guard drops).
_GENERIC_TCLNS_RE = _re.compile(r"namespace\s+eval\s+([A-Za-z_][\w]*)",
                               _re.IGNORECASE)
# MATLAB/Octave assignment-form definitions: `function out = name(args)`
# (also multi-output `function [a, b] = name(args)`). Tried before the
# generic keyword rule, which would otherwise mint the output variable.
_GENERIC_MATLAB_RE = _re.compile(
    r"function\s+(?:\[[^\]]*\]|[A-Za-z_][\w.]*)\s*=\s*([A-Za-z_]\w*)\s*\(",
    _re.IGNORECASE)
# `eval` is a builtin invocation (Tcl `namespace eval`), never a
# definition name; minting it would be a false symbol.
_GENERIC_RESERVED_NAMES = frozenset({"eval"})


def _generic_text(rel_path: str, text: str, repo_id: str, project_id: str,
                  suffix: str) -> ExtractionResult:
    res = ExtractionResult(extractor="generic-text", extractor_version=GENERIC_VERSION)
    raw = text.encode("utf-8", errors="ignore")
    ev = make_evidence(rel_path, text[:300], "generic-text", raw)
    res.evidence.append(ev)
    fid = file_node_id(repo_id, rel_path)
    res.nodes.append(Node(fid, "file", rel_path, rel_path, project_id, repo_id, rel_path,
                          Location(1, 0, max(1, len(text.splitlines())), 0), "text",
                          content_hash(raw), {"suffix": suffix}, [ev.id], schema_version=2))
    # light symbol scan: keyword-led declarations only (Phase 34: modifier
    # tolerant, extended keyword set). Stable IDs (no lines); per-file
    # owner-qualified identity (same name in two files = two nodes, like
    # module-qualified Python/JS IDs) plus an occurrence discriminator for
    # duplicate names in one file. Without the owner qualifier, re-parsing
    # one file flips shared-name ownership (agent-observed, Phase 34).
    # Contract (locked by test_phase34_scanner_languages.py and
    # test_phase37_all_languages.py): leading access / storage / linkage
    # modifiers never block a keyword; container keywords
    # (namespace/interface/trait/struct/enum/module/type/object/
    # mixin/extension/record, plus sub/subroutine/defp/proc/program/
    # procedure/implementation/unit/mod/crate, `local`) yield coarse
    # `function`-typed nodes like everything else here; matching is
    # case-insensitive, `@`-prefixed declarations and Lisp/R/MATLAB/
    # Tcl-namespace forms included. Deliberate boundaries (NOT bugs):
    # return-type-prefixed definitions, Go receiver methods, K&R shell,
    # Erlang clauses, Elixir defmodule, Haskell `data`, properties, and
    # comment lines never match — crossing that line needs per-language
    # parsing, not a broader regex.
    seen: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = _GENERIC_MATLAB_RE.match(line)
        q = m.group(1) if m else None
        if q is None:
            m = _GENERIC_TCLNS_RE.match(line)
            q = m.group(1) if m else None
        if q is None:
            m = _GENERIC_DECL_RE.match(line)
            q = m.group(2) if m else None
        if q is None:
            m = _GENERIC_LISP_RE.match(line)
            q = m.group(2) if m else None
        if q is None:
            m = _GENERIC_R_RE.match(line)
            q = m.group(1) if m else None
        if not q or q in _GENERIC_RESERVED_NAMES:
            continue
        occ = seen.get(q, 0) + 1
        seen[q] = occ
        sig = f"@{rel_path}" if occ == 1 else f"@{rel_path}#overload{occ}"
        nid = canonical_symbol_id(repo_id, "text", "symbol", q, sig)
        # The signature is identity input: store it like python/jsts do,
        # or validate_graph's duplicate check (keyed on the same tuple)
        # flags cross-file same-name nodes it cannot tell apart.
        res.nodes.append(Node(nid, "function", q, q, project_id, repo_id, rel_path,
                               Location(i, 0, i, len(line)), "text", "", {"signature": sig}, [ev.id],
                               schema_version=2))
        res.edges.append(Edge(canonical_edge_id(fid, "defines", nid, rel_path),
                              fid, nid, "defines", "structural", "deterministic", 1.0,
                              [ev.id], "generic-text", GENERIC_VERSION,
                              file_path=rel_path, start_line=i))
    return res


EXTRACTOR_VERSIONS = {"python-ast": PY_VERSION, "markdown": MD_VERSION, "json": "1",
                      "yaml": YAML_VERSION, "toml": "1", "dockerfile": DOCKER_VERSION, "sql": SQL_VERSION, "terraform": TF_VERSION,
                      "generic-text": GENERIC_VERSION, "file-only": "1",
                      "jsts": JSTS_VERSION, "ts-go": UNIV_TS_VERSION,
                      "ts-rust": UNIV_TS_VERSION, "ts-java": UNIV_TS_VERSION,
                      "ts-c_sharp": UNIV_TS_VERSION,
                      "ts-php": UNIV_TS_VERSION,
                      "ts-cpp": UNIV_TS_VERSION, "ts-c": UNIV_TS_VERSION,
                      "ts-kotlin": UNIV_TS_VERSION,
                      "ts-lua": UNIV_TS_VERSION, "ts-ruby": UNIV_TS_VERSION,
                      "ts-swift": UNIV_TS_VERSION,
                      "ts-dart": UNIV_TS_VERSION,
                      "ts-gdscript": UNIV_TS_VERSION}


# Deep extractor names served by the unified tree-sitter engine (Phase 38+).
# Service-layer scoping treats these like jsts (repo-scoped re-resolution
# with marker gating), never like python (which has stricter file-type
# rules) — see service._univ_touched_repos.
UNIV_TS_EXTRACTORS = frozenset({"ts-go", "ts-rust", "ts-java",
                                 "ts-c_sharp", "ts-php", "ts-cpp",
                                 "ts-c", "ts-kotlin", "ts-lua",
                                 "ts-swift", "ts-dart", "ts-gdscript", "ts-ruby"})
