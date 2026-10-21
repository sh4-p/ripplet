"""Bind receiver-typed member calls for non-Python languages.

Consumes the syntactic annotations written by extraction.code.typed_params
and binds a `calls_unresolved` site only when the published symbol table
makes the target unique:

  receiver_owner  self/this (explicit, or implicit `m()` in a method of a
                  language with implicit this): `<Owner>.<m>`, else the
                  single `<OwnerLabel>.<m>` of the repo/language family.
  receiver_type   a parameter/local declared with type T: exactly one type
                  named T, exactly one matching `T.m`.
  chain           `X.a(..).b(..).m()`: each hop follows the callee's
                  *declared* return type (nested type of the declaring class
                  first, else the unique type of that name); the root is a
                  typed variable, self, or a type name (static call).
  arity           overloads are separate nodes; candidates are filtered by
                  the call's argument count against each signature's
                  [params_min, params_max] range.

Anything ambiguous after these filters stays unresolved. A site that a
per-language resolver already bound is skipped (no duplicate edges).
Go is excluded (go_resolve binds the same annotations package-scoped).
Pure recompute per run, like py_callbind: incremental == full rebuild.
"""
from __future__ import annotations

import collections
import json

from ..core.identifiers import canonical_edge_id, site_fingerprint
from ..core.models import Edge

METHOD = "typed-receiver"
VERSION = "44"  # v44: C# awaited call markers from the caller's class; v43: field paths rooted in markers, extension property types; v42: nested/local functions shadow class members for implicit calls; v41: result markers fall back to the declared return type (unique, then lexical); v40: chain hops through overloads agreeing on one result type; v39: an overload family of one type linked whatever its size (ambiguous); v38: inner classes read enclosing classes' fields; v37: same-file / imported function preferred for result markers; v36: ambiguous receiver type names narrowed to the one method they really have; v35: Rust loop elements of const/static collections; v34: chains rooted at a result marker; v33: Go pointer result types, field segments in result markers; v32: unresolved callback markers fall back to other rules, field walks through getters; v31: callback parameters typed by the callee's declared callback type; v30: declared types resolved through the declaring file's imports; v29: field paths rooted at a typed local/param (Go); v28: Go files a default build skips are never targets; v27: Go package-level variables type their receivers; v26: unresolved bases naming one repo type count as parents (Go embedding); v25: Go rows bound here too (result markers, interface methods, imports); v24: untyped receivers typed by the repo's own naming convention (inferred); v23: receiver types resolved through the file's own imports first; v22: Laravel Model::factory() chains reach <Model>Factory; v21: Swift extension methods on untyped receivers (distinctive names); v20: C# untyped locals calling a distinctive repo extension (app.MapDefaultEndpoints()); v19: chains rooted at a free function outside classes; v18: ambiguous field types narrowed to the one with the member; v17: multi-field receivers (self.a.b.m(), self.a.m().n()); v16: chain roots that are free functions (PHP user()->m()); v15: C# extension calls on untyped expression receivers (distinctive names); v14: Go calls through struct fields; v13: C# extension methods; v12: calls made inside test symbols are bound too; v11: C# qualified static calls and `new A.B()` locals; v10: in-scope extension functions on untyped receivers; v8: extension methods of external bases; Go package vars; v7: Swift argument-label overloads; v6: java+kotlin share one family; v5: locals typed through a free function's return type
TYPE_KINDS = ("class", "interface", "struct", "type", "trait", "enum", "object",
              "protocol", "record")
FAMILY = {"typescript": "ts", "tsx": "ts", "javascript": "ts",
          "cpp": "c", "c": "c", "java": "jvm", "kotlin": "jvm"}  # JVM interop: Java calls Kotlin
_CHAIN_MAX = 12
# Platform classes whose ancestry the repo cannot show: an extension the repo
# declares on an ancestor (`extension UIViewController`) applies to them.
_UIVC = ("UIViewController",)
_UIV = ("UIView",)
_KNOWN_BASES = {
    "UICollectionViewController": _UIVC, "UITableViewController": _UIVC,
    "UINavigationController": _UIVC, "UITabBarController": _UIVC,
    "UIPageViewController": _UIVC, "UISplitViewController": _UIVC,
    "UIAlertController": _UIVC, "UIHostingController": _UIVC,
    "UIScrollView": _UIV, "UITableView": ("UIScrollView", "UIView"),
    "UICollectionView": ("UIScrollView", "UIView"), "UITextView": ("UIScrollView", "UIView"),
    "UIImageView": _UIV, "UILabel": _UIV, "UIButton": ("UIControl", "UIView"),
    "UIControl": _UIV, "UIStackView": _UIV, "UITableViewCell": _UIV,
    "UICollectionViewCell": ("UICollectionReusableView", "UIView"),
    "NSCollectionView": ("NSView",), "NSTableView": ("NSControl", "NSView"),
    "AppCompatActivity": ("FragmentActivity", "ComponentActivity", "Activity", "Context"),
    "FragmentActivity": ("ComponentActivity", "Activity", "Context"),
    "ComponentActivity": ("Activity", "Context"), "Activity": ("Context",),
    "DialogFragment": ("Fragment",), "BottomSheetDialogFragment": ("DialogFragment", "Fragment"),
}


def _fam(lang: str) -> str:
    return FAMILY.get(lang or "", lang or "")


def _distinctive(name: str) -> bool:
    """Long enough and with two word breaks: `ShouldEqual`, `get_parameter_source`
    qualify; `Single`, `Add`, `First` (LINQ/BCL names) never do."""
    return len(name) >= 10 and sum(1 for ch in name if ch.isupper()) + name.count("_") >= 2


def _merge_site_occurrences(edges: list) -> None:
    """Several unresolved rows can bind to one edge id (same caller, callee
    and call shape); the store keeps the last row written. Give every copy
    the union of their call-site lines first so none is lost."""
    from collections import defaultdict
    by_id = defaultdict(list)
    for e in edges:
        by_id[e.id].append(e)
    for same in by_id.values():
        if len(same) < 2:
            continue
        occ = {}
        for e in same:
            for o in (e.attributes or {}).get("occurrences") or [{"line": e.start_line}]:
                if isinstance(o, dict) and isinstance(o.get("line"), int):
                    occ.setdefault(o["line"], o)
        merged = [occ[k] for k in sorted(occ)]
        for e in same:
            e.attributes = dict(e.attributes or {}, occurrences=merged)
            e.start_line = merged[0]["line"] if merged else e.start_line


