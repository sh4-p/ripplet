"""Cross-language HTTP links: client requests -> server route handlers.

A frontend `fetch('/api/users')` in TypeScript and the FastAPI handler of
`GET /api/users` are the same feature in two languages; no import or call
graph connects them. This stage does, from syntax only:

  routes   Python route nodes (python extractor, with `handles_route`);
           JS/TS `app.get('/p', handler)` style registrations (Express,
           Koa-router, Hono, Fastify shorthands); Go `r.GET("/p", h)` /
           `http.HandleFunc("/p", h)` (gin, echo, chi, net/http); Spring
           `@GetMapping("/p")` (+ class `@RequestMapping` prefix) in Java
           and Kotlin. Non-Python routes get `route` nodes here.
  clients  JS/TS `fetch(url)`, `axios(.verb)(url)`, `<x>.verb(url)` with a
           URL-shaped first argument; Python `requests`/`httpx` calls. URLs
           come from literals, template strings (`${id}` -> param) and
           `'/a/' + id` concatenation; scheme/host prefixes are dropped.
  match    Same segment count; route params (`{x}`, `:x`, `<int:x>`, `*`)
           and client params match any segment; known HTTP methods must
           agree. The most literal route wins and must be unique, else no
           edge (ambiguity is never guessed).

Edges: `calls_http` from the innermost function enclosing the request to
the handler (or the route node when the handler is inline). Recomputed
from scratch every run, like the other service-stage binders.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.identifiers import canonical_edge_id, canonical_symbol_id, site_fingerprint
from ..core.models import Edge, Location, Node

METHOD = "http-link"
VERSION = "5"  # v5: PHP routers; route nodes + handles_route for every server route; v4  # v2: ASP.NET Core attribute routes
VERBS = ("get", "post", "put", "delete", "patch", "head", "options")
_VERB_ALIAS = {"del": "delete"}  # superagent `request.del(url)`
_JS_EXT = {".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
           ".ts": "typescript", ".tsx": "tsx"}
_SERVER_RECV = re.compile(r"(^|\.)(app|router|server|routes?|r|e|g|v\d|api_?router|\w*Router|\w*App)$")
_URLISH = re.compile(r"^(https?://[^/]+)?/")
_SPRING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*(?:\(\s*(?:(?:value|path)\s*=\s*)?"
                     r"(?:\[|\{)?\s*\"([^\"]*)\")?")
_SPRING_METHOD = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)")


def _segs(path: str) -> list[str]:
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path.startswith("{}") and not path.startswith("{}/"):
        path = "/" + path[2:]  # `${serverApiUrl}api/x`: base URL variable
    path = _URLISH.sub("/", path) if path.startswith("http") else path
    return [s for s in path.strip().split("/") if s]


_ALT = re.compile(r"\x00([^\x00]*)\x00")


def _variants(url, cap: int = 8) -> list:
    """`/articles\x00/feed\x1f\x00` (from `'/articles' + (feed ? '/feed' : '')`)
    -> ['/articles/feed', '/articles']: every branch of a JS conditional."""
    if not url or "\x00" not in url:
        return [url]
    out, todo = [], [url]
    while todo and len(out) < cap:
        u = todo.pop(0)
        m = _ALT.search(u)
        if not m:
            out.append(u)
            continue
        todo.extend(u[:m.start()] + alt + u[m.end():] for alt in m.group(1).split("\x1f"))
    return out


def _urlish(url) -> bool:
    return bool(url) and any(v and _URLISH.match(_norm(v)) for v in _variants(url))


def _add(clients: list, rel, line, url, method) -> None:
    for v in dict.fromkeys(_norm(v) for v in _variants(url)):
        if v and _URLISH.match(v):
            clients.append((rel, line, v, method))


def _norm(url):
    """`{}api/x` / `{}/x` (base URL variable first) -> `/api/x` / `/x`."""
    if url and url.startswith("{}"):
        url = url[2:]
        url = url if url.startswith("/") else "/" + url
    return url


def _is_param(seg: str) -> bool:
    return seg in ("{}", "*") or seg.startswith((":", "{", "<", "*")) or "{" in seg


_BASE_SEG = re.compile(r"^(api|rest|v\d+)$", re.I)


def _score_based(client: list[str], route: list[str], penal: bool = True):
    """Exact match, else allow a leading `api`/`v1` base on either side
    (`/articles/{}` from a client whose base URL ends in /api vs
    `/api/articles/<slug>`). A base-skip match scores below exact ones."""
    sc = _score(client, route)
    if sc is not None:
        return sc
    for a, b in ((client, route), (route, client)):
        k = 0
        while k < len(b) - len(a) and _BASE_SEG.match(b[k]):
            k += 1
        if k and len(b) - k == len(a):
            sub = _score(a, b[k:]) if a is client else _score(b[k:], a)
            if sub:
                return max(sub - 1, 1) if penal else sub
    return None


def _score(client: list[str], route: list[str]):
    """Match strength, or None when the paths cannot be the same URL.
    literal==literal 2, param~param 1, literal client vs route param 1,
    client param vs literal route 0 (`/users/{id}` prefers `/users/{user_id}`
    over `/users/me`). Needs at least one literal==literal segment."""
    if len(client) != len(route):
        return None
    score = lits = 0
    for c, r in zip(client, route):
        pc, pr = _is_param(c), _is_param(r)
        if pc and pr:
            score += 1
        elif pr:
            score += 1
        elif pc:
            continue
        elif c != r:
            return None
        else:
            score += 2
            lits += 1
    return score if lits else 0


def _js_url(node, raw: bytes, consts: dict | None = None) -> str | None:
    t = node.type
    txt = raw[node.start_byte:node.end_byte].decode("utf-8", "ignore")
    consts = consts or {}
    if t == "string":
        return txt[1:-1]
    if t == "template_string":
        # `${this.resourceUrl}/${id}`: known same-file URL constants inline
        return re.sub(r"\$\{\s*(?:this\.)?([\w$]+)\s*\}|\$\{[^}]*\}",
                      lambda m: consts.get(m.group(1), "{}") if m.group(1) else "{}", txt[1:-1])
    if t in ("identifier", "member_expression"):
        return consts.get(txt.removeprefix("this."))
    if t == "parenthesized_expression":
        kids = _named_kids(node)
        return _js_url(kids[0], raw, consts) if len(kids) == 1 else None
    if t == "ternary_expression":
        alts = [_js_url(node.child_by_field_name(f), raw, consts)
                for f in ("consequence", "alternative")]
        if all(a is None for a in alts) or any(a and "\x00" in a for a in alts):
            return None
        return "\x00" + "\x1f".join(a if a is not None else "{}" for a in alts) + "\x00"
    if t == "binary_expression":
        parts = [c for c in node.children if c.type != "+"]
        if len(node.children) == 3 and node.children[1].type == "+":
            left = _js_url(parts[0], raw, consts)
            right = _js_url(parts[1], raw, consts)
            if left is None and right is None:
                return None
            return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def _named_kids(n):
    return [c for c in n.children if c.is_named]


def _parse(lang: str, raw: bytes):
    from ..extraction.code import tree_sitter as tsa
    try:
        if lang in ("javascript", "typescript", "tsx"):
            p = tsa.parse(lang, raw)
            return p.tree.root_node if p.ok else None
        return tsa.get_univ_parser(lang).parse(raw).root_node
    except Exception:
        return None


def _walk(root):
    """Preorder in source order (declarations before later uses)."""
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _js_consts(root, raw: bytes) -> dict:
    """Same-file names bound to a URL-shaped string (`const API = '/api'`,
    class field `resourceUrl = `${base}api/x``, `getEndpointFor('api/x')`)."""
    out: dict = {}
    for n in _walk(root):
        if n.type in ("variable_declarator", "public_field_definition", "field_definition"):
            nm = n.child_by_field_name("name") or n.child_by_field_name("property")
            val = n.child_by_field_name("value")
            if nm is None or val is None:
                continue
            u = _js_url(val, raw, out) if val.type in ("string", "template_string",
                                                       "binary_expression") else None
            if u is None and val.type == "call_expression":
                args = val.child_by_field_name("arguments")
                av = _named_kids(args) if args is not None else []
                u = _js_url(av[0], raw, out) if len(av) == 1 and av[0].type in (
                    "string", "template_string") else None
            if u and "/" in u and not u.startswith(("./", "../")) and " " not in u:
                out[raw[nm.start_byte:nm.end_byte].decode()] = \
                    u if u.startswith(("/", "{}", "http")) else "/" + u
    return out


def _scan_js(rel: str, lang: str, raw: bytes, routes: list, clients: list, mounts: list):
    root = _parse(lang, raw)
    if root is None:
        return
    consts = _js_consts(root, raw)
    for n in _walk(root):
        if n.type == "object":
            # generated clients: `request({ method: "GET", url: "/api/v1/items/{id}" })`
            kv = {}
            for p in n.children:
                if p.type == "pair":
                    k, v = p.child_by_field_name("key"), p.child_by_field_name("value")
                    if k is not None and v is not None:
                        kv[raw[k.start_byte:k.end_byte].decode("utf-8", "ignore").strip("'\"")] = v
            if "url" in kv and "method" in kv:
                u = _js_url(kv["url"], raw, consts)
                mv = kv["method"]
                m = raw[mv.start_byte:mv.end_byte].decode("utf-8", "ignore").strip("'\"`").upper()
                if u and _urlish(u) and m.lower() in VERBS:
                    _add(clients, rel, n.start_point[0] + 1, u, m)
            continue
        if n.type not in ("call_expression",):
            continue
        fn = n.child_by_field_name("function")
        args = n.child_by_field_name("arguments")
        if fn is None or args is None:
            continue
        av = _named_kids(args)
        if not av:
            continue
        url = _js_url(av[0], raw, consts)
        if url is None and av[0].type == "object":
            # `client.get({ url: '/api/v1/items/', ...options })`
            for p in av[0].children:
                if p.type == "pair":
                    k = p.child_by_field_name("key")
                    if k is not None and raw[k.start_byte:k.end_byte].decode().strip("'\"") == "url":
                        url = _js_url(p.child_by_field_name("value"), raw, consts)
        url = _norm(url)
        line = n.start_point[0] + 1
        if fn.type == "identifier":
            name = raw[fn.start_byte:fn.end_byte].decode()
            if name in ("fetch", "axios", "$fetch", "ofetch", "ky") and url and _urlish(url):
                method = "GET"
                if len(av) > 1 and av[1].type == "object":
                    m = re.search(r"method\s*:\s*['\"`](\w+)", raw[av[1].start_byte:av[1].end_byte].decode("utf-8", "ignore"))
                    if m:
                        method = m.group(1).upper()
                _add(clients, rel, line, url, method)
            continue
        if fn.type != "member_expression":
            continue
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        verb = raw[prop.start_byte:prop.end_byte].decode() if prop is not None else ""
        verb = _VERB_ALIAS.get(verb, verb)
        recv = raw[obj.start_byte:obj.end_byte].decode("utf-8", "ignore") if obj is not None else ""
        if verb == "use" and url and url.startswith("/") and len(av) >= 2 \
                and av[-1].type == "identifier":
            # `app.use('/api/orders', orderRoutes)`: mount prefix of a router module
            mounts.append((rel, url, raw[av[-1].start_byte:av[-1].end_byte].decode()))
            continue
        chain_path = None
        if obj is not None and obj.type == "call_expression" and verb.lower() in VERBS + ("all",):
            # `router.route('/:id').get(protect, getOrderById).put(...)`
            cur = obj
            while cur is not None and cur.type == "call_expression":
                f2 = cur.child_by_field_name("function")
                if f2 is None or f2.type != "member_expression":
                    break
                p2 = f2.child_by_field_name("property")
                if p2 is not None and raw[p2.start_byte:p2.end_byte].decode() == "route":
                    a2 = cur.child_by_field_name("arguments")
                    a2 = _named_kids(a2) if a2 is not None else []
                    chain_path = _js_url(a2[0], raw, consts) if a2 else None
                    break
                cur = f2.child_by_field_name("object")
        if chain_path is not None:
            last = av[-1]
            if last.type in ("identifier", "member_expression"):
                handler = raw[last.start_byte:last.end_byte].decode().rsplit(".", 1)[-1]
                routes.append((rel, line, verb.upper() if verb.lower() in VERBS else "*",
                               chain_path, handler))
            continue
        if verb.lower() not in VERBS + ("all", "route", "request"):
            continue
        if url is None or not _urlish(url):
            continue
        last = av[-1]
        handlerish = len(av) >= 2 and last.type in ("identifier", "member_expression", "function_expression",
                                                    "arrow_function", "function")
        if handlerish and _SERVER_RECV.search(recv) and verb != "request":
            handler = ""
            if last.type == "identifier":
                handler = raw[last.start_byte:last.end_byte].decode()
            elif last.type == "member_expression":
                handler = raw[last.start_byte:last.end_byte].decode().rsplit(".", 1)[-1]
            elif last.type in ("function_expression", "function"):
                nm = last.child_by_field_name("name")
                handler = "" if nm is None else raw[nm.start_byte:nm.end_byte].decode()
            routes.append((rel, line, verb.upper() if verb.lower() in VERBS else "*", url, handler))
        elif verb.lower() in VERBS:
            _add(clients, rel, line, url, verb.upper())


def _js_imports(rel: str, text: str, rels: set) -> dict:
    """local name -> repo file for relative default imports / requires."""
    import posixpath
    out = {}
    d = rel.rsplit("/", 1)[0] if "/" in rel else ""
    named = []
    for names, spec in re.findall(r"import\s*\{([^}]*)\}\s*from\s*['\"](\.{1,2}/[^'\"]+)['\"]", text):
        for part in names.split(","):
            bits = part.strip().split(" as ")
            if bits[0].strip():
                named.append(((bits[-1].strip()), spec))
    for names, spec in re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]", text):
        for part in names.split(","):
            bits = part.strip().split(":")
            if bits[0].strip():
                named.append((bits[-1].strip(), spec))
    pairs = named + re.findall(r"import\s+([\w$]+)\s+from\s+['\"](\.{1,2}/[^'\"]+)['\"]", text) + \
        re.findall(r"(?:const|let|var)\s+([\w$]+)\s*=\s*require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)", text)
    for nm, spec in pairs:
        base = posixpath.normpath(posixpath.join(d, spec))
        for cand in (base, base + ".js", base + ".ts", base + ".mjs", base + "/index.js", base + "/index.ts"):
            if cand in rels:
                out[nm] = cand
                break
    return out


def _go_str(node, raw: bytes) -> str | None:
    if node.type in ("interpreted_string_literal", "raw_string_literal"):
        return raw[node.start_byte:node.end_byte].decode("utf-8", "ignore")[1:-1]
    return None


def _go_group(node, raw: bytes, groups: dict):
    """Prefix of a router expression: `v1` (a known group var) or
    `x.Group("/p")` (nested); None when it is not a group."""
    if node.type == "identifier":
        return groups.get(raw[node.start_byte:node.end_byte].decode())
    if node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and fn.type == "selector_expression" and args is not None:
            f = fn.child_by_field_name("field")
            if f is not None and raw[f.start_byte:f.end_byte].decode() in ("Group", "Route"):
                av = _named_kids(args)
                p = _go_str(av[0], raw) if av else None
                if p is not None:
                    base = _go_group(fn.child_by_field_name("operand"), raw, groups) or ""
                    return base.rstrip("/") + p
    return None


def _scan_go(rel: str, raw: bytes, routes: list, go_mounts: list | None = None):
    """gin/echo/chi/net-http registrations. Group prefixes: `v1 :=
    r.Group("/api")` vars per function, and `pkg.Register(v1.Group("/x"))`
    mounts that prefix routes registered on that function's parameter."""
    root = _parse("go", raw)
    if root is None:
        return
    go_mounts = go_mounts if go_mounts is not None else []
    for fdecl in _walk(root):
        if fdecl.type not in ("function_declaration", "method_declaration"):
            continue
        nm = fdecl.child_by_field_name("name")
        fname = raw[nm.start_byte:nm.end_byte].decode() if nm is not None else ""
        params = set()
        pl = fdecl.child_by_field_name("parameters")
        for pd in (pl.children if pl is not None else []):
            if pd.type == "parameter_declaration":
                for c in pd.children:
                    if c.type == "identifier":
                        params.add(raw[c.start_byte:c.end_byte].decode())
        groups: dict = {}
        body = fdecl.child_by_field_name("body")
        if body is None:
            continue
        for n in _walk(body):
            if n.type in ("short_var_declaration", "assignment_statement"):
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                ls = _named_kids(left) if left is not None else []
                rs = _named_kids(right) if right is not None else []
                if len(ls) == 1 and len(rs) == 1 and ls[0].type == "identifier":
                    g = _go_group(rs[0], raw, groups)
                    if g is not None:
                        groups[raw[ls[0].start_byte:ls[0].end_byte].decode()] = g
                continue
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            args = n.child_by_field_name("arguments")
            if fn is None or args is None:
                continue
            av = _named_kids(args)
            # mount: Register(v1.Group("/articles")) / pkg.Register(v1)
            callee = raw[fn.start_byte:fn.end_byte].decode("utf-8", "ignore").rsplit(".", 1)[-1]
            for a in av:
                g = _go_group(a, raw, groups)
                if g is not None and callee not in ("Group", "Route"):
                    go_mounts.append((callee, g))
            if fn.type != "selector_expression":
                continue
            field = fn.child_by_field_name("field")
            verb = raw[field.start_byte:field.end_byte].decode() if field is not None else ""
            if len(av) < 2:
                continue
            url = _go_str(av[0], raw)
            if url is None or not (url.startswith("/") or url == ""):
                continue
            operand = fn.child_by_field_name("operand")
            recv = raw[operand.start_byte:operand.end_byte].decode("utf-8", "ignore") if operand is not None else ""
            prefix = groups.get(recv)
            pending = fname if (prefix is None and recv in params) else ""
            url = ((prefix or "").rstrip("/") + ("/" + url.lstrip("/") if url.strip("/") else "")) or "/"
            up = verb.upper()
            if up in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                method = up
            elif verb in ("Handle", "HandleFunc", "Any"):
                method = "*"
            else:
                continue
            last = av[-1]
            handler = raw[last.start_byte:last.end_byte].decode("utf-8", "ignore").rsplit(".", 1)[-1] \
                if last.type in ("identifier", "selector_expression") else ""
            routes.append((rel, n.start_point[0] + 1, method, url, handler, pending))


