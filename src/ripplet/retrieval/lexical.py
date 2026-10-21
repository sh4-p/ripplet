from __future__ import annotations
def search(store, term: str, limit: int = 20):
    return store.search_lexical(term, limit)
