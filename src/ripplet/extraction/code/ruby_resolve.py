"""Ruby repo-scoped resolution.

Binds ``ts-ruby`` placeholders that need the whole repository:

  * `class A < B` / `include M` / `extend M` / `prepend M` -> the repo
    class/module named B / M (qualified `A::B` first, then the lexically
    closest of several same-named ones, then the only one); unknown
    names (gems, stdlib) stay unresolved
  * `require_relative "x"` -> the file next to the requirer;
    `require "x"` -> `x.rb` under `lib/`, `app/` or the root (gems stay
    external); a `depends_on` file edge each
  * bare `foo(..)` that is not a method of the enclosing class chain ->
    the repo's only top-level `def foo`
  * `@account.sync_later` on an untyped receiver -> the repo's only method
    of that distinctive name (a concern's `def sync_later`), marked inferred

Member calls (implicit/explicit self, `Const.m`, `x = Const.new; x.m`)
are bound afterwards by graph/typed_callbind.py, which walks the
heritage edges minted here. Single targets only.
"""
from __future__ import annotations

import posixpath

METHOD = "ruby-resolve"
VERSION = "3"  # v3: Ruby/Rails core method names never bound by unique name; v2: untyped member calls to a distinctive repo-unique method (inferred)

# Ruby core / Rails methods a repo may also define (an override, a stub):
# an untyped `x.respond_to` / bare `respond_to do` is far more often the
# framework's, so these are never bound by the unique-name rule
_CORE_NAMES = frozenset("""
respond_to respond_to? redirect_to render_to_string find_or_create_by find_or_initialize_by
find_by find_each in_batches update_column update_columns update_attribute assign_attributes
has_many belongs_to has_one validates validate before_action after_action around_action
skip_before_action before_save after_save after_create before_create after_commit after_update
before_validation after_initialize after_destroy before_destroy attr_accessor attr_reader
attr_writer define_method instance_variable_get instance_variable_set send_data send_file
to_param as_json to_json with_lock deliver_later deliver_now perform_later perform_now
broadcast_replace_to broadcast_append_to broadcast_remove_to broadcast_update_to
broadcast_prepend_to broadcast_refresh_to url_for link_to button_to form_with content_tag
number_to_currency time_ago_in_words method_missing respond_to_missing? instance_of?
is_a? kind_of? each_with_object each_with_index group_by index_by sort_by min_by max_by
sum_by find_index flat_map filter_map to_sentence html_safe present? blank? presence
""".split())