def _scan_spring(rel: str, text: str, routes: list):
    lines = text.splitlines()
    prefix = ""
    # Kotlin `fun name(`; Java: the identifier before `(` on a declaration
    # line (the parameter list may continue on the following lines)
    fn_rx = re.compile(r"\bfun\s+(\w+)\s*\(" if rel.endswith((".kt", ".kts"))
                       else r"^\s*(?:(?:public|protected|private|static|final|synchronized)\s+)*"
                            r"[\w<>\[\],.?]+(?:<[^()]*>)?\s+(\w+)\s*\(")
    for i, line in enumerate(lines):
        for m in _SPRING.finditer(line):
            kind, path = m.group(1), m.group(2) or ""
            # class-level mapping: the next non-annotation line declares a type
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("@"):
                j += 1
            nxt = " ".join(lines[i:j + 1])
            if re.search(r"\b(class|interface|object)\s+\w+", nxt) and kind == "Request":
                prefix = path
                continue
            name = ""
            for k in range(i, min(i + 6, len(lines))):
                fm = fn_rx.search(lines[k] if k != i else line[m.end():])
                if fm and fm.group(1) not in ("if", "for", "while", "switch", "catch", "return"):
                    name = fm.group(1)
                    break
            method = kind.upper() if kind != "Request" else "*"
            sm = _SPRING_METHOD.search(line)
            if sm:
                method = sm.group(1)
            routes.append((rel, i + 1, method, (prefix.rstrip("/") + "/" + path.lstrip("/"))
                           if prefix else (path or "/"), name))


