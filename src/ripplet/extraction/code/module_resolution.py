"""JavaScript/TypeScript module resolution (Phase 3).

Pure path/config logic, no parsing, no graph I/O — fully unit-testable.
Never executes configuration or application code: ``tsconfig.json`` is read as
JSON-with-comments, ``package.json`` as strict JSON.

Supported: relative imports (explicit / extensionless / extension-substituted /
``index`` conventions), ``tsconfig`` ``extends`` chains (bounded), ``baseUrl``,
``paths`` aliases (longest-prefix, single ``*``), self-reference via package
``name``, ``main``/``.``-export directory mapping, builtin vs external-package
distinction, and recorded (not silent) gaps.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
TS_EXTS = (".ts", ".tsx", ".mts", ".cts")
ALL_CODE_EXTS = JS_EXTS + TS_EXTS
# TS importing a sibling compiled as .js (allowJs / declaration-style imports)
TS_TO_TS = {".js": ".ts", ".jsx": ".tsx", ".mjs": ".mts", ".cjs": ".cts"}

BUILTINS = {
    "fs", "path", "os", "util", "events", "stream", "http", "https", "url", "querystring",
    "crypto", "child_process", "cluster", "dgram", "dns", "assert", "buffer", "zlib",
    "readline", "repl", "tty", "v8", "vm", "worker_threads", "perf_hooks", "async_hooks",
    "fs/promises", "path/posix", "path/win32", "stream/promises", "stream/consumers",
    "stream/web", "util/types", "dns/promises", "timers", "timers/promises", "process",
    "sys", "module", "console",
}

TSCONFIG_NAME = "tsconfig.json"


def _strip_json_comments(text: str) -> str:
    out = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    # trailing commas before } or ]
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


@dataclass
class TsConfig:
    base_url: str = ""          # repo-relative, "" = none
    paths: dict = field(default_factory=dict)
    extends_chain: list = field(default_factory=list)
    module_resolution: str = ""
    unsupported: list = field(default_factory=list)


def load_tsconfig(repo_root: str) -> TsConfig:
    """Read <root>/tsconfig.json + `extends` chain (max depth 5, cycle-guarded)."""
    cfg = TsConfig()
    seen: set[str] = set()
    current = str(Path(repo_root) / TSCONFIG_NAME)
    depth = 0
    merged_paths: dict = {}
    merged_base = ""
    module_res = ""
    while current and depth < 6:
        depth += 1
        key = str(Path(current).resolve())
        if key in seen:
            cfg.unsupported.append(f"extends cycle at {current}; chain truncated")
            break
        seen.add(key)
        try:
            raw = Path(current).read_text(encoding="utf-8", errors="ignore")
            data = json.loads(_strip_json_comments(raw))
        except FileNotFoundError:
            if depth == 1:
                return cfg  # no tsconfig: not an error
            cfg.unsupported.append(f"extends target missing: {current}")
            break
        except Exception as e:
            cfg.unsupported.append(f"could not parse {current}: {e}")
            break
        cfg.extends_chain.append(str(Path(current).relative_to(repo_root))
                                 if str(current).startswith(str(repo_root)) else current)
        opts = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        if isinstance(opts, dict):
            if not merged_base and isinstance(opts.get("baseUrl"), str):
                base = opts["baseUrl"]
                parent = str(Path(current).parent)
                try:
                    merged_base = str(Path(parent, base).resolve().relative_to(repo_root))
                except ValueError:
                    merged_base = base
            if not merged_paths and isinstance(opts.get("paths"), dict):
                for k, v in opts["paths"].items():
                    if isinstance(v, list) and all(isinstance(x, str) for x in v):
                        merged_paths.setdefault(k, v)
                    else:
                        cfg.unsupported.append(f"paths['{k}'] has non-string targets; ignored")
            if not module_res and isinstance(opts.get("moduleResolution"), str):
                module_res = opts["moduleResolution"]
            for known_unsupported in ("references", "rootDirs", "types", "typeRoots"):
                if known_unsupported in opts and known_unsupported not in \
                        [u.split(":")[0] for u in cfg.unsupported]:
                    cfg.unsupported.append(
                        f"{known_unsupported}: present but not used for resolution")
            if isinstance(data, dict) and "references" in data:
                cfg.unsupported.append("references (project references): not resolved")
        ext = data.get("extends") if isinstance(data, dict) else None
        if isinstance(ext, str):
            current = str((Path(current).parent / ext).resolve())
            if not current.endswith(".json"):
                current += ".json"
        else:
            current = ""
    if depth >= 6:
        cfg.unsupported.append("extends chain deeper than 5; truncated")
    cfg.base_url = merged_base.replace("\\", "/")
    cfg.paths = merged_paths
    cfg.module_resolution = module_res
    return cfg


@dataclass
class PackageInfo:
    name: str = ""
    pkg_type: str = ""          # module | commonjs | ""
    dependencies: set = field(default_factory=set)
    main: str = ""
    exports_dot: str = ""
    unsupported: list = field(default_factory=list)


def load_package(repo_root: str, from_dir: str = "") -> PackageInfo:
    """Nearest package.json at or above from_dir (bounded walk to repo root)."""
    info = PackageInfo()
    cur = Path(repo_root) / from_dir if from_dir else Path(repo_root)
    root = Path(repo_root).resolve()
    for _ in range(12):
        cand = cur / "package.json"
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8", errors="ignore"))
            except Exception as e:
                info.unsupported.append(f"could not parse {cand}: {e}")
                return info
            if isinstance(data, dict):
                info.name = data.get("name", "") or ""
                info.pkg_type = data.get("type", "") or ""
                for f in ("dependencies", "devDependencies", "peerDependencies",
                          "optionalDependencies"):
                    d = data.get(f, {})
                    if isinstance(d, dict):
                        info.dependencies |= set(d.keys())
                main = data.get("main", "")
                if isinstance(main, str):
                    info.main = main
                exp = data.get("exports")
                if isinstance(exp, dict):
                    dot = exp.get(".")
                    if isinstance(dot, str):
                        info.exports_dot = dot
                    elif dot is not None:
                        info.unsupported.append("exports['.'] conditional map: using main only")
                elif isinstance(exp, str):
                    info.exports_dot = exp
            return info
        if cur.resolve() == root or cur.parent == cur:
            break
        cur = cur.parent
    return info


@dataclass
class ResolveResult:
    target: str | None = None   # repo-relative path when internal
    kind: str = "unresolved"    # internal|external|builtin|unresolved
    via: str = ""               # relative|paths|baseUrl|index|extension|main|self|package
    note: str = ""


def _try_file(repo_root: str, candidate: str, file_set: set[str]) -> str | None:
    c = candidate.replace("\\", "/").lstrip("./")
    # normalize ./x and bare
    if candidate.startswith("./") or candidate.startswith("../"):
        pass
    if c in file_set:
        return c
    return None


def _with_extensions(repo_root: str, base: str, file_set: set[str]) -> tuple[str | None, str]:
    """Try base as-is, with code extensions, TS substitutions, and index files."""
    direct = _try_file(repo_root, base, file_set)
    if direct:
        suf = Path(direct).suffix.lower()
        if suf in ALL_CODE_EXTS or suf == ".json":
            return direct, "relative"
        return None, ""
    for ext in ALL_CODE_EXTS:
        hit = _try_file(repo_root, base + ext, file_set)
        if hit:
            return hit, "extension"
    # .js -> .ts substitution (TS sources imported by compiled name)
    stem, dot, suffix = base.rpartition(".")
    if dot and ("." + suffix.lower()) in TS_TO_TS:
        hit = _try_file(repo_root, stem + TS_TO_TS["." + suffix.lower()], file_set)
        if hit:
            return hit, "extension"
    for ext in ALL_CODE_EXTS:
        hit = _try_file(repo_root, base.rstrip("/") + "/index" + ext, file_set)
        if hit:
            return hit, "index"
    return None, ""


_WS_CACHE: dict = {}


def _workspace_packages(repo_root: str, file_set: set) -> dict:
    """name -> (package dir, exports) for every package.json in the repo."""
    import json as _json
    key = (repo_root, len(file_set))
    if key in _WS_CACHE:
        return _WS_CACHE[key]
    out = {}
    for f in file_set:
        if not (f == "package.json" or f.endswith("/package.json")) or "node_modules/" in f:
            continue
        try:
            data = _json.loads((Path(repo_root) / f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name and name not in out:
            out[name] = (f.rpartition("/")[0], data.get("exports"), data)
    _WS_CACHE.clear()
    _WS_CACHE[key] = out
    return out


def _export_targets(entry) -> list:
    """Every path a conditional exports entry can point to, in order."""
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, list):
        return [t for x in entry for t in _export_targets(x)]
    if isinstance(entry, dict):
        return [t for v in entry.values() for t in _export_targets(v)]
    return []


def _workspace_package_target(spec: str, repo_root: str, file_set: set) -> str | None:
    if spec.startswith((".", "/")):
        return None
    pkgs = _workspace_packages(repo_root, file_set)
    if not pkgs:
        return None
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    hit = pkgs.get(name)
    if hit is None:
        return None
    pdir, exports, data = hit
    sub = spec[len(name):].lstrip("/")
    join = (lambda p: (pdir + "/" + p.lstrip("./")) if pdir else p.lstrip("./"))
    cands = []
    if isinstance(exports, dict) and any(k.startswith(".") for k in exports):
        key = "./" + sub if sub else "."
        entry = exports.get(key)
        if entry is None:
            for k, v in exports.items():           # "./*" patterns
                if k.endswith("*") and key.startswith(k[:-1]):
                    entry = [t.replace("*", key[len(k) - 1:]) for t in _export_targets(v)]
                    break
        cands += _export_targets(entry)
    elif not sub and exports is not None:
        cands += _export_targets(exports)
    if not sub:
        for k in ("source", "module", "main", "types"):
            if isinstance(data.get(k), str):
                cands.append(data[k])
    # the published files are build output; the sources usually sit in src/
    base = sub or "index"
    cands += [f"src/{base}", base, f"src/{base}/index", f"{base}/index", "src/index"]
    for c in cands:
        found, _via = _with_extensions(repo_root, join(c), file_set)
        if found:
            return found
        # a built path (`./dist/v4/index.js`, `./v4/index.d.ts`) -> its source
        stem = re.sub(r"\.d\.(c|m)?ts$|\.(c|m)?js$", "", c.lstrip("./"))
        stem = re.sub(r"^(dist|build|lib|out|esm|cjs)/", "", stem)
        for alt in (f"src/{stem}", stem):
            found, _via = _with_extensions(repo_root, join(alt), file_set)
            if found:
                return found
    return None


def resolve_specifier(spec: str, from_rel: str, repo_root: str, file_set: set[str],
                      tsconfig: TsConfig | None = None,
                      package: PackageInfo | None = None) -> ResolveResult:
    """Resolve one import specifier. No filesystem access beyond file_set."""
    spec = (spec or "").strip()
    if not spec:
        return ResolveResult(kind="unresolved", note="empty specifier")
    bare = spec[3:] if spec.startswith("node:") else spec
    if spec.startswith("node:") or bare.split("/")[0] in BUILTINS or bare in BUILTINS:
        return ResolveResult(kind="builtin", via="builtin",
                             note=f"runtime builtin: {spec}")
    if spec in (".", ".."):
        spec += "/"          # `from '.'`: this directory's index file
    if spec.startswith("./") or spec.startswith("../"):
        import posixpath
        # Lexical collapse only (no filesystem access): `a/b/../c` never
        # matches a file_set entry verbatim, so every parent-relative
        # import silently stayed unresolved (Phase 35: C-ts-import-03).
        # normpath is identity for already-flat paths (same-dir behavior
        # unchanged, locked by tests).
        base = posixpath.normpath(
            str(Path(Path(from_rel).parent / spec)).replace("\\", "/"))
        # Climbing above the importing file's directory tree escapes the
        # repository: count leading `..` against the parent depth.
        parent = str(Path(from_rel).parent).replace("\\", "/")
        depth = 0 if parent in (".", "") else len(parent.split("/"))
        ups = 0
        for part in spec.replace("\\", "/").split("/"):
            if part == "..":
                ups += 1
            elif part not in (".", ""):
                break
        if base in (".", "..") or base.startswith("../") or ups > depth:
            return ResolveResult(kind="unresolved", via="relative",
                                 note=f"relative target escapes repository: "
                                      f"{spec} from {from_rel}")
        hit, via = _with_extensions(repo_root, base, file_set)
        if hit:
            return ResolveResult(target=hit, kind="internal", via=via)
        return ResolveResult(kind="unresolved", via="relative",
                             note=f"relative target missing: {spec} from {from_rel}")
    # bare specifier: tsconfig paths (longest prefix, single *) first
    if tsconfig and tsconfig.paths:
        best: tuple[int, str, str] | None = None
        for pattern, targets in tsconfig.paths.items():
            if pattern == "*":
                key, plen = "", 0
            elif "*" in pattern:
                pre, _, _ = pattern.partition("*")
                if not spec.startswith(pre):
                    continue
                key, plen = pre, len(pre)
            else:
                if not (spec == pattern or spec.startswith(pattern + "/")):
                    continue
                key, plen = pattern, len(pattern)
            star = spec[len(key):] if "*" in pattern or pattern == "*" else \
                spec[len(pattern):].lstrip("/")
            for t in targets:
                cand = t.replace("*", star)
                if tsconfig.base_url:
                    cand = tsconfig.base_url.rstrip("/") + "/" + cand
                hit, via = _with_extensions(repo_root, cand, file_set)
                if hit and (best is None or plen > best[0]):
                    best = (plen, hit, via)
        if best:
            return ResolveResult(target=best[1], kind="internal", via="paths")
    if tsconfig and tsconfig.base_url:
        hit, via = _with_extensions(repo_root, tsconfig.base_url.rstrip("/") + "/" + spec,
                                    file_set)
        if hit:
            return ResolveResult(target=hit, kind="internal", via="baseUrl")
    # another package of this repository (npm/pnpm/yarn workspaces, monorepos):
    # `zod/v4` -> packages/zod's exports["./v4"] -> its source file
    hit = _workspace_package_target(spec, repo_root, file_set)
    if hit:
        return ResolveResult(target=hit, kind="internal", via="workspace")
    # self-reference via package name
    if package and package.name and (spec == package.name or spec.startswith(package.name + "/")):
        rest = spec[len(package.name):].lstrip("/")
        hit, via = _with_extensions(repo_root, rest or "index", file_set)
        if hit:
            return ResolveResult(target=hit, kind="internal", via="self")
    # package directory with main/exports-dot
    if package:
        first = spec.split("/")[0].lstrip("@")
        if spec in package.dependencies or first in package.dependencies or \
                (package.name and spec.startswith(package.name)):
            entry = package.exports_dot or package.main
            if entry:
                return ResolveResult(kind="external", via="package",
                                     note=f"installed package {spec} (entry {entry}; tree not scanned)")
            return ResolveResult(kind="external", via="package",
                                 note=f"installed package {spec}; tree not scanned")
    return ResolveResult(kind="unresolved", via="",
                         note=f"no mapping for bare specifier: {spec}")


def config_state(repo_root: str, jsts_version: str, grammar_versions: dict) -> str:
    """Fingerprint of everything (besides file bytes) that JS/TS resolution
    depends on: tsconfig chain, package.json, grammar + extractor versions."""
    import hashlib
    h = hashlib.sha256()
    h.update(f"jsts={jsts_version}".encode())
    for k in sorted(grammar_versions):
        h.update(f"{k}={grammar_versions[k]}".encode())
    for name in (TSCONFIG_NAME, "package.json"):
        p = Path(repo_root) / name
        try:
            h.update(name.encode())
            h.update(p.read_bytes()[:65536])
        except OSError:
            h.update(b"<missing>")
    return "cfgjs:" + h.hexdigest()[:16]
