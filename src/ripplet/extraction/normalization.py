from __future__ import annotations
import re
SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|_+|-|\\s+")
def normalize_identifier(name: str) -> list[str]:
    parts = [p for p in SPLIT.split(name) if p]
    toks = []
    for p in parts:
        toks.append(p.lower())
    if name.lower() not in toks: toks.append(name.lower())
    return toks
def normalize_path(p: str) -> str: return p.replace("\\","/")
def truncate(s: str, n: int = 500) -> str: return s if len(s) <= n else s[:n]