def bind_typed_receivers(store) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    for i in range(0, len(old), 500):
        ch = old[i:i + 500]
        q = ",".join("?" * len(ch))
        cx.execute(f"DELETE FROM edges WHERE id IN ({q})", ch)
        cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)

    node_lang: dict[str, tuple[str, str]] = {}
    by_q: dict[tuple, list[str]] = {}
    types_by_label: dict[tuple, list[str]] = {}
    type_by_q: dict[tuple, str] = {}
    meth_by_suffix: dict[tuple, list[str]] = {}
    qn: dict[str, str] = {}
    sig: dict[str, tuple] = {}
    ptypes: dict[str, list] = {}
    fields: dict[str, dict] = {}
    label_of: dict[str, str] = {}
    ext_by_recv: dict[tuple, list[str]] = {}
    ext_by_label: dict[tuple, list[str]] = {}  # (repo, fam, name) -> extension functions
    ext_recv: dict[str, str] = {}  # extension function -> receiver type label
    func_labels: set = set()
    free_funcs: dict[tuple, list[str]] = {}
    explicit: set = set()  # C# explicit interface implementations
    plabels: dict[str, list] = {}  # Swift external argument labels
    node_file: dict[str, str] = {}
    cb_types: dict[str, dict] = {}      # function id -> {arg index: [callback param types]}
    from .go_build import inactive_go_files
    go_off = inactive_go_files(cx)       # build-tag variants a default build skips
    for nid, t, label, q, repo, lang, attrs, fp_ in cx.execute(
            "SELECT id, type, label, qualified_name, repository_id, language, attributes, file_path "
            "FROM nodes WHERE language <> 'python' AND type IN "
            "('function','method','class','interface','struct','type','trait',"
            "'enum','object','protocol','record','test','module')"):
        fam = _fam(lang)
        node_lang[nid] = (repo, fam)
        qn[nid] = q or ""
        label_of[nid] = label or ""
        node_file[nid] = (fp_ or "").replace("\\", "/")
        if go_off and lang == "go" and (fp_ or "").replace("\\", "/") in go_off:
            continue                     # a caller, never a target
        if t in ("function", "method"):
            by_q.setdefault((repo, fam, q), []).append(nid)
            func_labels.add((repo, fam, label))
            if t == "function":
                free_funcs.setdefault((repo, fam, label), []).append(nid)
            parts = (q or "").split(".")
            if len(parts) >= 2:
                meth_by_suffix.setdefault((repo, fam, parts[-2], parts[-1]), []).append(nid)
            try:
                d = json.loads(attrs or "{}")
            except ValueError:
                d = {}
            if d.get("param_labels") is not None:
                plabels[nid] = d["param_labels"]
            if d.get("explicit_interface"):
                explicit.add(nid)
            if d.get("cb_param_types"):
                cb_types[nid] = d["cb_param_types"]
            if "params_min" in d:
                sig[nid] = (d.get("params_min"), d.get("params_max"), d.get("returns") or "")
                ptypes[nid] = d.get("param_types") or []
            if fam == "swift" and t == "method" and ".extension." in (q or "") and not d.get("extension_receiver"):
                # `extension DeclModifierListSyntax { func m() }`: an extension
                # method of that (often external) type
                d = dict(d, extension_receiver=(q or "").split(".extension.", 1)[1].split(".")[0])
            if d.get("extension_receiver"):
                rlab = str(d["extension_receiver"]).split("<")[0].split(".")[-1].rstrip("?")
                ext_by_recv.setdefault((repo, fam, rlab, label), []).append(nid)
                ext_by_label.setdefault((repo, fam, label), []).append(nid)
                ext_recv[nid] = rlab
        elif t in TYPE_KINDS:
            types_by_label.setdefault((repo, fam, label), []).append(nid)
            type_by_q[(repo, fam, q)] = nid
            try:
                f = (json.loads(attrs or "{}") or {}).get("fields")
            except ValueError:
                f = None
            if f:
                fields[nid] = f

    # resolved heritage between repo types (any resolver), for member lookup
    parents: dict[str, list[str]] = {}
    ext_bases: dict[str, list[str]] = {}  # type -> external base/protocol labels
    for s_, t_, tl in cx.execute(
            "SELECT e.source, e.target, n.label FROM edges e LEFT JOIN nodes n ON n.id=e.target "
            "WHERE e.relation IN ('inherits','implements')"):
        if s_ in qn and t_ in qn and s_ != t_:
            parents.setdefault(s_, []).append(t_)
        elif s_ in qn and str(t_).startswith("ext:"):
            # placeholders often have no node row: the name is the id's tail
            b = str(tl or t_).split("<")[0].split(":")[-1].split(".")[-1].strip()
            ext_bases.setdefault(s_, []).extend((b,) + _KNOWN_BASES.get(b, ()))
            # an unresolved base that names exactly one repo type of the same
            # language (Go embedded structs, unresolved heritage spellings)
            if s_ in node_lang:
                r_, f_ = node_lang[s_]
                one = types_by_label.get((r_, f_, b), [])
                if len(one) == 1 and one[0] != s_:
                    parents.setdefault(s_, []).append(one[0])

    def mro(type_id: str) -> list:
        out, seen, queue = [], set(), [type_id]
        while queue and len(out) < 16:
            c = queue.pop(0)
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            queue += sorted(set(parents.get(c, [])))
        return out

    def fits(nid: str, arity) -> bool:
        s = sig.get(nid)
        if s is None or arity is None:
            return True
        lo, hi = s[0], s[1]
        return (lo is None or arity >= lo) and (hi is None or arity <= hi)

    _ANY = {"Object", "object", "Any", "any", "dynamic", "mixed", "T", "E", "K", "V", ""}

    def narrow(cands: list, arity, arg_types=None, labels=None) -> list:
        cands = sorted(set(cands))
        if len(cands) > 1 and labels:
            # Swift: the call's argument labels, in order, must be labels
            # the declaration has (`position(after:)` is not `position(of:)`)
            def fits_labels(c):
                it = iter(plabels.get(c) or [])
                return c in plabels and all(any(lb == p for p in it) for lb in labels)
            cands = [c for c in cands if fits_labels(c)] or cands
        if len(cands) > 1:
            # a class-typed receiver never reaches `void IFoo.M()`
            cands = [c for c in cands if c not in explicit] or cands
        if len(cands) <= 1 or arity is None:
            return cands
        cands = [c for c in cands if fits(c, arity)]
        if len(cands) > 1 and arg_types:
            # Overloads with the same arity: keep those whose declared
            # parameter types match every argument type the syntax states;
            # prefer exact matches. Unknown types never eliminate.
            def score(c):
                pt = ptypes.get(c) or []
                ex = 0
                for i, at in enumerate(arg_types):
                    if not at or i >= len(pt):
                        continue
                    if pt[i] == at:
                        ex += 1
                    elif pt[i] not in _ANY and len(pt[i]) > 1:
                        return -1
                return ex
            scored = [(score(c), c) for c in cands]
            ok = [x for x in scored if x[0] >= 0]
            if ok:
                best = max(x[0] for x in ok)
                cands = [c for sc, c in ok if sc == best]
        return cands

    def methods_on(repo, fam, type_q: str, m: str) -> list:
        """`m` on the type or, if it declares none, on its nearest base."""
        own = by_q.get((repo, fam, f"{type_q}.{m}"), [])
        tid = type_by_q.get((repo, fam, type_q))
        if own or tid is None:
            return own
        for b in mro(tid)[1:]:
            hit = by_q.get((repo, fam, f"{qn[b]}.{m}"), [])
            if hit:
                return hit
        return []

    def field_type(repo, fam, owner_q: str, name: str, bounds=None):
        tid = type_by_q.get((repo, fam, owner_q))
        if tid is None:
            return None, False
        for t_ in mro(tid):
            ft = (fields.get(t_) or {}).get(name)
            if ft:
                # `formatter: F` with `F: Formatter` in scope -> the trait
                ft = (bounds or {}).get(ft, ft)
                u = unique_type(repo, fam, ft)
                if u is None:
                    u = nearest_type(owner_q, by_label(repo, fam, ft))
                return u, True
        for t_ in mro(tid):
            # a typed property declared in an extension (Swift
            # `extension Application { var fileio: FileIO {..} }`)
            pt_ = ext_props.get((repo, fam, label_of.get(t_, ""), name))
            if pt_:
                return unique_type(repo, fam, pt_) or nearest_type(owner_q, by_label(repo, fam, pt_)), True
        if fam in ("jvm", "c_sharp", "csharp", "swift", "dart") and "." in owner_q:
            # an inner/nested class reads its enclosing class's fields
            # (JUnit @Nested tests using the outer test's `pet`)
            outer = owner_q.rsplit(".", 1)[0]
            if (repo, fam, outer) in type_by_q:
                return field_type(repo, fam, outer, name, bounds)
        return None, False

    def field_type_cands(repo, fam, owner_q: str, name: str, bounds=None):
        tid = type_by_q.get((repo, fam, owner_q))
        if tid is None:
            return []
        for t_ in mro(tid):
            ft = (fields.get(t_) or {}).get(name)
            if ft:
                return by_label(repo, fam, (bounds or {}).get(ft, ft))
        return []

    def nearest_type(owner_q: str, cands):
        """Among same-named types, the one sharing the longest module path
        with the owner (`Core.config: Config` -> searcher's own Config)."""
        if not cands:
            return None
        op = owner_q.split(".")

        def common(t):
            k = 0
            for x, y in zip(op, qn.get(t, "").split(".")):
                if x != y:
                    break
                k += 1
            return k
        scored = sorted(((common(t), t) for t in cands), reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def walk_fields(repo, fam, owner_q: str, names, bounds=None):
        """Type of `self.a.b`: each field's declared type in turn (a getter /
        property's declared return type where there is no field)."""
        cur_q, tid = owner_q, None
        for nm in names:
            tid, is_field = field_type(repo, fam, cur_q, nm, bounds)
            if tid is None:
                owner_t = type_by_q.get((repo, fam, cur_q))
                tid = member_type(repo, fam, owner_t, nm) if owner_t else None
            if tid is None:
                return None
            cur_q = qn.get(tid, "")
        return tid

    def by_label(repo, fam, label: str):
        """Types named `label`; `Policy`1` selects the generic arity among `Policy`s."""
        if "." in label and "`" not in label:
            # `Edit.Handler` / `Conduit.Features.Tags.TagCanonical`: the type
            # whose qualified name is, or ends with, that dotted name
            exact = type_by_q.get((repo, fam, label))
            if exact:
                return [exact]
            tail = label.rsplit(".", 1)[-1]
            return [t for t in types_by_label.get((repo, fam, tail), [])
                    if qn.get(t, "").endswith("." + label)]
        if "`" in label:
            base = label.split("`", 1)[0]
            return [t for t in types_by_label.get((repo, fam, base), [])
                    if qn.get(t, "").rsplit(".", 1)[-1] == label]
        return types_by_label.get((repo, fam, label), [])

    def unique_type(repo, fam, label: str):
        ts = by_label(repo, fam, label)
        if len(ts) > 1:
            # C# `Policy` vs `Policy`1`: a bare name means the non-generic one
            plain = [t for t in ts if "`" not in qn.get(t, "")]
            if len(plain) == 1 and len(ts) - 1 == sum("`" in qn.get(t, "") for t in ts):
                return plain[0]
        return ts[0] if len(ts) == 1 else None

    def return_type(repo, fam, meth: str):
        """Type node the method's declared return type names: a nested type
        of the declaring class first (`TypeSpec.Builder`), else unique."""
        s = sig.get(meth)
        if not s or not s[2]:
            return None
        rt = s[2].strip().lstrip("*&").split("[", 1)[0]     # Go `*ValueBinder`
        if "." in rt and fam == "go":
            rt = rt.rsplit(".", 1)[-1]                          # `echo.HTTPError`
        owner = qn[meth].rsplit(".", 1)[0]
        for cand in (f"{owner}.{rt}", f"{owner.rsplit('.', 1)[0]}.{rt}" if "." in owner else ""):
            if cand and (repo, fam, cand) in type_by_q:
                return type_by_q[(repo, fam, cand)]
        if rt == owner.rsplit(".", 1)[-1] and (repo, fam, owner) in type_by_q:
            return type_by_q[(repo, fam, owner)]  # fluent `return this`
        # several types of that name: the declaring file's import, then the
        # nearest namespace (lexical_type)
        return unique_type(repo, fam, rt) or lexical_type(repo, fam, meth, rt)

    def call_on_type(repo, fam, type_id: str, m: str, arity, arg_types=None, labels=None):
        c = narrow(methods_on(repo, fam, qn[type_id], m), arity, arg_types, labels)
        if not c:
            c = narrow(meth_by_suffix.get((repo, fam, label_of[type_id], m), []), arity, arg_types, labels)
        if not c:
            for t_ in mro(type_id):  # extension functions on the type or a base
                c = narrow(ext_by_recv.get((repo, fam, label_of[t_], m), []), arity, arg_types)
                if c:
                    break
        return c

    def lexical_type(repo, fam, src: str, name: str):
        """Type `name` as seen from symbol `src`: nested types of the
        enclosing classes first, then the longest shared qualified prefix
        (same namespace/package), then the unique type of that name."""
        name = name.split("<")[0].split("[")[0].split("::")[-1].split("\\")[-1].split(".")[-1].strip().lstrip("*&")
        ts = by_label(repo, fam, name)
        if not ts:
            return None
        if len(ts) == 1:
            return ts[0]
        # the declaring file imports that name: `get req(): HonoRequest` in
        # context.ts with `import { HonoRequest } from './request'`
        imp_ = imported_types.get((node_file.get(src, ""), name))
        if imp_ in ts:
            return imp_
        parts = qn.get(src, "").split(".")
        for i in range(len(parts) - 1, 0, -1):
            hit = type_by_q.get((repo, fam, ".".join(parts[:i]) + "." + name))
            if hit:
                return hit
        best, best_len, tie = None, -1, False
        for t_ in ts:
            tq = qn[t_].split(".")
            k = 0
            while k < min(len(tq) - 1, len(parts)) and tq[k] == parts[k]:
                k += 1
            if k > best_len:
                best, best_len, tie = t_, k, False
            elif k == best_len:
                tie = True
        return None if tie else best

    free_ids = {x for ids_ in free_funcs.values() for x in ids_}
    # (repo, fam, extended type label, property) -> declared type label
    ext_props: dict = {}
    for q_, repo_, lang_, attrs_ in cx.execute(
            "SELECT qualified_name, repository_id, language, attributes FROM nodes "
            "WHERE type='variable' AND qualified_name LIKE '%.extension.%' AND attributes LIKE '%var_type%'"):
        try:
            vt_ = (json.loads(attrs_ or "{}") or {}).get("var_type")
        except ValueError:
            vt_ = None
        tail_ = (q_ or "").split(".extension.", 1)[1].split(".")
        if vt_ and len(tail_) >= 2:
            ext_props.setdefault((repo_, _fam(lang_), tail_[-2], tail_[-1]), vt_)
    # (file, local name) -> the type node a resolver bound that import to
    imported_types: dict[tuple, str] = {}
    _amb: set = set()
    for fp, tgt, attrs in cx.execute("SELECT file_path, target, attributes FROM edges "
                                     "WHERE relation='imports' AND target NOT LIKE 'ext:%'"):
        if tgt not in label_of or tgt not in node_lang:
            continue
        try:
            ad = json.loads(attrs or "{}")
        except ValueError:
            ad = {}
        local = ad.get("local") or ad.get("imported") or label_of[tgt]
        k = ((fp or "").replace("\\", "/"), local)
        if k in imported_types and imported_types[k] != tgt:
            _amb.add(k)
        imported_types[k] = tgt
    for k in _amb:
        imported_types.pop(k, None)
    imported_funcs: dict[tuple, str] = {}
    for fp, tgt, attrs in cx.execute("SELECT file_path, target, attributes FROM edges "
                                     "WHERE relation='imports' AND target LIKE 'function:%'"):
        try:
            ad = json.loads(attrs or "{}")
        except ValueError:
            ad = {}
        imported_funcs[((fp or "").replace("\\", "/"), ad.get("local") or ad.get("imported") or label_of.get(tgt, ""))] = tgt

    owners: dict[str, str] = {}
    for eid, fk in cx.execute("SELECT edge_id, file_key FROM file_edges"):
        owners.setdefault(eid, fk)
    resolved_sites = set()
    for s, ln, t in cx.execute("SELECT e.source, e.start_line, n.label FROM edges e "
                               "JOIN nodes n ON n.id = e.target "
                               "WHERE e.relation IN ('calls','instantiates') AND e.method != ?",
                               (METHOD,)):
        resolved_sites.add((s, ln, t))

    # Name convention: how the repository itself annotates a receiver name.
    # `(c: Context) =>` written 57 times out of 58 makes an untyped callback
    # parameter `c` (`app.get('/', (c) => c.json(..))`, typed only by
    # contextual inference) a Context. Used only when nothing else types the
    # receiver, only for a dominant repo type, and the member must exist on
    # it; edges are marked inferred.
    _conv_cnt: dict = {}
    for src_, fp_, attrs_ in cx.execute("SELECT source, file_path, attributes FROM edges WHERE attributes LIKE "
                                        "'%receiver_type%' AND attributes LIKE '%\"base\"%'"):
        if src_ not in node_lang:
            continue
        try:
            d_ = json.loads(attrs_ or "{}")
        except ValueError:
            continue
        b_, t_ = str(d_.get("base") or ""), str(d_.get("receiver_type") or "")
        if not b_ or not t_ or "." in b_ or t_.startswith("()") or b_ in ("this", "self", "super"):
            continue
        t_ = t_.split("<")[0]
        r_, f_ = node_lang[src_]
        # the type node this annotation meant, as the binder resolves it
        tid_ = imported_types.get(((fp_ or "").replace("\\", "/"), t_)) or unique_type(r_, f_, t_) or ("?" + t_)
        _conv_cnt.setdefault((r_, f_, b_), collections.Counter())[tid_] += 1
    conv: dict = {}
    for k_, cnt_ in _conv_cnt.items():
        tot_ = sum(cnt_.values())
        tid_, n_ = cnt_.most_common(1)[0]
        if tot_ >= 5 and n_ >= 0.9 * tot_ and not tid_.startswith("?"):
            conv[k_] = tid_

    # Go package-level variables: (repo, package dir, name) -> type node
    go_vars: dict = {}
    nfile_of = {i_: (f_ or "").replace("\\", "/") for i_, f_ in cx.execute(
        "SELECT id, file_path FROM nodes WHERE language='go' AND type IN ('class','interface','type','struct')")}
    for vlab_, vrepo_, vfp_, vattrs_ in cx.execute("SELECT label, repository_id, file_path, attributes FROM nodes "
                                                  "WHERE type='variable' AND language='go' "
                                                  "AND attributes LIKE '%var_type%'"):
        try:
            vt_ = json.loads(vattrs_ or "{}").get("var_type") or ""
        except ValueError:
            continue
        if (vfp_ or "").replace("\\", "/") in go_off:
            continue
        vdir_ = (vfp_ or "").replace("\\", "/").rpartition("/")[0]
        cands_ = types_by_label.get((vrepo_, "go", vt_), [])
        same_ = [c for c in cands_ if (nfile_of.get(c) or "").rpartition("/")[0] == vdir_]
        pick_ = same_[0] if len(same_) == 1 else (cands_[0] if len(cands_) == 1 else None)
        if pick_:
            go_vars[(vrepo_, vdir_, vlab_)] = pick_

    # Rust const/static collections: (repo, name) -> element type label
    rust_elems: dict = {}
    for vlab_, vrepo_, vattrs_ in cx.execute("SELECT label, repository_id, attributes FROM nodes "
                                             "WHERE type='variable' AND language='rust' "
                                             "AND attributes LIKE '%elem_type%'"):
        try:
            et_ = json.loads(vattrs_ or "{}").get("elem_type") or ""
        except ValueError:
            continue
        k_ = (vrepo_, vlab_)
        rust_elems[k_] = et_ if rust_elems.get(k_, et_) == et_ else ""   # differing same-named: unknown

    def hop_result(repo, fam, cur, hc, field_name):
        """Type after one chain hop: the method's declared result; overloads
        agreeing on one result (`append(..)` x40 -> ToStringBuilder) count as
        one; no method: the field/getter `field_name`."""
        if len(hc) == 1:
            return return_type(repo, fam, hc[0])
        if hc:
            rts = {return_type(repo, fam, x) for x in hc}
            return rts.pop() if len(rts) == 1 else None
        return member_type(repo, fam, cur, field_name) if field_name else None

    def marker_type(repo, fam, row, T: str):
        """Type a result marker names: `()f` (f's declared result), `()A.b.c`
        (A a type / function / package, then methods' results or fields),
        `=v` (a Go package variable)."""
        if T.startswith("[]"):
            et_ = rust_elems.get((repo, T[2:])) or ""
            return lexical_type(repo, fam, row["source"], et_) if et_ else None
        if T.startswith("="):
            return go_vars.get((repo, (row["file_path"] or "").replace("\\", "/").rpartition("/")[0], T[1:]))
        segs = T[2:].split(".")
        cur = imported_types.get(((row["file_path"] or "").replace("\\", "/"), segs[0]))             or unique_type(repo, fam, segs[0])
        rest = segs[1:]
        if cur is None:
            fns = [imported_funcs[k] for k in [((row["file_path"] or "").replace("\\", "/"), segs[0])]
                   if k in imported_funcs] or free_funcs.get((repo, fam, segs[0]), [])
            if not fns and len(segs) >= 2:                  # package qualifier
                fns, rest = free_funcs.get((repo, fam, segs[1]), []), segs[2:]
            cur = return_type(repo, fam, fns[0]) if len(fns) == 1 else None
        for mm in rest:
            if cur is None:
                break
            hc = call_on_type(repo, fam, cur, mm, None)
            cur = hop_result(repo, fam, cur, hc, mm)
        return cur

    def resolve_cb(repo, fam, row, marker: str):
        """`()cb:stream:1:0`: the type the callee declares for parameter 0 of
        its callback argument 1, or None."""
        try:
            _, cal_, ai_, pj_ = marker.split(":", 3)
            j_ = int(pj_)
        except ValueError:
            return None
        fn_ = imported_funcs.get(((row["file_path"] or "").replace("\\", "/"), cal_))
        if fn_ is None:
            fs_ = free_funcs.get((repo, fam, cal_), [])
            fn_ = fs_[0] if len(fs_) == 1 else None
        lst_ = (cb_types.get(fn_) or {}).get(ai_) if fn_ else None
        if not lst_ or j_ >= len(lst_) or not lst_[j_]:
            return None
        return lexical_type(repo, fam, fn_, lst_[j_])

    def member_type(repo, fam, tid, name):
        """Type of `x.name` for x of type tid: a field's type, else a getter's
        (zero-parameter method's) declared return type."""
        t_, is_f = field_type(repo, fam, qn.get(tid, ""), name)
        if t_ is not None:
            return t_
        meths = [x for x in call_on_type(repo, fam, tid, name, 0)]
        if len(meths) == 1:
            return return_type(repo, fam, meths[0])
        return None

    stats = {"candidates": 0, "bound": 0, "ambiguous": 0, "chain": 0, "instantiates": 0}
    new: list[tuple[Edge, str]] = []
    rows = cx.execute(
        # Go is bound by go_resolve, except calls through a struct field
        # (`cmd.parent.m()`), which need the field table kept here
        "SELECT * FROM edges WHERE relation='calls_unresolved' AND method != 'python-call-resolution' "
        "AND (attributes LIKE '%receiver_type%' "
        "OR attributes LIKE '%receiver_owner%' OR attributes LIKE '%receiver_static%' "
        "OR attributes LIKE '%receiver_field%' OR attributes LIKE '%receiver_name%' OR attributes LIKE '%field_path%' "
        "OR attributes LIKE '%\"callee_kind\": \"path\"%' "
        "OR attributes LIKE '%\"chain\"%' OR attributes LIKE '%\"qualifier\"%' "
        "OR (method IN ('ts-c_sharp','ts-swift') AND attributes LIKE '%\"callee_kind\": \"complex\"%') "
        "OR (method IN ('ts-c_sharp','ts-go') AND attributes LIKE '%\"callee_kind\": \"selector\"%') "
        "OR (method LIKE 'jsts-%' AND attributes LIKE '%\"callee_kind\": \"member\"%'))").fetchall()
    for row in rows:
        try:
            a = json.loads(row["attributes"] or "{}")
        except ValueError:
            continue
        src = row["source"]
        if src not in node_lang:
            continue
        repo, fam = node_lang[src]
        if a.get("callee_kind") == "path" and a.get("root", "")[:1].isupper() \
                and a.get("member") and not a.get("receiver_static"):
            a["receiver_static"] = a["root"]
        m = a.get("member") or a.get("implicit_member") or ""
        if not m or (src, row["start_line"], m) in resolved_sites:
            continue
        arity = a.get("arity")
        argt = a.get("arg_types")
        labs = a.get("arg_labels")
        stats["candidates"] += 1
        tgt, via, cands = None, "", []
        if str(a.get("receiver_type") or "").startswith("()cb:"):
            a = dict(a)
            cbt_ = resolve_cb(repo, fam, row, a["receiver_type"])
            if cbt_ is None:
                a.pop("receiver_type")          # `(c: any) => ..`: fall back to the other rules
            else:
                a["_cb_tid"] = cbt_
        base_s = str(a.get("base") or "")
        conv_t = None
        if base_s and not any(a.get(k) for k in ("receiver_type", "receiver_owner", "receiver_static",
                                                  "receiver_field", "receiver_name", "field_path",
                                                  "chain", "qualifier", "import_binding",
                                                  "receiver_local")):
            segs = base_s.split(".")
            conv_t = conv.get((repo, fam, segs[0]))
            for sg in segs[1:]:                     # `c.req.json()`: Context.req -> HonoRequest
                if conv_t is None:
                    break
                conv_t = member_type(repo, fam, conv_t, sg)
        gv_t = None
        if fam == "go" and base_s and "." not in base_s and not a.get("receiver_type")                 and not a.get("receiver_local"):
            gv_t = go_vars.get((repo, (row["file_path"] or "").replace("\\", "/").rpartition("/")[0], base_s))
        if gv_t is not None:
            cands, via = call_on_type(repo, fam, gv_t, m, arity, argt, labs), "package-var"
        elif conv_t is not None:
            cands, via = call_on_type(repo, fam, conv_t, m, arity, argt, labs), "name-convention"
        elif a.get("chain"):
            cur = None
            hops = list(a["chain"] or [])
            if a.get("chain_root_owner") and a.get("chain_root_fields"):
                cur = walk_fields(repo, fam, a["chain_root_owner"], a["chain_root_fields"],
                                  a.get("type_bounds"))
            elif a.get("chain_root_owner"):
                cur = type_by_q.get((repo, fam, a["chain_root_owner"]))
                first = hops[0].get("m", "") if hops else ""
                if first and (cur is None or not call_on_type(repo, fam, cur, first, hops[0].get("n"))):
                    # PHP/C `user()->m()`: the root is a free function, not a
                    # method of the enclosing class: its declared return type
                    fns = free_funcs.get((repo, fam, first), [])
                    if len(fns) == 1 and (sig.get(fns[0]) or (0, 0, ""))[2]:
                        cur = lexical_type(repo, fam, fns[0], sig[fns[0]][2])
                        hops = hops[1:]
            elif a.get("chain_root_type"):
                crt_ = str(a["chain_root_type"])
                cur = marker_type(repo, fam, row, crt_) if crt_.startswith(("()", "="))                     else (imported_types.get(((row["file_path"] or "").replace("\\", "/"), crt_))
                          or unique_type(repo, fam, crt_))
            elif a.get("chain_root_static"):
                cur = unique_type(repo, fam, a["chain_root_static"])
            elif hops and not a.get("base"):
                # file-level `user()->isGuest()` (views, scripts): the root
                # call is a free function; its declared return type
                fns = free_funcs.get((repo, fam, hops[0].get("m", "")), [])
                if len(fns) == 1 and (sig.get(fns[0]) or (0, 0, ""))[2]:
                    cur = lexical_type(repo, fam, fns[0], sig[fns[0]][2])
                    hops = hops[1:]
            for hop in hops[:_CHAIN_MAX]:
                if cur is None:
                    break
                hc = call_on_type(repo, fam, cur, hop.get("m", ""), hop.get("n"))
                if not hc and fam == "php" and hop.get("m") == "factory":
                    # Laravel `Song::factory()` (HasFactory, external): by
                    # convention the model's `SongFactory`
                    cur = unique_type(repo, fam, label_of.get(cur, "") + "Factory")
                    continue
                cur = hop_result(repo, fam, cur, hc, None)
            if cur is not None:
                cands, via = call_on_type(repo, fam, cur, m, arity, argt, labs), "chain"
        elif a.get("field_path"):
            fo_ = a.get("field_owner", "")
            if not fo_ and a.get("field_root_type"):
                frt_ = a["field_root_type"]
                if frt_.startswith("()cb:"):        # `withApp { app in app.fileio.m() }`
                    rt_ = resolve_cb(repo, fam, row, frt_)
                elif frt_.startswith("()"):
                    rt_ = marker_type(repo, fam, row, frt_)
                else:
                    rt_ = imported_types.get(((row["file_path"] or "").replace("\\", "/"), frt_)) \
                        or unique_type(repo, fam, frt_)
                fo_ = qn.get(rt_, "") if rt_ else ""
            tid = walk_fields(repo, fam, fo_, a["field_path"], a.get("type_bounds"))
            if tid is not None:
                cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "field"
        elif a.get("receiver_field") or a.get("receiver_name"):
            nm = a.get("receiver_field") or a.get("receiver_name")
            tid, is_field = field_type(repo, fam, a.get("field_owner", ""), nm,
                                       a.get("type_bounds"))
            if is_field and tid is None:
                # several types share the field's type name: the one that has
                # this member (`_basket.AddItem()`, two `Basket` classes)
                hits = [t for t in field_type_cands(repo, fam, a.get("field_owner", ""), nm,
                                                    a.get("type_bounds"))
                        if any(narrow(methods_on(repo, fam, qn[u], m), arity, argt, labs)
                               for u in mro(t))]
                if len(hits) == 1:
                    tid = hits[0]
            if is_field:
                if tid is not None:
                    cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "field"
            elif a.get("receiver_name") and nm[:1].isupper():
                tid = unique_type(repo, fam, nm)
                if tid is not None:
                    cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "static"
        elif a.get("receiver_owner"):
            owner = a["receiver_owner"]
            # a function nested in the caller (C# local function, Kotlin/Swift
            # nested fun) shadows the class's members
            local_ = by_q.get((repo, fam, f"{qn.get(src, '')}.{m}"), []) if a.get("implicit_member") else []
            cands = narrow(local_, arity, argt, labs) if local_ else \
                narrow(methods_on(repo, fam, owner, m), arity, argt, labs)
            if not cands and not methods_on(repo, fam, owner, m):
                cands = narrow(meth_by_suffix.get((repo, fam, owner.rsplit(".", 1)[-1], m), []), arity, argt, labs)
            if not cands:
                # `class VC: UIViewController` calling a method the repo adds
                # in `extension UIViewController { ... }` (external base).
                tid = type_by_q.get((repo, fam, owner))
                for t_ in (mro(tid) if tid else []):
                    for base_label in ext_bases.get(t_, []):
                        hit = meth_by_suffix.get((repo, fam, base_label, m), []) + \
                            ext_by_recv.get((repo, fam, base_label, m), [])
                        if hit:
                            cands = narrow(hit, arity, argt, labs)
                            break
                    if cands:
                        break
            via = "self"
        elif a.get("receiver_type"):
            T = a["receiver_type"]
            T = (a.get("type_bounds") or {}).get(T, T)  # `x: &F`, `F: Trait`
            marker_tid = None
            if T.startswith("[]"):
                marker_tid, T = marker_type(repo, fam, row, T), ""
            elif T.startswith("()cb:"):
                marker_tid, T = a.get("_cb_tid"), ""
            elif T.startswith("=") and fam == "go":
                # `b := Uri`: the package variable's type
                marker_tid = go_vars.get((repo, (row["file_path"] or "").replace("\\", "/").rpartition("/")[0], T[1:]))
                T = T[1:]
            elif T.startswith("()") and "." in T[2:] and fam == "go":
                # Go result markers: `()Engine.allocateContext` (a method's result
                # on a typed receiver), `()New.allocateContext` (New()'s result,
                # then the method), `()gin.New` (a package function's result)
                segs = T[2:].split(".")
                cur = unique_type(repo, fam, segs[0])
                rest = segs[1:]
                if cur is None:
                    fns = free_funcs.get((repo, fam, segs[0]), [])
                    if not fns and len(segs) >= 2:      # package qualifier
                        fns, rest = free_funcs.get((repo, fam, segs[1]), []), segs[2:]
                    cur = lexical_type(repo, fam, fns[0], sig[fns[0]][2]) \
                        if len(fns) == 1 and (sig.get(fns[0]) or (0, 0, ""))[2] else None
                for mm in rest:
                    if cur is None:
                        break
                    hc = call_on_type(repo, fam, cur, mm, None)
                    # `()New.router`: a field when it is not a method
                    cur = hop_result(repo, fam, cur, hc, mm)
                marker_tid = cur
                T = segs[-1]
            elif T.startswith("()") and fam in ("c_sharp", "csharp"):
                # `var b = await _env.BasketService.GetBasketAsync()` /
                # `await GetIt()`: from the caller's class — its field (or
                # static type), then each property / method result in turn
                segs = T[2:].split(".")
                owner_q_ = qn.get(src, "").rsplit(".", 1)[0]
                own_t = type_by_q.get((repo, fam, owner_q_))
                cur = None
                if len(segs) == 1 and own_t is not None:
                    hc = call_on_type(repo, fam, own_t, segs[0], None)
                    cur = return_type(repo, fam, hc[0]) if len(set(hc)) == 1 else None
                elif own_t is not None:
                    cur, _f = field_type(repo, fam, owner_q_, segs[0])
                    if cur is None and segs[0][:1].isupper():
                        cur = unique_type(repo, fam, segs[0])
                    for mm in segs[1:]:
                        if cur is None:
                            break
                        hc = call_on_type(repo, fam, cur, mm, None)
                        cur = hop_result(repo, fam, cur, hc, mm)
                marker_tid, T = cur, ""
            elif T.startswith("()"):
                T = T[2:]
            # the class this file imports under that name first (four `Hono`
            # classes in hono; `import { Hono } from './hono'` says which)
            imp_ = imported_types.get(((row["file_path"] or "").replace("\\", "/"), T.split("<")[0]))
            if imp_ is not None and imp_ in free_ids:
                imp_ = None          # `use function Tests\create_playlist`: not a type
            tid = marker_tid or imp_ \
                or (unique_type(repo, fam, T) if not a["receiver_type"].startswith("()") or "." not in T
                    else None)
            if tid is None and "." in T:
                # `Edit.Handler` in Articles and in Users: the one whose
                # namespace shares most segments with the caller
                cs = by_label(repo, fam, T)
                here = set(qn.get(src, "").split("."))
                ranked = sorted(cs, key=lambda c: -len(here & set(qn.get(c, "").split("."))))
                if len(ranked) > 1 and len(here & set(qn[ranked[0]].split("."))) > \
                        len(here & set(qn[ranked[1]].split("."))):
                    tid = ranked[0]
            if tid is None and not by_label(repo, fam, T):
                # `val c = make()` / `c := make()`: no type of that name, so
                # follow the unique free function's declared return type
                # (the same file's, or the imported one, when several exist).
                fns = free_funcs.get((repo, fam, T), [])
                if len(fns) > 1:
                    here_f = (row["file_path"] or "").replace("\\", "/")
                    fns = [x for x in fns if node_file.get(x) == here_f] or                         [x for x in [imported_funcs.get((here_f, T))] if x] or fns
                if len(fns) == 1 and (sig.get(fns[0]) or (0, 0, ""))[2]:
                    tid = lexical_type(repo, fam, fns[0], sig[fns[0]][2]) or return_type(repo, fam, fns[0])
                    if tid is None:
                        # an ambiguous result type name: narrowed below by the method
                        T = sig[fns[0]][2].split("<")[0].strip().lstrip("*&")
            if tid is not None:
                cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "param-type"
                if not cands:
                    T = label_of.get(tid, T)       # `type H3 = ..` has no methods: narrow by its name
            if not cands and len(by_label(repo, fam, T)) > 1:
                # several types of that name (a class, a `declare class`, an
                # IIFE-scoped class): the methods they really have, through
                # their bases, must all be one method
                own = set()
                for c_ in by_label(repo, fam, T):
                    for u_ in mro(c_):
                        own |= set(narrow(methods_on(repo, fam, qn.get(u_, ""), m), arity, argt, labs))
                if len(own) == 1:
                    cands, via = sorted(own), "param-type-narrowed"
                else:
                    stats["ambiguous"] += 1
        elif a.get("receiver_static"):
            tid = imported_types.get(((row["file_path"] or "").replace("\\", "/"), a["receiver_static"])) \
                or unique_type(repo, fam, a["receiver_static"])
            if tid is not None:
                cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "static"
        elif a.get("qualifier"):
            # fully qualified static call: the dotted name must be a repo type
            tid = unique_type(repo, fam, a["qualifier"])
            if tid is not None:
                cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "static"
        if not cands and m and (a.get("base") or a.get("qualifier")) and not a.get("receiver_owner") \
                and a.get("base") not in ("this", "self", "super"):
            # `source.startsWithGzipMagicNumber()` with the receiver's type
            # unknown: the one extension function of that name whose
            # declaring scope (package or class) encloses the caller. A
            # receiver typed as some other repo type never takes this path.
            T = a.get("receiver_type") or ""
            base_ = str(a.get("base") or "")
            if fam in ("c_sharp", "csharp") and base_[:1].isupper() and "." not in base_ and not T:
                # `Guard.AgainstNull(x)`: a repo class named like the receiver,
                # unless the enclosing class has a member of that name (`Name.Trim()`)
                owner_q = qn.get(src, "").rsplit(".", 1)[0]
                owner_t = type_by_q.get((repo, fam, owner_q))
                if base_ not in (fields.get(owner_t) or {}):
                    tid = unique_type(repo, fam, base_)
                    if tid is not None:
                        cands, via = call_on_type(repo, fam, tid, m, arity, argt, labs), "static"
        complex_cs = fam in ("c_sharp", "csharp", "swift") and a.get("callee_kind") == "complex" \
            and not a.get("base") and not a.get("qualifier") and _distinctive(m)
        # `app.MapDefaultEndpoints()` with `var app = builder.Build()` (an
        # external type): an untyped local binds only a distinctive name
        untyped_local = fam in ("c_sharp", "csharp") and a.get("callee_kind") == "selector" \
            and str(a.get("base") or "")[:1].islower() and not a.get("receiver_type") \
            and not a.get("receiver_name")
        if untyped_local and not _distinctive(m):
            pass
        elif not cands and m and (a.get("base") or a.get("qualifier") or complex_cs) and not a.get("receiver_owner") \
                and a.get("base") not in ("this", "self", "super"):
            # `result.Errors[0].ErrorCode.ShouldEqual(..)`: the receiver is an
            # expression we cannot type; an extension method visible through
            # `using` with a distinctive, repo-unique name is the only candidate
            T = a.get("receiver_type") or ""
            exts = narrow(ext_by_label.get((repo, fam, m), []), arity, argt, labs)
            base_ = str(a.get("base") or "")
            if len(exts) > 1 and fam in ("c_sharp", "csharp") and \
                    len({qn.get(x, "").rsplit(".", 1)[0] for x in exts}) == 1:
                exts_family = exts       # overloads of one extension class: linked as ambiguous below
            else:
                exts_family = None
            rlab_ = ext_recv.get(exts[0], "") if exts else ""
            static_other = base_[:1].isupper() and base_ != rlab_  # `TermUi.echo()`: an object's own member
            if exts_family and not static_other and not (T and unique_type(repo, fam, T) is not None):
                cands, via = exts_family, "extension"
            elif len(exts) == 1 and not static_other and not (T and unique_type(repo, fam, T) is not None):
                scope = qn.get(exts[0], "").rsplit(".", 1)[0]
                here = qn.get(src, "")
                # C# extension methods are visible through `using`, not nesting:
                # the repo's only extension of that name binds
                if fam in ("c_sharp", "csharp", "swift") or scope and (here == scope or here.startswith(scope + ".")):
                    cands, via = exts, "extension"
        targets, cclass, score = [], "resolved", 0.85
        if via == "name-convention":
            cclass, score = "inferred", 0.6
        if len(cands) == 1:
            targets = cands
        elif len(cands) > 1:
            stats["ambiguous"] += 1
            owners_ = {qn[c].rsplit(".", 1)[0] for c in cands}
            if len(owners_) == 1:
                # Same method family on one type, overload not decidable from
                # syntax: link every candidate, marked ambiguous (never
                # presented as a resolved single target).
                targets, cclass, score = cands, "ambiguous", 0.5
        if not targets:
            continue
        tgt = targets[0]
        if via == "chain":
            stats["chain"] += 1
        # No line numbers in identity (project rule): textually identical
        # sites in one scope share one logical edge, like other resolvers.
        # Identity = call shape (how the receiver was established), never a
        # line: `this.m.send()` and `get().send()` in one method are two
        # sites even though both bind S.send (they collided before).
        site = site_fingerprint("recv", via, a.get("base") or "", m, str(arity),
                                str(a.get("receiver_field") or a.get("receiver_name") or ""),
                                json.dumps(a.get("chain") or [], sort_keys=True),
                                str(a.get("receiver_type") or a.get("receiver_static") or ""))
        attrs = {"via": via, "receiver": a.get("receiver_type") or a.get("receiver_owner")
                 or a.get("chain_root_static") or a.get("chain_root_type"),
                 "base": a.get("base"), "member": m}
        if arity is not None:
            attrs["arity"] = arity
        if a.get("occurrences"):
            attrs["occurrences"] = a["occurrences"]   # every call site, not just the first
        if cclass == "ambiguous":
            attrs["overload_candidates"] = len(targets)
        for tgt in targets:
            e = Edge(canonical_edge_id(src, "calls", tgt, row["file_path"] or "", site),
                     src, tgt, "calls", cclass, cclass, score,
                     json.loads(row["provenance"] or "[]"), METHOD, VERSION,
                     file_path=row["file_path"], start_line=row["start_line"],
                     attributes=attrs)
            new.append((e, owners.get(row["id"], "")))
        stats["bound"] += 1
    ctor_rows = cx.execute(
        "SELECT * FROM edges WHERE relation IN ('instantiates','calls_unresolved') "
        "AND target LIKE 'ext:%' AND method NOT IN ('python-call-resolution','ts-go')").fetchall()
    have_inst = {(r[0], r[1]) for r in cx.execute(
        "SELECT source, start_line FROM edges WHERE relation='instantiates' AND target NOT LIKE 'ext:%'")}
    for row in ctor_rows:
        src = row["source"]
        if src not in node_lang:
            continue
        repo, fam = node_lang[src]
        try:
            a = json.loads(row["attributes"] or "{}")
        except ValueError:
            a = {}
        if row["relation"] == "calls_unresolved":
            # Kotlin/Swift/Dart/C++ `T(...)`: a bare call that names a type
            # and no function of that name exists on the path.
            if a.get("member") or not a.get("base") or a.get("callee_kind") not in (
                    "function", "identifier", None) or (row["method"] or "").removeprefix(
                    "ts-") not in ("kotlin", "swift", "dart", "c", "cpp"):  # never Java
                continue
            name = a["base"]
            if (repo, fam, name) in func_labels:
                continue  # a function of that name exists: not provably a ctor
        else:
            name = a.get("created") or a.get("class") or row["target"].split(":")[-1]
        if (src, row["start_line"]) in have_inst and row["relation"] == "calls_unresolved":
            continue
        tid = lexical_type(repo, fam, src, name or "")
        if tid is None:
            continue
        site = site_fingerprint("new", name or "", str(a.get("arity")))
        e = Edge(canonical_edge_id(src, "instantiates", tid, row["file_path"] or "", site),
                 src, tid, "instantiates", "resolved", "resolved", 0.85,
                 json.loads(row["provenance"] or "[]"), METHOD, VERSION,
                 file_path=row["file_path"], start_line=row["start_line"],
                 attributes=dict({"via": "constructor", "created": name},
                                 **({"occurrences": a["occurrences"]} if a.get("occurrences") else {})))
        new.append((e, owners.get(row["id"], "")))
        cx.execute("INSERT OR IGNORE INTO superseded_edges VALUES(?,?)", (row["id"], e.id))
        stats["instantiates"] += 1
    _merge_site_occurrences([e for e, _ in new])
    store.upsert_edges([e for e, _ in new])
    for e, fk in new:
        if fk:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    return stats
