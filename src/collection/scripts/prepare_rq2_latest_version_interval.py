"""Prepare the RQ2 cohort by latest-workflow-version effective intervals.

The RQ2 exposure is the workflow YAML that is current on the default branch at
the fixed study cutoff. A completed run is assigned to that exposure when:

* it has the same repository, workflow path, and workflow id;
* it runs on the repository default branch;
* its event is in the prespecified stable event set; and
* its creation time falls between the latest workflow-file content change that
  produced the current YAML and the study cutoff (optionally restricted to the
  final window_days).

In strict mode, a workflow without a verified retained workflow-history event
is excluded rather than assigned the window start as an inferred boundary.
The optional ``allow_inferred_history`` mode is retained only for sensitivity
analysis and marks such records explicitly as inferred.

This deliberately does not require fetching the YAML at every run's head_sha.
Ordinary source-code commits therefore do not split an unchanged workflow
version, while runs before a later workflow-file change do not inherit the
current smell label.
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
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPOSITORY_ROOT / "data_26_6_9_complete_20260609"
DEFAULT_SCAN = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_snapshot_repair_applied"
    / "smell_scan_results_at_cutoff.jsonl"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_version_interval_20260609_strict_20260915"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})

WorkflowKey = Tuple[str, str]
RunKey = Tuple[str, str]


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


def timestamp_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def normalize_repo(value: Any) -> str:
    return str(value or "").strip().lower()


def workflow_key(entry: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = normalize_repo(entry.get("repository_name"))
    path = str(entry.get("workflow_path") or "").strip()
    if not repository or not path:
        return None
    return repository, path


def run_key(entry: Mapping[str, Any]) -> Optional[RunKey]:
    repository = normalize_repo(entry.get("repository_name"))
    run_id = entry.get("run_id")
    attempt = entry.get("run_attempt") or 1
    if not repository or run_id in (None, ""):
        return None
    return str(run_id), str(attempt)


def scan_entries(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield either a direct scan record or its one-level nested record."""

    if workflow_key(record) is not None:
        yield record
        return
    for value in record.values():
        if isinstance(value, dict) and workflow_key(value) is not None:
            yield value