_ASP_HTTP = re.compile(r"\[\s*Http(Get|Post|Put|Delete|Patch)\s*(?:\(\s*(?:template\s*:\s*)?\"([^\"]*)\"[^)]*\))?\s*\]")
_ASP_ROUTE = re.compile(r"\[\s*Route\s*\(\s*\"([^\"]*)\"")
_CS_METHOD = re.compile(r"\b(?:public|internal|protected)\s+(?:static\s+|async\s+|virtual\s+|override\s+)*"
                        r"[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")


def _scan_aspnet(rel: str, text: str, routes: list):
    """ASP.NET Core attribute routing: class `[Route("articles")]` (with the
    `[controller]` token) + action `[HttpPost("{slug}/favorite")]` (or an
    action-level `[Route]`). A global prefix convention (`api/...`) is left
    to base-prefix tolerance in matching."""
    lines = text.splitlines()
    prefix, ctrl = "", ""
    for i, line in enumerate(lines):
        cm = re.search(r"\bclass\s+(\w+?)(Controller)?\b", line)
        if cm:
            ctrl = (cm.group(1) or "").lower()
            j = i - 1
            prefix = ""
            while j >= 0 and (lines[j].strip().startswith("[") or not lines[j].strip()):
                rm = _ASP_ROUTE.search(lines[j])
                if rm:
                    prefix = rm.group(1).replace("[controller]", ctrl)
                j -= 1
            continue
        hm = _ASP_HTTP.search(line)
        if not hm:
            continue
        verb, tmpl = hm.group(1).upper(), hm.group(2)
        # an action-level [Route] next to the verb attribute
        k = i + 1
        while k < len(lines) and lines[k].strip().startswith("["):
            rm = _ASP_ROUTE.search(lines[k])
            if rm and tmpl is None:
                tmpl = rm.group(1)
            k += 1
        name = ""
        for k2 in range(k, min(k + 4, len(lines))):
            mm = _CS_METHOD.search(lines[k2])
            if mm:
                name = mm.group(1)
                break
        tmpl = tmpl or ""
        path = tmpl if tmpl.startswith("/") else \
            ("/" + "/".join(x.strip("/") for x in (prefix, tmpl) if x.strip("/")))
        path = re.sub(r"\{(\w+)(?::[^}]*)?\??\}", r"{\1}", path)   # {id:int} -> {id}
        routes.append((rel, i + 1, verb, path or "/", name))


