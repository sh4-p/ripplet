from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from .config import RippletConfig
from .diagnostics import collect
from . import __version__ as VERSION
def _cfg(args) -> RippletConfig:
    cands=[]
    if getattr(args,"config",None): cands.append(args.config)
    from .legacy import config_path, migrate_upwards
    migrate_upwards(Path(getattr(args, "path", None) or "."))
    cands += [str(config_path(Path(".")))]
    for p in cands:
        if p and Path(p).exists():
            try: return RippletConfig.load(p)
            except Exception as e: print(f"warn: cannot load {p}: {e}", file=sys.stderr)
    c=RippletConfig.from_env()
    # env override storage?
    if getattr(args,"storage",None): c.storage_path=args.storage
    return c
def _cli_index_scope(cfg, args):
    """Shared scope construction for index/update/watch (Priority 0).

    An explicit path argument selects that workspace (preferring its own
    config, like init would declare it); otherwise the ambient config
    applies. Storage follows the selected workspace when the default
    relative value is in effect, so a bare `watch <path>` can no longer
    silently write one project's graph into another project's database.
    Identity itself is validated inside the service before any mutation.
    """
    from .workspace_scope import normalize_root
    if getattr(args, "path", None):
        from .legacy import config_path
        pc = config_path(Path(args.path))
        if pc.exists():
            try: cfg = RippletConfig.load(str(pc))
            except Exception: cfg = _cfg(args)
        else: cfg = _cfg(args)
        new_root = str(Path(args.path).resolve())
        if not Path(new_root).is_dir():
            print(f"workspace root is not a directory: {args.path}", file=sys.stderr)
            return None
        if cfg.workspace_roots and new_root not in [str(Path(r).resolve()) for r in cfg.workspace_roots]:
            print(f"note: indexing scope {new_root} into the same database as "
                  f"{cfg.workspace_roots}; graphs are namespaced per repository. "
                  f"Use --storage for a separate index.", file=sys.stderr)
        if new_root not in [str(Path(r).resolve()) for r in (cfg.workspace_roots or [])]:
            cfg.workspace_roots=[new_root]
        # else: keep every root linked by `init --link` (multi-repo workspace)
        # keep storage inside workspace if default relative path is used
        if cfg.storage_path in (".ripplet/graph.db", ".ripplet\\graph.db"):
            cfg.storage_path = str(Path(args.path).resolve()/".ripplet"/"graph.db")
    else: cfg=_cfg(args)
    if not cfg.workspace_roots: print("no workspace; run init first", file=sys.stderr); return None
    return cfg
def cmd_hooks(args):
    """`hooks on|off|stats`: the Claude Code PostToolUse hook that shows large
    Read/Grep results compactly (context/hook.py)."""
    from .context import hook as _hook
    root = Path(args.path or ".").resolve()
    if args.action == "on":
        if _hook._state_dir(root / "_") is None:
            print(f"no ripplet index under {root}; run init + index first", file=sys.stderr)
            return 2
        p = _hook.install(root)
        print(f"compact Read/Grep hook enabled: {p}")
        print("experimental: on six explain tasks with Haiku it lowered answer completeness (97% -> 88% of the key "
              "facts) and did not lower cost (the agent re-reads what the view hid); keep it off unless you "
              "measure a gain on your own tasks")
    elif args.action == "off":
        print("hook removed" if _hook.uninstall(root) else "no ripplet hook installed")
    else:
        s = _hook.stats(root)
        for tool, (n, orig, new) in s.items():
            saved = orig - new
            pct = f"{saved / orig:.0%}" if orig else "-"
            print(f"{tool:5} {n:5} views  {orig:>10,} -> {new:>10,} chars  (saved {saved:,}, {pct})")
    return 0


def cmd_guard(args):
    """`guard on|off|status`: run check_change automatically when the agent
    finishes (Claude Code Stop hook and/or OpenCode plugin)."""
    from .context import guard as _g
    root = Path(args.path or ".").resolve()
    targets = {"claude", "opencode"} if args.target == "both" else {args.target}
    if args.action == "on":
        if _g.find_config(root) is None:
            print(f"no ripplet index under {root}; run init + index first", file=sys.stderr)
            return 2
        src = str(Path(__file__).resolve().parents[1])
        if "claude" in targets:
            print(f"Claude Code Stop hook: {_g.install_claude(root)}")
        if "opencode" in targets:
            print(f"OpenCode plugin: {_g.install_opencode(root, src)}")
    elif args.action == "off":
        if "claude" in targets:
            print("Claude Code hook removed" if _g.uninstall_claude(root) else "no Claude Code guard")
        if "opencode" in targets:
            print("OpenCode plugin removed" if _g.uninstall_opencode(root) else "no OpenCode guard")
    else:
        for rec in _g.history(root):
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['t']))}  "
                  f"{'BLOCK' if rec['block'] else 'ok   '}  {rec['seconds']:>5}s  {rec.get('summary', '')}")
    return 0
def cmd_rules(args):
    """`rules on|off|status`: Ripplet guidance for every coding agent (Claude Code,
    OpenCode, Codex, Gemini CLI, Cursor, Copilot, Kiro, Windsurf, Antigravity,
    Cline, Codebuddy, Hermes...): always-on rules in the files each one reads,
    plus hooks where the agent supports them. --strict blocks the first raw
    read/search of source once per session until a Ripplet tool is used."""
    from .context import integrations as _ig
    from .context import rules as _r
    root = Path(args.path or ".").resolve()
    if args.action == "on":
        if _r._db(root) is None:
            print(f"no ripplet index under {root}; run init + index first", file=sys.stderr)
            return 2
        for line in _ig.on(root, args.target, strict=args.strict, hooks=not args.no_nudge,
                           prefetch=True if args.prefetch else None):
            print(line)
    elif args.action == "off":
        for line in _ig.off(root) or ["nothing to remove"]:
            print(line)
    else:
        for line in _ig.status(root):
            print(line)
    return 0
def cmd_tool(args):
    """`tool <name> key=value ...`: call any MCP tool from the shell, with the
    same bounded output an agent gets over MCP."""
    from .storage.sqlite_store import SQLiteStore
    from .mcp.tools import call_tool
    from .mcp.budget import fit
    kw = {}
    for a in args.kv:
        if "=" not in a:
            print(f"argument {a!r} is not key=value", file=sys.stderr)
            return 2
        k, v = a.split("=", 1)
        try:
            kw[k] = json.loads(v)
        except ValueError:
            kw[k] = v
    cfg = _cfg(args); st = SQLiteStore(cfg.storage_path)
    try:
        print(fit(call_tool(st, cfg, args.name, kw), args.name, kw))
    finally:
        st.close()
    return 0
def cmd_init(args):
    target=args.path or "."
    rp=Path(target).resolve(); rp.mkdir(parents=True, exist_ok=True)
    cfg_path = rp/".ripplet"/"config.json"
    storage = getattr(args, "storage", None) or str(rp/".ripplet"/"graph.db")
    if cfg_path.exists() and not getattr(args, "force", False):
        try:
            old = RippletConfig.load(str(cfg_path))
            if ([str(Path(r).resolve()) for r in (old.workspace_roots or [])][:1] != [str(rp)] or
                    str(Path(old.storage_path).resolve() if Path(old.storage_path).is_absolute() else (Path.cwd()/old.storage_path).resolve()) != str(Path(storage).resolve())):
                print(f"existing workspace config at {cfg_path} differs; "
                      f"re-run with --force to overwrite it", file=sys.stderr)
                return 2
        except Exception:
            pass
    linked = []
    for extra in getattr(args, "link", None) or []:
        ep = Path(extra).resolve()
        if not ep.is_dir():
            print(f"linked repository is not a directory: {extra}", file=sys.stderr)
            return 2
        if str(ep) != str(rp) and str(ep) not in linked:
            linked.append(str(ep))
    cfg=RippletConfig(workspace_roots=[str(rp)] + linked, storage_path=storage, project_id=rp.name)
    cfg.save(str(cfg_path))
    print(f"initialized ripplet in {rp}\n  config: {cfg_path}\n  storage: {cfg.storage_path}")
    for ep in linked:
        print(f"  linked repository: {ep}")
    _write_opencode_binding(rp, storage)
    _write_claude_binding(rp, storage)
    if not getattr(args, "no_rules", False):
        # zero-config guidance for whichever agent the user runs here; hooks act
        # only once the index exists (`ripplet rules off` removes all of it)
        from .context import integrations as _ig
        for line in _ig.on(rp, "auto"):
            print(f"  {line}")
    return 0
