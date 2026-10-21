"""Adapter contract (Phase 8 Stage 2).

Every adapter parses WITHOUT executing input, validates structure, enforces
byte/record caps, normalizes paths safely, returns warnings, supports partial
import, rejects ambiguous data explicitly, and stamps its name/version.
Detection is structural (content sniffing), never by filename alone.
"""
from __future__ import annotations
import hashlib
import json

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 200_000


class ReportRejected(Exception):
    pass


def report_id(report_bytes: bytes, repo_id: str, label: str) -> tuple[str, str]:
    digest = "sha256:" + hashlib.sha256(report_bytes).hexdigest()
    short = hashlib.sha256(f"{digest}\x00{repo_id}\x00{label}".encode()).hexdigest()[:12]
    return f"cov:{short}", digest


def strict_json(text: str):
    """JSON without NaN/Infinity (non-finite counters are rejected, not coerced)."""
    def _bad(v):
        raise ReportRejected("non-finite JSON constant (NaN/Infinity not permitted)")
    return json.loads(text, parse_constant=_bad)


def check_count(v, what: str, allow_none: bool = True) -> int | None:
    """Validate a numeric counter: bool/int only, finite, non-negative."""
    if v is None:
        if allow_none:
            return None
        raise ReportRejected(f"{what}: missing counter")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ReportRejected(f"{what}: invalid counter {v!r}")
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ReportRejected(f"{what}: non-finite counter")
        if not v.is_integer():
            raise ReportRejected(f"{what}: fractional counter {v}")
        v = int(v)
    if v < 0:
        raise ReportRejected(f"{what}: negative counter {v}")
    if v > 10 ** 12:
        raise ReportRejected(f"{what}: absurd counter {v}")
    return v


def check_line(v, what: str) -> int:
    n = check_count(v, what, allow_none=False)
    if n is not None and (n < 1 or n > 10 ** 7):
        raise ReportRejected(f"{what}: implausible line number {n}")
    return n


def sanitize_text(s: str, limit: int = 500) -> str:
    s = "".join(c for c in str(s or "") if c == "\n" or c == "\t" or ord(c) >= 32)
    return s[:limit]


ADAPTERS: list = []


def register(cls):
    ADAPTERS.append(cls())
    return cls


def detect(text: str):
    """Return (adapter, confidence) by validated structure, not filename."""
    for ad in ADAPTERS:
        try:
            if ad.detect(text):
                return ad, "structural"
        except ReportRejected:
            continue
        except Exception:
            continue
    return None, "unknown"


def parse_bytes(report_bytes: bytes, repo_id: str, label: str = "",
                fmt: str = "", limit_bytes: int = MAX_REPORT_BYTES):
    if len(report_bytes) > limit_bytes:
        raise ReportRejected(f"report exceeds {limit_bytes} bytes ({len(report_bytes)})")
    try:
        text = report_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = report_bytes.decode("utf-8", errors="strict")
        except Exception:
            raise ReportRejected("report is not valid UTF-8 text")
    if fmt:
        for ad in ADAPTERS:
            if ad.name == fmt:
                return ad.parse(text, report_bytes, repo_id, label)
        raise ReportRejected(f"unsupported format: {fmt}")
    ad, _conf = detect(text)
    if ad is None:
        raise ReportRejected("unrecognized coverage format (validated by structure, "
                             "not filename)")
    return ad.parse(text, report_bytes, repo_id, label)