_PHP_ROUTE = re.compile(
    r"(?:\$\w+->|Route::)(get|post|put|patch|delete|options|any|match)\s*\(\s*"
    r"(?:\[[^\]]*\]\s*,\s*)?"                                   # Route::match(['get','post'], ...)
    r"['\"]([^'\"]*)['\"]\s*,\s*"
    r"(?:\[\s*\\?([\w\\]+)::class\s*,\s*['\"](\w+)['\"]\s*\]"      # [Ctrl::class, 'm']
    r"|['\"]([\w\\]+)@(\w+)['\"])", re.I)                          # 'Ctrl@m'


def _scan_php(rel: str, text: str, routes: list):
    """PHP routers: a project's own `$r->post('/p', [Ctrl::class, 'm'])`,
    Laravel `Route::post('/p', [Ctrl::class, 'm'])` / `'Ctrl@m'`. The handler
    is named `Class::method` and resolved by its qualified name."""
    import bisect
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    for m in _PHP_ROUTE.finditer(text):
        verb = m.group(1).upper()
        verb = "*" if verb in ("ANY", "MATCH") else verb
        cls = (m.group(3) or m.group(5) or "").rsplit("\\", 1)[-1]
        meth = m.group(4) or m.group(6) or ""
        path = "/" + m.group(2).strip("/") if m.group(2).strip("/") else "/"
        routes.append((rel, bisect.bisect_right(starts, m.start()), verb, path, f"{cls}::{meth}"))


