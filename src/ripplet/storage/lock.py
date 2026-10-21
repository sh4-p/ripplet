"""Portable portable inter-process lock for Ripplet indexing (Phase 2).

Uses atomic file creation (O_CREAT | O_EXCL), which is atomic on Windows, macOS
and Linux. No third-party dependencies. Readers never take the lock; concurrent
writers either wait (up to `timeout_s`) or fail with an actionable error.
"""
from __future__ import annotations
import os
import time


class LockError(RuntimeError):
    pass


class IndexLock:
    def __init__(self, lock_path: str, timeout_s: float = 30.0, stale_after_s: float = 300.0):
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        self.stale_after_s = stale_after_s
        self._fd = None

    def _is_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.lock_path)
            return age > self.stale_after_s
        except OSError:
            return False

    def acquire(self) -> None:
        deadline = time.time() + self.timeout_s
        while True:
            try:
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"{os.getpid()}@{time.time()}".encode())
                return
            except FileExistsError:
                if self._is_stale():
                    try:
                        os.remove(self.lock_path)
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    raise LockError(
                        f"index is locked by another writer ({self.lock_path}). "
                        f"Wait for it to finish or, if no indexing is running, "
                        f"remove the stale lock file."
                    )
                time.sleep(0.2)

    def release(self) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        except OSError:
            pass
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def __enter__(self) -> "IndexLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path_for(storage_path: str) -> str:
    base, _ = os.path.splitext(storage_path)
    return base + ".lock"
