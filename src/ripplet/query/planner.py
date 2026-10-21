from __future__ import annotations
import re
CATEGORIES = ["symbol_lookup","explanation","dependency_tracing","architecture_overview","change_impact","error_investigation","history","documentation","cross_project","test_discovery","unresolved"]
def classify(q: str) -> str:
    s=q.lower()
    if any(k in s for k in ["impact","break","affect","change"]): return "change_impact"
    if any(k in s for k in ["test","coverage"]): return "test_discovery"
    if any(k in s for k in ["path","reach","depend","call chain","chain"]): return "dependency_tracing"
    if any(k in s for k in ["architect","overview","module","structure"]): return "architecture_overview"
    if any(k in s for k in ["error","trace","fail","exception"]): return "error_investigation"
    if any(k in s for k in ["histor","commit","when did","evol"]): return "history"
    # imports/exports questions ("what does X import?") must precede the generic
    # "what does" explanation rule, which would otherwise swallow them.
    if any(k in s for k in ["import", "export", "re-export", "barrel"]): return "imports_exports"
    if any(k in s for k in ["doc","explain","how does","what does"]): return "explanation"
    if "unresolv" in s: return "unresolved"
    m=re.search(r"[A-Za-z_][\w.]*",q)
    return "symbol_lookup"
def plan(query: str) -> dict:
    cat=classify(query)
    tools={"symbol_lookup":["search_symbols","get_node"],"explanation":["search_symbols","explain_symbol","get_neighbors"],"dependency_tracing":["search_symbols","find_path","get_neighbors"],"architecture_overview":["get_project_overview","get_module_dependencies"],"change_impact":["analyze_change_impact","find_related_tests"],"test_discovery":["find_related_tests"],"error_investigation":["investigate_error","find_path"],"history":["get_changes_since","get_changed_symbols"],"documentation":["search_documents"],"cross_project":["query_graph"],"unresolved":["find_unresolved_relationships"]}.get(cat,["search_symbols"])
    return {"category": cat, "tools": tools, "strategy": f"{cat} via {','.join(tools)}"}
