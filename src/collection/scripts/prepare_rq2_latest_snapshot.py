"""Prepare the exact latest-snapshot cohort used by the primary RQ2 analysis.

Each eligible run must already have an exact workflow YAML snapshot fetched at
its ``head_sha``.  The snapshot's SHA-256 content hash is compared with the
workflow file recorded as current at the fixed study cutoff.  A run is retained
only when those hashes are identical, so unrelated repository commits do not
split a workflow version and older workflow definitions cannot inherit current
smell labels.

The default cohort is restricted to completed default-branch runs created in
the 30 days ending at the study cutoff and to events whose workflow definition
can be interpreted without pull-request branch ambiguity: ``push``,
``schedule``, and ``workflow_dispatch``.
"""

from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import loads_smell_json


import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPOSITORY_ROOT / "data_26_6_9_complete_20260609"
DEFAULT_SCAN = (
    REPOSITORY_ROOT
    / "output_full_logical_20260914_complete"
    / "smell_scan_results.jsonl"
)
DEFAULT_EXACT_INPUT = (
    REPOSITORY_ROOT
    / "archive"
    / "legacy_experiment_results_20260915"
    / "output_full_logical_20260912_extended"
    / "rq2_exact_version_all11_20260609"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "archive"
    / "legacy_experiment_results_20260915"
    / "output_full_logical_20260914_complete"
    / "rq2_latest_exact_20260609"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})

