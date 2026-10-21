"""Receiver typing from syntax for member calls (precision pass, all langs).

A member call `p.m()` can only be bound when the receiver's type is stated
by the program itself. Two syntactic sources qualify:

  * a parameter declared with a single named type (`A p`, `p: A`, `A* p`,
    `A $p`, `p: &A`, `p: A?`), and
  * the method's own receiver (`self`/`this`) — recorded as the owner's
    qualified name.

This module only *annotates* `calls_unresolved` edges produced by the
per-language visitors (`receiver_type` / `receiver_owner`); binding happens
in graph.typed_callbind against the published symbol table, so a file never
binds to a guess about another file. Generic, qualified, union, array or
inferred types are rejected; a name declared with two different types in
one symbol (e.g. a closure parameter shadowing) is dropped entirely.
"""
from __future__ import annotations

import re

PARAM_KINDS = {
    "rust": ("parameter",),
    "java": ("formal_parameter",),
    "c_sharp": ("parameter",),
    "php": ("simple_parameter", "property_promotion_parameter"),
    "kotlin": ("parameter", "class_parameter"),
    "swift": ("parameter", "lambda_parameter"),
    "dart": ("formal_parameter",),
    "cpp": ("parameter_declaration", "optional_parameter_declaration"),
    "c": ("parameter_declaration",),
    "gdscript": ("typed_parameter", "typed_default_parameter"),
    "go": ("parameter_declaration",),
    "typescript": ("required_parameter", "optional_parameter"),
    "tsx": ("required_parameter", "optional_parameter"),
}
SELF_WORDS = {"self", "this", "$this"}
_CPP_TYPE_NODES = ("type_identifier", "template_type", "qualified_identifier")


# `new X(..)` (Java/TS/C#/Dart) or PHP `(new \App\X($a))` at a chain root
_NEW_ROOT = re.compile(r"\(?\s*new\s+\\?([A-Za-z_][\w.\\]*)")


def _php_container(node, raw) -> str:
    """`app(X::class)` / `resolve(X::class)`: the instance's class X."""
    from .ts_universal import _php_container_type
    return _php_container_type(node, raw) if node is not None else ""


def _cpp_label(node, raw) -> str:
    """`ns::circular_q<T>` -> circular_q, `std::shared_ptr<logger>` -> logger,
    std containers -> '' (shared with the extractor's field typing)."""
    from .ts_universal import _cpp_type_label
    return _cpp_type_label(node, raw)


# Languages where a bare `m()` inside a method may mean `this.m()`.
IMPLICIT_THIS = ("java", "c_sharp", "kotlin", "swift", "dart", "cpp", "gdscript")
# `Type.m()` is a static call by syntax/convention; C# is excluded because
# PascalCase properties (`Name.Trim()`) look the same.
STATIC_TYPE_CALLS = ("java", "kotlin", "dart", "typescript", "tsx", "swift", "php", "cpp")
# Nested function-like scopes whose parameters must not leak into the
# enclosing symbol (calls inside them are attributed to that symbol, but
# the same name outside the closure is a different variable).
_INNER_SCOPES = ("lambda", "closure", "anonymous", "arrow_function",
                 "function_expression", "local_function", "function_literal",
                 "anonymous_function", "lambda_literal")
_NAME_KINDS = ("identifier", "simple_identifier", "variable_name")
_WRAPPERS = ("reference_type", "pointer_type", "nullable_type", "optional_type",
             "user_type", "named_type", "type", "type_annotation",
             "implicitly_unwrapped_type", "abstract_reference_declarator")
_SMART_POINTERS = {"Box", "Rc", "Arc", "Cow", "Pin", "shared_ptr", "unique_ptr"}
_REJECT = ("type_arguments", "type_parameters", "generic_type", "generic_name",
           "qualified_name", "scoped_type_identifier", "union_type", "array_type",
           "function_type", "tuple_type", "template_type", "qualified_identifier",
           "predefined_type", "primitive_type", "dynamic_type")


def _txt(n, raw: bytes) -> str:
    return raw[n.start_byte:n.end_byte].decode("utf-8", "ignore")


def _simple_type(t, raw: bytes) -> str:
    if t is not None and t.type == "generic_name":
        # C# `Policy<TResult>`: the generic type, named like its node (`Policy`1`)
        ident = next((c for c in t.children if c.type == "identifier"), None)
        args = next((c for c in t.children if c.type == "type_argument_list"), None)
        if ident is not None and args is not None:
            n = sum(1 for c in args.children if c.is_named)
            return f"{_txt(ident, raw)}`{n}" if n else ""
        return ""
    if t is not None and t.type == "union_type":
        # `TrieRouter<true> | undefined`, `Foo | null`: one type plus "nothing"
        parts = [c for c in t.children if c.is_named]
        real = [c for c in parts if not (c.type == "literal_type" and _txt(c, raw) in ("undefined", "null"))
                and _txt(c, raw) not in ("undefined", "null", "void")]
        return _simple_type(real[0], raw) if len(real) == 1 and len(parts) > 1 else ""
    if t is not None and t.type == "bounded_type":
        # Rust `dyn ParallelVisitor + 's`: the trait, not the lifetime bound
        first = next((c for c in t.children if c.is_named and c.type not in ("lifetime",)), None)
        return _simple_type(first, raw) if first is not None else ""
    if t is not None and t.type in ("dynamic_type", "abstract_type"):
        # Rust `&dyn Flag` / `impl Flag`: the trait's methods
        inner = next((c for c in t.children if c.type in ("type_identifier", "scoped_type_identifier",
                                                          "generic_type")), None)
        if inner is None:
            return ""
        if inner.type == "scoped_type_identifier":
            return _txt(inner, raw).rsplit("::", 1)[-1]
        return _simple_type(inner, raw)
    if t is not None and t.type == "generic_type":
        # `Router<string>`, `List<Pet>`, `Vec<T>`: the generic class's methods
        # do not depend on the arguments; the binder still requires a repo type
        base = next((c for c in t.children if c.type in ("type_identifier", "identifier")), None)
        name = _txt(base, raw) if base is not None else ""
        if name in _SMART_POINTERS:
            # `Box<dyn Flag>`, `Arc<Searcher>`: calls go to the pointee
            args = next((c for c in t.children if c.type in ("type_arguments", "type_argument_list")), None)
            inner = [c for c in args.children if c.is_named] if args is not None else []
            return _simple_type(inner[0], raw) if len(inner) == 1 else ""
        return name
    if t is None or t.type in _REJECT:
        return ""
    if t.type in ("type_identifier", "identifier", "name"):
        return _txt(t, raw)
    if t.type in _WRAPPERS:
        kids = [c for c in t.children if c.is_named]
        if any(c.type in _REJECT and c.type not in ("generic_type", "union_type", "dynamic_type", "bounded_type")
               for c in kids):
            return ""
        if t.type == "user_type" and len(kids) != 1:
            return ""  # dotted (Outer.Inner) or generic
        for c in kids:
            r = _simple_type(c, raw)
            if r:
                return r
    return ""


def _declarator_name(n, raw: bytes) -> str:
    if n.type in ("identifier", "field_identifier"):
        return _txt(n, raw)
    if n.type.endswith("declarator"):
        for c in n.children:
            r = _declarator_name(c, raw)
            if r:
                return r
    return ""


