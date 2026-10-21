from __future__ import annotations
from abc import ABC, abstractmethod
class GraphStore(ABC):
    @abstractmethod
    def upsert_nodes(self, nodes): raise NotImplementedError
    @abstractmethod
    def upsert_edges(self, edges): raise NotImplementedError
    @abstractmethod
    def upsert_evidence(self, ev): raise NotImplementedError
    @abstractmethod
    def neighbors(self, node_id, direction="both", relations=None, limit=100): raise NotImplementedError
    @abstractmethod
    def search_lexical(self, term, limit=20): raise NotImplementedError
    @abstractmethod
    def get_node(self, node_id): raise NotImplementedError
class DocumentStore(ABC):
    @abstractmethod
    def put(self, key, value): raise NotImplementedError
    @abstractmethod
    def get(self, key): raise NotImplementedError
class LexicalIndex(ABC):
    @abstractmethod
    def index(self, node_id, text): raise NotImplementedError
    @abstractmethod
    def search(self, term, limit=20): raise NotImplementedError
