from __future__ import annotations
import platform, sys, sqlite3, time
def collect(app_version: str = "") -> dict:
    from . import __version__
    app_version = app_version or __version__
    return {"app": "ripplet", "version": app_version, "python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
def format_text(d: dict) -> str:
    return "\n".join(f"{k}: {v}" for k,v in d.items())
