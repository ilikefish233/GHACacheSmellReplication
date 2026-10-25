# src/utils/checkpoint.py
import json
import threading
from pathlib import Path
from typing import Set, Dict, Any
from datetime import datetime

class CheckpointManager:
    def __init__(self, checkpoint_dir: Path):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.checkpoint_dir / "checkpoint.json"
        self.processed: Set[str] = set()
        self.metadata: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.checkpoint_file.exists():
            with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.processed = set(data.get('processed', []))
                self.metadata = data.get('metadata', {})
            print(f"加载检查点: {len(self.processed)} 条记录")

    def save(self):
        with self._lock:
            data = {
                'processed': list(self.processed),
                'metadata': self.metadata,
                'last_updated': datetime.now().isoformat(),
            }
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

    def mark_processed(self, item_id: str):
        with self._lock:
            self.processed.add(item_id)

    def is_processed(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self.processed

    def get_count(self) -> int:
        with self._lock:
            return len(self.processed)

    def set_metadata(self, key: str, value: Any):
        with self._lock:
            self.metadata[key] = value

    def get_metadata(self, key: str, default=None) -> Any:
        with self._lock:
            return self.metadata.get(key, default)

    def clear(self):
        with self._lock:
            self.processed.clear()
            self.metadata.clear()
            if self.checkpoint_file.exists():
                self.checkpoint_file.unlink()