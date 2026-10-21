"""investigate_error: from a stack trace or error message to the code.

Given what a test run or a log printed, this returns, without the agent
opening a single file:

  * the error line (type + message),
  * every stack frame that is in this repository, mapped to the enclosing
    function/method with the code on that line (library frames collapsed),
  * the frame where it happened (deepest repo frame) with its callers,
  * where the message is raised/thrown in the code, when the message text
    is literally in the source (works without a trace, e.g. from a log line).

Frame formats: Python, JavaScript/TypeScript (V8), Java/Kotlin, C#/.NET,
Go, PHP, Rust, and plain `path:line`.
"""
from __future__ import annotations

import re
from pathlib import Path

_FRAME_RX = [
    # Python: File "app/x.py", line 12, in run
    ("python", re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[\w<>.]+))?')),
    # C#: at Ns.Type.Method(args) in C:\src\x.cs:line 42
    ("csharp", re.compile(r"\bat (?P<func>[\w.<>`+\[\],]+)\(.*?\) in (?P<path>.+?):line (?P<line>\d+)")),
    # Java/Kotlin: at com.x.Type.method(Type.java:42)
    ("java", re.compile(r"\bat (?P<func>[\w$.<>]+)\((?P<path>[\w$.-]+\.(?:java|kt|scala|groovy)):(?P<line>\d+)\)")),
    # V8: at fn (path:12:5)  |  at path:12:5
    ("js", re.compile(r"\bat (?:(?P<func>[^\s(]+) \()?(?P<path>(?:file://)?[^\s()]+?\.(?:[cm]?[jt]sx?|vue|svelte)):(?P<line>\d+)(?::\d+)?\)?")),
    # PHP: #0 /var/www/app/X.php(42): App\X->run()  |  in /path/x.php on line 42  |  x.php:42
    ("php", re.compile(r"#\d+ (?P<path>[^\s(]+\.php)\((?P<line>\d+)\): (?P<func>[\w\\:>-]+)")),
    ("php", re.compile(r"in (?P<path>\S+\.php)(?::| on line )(?P<line>\d+)")),
    # Go: \t/home/u/proj/pkg/x.go:42 +0x1d   (function on the line before)
    ("go", re.compile(r"^\s*(?P<path>\S+\.go):(?P<line>\d+)(?: \+0x[0-9a-f]+)?\s*$", re.M)),
    # Rust: at src/x.rs:12:5
    ("rust", re.compile(r"(?:at |panicked at )'?(?P<path>[^\s:']+\.rs):(?P<line>\d+)(?::\d+)?")),
    # generic path:line (Ruby, Swift, Dart, C/C++, compiler errors, pytest short form)
    ("any", re.compile(r"(?P<path>[\w./\\-]+\.(?:rb|swift|dart|c|cc|cpp|h|hpp|py|ts|tsx|js|jsx|cs|java|kt|go|php|rs|lua))"
                       r"[:(](?P<line>\d+)")),
]
# anywhere on a line: log lines put a timestamp / level / logger name first
_ERR_RX = re.compile(r"(?:^|(?<=\s))(?:Uncaught |Unhandled exception\. |panic: |Fatal error: |PHP Fatal error:\s+)?"
                     r"(?P<type>(?:[\w.$\\]+\.)?\w*(?:Error|Exception|Failure|Panic|Fault|Warning)|panic|AssertionError)"
                     r"(?::|\s+-|\s+with message)\s*(?P<msg>.+)$", re.M)
_TEST_RX = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|_spec|\.test|\.spec|Tests?)\.[a-z]+$|"
                      r"(^|/)test_[^/]*$", re.I)


def _norm(p: str) -> str:
    p = p.strip().strip("'\"").replace("\\", "/")
    if p.startswith("file://"):
        p = p[7:]
    return re.sub(r"^/?[A-Za-z]:/", "/", p)