def _param(lang: str, n, raw: bytes) -> list[tuple[str, str]]:
    named = [c for c in n.children if c.is_named]
    if lang == "php":
        tnode = next((c for c in named if c.type in ("named_type", "optional_type")), None)
        var = next((c for c in named if c.type == "variable_name"), None)
        if tnode is None or var is None:
            return []
        t = _simple_type(tnode, raw)
        return [("$" + _txt(var, raw).lstrip("$"), t)] if t else []
    if lang == "c_sharp":
        ids = [c for c in named if c.type == "identifier"]
        tnode = next((c for c in named if c.type in ("nullable_type", "generic_name")), None)
        if len(ids) == 2 and tnode is None:
            return [(_txt(ids[1], raw), _txt(ids[0], raw))]
        if len(ids) == 1 and tnode is not None:
            t = _simple_type(tnode, raw)
            return [(_txt(ids[0], raw), t)] if t else []
        return []
    if lang in ("cpp", "c"):
        tnode = next((c for c in named if c.type in _CPP_TYPE_NODES), None)
        decl = next((c for c in named if c.type == "identifier" or
                     c.type.endswith("declarator")), None)
        if tnode is None or decl is None:
            return []
        name = _declarator_name(decl, raw)
        t = _cpp_label(tnode, raw)
        return [(name, t)] if name and t else []
    if lang == "go":
        names = [_txt(c, raw) for c in named if c.type == "identifier"]
        tnode = next((c for c in named if c.type not in ("identifier", "comment")), None)
        t = _simple_type(tnode, raw)
        return [(x, t) for x in names] if t else []
    # identifier-then-type (rust, kotlin, swift, gdscript, ts) and
    # type-then-identifier (java, dart) shapes
    names = [c for c in named if c.type in _NAME_KINDS]
    types = [c for c in named if c.type not in _NAME_KINDS and
             c.type not in ("comment", "modifiers", "annotation", "default_value",
                            "parameter_modifiers", "accessibility_modifier")]
    if lang == "rust" and not names:
        pat = next((c for c in named if c.type == "mut_pattern"), None)
        if pat is not None:
            names = [c for c in pat.children if c.type == "identifier"]
            types = [c for c in types if c is not pat]
    if not names or len(types) != 1:
        return []
    if lang == "swift":
        name = _txt(names[-1], raw)  # external label first, internal name last
    elif lang in ("java", "dart"):
        name = _txt(names[-1], raw)
    else:
        name = _txt(names[0], raw)
    t = _simple_type(types[0], raw)
    return [(name, t)] if t else []


LOCAL_KINDS = {
    "java": ("local_variable_declaration", "enhanced_for_statement"),
    "c_sharp": ("variable_declaration",),
    "kotlin": ("property_declaration",),
    "swift": ("property_declaration",),
    "dart": ("initialized_variable_definition",),
    "rust": ("let_declaration", "for_expression"),
    "go": ("var_spec", "short_var_declaration"),
    "cpp": ("declaration",),
    "c": ("declaration",),
    "php": ("assignment_expression",),
    "typescript": ("variable_declarator",),
    "tsx": ("variable_declarator",),
}
_RUST_CTORS = ("new", "default")


def _ctor_type(lang: str, v, raw: bytes) -> str:
    """Type constructed by an initializer expression, or "". Only forms that
    name the type syntactically; the binder additionally requires the name
    to be a declared type (so `C()` that is really a function never binds)."""
    if v is None:
        return ""
    t = v.type
    named = [c for c in v.children if c.is_named]
    if t in ("object_creation_expression", "new_expression"):
        qn_ = next((c for c in named if c.type == "qualified_name"), None)
        if lang == "c_sharp" and qn_ is not None and not any(c.type == "generic_name" for c in qn_.children):
            return _txt(qn_, raw).replace(" ", "")   # `new Edit.Handler(..)`: resolved by qn suffix
        tn = next((c for c in named if c.type in ("type_identifier", "identifier", "name")), None)
        # `new LinearRouter<string>()`: type arguments do not change the class
        if tn is not None and not any(c.type in _REJECT and c.type not in ("type_arguments",)
                                      for c in named):
            return _txt(tn, raw)
        return ""
    if t == "call_expression" and lang in ("kotlin", "swift", "cpp"):
        fn = named[0] if named else None
        if fn is not None and fn.type in ("identifier", "simple_identifier"):
            return _txt(fn, raw)
        return ""
    if t == "call_expression" and lang == "rust":
        fn = named[0] if named else None
        if fn is not None and fn.type == "scoped_identifier":
            ids = [c for c in fn.children if c.type in ("identifier", "type_identifier")]
            if len(ids) == 2 and _txt(ids[1], raw) in _RUST_CTORS:
                return _txt(ids[0], raw)
        return ""
    if t == "struct_expression":
        tn = next((c for c in named if c.type == "type_identifier"), None)
        return _txt(tn, raw) if tn is not None else ""
    if t == "composite_literal":
        tn = next((c for c in named if c.type == "type_identifier"), None)
        return _txt(tn, raw) if tn is not None else ""
    if t == "unary_expression" and lang == "go":
        inner = next((c for c in named if c.type == "composite_literal"), None)
        return _ctor_type(lang, inner, raw)
    return ""


def _call_marker(lang: str, v, raw: bytes) -> str:
    """`()f` for a local assigned from a bare same-scope call `f(...)`: the
    binder types it by f's declared return type (unique f only)."""
    if v is not None and lang == "c_sharp":
        if v.type == "await_expression":
            v = next((c for c in v.children if c.is_named), None)
        if v is not None and v.type == "invocation_expression":
            # `await _env.BasketService.GetBasketAsync()` / `GetIt()`: the
            # path from the caller's field (or the method itself) to the call
            fn = v.child_by_field_name("function")
            txt = re.sub(r"\s+", "", _txt(fn, raw)) if fn is not None else ""
            txt = txt[5:] if txt.startswith("this.") else txt
            if re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", txt):
                return "()" + txt
        return ""
    if v is None or v.type not in ("call_expression", "function_call_expression"):
        return ""
    fn = v.child_by_field_name("function")
    if fn is not None and fn.type in ("identifier", "name", "qualified_name"):
        # PHP `user()` or a namespaced `App\user()`: the function's own name
        return "()" + _txt(fn, raw).rsplit("\\", 1)[-1]
    return ""


_RUST_COLLECTIONS = {"Vec", "VecDeque", "HashSet", "BTreeSet", "LinkedList", "BinaryHeap"}


def _rust_elem_type(t, raw: bytes) -> str:
    """Element type of a Rust collection type: `&[&dyn Flag]`, `[T; 3]`,
    `Vec<Box<T>>` -> the element's simple type."""
    while t is not None and t.type == "reference_type":
        t = next((c for c in t.children if c.is_named and c.type != "lifetime"), None)
    if t is None:
        return ""
    if t.type == "array_type":
        el = next((c for c in t.children if c.is_named), None)
        return _simple_type(el, raw) if el is not None else ""
    if t.type == "generic_type":
        base = next((c for c in t.children if c.type == "type_identifier"), None)
        if base is not None and _txt(base, raw) in _RUST_COLLECTIONS:
            args = next((c for c in t.children if c.type == "type_arguments"), None)
            inner = [c for c in args.children if c.is_named] if args is not None else []
            return _simple_type(inner[0], raw) if len(inner) == 1 else ""
    return ""


def _rust_for_iter(v, raw: bytes):
    """`FLAGS.iter()` / `&flags` / `flags.into_iter()` -> the collection expression."""
    while v is not None:
        if v.type == "reference_expression":
            v = next((c for c in v.children if c.is_named), None)
        elif v.type == "call_expression":
            fn = v.child_by_field_name("function")
            fld = fn.child_by_field_name("field") if fn is not None and fn.type == "field_expression" else None
            if fld is not None and _txt(fld, raw) in ("iter", "into_iter", "iter_mut"):
                v = fn.child_by_field_name("value")
            else:
                return v
        else:
            return v
    return None


def _go_assert_type(v, raw: bytes) -> str:
    """`engine.pool.Get().(*Context)`: the asserted type."""
    if v is None or v.type != "type_assertion_expression":
        return ""
    tn = [c for c in v.children if c.is_named]
    t = _simple_type(tn[-1], raw) if len(tn) >= 2 else ""
    return t.lstrip("*").rsplit(".", 1)[-1] if t else ""


def _go_method_marker(v, raw: bytes) -> str:
    """`router.allocateContext(0)` / `gin.New()`: `()router.allocateContext`;
    the receiver is typed afterwards from the same scope (`()Engine.allocateContext`),
    a package qualifier stays (the binder takes that package's function)."""
    if v is not None and v.type == "selector_expression":
        fn = v                          # `r := e.router`: a field of a typed local
    elif v is None or v.type != "call_expression":
        return ""
    else:
        fn = v.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return ""
    op = fn.child_by_field_name("operand")
    fld = fn.child_by_field_name("field")
    if op is None or fld is None or op.type != "identifier":
        return ""
    return f"(){_txt(op, raw)}.{_txt(fld, raw)}"