def resolve_repo(repo_id: str, repo_root: str, file_set: set[str],
                 new_results: list, store, exclude_keys: set[str] | None = None,
                 fresh_keys: set[str] | None = None, prof=None) -> tuple[list, list, dict]:
    exclude_keys = exclude_keys or set()
    fresh_rels = {k.split(":", 1)[-1] for k in (fresh_keys or set())}
    if prof is None:
        from ..evaluation.stageprofile import make_profiler as _mp
        prof = _mp(False)
    prof.begin("ruby_collect")
    from ...core.models import Edge as EdgeCls
    from ...core.identifiers import file_node_id, canonical_edge_id
    from ...core.identifiers import site_fingerprint as _sf
    diag: dict = {"resolved_imports": 0, "external_imports": 0, "unresolved_imports": 0,
                  "depends_on_derived": 0, "resolved_calls": 0, "ambiguous_calls": 0,
                  "resolved_heritage": 0}

    def owner_rel(key: str) -> str:
        return key.split(":", 1)[-1]

    def file_id(rel: str) -> str:
        return file_node_id(repo_id, rel)

    def _attrs(d: dict) -> dict:
        import json as _json
        a = d.get("attributes") or {}
        if isinstance(a, str):
            try:
                a = _json.loads(a)
            except Exception:
                return {}
        return a if isinstance(a, dict) else {}

    rb_files = {f for f in file_set if f.lower().endswith((".rb", ".rake"))}
    rows: list = []
    classes: dict = {}        # label -> [(id, qname)]
    funcs: dict = {}          # label -> [id]   (top-level defs)
    methods_of: dict = {}     # class qname -> {method labels}
    meth_ids: dict = {}       # label -> [method ids] repo-wide

    def add_node(nid, t, label, qn, attrs):
        if t == "class":
            classes.setdefault(label, []).append((nid, qn))
        elif t == "function":
            funcs.setdefault(label, []).append(nid)
        elif t == "method":
            methods_of.setdefault(qn.rsplit(".", 1)[0], set()).add(label)
            meth_ids.setdefault(label, []).append(nid)

    repo_keys = sorted(k for k in store.list_file_keys()
                       if k.startswith(repo_id + ":") and k not in exclude_keys
                       and k.split(":", 1)[-1] not in fresh_rels)
    for key in repo_keys:
        if not owner_rel(key).lower().endswith((".rb", ".rake")):
            continue
        for nid in store.get_manifest_nodes(key):
            n = store.get_node(nid)
            if n is not None and n.language == "ruby":
                add_node(n.id, n.type, n.label, n.qualified_name or "", n.attributes or {})
        for eid in sorted(store.get_manifest_edges(key)):
            row = store.cx.execute("SELECT * FROM edges WHERE id=?", (eid,)).fetchone()
            if row is not None and (row["method"] or "").startswith("ts-ruby"):
                rows.append((key, dict(row)))
    for key, res in new_results:
        for n in getattr(res, "nodes", []):
            if getattr(n, "language", "") == "ruby":
                add_node(n.id, n.type, n.label, n.qualified_name or "", n.attributes or {})
        for e in res.edges:
            if (e.extraction_method or "").startswith("ts-ruby"):
                rows.append((key, {"id": e.id, "source": e.source, "target": e.target,
                                   "relation": e.relation, "method": e.extraction_method,
                                   "provenance": list(e.provenance_ids or []),
                                   "attributes": dict(e.attributes or {}),
                                   "file_path": e.file_path, "start_line": e.start_line}))
    prof.end("ruby_collect")
    extra: list = []

    def add_edge(owner_key, src, rel, tgt, src_row, attrs, site, cclass="resolved", score=0.9):
        from ...graph.builder import carry_occurrences as _carry
        attrs = _carry(attrs, src_row)
        e = EdgeCls(canonical_edge_id(src, rel, tgt, owner_rel(owner_key), site),
                    src, tgt, rel, cclass, cclass, score,
                    list(src_row.get("provenance", [])), METHOD, VERSION,
                    file_path=owner_rel(owner_key), start_line=src_row.get("start_line") or 0,
                    attributes=attrs)
        extra.append((owner_key, e))

    def find_class(name: str, qualified: str, from_qn: str):
        cands = classes.get(name, [])
        if not cands:
            return None
        if qualified and "." in qualified:
            exact = [c for c in cands if c[1] == qualified or c[1].endswith("." + qualified)]
            if len(exact) == 1:
                return exact[0][0]
        if len(cands) == 1:
            return cands[0][0]
        # Ruby constant lookup: the enclosing namespaces first
        scope = from_qn.split(".")
        for k in range(len(scope), -1, -1):
            want = ".".join(scope[:k] + [name])
            hit = [c for c in cands if c[1] == want]
            if len(hit) == 1:
                return hit[0][0]
        return None

    def class_qn_of(nid: str) -> str:
        for cs in classes.values():
            for i, q in cs:
                if i == nid:
                    return q
        return ""

    def has_method(cls_qn: str, name: str, seen=None) -> bool:
        return name in methods_of.get(cls_qn, set())

    def resolve_require(here: str, spec: str, relative: bool):
        base = spec[:-3] if spec.endswith(".rb") else spec
        cands = []
        if relative:
            cands.append(posixpath.normpath(posixpath.join(posixpath.dirname(here), base)) + ".rb")
        else:
            for root in ("lib", "app", "", "src"):
                cands.append(posixpath.normpath(posixpath.join(root, base)) + ".rb" if root else base + ".rb")
        for c in cands:
            if c in rb_files:
                return c
        return None

    prof.begin("ruby_bind")
    for key, d in rows:
        a = _attrs(d)
        rel = d["relation"]
        here = owner_rel(key)
        if rel == "imports":
            spec = a.get("specifier", "")
            tgt = resolve_require(here, spec, bool(a.get("relative")))
            if tgt is None:
                diag["external_imports" if not a.get("relative") else "unresolved_imports"] += 1
                continue
            diag["resolved_imports"] += 1
            add_edge(key, file_id(here), "depends_on", file_id(tgt), d,
                     {"specifier": spec, "via": "ruby-require"}, _sf("rbdep", spec))
            diag["depends_on_derived"] += 1
        elif rel in ("inherits", "implements"):
            name = a.get("superclass") or a.get("interface") or ""
            tgt = find_class(name, a.get("qualified", ""), class_qn_of(d["source"]))
            if tgt and tgt != d["source"]:
                diag["resolved_heritage"] += 1
                add_edge(key, d["source"], rel, tgt, d, {"via": "ruby-resolve", "name": name},
                         _sf("rbheritage", name))
        elif rel == "calls_unresolved" and a.get("callee_kind") == "function":
            name = a.get("base", "")
            owner_cls = a.get("receiver_owner", "")
            if owner_cls and has_method(owner_cls, name):
                continue           # a method of the class: typed_callbind binds it
            fs = funcs.get(name, [])
            if len(fs) == 1:
                diag["resolved_calls"] += 1
                add_edge(key, d["source"], "calls", fs[0], d, {"via": "ruby-resolve"}, _sf("rbcall", name))
            elif len(fs) > 1:
                diag["ambiguous_calls"] += 1
            else:
                # a concern calling a method its sibling concern defines
                ms = sorted(set(meth_ids.get(name, [])))
                if len(ms) == 1 and len(name) >= 8 and "_" in name.strip("_") and ms[0] != d["source"]                         and name not in _CORE_NAMES:
                    diag["resolved_calls"] += 1
                    add_edge(key, d["source"], "calls", ms[0], d,
                             {"via": "ruby-unique-name"}, _sf("rbcall", name), "inferred", 0.6)
        elif rel == "calls_unresolved" and a.get("callee_kind") in ("selector", "complex"):
            m = a.get("member", "")
            base = a.get("base", "")
            if not m or base[:1].isupper() or base == "self" or any(
                    a.get(k) for k in ("receiver_type", "receiver_owner", "receiver_static", "chain")):
                continue           # typed / constant / self receivers: typed_callbind
            ms = sorted(set(meth_ids.get(m, [])))
            if len(ms) == 1 and len(m) >= 8 and "_" in m.strip("_") and m not in _CORE_NAMES:
                diag["resolved_calls"] += 1
                add_edge(key, d["source"], "calls", ms[0], d,
                         {"via": "ruby-unique-name"}, _sf("rbmember", m), "inferred", 0.6)
    prof.end("ruby_bind")
    return [], extra, diag
