from __future__ import annotations
import json, os
from dataclasses import dataclass, field, asdict
from pathlib import Path
@dataclass
class RippletConfig:
    workspace_roots: list[str] = field(default_factory=list)
    storage_path: str = ".ripplet/graph.db"
    project_id: str = "default"
    max_file_bytes: int = 2_000_000
    max_depth: int = 6
    max_results: int = 50
    query_timeout_s: float = 10.0
    workers: int = 4
    offline: bool = True
    log_level: str = "INFO"
    lock_timeout_s: float = 30.0
    embedding_model: str = ""
    embedding_endpoint: str = "http://localhost:11434"
    semantic_top_k: int = 20
    allow_remote_embeddings: bool = False
    test_timeout_s: float = 300.0
    test_max_output_bytes: int = 1_000_000
    test_max_tests: int = 100
    test_max_concurrent: int = 1
    test_retention_runs: int = 100
    test_plan_retention: int = 200
    test_runner: str = "pytest"
    # Ambiguous-extension declarations:
    # some suffixes mean different languages in different projects
    # (`.h` C vs C++, `.m` Objective-C vs MATLAB, `.inc` PHP vs Pascal).
    # Without a declaration the file parses as the wrong language and yields
    # incidental nodes while the real symbols go missing. Keys are suffixes
    # (`".h"` or `"h"`); values are a bounded allowlist below. Unknown values
    # are ignored (reported once per index run, defaults kept). Part of the
    # config hash, so changing a declaration invalidates the file cache.
    language_overrides: dict = field(default_factory=dict)
    @staticmethod
    def defaults() -> "RippletConfig": return RippletConfig()
    def config_hash(self) -> str:
        import hashlib
        d = asdict(self)
        # lock timeout affects waiting only, never extraction: excluded so tuning
        # it does not invalidate the cache. Embedding settings likewise live
        # outside the extraction cache key (semantic index has its own keys).
        d.pop("lock_timeout_s", None)
        for k in ("embedding_model", "embedding_endpoint", "semantic_top_k",
                  "allow_remote_embeddings", "test_timeout_s",
                  "test_max_output_bytes", "test_max_tests",
                  "test_max_concurrent", "test_retention_runs",
                  "test_plan_retention", "test_runner"):
            d.pop(k, None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]
    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
    @staticmethod
    def load(path: str) -> "RippletConfig":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        cfg = RippletConfig()
        for k,v in d.items():
            if hasattr(cfg, k): setattr(cfg, k, v)
        for k in ("workspace_roots",):
            if isinstance(getattr(cfg,k), str): setattr(cfg,k,[getattr(cfg,k)])
        return cfg
    @staticmethod
    def from_env() -> "RippletConfig":
        c = RippletConfig.defaults()
        if os.getenv("RIPPLET_STORAGE"): c.storage_path = os.environ["RIPPLET_STORAGE"]
        if os.getenv("RIPPLET_OFFLINE","1") == "0": c.offline = False
        return c
CONFIG_PRECEDENCE = ["cli-override","env","user-config","project-config","defaults"]

# Bounded allowlist for language_overrides values (extractor dispatch keys).
LANGUAGE_OVERRIDE_VALUES = ("python", "javascript", "typescript", "tsx",
                             "generic-text", "file-only", "markdown",
                             "json", "yaml", "toml",
                             # Phase 41 Wave 3: ambiguity hatch (`.h`
                             # C/C++/ObjC). Opt-in, explicit, cache-keyed.
                             "cpp", "c", "kotlin", "lua")


def normalize_language_overrides(raw) -> dict:
    """Normalize to `{".ext": "language"}`; unknown values dropped.

    Never raises: unparseable input yields {} (defaults kept everywhere).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = k.strip().lower()
        if not key.startswith("."):
            key = "." + key
        val = v.strip().lower()
        if val in LANGUAGE_OVERRIDE_VALUES:
            out[key] = val
    return out
