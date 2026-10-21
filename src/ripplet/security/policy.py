from __future__ import annotations
from dataclasses import dataclass, field
@dataclass
class Policy:
    offline: bool = True
    allow_local_network: bool = False
    max_file_bytes: int = 2_000_000
    secret_patterns: list[str] = field(default_factory=lambda: [".env","id_rsa",".pem","credentials.json"])
    def blocks_file(self, name: str) -> bool:
        n = name.lower()
        return any(p.lower() in n for p in self.secret_patterns)
DEFAULT_POLICY = Policy()
