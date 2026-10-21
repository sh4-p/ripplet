from __future__ import annotations
from abc import ABC, abstractmethod
from ...core.models import ExtractionResult
class CodeExtractor(ABC):
    name = "base"; version = "1"
    @abstractmethod
    def extract(self, rel_path: str, text: str, repo_id: str, project_id: str) -> ExtractionResult: raise NotImplementedError
