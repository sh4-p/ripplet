"""Watch-mode orchestration (Phase 7 Stages 2-4).

One pipeline only: every batch — however triggered — runs the existing
`service.index_workspace`, which owns scanning, reconciliation, locking and
atomic publication. Event handlers never touch graph tables. The watcher is a
hint source; the scan is the source of truth.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from .discovery import watch as wev

# Terminal watch states (meta `watch_state`; distinct from `generation`).
ST_WATCHING = "watching"
ST_INDEXING = "indexing"
ST_DIRTY = "dirty"
ST_FAILED = "failed"
ST_STOPPED = "stopped"


@dataclass
class WatchSettings:
    debounce_s: float = 1.0
    reconcile_s: float = 30.0
    max_retries: int = 3
    max_pending_files: int = 5000
    max_batch_wait_s: float = 8.0
    initial_index: bool = True
    quiet: bool = False


class Debouncer:
    """Coalescing batch gate with an injectable clock (deterministic tests).

    First event opens a window of `interval_s`; further events join it. The
    window extends while events arrive but never past `max_wait_s` after the
    first event, so a continuously changing file cannot stall publication
    forever. Capacity is bounded; overflow drops the oldest entries and counts
    them (the reconciliation scan, not the queue, is authoritative, so drops
    are safe and visible).
    """

    def __init__(self, interval_s: float, max_wait_s: float,
                 max_files: int = 5000, clock=None):
        self.interval_s = interval_s
        self.max_wait_s = max_wait_s
        self.max_files = max_files
        self.clock = clock or time.monotonic
        self._events: list = []
        self._deadline: float | None = None
        self._first_at: float | None = None
        self.dropped = 0

    def push(self, events) -> None:
        now = self.clock()
        self._events.extend(events)
        if len(self._events) > self.max_files:
            cut = len(self._events) - self.max_files
            del self._events[:cut]
            self.dropped += cut
        if self._first_at is None:
            self._first_at = now
            self._deadline = now + self.interval_s
        else:
            self._deadline = min(now + self.interval_s,
                                 self._first_at + self.max_wait_s)

    def due(self) -> bool:
        return bool(self._events) and self._deadline is not None \
            and self.clock() >= self._deadline

    def force_due(self) -> bool:
        return bool(self._events)

    def pop(self) -> list:
        evs, self._events = self._events, []
        self._deadline = None
        self._first_at = None
        return evs

    def pending(self) -> int:
        return len(self._events)


@dataclass
class BatchReport:
    kind: str = "events"  # events | reconcile | initial
    files_hinted: int = 0
    coalesced: dict = field(default_factory=dict)
    config_changed: bool = False
    summary: dict = field(default_factory=dict)
    validate_ok: bool | None = None
    validate_findings: int = 0
    error: str = ""
    retries: int = 0
    seconds: float = 0.0

    @property
    def noop(self) -> bool:
        s = self.summary or {}
        return not self.error and s.get("parsed", 1) == 0 \
            and s.get("nodes_removed", 1) == 0 and s.get("edges_removed", 1) == 0


class WatchController:
    """Owns debounce, scheduling, status and diagnostics. Indexing itself is
    delegated to an injected `index_fn(cfg)` (default: the real service)."""

    def __init__(self, cfg, settings: WatchSettings | None = None,
                 index_fn=None, clock=None):
        self.cfg = cfg
        self.settings = settings or WatchSettings()
        self._index_fn = index_fn
        self.clock = clock or time.monotonic
        self.debouncer = Debouncer(self.settings.debounce_s,
                                   self.settings.max_batch_wait_s,
                                   self.settings.max_pending_files,
                                   clock=self.clock)
        self.batches: list[BatchReport] = []
        self.consecutive_failures = 0
        self._stop = False

    # ------------------------------------------------------------ status
    def _meta(self, key: str, value: str) -> None:
        from .storage.sqlite_store import SQLiteStore
        try:
            st = SQLiteStore(self.cfg.storage_path)
            try:
                st.cx.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, value))
                st.commit()
            finally:
                st.close()
        except Exception:
            pass

    def set_state(self, state: str) -> None:
        self._meta("watch_state", state)
        if state in (ST_FAILED, ST_STOPPED, ST_WATCHING) or True:
            self._meta("watch_updated_at", str(self.clock()))

    def get_status(self) -> dict:
        from .service import get_status
        s = get_status(self.cfg)
        try:
            from .storage.sqlite_store import SQLiteStore
            st = SQLiteStore(self.cfg.storage_path)
            try:
                kv = {r["k"]: r["v"] for r in
                      st.cx.execute("SELECT k,v FROM meta WHERE k LIKE 'watch%'").fetchall()}
            finally:
                st.close()
        except Exception:
            kv = {}
        s["watch"] = {"state": kv.get("watch_state", "ready" if not self.batches else ST_WATCHING),
                      "pending_events": self.debouncer.pending(),
                      "dropped_coalesced": self.debouncer.dropped,
                      "consecutive_failures": self.consecutive_failures,
                      "last_batch": self.batches[-1].__dict__ if self.batches else None,
                      "batches": len(self.batches)}
        return s

    # ------------------------------------------------------------ batches
    def run_batch(self, events: list, kind: str = "events") -> BatchReport:
        """Process one batch through the real incremental pipeline."""
        from .service import validate_graph
        t0 = self.clock()
        coalesced = wev.coalesce_events(events) if events else {}
        rep = BatchReport(kind=kind, files_hinted=len(coalesced), coalesced=coalesced,
                          config_changed=wev.batch_is_config_only(coalesced) if coalesced else False)
        self.set_state(ST_INDEXING)
        try:
            if self._index_fn is None:
                from .service import index_workspace
                summary = index_workspace(self.cfg)
            else:
                summary = self._index_fn(self.cfg)
            rep.summary = summary
            try:
                v = validate_graph(self.cfg)
                rep.validate_ok = bool(v.get("ok"))
                rep.validate_findings = len(v.get("findings", []))
            except Exception as e:
                rep.validate_ok = False
                rep.validate_findings = -1
                rep.error = (rep.error + f" | validate: {e}") if rep.error else f"validate: {e}"
            self.consecutive_failures = 0
            self._meta("last_batch", "ok")
            self._meta("last_reconciled", str(self.clock()))
            self.set_state(ST_WATCHING)
        except Exception as e:
            rep.error = f"{type(e).__name__}: {e}"
            self.consecutive_failures += 1
            self._meta("last_batch", f"failed: {rep.error}"[:300])
            self.set_state(ST_FAILED)
        rep.seconds = round(self.clock() - t0, 3)
        self.batches.append(rep)
        return rep

    def run_batch_with_retry(self, events: list, kind: str = "events") -> BatchReport:
        import time as _t
        last: BatchReport | None = None
        for attempt in range(self.settings.max_retries + 1):
            last = self.run_batch(events, kind)
            last.retries = attempt
            if not last.error:
                return last
            if "locked" in last.error.lower() and attempt < self.settings.max_retries:
                _t.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            break
        return last

    # ------------------------------------------------------------ loop
    def request_stop(self) -> None:
        self._stop = True

    @property
    def stopped(self) -> bool:
        return self._stop


def run_watch(cfg, settings: WatchSettings | None = None, index_fn=None,
              event_source=None, clock=None, on_batch=None, stop_event=None) -> dict:
    """Foreground watch loop. `event_source` yields sets of FsEvent (injectable
    for deterministic tests); default uses watchfiles when installed.
    `stop_event` (threading.Event) requests graceful shutdown between batches."""
    settings = settings or WatchSettings()
    wev.require_available()
    # Priority 0: fail fast on scope mismatch before any batch, state write,
    # or initial index can touch a foreign database. Per-batch service calls
    # re-validate independently. Gone roots skip the gate: batches then fail
    # through the historical incomplete-scan path, graph preserved.
    from .workspace_scope import check_scope as _check_scope
    from .workspace_scope import missing_roots as _missing_roots
    if not _missing_roots(cfg.workspace_roots):
        _scope = _check_scope(roots=cfg.workspace_roots,
                              storage_path=cfg.storage_path, intent="watch")
        cfg.workspace_roots = list(_scope["roots"])
        cfg.storage_path = _scope["storage"]
    ctl = WatchController(cfg, settings, index_fn=index_fn, clock=clock)
    if settings.initial_index:
        ctl.run_batch([], kind="initial")
        if on_batch:
            on_batch(ctl.batches[-1])
    filt = wev.WatchFilter(roots=[str(r) for r in cfg.workspace_roots])

    def _raw():
        # Yields raw items: watchfiles change-sets, injected FsEvent lists, or
        # the "__flush__" drain marker. Batching happens in one place below.
        if event_source is not None:
            for item in event_source:
                if ctl.stopped or (stop_event is not None and stop_event.is_set()):
                    break
                yield item
            # Drain: a pending debounced batch is processed exactly once at
            # shutdown instead of being silently dropped.
            if ctl.debouncer.force_due():
                yield "__flush__"
            return
        import watchfiles
        try:
            gen = watchfiles.watch(
                *filt.roots, **filt.watch_kwargs(), yield_on_timeout=True,
                rust_timeout=int(min(settings.reconcile_s, 30) * 1000),
                stop_event=stop_event)
        except TypeError:
            gen = watchfiles.watch(*filt.roots, **filt.watch_kwargs())
        yield from gen

    from .discovery.watch import FsEvent
    last_reconcile = ctl.clock()
    try:
        ctl.set_state(ST_WATCHING)
        for item in _raw():
            if ctl.stopped or (stop_event is not None and stop_event.is_set()):
                break
            if item == "__flush__":
                pass
            elif isinstance(item, list) and not item:
                # Explicit reconcile request (used by tests and by the
                # periodic timer path): run even with no pending events.
                rep = ctl.run_batch_with_retry([], "reconcile")
                last_reconcile = ctl.clock()
                if on_batch:
                    on_batch(rep)
                if ctl.stopped or (stop_event is not None and stop_event.is_set()):
                    break
                continue
            elif isinstance(item, set):
                evs = filt.normalize(item)
                if evs:
                    ctl.debouncer.push(evs)
                    ctl.set_state(ST_DIRTY)
            else:
                evs = [e for e in (item or []) if isinstance(e, FsEvent)]
                if evs:
                    ctl.debouncer.push(evs)
                    ctl.set_state(ST_DIRTY)
            now = ctl.clock()
            due = ctl.debouncer.due()
            timeout_due = (now - last_reconcile) >= settings.reconcile_s
            if due or timeout_due:
                batch = ctl.debouncer.pop() if due else []
                kind = "events" if batch else "reconcile"
                rep = ctl.run_batch_with_retry(batch, kind)
                last_reconcile = ctl.clock()
                if on_batch:
                    on_batch(rep)
                if ctl.stopped or (stop_event is not None and stop_event.is_set()):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        ctl.set_state(ST_STOPPED)
    return {"batches": len(ctl.batches),
            "failures": sum(1 for b in ctl.batches if b.error),
            "dropped_coalesced": ctl.debouncer.dropped}