def _local(lang: str, n, raw: bytes) -> list[tuple[str, str]]:
    named = [c for c in n.children if c.is_named]
    out: list[tuple[str, str]] = []
    if lang == "java" and n.type == "enhanced_for_statement":
        # `for (Pet pet : getPets())`: the declared loop variable
        tnode = n.child_by_field_name("type")
        nm = n.child_by_field_name("name")
        t = _simple_type(tnode, raw) if tnode is not None else ""
        return [(_txt(nm, raw), t)] if nm is not None and t and t != "var" else []
    if lang == "java":
        # `final Pair<IDKey, IDKey> pair = ..`: the type follows the modifiers
        tnode = n.child_by_field_name("type") or next((c for c in named if c.type != "modifiers"), None)
        declared = _simple_type(tnode, raw) if tnode is not None else ""
        if declared == "var":
            declared = ""
        for d in (c for c in named if c.type == "variable_declarator"):
            dn = [c for c in d.children if c.is_named]
            if dn and dn[0].type == "identifier":
                t = declared or _ctor_type(lang, dn[-1] if len(dn) > 1 else None, raw)
                if t:
                    out.append((_txt(dn[0], raw), t))
        return out
    if lang == "c_sharp":
        tnode = named[0] if named else None
        declared = _txt(tnode, raw) if tnode is not None and tnode.type == "identifier" else ""
        if tnode is not None and tnode.type in ("nullable_type", "generic_name"):
            declared = _simple_type(tnode, raw)
        for d in (c for c in named if c.type == "variable_declarator"):
            dn = [c for c in d.children if c.is_named]
            if dn and dn[0].type == "identifier":
                init_ = dn[-1] if len(dn) > 1 else None
                t = (declared if declared not in ("var", "implicit_type") else "") \
                    or _ctor_type(lang, init_, raw) or _call_marker(lang, init_, raw)
                if t:
                    out.append((_txt(dn[0], raw), t))
        return out
    if lang == "kotlin":
        vd = next((c for c in named if c.type == "variable_declaration"), None)
        if vd is None:
            return []
        ids = [c for c in vd.children if c.type == "identifier"]
        ut = next((c for c in vd.children if c.type in ("user_type", "nullable_type")), None)
        if not ids:
            return []
        t = _simple_type(ut, raw) if ut is not None else \
            _ctor_type(lang, next((c for c in named if c.type == "call_expression"), None), raw)
        return [(_txt(ids[0], raw), t)] if t else []
    if lang == "swift":
        pat = next((c for c in named if c.type == "pattern"), None)
        nm = next((c for c in pat.children if c.type == "simple_identifier"), None) if pat else None
        if nm is None:
            return []
        ta = next((c for c in named if c.type == "type_annotation"), None)
        t = _simple_type(ta, raw) if ta is not None else \
            _ctor_type(lang, next((c for c in named if c.type == "call_expression"), None), raw)
        return [(_txt(nm, raw), t)] if t else []
    if lang == "dart":
        tn = named[0] if named and named[0].type == "type_identifier" else None
        ids = [c for c in named if c.type == "identifier"]
        if not ids:
            return []
        t = _txt(tn, raw) if tn is not None else ""
        if not t:
            ne = next((c for c in named if c.type == "new_expression"), None)
            if ne is not None:
                t = _ctor_type(lang, ne, raw)
            elif len(ids) >= 2 and any(c.type == "selector" for c in named):
                t = _txt(ids[1], raw)  # `final c = C()`: binder checks C is a type
        return [(_txt(ids[0], raw), t)] if t else []
    if lang == "rust":
        if n.type == "for_expression":
            # `for flag in FLAGS.iter()`: an element of a const/static collection,
            # resolved by the binder (`[]FLAGS`)
            pat = n.child_by_field_name("pattern")
            coll = _rust_for_iter(n.child_by_field_name("value"), raw)
            if pat is not None and pat.type == "identifier" and coll is not None and coll.type == "identifier":
                return [(_txt(pat, raw), "[]" + _txt(coll, raw))]
            return []
        nm = named[0] if named and named[0].type == "identifier" else None
        if nm is None:
            return []
        tnode = next((c for c in named[1:] if c.type in
                      ("type_identifier", "reference_type")), None)
        t = _simple_type(tnode, raw) if tnode is not None else ""
        if not t and len(named) >= 2:
            t = _ctor_type(lang, named[-1], raw) or _call_marker(lang, named[-1], raw)
        return [(_txt(nm, raw), t)] if t else []
    if lang == "go":
        if n.type == "var_spec":
            names = [c for c in named if c.type == "identifier"]
            tnode = next((c for c in named if c.type in ("type_identifier", "pointer_type")), None)
            t = _simple_type(tnode, raw) if tnode is not None else ""
            return [(_txt(x, raw), t) for x in names] if t else []
        lists = [c for c in named if c.type == "expression_list"]
        if len(lists) != 2:
            return []
        left = [c for c in lists[0].children if c.is_named]
        right = [c for c in lists[1].children if c.is_named]
        if len(right) == 1 and len(left) > 1 and left[0].type == "identifier":
            # `c, err := New()`: c is the callee's first declared result;
            # `c, ok := v.(*Context)`: the asserted type
            mk = _go_assert_type(right[0], raw) or _call_marker(lang, right[0], raw)                 or _go_method_marker(right[0], raw)
            return [(_txt(left[0], raw), mk)] if mk else []
        if len(left) != len(right):
            return []
        for l, r in zip(left, right):
            if l.type == "identifier":
                t = _ctor_type(lang, r, raw) or _go_assert_type(r, raw) or _call_marker(lang, r, raw)                     or _go_method_marker(r, raw)                     or ("=" + _txt(r, raw) if r.type == "identifier" and _txt(r, raw) not in ("nil", "true", "false")
                        else "")
                if t:
                    out.append((_txt(l, raw), t))
        return out
    if lang in ("cpp", "c"):
        tn = next((c for c in named if c.type not in ("storage_class_specifier", "type_qualifier",
                                                       "attribute_declaration", "comment")), None)
        declared = _cpp_label(tn, raw) if tn is not None and tn.type in _CPP_TYPE_NODES else ""
        named = named[named.index(tn):] if tn is not None else named
        for d in named[1:]:
            if d.type == "identifier" and declared:
                out.append((_txt(d, raw), declared))
            elif d.type == "init_declarator":
                dn = [c for c in d.children if c.is_named]
                name = _declarator_name(dn[0], raw) if dn else ""
                t = declared or _ctor_type(lang, dn[-1] if len(dn) > 1 else None, raw)
                if name and t:
                    out.append((name, t))
        return out
    if lang == "php":
        var = named[0] if named and named[0].type == "variable_name" else None
        if var is None or len(named) < 2:
            return []
        # Dynamic language: any later non-`new` assignment may change the
        # type, so it is reported as "" and treated as a conflict.
        return [("$" + _txt(var, raw).lstrip("$"),
                 _ctor_type(lang, named[1], raw) or _php_container(named[1], raw)
                 or _call_marker(lang, named[1], raw))]
    if lang in ("typescript", "tsx"):
        nm = named[0] if named and named[0].type == "identifier" else None
        if nm is None:
            return []
        ta = next((c for c in named if c.type == "type_annotation"), None)
        init = named[-1] if len(named) > 1 else None
        t = _simple_type(ta, raw) if ta is not None else \
            (_ctor_type(lang, init, raw) or _call_marker(lang, init, raw))
        return [(_txt(nm, raw), t)] if t else []
    return []


PARAM_LISTS = ("formal_parameters", "parameter_list", "function_value_parameters",
               "parameters", "formal_parameter_list")
CALL_KINDS = ("method_invocation", "invocation_expression", "call_expression",
              "member_call_expression", "function_call_expression",
              "scoped_call_expression", "nullsafe_member_call_expression")
ARG_KINDS = ("argument_list", "arguments", "value_arguments")
_TRAIL_NAME = None


FIELD_KINDS = {
    "java": ("field_declaration",),
    "c_sharp": ("field_declaration", "property_declaration"),
    "kotlin": ("class_parameter", "property_declaration"),
    "swift": ("property_declaration",),
    "rust": ("field_declaration",),
    "go": ("field_declaration",),
    "php": ("property_declaration", "property_promotion_parameter"),
    "cpp": ("field_declaration",),
    "c": ("field_declaration",),
    "typescript": ("public_field_definition", "required_parameter", "optional_parameter"),
    "tsx": ("public_field_definition", "required_parameter", "optional_parameter"),
    "dart": ("declaration",),
}
TYPE_NODE_KINDS = ("class", "struct", "interface", "type", "object", "record", "trait", "enum")
_MODS = ("modifiers", "modifier", "visibility_modifier", "accessibility_modifier",
         "final_builtin", "const_builtin", "annotation", "marker_annotation", "attribute_list")


