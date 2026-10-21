from __future__ import annotations
REG: dict[str, dict] = {}
def register(suffixes: list[str], name: str, version: str = "1"):
    def deco(fn):
        for s in suffixes: REG[s.lower()] = {"fn": fn, "name": name, "version": version}
        return fn
    return deco
def lookup(filename: str):
    import pathlib
    return REG.get(pathlib.Path(filename).suffix.lower())
def supported_suffixes(): return sorted(REG.keys())
