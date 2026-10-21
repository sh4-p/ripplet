from __future__ import annotations
import subprocess
from dataclasses import dataclass
@dataclass
class CommitInfo:
    sha: str; author: str; date: str; message: str; files: list[str]
def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
def is_repo(path: str) -> bool:
    r = _run(["git","rev-parse","--is-inside-work-tree"], path)
    return r.returncode==0 and "true" in r.stdout
def log(path: str, limit: int = 50) -> list[CommitInfo]:
    r = _run(["git","log",f"-{limit}","--name-only","--pretty=format:%H|%an|%ad|%s","--date=iso"], path)
    if r.returncode!=0: return []
    out=[]; cur=None; files=[]
    for line in r.stdout.splitlines():
        if "|" in line and len(line)>40 and line[0] in "0123456789abcdef":
            if cur: out.append(CommitInfo(*cur, files))
            parts=line.split("|",3)
            cur=(parts[0][:40],parts[1],parts[2],parts[3] if len(parts)>3 else ""); files=[]
        elif line.strip(): files.append(line.strip())
    if cur: out.append(CommitInfo(*cur, files))
    return out
def diff_names(path: str, rev_a: str, rev_b: str = "") -> list[str]:
    args=["git","diff","--name-only",rev_a] + ([rev_b] if rev_b else [])
    r=_run(args, path)
    if r.returncode!=0: return []
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]
def current_sha(path: str) -> str:
    r=_run(["git","rev-parse","HEAD"], path)
    return r.stdout.strip() if r.returncode==0 else ""
