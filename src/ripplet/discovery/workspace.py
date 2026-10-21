from __future__ import annotations
import json, hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
@dataclass
class RepoEntry:
    id: str
    path: str
    name: str = ""
@dataclass
class Workspace:
    repos: list[RepoEntry] = field(default_factory=list)
    project_id: str = "default"
    @staticmethod
    def from_roots(roots: list[str], project_id: str = "default") -> "Workspace":
        repos=[]
        for r in roots:
            p = str(Path(r).resolve())
            rid = "repo-" + hashlib.sha256(p.encode()).hexdigest()[:10]
            repos.append(RepoEntry(rid, p, Path(p).name))
        return Workspace(repos, project_id)
    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"project_id": self.project_id, "repos": [asdict(r) for r in self.repos]}, indent=2), encoding="utf-8")
    @staticmethod
    def load(path: str) -> "Workspace":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return Workspace([RepoEntry(**r) for r in d.get("repos",[])], d.get("project_id","default"))