def _scan_python_clients(rel: str, text: str, clients: list):
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute) or not n.args:
            continue
        verb = n.func.attr.lower()
        base = n.func.value
        bname = base.id if isinstance(base, ast.Name) else ""
        if verb not in VERBS or bname not in ("requests", "httpx", "session", "http_client"):
            continue
        a0 = n.args[0]
        url = None
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            url = a0.value
        elif isinstance(a0, ast.JoinedStr):
            url = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in a0.values)
        if url and _URLISH.match(url):
            clients.append((rel, n.lineno, url, verb.upper()))


_CS_CLIENT = re.compile(
    r"\.(Get|Post|Put|Delete|Patch)(?:FromJson|AsJson)?Async\(\s*(\$?)@?\"([^\"\n]*)\"")


def _scan_csharp_clients(rel: str, text: str, clients: list):
    """`client.PostAsync($"/api/articles/{slug}/favorite", ...)` in C# (mostly
    integration tests calling their own API): one client per call site."""
    for m in _CS_CLIENT.finditer(text):
        url = m.group(3)
        if m.group(2):
            url = re.sub(r"\{[^}]*\}", "{}", url)
        if not _URLISH.match(url):
            continue
        clients.append((rel, text.count("\n", 0, m.start()) + 1, url, m.group(1).upper()))


