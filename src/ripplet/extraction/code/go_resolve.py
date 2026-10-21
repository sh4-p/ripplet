"""Go module-scoped resolution (Phase 38 Wave 0).

Binds the placeholders ``ts_universal`` emits: import specifiers to repo
directories (relative ./.. plus go.mod module-prefix mapping; stdlib and
module dependencies stay external with reasons), same-directory and
imported-package calls to real symbols. Calls on the
method's own receiver (``func (s *Server) ...`` then ``s.Start()``) bind via
the declared receiver type (v2); any other variable receiver stays
`calls_unresolved` — that needs type flow, not name matching.

Conventions mirror ``js_resolve``: repo-scoped inputs (stored manifests
minus ghosts/fresh plus new results), manifest-owned outputs, method
``go-resolve`` v1, deterministic site fingerprints, no guessing
(single targets only; test-package collisions stay ambiguous).
"""
from __future__ import annotations
import posixpath

METHOD = "go-resolve"
VERSION = "5"  # v5: build-tag variants skipped as targets; v4: conversions, cross-package receiver types, bare calls never methods; v3: locals typed by a same-package call result (v2: receiver-typed selector calls)

# Single-segment bare imports that are certainly not repo packages.
# Anything else bare and unmatched is external (stdlib or module dep)
# with a stated reason — never invented as internal.
_STDLIB = frozenset("""
archive/tar archive/zip bufio bytes cmp compress/flate compress/gzip
compress/lzw compress/zlib container/heap container/list container/ring
context crypto crypto/aes crypto/cipher crypto/des crypto/dsa
crypto/ecdsa crypto/ed25519 crypto/elliptic crypto/hmac crypto/md5
crypto/rand crypto/rc4 crypto/rsa crypto/sha1 crypto/sha256 crypto/sha512
crypto/subtle crypto/tls crypto/x509 database/sql debug/buildinfo
debug/dwarf debug/elf debug/gosym debug/macho debug/pe debug/plan9obj
embed encoding encoding/ascii85 encoding/asn1 encoding/base32
encoding/base64 encoding/binary encoding/csv encoding/gob encoding/hex
encoding/json encoding/pem encoding/xml errors expvar flag fmt hash
hash/adler32 hash/crc32 hash/crc64 hash/fnv hash/maphash html
html/template image image/color image/draw image/gif image/jpeg image/png
io io/fs io/ioutil iter log log/slog maps math math/big math/bits
math/cmplx math/rand math/rand/v2 mime mime/multipart mime/quotedprintable
net net/http net/http/cgi net/http/cookiejar net/http/fcgi
net/http/httptest net/http/httptrace net/http/httputil net/http/pprof
net/mail net/netip net/rpc net/rpc/jsonrpc net/smtp net/textproto net/url
os os/exec os/signal os/user path path/filepath plugin reflect regexp
regexp/syntax runtime runtime/cgo runtime/debug runtime/metrics
runtime/pprof runtime/race runtime/trace slices sort strconv strings
structs sync sync/atomic syscall testing testing/fstest testing/iotest
testing/quick testing/slogtest text/scanner text/template
text/template/parse time time/tzdata unicode unicode/utf16 unicode/utf8
unsafe errors io fs log os sort time math strings bytes
""".split())


