from __future__ import annotations
def lookup(store, name: str, limit: int = 20):
    exact = store.search_symbols(name, limit, partial=False)
    if exact: return exact
    return store.search_symbols(name, limit, partial=True)
def tokenize(name: str):
    import re
    return [t.lower() for t in re.split(r"(?<=[a-z])(?=[A-Z])|_+|-|\\s+", name) if t]
