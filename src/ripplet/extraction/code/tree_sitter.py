"""Tree-sitter parser adapter with explicit capability states (Phase 3).

All tree-sitter imports are lazy so the base package works without the optional
``treesitter`` extra. Nothing here raises on import; use :func:`capabilities`
to discover what is actually usable in this environment.

Parsers are cached per language and reused sequentially. Do not share one Parser
across threads/workers; construct via :func:`get_parser` per worker instead.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Centralized extension -> language mapping. The tree-sitter-javascript grammar
# covers JSX; TypeScript ships two grammars (typescript, tsx).
EXTENSION_LANGUAGE: dict[str, str] = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}
LANGUAGE_IDS = ("javascript", "typescript", "tsx")

_PARSERS: dict[str, object] = {}
_INIT_ERRORS: dict[str, str] = {}


def _load_grammar(language_id: str):
    """Import the grammar package and return a Language, or raise with reason."""
    if language_id == "javascript":
        import tree_sitter_javascript as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "typescript":
        import tree_sitter_typescript as g
        from tree_sitter import Language
        return Language(g.language_typescript())
    if language_id == "tsx":
        import tree_sitter_typescript as g
        from tree_sitter import Language
        return Language(g.language_tsx())
    raise ValueError(f"unsupported language: {language_id}")


def get_parser(language_id: str):
    """Return a cached Parser for language_id, initializing grammars on demand."""
    if language_id in _PARSERS:
        return _PARSERS[language_id]
    from tree_sitter import Parser
    lang = _load_grammar(language_id)
    parser = Parser(lang)
    _PARSERS[language_id] = parser
    return parser


def language_for_extension(suffix: str) -> str | None:
    return EXTENSION_LANGUAGE.get((suffix or "").lower())


def supported_extensions() -> list[str]:
    return sorted(EXTENSION_LANGUAGE)


@dataclass
class ParseResult:
    ok: bool = False
    status: str = "unavailable"  # ok|partial|fallback|unavailable|error
    tree: object = None
    error_count: int = 0
    missing_count: int = 0
    error_nodes: list[str] = field(default_factory=list)
    language_id: str = ""
    reason: str = ""


def _count_issues(root) -> tuple[int, int, list[str]]:
    errors = 0
    missing = 0
    samples: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t == "ERROR":
            errors += 1
            if len(samples) < 5:
                try:
                    samples.append(n.text.decode("utf-8", "ignore")[:60])
                except Exception:
                    samples.append("<undecodable>")
        elif n.is_missing:
            missing += 1
        stack.extend(n.children)
    return errors, missing, samples


def parse(language_id: str, data: bytes, max_bytes: int = 2_000_000) -> ParseResult:
    """Parse source bytes with the correct grammar. Never raises for syntax issues.

    Returns partial (not failure) when the tree contains ERROR/MISSING nodes:
    callers extract reliable surrounding declarations and mark coverage partial.
    Resource/initialization failures return status="error" with a reason.
    """
    if len(data) > max_bytes:
        return ParseResult(ok=False, status="error", language_id=language_id,
                           reason=f"source exceeds parser limit ({len(data)} bytes)")
    try:
        parser = get_parser(language_id)
    except ImportError as e:
        return ParseResult(ok=False, status="unavailable", language_id=language_id,
                           reason=f"parser package missing: {e}. Install with: pip install 'ripplet[treesitter]'")
    except Exception as e:
        _INIT_ERRORS[language_id] = str(e)
        return ParseResult(ok=False, status="error", language_id=language_id,
                           reason=f"parser initialization failed: {e}")
    try:
        tree = parser.parse(data)
    except Exception as e:
        return ParseResult(ok=False, status="error", language_id=language_id,
                           reason=f"parse crashed: {e}")
    root = tree.root_node
    has_error = root.has_error if isinstance(getattr(root, "has_error", False), bool) \
        else bool(root.has_error())
    errors, missing, samples = _count_issues(root)
    if has_error or errors or missing:
        return ParseResult(ok=True, status="partial", tree=tree, error_count=errors,
                           missing_count=missing, error_nodes=samples,
                           language_id=language_id,
                           reason=f"{errors} error nodes, {missing} missing nodes")
    return ParseResult(ok=True, status="ok", tree=tree, language_id=language_id)


def capabilities() -> dict:
    """Capability matrix: package, per-language grammar/init state, extensions."""
    try:
        import tree_sitter  # noqa: F401
        package = True
        pkg_version = getattr(tree_sitter, "__version__", "unknown")
    except ImportError as e:
        return {"package_installed": False, "package_version": None,
                "reason": f"{e}; pip install 'ripplet[treesitter]'",
                "languages": {}, "extensions": {}}
    langs: dict[str, dict] = {}
    for lid in LANGUAGE_IDS:
        try:
            get_parser(lid)
            langs[lid] = {"grammar_available": True, "initialized": True}
        except ImportError as e:
            langs[lid] = {"grammar_available": False, "initialized": False,
                          "reason": str(e)[:200]}
        except Exception as e:
            langs[lid] = {"grammar_available": True, "initialized": False,
                          "reason": str(e)[:200]}
    exts = {ext: {"language": lid,
                  "deep_parse": bool(langs.get(lid, {}).get("initialized")),
                  "reason": langs.get(lid, {}).get("reason", "")}
            for ext, lid in EXTENSION_LANGUAGE.items()}
    return {"package_installed": package, "package_version": pkg_version,
            "languages": langs, "extensions": exts}


def available() -> bool:
    """Legacy simple flag: any language parser initializes."""
    try:
        caps = capabilities()
        return any(v.get("initialized") for v in caps.get("languages", {}).values())
    except Exception:
        return False


AVAILABLE = None  # kept for import compatibility; call available()/capabilities()


# -------------------------------------------------- universal grammars (Phase 38)
# Additional tree-sitter languages beyond the JS family, served through the
# unified extractor (ts_universal). Kept in a SEPARATE registry so js-family
# scoping (language_for_extension), cache keys, and capability shapes never
# change meaning: `language_for_extension(".go")` stays None by design.
UNIV_EXTENSION_LANGUAGE: dict[str, str] = {
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "c_sharp",
    ".php": "php",
    # Phase 41 Wave 3: C++ family (unambiguous suffixes only; `.h` stays
    # fallback — shared with C/Objective-C headers, and the C++ grammar
    # yields ERROR-subtree garbage on ObjC `@interface`), C, Kotlin, Lua.
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".ipp": "cpp",
    ".c": "c",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".lua": "lua",
    # Phase 42 Wave 4: risk grammars (spike-measured call-carrying).
    ".swift": "swift",
    ".dart": "dart",
    ".gd": "gdscript",
    ".rb": "ruby",
    ".rake": "ruby",
}
UNIV_LANGUAGE_IDS = ("go", "rust", "java", "c_sharp", "php",
                     "cpp", "c", "kotlin", "lua",
                     "swift", "dart", "gdscript", "ruby")

_UNIV_PARSERS: dict[str, object] = {}
_UNIV_INIT_ERRORS: dict[str, str] = {}


def _load_univ_grammar(language_id: str):
    """Import a universal grammar package and return a Language, or raise."""
    if language_id == "go":
        import tree_sitter_go as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "rust":
        import tree_sitter_rust as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "java":
        import tree_sitter_java as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "c_sharp":
        import tree_sitter_c_sharp as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "php":
        import tree_sitter_php as g
        from tree_sitter import Language
        # Multi-grammar package (php, php_only, html): the full one.
        return Language(g.language_php())
    if language_id == "cpp":
        import tree_sitter_cpp as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "c":
        import tree_sitter_c as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "kotlin":
        import tree_sitter_kotlin as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "ruby":
        import tree_sitter_ruby as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "lua":
        import tree_sitter_lua as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "swift":
        import tree_sitter_swift as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "dart":
        import tree_sitter_dart as g
        from tree_sitter import Language
        return Language(g.language())
    if language_id == "gdscript":
        import tree_sitter_gdscript as g
        from tree_sitter import Language
        return Language(g.language())
    raise ValueError(f"unsupported universal language: {language_id}")


def get_univ_parser(language_id: str):
    """Cached Parser for a universal language_id (per-worker rule as above)."""
    if language_id in _UNIV_PARSERS:
        return _UNIV_PARSERS[language_id]
    from tree_sitter import Parser
    lang = _load_univ_grammar(language_id)
    parser = Parser(lang)
    _UNIV_PARSERS[language_id] = parser
    return parser


def univ_language_for_extension(suffix: str) -> str | None:
    return UNIV_EXTENSION_LANGUAGE.get((suffix or "").lower())


def univ_supported_extensions() -> list[str]:
    return sorted(UNIV_EXTENSION_LANGUAGE)


def univ_capabilities() -> dict:
    """Capability matrix for universal grammars (additive; legacy shape kept).

    Reads like capabilities() but never shares its language/extension maps.
    """
    try:
        import tree_sitter  # noqa: F401
        package = True
        pkg_version = getattr(tree_sitter, "__version__", "unknown")
    except ImportError as e:
        return {"package_installed": False, "package_version": None,
                "reason": f"{e}; pip install 'ripplet[treesitter-multilang]'",
                "languages": {}, "extensions": {}}
    langs: dict[str, dict] = {}
    for lid in UNIV_LANGUAGE_IDS:
        try:
            get_univ_parser(lid)
            langs[lid] = {"grammar_available": True, "initialized": True}
        except ImportError as e:
            langs[lid] = {"grammar_available": False, "initialized": False,
                          "reason": str(e)[:200]}
        except Exception as e:
            langs[lid] = {"grammar_available": True, "initialized": False,
                          "reason": str(e)[:200]}
    exts = {ext: {"language": lid,
                  "deep_parse": bool(langs.get(lid, {}).get("initialized")),
                  "reason": langs.get(lid, {}).get("reason", "")}
            for ext, lid in UNIV_EXTENSION_LANGUAGE.items()}
    return {"package_installed": package, "package_version": pkg_version,
            "languages": langs, "extensions": exts}
