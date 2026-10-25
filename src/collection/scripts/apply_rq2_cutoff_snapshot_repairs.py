"""Apply verified cutoff YAML repairs and rebuild RQ2 derived inputs.

For a workflow whose stored snapshot was collected after the study cutoff, the
actual blob at the latest pre-cutoff path-changing commit is the correct RQ2
exposure.  This script materializes those blobs, reruns the cache-smell
detector, replaces the corresponding current-workflow records, and rewrites
the matching history boundary.  Base inputs are never modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.detectors.smell.cache_smell_detector import CacheSmellDetector  # noqa: E402
from src.parsers.workflow_parser import WorkflowParser  # noqa: E402


WorkflowKey = Tuple[str, str]


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


def key(record: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = str(record.get("repository_name") or "").strip().lower()
    path = str(record.get("workflow_path") or record.get("path") or "").strip()
    if not repository or not path:
        return None
    return repository, path


def safe_relative_content_path(value: Any) -> Path:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe content_file path: {value!r}")
    return Path("cutoff") / candidate


def scan_keys(record: Mapping[str, Any]) -> set[WorkflowKey]:
    found: set[WorkflowKey] = set()
    direct = key(record)
    if direct is not None:
        found.add(direct)
    for value in record.values():
        if isinstance(value, dict):
            nested = key(value)
            if nested is not None:
                found.add(nested)
    return found


def history_replacement(
    record: Mapping[str, Any],
    repair: Mapping[str, Any],
    content_file: str,
) -> Dict[str, Any]:
    return {
        **dict(record),
        "content_hash": repair["content_hash"],
        "content_file": content_file,
        "workflow_id": repair.get("workflow_id") or record.get("workflow_id"),
        "source": "github_latest_workflow_change_at_cutoff_graphql_snapshot_repair",
        "verification": "graphql_blob_at_cutoff_snapshot_repair",
        "cutoff_snapshot_repair": True,
    }


def apply_repairs(
    base_workflows: Path,
    base_scan: Path,
    base_history: Path,
    content_sources: list[Path],
    output_dir: Path,
) -> Dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is non-empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    repairs: Dict[WorkflowKey, Dict[str, Any]] = {}
    stats: Counter = Counter()
    for source in content_sources:
        for record in load_jsonl(source):
            stats["content_records_read"] += 1
            target_key = key(record)
            if target_key is None or not isinstance(record.get("content"), str):
                stats["content_records_invalid"] += 1
                continue
            repairs[target_key] = dict(record)
    stats["content_identities"] = len(repairs)

    parser = WorkflowParser()
    detector = CacheSmellDetector()
    usable: Dict[WorkflowKey, Dict[str, Any]] = {}
    scan_records: Dict[WorkflowKey, Dict[str, Any]] = {}
    files_dir = output_dir / "workflow_files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for target_key, repair in sorted(repairs.items()):
        content = str(repair["content"])
        try:
            content_path = safe_relative_content_path(repair.get("stored_content_file"))
            materialized = files_dir / content_path
            materialized.parent.mkdir(parents=True, exist_ok=True)
            materialized.write_text(content, encoding="utf-8", newline="")
            model = parser.parse_content(
                content,
                repository_name=repair.get("repository_name"),
                workflow_path=repair.get("workflow_path"),
                workflow_id=repair.get("workflow_id"),
                file_sha=repair.get("blob_sha"),
            )
            if model is None:
                raise ValueError("cutoff YAML cannot be parsed")
            result = detector.detect(model).to_dict()
        except Exception as exc:  # noqa: BLE001 - preserve per-workflow audit status
            repair["repair_error"] = str(exc)
            stats["repair_parse_or_scan_errors"] += 1
            continue

        relative_content_file = content_path.as_posix()
        repair["content_file"] = relative_content_file
        usable[target_key] = repair
        scan_records[target_key] = {
            "repository_name": repair.get("repository_name"),
            "workflow_path": repair.get("workflow_path"),
            "workflow_id": repair.get("workflow_id"),
            "file_sha": repair.get("blob_sha"),
            "content_hash": repair.get("content_hash"),
            "content_file": relative_content_file,
            "snapshot_commit_sha": repair.get("cutoff_commit_sha"),
            "snapshot_commit_date": repair.get("cutoff_commit_date"),
            "detectors": {"cache_smell": result},
        }
        stats["usable_repairs"] += 1

    workflow_output = output_dir / "workflow_files_at_cutoff.jsonl"
    with workflow_output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in load_jsonl(base_workflows):
            target_key = key(record)
            repair = usable.get(target_key) if target_key is not None else None
            if repair is None:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                continue
            handle.write(json.dumps({
                **record,
                "_post_cutoff_content_hash": record.get("content_hash"),
                "_post_cutoff_file_sha": record.get("file_sha"),
                "_post_cutoff_content_file": record.get("content_file"),
                "content_hash": repair.get("content_hash"),
                "file_sha": repair.get("blob_sha"),
                "content_file": repair.get("content_file"),
                "snapshot_commit_sha": repair.get("cutoff_commit_sha"),
                "snapshot_commit_date": repair.get("cutoff_commit_date"),
                "snapshot_source": "github_graphql_cutoff_blob",
                "snapshot_repaired_for_rq2": True,
            }, ensure_ascii=False) + "\n")
            stats["workflow_records_replaced"] += 1

    scan_output = output_dir / "smell_scan_results_at_cutoff.jsonl"
    repaired_keys = set(scan_records)
    with scan_output.open("w", encoding="utf-8", newline="\n") as handle:
        for target_key in sorted(scan_records):
            handle.write(json.dumps(scan_records[target_key], ensure_ascii=False) + "\n")
        for raw_record in load_jsonl(base_scan):
            if scan_keys(raw_record) & repaired_keys:
                stats["base_scan_records_replaced"] += 1
                continue
            handle.write(json.dumps(raw_record, ensure_ascii=False) + "\n")

    history_replacements: Dict[WorkflowKey, Dict[str, Any]] = {}
    for target_key, repair in usable.items():
        history_replacements[target_key] = repair
    history_output = output_dir / "workflow_changes_at_cutoff.jsonl"
    found_history: set[WorkflowKey] = set()
    missing_history_records: list[Dict[str, Any]] = []
    with history_output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in load_jsonl(base_history):
            target_key = key(record)
            repair = history_replacements.get(target_key) if target_key is not None else None
            if repair is not None and str(record.get("commit_sha") or "").strip() == str(repair.get("cutoff_commit_sha") or "").strip():
                replacement = history_replacement(record, repair, str(repair["content_file"]))
                handle.write(json.dumps(replacement, ensure_ascii=False) + "\n")
                found_history.add(target_key)
                stats["history_records_replaced"] += 1
            else:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    for target_key, repair in history_replacements.items():
        if target_key in found_history:
            continue
        missing_history_records.append({
            "_id": f"{repair['repository_name']}_{repair['workflow_path']}_{str(repair['cutoff_commit_sha'])[:12]}",
            "repository_name": repair["repository_name"],
            "workflow_path": repair["workflow_path"],
            "workflow_id": repair.get("workflow_id"),
            "commit_sha": repair["cutoff_commit_sha"],
            "commit_date": repair["cutoff_commit_date"],
            "content_hash": repair["content_hash"],
            "content_file": repair["content_file"],
            "prev_content_hash": None,
            "is_change": True,
            "source": "github_latest_workflow_change_at_cutoff_graphql_snapshot_repair",
            "verification": "graphql_blob_at_cutoff_snapshot_repair",
            "cutoff_snapshot_repair": True,
            "fetched_at": repair.get("fetched_at"),
        })
    if missing_history_records:
        with history_output.open("a", encoding="utf-8", newline="\n") as handle:
            for record in missing_history_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        stats["history_records_added"] += len(missing_history_records)

    metadata = {
        "base_workflows": str(base_workflows),
        "base_scan": str(base_scan),
        "base_history": str(base_history),
        "content_sources": [str(path) for path in content_sources],
        "output_dir": str(output_dir),
        "workflow_output": str(workflow_output),
        "scan_output": str(scan_output),
        "history_output": str(history_output),
        "repaired_identities": len(usable),
        **dict(stats),
        "external_fetch_performed": False,
    }
    (output_dir / "cutoff_snapshot_repair_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply validated RQ2 cutoff snapshot repairs")
    parser.add_argument("--base-workflows", type=Path, required=True)
    parser.add_argument("--base-scan", type=Path, required=True)
    parser.add_argument("--base-history", type=Path, required=True)
    parser.add_argument("--content-source", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    apply_repairs(
        args.base_workflows,
        args.base_scan,
        args.base_history,
        args.content_source,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