def link_http(store, repo_roots: dict) -> dict:
    cx = store.cx
    old = [r[0] for r in cx.execute("SELECT id FROM edges WHERE method=?", (METHOD,))]
    old_nodes = [r[0] for r in cx.execute(
        "SELECT id FROM nodes WHERE type='route' AND attributes LIKE '%\"http_link\": true%'")]
    for ids, tbl in ((old, "edges"), (old_nodes, "nodes")):
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            q = ",".join("?" * len(ch))
            cx.execute(f"DELETE FROM {tbl} WHERE id IN ({q})", ch)
            if tbl == "edges":
                cx.execute(f"DELETE FROM file_edges WHERE edge_id IN ({q})", ch)
            else:
                cx.execute(f"DELETE FROM file_nodes WHERE node_id IN ({q})", ch)
    stats = {"routes": 0, "clients": 0, "linked": 0, "ambiguous": 0, "unmatched": 0}
    new_nodes: list = []
    new_edges: list = []
    all_targets: list = []  # (segs, method, target node id, repo id)
    contexts: list = []     # (repo id, clients, fns, mods)
    for rid, root in sorted(repo_roots.items()):
        routes: list = []   # (rel, line, method, path, handler label)
        clients: list = []  # (rel, line, url, method)
        mounts: list = []   # (rel, prefix, local router name)
        go_routes: list = []  # + pending function name (routes on a router parameter)
        go_mounts: list = []  # (registering function name, group prefix)
        imports: dict = {}  # rel -> {local name: target rel}
        rels = [r[0].split(":", 1)[1] for r in cx.execute(
            "SELECT path FROM files WHERE path LIKE ?", (rid + ":%",))]
        rels_set = set(rels)
        for rel in sorted(rels):
            ext = Path(rel).suffix.lower()
            if ext not in _JS_EXT and ext not in (".go", ".java", ".kt", ".kts", ".py", ".cs", ".php"):
                continue
            try:
                raw = (Path(root) / rel).read_bytes()
            except OSError:
                continue
            if len(raw) > 2_000_000:
                continue
            text = raw.decode("utf-8", "ignore")
            if ext in _JS_EXT:
                if re.search(r"fetch\(|axios|url\s*:|\.(get|post|put|delete|del|patch|all|route|use)\s*[<(]", text):
                    _scan_js(rel, _JS_EXT[ext], raw, routes, clients, mounts)
                    imports[rel] = _js_imports(rel, text, rels_set)
            elif ext == ".go":
                if re.search(r"\.(GET|POST|PUT|DELETE|PATCH|Get|Post|Put|Delete|Patch|Handle|HandleFunc|Any|Group)\(\s*\"", text):
                    _scan_go(rel, raw, go_routes, go_mounts)
            elif ext in (".java", ".kt", ".kts"):
                if "Mapping" in text:
                    _scan_spring(rel, text, routes)
            elif ext == ".cs":
                if "[Http" in text and "Controller" in text:
                    _scan_aspnet(rel, text, routes)
                if "Async(" in text and _CS_CLIENT.search(text):
                    _scan_csharp_clients(rel, text, clients)
            elif ext == ".php":
                if "::class" in text or "@" in text:
                    _scan_php(rel, text, routes)
            elif ext == ".py" and ("requests." in text or "httpx." in text):
                _scan_python_clients(rel, text, clients)
        # Go routes registered on a router parameter take every prefix the
        # function is mounted with (`ArticlesRegister(v1.Group("/articles"))`)
        for rel, line, m, path, h, pending in go_routes:
            pres = sorted({g for f, g in go_mounts if f == pending}) if pending else [""]
            for pre in pres or [""]:
                routes.append((rel, line, m, (pre.rstrip("/") + ("/" + path.lstrip("/")
                               if path.strip("/") else "")) or "/", h))
        # router modules mounted under a prefix serve prefix + path only
        prefixes: dict = {}
        for rel, prefix, local in mounts:
            tgt_rel = (imports.get(rel) or {}).get(local)
            if tgt_rel:
                prefixes.setdefault(tgt_rel, set()).add(prefix)
        if prefixes:
            routes = [(rel, line, m, (p.rstrip("/") + ("/" + path.lstrip("/") if path.strip("/") else ""))
                       or "/", h)
                      for rel, line, m, path, h in routes
                      for p in sorted(prefixes.get(rel) or {""})]
        # symbol tables for handlers / enclosing functions
        fns: dict = {}
        handlers: dict = {}  # + `const getOrders = asyncHandler(async ...)` variables
        for nid, label, fp, sl, el, typ in cx.execute(
                "SELECT id, label, file_path, start_line, end_line, type FROM nodes WHERE repository_id=? "
                "AND type IN ('function','method','variable','constant')", (rid,)):
            if typ in ("function", "method"):
                fns.setdefault(fp, []).append((nid, label, sl or 0, el or 0))
            handlers.setdefault(fp, []).append((nid, label, sl or 0, el or 0))
        mods = {fp: nid for nid, fp in cx.execute(
            "SELECT id, file_path FROM nodes WHERE repository_id=? AND type='module'", (rid,))}
        targets: list = []  # (segs, method, target node id)
        for nid, attrs in cx.execute(
                "SELECT id, attributes FROM nodes WHERE repository_id=? AND type='route'", (rid,)):
            a = json.loads(attrs or "{}")
            if a.get("http_link"):
                continue
            h = cx.execute("SELECT target FROM edges WHERE source=? AND relation='handles_route'",
                           (nid,)).fetchone()
            for p in (a.get("full_paths") or [a.get("full_path") or a.get("raw_path") or ""]):
                targets.append((_segs(p), (a.get("http_method") or "*").upper(), h[0] if h else nid))
        for rel, line, method, path, handler in routes:
            stats["routes"] += 1
            tgt = None
            if handler and "::" in handler:
                cls, meth = handler.split("::", 1)
                hits = [r_[0] for r_ in cx.execute(
                    "SELECT id FROM nodes WHERE repository_id=? AND type IN ('method','function') AND label=? "
                    "AND (qualified_name LIKE ? OR qualified_name LIKE ?)",
                    (rid, meth, "%" + chr(92) + f"{cls}.{meth}", f"%.{cls}.{meth}"))] or                    [r_[0] for r_ in cx.execute(
                        "SELECT id FROM nodes WHERE repository_id=? AND type IN ('method','function') "
                        "AND qualified_name=?", (rid, f"{cls}.{meth}"))]
                tgt = hits[0] if len(hits) == 1 else None
            elif handler:
                hits = [nid for nid, lab, _s, _e in handlers.get(rel, []) if lab == handler]
                if len(hits) > 1:
                    # two actions with one name (`Get` list + `Get` by slug):
                    # the one declared right after this route attribute
                    near = [nid for nid, lab, s_, _e in handlers.get(rel, [])
                            if lab == handler and line <= s_ <= line + 6]
                    hits = near if len(near) == 1 else hits
                if not hits:
                    d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    hits = [nid for fp, lst in fns.items()
                            if (fp.rsplit("/", 1)[0] if "/" in fp else "") == d
                            for nid, lab, _s, _e in lst if lab == handler]
                if not hits and (imports.get(rel) or {}).get(handler):
                    # `import { getOrders } from '../controllers/orderController.js'`
                    hits = [nid for nid, lab, _s, _e in handlers.get(imports[rel][handler], [])
                            if lab == handler]
                tgt = hits[0] if len(hits) == 1 else None
            q = f"{rel}#{method} {path}"
            rnode = canonical_symbol_id(rid, "http", "route", q, "")
            # every server route is an entry point of its own (repo_map, arch
            # rules `for: {route: ..}`, change impact): a route node, linked to
            # its handler when the handler is known
            new_nodes.append((Node(rnode, "route", f"{method} {path}"[:200], q, "", rid, rel,
                                   Location(line, 0, line, 0), "", "",
                                   {"http_method": method, "full_path": path, "http_link": True,
                                    "handler": handler}, [], schema_version=2), rid + ":" + rel))
            if tgt is not None:
                new_edges.append((Edge(canonical_edge_id(rnode, "handles_route", tgt, rel, ""), rnode, tgt,
                                       "handles_route", "resolved", "resolved", 0.9, [], METHOD, VERSION,
                                       file_path=rel, start_line=line, attributes={"via": "route-registration"}),
                                  rid + ":" + rel))
            else:
                tgt = rnode
            targets.append((_segs(path), method, tgt))
        all_targets.extend((sg, m, t, rid) for sg, m, t in targets)
        contexts.append((rid, clients, fns, mods))

    def _best(cs, method, cands, penal=True):
        """(best score, [(target, repo, route segs, route method)]) of the top tier."""
        top, best_lit = [], -1
        for segs, rmethod, tgt, r in cands:
            if method and rmethod not in ("*", method):
                continue
            sc = _score_based(cs, segs, penal)
            if sc is None:
                continue
            if sc > best_lit:
                top, best_lit = [(tgt, r, segs, rmethod)], sc
            elif sc == best_lit and all(t != tgt for t, *_ in top):
                top.append((tgt, r, segs, rmethod))
        return best_lit, top

    # A frontend repo and its backend repo indexed together: a client binds
    # to its own repo's routes first, else to any other repo of the index.
    for rid, clients, fns, mods in contexts:
        own = [t for t in all_targets if t[3] == rid]
        other = [t for t in all_targets if t[3] != rid]
        for rel, line, url, method in clients:
            stats["clients"] += 1
            cs = _segs(url)
            if cs and cs[0] == "{}":
                cs = cs[1:]  # `${API_BASE}/users`: base URL variable
            best_lit, top = _best(cs, method, own)
            if not top or best_lit <= 0:
                # other repos each serve their own base path: no base penalty
                best_lit, top = _best(cs, method, other, penal=False)
            if not top or best_lit <= 0:
                stats["unmatched"] += 1
                continue
            cclass = "resolved"
            if len(top) > 1:
                # Same API served by several linked repos (two backends of
                # one frontend): link each, marked ambiguous. Two routes of
                # one repo tying is a real ambiguity: never guessed.
                if len({r for _t, r, *_ in top}) != len(top) or len(top) > 4:
                    stats["ambiguous"] += 1
                    continue
                cclass = "ambiguous"
            encl = [(e - s, nid) for nid, _l, s, e in fns.get(rel, []) if s <= line <= e]
            src = min(encl)[1] if encl else mods.get(rel)
            if src is None:
                continue
            site = site_fingerprint("http", method or "", url)
            for best, trepo, rsegs, rmethod in top:
                if best == src:
                    continue
                new_edges.append((Edge(canonical_edge_id(src, "calls_http", best, rel, site), src, best,
                                       "calls_http", cclass, cclass, 0.8 if cclass == "resolved" else 0.5,
                                       [], METHOD, VERSION, file_path=rel, start_line=line,
                                       attributes={"url": url[:300], "http_method": method,
                                                   "route": "/" + "/".join(rsegs), "route_method": rmethod,
                                                   "cross_repo": trepo != rid}),
                                  rid + ":" + rel))
            stats["linked"] += 1
    if new_nodes:
        store.upsert_nodes([n for n, _ in new_nodes])
        for n, fk in new_nodes:
            cx.execute("INSERT OR IGNORE INTO file_nodes VALUES(?,?)", (fk, n.id))
    if new_edges:
        store.upsert_edges([e for e, _ in new_edges])
        for e, fk in new_edges:
            cx.execute("INSERT OR IGNORE INTO file_edges VALUES(?,?)", (fk, e.id))
    return stats