def _write_opencode_binding(rp: Path, storage: str) -> None:
    """Write/merge the project-local opencode MCP binding (zero-config
    fix): `opencode.json` in the project root pins the ripplet server
    to this workspace (cwd + absolute storage), so fresh setups work
    without manual MCP configuration. Existing files are merged, never
    replaced; only the `mcp.ripplet` key is touched."""
    import sys as _sys
    target = rp/"opencode.json"
    entry = {"type": "local",
             "command": [_sys.executable, "-m", "ripplet.mcp.server"],
             "cwd": str(rp),
             "environment": {"RIPPLET_STORAGE": str(storage)},
             "enabled": True, "timeout": 180000}
    try:
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"warn: {target} is not valid JSON ({e}); "
                      f"leaving it untouched — add the ripplet MCP "
                      f"binding manually", file=sys.stderr)
                return
            if not isinstance(data, dict):
                print(f"warn: {target} is not a JSON object; leaving it "
                      f"untouched", file=sys.stderr)
                return
        else:
            data = {"$schema": "https://opencode.ai/config.json"}
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        legacy = mcp.pop("atlasgraph", None) is not None
        # the explore subagent does the reading in opencode sessions: let it
        # use the graph tools too (an existing explicit rule is kept)
        agents = data.setdefault("agent", {}) if isinstance(data.get("agent", {}), dict) else {}
        explore = agents.setdefault("explore", {}) if isinstance(agents.get("explore", {}), dict) else {}
        perm = explore.setdefault("permission", {}) if isinstance(explore.get("permission", {}), dict) else {}
        added = "ripplet_*" not in perm
        perm.setdefault("ripplet_*", "allow")
        if mcp.get("ripplet") == entry and not legacy and not added:
            return
        mcp["ripplet"] = entry
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  mcp binding: {target} (cwd + storage pinned)")
    except OSError as e:
        print(f"warn: cannot write {target}: {e}", file=sys.stderr)
def _write_claude_binding(rp: Path, storage: str) -> None:
    """Project-scoped Claude Code MCP binding (`.mcp.json`), merged like the
    opencode one: only `mcpServers.ripplet` is touched."""
    import sys as _sys
    target = rp / ".mcp.json"
    entry = {"command": Path(_sys.executable).as_posix(),
             "args": ["-m", "ripplet.mcp.server"],
             "env": {"RIPPLET_STORAGE": str(storage)}}
    try:
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"warn: {target} is not valid JSON ({e}); leaving it untouched", file=sys.stderr)
                return
            if not isinstance(data, dict):
                print(f"warn: {target} is not a JSON object; leaving it untouched", file=sys.stderr)
                return
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        legacy = servers.pop("atlasgraph", None) is not None
        if servers.get("ripplet") == entry and not legacy:
            return
        servers["ripplet"] = entry
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  mcp binding: {target} (Claude Code)")
    except OSError as e:
        print(f"warn: cannot write {target}: {e}", file=sys.stderr)


def cmd_setup(args):
    """One command to make a repository agent-ready: init (when needed),
    index, MCP bindings for Claude Code and OpenCode, always-on rules and
    the change guard. Every step is idempotent and reversible."""
    from types import SimpleNamespace
    from .context import guard as _g, rules as _r
    rp = Path(args.path or ".").resolve()
    cfg_path = rp / ".ripplet" / "config.json"
    steps = []
    if not cfg_path.exists():
        rc = cmd_init(SimpleNamespace(path=str(rp), force=False, link=[], storage=None))
        if rc:
            return rc
        steps.append("initialized")
    cfg = RippletConfig.load(str(cfg_path))
    _write_opencode_binding(rp, cfg.storage_path)
    _write_claude_binding(rp, cfg.storage_path)
    from .service import index_workspace
    print("indexing ...")
    s = index_workspace(cfg)
    steps.append(f"indexed {s['files']} files -> {s['nodes']} nodes, {s['edges']} edges ({s['seconds']}s)")
    if s.get("errors"):
        print("errors:\n" + "\n".join(s["errors"][:10]), file=sys.stderr)
    if not args.no_rules:
        # every agent the repository uses: rules text + hooks/plugins
        from .context import integrations as _ig
        steps.extend(_ig.on(rp, "auto", hooks=not args.no_nudge))
    if not args.no_guard:
        _g.install_claude(rp)
        _g.install_opencode(rp, str(Path(__file__).resolve().parents[1]))
        steps.append("change guard on (Claude Code Stop hook + OpenCode plugin)")
    print(f"\nripplet is set up in {rp}:")
    for x in steps:
        print(f"  - {x}")
    print("\nRestart the agent so it loads the MCP server. Undo: `ripplet rules off`, "
          "`ripplet guard off`, remove `ripplet` from .mcp.json / opencode.json.")
    return 0


def cmd_index(args):
    from .service import index_workspace
    from .workspace_scope import WorkspaceMismatchError
    cfg=_cli_index_scope(_cfg(args), args)
    if cfg is None: return 2
    def prog(p):
        if not args.quiet: print(f"  {p}")
    try:
        s=index_workspace(cfg, None if args.quiet else prog)
    except WorkspaceMismatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if s.get("workspace_notice"): print(f"note: {s['workspace_notice']}", file=sys.stderr)
    print(json.dumps(s,indent=2) if args.json else f"indexed {s['files']} files ({s['reused']} reused) -> {s['nodes']} nodes, {s['edges']} edges in {s['seconds']}s")
    if s.get("errors"): print("errors:\n"+"\n".join(s["errors"][:10]), file=sys.stderr)
    return 0
def cmd_update(args): return cmd_index(args)
def cmd_status(args):
    from .service import get_status
    from .extraction.code import tree_sitter as tsa
    s=get_status(_cfg(args))
    if args.json: print(json.dumps(s,indent=2,default=str))
    else:
        print(f"state={s['state']} nodes={s['nodes']} edges={s['edges']} files={s['tracked_files']}\nstorage={s['storage']}")
        caps = tsa.capabilities()
        deep = [l for l, v in caps.get("languages", {}).items() if v.get("initialized")]
        if deep: print(f"deep parse: {', '.join(sorted(deep))} (tree-sitter {caps.get('package_version')})")
        else: print(f"deep parse: unavailable ({caps.get('reason', 'no parser')}); JS/TS use generic fallback")
    return 0
def cmd_parsers(args):
    from .extraction.code import tree_sitter as tsa
    from .storage.sqlite_store import SQLiteStore
    from collections import Counter
    caps = tsa.capabilities()
    rep = {"capabilities": caps, "files_by_parser": {}, "unresolved_js_imports": 0,
           "ambiguous_js_calls": 0}
    try:
        from .extraction.pipeline import LANGUAGE_MATRIX
        rep["language_matrix"] = LANGUAGE_MATRIX
    except Exception:
        rep["language_matrix"] = {}
    try:
        # Phase 38: universal-grammar capabilities ride alongside the
        # legacy ones (additive key; legacy shapes untouched).
        rep["univ_capabilities"] = tsa.univ_capabilities()
    except Exception:
        rep["univ_capabilities"] = {}
    try:
        cfg = _cfg(args); st = SQLiteStore(cfg.storage_path)
        try:
            from .config import normalize_language_overrides as _nlo
            rep["language_overrides"] = _nlo(getattr(cfg, "language_overrides", None))
        except Exception:
            rep["language_overrides"] = {}
        try:
            kinds = Counter()
            # Phase 31: per-language fallback counts. files_by_parser keeps
            # its legacy backend:status:language buckets byte-identically;
            # this additive key breaks out the non-JS fallback languages
            # (go/rs/java/...) that the generic bucket collapses into one.
            fallback_langs = Counter()
            for n in st.all_nodes():
                if n.type == "file":
                    p = (n.attributes or {}).get("parser") or {}
                    backend = p.get("backend", "unknown")
                    kinds[f"{backend}:{p.get('status', '')}:{n.language}"] += 1
                    if backend == "generic-fallback" and \
                            p.get("status") == "fallback" and p.get("language"):
                        fallback_langs[str(p["language"])] += 1
            rep["files_by_parser"] = dict(kinds)
            rep["fallback_files_by_language"] = dict(sorted(fallback_langs.items()))
            rep["unresolved_js_imports"] = len(
                [e for e in st.all_edges() if e["relation"] == "imports"
                 and e["method"].startswith("jsts") and e["target"].startswith("ext:")])
            rep["ambiguous_js_calls"] = len(
                [e for e in st.all_edges() if "ambiguous" in str(e["attributes"])])
        finally: st.close()
    except Exception as e:
        rep["error"] = str(e)[:300]
    print(json.dumps(rep, indent=2, default=str))
    return 0
