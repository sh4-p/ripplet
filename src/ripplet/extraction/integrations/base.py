from __future__ import annotations
from abc import ABC, abstractmethod
class Connector(ABC):
    name="base"
    @abstractmethod
    def check(self) -> tuple[bool,str]: raise NotImplementedError
