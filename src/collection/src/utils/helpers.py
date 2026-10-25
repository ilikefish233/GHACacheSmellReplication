# src/utils/helpers.py
import json
from pathlib import Path
from typing import Any

def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path

def save_json(data: Any, file_path: Path, indent: int = 2):
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)

def load_json(file_path: Path) -> Any:
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)