def _field(lang: str, n, raw: bytes) -> list[tuple[str, str]]:
    """(name, declared type) for one class/struct member declaration."""
    named = [c for c in n.children if c.is_named and c.type not in _MODS and c.type != "comment"]
    if lang == "java":
        tnode = named[0] if named else None
        t = _simple_type(tnode, raw) if tnode is not None else ""
        return [(_txt(d.children[0], raw), t) for d in named
                if d.type == "variable_declarator" and d.children] if t else []
    if lang == "c_sharp":
        if n.type == "field_declaration":
            vd = next((c for c in named if c.type == "variable_declaration"), None)
            return _local("c_sharp", vd, raw) if vd is not None else []
        ids = [c for c in named if c.type == "identifier"]
        tnode = named[0] if named else None
        if len(ids) >= 2 and tnode is ids[0]:
            return [(_txt(ids[1], raw), _txt(ids[0], raw))]
        if tnode is not None and tnode.type in ("nullable_type", "generic_name") and ids:
            t = _simple_type(tnode, raw)
            return [(_txt(ids[0], raw), t)] if t else []
        return []
    if lang == "kotlin":
        if n.type == "class_parameter":
            if not any(c.type in ("val", "var", "binding_pattern_kind") for c in n.children) \
                    and not _txt(n, raw).lstrip().split(" ")[0] in ("val", "var") \
                    and " val " not in f" {_txt(n, raw)} " and " var " not in f" {_txt(n, raw)} ":
                return []
            return _param("kotlin", n, raw)
        return _local("kotlin", n, raw)
    if lang == "swift":
        return _local("swift", n, raw)
    if lang == "go":
        # `parent *Command` / `a, b Handler`; embedded fields (no name) skipped
        names = [c for c in named if c.type == "field_identifier"]
        tnode = next((c for c in named if c.type not in ("field_identifier", "tag")), None)
        t = _simple_type(tnode, raw) if tnode is not None else ""
        t = t.lstrip("*").split(".")[-1] if t else ""
        return [(_txt(nm, raw), t) for nm in names] if t else []
    if lang == "rust":
        nm = next((c for c in named if c.type == "field_identifier"), None)
        tnode = next((c for c in named if c is not nm), None)
        t = _simple_type(tnode, raw) if tnode is not None else ""
        return [(_txt(nm, raw), t)] if nm is not None and t else []
    if lang == "php":
        tnode = next((c for c in named if c.type in ("named_type", "optional_type")), None)
        t = _simple_type(tnode, raw) if tnode is not None else ""
        if not t:
            return []
        out = []
        for c in n.children:
            v = c if c.type == "variable_name" else next(
                (g for g in c.children if g.type == "variable_name"), None) \
                if c.type == "property_element" else None
            if v is not None:
                out.append((_txt(v, raw).lstrip("$"), t))
        return out
    if lang in ("cpp", "c"):
        tnode = next((c for c in named if c.type in _CPP_TYPE_NODES), None)
        if tnode is None or not _cpp_label(tnode, raw):
            return []
        out = []
        for c in named:
            if c is tnode:
                continue
            nm = c if c.type == "field_identifier" else None
            if nm is None and c.type.endswith("declarator"):
                stack = [c]
                while stack and nm is None:
                    x = stack.pop()
                    if x.type == "field_identifier":
                        nm = x
                    stack.extend(x.children)
            if nm is not None:
                out.append((_txt(nm, raw), _cpp_label(tnode, raw)))
        return out
    if lang in ("typescript", "tsx"):
        if n.type != "public_field_definition" and not any(
                c.type == "accessibility_modifier" or c.type == "readonly" for c in n.children):
            return []  # plain parameter, not a parameter property
        nm = next((c for c in n.children if c.type in ("property_identifier", "identifier")), None)
        ta = next((c for c in n.children if c.type == "type_annotation"), None)
        t = _simple_type(ta, raw) if ta is not None else ""
        return [(_txt(nm, raw), t)] if nm is not None and t else []
    if lang == "dart":
        tnode = next((c for c in named if c.type == "type_identifier"), None)
        lst = next((c for c in named if c.type == "initialized_identifier_list"), None)
        if tnode is None or lst is None:
            return []
        out = []
        for ii in lst.children:
            ident = next((g for g in ii.children if g.type == "identifier"), None) \
                if ii.type == "initialized_identifier" else None
            if ident is not None:
                out.append((_txt(ident, raw), _txt(tnode, raw)))
        return out
    return []


_LITERAL_TYPES = {
    "string_literal": "String", "string": "String", "interpreted_string_literal": "string",
    "raw_string_literal": "string", "template_string": "string", "line_string_literal": "String",
    "class_literal": "Class", "true": "boolean", "false": "boolean",
    "decimal_integer_literal": "int", "integer_literal": "int", "number": "number",
}


def _arg_types(lang: str, args, raw: bytes) -> list:
    """Per argument: a simple type name when the syntax states it, "@name"
    for a bare identifier (resolved later against params/locals), else None."""
    out = []
    for c in args.children:
        if not c.is_named or c.type == "comment":
            continue
        x = c
        if x.type in ("argument", "value_argument") and x.named_child_count == 1:
            x = x.named_children[0]
        if x.type in _LITERAL_TYPES:
            out.append(_LITERAL_TYPES[x.type])
        elif x.type in ("object_creation_expression", "new_expression"):
            out.append(_ctor_type(lang, x, raw) or None)
        elif x.type in ("identifier", "simple_identifier"):
            out.append("@" + _txt(x, raw))
        else:
            out.append(None)
    return out