def load_current_workflows(
    path: Path,
) -> Tuple[Dict[WorkflowKey, Dict[str, Any]], set[WorkflowKey], Counter]:
    """Load current workflow snapshots and surface hash conflicts."""

    workflows: Dict[WorkflowKey, Dict[str, Any]] = {}
    conflicts: set[WorkflowKey] = set()
    stats: Counter = Counter()
    for entry in load_jsonl(path):
        stats["workflow_file_records"] += 1
        key = workflow_key(entry)
        content_hash = str(entry.get("content_hash") or "").strip().lower()
        if key is None or not content_hash:
            stats["workflow_records_missing_identity_or_hash"] += 1
            continue
        candidate = {
            "repository_name": str(entry.get("repository_name") or ""),
            "workflow_path": key[1],
            "workflow_id": str(entry.get("workflow_id") or "").strip(),
            "content_hash": content_hash,
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
    stats["current_repositories"] = len({key[0] for key in workflows})
    return workflows, conflicts, stats


def load_default_branches(path: Path) -> Dict[str, str]:
    branches: Dict[str, str] = {}
    for entry in load_jsonl(path):
        repository = entry.get("repo") or {}
        branch = str(repository.get("defaultBranch") or "").strip()
        if not branch:
            continue
        for alias in (entry.get("_id"), repository.get("name")):
            normalized = normalize_repo(alias)
            if normalized:
                branches.setdefault(normalized, branch)
    return branches


def load_scan_index(path: Path) -> Tuple[Dict[WorkflowKey, Dict[str, Any]], Counter]:
    """Index the current full-corpus detector output by workflow identity."""

    scans: Dict[WorkflowKey, Dict[str, Any]] = {}
    stats: Counter = Counter()
    for record in load_jsonl(path):
        stats["scan_records_read"] += 1
        for entry in scan_entries(record):
            key = workflow_key(entry)
            if key is None:
                continue
            detector = (entry.get("detectors") or {}).get("cache_smell") or {}
            candidate = {
                "repository_name": key[0],
                "workflow_path": key[1],
                "workflow_id": str(entry.get("workflow_id") or "").strip(),
                "file_sha": str(entry.get("file_sha") or "").strip(),
                "detector": detector,
            }
            previous = scans.get(key)
            if previous is not None:
                stats["duplicate_scan_identities"] += 1
                if previous.get("file_sha") != candidate.get("file_sha"):
                    stats["scan_identity_conflicts"] += 1
                continue
            scans[key] = candidate
    stats["scan_workflow_identities"] = len(scans)
    return scans, stats


def load_latest_effective_versions(
    path: Path,
    current_workflows: Mapping[WorkflowKey, Mapping[str, Any]],
    cutoff: datetime,
    window_start: Optional[datetime] = None,
    allow_inferred_history: bool = False,
) -> Tuple[Dict[WorkflowKey, Dict[str, Any]], Counter]:
    """Find the effective start of each current workflow version.

    A current version is usable when the latest retained workflow-change event
    on or before the cutoff has the same content hash as the current snapshot.
    If no history event is available, a fixed-window analysis may optionally
    use the current snapshot from ``window_start`` onward.  This fallback is
    disabled for the strict primary analysis because an unobserved boundary
    cannot establish when the current content became effective.  When enabled,
    such versions are marked as having an inferred, unobserved boundary.
    If the history ends with another hash, using that older matching event would
    incorrectly assign runs to the current exposure, so the workflow is left
    unmatched and reported explicitly.
    """

    history: Dict[WorkflowKey, list[Dict[str, Any]]] = defaultdict(list)
    stats: Counter = Counter()
    for entry in load_jsonl(path):
        stats["workflow_change_records_read"] += 1
        key = workflow_key(entry)
        commit_date = parse_timestamp(entry.get("commit_date"))
        content_hash = str(entry.get("content_hash") or "").strip().lower()
        commit_sha = str(entry.get("commit_sha") or "").strip()
        if key is None or commit_date is None or not content_hash or not commit_sha:
            stats["workflow_change_records_invalid"] += 1
            continue
        if commit_date > cutoff:
            stats["workflow_change_records_after_cutoff"] += 1
            continue
        history[key].append({
            "commit_date": commit_date,
            "commit_sha": commit_sha,
            "content_hash": content_hash,
            "content_file": str(entry.get("content_file") or "").strip(),
        })

    versions: Dict[WorkflowKey, Dict[str, Any]] = {}
    for key, current in current_workflows.items():
        events = sorted(
            history.get(key, []),
            key=lambda value: (
                value["commit_date"],
                value["commit_sha"],
                value["content_file"],
            ),
        )
        if not events:
            stats["current_workflows_missing_history"] += 1
            if window_start is not None and allow_inferred_history:
                versions[key] = {
                    **dict(current),
                    "last_change_at": window_start,
                    "last_change_commit_sha": None,
                    "history_content_file": current.get("content_file", ""),
                    "history_event_count": 0,
                    "history_boundary_inferred": True,
                }
                stats["current_workflows_history_boundary_inferred"] += 1
                stats["current_workflows_with_effective_start"] += 1
            continue
        latest = events[-1]
        if latest["content_hash"] != current["content_hash"]:
            stats["current_workflows_history_ends_at_different_hash"] += 1
            continue
        versions[key] = {
            **dict(current),
            "last_change_at": latest["commit_date"],
            "last_change_commit_sha": latest["commit_sha"],
            "history_content_file": latest["content_file"],
            "history_event_count": len(events),
            "history_boundary_inferred": False,
        }
        stats["current_workflows_with_effective_start"] += 1
    stats["history_timelines"] = len(history)
    stats["current_repositories_with_effective_start"] = len({
        key[0] for key in versions
    })
    return versions, stats


def prepare(
    data_dir: Path,
    smell_scan_path: Path,
    output_dir: Path,
    cutoff: datetime,
    window_days: Optional[float],
    events: Sequence[str] = tuple(sorted(DEFAULT_EVENTS)),
    runs_path: Optional[Path] = None,
    workflow_files_path: Optional[Path] = None,
    workflow_changes_path: Optional[Path] = None,
    allow_inferred_history: bool = False,
) -> Dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is non-empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_workflow_files_path = workflow_files_path or (data_dir / "workflow_files.jsonl")
    selected_workflow_changes_path = workflow_changes_path or (data_dir / "workflow_changes.jsonl")
    current_workflows, conflicts, workflow_stats = load_current_workflows(
        selected_workflow_files_path
    )
    scan_index, scan_stats = load_scan_index(smell_scan_path)
    default_branches = load_default_branches(data_dir / "repo_info.jsonl")
    window_start = (
        cutoff - timedelta(days=window_days)
        if window_days is not None
        else None
    )
    effective_versions, history_stats = load_latest_effective_versions(
        selected_workflow_changes_path,
        current_workflows,
        cutoff,
        window_start=window_start,
        allow_inferred_history=allow_inferred_history,
    )
    allowed_events = frozenset(
        str(value).strip().lower() for value in events if str(value).strip()
    )

    stats: Counter = Counter()
    stats.update(workflow_stats)
    stats.update(scan_stats)
    stats.update(history_stats)
    stats["default_branch_mappings"] = len(default_branches)
    stats["current_workflows_missing_scan"] = len(
        set(effective_versions) - set(scan_index)
    )
    stats["raw_run_records"] = 0
    stats["completed_run_records_total"] = 0
    stats["selected_run_records"] = 0
    stats["selected_workflow_identities"] = 0
    stats["selected_repositories"] = 0
    stats["excluded_after_cutoff"] = 0
    stats["excluded_before_window"] = 0
    stats["excluded_before_current_version"] = 0
    stats["excluded_outside_current_workflow_index"] = 0
    stats["excluded_missing_current_version_history"] = 0
    stats["excluded_missing_created_at"] = 0
    stats["excluded_missing_default_branch"] = 0
    stats["excluded_missing_head_branch"] = 0
    stats["excluded_non_default_branch"] = 0
    stats["excluded_event"] = 0
    stats["excluded_missing_workflow_id"] = 0
    stats["excluded_workflow_id_mismatch"] = 0
    stats["excluded_missing_current_scan"] = 0
    stats["excluded_missing_run_identity"] = 0
    stats["excluded_duplicate_run_attempt"] = 0
    stats["current_workflows_without_selected_run"] = 0

    selected_runs: list[Dict[str, Any]] = []
    seen_run_keys: set[RunKey] = set()
    version_stats: Dict[str, Counter] = defaultdict(Counter)

    selected_runs_path = runs_path or (data_dir / "workflow_runs.jsonl")
    for entry in load_jsonl(selected_runs_path):
        stats["raw_run_records"] += 1
        if str(entry.get("status") or "").strip().lower() != "completed":
            continue
        stats["completed_run_records_total"] += 1

        key = workflow_key(entry)
        if key is None or key not in current_workflows:
            stats["excluded_outside_current_workflow_index"] += 1
            continue
        if key not in effective_versions:
            stats["excluded_missing_current_version_history"] += 1
            continue
        version = effective_versions[key]
        scan = scan_index.get(key)
        if scan is None:
            stats["excluded_missing_current_scan"] += 1
            continue

        created_at = parse_timestamp(entry.get("created_at"))
        if created_at is None:
            stats["excluded_missing_created_at"] += 1
            continue
        if created_at > cutoff:
            stats["excluded_after_cutoff"] += 1
            continue
        if created_at < version["last_change_at"]:
            stats["excluded_before_current_version"] += 1
            continue
        if window_start is not None and created_at < window_start:
            stats["excluded_before_window"] += 1
            continue

        default_branch = default_branches.get(key[0])
        if not default_branch:
            stats["excluded_missing_default_branch"] += 1
            continue
        head_branch = entry.get("head_branch")
        if not isinstance(head_branch, str) or not head_branch:
            stats["excluded_missing_head_branch"] += 1
            continue
        if head_branch != default_branch:
            stats["excluded_non_default_branch"] += 1
            continue

        event = str(entry.get("event") or "").strip().lower()
        if allowed_events and event not in allowed_events:
            stats["excluded_event"] += 1
            continue

        current_workflow_id = str(version.get("workflow_id") or "").strip()
        run_workflow_id = str(entry.get("workflow_id") or "").strip()
        if not current_workflow_id or not run_workflow_id:
            stats["excluded_missing_workflow_id"] += 1
            continue
        if current_workflow_id != run_workflow_id:
            stats["excluded_workflow_id_mismatch"] += 1
            continue

        identity = run_key(entry)
        if identity is None:
            stats["excluded_missing_run_identity"] += 1
            continue
        if identity in seen_run_keys:
            stats["excluded_duplicate_run_attempt"] += 1
            continue
        seen_run_keys.add(identity)

        version_id = (
            f"{key[0]}|{key[1]}|content:{version['content_hash']}"
        )
        selected_runs.append({
            **dict(entry),
            "version_id": version_id,
            "logical_version_id": version_id,
            "repository_name": key[0],
            "workflow_path": key[1],
            "workflow_id": current_workflow_id,
            "commit_sha": version["last_change_commit_sha"],
            "commit_date": timestamp_text(version["last_change_at"]),
            "next_commit_date": timestamp_text(cutoff),
            "content_hash": version["content_hash"],
            "content_file": version["content_file"],
            "last_change_at": timestamp_text(version["last_change_at"]),
            "last_change_commit_sha": version["last_change_commit_sha"],
            "effective_end_at": timestamp_text(cutoff),
            "commit_date_is_run_time_proxy": False,
            "history_boundary_inferred": bool(
                version.get("history_boundary_inferred", False)
            ),
        })
        stats["selected_run_records"] += 1
        counters = version_stats[version_id]
        counters["completed_count"] += 1
        conclusion = str(entry.get("conclusion") or "").strip().lower()
        counters[f"conclusion:{conclusion}"] += 1
        if conclusion == "success":
            counters["success_count"] += 1
        if conclusion in {"success", "failure", "startup_failure", "timed_out"}:
            counters["technical_count"] += 1

    selected_runs.sort(key=lambda value: (
        str(value.get("version_id") or ""),
        str(value.get("created_at") or ""),
        str(value.get("run_id") or ""),
        str(value.get("run_attempt") or "1"),
    ))
    run_orders: Counter = Counter()
    for run in selected_runs:
        version_id = str(run["version_id"])
        run_orders[version_id] += 1
        run["run_order_in_version"] = run_orders[version_id]
        run["is_first_run_in_version"] = run_orders[version_id] == 1

    observed_version_ids = set(run_orders)
    selected_keys = {
        (str(run["repository_name"]), str(run["workflow_path"]))
        for run in selected_runs
    }
    stats["selected_workflow_identities"] = len(selected_keys)
    stats["selected_repositories"] = len({
        key[0] for key in selected_keys
    })
    stats["current_workflows_without_selected_run"] = (
        len(effective_versions) - len(selected_keys)
    )

    run_output = output_dir / "run_records.jsonl"
    with run_output.open("w", encoding="utf-8", newline="\n") as handle:
        for run in selected_runs:
            handle.write(json.dumps(run, ensure_ascii=False) + "\n")

    version_output = output_dir / "version_records.jsonl"
    scan_output = output_dir / "version_scan_results.jsonl"
    with version_output.open("w", encoding="utf-8", newline="\n") as version_handle, \
            scan_output.open("w", encoding="utf-8", newline="\n") as scan_handle:
        for key, version in sorted(effective_versions.items()):
            version_id = (
                f"{key[0]}|{key[1]}|content:{version['content_hash']}"
            )
            if version_id not in observed_version_ids:
                continue
            counters = version_stats[version_id]
            version_record = {
                "version_id": version_id,
                "logical_version_id": version_id,
                "repository_name": key[0],
                "workflow_path": key[1],
                "workflow_id": version["workflow_id"],
                "commit_sha": version["last_change_commit_sha"],
                "commit_date": timestamp_text(version["last_change_at"]),
                "next_commit_date": timestamp_text(cutoff),
                "content_hash": version["content_hash"],
                "content_file": version["content_file"],
                "last_change_at": timestamp_text(version["last_change_at"]),
                "effective_end_at": timestamp_text(cutoff),
                "sequence": 0,
                "completed_count": counters["completed_count"],
                "success_count": counters["success_count"],
                "technical_total_count": counters["technical_count"],
                "history_event_count": version["history_event_count"],
                "history_boundary_inferred": bool(
                    version.get("history_boundary_inferred", False)
                ),
            }
            version_handle.write(
                json.dumps(version_record, ensure_ascii=False) + "\n"
            )
            detector = scan_index[key]["detector"]
            scan_record = {
                "version_id": version_id,
                "logical_version_id": version_id,
                "repository_name": key[0],
                "workflow_path": key[1],
                "workflow_id": version["workflow_id"],
                "commit_sha": version["last_change_commit_sha"],
                "commit_date": timestamp_text(version["last_change_at"]),
                "next_commit_date": timestamp_text(cutoff),
                "content_hash": version["content_hash"],
                "file_sha": version["file_sha"],
                "content_file": version["content_file"],
                "last_change_at": timestamp_text(version["last_change_at"]),
                "effective_end_at": timestamp_text(cutoff),
                "detector": detector,
            }
            scan_handle.write(json.dumps(scan_record, ensure_ascii=False) + "\n")

    metadata = {
        **dict(stats),
        "study_cutoff": timestamp_text(cutoff),
        "window_days": window_days,
        "window_start": timestamp_text(window_start),
        "events": sorted(allowed_events),
        "branch_scope": "default branch only",
        "workflow_identity": (
            "(repository_name, workflow_path) with mandatory workflow_id "
            "equality and path cross-check"
        ),
        "matching_rule": (
            "A completed run is assigned to the latest workflow snapshot when "
            "last_change_at <= run.created_at <= study_cutoff, optionally "
            "after study_cutoff - window_days; exact YAML content matching at "
            "run head_sha is not required."
        ),
        "ordinary_commit_rule": (
            "Repository commits that do not change the workflow file do not "
            "create a new workflow version."
        ),
        "exposure_unit": "latest workflow snapshot content",
        "outcome_unit": "matched workflow run",
        "history_boundary": (
            "The effective start is the latest retained workflow-change event "
            "whose content hash equals the current snapshot. When no retained "
            "event exists, the workflow is excluded unless the optional "
            "inferred-boundary mode is enabled. A timeline ending at another "
            "hash is excluded rather than assigned by an older event."
        ),
        "workflow_files_path": str(selected_workflow_files_path),
        "workflow_changes_path": str(selected_workflow_changes_path),
        "repo_info_path": str(data_dir / "repo_info.jsonl"),
        "runs_path": str(selected_runs_path),
        "smell_scan_path": str(smell_scan_path),
        "run_records_path": str(run_output),
        "version_records_path": str(version_output),
        "version_scan_results_path": str(scan_output),
        "workflow_history_exact_match_required": False,
        "workflow_hash_conflicts_excluded": len(conflicts),
        "history_boundary_inference": (
            "Strict mode excludes workflows without a verified boundary; "
            "optional inferred-boundary mode includes them from window_start "
            "and marks history_boundary_inferred. Workflows whose retained "
            "history ends at a different hash remain excluded."
        ),
        "allow_inferred_history": bool(allow_inferred_history),
    }
    with (output_dir / "matching_metadata.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)

    print(f"Run records: {run_output}")
    print(f"Version records: {version_output}")
    print(f"Version scans: {scan_output}")
    print(json.dumps({
        key: metadata[key]
        for key in (
            "current_workflow_identities",
            "current_repositories",
            "current_workflows_with_effective_start",
            "current_repositories_with_effective_start",
            "current_workflows_missing_scan",
            "raw_run_records",
            "completed_run_records_total",
            "selected_run_records",
            "selected_workflow_identities",
            "selected_repositories",
            "current_workflows_without_selected_run",
        )
    }, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare RQ2 runs matched to the latest workflow version interval"
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--smell-scan", type=Path, default=DEFAULT_SCAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--runs-path",
        type=Path,
        default=None,
        help="Optional derived run JSONL; defaults to <data-dir>/workflow_runs.jsonl",
    )
    parser.add_argument(
        "--workflow-files-path",
        type=Path,
        default=None,
        help="Optional derived workflow index; defaults to <data-dir>/workflow_files.jsonl",
    )
    parser.add_argument(
        "--workflow-changes-path",
        type=Path,
        default=None,
        help="Optional merged workflow history; defaults to <data-dir>/workflow_changes.jsonl",
    )
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument(
        "--window-days",
        type=float,
        default=30.0,
        help="Final observation window length; use a negative value for the full effective period",
    )
    parser.add_argument(
        "--events",
        default=",".join(sorted(DEFAULT_EVENTS)),
        help="Comma-separated event names; an empty value keeps all events",
    )
    parser.add_argument(
        "--allow-inferred-history",
        action="store_true",
        help=(
            "include workflows without a verified last-change boundary from "
            "the observation-window start; intended only for sensitivity analysis"
        ),
    )
    args = parser.parse_args()

    cutoff = parse_timestamp(args.cutoff)
    if cutoff is None:
        raise SystemExit(f"Invalid cutoff timestamp: {args.cutoff}")
    window_days = None if args.window_days < 0 else args.window_days
    events = tuple(
        value.strip().lower()
        for value in args.events.split(",")
        if value.strip()
    )
    prepare(
        args.data_dir,
        args.smell_scan,
        args.output_dir,
        cutoff,
        window_days,
        events,
        runs_path=args.runs_path,
        workflow_files_path=args.workflow_files_path,
        workflow_changes_path=args.workflow_changes_path,
        allow_inferred_history=args.allow_inferred_history,
    )


if __name__ == "__main__":
    main()
