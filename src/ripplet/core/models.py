from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCHEMA_VERSION = 2

@dataclass
class Location:
    start_line: int = 1
    start_column: int = 0
    end_line: int = 1
    end_column: int = 0
    def to_dict(self): return asdict(self)
    @staticmethod
    def from_dict(d: dict) -> "Location":
        return Location(d.get("start_line",1), d.get("start_column",0), d.get("end_line",1), d.get("end_column",0))

@dataclass
class Evidence:
    id: str
    source_uri: str
    file_hash: str = ""
    start_line: int = 0
    end_line: int = 0
    extraction_method: str = ""
    extraction_version: str = "1"
    backend: str = "local"
    quote: str = ""
    claim_hash: str = ""
    status: str = "exists"
    timestamp: float = field(default_factory=time.time)
    supersedes: list[str] = field(default_factory=list)
    def to_dict(self): return asdict(self)
    @staticmethod
    def from_dict(d: dict) -> "Evidence": return Evidence(**{k:v for k,v in d.items() if k in Evidence.__dataclass_fields__})

@dataclass
class Node:
    id: str
    type: str
    label: str
    qualified_name: str = ""
    project_id: str = "default"
    repository_id: str = "default"
    file_path: str = ""
    location: Location = field(default_factory=Location)
    language: str = ""
    content_hash: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance_ids: list[str] = field(default_factory=list)
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    valid_from: str = ""
    valid_until: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    def to_dict(self):
        d = asdict(self); d["location"] = self.location.to_dict() if isinstance(self.location, Location) else self.location; return d
    @staticmethod
    def from_dict(d: dict) -> "Node":
        d = dict(d)
        if isinstance(d.get("location"), dict): d["location"] = Location.from_dict(d["location"])
        return Node(**{k:v for k,v in d.items() if k in Node.__dataclass_fields__})

@dataclass
class Edge:
    id: str
    source: str
    target: str
    relation: str
    evidence_type: str = "structural"
    confidence_class: str = "deterministic"
    confidence_score: Optional[float] = None
    provenance_ids: list[str] = field(default_factory=list)
    extraction_method: str = ""
    extraction_version: str = "1"
    valid_from: str = ""
    valid_until: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    def to_dict(self): return asdict(self)
    @staticmethod
    def from_dict(d: dict) -> "Edge": return Edge(**{k:v for k,v in d.items() if k in Edge.__dataclass_fields__})

@dataclass
class ExtractionResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extractor: str = ""
    extractor_version: str = "1"
    partial: bool = False
    def merge(self, other: "ExtractionResult") -> "ExtractionResult":
        return ExtractionResult(self.nodes+other.nodes, self.edges+other.edges, self.evidence+other.evidence, self.warnings+other.warnings, self.extractor, self.extractor_version, self.partial or other.partial)
