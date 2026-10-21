from __future__ import annotations
import json
from pathlib import Path
def parse_manifests(root: str) -> dict:
    out: dict = {"packages": [], "deps": []}
    rootp = Path(root)
    pp = rootp/"pyproject.toml"
    if pp.exists():
        try:
            txt = pp.read_text(encoding="utf-8", errors="ignore")
            out["packages"].append({"file": "pyproject.toml", "kind": "python"})
            for line in txt.splitlines():
                s=line.strip()
                if s.startswith("dependencies") or s.startswith('"') or ("==" in s and len(s)<80):
                    out["deps"].append(s[:120])
        except OSError: pass
    pj = rootp/"package.json"
    if pj.exists():
        try:
            d = json.loads(pj.read_text(encoding="utf-8", errors="ignore"))
            out["packages"].append({"file": "package.json", "kind": "node", "name": d.get("name","")})
            for k in list(d.get("dependencies",{}))+list(d.get("devDependencies",{})):
                out["deps"].append(k)
        except Exception: pass
    req = rootp/"requirements.txt"
    if req.exists():
        try:
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                line=line.strip()
                if line and not line.startswith("#"): out["deps"].append(line[:120])
        except OSError: pass
    return out
