"""Laravel Blade templates -> PHP the PHP grammar can parse, line for line.

A `.blade.php` view is mostly HTML whose code lives in Blade syntax the PHP
grammar does not know (`{{ user()->name }}`, `@if(!user()->isGuest())`,
`@foreach($books as $book)`), so every call in a view was invisible. Each
construct becomes plain PHP on the same lines (newlines are kept, so every
call keeps its line number):

  {{ e }} / {!! e !!}        <?php echo (e); ?>
  @if(e) @elseif(e) @unless(e) @isset(e) @empty(e) @can(e) ...
                             <?php if (e) {} ?>
  @foreach(a as b) @forelse  <?php foreach (a as b) {} ?>
  @for(...) @while(e)        <?php for (...) {} ?> / <?php while (e) {} ?>
  @include('v', [..]) and other directives with arguments
                             <?php [args]; ?>
  @php ... @endphp           <?php ... ?>
  {{-- comment --}}, @endif, @csrf, ...   blanked

Only the code inside matters to the graph; the template's control flow is not
modelled (an `if` with an empty body).
"""
from __future__ import annotations

import re

_IF_LIKE = {"if", "elseif", "unless", "isset", "empty", "can", "cannot", "canany", "auth", "guest", "env",
            "production", "hasSection", "sectionMissing", "switch", "case", "error"}
_DIRECTIVE = re.compile(rb"@([A-Za-z_]\w*)")


def _blank(b: bytes) -> bytes:
    """Same length, newlines kept, everything else a space."""
    return bytes(c if c in (10, 13) else 32 for c in b)


def _paren_end(src: bytes, i: int) -> int:
    """Index after the parenthesis closing the one at src[i] ('('), strings skipped."""
    depth, j, quote = 0, i, 0
    while j < len(src):
        c = src[j]
        if quote:
            if c == 92:          # backslash escape
                j += 2
                continue
            if c == quote:
                quote = 0
        elif c in (39, 34):
            quote = c
        elif c == 40:
            depth += 1
        elif c == 41:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def blade_to_php(raw: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        if raw.startswith(b"{{--", i):
            j = raw.find(b"--}}", i + 4)
            j = n if j < 0 else j + 4
            out += _blank(raw[i:j])
            i = j
            continue
        if raw.startswith(b"{!!", i) or raw.startswith(b"{{", i):
            close = b"!!}" if raw.startswith(b"{!!", i) else b"}}"
            start = i + (3 if close == b"!!}" else 2)
            j = raw.find(close, start)
            if j < 0:
                out += raw[i:]
                break
            expr = raw[start:j]
            out += b"<?php echo (" + expr + b"); ?>"
            i = j + len(close)
            continue
        if raw.startswith(b"@php", i) and not raw[i + 4:i + 5].isalnum():
            j = raw.find(b"@endphp", i + 4)
            if j < 0:
                out += raw[i:]
                break
            out += b"<?php" + raw[i + 4:j] + b"?>" + b" " * (len(b"@endphp") - 2)
            i = j + len(b"@endphp")
            continue
        if raw[i] == 64 and (i == 0 or not (chr(raw[i - 1]).isalnum() or raw[i - 1] == 64)):
            m = _DIRECTIVE.match(raw, i)
            if m:
                name = m.group(1).decode("ascii", "ignore")
                k = m.end()
                while k < n and raw[k] in (32, 9):
                    k += 1
                if k < n and raw[k] == 40:
                    end = _paren_end(raw, k)
                    if end > 0:
                        inner = raw[k + 1:end - 1]
                        if name in ("foreach", "forelse"):
                            out += b"<?php foreach (" + inner + b") {} ?>"
                        elif name == "for":
                            out += b"<?php for (" + inner + b") {} ?>"
                        elif name == "while":
                            out += b"<?php while (" + inner + b") {} ?>"
                        elif name in _IF_LIKE:
                            out += b"<?php if (" + inner + b") {} ?>"
                        else:
                            out += b"<?php [" + inner + b"]; ?>"
                        i = end
                        continue
                # a bare directive (@endif, @csrf, @else): no code
                out += _blank(raw[i:m.end()])
                i = m.end()
                continue
        out.append(raw[i])
        i += 1
    return bytes(out)
