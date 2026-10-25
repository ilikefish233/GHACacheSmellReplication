"""按逻辑工作流键去重 workflow_files.jsonl。

逻辑工作流由 ``(repository_name, workflow_path)`` 唯一确定。
同一逻辑工作流的重复记录只保留首次出现的记录；不同逻辑工作流
即使共享同一个 ``content_file``，也不会被合并。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple


KEY_FIELDS = ("repository_name", "workflow_path")
COMPARE_FIELDS = (
    "repository_name",
    "workflow_path",
    "workflow_id",
    "file_sha",
    "content_hash",
    "content_file",
)


def read_unique_records(path: Path) -> Tuple[List[str], int, int, List[dict]]:
    """读取文件，返回首条记录文本、总行数、重复行数和冲突重复信息。"""
    seen: Dict[Tuple[str, str], dict] = {}
    unique_lines: List[str] = []
    total_rows = 0
    duplicate_rows = 0
    conflicts: List[dict] = []

    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, 1):
            line = raw_line.strip()
            if not line:
                continue

            record = json.loads(line)
            total_rows += 1
            key = tuple(record.get(field, "") for field in KEY_FIELDS)

            if key not in seen:
                seen[key] = record
                unique_lines.append(line)
                continue

            duplicate_rows += 1
            first = seen[key]
            differences = {
                field: (first.get(field), record.get(field))
                for field in COMPARE_FIELDS
                if first.get(field) != record.get(field)
            }
            if differences:
                conflicts.append(
                    {
                        "key": key,
                        "line_number": line_number,
                        "differences": differences,
                    }
                )

    return unique_lines, total_rows, duplicate_rows, conflicts


def main() -> None:
    parser = argparse.ArgumentParser(description="按仓库和工作流路径去重 workflow_files.jsonl")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()

    input_path = args.input.resolve()
    backup_path = args.backup.resolve()
    if not input_path.exists():
        raise SystemExit(f"输入文件不存在: {input_path}")
    if backup_path == input_path:
        raise SystemExit("备份文件不能与输入文件相同")

    unique_lines, total_rows, duplicate_rows, conflicts = read_unique_records(input_path)
    if conflicts:
        raise SystemExit(
            f"发现 {len(conflicts)} 个重复逻辑工作流存在字段冲突，未修改输入文件。"
        )

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        raise SystemExit(f"备份文件已存在，为避免覆盖请指定新路径: {backup_path}")
    shutil.copy2(input_path, backup_path)

    fd, temp_name = tempfile.mkstemp(
        prefix=f"{input_path.stem}.dedup.",
        suffix=input_path.suffix,
        dir=input_path.parent,
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            for line in unique_lines:
                target.write(line + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, input_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    print(f"input_rows={total_rows}")
    print(f"unique_logical_workflows={len(unique_lines)}")
    print(f"removed_duplicate_rows={duplicate_rows}")
    print(f"backup={backup_path}")
    print(f"output={input_path}")


if __name__ == "__main__":
    main()
