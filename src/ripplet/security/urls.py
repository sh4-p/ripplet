from __future__ import annotations
import ipaddress, socket
from urllib.parse import urlparse
from ..core.errors import SecurityError
ALLOWED = {"http","https"}
BLOCKED_HOSTS = {"localhost"}
def validate_url(url: str, allow_local: bool = False) -> str:
    u = urlparse(url)
    if u.scheme not in ALLOWED: raise SecurityError(f"disallowed scheme: {u.scheme}")
    if not u.hostname: raise SecurityError("URL without host")
    host = u.hostname.lower()
    if host in BLOCKED_HOSTS and not allow_local: raise SecurityError("blocked host")
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_loopback or ip.is_link_local) and not allow_local:
            raise SecurityError("blocked internal IP")
    except ValueError:
        pass
    return url
def check_redirect(target: str, allow_local: bool = False) -> str:
    return validate_url(target, allow_local)