def _fn_info(lang: str, fn, raw: bytes) -> dict:
    """{min, max (None = variadic), returns} for a function/method node."""
    if lang == "swift":
        plist = fn
        # `setOnly: Bool = false`: the default is a sibling after the parameter
        kids = list(fn.children)
        params = [("opt", c) if i + 1 < len(kids) and kids[i + 1].type == "=" else c
                  for i, c in enumerate(kids) if c.type == "parameter"]
    else:
        plist = fn.child_by_field_name("parameters")
        if plist is None:
            stack = list(fn.children)
            while stack and plist is None:
                c = stack.pop(0)
                if c.type in PARAM_LISTS:
                    plist = c
                elif c.type in ("function_signature", "method_signature", "function_declarator",
                                "pointer_declarator", "reference_declarator"):
                    stack = list(c.children) + stack
        if plist is None:
            return {}
        params = []
        vararg_next = False
        for c in plist.children:
            if not c.is_named or c.type in ("comment", "self_parameter", "line_comment",
                                             "block_comment"):
                continue
            if c.type in ("optional_formal_parameters",):  # dart [..] / {..}
                params += [("opt", g) for g in c.children if g.is_named]
                continue
            if c.type == "parameter_modifiers":  # kotlin: modifiers precede the parameter
                vararg_next = vararg_next or "vararg" in _txt(c, raw)
                continue
            if vararg_next:
                params.append(("vararg", c))
                vararg_next = False
                continue
            params.append(c)
    total = mins = 0
    variadic = False
    ptypes: list = []
    for p in params:
        opt = False
        if isinstance(p, tuple):
            if p[0] == "vararg":
                variadic = True
                continue
            opt, p = True, p[1]
        txt = _txt(p, raw)
        if p.type in ("variadic_parameter", "spread_parameter", "rest_pattern",
                      "variadic_parameter_declaration") or "..." in txt.split("=")[0] \
                or txt.startswith(("vararg ", "params ")) or " vararg " in txt:
            variadic = True
            continue
        n = 1
        if lang == "go":
            n = max(1, sum(1 for g in p.children if g.type == "identifier"))
        try:
            pt = [t for _, t in _param(lang, p, raw)] if lang in PARAM_KINDS else []
        except Exception:
            pt = []
        ptypes += (pt + [""] * n)[:n]
        total += n
        if not opt and "=" not in txt and p.type not in ("optional_parameter",
                                                           "optional_parameter_declaration",
                                                           "typed_default_parameter"):
            mins += n
    ret = None
    if lang == "swift":
        seen = False
        for c in fn.children:
            if c.type == "->":
                seen = True
            elif seen and c.is_named:
                ret = c
                break
    elif lang == "kotlin":
        seen = False
        for c in fn.children:
            if c.type == ":":
                seen = True
            elif seen and c.is_named:
                ret = c
                break
    elif lang == "dart":
        sig = fn if fn.type.endswith("signature") else None
        for c in (sig.children if sig is not None else []):
            if c.type in ("type_identifier",):
                ret = c
                break
            if c.type == "identifier":
                break
    else:
        for f in ("type", "returns", "return_type", "result"):
            ret = fn.child_by_field_name(f)
            if ret is not None:
                break
        if lang == "go" and ret is not None and ret.type == "parameter_list":
            # `(T, error)`: the first result is what `x, err := f()` types x as
            first = next((c for c in ret.children if c.type == "parameter_declaration"), None)
            ret = first.child_by_field_name("type") if first is not None else None
    if lang == "c_sharp" and ret is not None and ret.type == "generic_name":
        # `async Task<CustomerBasket> GetBasketAsync()`: callers `await` it, so
        # the result type is the task's argument
        gid = next((c for c in ret.children if c.type == "identifier"), None)
        targs = next((c for c in ret.children if c.type == "type_argument_list"), None)
        inner = [c for c in targs.children if c.is_named] if targs is not None else []
        if gid is not None and _txt(gid, raw) in ("Task", "ValueTask") and len(inner) == 1:
            ret = inner[0]
    rt = _simple_type(ret, raw) if ret is not None else ""
    if lang == "php" and rt in ("", "mixed"):
        # `@return mixed|SettingService` above `function setting(): mixed`:
        # the one class type the docblock names
        doc = fn.prev_named_sibling
        if doc is not None and doc.type == "comment":
            m = re.search(r"@return\s+([^\s*]+)", _txt(doc, raw))
            cls_ = [t.lstrip("\\?").rsplit("\\", 1)[-1] for t in (m.group(1).split("|") if m else [])]
            cls_ = [t for t in cls_ if t[:1].isupper() and t not in ("Closure",)]
            if len(cls_) == 1:
                rt = cls_[0]
    ext_recv = ""
    if lang == "c_sharp" and params:
        p0 = params[0][1] if isinstance(params[0], tuple) else params[0]
        if _txt(p0, raw).lstrip().startswith("this "):
            # `static void AddConduit(this IServiceCollection services)`: an
            # extension method; call sites do not pass the receiver
            ext_recv = (ptypes[0] if ptypes else "") or "?"
            total, mins, ptypes = max(0, total - 1), max(0, mins - 1), ptypes[1:]
    info = {"params_min": mins, "params_max": None if variadic else total, "returns": rt,
            "param_types": ptypes}
    if lang in ("typescript", "tsx", "javascript"):
        cbt = {}
        for k, p in enumerate(params):
            p = p[1] if isinstance(p, tuple) else p
            ta = next((c for c in p.children if c.type == "type_annotation"), None)
            ft = next((c for c in ta.children if c.type == "function_type"), None) if ta is not None else None
            fp = next((c for c in ft.children if c.type == "formal_parameters"), None) if ft is not None else None
            if fp is None:
                continue
            inner = []
            for q in (c for c in fp.children if c.is_named):
                try:
                    tq = _param(lang, q, raw)
                except Exception:
                    tq = []
                inner.append(tq[0][1] if tq and tq[0][1] else "")
            if any(inner):
                cbt[str(k)] = inner
        if cbt:
            info["cb_param_types"] = cbt
    if ext_recv:
        info["extension_receiver"] = ext_recv
    if lang == "swift":
        # external argument labels: `of origin: T` -> "of", `_ x` -> "_", `y: T` -> "y"
        labels = []
        for p in params:
            if isinstance(p, tuple):
                p = p[1]
            ids = [c for c in p.children if c.type == "simple_identifier"]
            labels.append(_txt(ids[0], raw) if ids else "_")
        info["param_labels"] = labels
        # `_ test: (Application) async throws -> T`: the callback's parameter
        # types, by position, external label and (trailing closure) "last"
        cbt = {}
        plist = [p[1] if isinstance(p, tuple) else p for p in params]
        for k, p in enumerate(plist):
            ft = next((c for c in p.children if c.type == "function_type"), None)
            if ft is None:
                continue
            tt = next((c for c in ft.children if c.type == "tuple_type"), None)
            items = [c for c in tt.children if c.type == "tuple_type_item"] if tt is not None else []
            inner = []
            for it in items:
                ut = next((c for c in it.children if c.is_named), None)
                inner.append(_simple_type(ut, raw) if ut is not None else "")
            if any(inner):
                cbt[str(k)] = inner
                cbt["label=" + labels[k]] = inner
                if k == len(plist) - 1:
                    cbt["last"] = inner
        if cbt:
            info["cb_param_types"] = cbt
    return info


def _callee_split(call, args, raw: bytes):
    """(member, receiver_node | None, receiver_text) for a call node."""
    import re
    global _TRAIL_NAME
    if _TRAIL_NAME is None:
        _TRAIL_NAME = re.compile(r"([A-Za-z_$][\w$]*!?)\s*(?:<[^()]*>)?\s*$")
    head = raw[call.start_byte:args.start_byte].decode("utf-8", "ignore")
    m = _TRAIL_NAME.search(head)
    if not m:
        return "", None, ""
    member = m.group(1).rstrip("!")
    rtext = head[:m.start()].rstrip()
    for sep in ("?.", "->", "::", ".", ":"):
        if rtext.endswith(sep):
            rtext = rtext[: -len(sep)].rstrip()
            break
    else:
        return member, None, ""  # bare call
    if not rtext:
        return member, None, ""
    end = call.start_byte + len(rtext.encode("utf-8"))
    node = call
    # smallest descendant spanning exactly the receiver text
    best = None
    stack = [call]
    while stack:
        n = stack.pop()
        if n.start_byte == call.start_byte and n.end_byte == end:
            best = n
        if n.start_byte <= call.start_byte and n.end_byte >= end:
            stack.extend(n.children)
    return member, best, rtext


def _args_of(call):
    for c in call.children:
        if c.type in ARG_KINDS:
            return c
        if c.type == "call_suffix":
            for g in c.children:
                if g.type in ARG_KINDS:
                    return g
    return None


def _arity(args, call) -> int:
    n = sum(1 for c in args.children if c.is_named and c.type not in ("comment",))
    for c in call.children:  # kotlin/swift trailing lambda
        if c.type == "call_suffix":
            n += sum(1 for g in c.children if g.type in ("annotated_lambda", "lambda_literal"))
    return n


def _call_sites(lang: str, root, raw: bytes) -> dict:
    """(line, member) -> list of {arity, chain_root, chain} for every call."""
    out: dict = {}
    stack = [root]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.type not in CALL_KINDS:
            continue
        args = _args_of(n)
        if args is None:
            continue
        member, recv, rtext = _callee_split(n, args, raw)
        if not member:
            continue
        chain = []
        cur = recv
        bare_root = False
        for _ in range(12):  # bounded chain walk toward the root receiver
            if cur is None or cur.type not in CALL_KINDS:
                break
            a2 = _args_of(cur)
            if a2 is None:
                cur = None
                break
            m2, r2, rt2 = _callee_split(cur, a2, raw)
            if not m2:
                cur = None
                break
            chain.insert(0, {"m": m2, "n": _arity(a2, cur)})
            if r2 is None and not rt2:
                bare_root = True  # innermost hop is a bare `get()`
            cur, rtext = r2, rt2
        root_txt = rtext.strip() if chain or recv is not None else ""
        if chain and (cur is None or not root_txt):
            root_txt = ""
        rec = {"arity": _arity(args, n), "chain": chain,
             "chain_root": "this" if (chain and bare_root and not root_txt) else root_txt,
             "recv": rtext.strip() if not chain else "", "arg_types": _arg_types(lang, args, raw),
             "arg_labels": _arg_labels(args, raw) if lang == "swift" else None,
             "bounds": _rust_bounds(n, raw) if lang == "rust" and recv is not None else None}
        out.setdefault((n.start_point[0] + 1, member), []).append(rec)
        # extractors that place the site on the method's name (`b.Int64(..).`
        # then `BindError()` on a later line) look it up there
        fn_ = n.child_by_field_name("function")
        nm_ = fn_.child_by_field_name("field") if fn_ is not None else n.child_by_field_name("name")
        if nm_ is not None and nm_.start_point[0] != n.start_point[0]:
            out.setdefault((nm_.start_point[0] + 1, member), []).append(rec)
    return out