WorkflowKey = Tuple[str, str]
RunKey = Tuple[str, str, str]


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = loads_smell_json(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                yield value


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def workflow_key(entry: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = str(entry.get("repository_name") or "").strip().lower()
    path = str(entry.get("workflow_path") or "").strip()
    if not repository or not path:
        return None
    return repository, path


def scan_entries(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    if workflow_key(record) is not None:
        yield record
        return
    for value in record.values():
        if isinstance(value, dict) and workflow_key(value) is not None:
            yield value


def load_current_workflows(path: Path) -> Tuple[Dict[WorkflowKey, Dict[str, Any]], set[WorkflowKey], Counter]:
    workflows: Dict[WorkflowKey, Dict[str, Any]] = {}
    conflicts: set[WorkflowKey] = set()
    stats: Counter = Counter()
    for entry in load_jsonl(path):
        stats["workflow_file_records"] += 1
        key = workflow_key(entry)
        content_hash = str(entry.get("content_hash") or "").strip().lower()
        if key is None or not content_hash:
            stats["workflow_file_records_missing_identity_or_hash"] += 1
            continue
        candidate = {
            "content_hash": content_hash,
            "workflow_id": str(entry.get("workflow_id") or "").strip(),
            "file_sha": str(entry.get("file_sha") or "").strip(),
            "content_file": str(entry.get("content_file") or "").strip(),
            "study_as_of": str(entry.get("study_as_of") or "").strip(),
        }
        previous = workflows.get(key)
        if previous is not None and previous["content_hash"] != content_hash:
            conflicts.add(key)
            stats["workflow_identity_hash_conflicts"] += 1
            continue
        workflows[key] = candidate
    for key in conflicts:
        workflows.pop(key, None)
    stats["current_workflow_identities"] = len(workflows)
    return workflows, conflicts, stats


def load_default_branches(path: Path) -> Dict[str, str]:
    branches: Dict[str, str] = {}
    for entry in load_jsonl(path):
        repository = entry.get("repo") or {}
        branch = str(repository.get("defaultBranch") or "").strip()
        if not branch:
            continue
        for alias in (entry.get("_id"), repository.get("name")):
            normalized = str(alias or "").strip().lower()
            if normalized:
                branches.setdefault(normalized, branch)
    return branches


def run_key(entry: Mapping[str, Any]) -> Optional[RunKey]:
    repository = str(entry.get("repository_name") or "").strip().lower()
    run_id = entry.get("run_id")
    attempt = entry.get("run_attempt") or 1
    if not repository or run_id in (None, ""):
        return None
    return repository, str(run_id), str(attempt)


def eligible_run(
    entry: Mapping[str, Any],
    current_workflows: Mapping[WorkflowKey, Mapping[str, Any]],
    default_branches: Mapping[str, str],
    cutoff: datetime,
    window_start: datetime,
    events: frozenset[str],
) -> Tuple[bool, str, Optional[WorkflowKey], Optional[datetime]]:
    key = workflow_key(entry)
    if key is None or key not in current_workflows:
        return False, "outside_current_workflow_index", key, None

    created_at = parse_timestamp(entry.get("created_at"))
    if created_at is None:
        return False, "missing_created_at", key, None
    if created_at > cutoff:
        return False, "after_cutoff", key, created_at
    if created_at < window_start:
        return False, "before_window", key, created_at

    default_branch = default_branches.get(key[0])
    if not default_branch:
        return False, "missing_default_branch", key, created_at
    if str(entry.get("head_branch") or "").strip() != default_branch:
        return False, "non_default_branch", key, created_at

    event = str(entry.get("event") or "").strip().lower()
    if events and event not in events:
        return False, "excluded_event", key, created_at

    exact_hash = str(entry.get("content_hash") or "").strip().lower()
    if not exact_hash:
        return False, "missing_exact_content_hash", key, created_at
    if exact_hash != current_workflows[key]["content_hash"]:
        return False, "older_or_different_workflow_content", key, created_at

    current_workflow_id = str(current_workflows[key].get("workflow_id") or "").strip()
    run_workflow_id = str(entry.get("workflow_id") or "").strip()
    if current_workflow_id and run_workflow_id and current_workflow_id != run_workflow_id:
        return False, "workflow_id_mismatch", key, created_at

    if run_key(entry) is None:
        return False, "missing_run_identity", key, created_at
    return True, "selected", key, created_at


def prepare(
    workflow_files_path: Path,
    repo_info_path: Path,
    smell_scan_path: Path,
    exact_run_records_path: Path,
    output_dir: Path,
    cutoff: datetime,
    window_days: float,
    events: frozenset[str] = DEFAULT_EVENTS,
) -> Dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is non-empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    current_workflows, conflicts, workflow_stats = load_current_workflows(
        workflow_files_path
    )
    default_branches = load_default_branches(repo_info_path)
    window_start = cutoff - timedelta(days=window_days)

    stats: Counter = Counter(workflow_stats)
    stats["default_branch_mappings"] = len(default_branches)
    selected_run_keys: set[RunKey] = set()
    selected_versions: Dict[WorkflowKey, str] = {}
    order_inputs: Dict[str, list[Tuple[datetime, RunKey]]] = defaultdict(list)

    for entry in load_jsonl(exact_run_records_path):
        stats["exact_run_records_read"] += 1
        selected, reason, key, created_at = eligible_run(
            entry,
            current_workflows,
            default_branches,
            cutoff,
            window_start,
            events,
        )
        if not selected or key is None or created_at is None:
            stats[f"excluded_{reason}"] += 1
            continue
        identity = run_key(entry)
        assert identity is not None
        if identity in selected_run_keys:
            stats["excluded_duplicate_run_attempt"] += 1
            continue
        selected_run_keys.add(identity)
        version_id = (
            f"{key[0]}|{key[1]}|content:"
            f"{current_workflows[key]['content_hash']}"
        )
        selected_versions[key] = version_id
        order_inputs[version_id].append((created_at, identity))

    run_order: Dict[RunKey, int] = {}
    for version_id, records in order_inputs.items():
        records.sort(key=lambda item: (item[0], item[1]))
        for index, (_, identity) in enumerate(records, start=1):
            run_order[identity] = index

    runs_output = output_dir / "run_records.jsonl"
    written_run_keys: set[RunKey] = set()
    with runs_output.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in load_jsonl(exact_run_records_path):
            identity = run_key(entry)
            if identity is None or identity not in selected_run_keys:
                continue
            if identity in written_run_keys:
                continue
            key = workflow_key(entry)
            if key is None or key not in selected_versions:
                continue
            written_run_keys.add(identity)
            output = dict(entry)
            output["version_id"] = selected_versions[key]
            output["logical_version_id"] = selected_versions[key]
            output["current_snapshot_content_hash"] = current_workflows[key]["content_hash"]
            output["current_snapshot_file_sha"] = current_workflows[key]["file_sha"]
            output["snapshot_as_of"] = cutoff.isoformat().replace("+00:00", "Z")
            output["run_order_in_version"] = run_order[identity]
            output["is_first_run_in_version"] = run_order[identity] == 1
            handle.write(json.dumps(output, ensure_ascii=False) + "\n")
            stats["selected_run_records"] += 1

    selected_scan_keys: set[WorkflowKey] = set()
    version_scan_output = output_dir / "version_scan_results.jsonl"
    with version_scan_output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in load_jsonl(smell_scan_path):
            for entry in scan_entries(record):
                key = workflow_key(entry)
                if key is None or key not in selected_versions or key in selected_scan_keys:
                    continue
                detector = (entry.get("detectors") or {}).get("cache_smell") or {}
                current = current_workflows[key]
                output = {
                    "version_id": selected_versions[key],
                    "repository_name": key[0],
                    "workflow_path": key[1],
                    "workflow_id": current["workflow_id"],
                    "content_hash": current["content_hash"],
                    "file_sha": current["file_sha"],
                    "content_file": current["content_file"],
                    "snapshot_as_of": cutoff.isoformat().replace("+00:00", "Z"),
                    "detector": detector,
                }
                handle.write(json.dumps(output, ensure_ascii=False) + "\n")
                selected_scan_keys.add(key)
                stats["selected_version_scan_records"] += 1

    missing_scans = set(selected_versions) - selected_scan_keys
    stats["selected_workflows_missing_scan"] = len(missing_scans)
    stats["selected_workflow_identities"] = len(selected_versions)
    stats["selected_repositories"] = len({key[0] for key in selected_versions})
    stats["current_workflows_without_selected_exact_run"] = (
        len(current_workflows) - len(selected_versions)
    )

    metadata = {
        **dict(stats),
        "study_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "window_days": window_days,
        "window_start": window_start.isoformat().replace("+00:00", "Z"),
        "events": sorted(events),
        "branch_scope": "default branch only",
        "workflow_identity": "(repository_name, workflow_path) with workflow_id cross-check",
        "matching_rule": (
            "A completed run is retained only when the workflow YAML fetched at "
            "that run's head_sha has the same content_hash as the workflow's latest "
            "snapshot at the fixed study cutoff."
        ),
        "exposure_unit": "latest workflow snapshot content",
        "outcome_unit": "matched workflow run",
        "workflow_files_path": str(workflow_files_path),
        "repo_info_path": str(repo_info_path),
        "smell_scan_path": str(smell_scan_path),
        "exact_run_records_path": str(exact_run_records_path),
        "run_records_path": str(runs_output),
        "version_scan_results_path": str(version_scan_output),
        "workflow_hash_conflicts_excluded": len(conflicts),
    }
    with (output_dir / "matching_metadata.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare exact current/latest workflow snapshot runs for RQ2"
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--smell-scan", type=Path, default=DEFAULT_SCAN)
    parser.add_argument("--exact-input-dir", type=Path, default=DEFAULT_EXACT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--window-days", type=float, default=30.0)
    parser.add_argument(
        "--events",
        default=",".join(sorted(DEFAULT_EVENTS)),
        help="comma-separated event names; use an empty value to keep all events",
    )
    args = parser.parse_args()

    cutoff = parse_timestamp(args.cutoff)
    if cutoff is None:
        raise ValueError(f"Invalid cutoff timestamp: {args.cutoff}")
    if args.window_days <= 0:
        raise ValueError("--window-days must be positive")
    events = frozenset(
        value.strip().lower() for value in args.events.split(",") if value.strip()
    )
    result = prepare(
        args.data_dir / "workflow_files.jsonl",
        args.data_dir / "repo_info.jsonl",
        args.smell_scan,
        args.exact_input_dir / "run_records.jsonl",
        args.output_dir,
        cutoff,
        args.window_days,
        events,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
