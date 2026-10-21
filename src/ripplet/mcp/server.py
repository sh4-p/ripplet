from __future__ import annotations
import json
import os
import queue
import sys

from .. import __version__
import threading
import time
from ..config import RippletConfig
from ..storage.sqlite_store import SQLiteStore
from .tools import TOOL_DEFS, call_tool, listed_tools

# Sent in `initialize` (MCP server instructions; clients add it to the
# agent's context). Measured waste it targets: agents spent turns on
# use_workspace/get_index_status and on Read/Grep follow-ups after
# explain/query because the old answers lacked code and call sites.
SERVER_INSTRUCTIONS = (
    "Ripplet: a prebuilt code graph of this workspace. The index is "
    "already built and bound to the current directory; no setup or status "
    "calls are needed. Prefer one call over several: explain_symbol returns "
    "a symbol's source, callers and callees with file:line in one response; "
    "analyze_change_impact lists what breaks with call sites. Unresolved "
    "calls are listed by name and were not guessed. repo_map orients in an "
    "unfamiliar repo; change_plan lists every line a rename/signature change "
    "must touch before you edit. Use Read only for code "
    "outside the returned spans. A shortened answer ends with an "
    "'[ripplet: ...]' line saying how to get the rest.")

_inbox: "queue.Queue" = queue.Queue()


def _pump() -> None:
    for line in sys.stdin:
        _inbox.put(line)
    _inbox.put(None)


def _load_cfg() -> RippletConfig:
    from pathlib import Path
    from .. import legacy as _lg
    _lg.migrate_upwards(Path("."))
    for cand in [str(_lg.config_path(Path("."))), os.path.expanduser("~/.ripplet/config.json")]:
        if Path(cand).exists():
            try: return RippletConfig.load(cand)
            except Exception: pass
    c=RippletConfig.from_env()
    return c


def _root_to_path(entry: dict) -> str:
    """A roots/list entry to a local directory ("" when not usable)."""
    from pathlib import Path
    from urllib.parse import urlparse
    from urllib.request import url2pathname
    if not isinstance(entry, dict):
        return ""
    uri = entry.get("uri") or entry.get("path") or ""
    if not isinstance(uri, str) or not uri:
        return ""
    try:
        if "://" in uri:
            p = url2pathname(urlparse(uri).path)
        else:
            p = uri
        p = os.path.expanduser(p)
        return p if Path(p).is_dir() else ""
    except Exception:
        return ""


class _Binding:
    """Server-side project binding (Phase 23 zero-config fix).

    Startup behavior is unchanged (cwd config, home config, env). When
    the client advertises the `roots` capability, the server asks for
    the workspace roots and prefers `<root>/.ripplet/config.json`
    when present. Resolution re-runs on every call (cheap stat): a
    project `init`'d mid-session binds without a server restart.
    An explicit RIPPLET_STORAGE env always wins and disables
    re-resolution.
    """

    def __init__(self) -> None:
        self.pinned = bool(os.getenv("RIPPLET_STORAGE"))
        self.cfg = _load_cfg()
        self.store = SQLiteStore(self.cfg.storage_path)
        self.roots: list[str] = []
        self._key: tuple = ()

    def note_roots(self, entries: list) -> None:
        seen = []
        for e in entries or []:
            p = _root_to_path(e)
            if p and p not in seen:
                seen.append(p)
        self.roots = seen
        self.refresh(force=True)

    def _candidate(self):
        from pathlib import Path
        for root in self.roots:
            from ..legacy import config_path
            cfg_path = config_path(Path(root))
            try:
                if cfg_path.is_file():
                    return str(cfg_path), cfg_path.stat().st_mtime
            except OSError:
                continue
        return None

    def set_manual(self, root: str, cfg, store) -> None:
        """Explicit session binding (use_workspace tool). The manual root
        leads resolution afterwards, so later roots traffic cannot
        silently switch the project away again."""
        try:
            self.store.close()
        except Exception:
            pass
        self.cfg, self.store = cfg, store
        from pathlib import Path as _Path
        try:
            from ..legacy import config_path
            key_src = config_path(_Path(root))
            self._key = (str(key_src),
                         key_src.stat().st_mtime if key_src.is_file()
                         else 0)
        except OSError:
            self._key = (root, 0)
        self.roots = [root] + [r for r in self.roots if r != root]

    def refresh(self, force: bool = False) -> None:
        if self.pinned:
            return
        cand = self._candidate()
        if cand is None or cand == self._key and not force:
            return
        try:
            cfg = RippletConfig.load(cand[0])
        except Exception:
            return
        try:
            new_store = SQLiteStore(cfg.storage_path)
        except Exception:
            return
        try:
            self.store.close()
        except Exception:
            pass
        self.cfg, self.store, self._key = cfg, new_store, cand

    def current(self):
        self.refresh()
        return self.cfg, self.store


MAX_TEXT = 20000
MAX_RAW_TEXT = 80000


