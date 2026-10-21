from __future__ import annotations
import html, re
INJECTION = re.compile(r"(ignore\s+previous\s+instructions|reveal\s+.*env|exec\(|system\s*:)", re.I)
def escape_html(s: str) -> str: return html.escape(s)
def contains_injection(text: str) -> bool: return bool(INJECTION.search(text))
def mark_untrusted(text: str) -> str: return text
def redact_secrets(text: str) -> str:
    import re as _re
    # key=value style secrets (broadened for captured test output: passwords,
    # bearer tokens, private keys). Replacement keeps the key name so the
    # redaction itself stays auditable.
    text = _re.sub(
        r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|bearer|private[_-]?key|"
        r"aws[_-](secret|session[_-]?token)|connection[_-]?string)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text)
    # PEM private-key blocks (multi-line) -> single redaction marker.
    text = _re.sub(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]", text, flags=_re.S)
    return text
