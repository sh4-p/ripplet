"""Minimal SCIP index reader (no protobuf dependency).

SCIP (https://github.com/sourcegraph/scip) is the index format written by
compiler-backed indexers: scip-typescript, scip-python, scip-java,
rust-analyzer, scip-dotnet, scip-clang, scip-go ... Each document lists
occurrences of symbols with exact ranges and roles, resolved by the
language's own type checker. Ripplet reads it to compare its tree-sitter
graph against a compiler-grade one (differential testing) and, where an
index exists, to take precise call targets from it.

Only the fields Ripplet uses are decoded:

  Index        2: documents (Document)
  Document     1: relative_path  2: occurrences  3: symbols  4: language
  Occurrence   1: range (packed int32)  2: symbol  3: symbol_roles
               7: enclosing_range
  SymbolInfo   1: symbol  4: relationships  5: kind  6: display_name
  Relationship 1: symbol  2: is_reference  3: is_implementation
               4: is_type_definition  5: is_definition
"""
from __future__ import annotations

from dataclasses import dataclass, field

ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8
ROLE_GENERATED = 0x10
ROLE_TEST = 0x20
ROLE_FORWARD_DEFINITION = 0x40


@dataclass
class Occurrence:
    symbol: str
    line: int            # 0-based, as in SCIP
    col: int
    end_line: int
    end_col: int
    roles: int
    enclosing: tuple = ()

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)

    @property
    def is_import(self) -> bool:
        return bool(self.roles & ROLE_IMPORT)


@dataclass
class Relationship:
    symbol: str
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass
class SymbolInfo:
    symbol: str
    kind: int = 0
    display_name: str = ""
    relationships: list = field(default_factory=list)


@dataclass
class Document:
    path: str
    language: str = ""
    occurrences: list = field(default_factory=list)
    symbols: list = field(default_factory=list)


def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf, start=0, end=None):
    """(field number, wire type, value) of one message; length-delimited
    values come back as (start, end) offsets into buf."""
    i, end = start, len(buf) if end is None else end
    while i < end:
        key, i = _varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
            yield fn, wt, v
        elif wt == 2:
            ln, i = _varint(buf, i)
            yield fn, wt, (i, i + ln)
            i += ln
        elif wt == 1:
            yield fn, wt, buf[i:i + 8]
            i += 8
        elif wt == 5:
            yield fn, wt, buf[i:i + 4]
            i += 4
        else:                                   # groups: not used by SCIP
            raise ValueError(f"unsupported wire type {wt}")


def _str(buf, span) -> str:
    return bytes(buf[span[0]:span[1]]).decode("utf-8", errors="replace")


def _packed(buf, span) -> list:
    out, i = [], span[0]
    while i < span[1]:
        v, i = _varint(buf, i)
        out.append(v - (1 << 32) if v >= 1 << 31 else v)
    return out


def _range(r: list):
    if len(r) == 3:                             # single-line: line, start, end
        return r[0], r[1], r[0], r[2]
    if len(r) == 4:
        return r[0], r[1], r[2], r[3]
    return 0, 0, 0, 0


def _occurrence(buf, s, e) -> Occurrence:
    rng, sym, roles, encl = [], "", 0, []
    for fn, wt, v in _fields(buf, s, e):
        if fn == 1:
            rng = _packed(buf, v) if wt == 2 else rng + [v]
        elif fn == 8 and wt == 2 and not rng:
            # newer writers (scip-go): a range message {1 line, 2 start, 3 end}
            # or {1 start line, 2 start char, 3 end line, 4 end char}
            vals = {f_: x_ for f_, w_, x_ in _fields(buf, *v) if w_ == 0}
            if 4 in vals:
                rng = [vals.get(1, 0), vals.get(2, 0), vals.get(3, 0), vals[4]]
            else:
                rng = [vals.get(1, 0), vals.get(2, 0), vals.get(3, 0)]
        elif fn == 2:
            sym = _str(buf, v)
        elif fn == 3:
            roles = v
        elif fn == 7:
            encl = _packed(buf, v) if wt == 2 else encl + [v]
    l0, c0, l1, c1 = _range(rng)
    return Occurrence(sym, l0, c0, l1, c1, roles, tuple(_range(encl)) if encl else ())


def _relationship(buf, s, e) -> Relationship:
    r = Relationship("")
    for fn, _wt, v in _fields(buf, s, e):
        if fn == 1:
            r.symbol = _str(buf, v)
        elif fn == 2:
            r.is_reference = bool(v)
        elif fn == 3:
            r.is_implementation = bool(v)
        elif fn == 4:
            r.is_type_definition = bool(v)
        elif fn == 5:
            r.is_definition = bool(v)
    return r


def _symbol(buf, s, e) -> SymbolInfo:
    si = SymbolInfo("")
    for fn, _wt, v in _fields(buf, s, e):
        if fn == 1:
            si.symbol = _str(buf, v)
        elif fn == 4:
            si.relationships.append(_relationship(buf, *v))
        elif fn == 5:
            si.kind = v
        elif fn == 6:
            si.display_name = _str(buf, v)
    return si


def _document(buf, s, e) -> Document:
    d = Document("")
    for fn, _wt, v in _fields(buf, s, e):
        if fn == 1:
            d.path = _str(buf, v).replace("\\", "/")
        elif fn == 2:
            d.occurrences.append(_occurrence(buf, *v))
        elif fn == 3:
            d.symbols.append(_symbol(buf, *v))
        elif fn == 4:
            d.language = _str(buf, v)
    return d


def read_index(path: str):
    """Yield every Document of a .scip file."""
    with open(path, "rb") as f:
        buf = memoryview(f.read())
    for fn, _wt, v in _fields(buf):
        if fn == 2:
            yield _document(buf, *v)


def is_local(symbol: str) -> bool:
    return symbol.startswith("local ")


def descriptor_name(symbol: str) -> str:
    """Last descriptor's name: `npm pkg 1.0 src/`a.ts`/Foo#bar().` -> bar."""
    if not symbol or is_local(symbol):
        return ""
    tail = symbol.rstrip(".#/:!")
    if tail.endswith(")"):
        tail = tail[:tail.rfind("(")]
    # the name is after the last descriptor separator, possibly `backticked`
    if tail.endswith("`"):
        j = tail.rfind("`", 0, len(tail) - 1)
        return tail[j + 1:-1]
    for k in range(len(tail) - 1, -1, -1):
        if tail[k] in "/#.:!` ":
            return tail[k + 1:]
    return tail


def is_callable(symbol: str) -> bool:
    """SCIP method descriptors end with `().` (or `(+1).` for overloads)."""
    s = symbol.rstrip()
    return s.endswith(").") and "(" in s
