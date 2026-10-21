from __future__ import annotations
import hashlib, re

def _slug(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_.\-/]+", "_", s).strip("_")
    return s[:180] or "unnamed"

def stable_id(*parts: str) -> str:
    h = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{_slug(parts[0]) if parts else 'id'}:{h}" if parts else h

def file_node_id(repo: str, relpath: str) -> str:
    return stable_id("file", repo, relpath.replace("\\", "/"))

def normalize_qualified(q: str) -> str:
    q = (q or "").replace("\\", "/").strip()
    while ".." in q:
        q = q.replace("..", ".")
    return q.strip(".")[:300]

def canonical_symbol_id(repo: str, language: str, kind: str, qualified: str, signature: str = "") -> str:
    """Stable identity for named symbols. Line/column are NOT inputs.

    canonical = repo + language + kind + normalized qualified name + signature/
    overload discriminator. SHA-256 over the canonical form, truncated to 16 hex.
    """
    q = normalize_qualified(qualified)
    canon = "\x00".join([repo or "", language or "", kind or "", q, signature or ""])
    h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    return f"{_slug(kind or 'symbol')}:{h}"

def fallback_anon_id(repo: str, language: str, kind: str, parent_qualified: str, index: int, body_hash: str = "") -> str:
    """Documented fallback for entities without stable names (lambdas, anonymous).

    Uses parent scope + occurrence index + body content hash — never a raw source
    position alone. Stable under insertions above the parent.
    """
    q = normalize_qualified(parent_qualified) + f"#<{kind}@{index}>"
    return canonical_symbol_id(repo, language, kind, q, body_hash[:16])

def symbol_node_id(repo: str, qualified: str, kind: str, relpath: str, line: int = 0,
                    language: str = "python", signature: str = "") -> str:
    # Deprecated line-based wrapper kept for compatibility. `line`/`relpath` are
    # accepted but ignored (except as qualified fallback) so old call sites keep
    # working while producing the new stable identity.
    return canonical_symbol_id(repo, language, kind, qualified or relpath, signature)

def canonical_edge_id(source: str, relation: str, target: str, owner_file: str = "",
                        site: str = "") -> str:
    """Stable edge identity: endpoints + relation + owning file (+ optional site).

    No line numbers. ``site`` distinguishes multiple same-triple occurrences in
    one file (e.g. two call sites of one function) with a *stable* fingerprint
    of the occurrence (callee shape, specifier, arity) — never a line number,
    so the identity survives insertions and is order-independent across runs.
    """
    base = hashlib.sha256(
        f"{source}\x00{relation}\x00{target}\x00{owner_file}\x00{site}".encode()).hexdigest()[:12]
    return f"edge:{base}"


def site_fingerprint(*parts: str) -> str:
    """Short stable fingerprint for one occurrence site (no positions)."""
    return hashlib.sha256("\x00".join(p or "" for p in parts).encode()).hexdigest()[:10]


def edge_id(source: str, relation: str, target: str, line: int = 0, owner_file: str = "") -> str:
    # Deprecated wrapper: `line` ignored. Pass owner_file for per-file dedup.
    return canonical_edge_id(source, relation, target, owner_file)

def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()

def claim_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
