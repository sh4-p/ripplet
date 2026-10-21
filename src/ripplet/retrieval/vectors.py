"""Vector-search interface (Phase 4 Stage 6).

SQLite-first: vectors live in the existing database as packed-float BLOBs.
The default implementation is exact cosine similarity — a correctness baseline
that is honest about its O(n) cost. An indexed backend can implement the same
interface later; `EXACT_ROW_THRESHOLD` documents the switch-over point.
"""
from __future__ import annotations
import math
import struct
from abc import ABC, abstractmethod

# Above this many rows, exact scan is discouraged (diagnostic hint only).
EXACT_ROW_THRESHOLD = 50000


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    if len(blob) % 4 != 0:
        raise ValueError(f"corrupt vector blob ({len(blob)} bytes)")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def check_vector(vec, dim: int | None = None) -> int:
    """Validate a candidate vector. Returns its dimension. Raises on abuse."""
    if not isinstance(vec, (list, tuple)) or not vec:
        raise ValueError("vector must be a non-empty list")
    for v in vec:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("vector contains non-finite values")
    if dim is not None and len(vec) != dim:
        raise ValueError(f"dimension mismatch: got {len(vec)}, want {dim}")
    return len(vec)


class VectorIndex(ABC):
    @abstractmethod
    def add(self, key: str, vec: list[float]) -> None:
        raise NotImplementedError

    @abstractmethod
    def remove(self, keys) -> int:
        raise NotImplementedError

    @abstractmethod
    def search(self, vec: list[float], k: int) -> list[tuple[str, float]]:
        """Top-k (key, cosine) sorted by score desc. Deterministic ties by key."""
        raise NotImplementedError

    @abstractmethod
    def __len__(self) -> int:
        raise NotImplementedError


class ExactCosineIndex(VectorIndex):
    """In-memory exact baseline. Loads are explicit; no hidden state."""

    def __init__(self, dim: int | None = None):
        self._dim = dim
        self._rows: dict[str, list[float]] = {}

    def add(self, key: str, vec: list[float]) -> None:
        dim = check_vector(vec, self._dim)
        if self._dim is None:
            self._dim = dim
        self._rows[key] = [float(v) for v in vec]

    def remove(self, keys) -> int:
        n = 0
        for k in keys:
            if k in self._rows:
                del self._rows[k]
                n += 1
        return n

    def search(self, vec: list[float], k: int) -> list[tuple[str, float]]:
        check_vector(vec, self._dim)
        scored = [(key, cosine(vec, row)) for key, row in self._rows.items()]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:max(0, k)]

    def __len__(self) -> int:
        return len(self._rows)