def _rust_bounds(node, raw: bytes) -> dict:
    """Generic name -> first trait bound, from every enclosing impl/fn
    (`impl<W, F: Formatter>`, `where F: Formatter`); innermost wins."""
    out: dict = {}

    def first_trait(tb):
        for c in tb.children:
            if c.type == "type_identifier":
                return _txt(c, raw)
            if c.type in ("scoped_type_identifier", "generic_type"):
                ids = [g for g in c.children if g.type == "type_identifier"]
                if ids:
                    return _txt(ids[-1], raw)
        return ""

    p = node.parent
    while p is not None:
        if p.type in ("impl_item", "function_item", "trait_item"):
            for c in p.children:
                if c.type == "type_parameters":
                    for tp in c.children:
                        nm = next((g for g in tp.children if g.type == "type_identifier"), None)
                        tb = next((g for g in tp.children if g.type == "trait_bounds"), None)
                        if nm is not None and tb is not None:
                            out.setdefault(_txt(nm, raw), first_trait(tb))
                elif c.type == "where_clause":
                    for wp in c.children:
                        if wp.type != "where_predicate":
                            continue
                        nm = next((g for g in wp.children if g.type == "type_identifier"), None)
                        tb = next((g for g in wp.children if g.type == "trait_bounds"), None)
                        if nm is not None and tb is not None:
                            out.setdefault(_txt(nm, raw), first_trait(tb))
        p = p.parent
    return {k: v for k, v in out.items() if v}


def _arg_labels(args, raw: bytes) -> list:
    """Swift call labels in order (`f(of: 1, 2)` -> ["of", "_"])."""
    out = []
    for v in args.children:
        if v.type == "value_argument":
            lab = next((c for c in v.children if c.type == "value_argument_label"), None)
            out.append(_txt(lab, raw).strip() if lab is not None else "_")
    return out


def _cb_param(param, fn, raw: bytes, lang: str):
    """(name, type) for a parameter of the callback `fn`: its annotation, else
    `()cb:<callee>:<arg index>:<param index>` when `fn` is an argument of a
    plain call `callee(..)`; None when neither applies."""
    try:
        typed = _param(lang, param, raw)
    except Exception:
        typed = []
    if typed and typed[0][1]:
        return typed[0]
    name = next((_txt(c, raw) for c in param.children if c.type in ("identifier", "simple_identifier")), "")
    if not name and param.type == "identifier":
        name = _txt(param, raw)
    if not name:
        return None
    if lang == "swift":
        return _swift_cb_param(name, param, fn, raw)
    args = fn.parent
    call = args.parent if args is not None else None
    if args is None or call is None or args.type != "arguments" or call.type != "call_expression":
        return None
    callee = call.child_by_field_name("function")
    if callee is None or callee.type != "identifier":
        return None
    ai = [c for c in args.children if c.is_named].index(fn) if fn in args.children else -1
    plist = param.parent
    pj = [c for c in plist.children if c.is_named].index(param) if plist is not None else -1
    if ai < 0 or pj < 0:
        return None
    return (name, f"()cb:{_txt(callee, raw)}:{ai}:{pj}")


def _swift_cb_param(name: str, param, fn, raw: bytes):
    """Swift `withApp(..) { app in }` (trailing: "last") / `f(test: { a in })`
    (labeled): `()cb:<callee>:<key>:<param index>`."""
    while fn is not None and fn.type != "lambda_literal":   # from the params list up
        fn = fn.parent if fn.type.startswith("lambda_") else None
    if fn is None:
        return None
    p_ = fn.parent
    key = ""
    if p_ is not None and p_.type == "call_suffix":
        key, suffix = "last", p_
    elif p_ is not None and p_.type == "value_argument":
        lab = next((c for c in p_.children if c.type == "value_argument_label"), None)
        if lab is None:
            return None
        key = "label=" + _txt(lab, raw).strip()
        suffix = p_.parent.parent if p_.parent is not None else None
    else:
        return None
    call = suffix.parent if suffix is not None else None
    if call is None or call.type != "call_expression":
        return None
    callee = call.children[0] if call.children else None
    if callee is None or callee.type != "simple_identifier":
        return None
    plist = param.parent
    pj = [c for c in plist.children if c.type == "lambda_parameter"].index(param) \
        if plist is not None and param in plist.children else -1
    if pj < 0:
        return None
    return (name, f"()cb:{_txt(callee, raw)}:{key}:{pj}")


_RET_KINDS = ("return_statement", "return_expression")
_FN_KINDS = ("function", "arrow", "lambda", "closure", "method", "constructor")


def _infer_return(fn, raw: bytes, lang: str, scope: dict) -> str:
    """Undeclared return type from the body: every `return` gives the same
    typed local or `new X()` (`const createApp = () => { const app = new H3();
    ...; return app }`). Nested functions are not looked into; anything else
    (two types, an untyped value) gives nothing."""
    body = fn.child_by_field_name("body")
    if body is None:
        return ""
    exprs = []
    if body.type not in ("statement_block", "block", "compound_statement", "function_body",
                         "constructor_body"):
        exprs.append(body)                    # `() => new X()` expression body
    else:
        stack = list(body.children)
        while stack:
            n = stack.pop()
            if any(k in n.type for k in _FN_KINDS) or n.type == "class_body":
                continue
            if n.type in _RET_KINDS:
                v = next((c for c in n.children if c.is_named), None)
                if v is None:
                    return ""
                exprs.append(v)
                continue
            stack.extend(n.children)
    types = set()
    for v in exprs:
        while v is not None and v.type in ("parenthesized_expression", "await_expression"):
            v = next((c for c in v.children if c.is_named), None)
        t = ""
        if v is not None and v.type == "identifier":
            t = scope.get(_txt(v, raw), "")
        elif v is not None:
            t = _ctor_type(lang, v, raw)
        if not t or t.startswith(("()", "=", "[]")):
            return ""
        types.add(t)
    return types.pop() if len(types) == 1 else ""