def cmd_search(args):
    from .storage.sqlite_store import SQLiteStore
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        res=st.search_lexical(args.term, args.limit)
        if args.json: print(json.dumps([n.to_dict() for n in res],indent=2,default=str))
        else:
            for n in res: print(f"{n.type:12} {n.qualified_name or n.label}  {n.file_path}:{n.location.start_line}")
            if not res: print("(no results)")
    finally: st.close()
    return 0
def cmd_query(args):
    from .storage.sqlite_store import SQLiteStore
    from .query.executor import execute
    from .query.formatter import to_text
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        if getattr(args, "format", "")=="fact_pack_v1":
            from .mcp.tools import call_tool
            r=call_tool(st,cfg,"query_graph",{"compact":False,"query":args.question,"limit":args.limit,
                        "mode":getattr(args, "mode", "hybrid") or "hybrid",
                        "format":"fact_pack_v1"})
            print(json.dumps(r,indent=2,default=str))
            return 0
        r=execute(st, args.question, args.limit, cfg=cfg,
                  mode=getattr(args, "mode", "hybrid") or "hybrid")
        print(json.dumps(r,indent=2,default=str) if args.json else to_text(r))
    finally: st.close()
    return 0
def cmd_explain(args):
    from .storage.sqlite_store import SQLiteStore
    from .query.executor import execute
    from .query.formatter import to_text
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        r=execute(st, f"Explain how {args.symbol} works", args.limit)
        print(json.dumps(r,indent=2,default=str) if args.json else to_text(r))
        # unresolved calls explicit
        nbs=[]
        for n in st.search_symbols(args.symbol,3):
            for e in st.neighbors(n.id, "out", ["calls_unresolved"], 20): nbs.append(e)
        if nbs and not args.json: print(f"\nUnresolved calls: {len(nbs)}"); [print(f"  ? {e['target']} @ {e['file_path']}:{e['start_line']}") for e in nbs[:10]]
    finally: st.close()
    return 0
def cmd_path(args):
    from .storage.sqlite_store import SQLiteStore
    from .mcp.tools import call_tool
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        out=call_tool(st,cfg,"find_path",{"source":args.source,"target":args.target,"max_depth":args.depth})
        print(json.dumps(out,indent=2,default=str))
    finally: st.close()
    return 0
def cmd_impact(args):
    from .storage.sqlite_store import SQLiteStore
    from .mcp.tools import call_tool
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        syms=[]
        if args.symbol: syms=[args.symbol]
        files=[]
        if args.diff:
            import subprocess
            try:
                d=subprocess.run(["git","diff","--name-only",args.diff],capture_output=True,text=True,timeout=15,stdin=subprocess.DEVNULL)
                if d.returncode==0: files=[l.strip() for l in d.stdout.splitlines() if l.strip()]
            except Exception: pass
            if not files and Path(args.diff).exists(): files=[args.diff]
        out=call_tool(st,cfg,"analyze_change_impact",{"symbols":syms,"files":files,"max_depth":args.depth,"compact":False})
        print(json.dumps(out,indent=2,default=str))
    finally: st.close()
    return 0
def cmd_history(args):
    from .extraction.integrations import git as G
    root=( _cfg(args).workspace_roots or ["."])[0]
    if not G.is_repo(root): print("not a git repo: "+root); return 1
    for c in G.log(root, args.limit)[:args.limit]:
        print(f"{c.sha[:8]} {c.date} {c.author}: {c.message} ({len(c.files)} files)")
    return 0
def cmd_validate(args):
    from .service import validate_graph
    print(json.dumps(validate_graph(_cfg(args)),indent=2))
    return 0
def cmd_diagnostics(args):
    from .extraction.code import tree_sitter as tsa
    d = collect(VERSION)
    d["capabilities"] = tsa.capabilities()
    print(json.dumps(d,indent=2))
    return 0
def cmd_export(args):
    from .storage.sqlite_store import SQLiteStore
    cfg=_cfg(args); st=SQLiteStore(cfg.storage_path)
    try:
        if args.format=="json":
            from .exports.json_exporter import export
            print(export(st, args.out))
        elif args.format=="graphml":
            from .exports.graphml_exporter import export
            print(export(st, args.out))
        elif args.format=="html":
            from .exports.html_exporter import export
            print(export(st.all_nodes(), st.all_edges(), args.out))
        else: print("unknown format", file=sys.stderr); return 2
    finally: st.close()
    return 0
def cmd_check(args):
    from .change.check import check_change, render_text
    cfg = _cfg(args)
    if getattr(args, "path", None):
        from .legacy import config_path
        cp = config_path(Path(args.path))
        if cp.exists():
            cfg = RippletConfig.load(str(cp))
    r = check_change(cfg, base=args.base or None, depth=args.depth, reindex=not args.no_reindex,
                     task=getattr(args, "task", "") or "")
    runs = []
    if getattr(args, "run_tests", False) and (r.get("tests") or {}).get("commands"):
        from .change.verify import render_runs, run_tests
        from .storage.sqlite_store import SQLiteStore
        st = SQLiteStore(cfg.storage_path)
        try:
            runs = run_tests(st, cfg, r["tests"]["commands"], timeout=args.test_timeout)
        finally:
            st.close()
        r["test_runs"] = runs
    print(json.dumps(r, indent=2, default=str) if args.json else render_text(r) + (
        "\n" + render_runs(runs) if runs else ""))
    if r.get("error"):
        return 1
    return 1 if any(not x["passed"] for x in runs) else 0
def cmd_review(args):
    """`review --base <ref>`: the branch/PR reviewed against its base, as a
    PR comment (markdown), SARIF for code scanning, or JSON; exit status
    follows --fail-on for CI."""
    from .change.review import build_review, exit_code, render
    from .legacy import config_path
    cfg = _cfg(args)
    if getattr(args, "path", None):
        cp = config_path(Path(args.path))
        if cp.exists():
            cfg = RippletConfig.load(str(cp))
    r = build_review(cfg, args.base, depth=args.depth, task=getattr(args, "task", "") or "")
    text = render(r, args.format)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}: {r.get('counts', {})}")
    else:
        print(text)
    return exit_code(r, args.fail_on)
def cmd_arch(args):
    """`arch`: every architecture rule violation in the indexed graph (exit 1 on any error)."""
    from .change.rules import EXAMPLE, check_rules, render
    if args.example:
        print(EXAMPLE)
        return 0
    cfg = _cfg(args)
    if getattr(args, "path", None):
        from .legacy import config_path
        cp = config_path(Path(args.path))
        if cp.exists():
            cfg = RippletConfig.load(str(cp))
    r = check_rules(cfg)
    print(json.dumps(r, indent=2, default=str) if args.json else render(r))
    if r.get("error"):
        return 2
    return 1 if any(v["severity"] == "error" for v in r["violations"]) else 0
def cmd_ui(args):
    from .ui.server import serve
    cfg = _cfg(args)
    if getattr(args, "path", None):
        from .legacy import config_path
        cp = config_path(Path(args.path))
        if not cp.exists():
            print(f"no index in {args.path}; run `ripplet init` and `ripplet index` there first", file=sys.stderr)
            return 2
        cfg = RippletConfig.load(str(cp))
    return serve(cfg, host=args.host, port=args.port, open_browser=not args.no_browser)
def cmd_mcp(args):
    from .mcp.server import serve_stdio
    serve_stdio(); return 0
