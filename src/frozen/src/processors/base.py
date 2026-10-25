# src/processors/base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BaseProcessor(ABC):
    @abstractmethod
    def process(self, record: Any) -> Dict[str, Any]:
        pass
    
    def process_batch(self, batch: List[Any]) -> List[Dict[str, Any]]:
        return [self.process(item) for item in batch]
    
    def finalize(self) -> Dict[str, Any]:
        return {}