"""Query term extraction (Phase 4 Stage 3).

Splits a natural-language query into identifier-like spans (used for exact /
symbol lookup) and keyword spans (used for lexical search). Stopwords cover
English and Turkish question words. Quoted or backticked spans always win.
Deterministic, no models, no weights.
"""
from __future__ import annotations
import re

STOPWORDS = frozenset("""
what which where when how why who whom whose is are was were does do did has have had
can could should would will shall may might must the a an of to in on for with by from
as at and or not no if then than that this these those it its into out over under about
ne nasil nasıl ne nedir neden niçin nicin hangi hangisi kim kime kimi nerede nereden nereye
mi mı mu mü midir nedir degil değil var yok için gibi kadar çok bir ve veya ile de da ki
neye neyi yapar yapiyor yapıyor yapar eder olur olan is işi isi açıkla anlat göster bul listele
""".split())

CODEWORDS = frozenset("""
return import export from class def function func fn const let var new delete typeof
instanceof this super self await async yield raise try except finally with as pass
break continue lambda match case if else elif for while in is none true false null
undefined interface type enum implements extends struct union
""".split())

_QUOTED = re.compile(r"`([^`]+)`|'([^']+)'|\"([^\"]+)\"")
_IDENT = re.compile(r"[A-Za-z_][\w.]*")


def extract_terms(query: str) -> dict:
    """Returns {identifiers: [...], keywords: [...], scope: str|None}."""
    quoted: list[str] = []
    for m in _QUOTED.finditer(query or ""):
        span = next(g for g in m.groups() if g)
        span = span.strip().strip("`'\"")
        if span:
            quoted.append(span)
    bare = _QUOTED.sub(" ", query or "")
    idents: list[str] = []
    keywords: list[str] = []
    for tok in _IDENT.findall(bare):
        low = tok.lower().strip(".")
        if not low or low in STOPWORDS or low in CODEWORDS:
            continue
        # Every remaining token is identifier-eligible (short names like `run`
        # or `api` are real symbols); dotted/cased tokens stay preferred only
        # by order, never by exclusion.
        if tok not in idents:
            idents.append(tok)
        if "." not in tok and tok not in keywords:
            keywords.append(tok)
    for q in quoted:
        if q not in idents:
            idents.insert(0, q)
    scope = None
    scope_from = None
    dotted = [t for t in idents if "." in t]
    if dotted:
        scope = max(dotted, key=len)
        scope_from = "ident"
    elif keywords:
        # Shortest keyword: module/repository names ("pkg", "src") are short,
        # jargon ("structure", "modules") is long.
        scope = min(keywords, key=len)
        scope_from = "keyword"
    return {"identifiers": idents[:6], "keywords": keywords[:8], "scope": scope,
            "scope_from": scope_from}