def cmd_test_plan(args):
    import json as _json
    from .testplan import changes as _ch, plan as _plan
    from .storage.sqlite_store import SQLiteStore
    cfg = _cfg(args)
    if not cfg.workspace_roots:
        print("no workspace; run init first", file=sys.stderr)
        return 2
    mode = (args.mode or "balanced").lower()
    if mode not in ("conservative", "balanced", "broad"):
        print(f"unknown mode: {args.mode} (conservative|balanced|broad)", file=sys.stderr)
        return 2
    root = str(Path(cfg.workspace_roots[0]).resolve())
    from .config import RippletConfig as _AC
    _rid = __import__("hashlib").sha256(root.encode()).hexdigest()[:10]
    repo_id = "repo-" + _rid
    st = SQLiteStore(cfg.storage_path)
    try:
        changed, revision, digest_src = [], "", ""
        if args.diff:
            if "..." in args.diff:
                base, head = args.diff.split("...", 1)
                sep = "..."
            elif ".." in args.diff:
                base, head = args.diff.split("..", 1)
                sep = ".."
            else:
                base, head, sep = args.diff, "", "..."
            try:
                changed = _ch.diff_files(root, base.strip(), head.strip(),
                                         separator=sep)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            revision = (head.strip() or base.strip())
            digest_src = f"diff:{args.diff}"
        elif args.working_tree:
            try:
                changed = _ch.working_tree_files(root)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            revision = "working-tree"
            digest_src = "working-tree"
        elif getattr(args, "files", None):
            try:
                changed = [_ch.ChangedFile(_ch.resolve_repo(cfg.workspace_roots, f)[1])
                           for f in args.files]
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            digest_src = "files:" + ",".join(sorted(args.files))
        else:
            changed = []
            digest_src = "symbols:" + ",".join(sorted(args.symbol or []))
        mapped = _ch.map_to_symbols(st, repo_id, changed)
        # explicit symbols bypass file mapping
        for sym in (args.symbol or []):
            cands = st.search_symbols(sym, 5, partial=False) or \
                st.search_symbols(sym, 5, partial=True)
            if not cands:
                mapped.setdefault("unmapped_files", []).append(f"symbol:{sym}")
                continue
            mapped.setdefault("symbols", {}).setdefault(
                cands[0].id, {"change": "modified", "via": "explicit",
                              "file": cands[0].file_path, "status": "modified"})
        if not changed and not (args.symbol or []):
            print("nothing to plan: empty diff and no files/symbols", file=sys.stderr)
            return 2
        plan = _plan.build(st, repo_id, root, changed, mapped, mode=mode,
                           revision=_ch.current_sha(root), change_digest=digest_src)
        plan["candidates"] = plan["candidates"][:args.limit]
        try:
            from .testrun import service as _tr
            from .workspace_scope import check_scope as _check_scope
            from .workspace_scope import WorkspaceMismatchError as _Mismatch
            try:
                _check_scope(roots=[root], storage_path=cfg.storage_path,
                             intent="test-plan")
            except _Mismatch as e:
                # Planning stays read-only-safe: a foreign database gets no
                # plan record, and planning itself still succeeds.
                print(f"warn: plan record not stored (scope): {str(e).splitlines()[0]}",
                      file=sys.stderr)
            else:
                _tr._persist_plan(st, cfg, plan)
        except Exception as e:
            # persistence is best-effort: planning stays read-only and must
            # never fail because the plan record could not be stored.
            print(f"warn: plan record not stored: {e}", file=sys.stderr)
        if (args.format or "text") == "json":
            print(_json.dumps(plan, indent=2, default=str))
        else:
            print(_plan.to_markdown(plan))
        return 0
    finally:
        try: st.close()
        except Exception: pass
