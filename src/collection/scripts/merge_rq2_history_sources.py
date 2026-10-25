"""Merge RQ2 workflow-history sources without contacting GitHub.

The base dataset already contains historical workflow-change records, while
RQ2 may add only the latest pre-cutoff boundary for workflows that were
missing from that history.  This utility keeps both sources, removes exact
commit duplicates, and writes a provenance-preserving derived history file.
It never modifies an input file or performs network access.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = [
    REPOSITORY_ROOT / "data_26_6_9_complete_20260609" / "workflow_changes.jsonl",
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_history_supplement_final"
    / "workflow_changes.jsonl",
]
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_input_merged_final"
    / "workflow_changes.jsonl"
)


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                yield value


def history_key(record: Mapping[str, Any]) -> Optional[Tuple[str, str, str]]:
    repository = str(record.get("repository_name") or "").strip().lower()
    path = str(record.get("workflow_path") or record.get("path") or "").strip()
    commit_sha = str(record.get("commit_sha") or "").strip().lower()
    if not repository or not path or not commit_sha:
        return None
    return repository, path, commit_sha


def richness(record: Mapping[str, Any]) -> int:
    fields = (
        "repository_name",
        "workflow_path",
        "workflow_id",
        "commit_sha",
        "commit_date",
        "content_hash",
        "content_file",
        "prev_content_hash",
        "source",
        "verification",
    )
    return sum(record.get(field) not in (None, "") for field in fields)


def merge_sources(sources: Sequence[Path], output: Path) -> Dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing history source(s): " + ", ".join(missing))

    records: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    source_counts: Counter[str] = Counter()
    invalid_counts: Counter[str] = Counter()
    duplicate_count = 0
    for source in sources:
        source_key = str(source)
        for record in load_jsonl(source):
            source_counts[source_key] += 1
            key = history_key(record)
            if key is None:
                invalid_counts[source_key] += 1
                continue
            previous = records.get(key)
            if previous is not None:
                duplicate_count += 1
                if richness(record) > richness(previous):
                    records[key] = {**record, "_merged_source": source_key}
                continue
            records[key] = {**record, "_merged_source": source_key}

    ordered = sorted(
        records.values(),
        key=lambda value: (
            str(value.get("repository_name") or "").lower(),
            str(value.get("workflow_path") or value.get("path") or ""),
            str(value.get("commit_date") or ""),
            str(value.get("commit_sha") or ""),
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in ordered:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    metadata = {
        "source_files": [str(path) for path in sources],
        "source_record_counts": dict(source_counts),
        "source_invalid_record_counts": dict(invalid_counts),
        "raw_records": sum(source_counts.values()),
        "unique_records": len(ordered),
        "duplicate_records_removed": duplicate_count,
        "deduplication_key": "(repository_name, workflow_path, commit_sha)",
        "output_file": str(output),
        "external_fetch_performed": False,
    }
    output.with_name("history_sources_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge existing RQ2 workflow-history sources")
    parser.add_argument(
        "--source",
        type=Path,
        action="append",
        default=None,
        help="Input history JSONL; may be repeated. Defaults to the final RQ2 sources.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    merge_sources(args.source or DEFAULT_SOURCES, args.output)


if __name__ == "__main__":
    main()
