from __future__ import annotations
from dataclasses import dataclass
@dataclass
class TraceEvent:
    symbol: str; file: str; line: int; kind: str = "call"
def parse_trace_lines(lines: list[str]) -> list[TraceEvent]:
    out=[]
    for ln in lines:
        parts=ln.strip().split(":")
        if len(parts)>=3:
            try: out.append(TraceEvent(parts[0], parts[1], int(parts[2])))
            except ValueError: continue
    return out