def parse_frames(text: str) -> list:
    """[(path, line, func)] in the order printed, deduplicated."""
    found = []
    taken = set()
    for kind, rx in _FRAME_RX:
        for m in rx.finditer(text):
            span = (m.start("path"), m.end("path"))
            if any(a <= span[0] < b for a, b in taken):
                continue                     # a more specific format already took this text
            taken.add((m.start(), m.end()))
            func = m.groupdict().get("func") or ""
            if kind == "go" and not func:
                prev = text[:m.start()].rstrip("\n").rsplit("\n", 1)[-1].strip()
                func = prev.split("(")[0] if "(" in prev else ""
            found.append((m.start(), _norm(m.group("path")), int(m.group("line")), func))
    found.sort()
    out, seen = [], set()
    for _pos, p, ln, fn in found:
        if (p, ln) in seen:
            continue
        seen.add((p, ln))
        out.append((p, ln, fn))
    return out


def _match_file(path: str, files: list, by_name: dict) -> str | None:
    """Indexed file a printed path refers to: the longest shared path suffix."""
    parts = [x for x in path.split("/") if x not in ("", ".")]
    if not parts:
        return None
    cands = by_name.get(parts[-1].lower(), [])
    best, best_n = None, 0
    for f in cands:
        fp = f.lower().split("/")
        n = 0
        for a, b in zip(reversed(fp), reversed([x.lower() for x in parts])):
            if a != b:
                break
            n += 1
        if n > best_n:
            best, best_n = f, n
    if best is None:
        return None
    if best_n == 1 and len(cands) > 1:
        return None                         # `index.js` alone matches many files: not decidable
    return best


def _lines_of(roots, rel, cache):
    if rel not in cache:
        cache[rel] = []
        for r in roots:
            p = Path(r) / rel
            if p.is_file():
                try:
                    cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    pass
                break
    return cache[rel]


def _enclosing(cx, rel: str, line: int):
    r = cx.execute(
        "SELECT id, qualified_name, type, start_line, end_line FROM nodes WHERE file_path=? "
        "AND type IN ('function','method','test','class','struct','record','interface','trait') "
        "AND start_line<=? AND (end_line>=? OR end_line IS NULL OR end_line=0) "
        "ORDER BY (CASE WHEN type IN ('function','method','test') THEN 0 ELSE 1 END), "
        "(COALESCE(end_line, start_line) - start_line) LIMIT 1", (rel, line, line)).fetchone()
    return r


def _callers(cx, nid: str, limit: int = 8) -> list:
    rows = cx.execute(
        "SELECT n.qualified_name, e.file_path, e.start_line FROM edges e JOIN nodes n ON n.id=e.source "
        "WHERE e.target=? AND e.relation IN ('calls','dispatches','instantiates') "
        "AND e.id NOT IN (SELECT edge_id FROM superseded_edges) LIMIT ?", (nid, limit + 1)).fetchall()
    return [f"{r[0]} @ {r[1]}:{r[2]}" for r in rows]


def _message_sites(cx, roots, msg: str, cache, limit: int = 6) -> list:
    """Source lines that contain the literal part of the message."""
    msg = msg.strip().strip("'\"")
    # the longest run without interpolated values (numbers, quoted values, ids)
    pieces = [p.strip(" :,.'\"") for p in re.split(r"'[^']*'|\"[^\"]*\"|\b\d[\w.-]*\b|<[^>]*>|\{[^}]*\}", msg)]
    pieces = sorted((p for p in pieces if len(p) >= 12), key=len, reverse=True)
    if not pieces:
        return []
    needle = pieces[0][:80]
    out = []
    files = [r[0] for r in cx.execute("SELECT DISTINCT file_path FROM nodes WHERE file_path<>''")]
    for rel in files:
        lines = _lines_of(roots, rel, cache)
        for i, t in enumerate(lines, 1):
            if needle in t:
                out.append((rel, i, t.strip()[:140]))
                if len(out) >= limit:
                    return out
    return out


