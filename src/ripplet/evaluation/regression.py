from __future__ import annotations
import json
from pathlib import Path
def save_baseline(path: str, results: dict):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(results,indent=2),encoding="utf-8")
def compare(baseline: dict, current: dict) -> dict:
    out={}
    for k in set(baseline)|set(current):
        out[k]={"baseline":baseline.get(k),"current":current.get(k),"regressed":baseline.get(k)!=current.get(k)}
    return out