def annotate_receivers(lang: str, root, raw: bytes, res) -> int:
    """Add receiver_type / receiver_owner to this file's unresolved member
    calls. Returns the number of annotated edges."""
    kinds = PARAM_KINDS.get(lang, ())
    lkinds = LOCAL_KINDS.get(lang, ())
    syms = {}
    for n in res.nodes:
        if n.type in ("function", "method", "test") and n.location:
            loc = n.location
            syms.setdefault((loc.start_line, loc.end_line), []).append(n)
    params: dict[str, dict[str, str]] = {}
    conflicted: dict[str, set] = {}
    declared: dict[str, set] = {}   # owner -> every local name declared (typed or not)
    globals_: dict[str, str] = {}  # Go package-level typed vars of this file
    mod_ids = [n.id for n in res.nodes if n.type == "module"]
    # outermost first: a nearer test's locals override (merged in order)
    test_spans = sorted(((n.location.start_line, n.location.end_line, n.id) for n in res.nodes
                         if n.type == "test" and n.location),
                        key=lambda t: t[0] - t[1])
    if kinds or lkinds:
        stack = [root]
        while stack:
            node = stack.pop()
            is_param = node.type in kinds
            if is_param or node.type in lkinds:
                owner = None
                cb_param = None
                a = node.parent
                while a is not None:
                    hit = syms.get((a.start_point[0] + 1, a.end_point[0] + 1))
                    if not hit and a.type == "function_body":
                        # Dart: signature and body are siblings and the
                        # symbol spans the signature only.
                        sig = a.prev_named_sibling
                        if sig is not None:
                            hit = syms.get((sig.start_point[0] + 1, sig.end_point[0] + 1))
                    if hit:
                        owner = hit[0].id
                        break
                    if any(k in a.type for k in _INNER_SCOPES) and is_param and cb_param is None                             and lang in ("typescript", "tsx", "javascript", "swift"):
                        # a callback's parameter: typed by its annotation, or by the
                        # callee's declared callback type (`stream(c, async (stream) =>
                        # stream.write())` with `cb: (stream: StreamingApi) => ..`)
                        cb_param = _cb_param(node, a, raw, lang)
                        if cb_param:
                            a = a.parent
                            continue
                    if any(k in a.type for k in _INNER_SCOPES) and not is_param:
                        # A local declared inside a closure (`test('x', () {
                        # final fm = FormData(); fm.m(); })`): the closure's
                        # calls are attributed to the enclosing symbol, so its
                        # locals are too (a name typed differently in two
                        # closures is poisoned by the conflict check below)
                        a = a.parent
                        continue
                    if cb_param is not None and lang == "swift" and any(k in a.type for k in _INNER_SCOPES):
                        a = a.parent        # the typed closure's own lambda_* nodes
                        continue
                    if any(k in a.type for k in _INNER_SCOPES):
                        # Closure parameter: not the symbol's scope, but it
                        # shadows a same-named variable of the enclosing
                        # symbol inside the closure, so that name is poisoned.
                        if is_param:
                            b = a.parent
                            while b is not None:
                                outer = syms.get((b.start_point[0] + 1, b.end_point[0] + 1))
                                if outer:
                                    names = [nm for nm, _t in _param(lang, node, raw)] or                                         [_txt(c, raw) for c in node.children if c.type in _NAME_KINDS]
                                    for name in names:
                                        if name:
                                            conflicted.setdefault(outer[0].id, set()).add(name)
                                    break
                                b = b.parent
                        break
                    a = a.parent
                if owner is None and is_param and lang == "c_sharp" and node.parent is not None \
                        and node.parent.type == "parameter_list" and node.parent.parent is not None \
                        and node.parent.parent.type in ("class_declaration", "record_declaration",
                                                        "struct_declaration"):
                    # C# 12 primary constructor `class H(ConduitContext context)`:
                    # the parameter is in scope in every member of the type
                    cls = node.parent.parent
                    a0, b0 = cls.start_point[0] + 1, cls.end_point[0] + 1
                    for name, t in _param(lang, node, raw):
                        if not name or not t:
                            continue
                        for (s_, e_), ns in syms.items():
                            if a0 <= s_ and e_ <= b0:
                                for n_ in ns:
                                    params.setdefault(n_.id, {}).setdefault(name, t)
                if owner is None and (not is_param or cb_param) and a is None and mod_ids and lang != "go":
                    # module-scope code and the closures in it (`describe(() =>
                    # { const e = new X(); e.m() })`): their calls are the
                    # module's, so are their locals
                    owner = mod_ids[0]
                if owner is None and lang == "go" and node.type == "var_spec" \
                        and node.parent is not None and node.parent.parent is not None \
                        and node.parent.parent.type == "source_file":
                    # package-level `var v *Viper`: in scope for every function
                    for name, t in _local(lang, node, raw):
                        if name and t:
                            globals_[name] = t
                if owner:
                    found = [cb_param] if cb_param else                         (_param(lang, node, raw) if is_param else _local(lang, node, raw))
                    if not is_param:
                        # every declared local, typed or not: an untyped one
                        # (`const req = await client.post()`) must never be
                        # typed by naming convention later
                        nm0 = next((c for c in node.children if c.type in _NAME_KINDS), None)
                        declared_now = [n for n, _t in found] or ([_txt(nm0, raw)] if nm0 is not None else [])
                        lhs = next((c for c in node.children if c.type == "expression_list"), None)
                        if lhs is not None and not declared_now:
                            # Go `b := Uri` / `a, err := f()`: names in the left list
                            declared_now = [_txt(c, raw) for c in lhs.children if c.type == "identifier"]
                        for nm_ in declared_now:
                            if nm_:
                                declared.setdefault(owner, set()).add(nm_)
                    for name, t in found:
                        if not name:
                            continue
                        if not t:  # untyped (re)assignment: poisons the name
                            conflicted.setdefault(owner, set()).add(name)
                            continue
                        prev = params.setdefault(owner, {}).get(name)
                        if prev is not None and prev != t:
                            conflicted.setdefault(owner, set()).add(name)
                        params[owner][name] = t
            stack.extend(node.children)
    for owner, names in conflicted.items():
        for nm in names:
            params.get(owner, {}).pop(nm, None)  # poisoned before any typed one
    for owner, sc in params.items():
        for nm, t in list(sc.items()):
            if t.startswith("()") and "." in t[2:]:
                recv_, m_ = t[2:].split(".", 1)
                rt = sc.get(recv_)
                if rt and not rt.startswith("()"):
                    sc[nm] = f"(){rt}.{m_}"     # `c := router.allocateContext()` -> ()Engine.allocateContext
                elif rt:
                    sc[nm] = f"{rt}.{m_}"       # router := New(): ()New.allocateContext (New's result, then m)
    owner_q = {n.id: n.qualified_name for n in res.nodes
               if n.type in ("method",)}
    # signatures: arity range + declared return type on every function node
    fstack = [root]
    while fstack:
        node = fstack.pop()
        fstack.extend(node.children)
        if not any(k in node.type for k in ("function", "method", "constructor", "signature")):
            continue
        hit = syms.get((node.start_point[0] + 1, node.end_point[0] + 1))
        if not hit:
            continue
        for sym in hit:
            if (sym.attributes or {}).get("params_min") is not None:
                continue
            info = _fn_info(lang, node, raw)
            if info:
                sym.attributes = dict(sym.attributes or {})
                sym.attributes.update({k: v for k, v in info.items() if v is not None or k == "params_max"})
            if not (sym.attributes or {}).get("returns"):
                inf = _infer_return(node, raw, lang, params.get(sym.id, {}))
                if inf:
                    sym.attributes = dict(sym.attributes or {})
                    sym.attributes["returns"] = inf
                    sym.attributes["returns_inferred"] = True
    # class/struct member declarations -> fields on the type node
    tsyms = {}
    for n in res.nodes:
        if n.type in TYPE_NODE_KINDS and n.location:
            tsyms.setdefault((n.location.start_line, n.location.end_line), []).append(n)
    fkinds = FIELD_KINDS.get(lang, ())
    if fkinds and tsyms:
        fstack = [root]
        while fstack:
            node = fstack.pop()
            fstack.extend(node.children)
            if node.type not in fkinds:
                continue
            a_ = node.parent
            owner_t = None
            while a_ is not None:
                if any(k in a_.type for k in ("function", "method", "lambda", "closure"))                         and not (lang in ("typescript", "tsx", "php")
                                 and any(k in _txt(a_, raw)[:60] for k in ("constructor", "__construct"))
                                 and node.type != "public_field_definition"):
                    break  # a local, not a member
                hit = tsyms.get((a_.start_point[0] + 1, a_.end_point[0] + 1))
                if hit:
                    owner_t = hit[0]
                    break
                a_ = a_.parent
            if owner_t is None:
                continue
            try:
                found = _field(lang, node, raw)
            except Exception:
                found = []
            for nm, t in found:
                if nm and t:
                    owner_t.attributes = dict(owner_t.attributes or {})
                    owner_t.attributes.setdefault("fields", {})[nm] = t
    sites = _call_sites(lang, root, raw)
    count = 0
    for e in res.edges:
        if e.relation != "calls_unresolved":
            continue
        a = dict(e.attributes or {})
        base, member = a.get("base"), a.get("member")
        name = member or base
        site = sites.get((e.start_line or 0, name or "")) if name else None
        scope = params.get(e.source, {})
        if test_spans:
            # `describe(() => { let r: R; it(() => { r.m() }) })`: locals of every
            # test block around the call (outer first, nearer ones override),
            # then the caller's own scope on top
            ln_ = e.start_line or 0
            merged: dict = {}
            for s_, e_, tid in test_spans:
                if s_ <= ln_ <= e_ and tid != e.source and params.get(tid):
                    merged.update(params[tid])
            if merged:
                scope = {**merged, **scope}
        if globals_:
            # file globals, unless the function rebinds or poisons the name
            shadow = conflicted.get(e.source, set())
            scope = {**{k: v for k, v in globals_.items() if k not in shadow}, **scope}
        if site:
            first = site[0]
            if "arity" not in a and all(x["arity"] == first["arity"] for x in site):
                a["arity"] = first["arity"]
            if len(site) == 1 and first.get("arg_types"):
                at = [scope.get(t[1:]) if isinstance(t, str) and t.startswith("@") else t
                      for t in first["arg_types"]]
                if any(at):
                    a["arg_types"] = at
            if len(site) == 1 and first.get("arg_labels"):
                a["arg_labels"] = first["arg_labels"]
            if first.get("bounds") and all(x.get("bounds") == first["bounds"] for x in site):
                a["type_bounds"] = first["bounds"]
            if first["chain"] and all(x["chain"] == first["chain"] for x in site) \
                    and not (lang == "php" and a.get("receiver_type")):  # `app(X::class)->m()` typed
                a["chain"] = first["chain"]
                root_txt = first["chain_root"]
                fpath = _self_field_path(root_txt)
                mnew = _NEW_ROOT.match(root_txt or "")
                if mnew:
                    # `new CompareToBuilder().append(a, b).toComparison()`,
                    # PHP `(new BookContents($b))->getTree()->all()`
                    a["chain_root_type"] = re.split(r"[.\\]", mnew.group(1))[-1]
                if root_txt in SELF_WORDS or fpath:
                    q = owner_q.get(e.source, "")
                    if "." in q:
                        a["chain_root_owner"] = q.rsplit(".", 1)[0]
                        if fpath:
                            # `self.searcher.line_terminator().as_byte()`
                            a["chain_root_fields"] = fpath
                elif root_txt and scope.get(root_txt):
                    a["chain_root_type"] = scope[root_txt]
                elif root_txt and root_txt.replace("_", "").isalnum():
                    a["chain_root_static"] = root_txt
            elif len(site) == 1 and _NEW_ROOT.match(first.get("recv") or "") and not a.get("receiver_type"):
                # `new EqualsBuilder().append(..)` / `(new HtmlContentFilter($c))->filterString()`
                a["receiver_type"] = re.split(r"[.\\]", _NEW_ROOT.match(first["recv"]).group(1))[-1]
            elif len(site) == 1 and first.get("recv") and e.source in owner_q:
                # `this.f.m()` / `self.f.m()` / `$this->f->m()`: a field of
                # the enclosing type (binder looks it up incl. base types)
                import re as _re
                mm = _re.match(r"^(this|self|\$this)\s*(\.|->|\?\.)\s*([A-Za-z_]\w*)$", first["recv"])
                if mm:
                    a["receiver_field"] = mm.group(3)
                    a["field_owner"] = owner_q[e.source].rsplit(".", 1)[0]
                elif len(_self_field_path(first["recv"])) > 1:
                    # `self.config.line_term.m()`: walk the field types
                    a["field_path"] = _self_field_path(first["recv"])
                    a["field_owner"] = owner_q[e.source].rsplit(".", 1)[0]
        if not member and base and lang in IMPLICIT_THIS and e.source in owner_q                 and a.get("callee_kind") in ("function", "identifier", "local", None):
            q = owner_q[e.source]
            if lang == "kotlin" and (base in scope or base in declared.get(e.source, ())):
                # `queryBuilder { .. }` on a parameter / local: its invoke(),
                # never a same-named method found by name
                a["local_callee"] = True
            if "." in q:
                a["implicit_member"] = base
                a["receiver_owner"] = q.rsplit(".", 1)[0]
                e.attributes = a
                count += 1
            continue
        e.attributes = a
        if a.get("receiver_field") or a.get("field_path"):
            count += 1
            continue
        if base and member and not a.get("receiver_type") and not a.get("receiver_owner"):
            first_ = base.split(".")[0]
            decl_ = set(declared.get(e.source, ()))
            for s_, e_, tid in test_spans:
                if s_ <= (e.start_line or 0) <= e_:
                    decl_ |= declared.get(tid, set())
            if first_ in decl_ and not scope.get(first_):
                a["receiver_local"] = True          # a local we could not type
                e.attributes = a
        if lang in ("go", "typescript", "tsx", "javascript") and base and "." in base and member                 and not a.get("receiver_type") and not a.get("receiver_field")                 and not base.startswith(("this.", "self.")):
            first_, rest_ = base.split(".", 1)
            rt_ = scope.get(first_)
            if rt_ and not rt_.startswith(("()", "=")):
                # `c.Writer.Status()` with `c *Context`: walk Context's fields
                a["field_path"] = rest_.split(".")
                a["field_root_type"] = rt_
                e.attributes = a
                count += 1
                continue
        if lang == "c_sharp" and a.get("callee_kind") == "complex" and member and a.get("qualifier") \
                and not a.get("receiver_type") and e.source in owner_q:
            qpath = re.sub(r"^this\.", "", re.sub(r"\s+", "", a["qualifier"]))
            if re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", qpath):
                head_, rest_ = qpath.split(".", 1)
                rt_ = scope.get(head_)
                if rt_ and not rt_.startswith(("()", "=")):
                    # `basket.Items.Add(x)` on a typed local
                    a["field_path"], a["field_root_type"] = rest_.split("."), rt_
                elif head_ not in scope and head_ not in declared.get(e.source, ()) \
                        and head_[:1] in ("_",) + tuple("abcdefghijklmnopqrstuvwxyz"):
                    # `_env.BasketService.GetBasketAsync()`: a field of the class
                    a["field_path"] = qpath.split(".")
                    a["field_owner"] = owner_q[e.source].rsplit(".", 1)[0]
                if a.get("field_path"):
                    e.attributes = a
                    count += 1
                    continue
        if lang == "swift" and a.get("callee_kind") == "complex" and member and site \
                and len(site) == 1 and not a.get("receiver_type") and not a.get("chain"):
            mpath = re.fullmatch(r"([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)+)", (site[0].get("recv") or "").strip())
            rt_ = scope.get(mpath.group(1)) if mpath else None
            if rt_ and not rt_.startswith("="):
                # `app.fileio.streamFile(..)` with `app` typed (a parameter,
                # a closure parameter's callee type): walk the property types
                a["field_path"] = mpath.group(2)[1:].split(".")
                a["field_root_type"] = rt_
                e.attributes = a
                count += 1
                continue
        if a.get("callee_kind") == "this" and member and not a.get("receiver_owner")                 and e.source in owner_q and "." in owner_q[e.source]:
            # Java `this.getFirstName()`: the enclosing class (bases via mro)
            a = dict(a)
            a["receiver_owner"] = owner_q[e.source].rsplit(".", 1)[0]
            e.attributes = a
            count += 1
            continue
        if not base or not member or a.get("receiver_type") or a.get("receiver_owner"):
            continue
        if base in SELF_WORDS:
            q = owner_q.get(e.source, "")
            if "." in q:
                a = dict(a)
                a["receiver_owner"] = q.rsplit(".", 1)[0]
                e.attributes = a
                count += 1
            continue
        t = scope.get(base)
        if t:
            a = dict(a)
            a["receiver_type"] = t
            e.attributes = a
            count += 1
            continue
        if a.get("chain") or not base.replace("_", "").replace("$", "").isalnum():
            continue
        a = dict(a)
        if lang in IMPLICIT_THIS and e.source in owner_q:
            # bare `f.m()`: an implicit field of the enclosing type, or a type
            # name (static call) -- the binder decides, fields first.
            a["receiver_name"] = base
            a["field_owner"] = owner_q[e.source].rsplit(".", 1)[0]
        elif lang in STATIC_TYPE_CALLS and base[:1].isupper():
            # `TypeSpec.classBuilder(..)`: the binder accepts it only if the
            # name is a declared type of the repo (else it stays unresolved).
            a["receiver_static"] = base
        else:
            continue
        e.attributes = a
        count += 1
    return count


def _self_field_path(txt) -> list:
    """`self.a.b` / `this.a.b` / `$this->a->b` -> ["a", "b"]; else []."""
    import re as _re
    if not isinstance(txt, str):
        return []
    m = _re.match(r"^(this|self|\$this)((?:\s*(?:\.|->|\?\.)\s*[A-Za-z_]\w*)+)$", txt.strip())
    if not m:
        return []
    return _re.findall(r"[A-Za-z_]\w*", m.group(2))