def investigate_error(store, cfg, text: str) -> str:
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    files = [r[0].replace("\\", "/") for r in cx.execute("SELECT DISTINCT file_path FROM nodes WHERE file_path<>''")]
    by_name: dict = {}
    for f in files:
        by_name.setdefault(f.rsplit("/", 1)[-1].lower(), []).append(f)
    cache: dict = {}
    # PHP/.NET append the location to the message: `... in /x/Task.php:340`
    errs = [(m.group("type"), re.sub(r"\s+in \S+?(?::\d+| on line \d+)\s*$", "", m.group("msg").strip()))
            for m in _ERR_RX.finditer(text)]
    frames = parse_frames(text)
    out = ["# investigate_error"]
    if errs:
        t, msg = errs[-1] if text.lstrip().startswith("Traceback") else errs[0]
        out.append(f"Error: {t}: {msg[:300]}")
    repo_frames, lib = [], 0
    for p, ln, fn in frames:
        rel = _match_file(p, files, by_name)
        if rel is None:
            lib += 1
            continue
        repo_frames.append((rel, ln, fn))
    # Python prints the innermost call last; everyone else prints it first.
    innermost_last = bool(re.search(r"Traceback \(most recent call last\)", text))
    if repo_frames:
        where = repo_frames[-1] if innermost_last else repo_frames[0]
        out.append(f"{len(repo_frames)} frame(s) in this repository"
                   + (f", {lib} in libraries/runtime (not shown)" if lib else "")
                   + f". Innermost repo frame (where it happened): {where[0]}:{where[1]}")
        out.append("")
        out.append("=== Frames in this repository (" + ("outermost first" if innermost_last else "innermost first") + ")")
        for rel, ln, fn in repo_frames:
            enc = _enclosing(cx, rel, ln)
            lines = _lines_of(roots, rel, cache)
            code = lines[ln - 1].strip()[:160] if 1 <= ln <= len(lines) else "(line beyond end of file: the index or the trace is from other code)"
            sym = f"{enc[1]} ({enc[2]})" if enc else (fn or "module level")
            mark = "  <-- here" if (rel, ln, fn) == where else ""
            out.append(f"- {rel}:{ln}  in {sym}{'  (test)' if _TEST_RX.search(rel) else ''}{mark}")
            out.append(f"    {code}")
        rel, ln, _fn = where
        lines = _lines_of(roots, rel, cache)
        enc = _enclosing(cx, rel, ln)
        if lines:
            a, b = max(1, ln - 4), min(len(lines), ln + 3)
            out.append("")
            out.append(f"=== Code around {rel}:{ln}" + (f" in {enc[1]}" if enc else ""))
            out.extend(f"{'>' if i == ln else ' '}{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))
        if enc and enc[2] in ("function", "method"):
            cs = _callers(cx, enc[0])
            if cs:
                out.append("")
                out.append(f"=== Callers of {enc[1].rsplit('.', 1)[-1]} ({len(cs) if len(cs) <= 8 else '8+'})")
                out.extend("- " + c for c in cs[:8])
    elif frames:
        out.append(f"{len(frames)} frame(s) found, none in this repository's indexed files "
                   f"(library/runtime code, or paths from another checkout).")
    if errs:
        sites = _message_sites(cx, roots, errs[-1][1] if innermost_last else errs[0][1], cache)
        if sites:
            out.append("")
            out.append("=== Where this message is raised/thrown in the code (literal text match)")
            for rel, ln, code in sites:
                enc = _enclosing(cx, rel, ln)
                out.append(f"- {rel}:{ln}" + (f"  in {enc[1]}" if enc else ""))
                out.append(f"    {code}")
    if len(out) == 1:
        out.append("No stack frame, error type or message from this repository found in the text. "
                   "Paste the full trace, or use explain_symbol / repo_map.")
    out.append("")
    out.append("Next: explain_symbol(<function above>) for its full source and callers.")
    return "\n".join(out)