def _go_module(repo_root: str) -> str:
    """Module path from <root>/go.mod (`module <path>`), or "".

    Plain text read (first `module` directive); never executed. Only the
    main-module case is mapped (nested modules stay external, documented).
    """
    try:
        from pathlib import Path
        for line in Path(repo_root, "go.mod").read_text(
                encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("module "):
                return s[len("module "):].strip().strip('"')
            if s and not s.startswith("//"):
                continue
    except OSError:
        pass
    return ""


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) \
        -> tuple[list, list, dict]:
    """Returns ([], [(owner_key, Edge)], diagnostics) for one repo. See
    js_resolve.resolve_repo for the exclude/fresh contract."""
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("go_collect")
    from ...core.models import Edge as EdgeCls
    from ...core.identifiers import file_node_id, canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf
    diag: dict = {"go_module": "", "resolved_imports": 0,
                  "external_imports": 0, "unresolved_imports": 0,
                  "depends_on_derived": 0, "resolved_calls": 0,
                  "ambiguous_calls": 0}
    go_module = _go_module(repo_root)
    diag["go_module"] = go_module

    def owner_rel(key: str) -> str:
        return key.split(":", 1)[-1]

    def mod_id(rel: str) -> str:
        return file_node_id(repo_id, rel + "#module")

    # repo dirs holding .go files (resolution targets)
    go_dirs: set[str] = set()
    for f in file_set:
        if f.lower().endswith(".go"):
            d = f.rpartition("/")[0]
            go_dirs.add(d)

    def _attrs(d: dict) -> dict:
        import json as _json
        a = d.get("attributes") or {}
        if isinstance(a, str):
            try:
                a = _json.loads(a)
            except Exception:
                return {}
        return a if isinstance(a, dict) else {}

    # ---- collect: stored manifests + new results
    imports: list[tuple[str, dict]] = []
    unres_calls: list[tuple[str, dict]] = []
    repo_keys = sorted(k for k in store.list_file_keys()
                       if k.startswith(repo_id + ":") and k not in exclude_keys
                       and k.split(":", 1)[-1] not in fresh_rels)
    for key in repo_keys:
        for eid in sorted(store.get_manifest_edges(key)):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?",
                                   (eid,)).fetchone()
            if row is None:
                continue
            d = dict(row)
            if d["relation"] == "imports" and \
                    (d["method"] or "").startswith("ts-go"):
                imports.append((key, d))
            elif d["relation"] == "calls_unresolved" and \
                    (d["method"] or "").startswith("ts-go"):
                unres_calls.append((key, d))
    for key, res in new_results:
        for e in res.edges:
            d = {"id": e.id, "source": e.source, "target": e.target,
                 "relation": e.relation,
                 "method": e.extraction_method,
                 "provenance": list(e.provenance_ids or []),
                 "attributes": dict(e.attributes or {}),
                 "file_path": e.file_path, "start_line": e.start_line}
            if e.relation == "imports":
                imports.append((key, d))
            elif e.relation == "calls_unresolved":
                unres_calls.append((key, d))

    # ---- per-directory symbol tables (stored + new nodes)
    pkg_syms: dict[str, dict[str, list[str]]] = {}
    # dir rel -> label -> [node ids]
    pkg_types: dict[str, dict[str, str]] = {}
    # dir rel -> label -> node type (class/interface/type/function/...)
    fn_returns: dict[str, str] = {}  # function node id -> declared first result type

    from ...graph.go_build import default_build as _default_build
    go_off: set = set()                  # build-tag variants a default build skips
    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and getattr(n, "type", "") == "module" and                     not _default_build((n.attributes or {}).get("go_build", "")):
                go_off.add(owner_rel(key))
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if getattr(n, "type", "") == "module" and                     not _default_build((getattr(n, "attributes", None) or {}).get("go_build", "")):
                go_off.add(owner_rel(key))

    def _add_sym(rel: str, node):
        if getattr(node, "type", "") not in ("function", "method", "class",
                                             "interface", "type"):
            return
        if rel in go_off:
            return                       # never a binding target
        d = rel.rpartition("/")[0]
        label = getattr(node, "label", "")
        pkg_syms.setdefault(d, {}).setdefault(label, []).append(node.id)
        if getattr(node, "type", "") == "function":
            ret = (getattr(node, "attributes", None) or {}).get("returns")
            if ret:
                fn_returns[node.id] = ret
        pkg_types.setdefault(d, {}).setdefault(label,
                                               getattr(node, "type", ""))

    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and (n.file_path or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if (getattr(n, "file_path", "") or "") == owner_rel(key):
                _add_sym(owner_rel(key), n)

    def _dir_of(rel: str) -> str:
        return rel.rpartition("/")[0]

    def resolve_spec(from_rel: str, spec: str):
        """(target_dir_rel or None, via, note)."""
        spec = (spec or "").strip()
        if not spec:
            return None, "", "empty specifier"
        if spec.startswith("./") or spec.startswith("../"):
            base = posixpath.normpath(
                _dir_of(from_rel) + "/" + spec if _dir_of(from_rel)
                else spec)
            if base in (".", "..") or base.startswith("../"):
                return None, "", f"escapes repository: {spec}"
            if base in go_dirs:
                return base, "relative", ""
            return None, "", f"relative target missing: {spec}"
        if go_module and (spec == go_module or
                          spec.startswith(go_module + "/")):
            rest = spec[len(go_module):].lstrip("/")
            if rest in go_dirs or rest == "":
                return rest, "gomod", ""
            return None, "", f"module path not in repo: {spec}"
        if "/" not in spec or spec in _STDLIB:
            return None, "external", f"stdlib or module dependency: {spec}"
        return None, "external", f"module dependency: {spec}"

    extra: list[tuple[str, object]] = []

    def add_edge(owner_key: str, src: str, rel: str, tgt: str, src_row: dict,
                 etype: str, cclass: str, score, attrs: dict, site: str = ""):
        from ...graph.builder import carry_occurrences as _carry
        attrs = _carry(attrs, src_row)
        e = EdgeCls(canonical_edge_id(src, rel, tgt, owner_rel(owner_key),
                                      site),
                    src, tgt, rel, etype, cclass, score,
                    list(src_row.get("provenance", [])), METHOD, VERSION,
                    file_path=owner_rel(owner_key),
                    start_line=src_row.get("start_line") or 0,
                    attributes=attrs)
        extra.append((owner_key, e))
        return e

    prof.end("go_collect")
    prof.begin("go_bind")
    # ---- 1. imports -> depends_on (+ package bindings for calls)
    # Go imports whole packages, not symbols: the binding maps an import
    # local name to its target DIRECTORY; member lookup happens at call
    # time against that dir's table (single targets only, no guessing).
    bindings: dict[tuple[str, str], str] = {}
    # (owner_key, local) -> target dir rel
    for key, d in sorted(imports, key=lambda x: x[1]["id"]):
        a = _attrs(d)
        spec = a.get("specifier", "")
        if not spec:
            continue
        tgt_dir, via, note = resolve_spec(owner_rel(key), spec)
        if tgt_dir is None:
            if via == "external":
                diag["external_imports"] += 1
            else:
                diag["unresolved_imports"] += 1
            continue
        diag["resolved_imports"] += 1
        src_mid = mod_id(owner_rel(key))
        # module node id is deterministic from a real file rel; anchor on
        # any .go file in the target dir (never invented).
        tgt_mid = mod_id(_anchor_go_file(file_set, tgt_dir))
        add_edge(key, src_mid, "depends_on", tgt_mid, d,
                 "resolved", "resolved", 0.9,
                 {"specifier": spec, "via": "go-" + via},
                 site=_sf("godep", spec, tgt_dir))
        diag["depends_on_derived"] += 1
        names = a.get("names") or []
        for local in ([n for n in names if isinstance(n, str)] or []):
            bindings.setdefault((key, local), tgt_dir)
    prof.end("go_bind")
    prof.begin("go_calls")
    # ---- 2. calls: same-dir functions, bound package members, same-dir
    # Type.Method. Receiver-variable calls (s.Start) stay unresolved.
    # Method owners come from qualified names (pkg.Type.Method).
    method_owner: dict[str, str] = {}  # method node id -> type label
    for key in repo_keys:
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and getattr(n, "type", "") == "method":
                parts = (getattr(n, "qualified_name", "") or "").split(".")
                if len(parts) >= 3:
                    method_owner[nid] = parts[-2]
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if getattr(n, "type", "") == "method":
                parts = (getattr(n, "qualified_name", "") or "").split(".")
                if len(parts) >= 3:
                    method_owner[getattr(n, "id", "")] = parts[-2]
    for key, d in unres_calls:
        a = _attrs(d)
        base = a.get("base", "")
        member = a.get("member", "")
        here = _dir_of(owner_rel(key))
        tgt = None
        if a.get("callee_kind") == "function":
            # a bare call is a function, never a method (incl. interface method specs)
            cands = [c for c in (pkg_syms.get(here, {}) or {}).get(base, []) if c not in method_owner]
            tgt = cands[0] if len(cands) == 1 else None
            if tgt is None and len(cands) > 1:
                diag["ambiguous_calls"] += 1
        elif a.get("callee_kind") == "selector" and member and                 a.get("receiver_type"):
            # Receiver-variable call with the receiver's declared type
            # (syntax): the method set of that named type in this package.
            # Go forbids duplicate methods per type per package, so >1 hit
            # means a test-package or build-tag split -> stay unresolved.
            rtype = a["receiver_type"]
            tdir_ = here
            if rtype.startswith("()"):
                if (pkg_types.get(here, {}) or {}).get(rtype[2:]):
                    rtype = rtype[2:]          # `w := ResponseWriter(writer)`: a conversion
                else:
                    # `c, err := New(...)`: typed by the unique same-package
                    # function's declared first result.
                    fns = [nid for nid in (pkg_syms.get(here, {}) or {}).get(rtype[2:], [])
                           if nid not in method_owner and nid in fn_returns]
                    rtype = fn_returns[fns[0]] if len(fns) == 1 else ""
            if "." in rtype:
                # `c *gin.Context` from another package: the import's package dir
                q_, _, rtype = rtype.rpartition(".")
                tdir_ = bindings.get((key, q_)) or ""
            meths = [nid for nid in
                     (pkg_syms.get(tdir_, {}) or {}).get(member, [])
                     if method_owner.get(nid) == rtype]
            if len(meths) == 1:
                tgt = meths[0]
            elif len(meths) > 1:
                diag["ambiguous_calls"] += 1
        elif a.get("callee_kind") == "selector" and member:
            tdir = bindings.get((key, base))
            if tdir is not None:
                cands = (pkg_syms.get(tdir, {}) or {}).get(member, [])
                # A package member call resolves only to a non-method
                # symbol (methods need a receiver value, never a bare
                # package qualifier) or stays unresolved.
                plain = [nid for nid in cands if nid not in method_owner]
                tgt = plain[0] if len(plain) == 1 else None
                if tgt is None and cands:
                    diag["ambiguous_calls"] += 1
            elif (pkg_types.get(here, {}) or {}).get(base) in (
                    "class", "interface", "type"):
                # Same-package Type.Method static style.
                meths = [nid for nid in
                         (pkg_syms.get(here, {}) or {}).get(member, [])
                         if method_owner.get(nid) == base]
                if len(meths) == 1:
                    tgt = meths[0]
                elif len(meths) > 1:
                    diag["ambiguous_calls"] += 1
        if tgt:
            diag["resolved_calls"] += 1
            add_edge(key, d["source"], "calls", tgt, d,
                     "resolved", "resolved", 0.9,
                     {"via": "go-resolve"},
                     site=_sf("gocall", base, member or ""))
    prof.end("go_calls")
    prof.begin("go_files")
    _emit_file_deps(extra, diag, store, new_results, mod_id, METHOD,
                    VERSION, "go")
    prof.end("go_files")
    return [], extra, diag


def _emit_file_deps(extra, diag, store, new_results, mod_id, method,
                    version, tag):
    """File-level dependencies from resolved cross-file facts: a call
    that crosses files IS a file dependency, import statement or not.
    Deterministic per file pair."""
    from ...core.identifiers import canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf2
    from ...core.models import Edge as EdgeCls
    seen: set[tuple[str, str, str]] = set()
    nodes: dict[str, object] = {}
    fresh = {getattr(m, "id", ""): m for _k, res in new_results
             for m in getattr(res, "nodes", [])}

    def _get(nid: str):
        if nid not in nodes:  # fresh-node scan per lookup was quadratic
            n = store.get_node(nid)
            nodes[nid] = n if n is not None else fresh.get(nid)
        return nodes[nid]

    for owner_key, e in list(extra):
        if e.relation not in ("calls", "inherits", "implements",
                              "instantiates"):
            continue
        s = _get(e.source)
        t = _get(e.target)
        if s is None or t is None:
            continue
        sf = getattr(s, "file_path", "") or ""
        tf = getattr(t, "file_path", "") or ""
        if not sf or not tf or sf == tf:
            continue
        if (owner_key, sf, tf) in seen:
            continue
        seen.add((owner_key, sf, tf))
        eid = canonical_edge_id(mod_id(sf), "depends_on", mod_id(tf),
                                owner_key.split(":", 1)[-1],
                                _sf2(f"{tag}filedep", sf, tf))
        extra.append((owner_key, EdgeCls(
            eid, mod_id(sf), mod_id(tf), "depends_on", "resolved",
            "resolved", 0.9, list(getattr(e, "provenance_ids", []) or []),
            method, version,
            file_path=owner_key.split(":", 1)[-1], start_line=0,
            attributes={"via": f"{tag}-file-fact"})))
        diag["depends_on_derived"] = diag.get("depends_on_derived", 0) + 1


def _anchor_go_file(file_set: set[str], target_dir: str) -> str:
    """One real .go rel path in target_dir for deterministic module ids."""
    for f in sorted(file_set):
        if f.rpartition("/")[0] == target_dir and f.lower().endswith(".go"):
            return f
    return target_dir + "/_.go"