def _render(out, name: str = "", args: dict | None = None) -> str:
    """Tool result as MCP text, fitted to the tool's token budget (or the
    caller's `budget`). A cut is always announced with how to get the rest;
    JSON stays valid JSON."""
    from .budget import fit
    return fit(out, name, args)


def _send(o) -> None:
    sys.stdout.write(json.dumps(o)+"\n"); sys.stdout.flush()


def _use_workspace(binding, args: dict) -> dict:
    """Bind the session to a project directory (no restart needed).

    Reads <path>/.ripplet/config.json exactly like `init` wrote it;
    refuses when the project was never indexed (points at init instead
    of opening an unrelated database). Read-only: opens the existing
    database, never creates one."""
    from pathlib import Path
    raw = (args or {}).get("path", "")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("use_workspace needs a project directory path")
    root = str(Path(raw).expanduser())
    if not Path(root).is_dir():
        raise ValueError(f"not a directory: {root}")
    from ..legacy import config_path
    cfg_path = config_path(Path(root))
    if not cfg_path.is_file():
        raise ValueError(
            f"no ripplet index in {root} (missing "
            f".ripplet/config.json); run `ripplet init {root}` and "
            f"`ripplet index {root}` first")
    cfg = RippletConfig.load(str(cfg_path))
    store = SQLiteStore(cfg.storage_path)
    try:
        n, e = store.count()
    except Exception:
        n, e = -1, -1
    binding.set_manual(root, cfg, store)
    return {"status": "bound", "root": root, "storage": cfg.storage_path,
            "nodes": n, "edges": e,
            "workspace_roots": getattr(cfg, "workspace_roots", [root])}


_ROOTS_REQ_ID = "ripplet-roots-1"
_ROOTS_TIMEOUT_S = float(os.getenv("RIPPLET_ROOTS_TIMEOUT_S", "3") or 3)


def serve_stdio():
    threading.Thread(target=_pump, daemon=True).start()
    binding = _Binding()

    def handle(msg) -> bool:
        """Process one client message. Returns True when it was the
        roots/list response (handled by the handshake waiter)."""
        if not isinstance(msg, dict):
            return False
        if "method" not in msg and msg.get("id") == _ROOTS_REQ_ID and \
                ("result" in msg or "error" in msg):
            res = msg.get("result") or {}
            roots = res.get("roots") if isinstance(res, dict) else None
            if isinstance(roots, list):
                binding.note_roots(roots)
            return True
        mid = msg.get("id"); method = msg.get("method")
        params = msg.get("params", {}) or {}
        cfg, store = binding.current()
        try:
            if method=="initialize":
                caps = (params.get("capabilities") or {})
                _send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","serverInfo":{"name":"ripplet","version":__version__},"capabilities":{"tools":{}},"instructions":SERVER_INSTRUCTIONS}})
                if isinstance(caps.get("roots"), dict):
                    _send({"jsonrpc":"2.0","id":_ROOTS_REQ_ID,
                           "method":"roots/list","params":{}})
                    _await_roots(handle)
            elif method=="notifications/initialized": pass
            elif method=="tools/list": _send({"jsonrpc":"2.0","id":mid,"result":{"tools":listed_tools()}})
            elif method=="tools/call":
                name=params.get("name"); args=params.get("arguments",{}) or {}
                if name=="use_workspace":
                    try:
                        out=_use_workspace(binding, args)
                    except Exception as e:
                        _send({"jsonrpc":"2.0","id":mid,"error":{"code":-32602,"message":str(e)[:500]}})
                        return False
                    _send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text": json.dumps(out,default=str)[:20000]}]}})
                    return False
                try:
                    from .freshness import ensure_fresh
                    note = ensure_fresh(store, cfg, name)
                    out=call_tool(store,cfg,name,args)
                    text = _render(out, name, args)
                    _send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text": (note + "\n" + text) if note else text}]}})
                except Exception as e:
                    _send({"jsonrpc":"2.0","id":mid,"error":{"code":-32602,"message":str(e)[:500]}})
            elif method=="ping": _send({"jsonrpc":"2.0","id":mid,"result":{}})
            elif method is None: pass
            else: _send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":f"unknown method {method}"}})
        except Exception as e:
            _send({"jsonrpc":"2.0","id":mid,"error":{"code":-32603,"message":str(e)[:500]}})
        return False

    while True:
        line = _inbox.get()
        if line is None:
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"parse error"}})
            continue
        handle(msg)


def _await_roots(handle) -> None:
    """Wait briefly for the roots/list response, processing any other
    inbound traffic normally meanwhile. Never hangs: timeout, error,
    or EOF all fall back to the startup binding silently."""
    deadline = time.time() + max(_ROOTS_TIMEOUT_S, 0)
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        try:
            line = _inbox.get(timeout=remaining)
        except Exception:
            return
        if line is None:
            # EOF belongs to the main loop too: re-queue so serve_stdio
            # terminates instead of blocking forever after the handshake.
            _inbox.put(None)
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"parse error"}})
            continue
        if handle(msg):
            return


def main(): serve_stdio()
if __name__=="__main__": main()
