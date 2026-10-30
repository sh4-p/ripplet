from __future__ import annotations
import re
TOOL_DEFS = [
 {"name":"check_change","description":"Run after editing code (before running the full test suite): re-indexes your changes and reports places that touch the same data but do not use the helper you introduced, callers outside your change still using an old signature or a removed symbol, calls that resolve to nothing, frontend callers of changed endpoints, architecture rules (ripplet.arch.yaml) the change breaks, files changed outside the task, and exactly which tests reach the change with ready-to-run commands.","inputSchema":{"type":"object","properties":{"base":{"type":"string","description":"Compare against this branch/commit instead of the working tree"},"depth":{"type":"integer","default":2},"run_tests":{"type":"boolean","description":"Also run those tests and explain failures (server needs RIPPLET_ALLOW_TEST_RUN=1)"},"task":{"type":"string","description":"The task you are doing, in words: changed files it does not lead to are flagged"}}}},
 {"name":"feature_slice","description":"Build a feature like an existing one: returns the existing feature's files in full plus every touch point elsewhere (registrations, DTO/model fields, mappers, migrations, frontend services/components, tests) and its frontend->backend HTTP links, in one call.","inputSchema":{"type":"object","properties":{"feature":{"type":"string","description":"Name of the existing feature to imitate, e.g. 'favorite'"},"focus":{"type":"string","description":"Only files whose path contains this text (to get files a previous call listed as not shown)"}},"required":["feature"]}},
 {"name":"list_routes","description":"Every HTTP route with its handler and the access checks it makes (guard calls such as requireRole/authorize, or inline role tests). Answers 'which endpoint lets a user do X?' / 'which write routes skip the permission check?' in one call, and proposes a ripplet.arch.yaml rule to keep it enforced.","inputSchema":{"type":"object","properties":{"method":{"type":"string","description":"write (default: POST/PUT/PATCH/DELETE), all, or one verb"},"path":{"type":"string","description":"only routes whose path contains this"},"guards":{"type":"array","items":{"type":"string"},"description":"guard method names to look for (default: require*/authorize/can*/ensure*/...Role/...Permission)"}}}},
 {"name":"search_symbols","description":"Exact/partial symbol lookup with file evidence","inputSchema":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","default":20},"known":{"type":"object","description":"Opt-in cross-call ledger {snapshot, ids}: items already held are replaced by cited refs. Absent = full self-contained response."}} ,"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."},"required":["query"]}},
 {"name":"search_documents","description":"Search docs/claims/config text","inputSchema":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","default":20}},"required":["query"]}},
 {"name":"query_graph","description":"Natural-language evidence-grounded query (scope=disambiguate|deps|path|impact|tests|explore returns a task-aware projection with totals, truncation markers, and continuation instead of the full dump)","inputSchema":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","default":20},"scope":{"type":"string","description":"Opt-in task scope (Phase 22). Absent/unknown = legacy response."},"known":{"type":"object","description":"Opt-in cross-call ledger {snapshot, ids}: facts already held are replaced by cited refs. Absent = full self-contained response."},"format":{"type":"string","description":"Opt-in presentation: 'fact_pack_v1' for the compact equivalent. Absent/unknown = legacy response."},"evidence_mode":{"type":"string","description":"Opt-in evidence presentation: 'spans' returns exact symbol-range spans with quotes recoverable via expand_evidence. Absent/unknown = full indexed quotes."}},"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."},"required":["query"]}},
 {"name":"expand_evidence","description":"Read-only source expansion by canonical identity (node_id or evidence_id). Levels: span, context, symbol, file. Revision-verified; stale or missing sources are reported honestly, never fabricated.","inputSchema":{"type":"object","properties":{"node_id":{"type":"string"},"evidence_id":{"type":"string"},"level":{"type":"string","default":"span"},"context_lines":{"type":"integer","default":10}} }},
 {"name":"get_node","description":"Get node by id","inputSchema":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}},
 {"name":"get_neighbors","description":"Neighbors with relation filter (offset/include_total paginate without silent truncation; absent = legacy list)","inputSchema":{"type":"object","properties":{"id":{"type":"string"},"direction":{"type":"string","default":"both"},"relations":{"type":"array","items":{"type":"string"}},"limit":{"type":"integer","default":50},"offset":{"type":"integer","default":0,"description":"Skip this many neighbor rows."},"include_total":{"type":"boolean","description":"Opt-in envelope {results,total,offset,limit,complete,continuation}."},"known":{"type":"object","description":"Opt-in cross-call ledger {snapshot, ids}: edges already held are replaced by cited refs. Absent = full self-contained response."}},"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."},"required":["id"]}},
 {"name":"find_path","description":"Shortest typed path between symbols","inputSchema":{"type":"object","properties":{"source":{"type":"string"},"target":{"type":"string"},"max_depth":{"type":"integer","default":6}},"required":["source","target"]}},
 {"name":"explain_symbol","description":"Explain symbol with deps and evidence","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"symbols":{"type":"array","items":{"type":"string"}}},"required":[]}},
 {"name":"get_source_evidence","description":"Evidence rows for a node","inputSchema":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}},
 {"name":"get_project_overview","description":"Counts, modules, top degrees","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_module_dependencies","description":"Module-level depends_on graph","inputSchema":{"type":"object","properties":{"module":{"type":"string"}}}},
 {"name":"get_architecture_summary","description":"Alias of repo_map","inputSchema":{"type":"object","properties":{"focus":{"type":"string"}}}},
 {"name":"repo_map","description":"Ranked map of the repository: entry points, and per directory the files and symbols the rest of the code uses most, as one-line signatures with use counts, plus where tests are. Good first call in an unfamiliar repo.","inputSchema":{"type":"object","properties":{"focus":{"type":"string","description":"Only paths containing this text"}}}},
 {"name":"find_orphaned_symbols","description":"Nodes with no edges","inputSchema":{"type":"object","properties":{"limit":{"type":"integer","default":50}}}},
 {"name":"find_cycles","description":"Dependency cycles (SCC)","inputSchema":{"type":"object","properties":{}}},
 {"name":"find_unresolved_relationships","description":"calls_unresolved edges","inputSchema":{"type":"object","properties":{"limit":{"type":"integer","default":50}}}},
 {"name":"compare_subsystems","description":"Compare two modules","inputSchema":{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"string"}},"required":["a","b"]}},
 {"name":"analyze_change_impact","description":"Impact of changed symbols/files","inputSchema":{"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"}},"files":{"type":"array","items":{"type":"string"}},"max_depth":{"type":"integer","default":4},"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."}}}},
 {"name":"find_related_tests","description":"Tests related to symbols (compact file list by default; compact=false returns the legacy bare id list)","inputSchema":{"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"}},"max_depth":{"type":"integer","default":3},"compact":{"type":"boolean","description":"Compact file-list mode (Phase 23 default when absent). Pass false for the legacy bare id list."},"limit":{"type":"integer","default":200,"description":"Compact mode page size (files). Full totals always reported."},"offset":{"type":"integer","default":0,"description":"Compact mode page offset."}},"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."},"required":["symbols"]}},
 {"name":"get_changed_symbols","description":"Diff two snapshots (latest vs previous)","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_changes_since","description":"Snapshot history","inputSchema":{"type":"object","properties":{"limit":{"type":"integer","default":10}}}},
 {"name":"investigate_error","description":"Paste a stack trace, test failure or error log line: every frame in this repo mapped to its function with the code on that line, the code around where it happened, its callers, and where the message is raised in the source.","inputSchema":{"type":"object","properties":{"error":{"type":"string","description":"The trace / error output as printed"}},"required":["error"]}},
 {"name":"trace_dependency_chain","description":"Alias of find_path with evidence","inputSchema":{"type":"object","properties":{"source":{"type":"string"},"target":{"type":"string"}},"required":["source","target"]}},
 {"name":"get_index_status","description":"Index state and counts","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_stale_sources","description":"Files changed since index (mtime vs db)","inputSchema":{"type":"object","properties":{}}},
 {"name":"inspect_provenance","description":"Evidence rows for node/edge","inputSchema":{"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}},
 {"name":"get_conflicts","description":"Conflicting/contradictory claims","inputSchema":{"type":"object","properties":{}}},
 {"name":"validate_graph","description":"Integrity checks","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_query_diagnostics","description":"Last query plan/latency","inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
 {"name":"index_workspace","description":"(guarded) reindex. Requires allow_write.","inputSchema":{"type":"object","properties":{}}},
 {"name":"export_subgraph","description":"Subgraph around seed","inputSchema":{"type":"object","properties":{"seed":{"type":"string"},"depth":{"type":"integer","default":2}}}},
 {"name":"get_coverage_summary","description":"Coverage metrics for a run (denominators included)","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"}},"required":["run_id"]}},
 {"name":"list_coverage_runs","description":"Imported coverage runs with revision status","inputSchema":{"type":"object","properties":{}}},
 {"name":"get_symbol_coverage","description":"Observed coverage evidence for one symbol","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"run_id":{"type":"string"}}}},
 {"name":"find_uncovered_symbols","description":"Symbols with explicit zero-hit observations","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"},"scope":{"type":"string"},"limit":{"type":"integer","default":20}},"required":["run_id"]}},
 {"name":"compare_coverage_runs","description":"Diff two coverage runs (raw observations)","inputSchema":{"type":"object","properties":{"base":{"type":"string"},"head":{"type":"string"}},"required":["base","head"]}},
 {"name":"get_coverage_mapping_diagnostics","description":"Ambiguous/unmapped coverage and freshness","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"}},"required":["run_id"]}},
  {"name":"generate_test_plan","description":"Evidence-driven test plan (read-only, never executes tests)","inputSchema":{"type":"object","properties":{"files":{"type":"array","items":{"type":"string"}},"symbols":{"type":"array","items":{"type":"string"}},"mode":{"type":"string"},"limit":{"type":"integer","default":50},"telemetry":{"type":"boolean","description":"Opt-in envelope {data,telemetry} with latency_ms, response_bytes and retry_hint. Absent keeps the legacy shape."}}}},
  {"name":"get_test_run","description":"Persisted test-run record by id (read-only; bounded log excerpt)","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"},"excerpt":{"type":"integer","default":0}},"required":["run_id"]}},
  {"name":"list_test_runs","description":"Recent persisted test runs, newest first (read-only)","inputSchema":{"type":"object","properties":{"limit":{"type":"integer","default":20}}}},
  {"name":"compare_plan_run","description":"Compare a stored plan with a completed run (read-only, deterministic)","inputSchema":{"type":"object","properties":{"plan_id":{"type":"string"},"run_id":{"type":"string"}},"required":["plan_id","run_id"]}},
   {"name":"get_test_run_signals","description":"Descriptive run-history signals; never a relevance label, never executes (read-only)","inputSchema":{"type":"object","properties":{"limit":{"type":"integer","default":200},"include_coverage_requesters":{"type":"boolean","description":"Opt-in list of run ids behind coverage_requested_but_missing (bounded). Absent keeps the legacy shape."}}}},
   {"name":"rename_symbol","description":"Rename a function/method/class/variable everywhere in one call: the graph edits the declaration (and overrides), every call site and import line that names it, and nothing else (same-named other code is left alone). Returns the lines changed and the remaining text mentions to review. Use instead of reading and editing each file.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string","description":"Name, Class.member or qualified name"},"new_name":{"type":"string"},"dry_run":{"type":"boolean","default":False}},"required":["symbol","new_name"]}},
   {"name":"change_plan","description":"Before renaming, changing the signature of, or removing a symbol: every line to edit (declarations incl. overrides/interface members, each call site), callers that need no edit, same-named code to leave alone, and text mentions.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string","description":"Name, Class.member or qualified name"},"change":{"type":"string","enum":["rename","signature","remove"],"default":"rename"},"new_name":{"type":"string"}},"required":["symbol"]}},
   {"name":"compare_revisions","description":"What changed between two commits (branch, tag or SHA), symbol by symbol from git objects: added, removed, renamed and modified functions/classes/tables per file, with the commits in between.","inputSchema":{"type":"object","properties":{"base":{"type":"string"},"head":{"type":"string","default":"HEAD"},"path":{"type":"string","description":"Only files under this path"}},"required":["base"]}},
   {"name":"task_context","description":"Start of a task: give the task in plain words; returns the code it is about (ranked, with excerpts and real callers), related routes and tables, an existing feature to imitate for 'like X', the files to edit and the tests that reach them.","inputSchema":{"type":"object","properties":{"task":{"type":"string"}},"required":["task"]}},
   {"name":"use_workspace","description":"Bind this session to a project directory (reads <dir>/.ripplet/config.json). Use when get_index_status shows an empty or unexpected index instead of the project you work in.","inputSchema":{"type":"object","properties":{"path":{"type":"string","description":"Project root directory"}},"required":["path"]}},
]
# Experimental "sandbox" profile (RIPPLET_MCP_TOOLS=sandbox): read and edit code without leaving Ripplet, so an
# agent need not open a whole file only to be allowed to edit it. Not part of the default tool list.
SANDBOX_TOOLS = ("read_code", "edit_code", "write_code")
TOOL_DEFS.append(
 {"name":"move_symbol","description":"Move top-level functions/classes (Python), functions/classes/types/enums/consts (TypeScript/JavaScript ES modules) a class/interface/enum/record to another package (Java: to_file is the new Type.java) a C# type to another file (namespace follows the folder convention, usings fixed) a Go func/type/const/var to another file or package (type takes its methods; qualifiers and imports fixed) a PHP class/interface/trait/enum to another file (namespace follows composer psr-4, use statements fixed) a Dart class/mixin/enum/typedef/function/const to another file (imports, exports and prefixes fixed) a Rust fn/struct/enum/union/trait/type/const/static to another module file of the same crate (use paths fixed, impls follow the type) or a Kotlin class/object/interface/fun/val/typealias to another file or package (imports and package fixed); comma separated move together (Python, TypeScript). Cuts it, pastes it with the imports it needs, rewrites `from old import name` / `import {name} from './old'` everywhere, keeps a re-export where something still needs it. One call, all files or none.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"to_file":{"type":"string","description":"workspace path of the module to move it to (created when missing)"},"with_dependencies":{"type":"boolean","description":"also move the functions, classes and constants of the old module that the symbol needs (otherwise a symbol that needs them is refused)"},"dry_run":{"type":"boolean"}},"required":["symbol","to_file"]}})
TOOL_DEFS.append(
 {"name":"change_signature","description":"Add, remove or reorder the parameters of a function/method: edits its declaration (and overrides) and every call site the graph proves, in one call.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"add_param":{"type":"object","properties":{"decl":{"type":"string","description":"the parameter as the declaration spells it, e.g. `retries: Int = 0`"},"value":{"type":"string","description":"what every call site passes; omit where the default covers them"},"position":{"type":"integer","description":"0-based; default last"}},"required":["decl"]},"remove_param":{"type":"string","description":"name of the parameter to drop"},"reorder_params":{"type":"array","items":{"type":"string"},"description":"EVERY parameter name in the new order; call sites are permuted to match"},"dry_run":{"type":"boolean"}},"required":["symbol"]}})
TOOL_DEFS.extend([
 {"name":"read_code","description":"Read code: symbol names and/or file paths in ONE call (a symbol returns its source, a path its numbered lines; start/end pick a range).","inputSchema":{"type":"object","properties":{"targets":{"type":"array","items":{"type":"string"}},"start":{"type":"integer"},"end":{"type":"integer"}},"required":["targets"]}},
 {"name":"edit_code","description":"Edit code by exact replacement: edits=[{path or symbol, old, new, replace_all?}], several at once, all or nothing. `old` must match exactly one place. No prior read needed; returns the changed lines.","inputSchema":{"type":"object","properties":{"edits":{"type":"array","items":{"type":"object","properties":{"path":{"type":"string"},"symbol":{"type":"string"},"old":{"type":"string"},"new":{"type":"string"},"replace_all":{"type":"boolean"}},"required":["old","new"]}}},"required":["edits"]}},
 {"name":"write_code","description":"Create a file (an existing one only with overwrite=true).","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"overwrite":{"type":"boolean"}},"required":["path","content"]}},
])
from .budget import add_budget_param as _add_budget
_add_budget(TOOL_DEFS)
# Default `tools/list` profile. Every TOOL_DEFS entry stays callable by
# name; only the advertised set shrinks, because each advertised schema is
# re-sent on every agent turn (~3k tokens for all 42 in a measured run) and
# large catalogs get deferred behind tool search, so agents never call them.
CORE_TOOLS = ("task_context", "repo_map", "investigate_error", "check_change", "change_plan", "rename_symbol", "change_signature", "move_symbol", "feature_slice", "list_routes", "search_symbols", "explain_symbol", "get_neighbors",
              "analyze_change_impact", "find_related_tests", "find_path",
              "query_graph", "expand_evidence")
_CORE_HINTS = {
    "check_change": "Call after each batch of edits, BEFORE running the whole test suite: what your change missed (same data, no call to your new helper), callers you broke, and the exact tests to run.",
    "task_context": "FIRST call for a task: pass the task text. Returns the code it is about with excerpts and callers, related routes/tables, files to edit, tests to run.",
    "repo_map": "First call in an unfamiliar repo: entry points and the files/symbols the code uses most, ranked, with signatures. Replaces ls/glob/opening files to orient.",
    "investigate_error": "A test failed or something crashed: paste the trace/output. Returns each repo frame's function and code line, the code where it happened, callers, and where the message is thrown.",
    "rename_symbol": "To RENAME a symbol: does the whole rename itself (declarations, overrides, every call site and import), only on proven lines. One call instead of reading and editing every file.",
    "move_symbol": "To MOVE a top-level function/class (Python, TypeScript, JavaScript) a Java type, a C# type, a Go declaration, a PHP class, a Dart declaration, a Rust item or a Kotlin declaration to another module/package/file: cut, paste, needed imports and every `from old import name` in one call; the files are already changed when it returns, do not re-edit them.",
    "change_signature": "To ADD, REMOVE or REORDER (reorder_params=[all names, new order]) parameters: edits the declaration (and overrides) and EVERY call site the graph proves in one call (add_param={decl, value}, remove_param=name). One call instead of one edit per call site; the files are already edited when it returns, so do not Read or Edit those sites again (only the lines it lists as NOT edited).",
    "change_plan": "BEFORE a rename / signature change / removal: every line to edit (overrides and each call site included), same-named code to leave alone. Use instead of grep.",
    "list_routes": "Security/permission audits and 'which endpoint does X': every route, its handler and the access checks it calls, write routes without a check first. Use instead of reading every controller.",
    "feature_slice": "FIRST call when adding a feature similar to an existing one (e.g. 'favorite'): its full files + every touch point across backend and frontend, in one response. Replaces dozens of Read/Grep calls.",
    "search_symbols": "Find a symbol by name: qualified name, type, file. Start here.",
    "get_neighbors": "Callers (direction=in) / callees (out) of a node id, one line each with call site file:line.",
    "analyze_change_impact": "What breaks if these symbols change: direct dependents with call sites + related tests.",
    "explain_symbol": "A symbol's location, source code, callers and callees (with call sites). Use instead of Read+Grep for a known name. Pass every name you need in `symbols` to get them all in ONE call.",
    "query_graph": "Natural-language question about the code: ranked matches + the best match's source, callers and callees.",
}


def listed_tools(env=None) -> list:
    """TOOL_DEFS filtered by RIPPLET_MCP_TOOLS: unset -> core profile,
    `all` -> every tool, comma list -> exactly those (unknown names ignored)."""
    import os
    want = ((env if env is not None else os.environ).get("RIPPLET_MCP_TOOLS") or "").strip()
    if want.lower() == "all":
        return TOOL_DEFS
    sandbox = want.lower() == "sandbox"
    if sandbox:
        names = list(CORE_TOOLS) + list(SANDBOX_TOOLS)
    else:
        names = [w.strip() for w in want.split(",") if w.strip()] if want else list(CORE_TOOLS)
    out = []
    for t in TOOL_DEFS:
        if t["name"] in names:
            if not want or sandbox:
                t = _lean(t)
                if t["name"] in _CORE_HINTS:
                    t = dict(t, description=_CORE_HINTS[t["name"]])
            out.append(t)
    return out


# Opt-in parameters of older phases (ledgers, envelopes, alternate formats).
# Still accepted by name; left out of the default profile because every
# advertised schema is re-sent to the model on each turn.
_OPT_IN_PARAMS = ("known", "telemetry", "format", "evidence_mode", "include_total", "compact",
                  "scope", "include_coverage_requesters", "mode")


def _lean(t: dict) -> dict:
    sch = dict(t.get("inputSchema") or {})
    props = {k: v for k, v in (sch.get("properties") or {}).items() if k not in _OPT_IN_PARAMS}
    sch.pop("telemetry", None)
    sch["properties"] = props
    if "required" in sch:
        sch["required"] = [r for r in sch["required"] if r in props]
    return dict(t, inputSchema=sch)


def get_server_default(store, key: str):
    """Deployment-pinned default for an MCP tool (Phase 23 migration path).

    Reads the `mcp_defaults` table (created lazily); missing table or key
    returns None (caller falls back to the built-in default). Trials pin
    legacy behavior per-database without changing prompts or code."""
    try:
        store.cx.execute(
            "CREATE TABLE IF NOT EXISTS mcp_defaults(key TEXT PRIMARY KEY,"
            " value TEXT)")
        row = store.cx.execute("SELECT value FROM mcp_defaults WHERE key=?",
                               (key,)).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def set_server_default(store, key: str, value: str) -> None:
    store.cx.execute(
        "CREATE TABLE IF NOT EXISTS mcp_defaults(key TEXT PRIMARY KEY,"
        " value TEXT)")
    store.cx.execute("INSERT OR REPLACE INTO mcp_defaults VALUES(?,?)",
                     (key, value))
    store.commit()


def _norm_ref(ref: str) -> str:
    """`Automation::fire()`, `$this->move`, `App\\Core\\Automation#fire`, `Task.find(id)`
    -> `Automation.fire` style dotted name (the language's own spelling of a member)."""
    r = re.sub(r"\(.*$", "", (ref or "").strip())
    r = r.replace("::", ".").replace("->", ".").replace("#", ".").replace("\\", ".").replace("/", ".")
    r = re.sub(r"^\$?this\.", "", r)
    return ".".join(p for p in r.split(".") if p)


def _resolve(store, ref: str):
    n=store.get_node(ref)
    if n: return n
    norm = _norm_ref(ref)
    if norm and "." in norm:
        # `Class.member`: the member whose dotted qualified name ends with it
        # (`App\Core\Automation.fire` for `Automation::fire`)
        tail = norm.split(".")
        rows = store.cx.execute("SELECT id, qualified_name FROM nodes WHERE label=? AND type IN "
                                "('function','method','class','interface','struct','trait','enum','type')",
                                (tail[-1],)).fetchall()
        hits = [r[0] for r in rows
                if (("." + (r[1] or "").replace("\\", ".")).lower()).endswith("." + norm.lower())]
        if len(hits) == 1:
            return store.get_node(hits[0])
        ref = norm
    cands=store.search_symbols(ref,10,partial=True)
    if not cands: return None
    # prefer exact qualified/label match, then real types over external_symbol
    rl=ref.lower()
    def _k(c):
        exact = 0 if (c.qualified_name.lower()==rl or c.label.lower()==rl or c.qualified_name.lower().endswith("."+rl)) else 1
        ext = 1 if c.type=="external_symbol" else 0
        # `compose` the function beats `compose.ts` the module (shorter qualified name)
        mod = 1 if c.type in ("module", "package", "file") else 0
        return (exact, ext, mod, len(c.qualified_name))
    return sorted(cands, key=_k)[0]


def _did_you_mean(store, ref: str, n: int = 5) -> list:
    """Closest real symbols for a ref that did not resolve: names containing its last segment."""
    tail = (_norm_ref(ref) or ref or "").split(".")[-1]
    if not tail:
        return []
    out = []
    for c in store.search_symbols(tail, n * 3, partial=True):
        if c.type == "external_symbol":
            continue
        out.append(f"{c.qualified_name} ({c.type}) @ {c.file_path} #{c.id}")
        if len(out) >= n:
            break
    return out


def _unknown_ref(store, ref: str) -> dict:
    """The answer for a symbol or id that is not in the index. An empty list would read as 'none found'."""
    return {"error": f"no symbol or node id '{ref}' in the index",
            "did_you_mean": _did_you_mean(store, ref),
            "hint": "search_symbols(query=<part of the name>) lists matches; pass a qualified name or an id"}


def _node_for(store, ref: str):
    """Node for an id or an exact (qualified) name; None when neither resolves."""
    n = store.get_node(ref)
    if n:
        return n
    c = _resolve(store, ref)
    if c is None:
        return None
    rl = (_norm_ref(ref) or ref or "").lower()
    q = c.qualified_name.lower()
    if q == rl or c.label.lower() == rl or q.endswith("." + rl):
        return c
    return None


def _ledger_snapshot(store) -> str:
    """Latest snapshot id (ledger revision pin). Empty when none exists."""
    try:
        r = store.cx.execute(
            "SELECT id FROM snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
        return r["id"] if r else ""
    except Exception:
        return ""


def _ledger_parse(args: dict):
    """(snapshot_pin, ids_set, active). Inactive unless `known` is a dict
    with a snapshot string and an ids list — anything else fails safe to a
    full self-contained response."""
    known = (args or {}).get("known")
    if not isinstance(known, dict):
        return None, set(), False
    snap = known.get("snapshot", "")
    ids = known.get("ids", [])
    if not isinstance(snap, str) or not isinstance(ids, list):
        return None, set(), False
    return snap, {str(i) for i in ids if isinstance(i, str) and i}, True


def _item_ledger_key(it: dict) -> str:
    """Stable identity for omission matching: existing node/edge ids, else
    a deterministic marker key. Empty string = never omissible."""
    if not isinstance(it, dict):
        return ""
    nid = str(it.get("id", "") or "")
    if nid:
        return nid
    typ = str(it.get("type", "") or "")
    sym = str(it.get("symbol") or it.get("label") or "")
    if typ == "external_symbol" or sym.startswith("ext:") or \
            sym.startswith("import:"):
        return f"marker:{typ}:{sym}"
    return ""


def _omit_ref(it: dict, key: str) -> dict:
    """Cited reference replacing an already-held item. The ref identifies
    via the stable id and stays glanceable (file/symbol); edge refs
    additionally keep endpoints, relation, file path, lines, and the
    evidence/confidence classification, because those decide how a
    relationship is read. Occurrence spans and provenance lists resolve
    through the caller's ledger (they are identical to the first rendering
    by id-stability); these shapes' other scalar fields are either stable
    per id (label, qualified name, type) or carried here (file, line).
    A rename or re-identify mints a new id, so refs never pin stale facts.
    """
    ref = {"ref": key, "omitted": True}
    for f in ("file", "file_path", "symbol", "label", "line", "type"):
        if it.get(f) not in (None, ""):
            ref[f] = it[f]
    if it.get("source") and it.get("target"):
        ref["source"] = it["source"]
        ref["target"] = it["target"]
        for f in ("relation", "evidence_type", "confidence_class",
                  "start_line", "end_line"):
            if it.get(f) not in (None, ""):
                ref[f] = it[f]
    return ref


def _apply_ledger(store, items: list, known_snap: str | None,
                  known_ids: set, active: bool):
    """Returns (out_items, omitted_ids, ledger_status). Legacy shape
    untouched when inactive. Stale snapshots ignore `known` entirely."""
    if not active:
        return items, [], None
    snap = _ledger_snapshot(store)
    if known_snap != snap:
        return items, [], "stale-snapshot-ignored"
    out, omitted = [], []
    for it in items:
        key = _item_ledger_key(it) if isinstance(it, dict) else ""
        if key and key in known_ids:
            out.append(_omit_ref(it, key))
            omitted.append(key)
        else:
            out.append(it)
    return out, omitted, "applied"


def _telemetry_active(args: dict) -> bool:
    """Opt-in per-call telemetry (E-08): `telemetry: true` wraps the legacy
    result as `{data, telemetry}`. Absent/false keeps the legacy shape
    byte-identically (same rule as ledger/fact-pack/scope opt-ins)."""
    v = (args or {}).get("telemetry")
    return v in (True, 1, "true", "1")


def _retry_hint(name: str, args: dict, result) -> str | None:
    """Narrowing guidance when a result hit a bound. None when complete.

    Never invents server state: every hint re-derives from fields already
    present in the legacy result (continuation offsets, at-limit lists,
    budget limitations)."""
    try:
        if name == "get_neighbors" and isinstance(result, dict):
            if result.get("complete") is False:
                cont = result.get("continuation") or {}
                return (f"repeat with offset={cont.get('offset', '?')} "
                        f"(same id, limit) for the next page")
            return None
        if name == "find_related_tests" and isinstance(result, dict):
            if result.get("complete") is False:
                cont = result.get("continuation") or {}
                return (f"repeat with offset={cont.get('offset', '?')} "
                        f"for remaining test files")
            return None
        if name == "generate_test_plan" and isinstance(result, dict):
            lims = result.get("limitations") or []
            if any("budget" in str(x).lower() for x in lims):
                return ("narrow with mode=conservative and fewer "
                        "files/symbols, then widen")
            cands = result.get("candidates") or []
            if len(cands) >= int((args or {}).get("limit", 50) or 50):
                return ("candidate list reached the limit; raise limit or "
                        "narrow files/symbols")
            return None
        if name in ("search_symbols", "query_graph") and isinstance(result, list):
            if len(result) >= int((args or {}).get("limit", 20) or 20):
                return ("at-limit result set; more may exist — narrow the "
                        "query or raise limit")
            return None
        if name == "analyze_change_impact" and isinstance(result, dict):
            findings = result.get("findings") or []
            if len(findings) >= 200:
                return ("large impact set; scope with files=[...] or a "
                        "smaller max_depth")
            return None
    except Exception:
        return None
    return None


def _with_telemetry(name: str, args: dict, result, t0: float):
    """Wraps only when opted in; otherwise returns the legacy value."""
    if not _telemetry_active(args):
        return result
    import json as _json
    import time as _time
    try:
        raw = _json.dumps(result, default=str)
    except Exception:
        raw = ""
    return {"data": result,
            "telemetry": {"tool": name,
                          "latency_ms": round((_time.perf_counter() - t0) * 1000, 2),
                          "response_bytes": len(raw.encode("utf-8")),
                          "retry_hint": _retry_hint(name, args, result)}}


def _compact_wanted(store, args: dict, default_key: str) -> bool:
    """Compact is the agent default; `compact: false` (or a deployment pin
    `<default_key> = legacy`) returns the legacy shape byte-identically."""
    flag = (args or {}).get("compact")
    if flag in (False, 0, "false", "0"):
        return False
    if flag in (True, 1, "true", "1"):
        return True
    return get_server_default(store, default_key) != "legacy"


def _node_lines(store, ids) -> dict:
    """id -> (qualified name, "file:line") for compact rendering."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        q = ",".join("?" * len(ch))
        for r in store.cx.execute(
                f"SELECT id, qualified_name, label, file_path, start_line FROM nodes "
                f"WHERE id IN ({q})", ch).fetchall():
            name = r["qualified_name"] or r["label"] or r["id"]
            loc = f"{r['file_path']}:{r['start_line']}" if r["file_path"] else ""
            out[r["id"]] = (name, loc)
    return out


def _compact_neighbors(store, focus: str, rows: list, cfg=None, name: str = "") -> list:
    """One line per edge: `<- calls pkg.Cls.m @ a.py:12 [resolved] #id`.
    The location is the edge's site (call line), the #id the other node so
    the agent can hop without another lookup. With `cfg` and the callee's
    `name`, a caller line also carries the call's code (`:: x.save(1)`) and
    the caller's other call lines, so nothing needs to be opened to see how
    it is called."""
    other = [r["target"] if r["source"] == focus else r["source"] for r in rows]
    names = _node_lines(store, other)
    roots = [str(x) for x in (getattr(cfg, "workspace_roots", None) or ["."])] if cfg is not None else []
    out = []
    for r, o in zip(rows, other):
        arrow = "->" if r["source"] == focus else "<-"
        nm, _defloc = names.get(o, (o.split(":", 2)[-1], ""))
        site = f"{r['file_path']}:{r['start_line']}" if r.get("file_path") else ""
        conf = r.get("confidence_class") or ""
        code = ""
        if arrow == "<-" and r.get("file_path") and r["relation"] != "dispatches" and not (roots and name):
            from ..change.check import occurrence_lines
            lines = occurrence_lines(r.get("start_line") or 0, r.get("attributes"))
            if len(lines) > 1:
                # every call line of this caller, not just the first
                code = f" (+{len(lines) - 1} more call(s) here: lines {', '.join(map(str, lines[1:31]))}"
                code += ", ...)" if len(lines) > 31 else ")"
        if roots and name and arrow == "<-" and r.get("file_path") and r["relation"] != "dispatches":
            from ..change.check import occurrence_lines, site_line
            lines = occurrence_lines(r.get("start_line") or 0, r.get("attributes"))
            first = site_line(roots, r["file_path"], lines[0], name) if lines else None
            if first:
                site = f"{r['file_path']}:{first[0]}"
                code = f" :: {first[1]}"
                if len(lines) > 1:
                    code += f" (+{len(lines) - 1} more call(s) here: lines {', '.join(map(str, lines[1:31]))}"
                    code += ", ...)" if len(lines) > 31 else ")"
        rel = "dispatches (runs when this base/interface member is called)" \
            if r["relation"] == "dispatches" and arrow == "<-" else r["relation"]
        out.append(f"{arrow} {rel} {nm}" + (f" @ {site}" if site else "")
                   + (f" [{conf}]" if conf and conf != "resolved" else "") + code + f" #{o}")
    return out


_CODE_TYPES = ("function", "method", "class", "test", "interface", "struct",
               "type", "trait", "enum", "constant", "module", "table", "column")


def _source_span(store, cfg, node, max_lines: int) -> list:
    """Numbered source lines of a symbol, read from the indexed workspace."""
    from pathlib import Path
    loc = node.location
    start = getattr(loc, "start_line", 0) or 0
    end = getattr(loc, "end_line", 0) or start
    if not node.file_path or start <= 0:
        return []
    for root in (getattr(cfg, "workspace_roots", None) or ["."]):
        p = Path(root) / node.file_path
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
            end = max(end, start)
            body = lines[start - 1:min(end, start - 1 + max_lines)]
            out = [f"{start + i:>5}| {t}" for i, t in enumerate(body)]
            if end - start + 1 > max_lines:
                out.append(f"  ... {end - start + 1 - max_lines} more lines "
                           f"(Read {node.file_path} offset={start + max_lines})")
            return out
    return []


def _symbol_card(store, cfg, node, max_lines: int = 60, edges: int = 15) -> dict:
    """Everything an agent needs about one symbol in one response: where it
    is, its code, who calls it, what it calls (resolved with sites, plus the
    names of calls the graph could not bind)."""
    import json as _json
    inc_rels = ["calls", "instantiates", "calls_http", "dispatches"]
    if node.type in ("table", "column"):
        inc_rels = ["references"]        # code that reads/writes it, foreign keys, views
    import re as _re
    _test = _re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|_spec|\.test|\.spec|Tests?)\.[a-z]+$|(^|/)test_[^/]*$", _re.I)
    # production callers first: they are what a change must keep working
    inc = sorted(store.neighbors(node.id, "in", inc_rels, max(edges * 4, 80)),
                 key=lambda r: bool(_test.search(r.get("file_path") or "")))
    # a short caller list is given whole (an impact question needs every
    # site); a long one is cut with an explicit continuation below
    inc = inc if len(inc) <= max(edges, 40) else inc[:edges]
    try:
        q = ",".join("?" * len(inc_rels))
        total_in = store.cx.execute(
            f"SELECT COUNT(*) FROM edges WHERE target=? AND relation IN ({q}) "
            f"AND id NOT IN (SELECT edge_id FROM superseded_edges)", (node.id, *inc_rels)).fetchone()[0]
    except Exception:
        total_in = len(inc)
    out = store.neighbors(node.id, "out", ["calls", "instantiates", "calls_http"], edges)
    unres = store.neighbors(node.id, "out", ["calls_unresolved"], 40)
    names = []
    for e in unres:
        try:
            a = _json.loads(e["attributes"]) if isinstance(e["attributes"], str) else (e["attributes"] or {})
        except ValueError:
            a = {}
        nm = a.get("unresolved_call") or ".".join(x for x in (a.get("base"), a.get("member")) if x) \
            or str(e["target"]).split(":")[-1]
        if nm and nm not in names:
            names.append(nm)
    loc = node.location
    card = {"symbol": f"{node.qualified_name} ({node.type}) @ {node.file_path}:"
                      f"{getattr(loc, 'start_line', 0)}-{getattr(loc, 'end_line', 0)}",
            "source": _source_span(store, cfg, node, max_lines),
            "callers": _compact_neighbors(store, node.id, inc, cfg, node.label),
            "calls": _compact_neighbors(store, node.id, out)}
    if total_in > len(inc):
        card["callers_total"] = total_in
        card["more_callers"] = (f"{total_in - len(inc)} more: get_neighbors(id='{node.id}', "
                                f"direction='in', offset={len(inc)})")
    if names:
        card["unresolved_calls"] = names[:15]
    if node.type in ("function", "method"):
        _unverified_into(store, cfg, [node.id], node.label, card)
    return card


def _unverified_into(store, cfg, ids: list, name: str, out: dict, limit: int = 8) -> None:
    """Add the possible-but-unproven call sites of `name` (receiver of unknown type)."""
    try:
        from ..retrieval.uncertain import unverified_sites
        from ..change.plan import _family, _resolve as _pres
        roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
        fam = set(ids)
        for i in ids:
            row, _o = _pres(store.cx, i)
            if row is not None:
                fam |= {n["id"] for n in _family(store.cx, row)}
        sites, total = unverified_sites(store.cx, roots, fam, name, limit=limit)
    except Exception:
        return
    from ..retrieval.uncertain import TOO_COMMON
    if total > TOO_COMMON:
        out["unverified_total"] = total
        out["unverified_note"] = (f"`{name}` is a common member name: {total} calls on receivers of unknown "
                                  "type exist (mostly other types'); the callers above are the proven ones.")
        return
    if total:
        out["unverified_callers"] = [f"{f}:{ln}  {code}" for f, ln, code, _r in sites]
        out["unverified_total"] = total
        out["unverified_note"] = (f"`{name}` is also called at these sites on receivers whose type could "
                                  "not be established. For an impact list or a rename, count each one as a "
                                  "caller unless its code shows the receiver is a different type; the callers "
                                  "list above is the proven part, not necessarily all of it.")


def _compact_query(store, cfg, res: dict, limit: int) -> dict:
    """query_graph/explain_symbol answer: ranked matches one line each, the
    best code match expanded as a symbol card. Evidence quotes, metadata and
    file-head excerpts (the bulk of the legacy payload) are dropped; ask
    with compact=false for them."""
    facts = [f for f in (res.get("facts") or []) if isinstance(f, dict)]
    code = [f for f in facts if f.get("type") in _CODE_TYPES and f.get("id")]
    lines = []
    for f in facts[:limit]:
        loc = f"{f.get('file') or ''}:{f.get('line') or ''}".strip(":")
        lines.append(f"{f.get('qualified_name') or f.get('label')} ({f.get('type')})"
                     + (f" @ {loc}" if loc else "") + f" #{f.get('id')}")
    out = {"answer": res.get("answer", ""), "matches": lines}
    if code:
        top = store.get_node(code[0]["id"])
        if top is not None:
            out["best"] = _symbol_card(store, cfg, top, max_lines=40, edges=10)
    lim = res.get("limitations") or []
    if lim:
        out["limitations"] = lim[:3]
    return out


def _compact_impact(store, syms: list, findings: list, tests: list, cfg=None) -> dict:
    ids = list(syms) + [f["affected"] for f in findings] + list(tests)
    names = _node_lines(store, ids)
    lines = {"direct": [], "transitive": []}
    hop_cache: dict = {}

    def hop(src, tgt):
        """(is_containment_only, call-site) for the dependency hop src->tgt."""
        if (src, tgt) not in hop_cache:
            rs = store.cx.execute(
                "SELECT relation, file_path, start_line, attributes FROM edges WHERE source=? "
                "AND target=?", (src, tgt)).fetchall()
            dep = [r for r in rs if r["relation"] not in ("defines", "contains")]
            from ..change.check import occurrence_lines as _occ
            site = ""
            fp_ = next((r["file_path"] for r in dep if r["file_path"]), "")
            if fp_:
                # every call line of this dependent (all its edges to the target)
                ls_ = sorted({x for r in dep if r["file_path"] == fp_
                              for x in (_occ(r["start_line"] or 0, r["attributes"]) or [r["start_line"]])})
                site = f"{fp_}:{ls_[0]}"
                if len(ls_) > 1:
                    site += f" (+ lines {', '.join(map(str, ls_[1:31]))}{', ...' if len(ls_) > 31 else ''})"
            hop_cache[(src, tgt)] = (bool(rs) and not dep, site)
        return hop_cache[(src, tgt)]

    for f in findings:
        a = f["affected"]
        nm, defloc = names.get(a, (a, ""))
        via = f.get("via") or []
        # Containment anywhere on the path (owner defines member) makes the
        # finding "same owner", not "depends on": skip it in the compact view.
        if any(hop(via[k + 1], via[k])[0] for k in range(len(via) - 1)):
            continue
        site = hop(via[-1], via[-2])[1] if len(via) >= 2 else ""
        row = f"{nm} @ {site or defloc} [{f.get('confidence','')}]"
        if f.get("direct"):
            lines["direct"].append(row)
        else:
            lines["transitive"].append(row + f" depth={f.get('depth')}")
    test_rows = sorted({names.get(t, (t, ""))[1].split(":")[0] or names.get(t, (t, ""))[0]
                        for t in tests})
    out = {"symbols": [names.get(s, (s, ""))[0] for s in syms],
           "direct": lines["direct"], "transitive": lines["transitive"][:30],
           "transitive_total": len(lines["transitive"]),
           "tests": test_rows[:15], "tests_total": len(test_rows),
           "note": "direct = immediate dependents with call site; pass compact=false for raw ids"}
    for s in syms[:3]:
        n = store.get_node(s)
        if n is not None and n.type in ("function", "method"):
            _unverified_into(store, cfg, [s], n.label, out)
            break
    return out


def call_tool(store, cfg, name: str, args: dict):
    import time as _time
    _t0 = _time.perf_counter()
    from ..retrieval import symbols as sym
    from ..query.executor import execute
    from ..graph import algorithms as alg, impact as imp
    from ..exports.json_exporter import subgraph
    args=args or {}
    lim=int(args.get("limit",20))
    _known_snap, _known_ids, _ledger_on = _ledger_parse(args)
    if name=="search_symbols":
        from ..retrieval.reranking import rerank as _rr
        res=_rr(sym.lookup(store,args["query"],lim*2), args["query"])[:lim]
        items=[{"id":n.id,"type":n.type,"label":n.label,"qualified_name":n.qualified_name,"file":n.file_path} for n in res]
        if not _ledger_on:
            return _with_telemetry(name, args, items, _t0)
        out, omitted, status = _apply_ledger(store, items, _known_snap, _known_ids, True)
        return _with_telemetry(name, args, {"results": out, "snapshot": _ledger_snapshot(store),
                "omitted": omitted, "ledger": status}, _t0)
    if name in SANDBOX_TOOLS:
        from ..change import code_io as _io
        if name == "read_code":
            targets = [str(t) for t in (args.get("targets") or ([args["target"]] if args.get("target") else []))]
            if not targets:
                return "read_code: pass targets=[symbol names or file paths]"
            outs = []
            for t in targets[:12]:
                p_, _rel = _io.locate(cfg, t)
                if p_ is not None:
                    outs.append(_io.read_lines(cfg, t, args.get("start"), args.get("end")))
                    continue
                n_ = _node_for(store, t)
                if n_ is not None and n_.type in _CODE_TYPES and n_.file_path:
                    loc_ = n_.location
                    s_ = getattr(loc_, "start_line", 0) or 1
                    outs.append(_io.read_lines(cfg, n_.file_path, s_, getattr(loc_, "end_line", 0) or s_,
                                               header=f"== {n_.qualified_name} ({n_.type}) @ {n_.file_path}"))
                else:
                    u_ = _unknown_ref(store, t)
                    outs.append(f"== {t}\nnot a file of the workspace and no such symbol in the index"
                                + ("; did you mean: " + "; ".join(u_["did_you_mean"][:4]) if u_["did_you_mean"] else ""))
            return "\n\n".join(outs)
        if name == "edit_code":
            eds = []
            for e_ in args.get("edits") or []:
                e_ = dict(e_)
                if not e_.get("path") and e_.get("symbol"):
                    n_ = _node_for(store, str(e_["symbol"]))
                    if n_ is None or not n_.file_path:
                        return f"nothing written:\n- symbol {e_['symbol']!r} is not in the index"
                    loc_ = n_.location
                    e_["path"] = n_.file_path
                    e_["start"] = getattr(loc_, "start_line", 0) or None
                    e_["end"] = getattr(loc_, "end_line", 0) or None
                eds.append(e_)
            return _io.apply_edits(cfg, eds)
        return _io.write_file(cfg, str(args.get("path") or ""), str(args.get("content") or ""),
                              bool(args.get("overwrite")))
    if name=="check_change":
        from ..change.check import check_change, render_text
        _r = check_change(cfg, base=args.get("base") or None, depth=int(args.get("depth", 2) or 2),
                          task=str(args.get("task") or ""))
        text = render_text(_r)
        if args.get("run_tests") and (_r.get("tests") or {}).get("commands"):
            from ..change.verify import ALLOW_ENV, allowed, render_runs, run_tests
            if not allowed():
                text += (f"\n\n(run_tests needs {ALLOW_ENV}=1 in the MCP server environment; "
                         "run the commands above yourself instead.)")
            else:
                text += "\n" + render_runs(run_tests(store, cfg, _r["tests"]["commands"]))
        return text
    if name=="task_context":
        from ..retrieval.task_context import task_context as _tc
        return _tc(store, cfg, args["task"])
    if name=="compare_revisions":
        from ..change.revisions import compare_revisions as _cr
        return _cr(cfg, args["base"], args.get("head") or "HEAD", args.get("path") or "")
    if name=="rename_symbol":
        from ..change.rename import rename_symbol as _rn
        return _rn(store, cfg, args["symbol"], args.get("new_name") or "", apply=not args.get("dry_run"))
    if name=="move_symbol":
        from ..change.move import move_symbol as _mv
        return _mv(store, cfg, args["symbol"], args.get("to_file") or "", apply=not args.get("dry_run"),
                   with_dependencies=bool(args.get("with_dependencies")))
    if name=="change_signature":
        from ..change.signature import change_signature as _cs
        return _cs(store, cfg, args["symbol"], add_param=args.get("add_param"),
                   remove_param=args.get("remove_param"), apply=not args.get("dry_run"),
                   reorder_params=args.get("reorder_params"))
    if name=="change_plan":
        from ..change.plan import change_plan as _cp
        return _cp(store, cfg, args["symbol"], args.get("change") or "rename", args.get("new_name") or "")
    if name=="list_routes":
        from ..retrieval.routes import list_routes as _lr
        return _lr(store, cfg, args.get("method") or "write", args.get("path") or "",
                   args.get("guards") or None)
    if name=="feature_slice":
        from ..retrieval.feature_slice import feature_slice as _fs
        from .budget import budget_for, char_cap
        return _fs(store, cfg, args["feature"], char_cap(budget_for(name, args)) - 600,
                   focus=args.get("focus") or "")
    if name=="search_documents":
        res=store.search_lexical(args["query"],lim)
        return [{"id":n.id,"type":n.type,"label":n.label,"file":n.file_path} for n in res if n.type in ("doc_section","claim","file","config_key") or True][:lim]
    if name=="expand_evidence":
        from ..retrieval import spans as _spans
        _nid = args.get("node_id")
        if _nid and not store.get_node(_nid):
            _en = _node_for(store, _nid)
            if _en is None:
                return dict(_unknown_ref(store, _nid), error=f"unknown node: {_nid!r} (not an id or a symbol name)")
            _nid = _en.id
        return _spans.expand_evidence(
            store, cfg, evidence_id=args.get("evidence_id"),
            node_id=_nid,
            level=args.get("level") or "span",
            context_lines=int(args.get("context_lines", 10) or 10))
    if name=="query_graph":
        res=execute(store,args["query"],lim,
                    mode=args.get("mode") or "hybrid")
        if args.get("scope")=="deps" and isinstance(res, dict):
            # E-05: lift route-chain sinks (handler --calls--> service)
            # into deps facts with path evidence, before scope budgeting.
            from ..retrieval import routechain as _rc
            res=_rc.expand_deps(store, res)
        if args.get("scope") and isinstance(res, dict):
            from ..query import scoping as _scope
            res = _scope.apply_scope(res, args["scope"], limit=lim)
        if (args.get("evidence_mode")=="spans" or args.get("scope")) and isinstance(res, dict):
            from ..retrieval import spans as _spans
            res=_spans.spanify_response(res, store, cfg)
            if args.get("scope"):
                from ..query import scoping as _scope2
                res = _scope2.strip_fact_texts(res)
        if _ledger_on and isinstance(res, dict):
            facts=res.get("facts")
            if isinstance(facts, list):
                out, omitted, status = _apply_ledger(store, facts, _known_snap, _known_ids, True)
                res=dict(res)
                res["facts"]=out
                res["snapshot"]=_ledger_snapshot(store)
                res["omitted"]=omitted
                res["ledger"]=status
        if (isinstance(res, dict) and not args.get("scope") and not _ledger_on
                and not args.get("format") and not args.get("evidence_mode")
                and _compact_wanted(store, args, "query_default")):
            return _with_telemetry(name, args, _compact_query(store, cfg, res, min(lim, 8)), _t0)
        if args.get("format")=="fact_pack_v1" and isinstance(res, dict):
            from ..query.factpack import encode as _pack
            return _with_telemetry(name, args, _pack(res), _t0)
        return _with_telemetry(name, args, res, _t0)
    if name=="get_node":
        n=store.get_node(args["id"])
        return None if not n else n.to_dict()
    if name=="get_neighbors":
        _n = _node_for(store, args["id"])
        if _n is None:
            return _with_telemetry(name, args, _unknown_ref(store, args["id"]), _t0)
        _asked = args["id"]
        args = dict(args, id=_n.id)
        res=store.neighbors(args["id"],args.get("direction","both"),args.get("relations"),lim,
                            offset=int(args.get("offset",0) or 0),
                            include_total=args.get("include_total") in (True,1,"true","1"))
        if not _ledger_on and _compact_wanted(store, args, "neighbors_default"):
            if isinstance(res, list):
                lines = _compact_neighbors(store, args["id"], res)
                if all(l.startswith(("<- defines", "<- contains")) for l in lines):
                    # containment is not a caller; say so instead of leaving the list to be read as "none"
                    lines.append(f"none: {_n.qualified_name} has no {args.get('direction', 'both')} calls/uses in "
                                 f"the graph (calls through dynamic dispatch are not guessed)")
                if _asked != _n.id:
                    lines.insert(0, f"(resolved '{_asked}' -> {_n.qualified_name} #{_n.id})")
                off_ = int(args.get("offset", 0) or 0)
                if len(res) >= lim:
                    # never a silently cut list: say there is more and how to get it
                    lines.append(f"... more: get_neighbors(id='{args['id']}', direction="
                                 f"'{args.get('direction', 'both')}', offset={off_ + len(res)})")
                return _with_telemetry(name, args, lines, _t0)
            if isinstance(res, dict) and isinstance(res.get("results"), list):
                res = dict(res)
                res["results"] = _compact_neighbors(store, args["id"], res["results"])
                return _with_telemetry(name, args, res, _t0)
        if not _ledger_on or not isinstance(res, list):
            return _with_telemetry(name, args, res, _t0)
        out, omitted, status = _apply_ledger(store, res, _known_snap, _known_ids, True)
        return _with_telemetry(name, args, {"results": out, "snapshot": _ledger_snapshot(store),
                "omitted": omitted, "ledger": status}, _t0)
    if name in ("find_path","trace_dependency_chain"):
        from ..retrieval import multihop as _mh
        seeds=_mh.discover_seeds(store,[args["source"],args["target"]])
        srcs=[n for n in seeds["endpoints"][0]["candidates"]]
        dsts=[n for n in seeds["endpoints"][1]["candidates"]]
        if not srcs or not dsts:
            return {"error":"symbol not found","source":args["source"],"target":args["target"],
                    "diagnostics": {"no_candidates": True,
                                    "seeds": {e["identifier"]: [n.id for n in e["candidates"]]
                                              for e in seeds["endpoints"]}}}
        s, t = srcs[0], dsts[0]
        md=int(args.get("max_depth",4))
        rels=args.get("relations")
        # Try candidate pairs in order; the first validated ranking wins so an
        # ambiguous name resolves through the pair that actually connects.
        ranked, found, pair = [], {"paths": [], "expanded": 0, "budget_hit": None}, None
        direction = "forward"
        for cs in srcs:
            for ct in dsts:
                f=_mh.find_paths(store,[cs.id],[ct.id],max_depth=md,relations=rels)
                r=_mh.rank_paths(store,f["paths"],
                                 query_terms=set(str(args.get("query_terms") or "").split()) if args.get("query_terms") else None,
                                 seed_repo=getattr(cs,"repository_id",""))
                if r:
                    ranked, found, pair = r, f, (cs, ct)
                    break
            if ranked:
                break
        if pair is not None:
            s, t = pair
        if not ranked:
            # Reverse reachability: the dependency may flow target->source
            # (e.g. a response built from a model). Reported as reverse, never
            # disguised as forward evidence. Undirected adjacency is NOT
            # presented as a path.
            for cs in srcs:
                for ct in dsts:
                    f=_mh.find_paths(store,[ct.id],[cs.id],max_depth=md,relations=rels)
                    r=_mh.rank_paths(store,f["paths"],seed_repo=getattr(cs,"repository_id",""))
                    if r:
                        ranked, found = r, f
                        direction = "reverse"
                        break
                if ranked:
                    break
        best=ranked[0]["nodes"] if ranked else []
        if best and direction == "reverse":
            best = list(reversed(best))
        validation=alg.validate_path(best,store.all_edges(limit=200000)) if best and direction == "forward" else (
            {"valid": bool(best), "direction": direction,
             "reason": "reverse reachability: dependency flows target->source"} if best
            else {"valid": False, "reason": "no path found"})
        return {"path": best, "from":s.id,"to":t.id, "direction": "forward",
                "from_label": getattr(s,"qualified_name",s.id), "to_label": getattr(t,"qualified_name",t.id),
                "validation": validation,
                "ranked_paths": _mh.explain(store, ranked),
                "diagnostics": {"expanded": found["expanded"], "budget_hit": found["budget_hit"],
                                "candidates_considered": len(found["paths"]),
                                "ambiguous_source": seeds["endpoints"][0]["ambiguous"],
                                "ambiguous_target": seeds["endpoints"][1]["ambiguous"],
                                "weights": dict(_mh.PATH_WEIGHTS)}}
    if name=="explain_symbol":
        refs = [s for s in (args.get("symbols") or []) if isinstance(s, str) and s.strip()]
        if args.get("symbol") and args["symbol"] not in refs:
            refs.insert(0, args["symbol"])
        if not refs:
            return {"error": "pass `symbol` or `symbols`"}
        def _card(ref):
            n = _resolve(store, ref)
            if n is None or n.type not in _CODE_TYPES:
                return None
            cands = [c for c in store.search_symbols(ref.split(".")[-1], 8, partial=False)
                     if c.type in _CODE_TYPES and c.id != n.id]
            card = _symbol_card(store, cfg, n)
            if cands:
                card["other_matches"] = [f"{c.qualified_name} ({c.type}) @ {c.file_path} #{c.id}"
                                         for c in cands[:5]]
            return card
        if _compact_wanted(store, args, "explain_default"):
            if len(refs) > 1:
                cards = [_card(r) or dict(_unknown_ref(store, r), symbol=r) for r in refs[:8]]
                return _with_telemetry(name, args, {"symbols": cards}, _t0)
            card = _card(refs[0])
            if card is not None:
                return _with_telemetry(name, args, card, _t0)
        if len(refs) == 1 and not re.search(r"\s", refs[0].strip()) and _resolve(store, refs[0]) is None:
            # a bare name that is not in the index: the free-text answer below would be an unrelated "top match"
            return _with_telemetry(name, args, _unknown_ref(store, refs[0]), _t0)
        return execute(store,f"Explain how {refs[0]} works",20)
    if name=="get_source_evidence":
        n=store.get_node(args["id"])
        if not n: return []
        return [dict(r) for r in [store.cx.execute("SELECT * FROM evidence WHERE id=?",(pid,)).fetchone() for pid in (n.provenance_ids or [])] if r]
    if name=="get_project_overview":
        n,e=store.count(); nodes=store.all_nodes()
        from collections import Counter
        c=Counter(x.type for x in nodes)
        return {"nodes":n,"edges":e,"by_type":dict(c)}
    if name=="get_module_dependencies":
        raw=store.all_edges(); mods=sorted({e["source"] for e in raw if "module" in e["source"]} | {e["target"] for e in raw if "module" in e["target"]})
        return {"modules":mods[:100]}
    if name in ("repo_map", "get_architecture_summary"):
        from ..retrieval.repo_map import repo_map as _rm
        return _rm(store, cfg, args.get("focus") or "")
    if name=="find_orphaned_symbols":
        return alg.orphans(store.all_nodes(), [type("E",(),e)() for e in store.all_edges()])[:lim]
    if name=="find_cycles":
        nodes=store.all_nodes(); eo=[type("E",(),e)() for e in store.all_edges()]
        return alg.find_cycles(nodes,eo)[:20]
    if name=="find_unresolved_relationships":
        return [e for e in store.all_edges() if e["relation"]=="calls_unresolved"][:lim]
    if name=="compare_subsystems":
        return {"a":args["a"],"b":args["b"],"note":"compare via get_neighbors on each module; shared deps need manual review"}
    if name=="analyze_change_impact":
        raw_syms=list(args.get("symbols",[]) or [])
        syms=[]; missing=[]
        for s in raw_syms:
            if store.get_node(s): syms.append(s)
            else:
                r=_resolve(store, s)
                if r: syms.append(r.id)
                else: missing.append(s)
        not_found = [dict(_unknown_ref(store, m), symbol=m) for m in missing]
        if not_found and not syms and not args.get("files"):
            return _with_telemetry(name, args, {"error": "none of the symbols is in the index; no impact computed",
                                                "not_found": not_found}, _t0)
        for f in args.get("files",[]) or []:
            syms += [n.id for n in store.search_symbols(f.split("/")[-1].split(".")[0],10)]
        eo=[type("E",(),e)() for e in store.all_edges()]
        _findings = imp.analyze(syms, eo, int(args.get("max_depth",4)))
        _tests = imp.find_tests(store.all_nodes(), eo, syms)
        if _compact_wanted(store, args, "impact_default"):
            # Containment (file defines symbol) is not a dependent; the
            # compact view lists callers/importers with their call sites.
            _deps = [f for f in _findings if not str(f.get("affected", "")).startswith("file:")]
            _res = _compact_impact(store, syms, _deps, _tests, cfg)
        else:
            _res = {"symbols_resolved": syms, "findings": _findings, "tests": _tests}
        if not_found and isinstance(_res, dict):
            _res = dict(_res, not_found=not_found)
        return _with_telemetry(name, args, _res, _t0)
    if name=="find_related_tests":
        raw=list(args.get("symbols",[]) or [])
        syms=[]; missing=[]
        for s in raw:
            if store.get_node(s): syms.append(s)
            else:
                r=_resolve(store, s)
                if r: syms.append(r.id)
                else: missing.append(s)
        not_found = [dict(_unknown_ref(store, m), symbol=m) for m in missing]
        eo=[type("E",(),e)() for e in store.all_edges()]
        # Precedence (Phase 23): explicit `compact` flag wins; otherwise a
        # deployment-pinned server default (`mcp_defaults`); otherwise the
        # compact representation is the default. `compact: false`
        # preserves the legacy bare id list byte-identically.
        flag = args.get("compact")
        if flag in (False, 0, "false", "0"):
            return _with_telemetry(name, args, imp.find_tests(store.all_nodes(), eo, syms or raw, int(args.get("max_depth",3))), _t0)
        if flag not in (True, 1, "true", "1"):
            if get_server_default(store, "find_tests_default") == "legacy":
                return _with_telemetry(name, args, imp.find_tests(store.all_nodes(), eo, syms or raw, int(args.get("max_depth",3))), _t0)
        _ft = imp.find_tests_compact(
            store.all_nodes(), eo, syms or raw,
            int(args.get("max_depth", 3)),
            limit=int(args.get("limit", 200) or 200),
            offset=int(args.get("offset", 0) or 0))
        if not_found and isinstance(_ft, dict):
            _ft = dict(_ft, not_found=not_found)
            if not syms:
                _ft["warning"] = "none of the symbols is in the index, so '0 tests' means nothing; see not_found"
        return _with_telemetry(name, args, _ft, _t0)
    if name=="get_changed_symbols" or name=="get_changes_since":
        rows=[dict(r) for r in store.cx.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT ?", (int(args.get("limit",10)) if name=="get_changes_since" else 2,)).fetchall()]
        return rows
    if name=="investigate_error":
        from ..retrieval.error_trace import investigate_error as _ie
        return _ie(store, cfg, args["error"])
    if name=="get_index_status":
        from ..service import get_status
        from ..extraction.code import tree_sitter as tsa
        s = get_status(cfg)
        s["capabilities"] = tsa.capabilities()
        return s
    if name=="get_stale_sources":
        import os
        from ..discovery.scanner import fingerprint
        stale=[]
        for r in store.cx.execute("SELECT path,sha256 FROM files").fetchall():
            p=r["path"].split(":",1)[-1]
            for root in cfg.workspace_roots:
                ap=str(__import__("pathlib").Path(root)/p)
                if __import__("os").path.exists(ap):
                    try:
                        if fingerprint(ap)!=r["sha256"]: stale.append(p)
                    except Exception: pass
        return stale[:100]
    if name=="inspect_provenance": return call_tool(store,cfg,"get_source_evidence",args)
    if name=="get_conflicts":
        return [e for e in store.all_edges() if e["relation"] in ("conflicts_with",)] [:50]
    if name=="validate_graph":
        from ..service import validate_graph
        return validate_graph(cfg)
    if name=="get_query_diagnostics":
        from ..query.planner import plan
        return plan(args["query"])
    if name=="index_workspace": return {"error":"write operations require allow_write=true and explicit CLI confirmation"}
    if name=="export_subgraph": return subgraph(store, args["seed"], int(args.get("depth",2)))
    if name in ("get_coverage_summary","list_coverage_runs","get_symbol_coverage",
                "find_uncovered_symbols","compare_coverage_runs",
                "get_coverage_mapping_diagnostics"):
        from ..coverage import analysis as _an, store as _store
        if name=="get_coverage_summary": return _an.summary(store, args["run_id"])
        if name=="list_coverage_runs": return _store.list_runs(store)
        if name=="get_symbol_coverage":
            cands=store.search_symbols(args.get("symbol",""),5,partial=False) or \
                store.search_symbols(args.get("symbol",""),5,partial=True)
            if not cands: return {"symbol": args.get("symbol",""), "status": "unknown_symbol"}
            return _an.symbol_coverage(store, cands[0].id, args.get("run_id",""))
        if name=="find_uncovered_symbols":
            return _an.uncovered_symbols(store, args["run_id"], scope=args.get("scope",""),
                                         limit=int(args.get("limit",20)))
        if name=="compare_coverage_runs":
            return _an.compare(store, args["base"], args["head"])
        if name=="get_coverage_mapping_diagnostics":
            return _an.mapping_diagnostics(store, args["run_id"])
    if name=="generate_test_plan":
        from ..testplan import changes as _ch, plan as _plan
        from pathlib import Path as _Path
        mode = (args.get("mode") or "balanced").lower()
        if mode not in ("conservative", "balanced", "broad"):
            raise ValueError(f"unknown mode: {args.get('mode')}")
        roots = getattr(cfg, "workspace_roots", None) or ["."]
        root = str(_Path(roots[0]).resolve())
        import hashlib as _hl
        repo_id = "repo-" + _hl.sha256(root.encode()).hexdigest()[:10]
        # Priority 0 (read-only): the requested repository must be covered by
        # this database's workspace identity when one is recorded; otherwise
        # results would silently mix projects.
        from ..workspace_scope import read_db_workspace as _read_scope
        _ident = _read_scope(getattr(store, "path", "") or "")
        if _ident is not None:
            _covered = set()
            try:
                for _r in _ident.get("roots") or []:
                    _covered.add("repo-" + _hl.sha256(
                        str(_Path(_r).resolve()).encode()).hexdigest()[:10])
            except Exception:
                _covered = set()
            if repo_id not in _covered:
                raise ValueError(
                    f"repository {root} is not covered by this database's workspace "
                    f"identity; refusing a cross-project plan")
        changed = []
        for f in args.get("files", []) or []:
            _rid, rel = _ch.resolve_repo(roots, f)
            changed.append(_ch.ChangedFile(rel))
        mapped = _ch.map_to_symbols(store, repo_id, changed)
        for sym in args.get("symbols", []) or []:
            cands = store.search_symbols(sym, 5, partial=False) or \
                store.search_symbols(sym, 5, partial=True)
            if cands:
                mapped.setdefault("symbols", {}).setdefault(
                    cands[0].id, {"change": "modified", "via": "explicit",
                                  "file": cands[0].file_path, "status": "modified"})
        if not changed and not (args.get("symbols") or []):
            raise ValueError("generate_test_plan needs files or symbols")
        plan = _plan.build(store, repo_id, root, changed, mapped, mode=mode,
                           revision=_ch.current_sha(root),
                           change_digest="mcp:" + ",".join(sorted(
                               [c.rel for c in changed] + list(args.get("symbols", []) or []))))
        plan["candidates"] = plan["candidates"][:int(args.get("limit", 50))]
        return _with_telemetry(name, args, plan, _t0)
    if name in ("get_test_run", "list_test_runs", "compare_plan_run",
                "get_test_run_signals"):
        # Read-only history surface. These tools only read the Ripplet
        # database (plus the test files' current bytes for drift notes);
        # they never spawn a runner, write source files, or execute tests.
        from ..testrun import store as _rs, compare as _cmp
        if name == "get_test_run":
            row = _rs.get_run(store, args.get("run_id", ""),
                              with_log_excerpt=min(int(args.get("excerpt", 0) or 0), 8000))
            if not row:
                return {"error": f"unknown run: {args.get('run_id', '')}"}
            return row
        if name == "list_test_runs":
            return _rs.list_runs(store, limit=min(int(args.get("limit", 20) or 20), 100))
        if name == "compare_plan_run":
            prow = _rs.get_plan(store, args.get("plan_id", ""))
            rrow = _rs.get_run(store, args.get("run_id", ""))
            if not prow:
                return {"error": f"unknown plan: {args.get('plan_id', '')} "
                                 "(generate one with the test-plan CLI command first; "
                                 "MCP planning output is not persisted)"}
            if not rrow:
                return {"error": f"unknown run: {args.get('run_id', '')}"}
            cur = None
            try:
                from ..testrun import service as _trs
                from pathlib import Path as _P
                roots = getattr(cfg, "workspace_roots", None) or []
                if roots:
                    rels = list((rrow.get("source_fingerprint", {}) or {}).keys())
                    cur = _trs.fingerprint_files(str(_P(roots[0]).resolve()), rels)
            except Exception:
                cur = None
            return _cmp.compare_plan_run(prow.get("plan", {}), rrow, cur)
        if name == "get_test_run_signals":
            return _cmp.run_signals(store, limit=min(int(args.get("limit", 200) or 200), 500),
                                    include_coverage_requesters=args.get("include_coverage_requesters") in (True, 1, "true", "1"))
    raise ValueError(f"unknown tool {name}")
