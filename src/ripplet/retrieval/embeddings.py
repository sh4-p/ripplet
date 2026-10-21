"""Embedding provider interface + Ollama local backend (Phase 4 Stage 4).

Base installation stays embedding-free: providers are constructed explicitly
(``ripplet embed`` / semantic query modes) and every failure surfaces as a
structured, actionable diagnostic. Remote endpoints require explicit opt-in;
nothing is ever sent anywhere silently.
"""
from __future__ import annotations
import json
import math
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

PIPELINE_VERSION = "1"
DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_ENDPOINT = "http://localhost:11434"


class EmbeddingError(Exception):
    pass


class ProviderUnavailable(EmbeddingError):
    pass


class DimensionMismatch(EmbeddingError):
    pass


class RemoteNotAllowed(EmbeddingError):
    pass


class ModelMissing(EmbeddingError):
    pass


@dataclass
class EmbeddingConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout_s: float = 60.0
    batch_size: int = 16
    normalize: bool = True
    max_retries: int = 2
    allow_remote: bool = False

    def key(self) -> dict:
        return {"provider": "ollama", "model": self.model,
                "normalize": self.normalize, "pipeline": PIPELINE_VERSION}


def _is_local(endpoint: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = (urlparse(endpoint).hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _check_finite(vec: list) -> None:
    for v in vec:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise EmbeddingError("provider returned non-finite vector component")


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec))
    if n == 0:
        raise EmbeddingError("provider returned zero vector")
    return [v / n for v in vec]


class EmbeddingProvider(ABC):
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def embed(self, texts: list[str], cancel=None, progress=None) -> list[list[float]]:
        """Embed a batch. Returns one vector per input, in order."""
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @abstractmethod
    def capability(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Ollama-compatible local backend (verified against Ollama 0.5.13:
    ``POST /api/embed`` with ``{"model", "input"}`` returns ``{"embeddings"}``)."""

    def __init__(self, config: EmbeddingConfig | None = None):
        self.config = config or EmbeddingConfig()
        if not _is_local(self.config.endpoint) and not self.config.allow_remote:
            raise RemoteNotAllowed(
                f"endpoint {self.config.endpoint} is not local; remote embeddings "
                f"require explicit opt-in (allow_remote=True)")
        self._dim: int | None = None

    def provider_id(self) -> str:
        return "ollama"

    @property
    def dim(self) -> int | None:
        return self._dim

    def capability(self) -> dict:
        try:
            req = urllib.request.Request(f"{self.config.endpoint}/api/version",
                                         method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                server = json.loads(r.read().decode()).get("version", "?")
            return {"available": True, "server_version": server,
                    "model_configured": self.config.model,
                    "note": "model presence is verified on first embed, not here"}
        except Exception as e:
            return {"available": False, "model_configured": self.config.model,
                    "reason": f"Ollama not reachable at {self.config.endpoint}: {e}. "
                              f"Start it with `ollama serve` and pull a model, e.g. "
                              f"`ollama pull {self.config.model}`."}

    def model_info(self) -> dict:
        """Identify the resolved model WITHOUT credentials (Stage 5.3).

        Returns requested name, endpoint host, server version, family/size/
        quantization, capability tags, embedding length and the observed vector
        dimension. Raises the usual structured errors when unavailable.
        """
        from urllib.parse import urlparse
        payload = json.dumps({"model": self.config.model}).encode()
        try:
            req = urllib.request.Request(
                f"{self.config.endpoint}/api/show", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.config.timeout_s) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ModelMissing(f"model {self.config.model!r} not present: HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderUnavailable(f"Ollama not reachable: {e}")
        details = body.get("details", {}) or {}
        info = body.get("model_info", {}) or {}
        tags = info.get("general.tags", []) or []
        emb_len = next((v for k, v in info.items() if k.endswith("embedding_length")),
                       "?")
        return {"requested": self.config.model,
                "endpoint_host": (urlparse(self.config.endpoint).hostname or ""),
                "family": details.get("family", "?"),
                "parameter_size": details.get("parameter_size", "?"),
                "quantization": details.get("quantization_level", "?"),
                "tags": tags,
                "is_embedding_model": bool(
                    {"embedding", "feature-extraction", "sentence-similarity",
                     "text-embeddings-inference"} & set(tags)
                    or info.get("general.type", "") == "embedding"),
                "embedding_length": emb_len,
                "observed_dim": self._dim,
                "dim_matches_metadata": (self._dim is None or emb_len == "?"
                                         or emb_len == self._dim)}

    def _post(self, payload: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.config.endpoint}/api/embed",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=self.config.timeout_s) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode()[:300]
                except Exception:
                    detail = ""
                if e.code == 404 and "model" in detail.lower():
                    raise ModelMissing(
                        f"model {self.config.model!r} not present: {detail} "
                        f"Pull it explicitly, e.g. `ollama pull {self.config.model}`.")
                last = EmbeddingError(f"embedding endpoint HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = ProviderUnavailable(
                    f"Ollama not reachable at {self.config.endpoint}: {e}. "
                    f"Start it with `ollama serve`.")
            if attempt < self.config.max_retries:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        raise last or EmbeddingError("embedding request failed")

    def embed(self, texts: list[str], cancel=None, progress=None) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        bs = max(1, self.config.batch_size)
        for i in range(0, len(texts), bs):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise EmbeddingError("embedding cancelled")
            chunk = texts[i:i + bs]
            body = self._post({"model": self.config.model, "input": chunk})
            vecs = body.get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(chunk):
                raise EmbeddingError(
                    f"malformed provider response: keys={sorted(body.keys())} "
                    f"embeddings={len(vecs) if isinstance(vecs, list) else '?'} "
                    f"for {len(chunk)} inputs")
            for v in vecs:
                if not isinstance(v, list) or not v:
                    raise EmbeddingError("malformed provider response: empty vector")
                _check_finite(v)
                if self._dim is None:
                    self._dim = len(v)
                elif len(v) != self._dim:
                    raise DimensionMismatch(
                        f"provider changed dimensions mid-batch: {len(v)} != {self._dim}")
                out.append(_normalize([float(x) for x in v]) if self.config.normalize
                           else [float(x) for x in v])
            if progress is not None:
                try:
                    progress(len(out), len(texts))
                except Exception:
                    pass
        return out


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic stand-in for unit tests (no model, no network).

    Vectors derive from stable text hashes, so identical inputs always yield
    identical vectors. Similarity quality is NOT meaningful — tests assert
    plumbing (validation, caching, determinism), never retrieval gains.
    """

    def __init__(self, dim: int = 32, model: str = "mock-test-model"):
        self._dim = dim
        self.model = model
        self.calls = 0

    def provider_id(self) -> str:
        return "mock"

    @property
    def dim(self):
        return self._dim

    def capability(self) -> dict:
        return {"available": True, "model_configured": self.model, "mock": True}

    def embed(self, texts: list[str], cancel=None, progress=None) -> list[list[float]]:
        import hashlib
        import random
        out = []
        for t in texts:
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise EmbeddingError("embedding cancelled")
            self.calls += 1
            rng = random.Random(hashlib.sha256(f"{self.model}\x00{t}".encode()).hexdigest())
            v = [rng.uniform(-1, 1) for _ in range(self._dim)]
            out.append(_normalize(v))
            if progress is not None:
                try:
                    progress(len(out), len(texts))
                except Exception:
                    pass
        return out
