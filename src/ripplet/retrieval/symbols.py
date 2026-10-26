from __future__ import annotations
def lookup(store, name: str, limit: int = 20):
    exact = store.search_symbols(name, limit, partial=False)
    # an exact hit that is only an external symbol (`fixture:proxy`) must not hide
    # the repo's own `select_proxy` / `should_bypass_proxies`
    if any(n.type != "external_symbol" for n in exact): return exact
    seen = {n.id for n in exact}
    return exact + [n for n in store.search_symbols(name, limit, partial=True) if n.id not in seen]
def tokenize(name: str):
    import re
    return [t.lower() for t in re.split(r"(?<=[a-z])(?=[A-Z])|_+|-|\\s+", name) if t]
