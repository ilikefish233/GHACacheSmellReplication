"""Merge the existing exact-snapshot RQ2 cohort with new history matches.

The existing cohort is retained as the authoritative source for runs that are
already matched to an exact workflow snapshot.  History-based records are
added only when their ``(run_id, run_attempt)`` is not already present.  This
prevents the supplementary collection from double-counting runs while still
allowing newly covered repositories and workflow versions to enter RQ2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple


DEFAULT_EXISTING_DIR = Path(
    "archive/legacy_experiment_results_20260915/"
    "output_full_logical_20260912_extended/rq2_exact_version_all11_20260609"
)
DEFAULT_HISTORY_DIR = Path(
    "data_26_6_9_complete_20260609/rq2_history_versioned"
)
DEFAULT_OUTPUT_DIR = Path(
    "archive/legacy_experiment_results_20260915/"
    "output_full_logical_20260914_complete/rq2_complete_version_matching"
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


def run_key(record: Mapping[str, Any]) -> Tuple[str, str] | Tuple[str, str, str, str, str]:
    run_id = record.get("run_id")
    if run_id not in (None, ""):
        return str(run_id), str(record.get("run_attempt") or 1)
    return (
        str(record.get("repository_name") or ""),
        str(record.get("workflow_path") or ""),
        str(record.get("head_sha") or ""),
        str(record.get("created_at") or ""),
        str(record.get("run_attempt") or 1),
    )


def merge_rq2_inputs(
    existing_dir: Path,
    history_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    existing_scan_path = existing_dir / "version_scan_results.jsonl"
    existing_runs_path = existing_dir / "run_records.jsonl"
    history_scan_path = history_dir / "version_scan_results.jsonl"
    history_runs_path = history_dir / "run_records.jsonl"
    for path in (
        existing_scan_path,
        existing_runs_path,
        history_scan_path,
        history_runs_path,
    ):
        if not path.exists():
            raise FileNotFoundError(path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is non-empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    existing_scans = list(load_jsonl(existing_scan_path))
    existing_runs = list(load_jsonl(existing_runs_path))
    history_scans = list(load_jsonl(history_scan_path))
    history_runs = list(load_jsonl(history_runs_path))

    existing_version_ids = {
        str(record.get("version_id") or "")
        for record in existing_scans
        if record.get("version_id")
    }
    existing_run_keys = {run_key(record) for record in existing_runs}
    added_runs = []
    duplicate_history_runs = 0
    for record in history_runs:
        key = run_key(record)
        if key in existing_run_keys:
            duplicate_history_runs += 1
            continue
        existing_run_keys.add(key)
        added_runs.append(record)

    added_version_ids = {
        str(record.get("version_id") or "")
        for record in added_runs
        if record.get("version_id")
    }
    selected_history_scans = []
    skipped_scan_version_ids = set()
    for record in history_scans:
        version_id = str(record.get("version_id") or "")
        if not version_id or version_id not in added_version_ids:
            continue
        if version_id in existing_version_ids:
            skipped_scan_version_ids.add(version_id)
            continue
        selected_history_scans.append(record)

    merged_scans = existing_scans + selected_history_scans
    merged_runs = existing_runs + added_runs
    output_scan_path = output_dir / "version_scan_results.jsonl"
    output_runs_path = output_dir / "run_records.jsonl"
    with output_scan_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in merged_scans:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with output_runs_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in merged_runs:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    existing_repositories = {
        str(record.get("repository_name") or "")
        for record in existing_runs
        if record.get("repository_name")
    }
    added_repositories = {
        str(record.get("repository_name") or "")
        for record in added_runs
        if record.get("repository_name")
    }
    metadata = {
        "existing_input_dir": str(existing_dir),
        "history_input_dir": str(history_dir),
        "output_dir": str(output_dir),
        "merge_rule": (
            "retain all existing exact-snapshot runs; append history-matched runs "
            "only when (run_id, run_attempt) is not already present"
        ),
        "existing_scan_records": len(existing_scans),
        "history_scan_records": len(history_scans),
        "merged_scan_records": len(merged_scans),
        "existing_run_records": len(existing_runs),
        "history_run_records": len(history_runs),
        "duplicate_history_runs_removed": duplicate_history_runs,
        "added_history_runs": len(added_runs),
        "merged_run_records": len(merged_runs),
        "existing_repositories": len(existing_repositories),
        "added_repositories": len(added_repositories),
        "merged_repositories": len(existing_repositories | added_repositories),
        "history_scan_versions_without_new_run": len(
            {
                str(record.get("version_id") or "")
                for record in history_scans
                if record.get("version_id")
            }
            - added_version_ids
        ),
        "output_version_scan_results": str(output_scan_path),
        "output_run_records": str(output_runs_path),
    }
    with (output_dir / "matching_metadata.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge exact and history-matched RQ2 inputs")
    parser.add_argument("--existing-dir", type=Path, default=DEFAULT_EXISTING_DIR)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    merge_rq2_inputs(args.existing_dir, args.history_dir, args.output_dir)


if __name__ == "__main__":
    main()
