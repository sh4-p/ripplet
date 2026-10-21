from __future__ import annotations
import hashlib, json
def cache_key(content_hash: str, extractor: str, extractor_version: str, config_hash: str) -> str:
    return hashlib.sha256(f"{content_hash}|{extractor}|{extractor_version}|{config_hash}".encode()).hexdigest()[:24]
def is_cache_valid(stored: dict, content_hash: str, extractor_version: str, config_hash: str) -> bool:
    ch = stored.get("content_hash", stored.get("sha256"))
    return ch==content_hash and stored.get("extractor_version")==extractor_version and stored.get("config_hash")==config_hash