def cmd_test_run(args):
    import json as _json
    from .testrun import service as _tr, store as _store
    from .storage.sqlite_store import SQLiteStore
    cfg = _cfg(args)
    if not cfg.workspace_roots:
        print("no workspace; run init first", file=sys.stderr)
        return 2
    action = getattr(args, "run_action", "")
    st = SQLiteStore(cfg.storage_path)
    try:
        if action == "inspect":
            row = _store.get_plan(st, args.plan)
            if not row:
                print(f"unknown plan: {args.plan} (generate one with "
                      "ripplet test-plan first)", file=sys.stderr)
                return 1
            plan = row.get("plan", {})
            if args.json:
                print(_json.dumps(plan, indent=2, default=str))
            else:
                from .testplan import plan as _plan
                print(_plan.to_markdown(plan))
            return 0
        if action == "execute":
            from .testrun.models import ExecutionRejected
            row = _store.get_plan(st, args.plan)
            if not row:
                print(f"unknown plan: {args.plan} (generate one with "
                      "ripplet test-plan first)", file=sys.stderr)
                return 1
            try:
                req = _tr.build_request(
                    st, cfg, row, tests_subset=getattr(args, "tests", None),
                    runner=getattr(args, "runner", None) or "pytest",
                    timeout_s=args.timeout, max_output_bytes=args.max_output,
                    max_tests=args.max_tests,
                    coverage=bool(getattr(args, "coverage", False)),
                    python_exe=getattr(args, "python", None) or "")
            except ExecutionRejected as e:
                print(f"rejected: {e}", file=sys.stderr)
                return 2
            print(_tr.preview_request(req))
            fresh = _tr.check_freshness(req)
            if not fresh["ok"] and not getattr(args, "reapprove", False):
                print("source tree changed after the plan was approved: " +
                      ", ".join(fresh["changed"][:10]), file=sys.stderr)
                print("regenerate the plan or re-run with --reapprove "
                      "(recorded in the run)", file=sys.stderr)
                return 2
            if not getattr(args, "yes", False):
                try:
                    import sys as _s
                    if not _s.stdin.isatty():
                        print("refusing to execute without confirmation: stdin is not "
                              "a terminal; pass --yes for explicit non-interactive "
                              "authorization", file=sys.stderr)
                        return 2
                    ans = input("execute these tests? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print("cancelled", file=sys.stderr)
                    return 1
                if ans not in ("y", "yes"):
                    print("cancelled", file=sys.stderr)
                    return 1
                method = "cli-confirm"
            else:
                method = "cli-yes-flag"
            _tr.authorize(req, method, reapproval=bool(getattr(args, "reapprove", False)))
            try:
                rec = _tr.execute(req, st, cfg)
            except ExecutionRejected as e:
                print(f"rejected: {e}", file=sys.stderr)
                return 2
            out = rec.to_dict()
            if args.json:
                print(_json.dumps(out, indent=2, default=str))
            else:
                print(f"run {rec.id} [{rec.outcome}] exit={rec.exit_code} "
                      f"collected={rec.collected} passed={rec.passed} "
                      f"failed={rec.failed} skipped={rec.skipped} "
                      f"unreported={rec.unreported} in {rec.duration_ms}ms")
                if rec.coverage_run_id:
                    print(f"coverage: {rec.coverage_run_id} ({rec.coverage_attribution})")
                for w in rec.warnings[:10]:
                    print(f"warn: {w}")
                if rec.error:
                    print(f"error: {rec.error}")
            return 0 if rec.outcome == "passed" else 3
        if action == "status":
            row = _store.get_run(st, args.run_id)
            if not row:
                print(f"unknown run: {args.run_id}", file=sys.stderr)
                return 1
            row.pop("per_test", None)
            print(_json.dumps(row, indent=2, default=str) if args.json else
                  f"run {row['id']} [{row['outcome']}] exit={row['exit_code']} "
                  f"collected={row.get('collected', 0)} passed={row.get('passed', 0)} "
                  f"failed={row.get('failed', 0)} skipped={row.get('skipped', 0)} "
                  f"unreported={row.get('unreported', 0)} plan={row.get('plan_id', '')}")
            return 0
        if action == "results":
            row = _store.get_run(st, args.run_id,
                                 with_log_excerpt=int(getattr(args, "excerpt", 0) or 0))
            if not row:
                print(f"unknown run: {args.run_id}", file=sys.stderr)
                return 1
            lim = int(getattr(args, "limit", 50) or 50)
            if args.json:
                row["per_test"] = (row.get("per_test", []) or [])[:lim]
                print(_json.dumps(row, indent=2, default=str))
            else:
                for p in (row.get("per_test", []) or [])[:lim]:
                    print(f"{p.get('outcome', '?'):8} {p.get('node', '')}")
                n = len(row.get("per_test", []) or [])
                if n > lim:
                    print(f"... and {n - lim} more (see --limit)")
                if row.get("log_excerpt"):
                    print(f"--- log excerpt ({'truncated' if row.get('log_truncated') else 'full'}) ---")
                    print(row["log_excerpt"])
            return 0
        if action == "compare":
            from .testrun import compare as _cmp
            from .testrun import service as _trs
            prow = _store.get_plan(st, args.plan)
            rrow = _store.get_run(st, args.run)
            if not prow:
                print(f"unknown plan: {args.plan}", file=sys.stderr)
                return 1
            if not rrow:
                print(f"unknown run: {args.run}", file=sys.stderr)
                return 1
            cur = None
            try:
                rels = list((rrow.get("source_fingerprint", {}) or {}).keys())
                root = str(Path(cfg.workspace_roots[0]).resolve())
                cur = _trs.fingerprint_files(root, rels)
            except Exception:
                cur = None
            rep = _cmp.compare_plan_run(prow.get("plan", {}), rrow, cur)
            print(_json.dumps(rep, indent=2, default=str) if args.json else
                  _compare_text(rep))
            return 0
        if action == "history":
            rows = _store.list_runs(st, limit=int(getattr(args, "limit", 20) or 20))
            if args.json:
                print(_json.dumps(rows, indent=2, default=str))
            else:
                for r in rows:
                    print(f"{r['id']} [{r['outcome']}] plan={r.get('plan_id', '')} "
                          f"+{r.get('passed', 0)}/-{r.get('failed', 0)}/~{r.get('skipped', 0)} "
                          f"{r.get('duration_ms', 0)}ms")
                if not rows:
                    print("(no runs)")
            return 0
        if action == "signals":
            from .testrun import compare as _cmp
            want_req = bool(getattr(args, "include_coverage_requesters", False))
            rep = _cmp.run_signals(st, limit=int(getattr(args, "limit", 200) or 200),
                                   include_coverage_requesters=want_req)
            print(_json.dumps(rep, indent=2, default=str) if args.json else
                  _signals_text(rep))
            return 0
        print("usage: ripplet test-run {inspect|execute|status|results|compare|history|signals} ...",
              file=sys.stderr)
        return 2
    finally:
        try: st.close()
        except Exception: pass
def _compare_text(rep: dict) -> str:
    L = [f"plan {rep.get('plan_id', '')} vs run {rep.get('run_id', '')} "
         f"[{rep.get('outcome', '')}/{rep.get('parse_status', '')}]",
         f"planned={len(rep.get('planned_selectors', []))} "
         f"executed={rep.get('executed_count', 0)} "
         f"passed={len(rep.get('passed', []))} failed={len(rep.get('failed', []))} "
         f"skipped={len(rep.get('skipped', []))}",
         f"selection_covered={rep.get('selection_covered')}"]
    if rep.get("skipped_planned"):
        L.append("not executed:")
        L += [f"  - {s}" for s in rep["skipped_planned"][:20]]
    if rep.get("missing_from_collection"):
        L.append("missing from runner collection:")
        L += [f"  - {s}" for s in rep["missing_from_collection"][:20]]
    if rep.get("source_drift_since_approval"):
        L.append("source changed since approval:")
        L += [f"  - {s}" for s in rep["source_drift_since_approval"][:20]]
    if rep.get("coverage_run_id"):
        L.append(f"coverage: {rep['coverage_run_id']} ({rep.get('coverage_attribution', '')})")
    if rep.get("uninterpretable"):
        L.append("uninterpretable:")
        L += [f"  - {s}" for s in rep["uninterpretable"]]
    L.append("notes:")
    L += [f"  - {s}" for s in rep.get("notes", [])]
    return "\n".join(L) + "\n"
def _signals_text(rep: dict) -> str:
    L = [f"runs considered: {rep.get('runs_considered', 0)}",
         f"outcomes: {rep.get('outcomes', {})}",
         f"plan/executed gaps: {rep.get('plan_executed_gaps', 0)}",
         f"coverage requested but missing: {rep.get('coverage_requested_but_missing', 0)}"]
    if rep.get("coverage_missing_run_ids") is not None:
        L.append("coverage missing runs:")
        L += [f"  - {r}" for r in rep["coverage_missing_run_ids"][:50]]
        if not rep["coverage_missing_run_ids"]:
            L.append("  (none)")
    if rep.get("repeated_failures"):
        L.append("repeated failures:")
        L += [f"  - {r['node']}: {r['failures']} failures" for r in rep["repeated_failures"]]
    L.append(f"note: {rep.get('note', '')}")
    return "\n".join(L) + "\n"
def cmd_doctor(args):
    import json as _json
    from .workspace_scope import diagnose_scope
    cfg = _cfg(args)
    rep = diagnose_scope(cfg)
    if args.json:
        print(_json.dumps(rep, indent=2, default=str))
    else:
        print(f"cwd: {rep['cwd']}")
        print(f"config roots: {rep['configured_roots']}")
        print(f"resolved roots: {rep['resolved_roots']}")
        print(f"storage: {rep['storage_resolved'] or rep['storage_configured']}")
        print(f"database identity: {(rep['db_identity'] or {}).get('roots', '(none recorded)')}")
        print(f"database repositories: {rep['db_content_repos'] or '(empty)'}")
        print(f"verdict: {rep['verdict']}")
        for p in rep["problems"]:
            print(f"  problem: {p}")
    return 0 if rep["verdict"] == "ok" else 3
def cmd_workspace(args):
    import json as _json
    from .storage.sqlite_store import SQLiteStore
    from .workspace_scope import repo_id_for, read_db_workspace
    from pathlib import Path as _P
    cfg = _cfg(args)
    action = getattr(args, "ws_action", "")
    if action != "forget-repo":
        print("usage: ripplet workspace forget-repo --repo <repo-id> --confirm",
              file=sys.stderr)
        return 2
    rid = (args.repo or "").strip()
    if not rid.startswith("repo-") or len(rid) != 15:
        print(f"refusing: {rid!r} is not a repository id (expected 'repo-' + 10 hex chars)",
              file=sys.stderr)
        return 2
    if not getattr(args, "confirm", False):
        print("refusing: pass --confirm to deliberately delete one repository's "
              "facts. Run 'ripplet doctor' first to inspect.", file=sys.stderr)
        return 2
    from .storage.lock import IndexLock, LockError, lock_path_for
    try:
        lock = IndexLock(lock_path_for(cfg.storage_path),
                         timeout_s=getattr(cfg, "lock_timeout_s", 30.0))
        lock.acquire()
    except LockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    st = SQLiteStore(cfg.storage_path)
    try:
        from . import service as _svc
        out = _svc.forget_repository(st, rid, project_id=cfg.project_id)
        st.commit()
        # Rebind identity to what remains (deliberate, explicit change).
        from .workspace_scope import record_scope
        remaining = sorted({r.split(":", 1)[0]
                            for r in st.list_file_keys() if ":" in r})
        # map remaining repo ids back to roots via the old identity record
        ident = read_db_workspace(cfg.storage_path) or {}
        by_id = {}
        for r in (ident.get("roots") or []):
            try:
                by_id[repo_id_for(r)] = r
            except Exception:
                pass
        roots = sorted(by_id[i] for i in remaining if i in by_id)
        if roots:
            record_scope(st, roots)
            st.commit()
        else:
            st.cx.execute("DELETE FROM meta WHERE k='workspace_roots' OR k='workspace_identity_v'")
            st.commit()
        print(_json.dumps(out, indent=2, default=str) if args.json else
              f"forgot {out['repo_id']}: removed {out['files']} file records, "
              f"{out['nodes_removed']} nodes, {out['edges_removed']} edges, "
              f"{out['coverage_runs_removed']} coverage runs, "
              f"{out['plans_removed']} plans, {out['test_runs_removed']} test runs "
              f"({out['shared_nodes_kept']} shared nodes kept)")
        return 0
    except ValueError as e:
        try: st.rollback()
        except Exception: pass
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        try: st.close()
        except Exception: pass
        try: lock.release()
        except Exception: pass
def _cov_store(args):
    from .storage.sqlite_store import SQLiteStore
    cfg = _cfg(args)
    st = SQLiteStore(cfg.storage_path)
    roots = cfg.workspace_roots or ["."]
    return cfg, st, str(Path(roots[0]).resolve())
def cmd_coverage(args):
    import json as _json
    from .coverage import service as _cov, analysis as _an, store as _store
    action = (args.action or "").lower()
    if action == "import":
        if not args.target:
            print("usage: ripplet coverage import <report>", file=sys.stderr)
            return 2
        cfg, st, root = _cov_store(args)
        try:
            out = _cov.import_report(st, args.target, root, label=args.label or "",
                                     fmt=args.format or "",
                                     revision=args.revision or "",
                                     confirm_current=bool(args.confirm_current),
                                     project_id=cfg.project_id, max_bytes=args.max_bytes)
            st.close()
        except Exception as e:
            try: st.close()
            except Exception: pass
            print(f"import failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(_json.dumps(out, indent=2, default=str) if args.json else
              f"run {out['run_id']} [{out['status']}/{out['revision_status']}] "
              f"files={out['files']} lines={out['lines']} branches={out['branches']} "
              f"mapped={out['mapping']} warnings={len(out['warnings'])}")
        return 0
    cfg, st, _root = _cov_store(args)
    try:
        if action in ("runs", "rubs"):
            rows = _store.list_runs(st)
            print(_json.dumps(rows, indent=2, default=str) if args.json else
                  "\n".join(f"{r['id']} {r['tool']}/{r['format']} {r['label']} "
                            f"{r['revision_status']} {r['status']}" for r in rows) or "(no runs)")
        elif action == "summary":
            if not args.target:
                print("usage: ripplet coverage summary <run_id>", file=sys.stderr)
                return 2
            d = _an.summary(st, args.target)
            print(_json.dumps(d, indent=2, default=str) if args.json else
                  f"run {d['run_id']} [{d['revision_status']}] lines={d['lines']} "
                  f"funcs={d['functions']} branches={d['branches']}")
        elif action == "uncovered":
            if not args.target:
                print("usage: ripplet coverage uncovered <run_id> [--scope path]",
                      file=sys.stderr)
                return 2
            rows = _an.uncovered_symbols(st, args.target, scope=args.scope or "",
                                         limit=args.limit)
            print(_json.dumps(rows, indent=2, default=str) if args.json else
                  "\n".join(f"{r['symbol']} [{r['file']}]" for r in rows) or "(none uncovered)")
        elif action == "symbol":
            if not args.target:
                print("usage: ripplet coverage symbol <qualified-name> [--scope run]",
                      file=sys.stderr)
                return 2
            cands = st.search_symbols(args.target, 5, partial=False) or \
                st.search_symbols(args.target, 5, partial=True)
            if not cands:
                print(f"unknown symbol: {args.target}", file=sys.stderr)
                return 1
            d = _an.symbol_coverage(st, cands[0].id, args.scope or "")
            print(_json.dumps(d, indent=2, default=str))
        elif action == "compare":
            base, head = args.base or "", args.head or args.target or ""
            if not base or not head:
                print("usage: ripplet coverage compare --base <id> --head <id>",
                      file=sys.stderr)
                return 2
            print(_json.dumps(_an.compare(st, base, head), indent=2, default=str))
        elif action == "export":
            if not args.target:
                print("usage: ripplet coverage export <run_id>", file=sys.stderr)
                return 2
            run = _store.get_run(st, args.target)
            if run is None:
                print(f"unknown run: {args.target}", file=sys.stderr)
                return 1
            out = {"run": run,
                   "files": [dict(r) for r in st.cx.execute(
                       "SELECT * FROM coverage_files WHERE run_id=?", (args.target,)).fetchall()],
                   "summary": _an.summary(st, args.target)}
            print(_json.dumps(out, indent=2, default=str))
        else:
            print("usage: ripplet coverage {import|runs|summary|uncovered|symbol|compare|export} ...",
                  file=sys.stderr)
            return 2
        return 0
    finally:
        try: st.close()
        except Exception: pass
def cmd_watch(args):
    import threading
    from .watch import run_watch, WatchSettings
    from .discovery import watch as wev
    from .service import get_status
    from .extraction.code import tree_sitter as tsa
    try:
        wev.require_available()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    from .workspace_scope import WorkspaceMismatchError
    cfg = _cli_index_scope(_cfg(args), args)
    if cfg is None: return 2
    from .workspace_scope import check_scope, WorkspaceMismatchError
    try:
        check_scope(roots=cfg.workspace_roots, storage_path=cfg.storage_path,
                    intent="watch")
    except WorkspaceMismatchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    settings = WatchSettings(
        debounce_s=float(getattr(args, "debounce", 1.0) or 1.0),
        reconcile_s=float(getattr(args, "reconcile_interval", 30.0) or 30.0),
        initial_index=not bool(getattr(args, "no_initial", False)),
        quiet=bool(getattr(args, "quiet", False)))
    stop = threading.Event()
    if not settings.quiet:
        caps = tsa.capabilities()
        deep = sorted(l for l, v in caps.get("languages", {}).items() if v.get("initialized"))
        print(f"watching {cfg.workspace_roots} (deep: {', '.join(deep) or 'none'}; "
              f"debounce={settings.debounce_s}s reconcile={settings.reconcile_s}s)")
    def on_batch(rep):
        if args.quiet:
            return
        if getattr(args, "json", False):
            print(json.dumps({"kind": rep.kind, "files_hinted": rep.files_hinted,
                              "summary": rep.summary, "error": rep.error,
                              "validate_ok": rep.validate_ok,
                              "seconds": rep.seconds}, default=str))
        elif rep.error:
            print(f"[{rep.kind}] FAILED: {rep.error}")
        else:
            s = rep.summary or {}
            print(f"[{rep.kind}] files={s.get('files', '?')} parsed={s.get('parsed', '?')} "
                  f"reused={s.get('reused', '?')} +{s.get('nodes', '?')}n/"
                  f"{s.get('edges', '?')}e -{s.get('nodes_removed', 0)}n/"
                  f"{s.get('edges_removed', 0)}e ({s.get('seconds', '?')}s)")
    try:
        summary = run_watch(cfg, settings, on_batch=on_batch, stop_event=stop)
    except KeyboardInterrupt:
        summary = {"batches": 0, "interrupted": True}
    if not settings.quiet:
        try:
            st = get_status(cfg)
            print(f"stopped. batches={summary.get('batches', 0)} "
                  f"failures={summary.get('failures', 0)} "
                  f"state={st.get('watch', {}).get('state', '?')}")
        except Exception:
            pass
    return 1 if summary.get("failures") else 0
def cmd_embed(args):
    from .service import embed_workspace
    cfg = _cfg(args)
    if getattr(args, "model", None): cfg.embedding_model = args.model
    if getattr(args, "endpoint", None): cfg.embedding_endpoint = args.endpoint
    def prog(done, total):
        if not args.quiet: print(f"  embedded {done}/{total}")
    s = embed_workspace(cfg, progress=None if args.quiet else prog)
    print(json.dumps(s, indent=2, default=str))
    return 0
def cmd_benchmark(args):
    import shutil
    import tempfile
    from .config import RippletConfig
    from .service import index_workspace
    from .storage.sqlite_store import SQLiteStore
    from .evaluation.retrieval_bench import (
        load_dataset, evaluate_query, summarize, markdown_report, STRATEGIES,
        filter_split, paired_bootstrap)
    repo = Path(__file__).resolve().parents[2]
    fix = repo / "tests" / "fixtures"
    ds_path = Path(args.dataset) if getattr(args, "dataset", None) else \
        repo / "tests" / "evaluation" / "retrieval_benchmark_v2.json"
    ds = load_dataset(ds_path)
    split = getattr(args, "split", "all") or "all"
    queries = filter_split(ds["queries"], split)
    strategies = (args.strategies.split(",") if getattr(args, "strategies", None)
                  else STRATEGIES)
    limit = int(getattr(args, "limit", 10) or 10)
    embed_model = getattr(args, "embed_model", None)
    tmp = Path(tempfile.mkdtemp(prefix="ripplet-bench-"))
    stores = {}
    try:
        for scope in sorted({q["scope"] for q in ds["queries"]}):
            src = fix / scope
            if not src.exists():
                print(f"skip {scope}: fixture missing", file=sys.stderr)
                continue
            work = tmp / scope
            shutil.copytree(src, work)
            cfg = RippletConfig([str(work)], str(tmp / f"{scope}.db"), scope)
            index_workspace(cfg)
            if embed_model:
                from .service import embed_workspace
                cfg.embedding_model = embed_model
                try:
                    embed_workspace(cfg)
                except Exception as e:
                    print(f"embed {scope} unavailable: {e}", file=sys.stderr)
            stores[scope] = (SQLiteStore(str(tmp / f"{scope}.db")), cfg)
        provider = None
        if embed_model and any(n in strategies for n in
                               ("semantic", "lexical_semantic", "hybrid_full")):
            try:
                from .retrieval.embeddings import OllamaEmbeddingProvider, EmbeddingConfig
                from .evaluation.retrieval_bench import MemoProvider
                provider = MemoProvider(OllamaEmbeddingProvider(EmbeddingConfig(
                    model=embed_model)))
            except Exception as e:
                print(f"semantic provider unavailable: {e}", file=sys.stderr)
                provider = None
        results = []
        for q in queries:
            if q["scope"] not in stores:
                continue
            st, cfg = stores[q["scope"]]
            results.append(evaluate_query(st, cfg, q, strategies, limit,
                                          provider=provider))
        index_info = {}
        for scope, (st, _cfg) in stores.items():
            try:
                n, e = st.count()
                import os as _os
                index_info[scope] = {"nodes": n, "edges": e,
                                     "db_kb": round(_os.path.getsize(st.path) / 1024, 1)}
            except Exception:
                pass
        if provider is not None:
            summary_extra = {"query_vector_cache": {"hits": provider.hits,
                                                    "misses": provider.misses}}
        else:
            summary_extra = {}
        summary = summarize(results, strategies, index_info or None)
        summary.update(summary_extra)
        if "hybrid_rerank" in strategies and "hybrid_full" in strategies:
            summary["gate"] = {
                "hybrid_full_vs_hybrid_rerank_mrr":
                    paired_bootstrap(results, "hybrid_rerank", "hybrid_full", "mrr"),
                "note": "paired bootstrap (B=2000) on identical queries; "
                        "significant=false means no claim either way"}
        summary["split"] = split
        cal_path = getattr(args, "calibration_out", None)
        if cal_path:
            rows = [{"qid": r["qid"], **r["calibration"]} for r in results
                    if "calibration" in r]
            Path(cal_path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
            print(f"calibration rows: {len(rows)} -> {cal_path}", file=sys.stderr)
        payload = {"dataset": str(ds_path), "version": ds.get("version"),
                   "strategies": strategies, "limit": limit, "split": split,
                   "embed_model": embed_model, "summary": summary, "results": results}
        outdir = Path(args.out) if getattr(args, "out", None) else repo / "docs" / "architecture"
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "retrieval_benchmark.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        (outdir / "RETRIEVAL_BENCHMARK.md").write_text(
            markdown_report(summary, ds.get("version", "?")), encoding="utf-8")
        print(markdown_report(summary, ds.get("version", "?")))
        return 0
    finally:
        for st, _ in stores.values():
            try: st.close()
            except Exception: pass
def cmd_benchmark_selection(args):
    import tempfile
    from .evaluation import selection_bench as _sb
    repo = Path(__file__).resolve().parents[2]
    ds_path = Path(args.dataset) if getattr(args, "dataset", None) else \
        repo / "tests" / "evaluation" / "test_selection_benchmark_v2.json"
    modes = tuple(s.strip() for s in (getattr(args, "modes", "") or "").split(",") if s.strip()) or \
        ("conservative", "balanced", "broad")
    split = getattr(args, "split", "all") or "all"
    tmp = Path(tempfile.mkdtemp(prefix="ripplet-sel-bench-"))
    result = _sb.evaluate(ds_path, tmp, modes=modes, split=split)
    outdir = Path(args.out) if getattr(args, "out", None) else repo / "docs" / "architecture"
    outdir.mkdir(parents=True, exist_ok=True)
    ver = str(result.get("version", "selection-?"))
    tag = {"selection-v1": "V1", "selection-v2": "V2"}.get(ver, "CUSTOM")
    # The committed v1 history files (TEST_SELECTION_BENCHMARK.{json,md})
    # are never overwritten here; reruns land on versioned names.
    base = f"TEST_SELECTION_BENCHMARK_{tag}"
    (outdir / f"{base}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (outdir / f"{base}.md").write_text(_sb.markdown_report(result), encoding="utf-8")
    print(_sb.markdown_report(result))
    return 0
def build():
    p=argparse.ArgumentParser("ripplet", description="Ripplet evidence-driven code intelligence")
    p.add_argument("--config"); p.add_argument("--storage"); p.add_argument("--json",action="store_true"); p.add_argument("--quiet",action="store_true"); p.add_argument("--version",action="store_true")
    sub=p.add_subparsers(dest="cmd", required=False)
    def _common(sp):
        import argparse as _ap
        sp.add_argument("--json",action="store_true"); sp.add_argument("--quiet",action="store_true")
        sp.add_argument("--config", default=_ap.SUPPRESS); sp.add_argument("--storage", default=_ap.SUPPRESS)
        return sp
    a=_common(sub.add_parser("init")); a.add_argument("path",nargs="?"); a.add_argument("--force",action="store_true"); a.add_argument("--no-rules",action="store_true",help="do not write agent rules/hooks (CLAUDE.md, AGENTS.md, plugins)"); a.add_argument("--link",action="append",metavar="PATH",help="index another repository into the same graph (repeatable)"); a.set_defaults(fn=cmd_init)
    a=_common(sub.add_parser("hooks")); a.add_argument("action",choices=["on","off","stats"]); a.add_argument("path",nargs="?"); a.set_defaults(fn=cmd_hooks)
    a=_common(sub.add_parser("index")); a.add_argument("path",nargs="?"); a.add_argument("--limit",type=int,default=50); a.set_defaults(fn=cmd_index)
    a=_common(sub.add_parser("update")); a.add_argument("path",nargs="?"); a.add_argument("--limit",type=int,default=50); a.set_defaults(fn=cmd_update)
    a=_common(sub.add_parser("status")); a.set_defaults(fn=cmd_status)
    a=_common(sub.add_parser("search")); a.add_argument("term"); a.add_argument("--limit",type=int,default=20); a.set_defaults(fn=cmd_search)
    a=_common(sub.add_parser("query")); a.add_argument("question"); a.add_argument("--limit",type=int,default=20); a.add_argument("--mode",default="hybrid"); a.add_argument("--format",default=""); a.set_defaults(fn=cmd_query)
    a=_common(sub.add_parser("explain")); a.add_argument("symbol"); a.add_argument("--limit",type=int,default=20); a.set_defaults(fn=cmd_explain)
    a=_common(sub.add_parser("path")); a.add_argument("source"); a.add_argument("target"); a.add_argument("--depth",type=int,default=6); a.set_defaults(fn=cmd_path)
    a=_common(sub.add_parser("impact")); a.add_argument("--diff"); a.add_argument("--symbol"); a.add_argument("--depth",type=int,default=4); a.set_defaults(fn=cmd_impact)
    a=_common(sub.add_parser("history")); a.add_argument("--since",default=""); a.add_argument("--limit",type=int,default=20); a.set_defaults(fn=cmd_history)
    a=_common(sub.add_parser("validate")); a.set_defaults(fn=cmd_validate)
    a=_common(sub.add_parser("doctor")); a.set_defaults(fn=cmd_doctor)
    _w=_common(sub.add_parser("workspace")); _w_sub=_w.add_subparsers(dest="ws_action", required=False)
    _f=_common(_w_sub.add_parser("forget-repo")); _f.add_argument("--repo", required=True); _f.add_argument("--confirm", action="store_true"); _f.set_defaults(fn=cmd_workspace)
    a=_common(sub.add_parser("diagnostics")); a.set_defaults(fn=cmd_diagnostics)
    a=_common(sub.add_parser("parsers")); a.set_defaults(fn=cmd_parsers)
    a=_common(sub.add_parser("export")); a.add_argument("--format",default="json"); a.add_argument("--out",default="ripplet-export.json"); a.set_defaults(fn=cmd_export)
    a=_common(sub.add_parser("mcp")); a.set_defaults(fn=cmd_mcp)
    a=_common(sub.add_parser("guard", help="run check_change automatically when the agent finishes")); a.add_argument("action",choices=["on","off","status"]); a.add_argument("path",nargs="?"); a.add_argument("--target",choices=["claude","opencode","both"],default="both"); a.set_defaults(fn=cmd_guard)
    a=_common(sub.add_parser("setup", help="make a repo agent-ready: init, index, MCP bindings, rules, guard")); a.add_argument("path",nargs="?"); a.add_argument("--no-rules",action="store_true"); a.add_argument("--no-nudge",action="store_true"); a.add_argument("--no-guard",action="store_true"); a.set_defaults(fn=cmd_setup)
    a=_common(sub.add_parser("rules", help="Ripplet guidance for every coding agent: always-on rules + hooks/plugins (--strict to enforce)")); a.add_argument("action",choices=["on","off","status"]); a.add_argument("path",nargs="?"); a.add_argument("--target",default="auto",help="auto (CLAUDE.md+AGENTS.md and every agent the repo already uses), all, or a comma list of: claude,agents,gemini,copilot,cursor,kiro,windsurf,antigravity,cline,codebuddy,opencode"); a.add_argument("--strict",action="store_true",help="block the first raw read/search of source per session until a Ripplet tool is used (Claude Code, Codebuddy, OpenCode)"); a.add_argument("--no-nudge",action="store_true",help="rules text only, no hooks/plugins"); a.set_defaults(fn=cmd_rules); a.add_argument("--prefetch",action="store_true",help="Claude Code: add the task's Ripplet context to each request before the agent starts (UserPromptSubmit hook; RIPPLET_PREFETCH=0 switches it off)")
    a=_common(sub.add_parser("tool", help="call an MCP tool from the shell: tool <name> key=value ...")); a.add_argument("name"); a.add_argument("kv",nargs="*"); a.set_defaults(fn=cmd_tool)
    a=_common(sub.add_parser("review", help="review a branch/PR against its base: markdown, SARIF or JSON; CI exit status")); a.add_argument("path",nargs="?"); a.add_argument("--base",required=True,help="base ref, e.g. origin/main"); a.add_argument("--format",choices=["markdown","sarif","json"],default="markdown"); a.add_argument("--out",default=""); a.add_argument("--fail-on",choices=["error","warning","note","never"],default="error"); a.add_argument("--depth",type=int,default=2); a.add_argument("--task",default="",help="the PR's task in words: notes changed files it does not lead to"); a.set_defaults(fn=cmd_review)
    a=_common(sub.add_parser("check", help="after an edit: missed places, stale callers, tests to run")); a.add_argument("path",nargs="?"); a.add_argument("--base",default=""); a.add_argument("--depth",type=int,default=2); a.add_argument("--no-reindex",action="store_true"); a.add_argument("--run-tests",action="store_true",help="run the tests that reach the change and explain failures"); a.add_argument("--test-timeout",type=float,default=300.0); a.add_argument("--task",default="",help="the task in words: flags changed files it does not lead to"); a.set_defaults(fn=cmd_check)
    a=_common(sub.add_parser("arch", help="check architecture rules (ripplet.arch.yaml): must_call / forbid; CI exit status")); a.add_argument("path",nargs="?"); a.add_argument("--example",action="store_true",help="print an example rules file"); a.set_defaults(fn=cmd_arch)
    a=_common(sub.add_parser("ui", help="open the interactive graph explorer in the browser")); a.add_argument("path",nargs="?"); a.add_argument("--port",type=int,default=7420); a.add_argument("--host",default="127.0.0.1"); a.add_argument("--no-browser",action="store_true"); a.set_defaults(fn=cmd_ui)
    b=_common(sub.add_parser("benchmark")); b.add_argument("--dataset"); b.add_argument("--out"); b.add_argument("--limit",type=int,default=10); b.add_argument("--strategies"); b.add_argument("--embed-model"); b.add_argument("--split",default="all"); b.add_argument("--calibration-out"); b.set_defaults(fn=cmd_benchmark)
    w=_common(sub.add_parser("watch")); w.add_argument("path",nargs="?"); w.add_argument("--debounce",type=float,default=1.0); w.add_argument("--reconcile-interval",type=float,default=30.0); w.add_argument("--no-initial",action="store_true"); w.add_argument("-v","--verbose",action="store_true"); w.set_defaults(fn=cmd_watch)
    e=_common(sub.add_parser("embed")); e.add_argument("--model"); e.add_argument("--endpoint"); e.set_defaults(fn=cmd_embed)
    t=_common(sub.add_parser("test-plan")); t.add_argument("--diff"); t.add_argument("--working-tree", action="store_true"); t.add_argument("--files", nargs="*"); t.add_argument("--symbol", action="append", default=[]); t.add_argument("--mode", default="balanced"); t.add_argument("--format", default="text"); t.add_argument("--limit", type=int, default=50); t.set_defaults(fn=cmd_test_plan)
    tr=_common(sub.add_parser("test-run")); tr_sub=tr.add_subparsers(dest="run_action", required=False)
    def _tr_common(sp):
        sp.add_argument("--json",action="store_true"); sp.add_argument("--quiet",action="store_true")
        return sp
    _a=_tr_common(tr_sub.add_parser("inspect")); _a.add_argument("--plan", required=True); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("execute")); _a.add_argument("--plan", required=True); _a.add_argument("--tests", nargs="*", default=None); _a.add_argument("--runner", default="pytest"); _a.add_argument("--timeout", type=float, default=None); _a.add_argument("--max-output", type=int, default=None); _a.add_argument("--max-tests", type=int, default=None); _a.add_argument("--coverage", action="store_true"); _a.add_argument("--yes", action="store_true"); _a.add_argument("--reapprove", action="store_true"); _a.add_argument("--python", default=None); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("status")); _a.add_argument("run_id"); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("results")); _a.add_argument("run_id"); _a.add_argument("--limit", type=int, default=50); _a.add_argument("--excerpt", type=int, default=0); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("compare")); _a.add_argument("--plan", required=True); _a.add_argument("--run", required=True); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("history")); _a.add_argument("--limit", type=int, default=20); _a.set_defaults(fn=cmd_test_run)
    _a=_tr_common(tr_sub.add_parser("signals")); _a.add_argument("--limit", type=int, default=200); _a.add_argument("--include-coverage-requesters", action="store_true"); _a.set_defaults(fn=cmd_test_run)
    bs=_common(sub.add_parser("benchmark-selection")); bs.add_argument("--dataset"); bs.add_argument("--out"); bs.add_argument("--split", default="all"); bs.add_argument("--modes", default="conservative,balanced,broad"); bs.set_defaults(fn=cmd_benchmark_selection)
    c=_common(sub.add_parser("coverage")); c.add_argument("action", nargs="?",
        help="import|rubs|summary|uncovered|symbol|compare|export"); c.add_argument("target", nargs="?",
        help="report path (import), run id (summary/compare/export) or symbol/scope");
    c.add_argument("--format"); c.add_argument("--label", default=""); c.add_argument("--revision", default="");
    c.add_argument("--confirm-current", action="store_true"); c.add_argument("--scope", default="");
    c.add_argument("--base"); c.add_argument("--head"); c.add_argument("--limit", type=int, default=50);
    c.add_argument("--strict", action="store_true"); c.add_argument("--max-bytes", type=int, default=8*1024*1024);
    c.set_defaults(fn=cmd_coverage)
    return p
def main(argv=None):
    import sys as _s
    # Piped/captured output on Windows uses the ANSI code page (cp1254...):
    # never let an unencodable file name or symbol abort a command.
    for _stream in (_s.stdout, _s.stderr):
        try: _stream.reconfigure(errors="backslashreplace")
        except Exception: pass
    ap=build(); args=ap.parse_args(argv)
    if getattr(args,"version",False): print(VERSION); return 0
    if not args.cmd: ap.print_help(); return 0
    try: return args.fn(args)
    except BrokenPipeError: return 0
    except Exception as e:
        import traceback
        if getattr(args,"json",False): print(json.dumps({"error":str(e)}))
        else: print(f"error: {e}", file=_s.stderr)
        if "--debug" in (_s.argv or []): traceback.print_exc()
        return 1
if __name__=="__main__": raise SystemExit(main())
