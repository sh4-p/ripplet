from __future__ import annotations
import json
def to_text(r: dict) -> str:
    lines=[r.get("answer","")]
    for f in r.get("facts",[])[:10]: lines.append(f"- {f.get('qualified_name') or f.get('label')} [{f.get('type')}] {f.get('file')}:{f.get('line')}")
    if r.get("limitations"):
        lines.append("Limitations:"); [lines.append(f"  ! {l}") for l in r["limitations"]]
    return "\n".join(lines)
def to_json(r: dict) -> str: return json.dumps(r, indent=2, default=str)
