# src/loaders/json_loader.py
import json
import gzip
from pathlib import Path
from typing import Iterator, Dict, Any, List

class JSONLoader:
    def __init__(self, file_path: Path):
        self.file_path = Path(file_path)
        self._validate()
    
    def _validate(self):
        if not self.file_path.exists():
            raise FileNotFoundError(f"JSON文件不存在: {self.file_path}")
    
    def _open_file(self):
        if self.file_path.suffix == '.gz':
            return gzip.open(self.file_path, 'rt', encoding='utf-8')
        return open(self.file_path, 'r', encoding='utf-8')
    
    def iter_records(self) -> Iterator[Dict[str, Any]]:
        with self._open_file() as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"第 {line_num} 行 JSON 解析失败: {e}")
                    continue
    
    def iter_batches(self, batch_size: int = 1000) -> Iterator[List[Dict]]:
        batch = []
        for record in self.iter_records():
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
    
    def count_records(self) -> int:
        count = 0
        with self._open_file() as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
    
    def load_all(self) -> List[Dict]:
        return list(self.iter_records())