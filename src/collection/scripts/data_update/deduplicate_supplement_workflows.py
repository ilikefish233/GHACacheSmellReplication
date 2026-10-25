"""去重补抓工作流索引，保留原始 JSONL 作为审计记录。"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path


def deduplicate(input_path: Path, output_path: Path) -> dict:
    records = OrderedDict()
    conflicts = []
    raw_count = 0

    with input_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            raw_count += 1
            key = (record.get("repository_name"), record.get("workflow_path"))
            previous = records.get(key)
            if previous is not None:
                if previous.get("content_hash") != record.get("content_hash"):
                    conflicts.append(
                        {
                            "key": list(key),
                            "first_content_hash": previous.get("content_hash"),
                            "later_content_hash": record.get("content_hash"),
                            "line": line_number,
                        }
                    )
                # 保留最后一条，通常包含最新的 retrieved_at；内容哈希已核对。
            records[key] = record

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records.values():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "raw_records": raw_count,
        "deduplicated_records": len(records),
        "duplicate_records_removed": raw_count - len(records),
        "conflicting_duplicate_keys": len(conflicts),
        "conflicts": conflicts,
    }
    summary_path = output_path.with_name("workflow_files_deduplication_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="去重补抓工作流索引")
    parser.add_argument(
        "--input",
        default=r"data_26_6_9\supplement_missing_workflows_20260609\workflow_files.jsonl",
    )
    parser.add_argument(
        "--output",
        default=r"data_26_6_9\supplement_missing_workflows_20260609\workflow_files_deduplicated.jsonl",
    )
    args = parser.parse_args()
    print(json.dumps(deduplicate(Path(args.input), Path(args.output)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
