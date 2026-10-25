"""Audit RQ2 run coverage for the canonical workflow data layer.

This is a local-only, read-only audit.  It connects every canonical workflow
and run with ``(repository_id, normalized_workflow_path)`` and keeps the
workflow ID as an identity field that is either present or recovered from a
unique set of already collected runs.  It never calls GitHub and never edits
the canonical input files.

The audit intentionally separates four evidence axes:

* detector validity;
* version-boundary validity;
* workflow-ID recovery/ambiguity; and
* run-query coverage.

The RQ2 qualifying predicate is kept aligned with
``build_canonical_dataset.py``: completed runs on the repository default
branch, one of the three supported events, and
``max(last_change_at, cutoff - 30 days) <= created_at <= cutoff``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data_26_6_9_canonical_20260609"
DEFAULT_OUTPUT_DIR = ROOT / "output_canonical_rq2_coverage_audit_20260919"
DEFAULT_STATUS_FILE = (
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_run_status_unmatched_90d_20260918"
    / "repository_fetch_status.jsonl"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_STATUS_WINDOW_DAYS = 90
EVENTS = {"push", "schedule", "workflow_dispatch"}
VERIFIED_BOUNDARY_STATUSES = {
    "verified_content_boundary",
    "verified_path_change_boundary",
}
RECOVERY_RULE = "unique_default_branch_window_run_id"
WorkflowKey = Tuple[str, str]


try:
    # Reuse the canonical parser/constants instead of maintaining a second
    # definition of the cutoff/event conventions.
    from scripts.build_canonical_dataset import (  # type: ignore
        DEFAULT_RQ2_EVENTS,
        normalize_path as canonical_normalize_path,
        parse_timestamp as canonical_parse_timestamp,
    )

    EVENTS = set(DEFAULT_RQ2_EVENTS)
except ImportError:  # pragma: no cover - supports direct isolated imports
    def canonical_normalize_path(value: Any) -> str:
        return str(value or "").strip().replace("\\", "/")

    def canonical_parse_timestamp(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield JSON objects with line-aware errors."""

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


def parse_timestamp(value: Any) -> Optional[datetime]:
    return canonical_parse_timestamp(value)


def normalize_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def normalize_path(value: Any) -> str:
    return canonical_normalize_path(value)


def normalize_alias(value: Any) -> str:
    return str(value or "").strip().lower()


def workflow_key_text(key: WorkflowKey) -> str:
    return f"{key[0]}|{key[1]}"


def source_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def run_id(value: Mapping[str, Any]) -> str:
    return normalize_id(value.get("run_id") or value.get("id"))


def run_attempt(value: Mapping[str, Any]) -> str:
    return normalize_id(value.get("run_attempt") or 1)


def run_primary_key(value: Mapping[str, Any], repository_id: str) -> Optional[Tuple[str, str, str]]:
    identifier = run_id(value)
    if not identifier:
        return None
    return repository_id, identifier, run_attempt(value)


@dataclass
class Repository:
    repository_id: str
    canonical_name: str
    aliases: Tuple[str, ...]
    default_branch: str
    primary_ecosystem: str


@dataclass
class RunAggregate:
    raw_window_run_count: int = 0
    completed_run_count: int = 0
    default_branch_run_count: int = 0
    supported_event_run_count: int = 0
    version_interval_run_count: int = 0
    qualifying_run_count: int = 0
    candidate_ids: Set[str] = field(default_factory=set)
    evidence_run_count: int = 0
    candidate_version_interval_counts: Counter[str] = field(default_factory=Counter)
    acquisition_sources: Counter[str] = field(default_factory=Counter)
    duplicate_run_keys: int = 0
    runs_missing_id: int = 0
    post_cutoff_records: int = 0


@dataclass
class WorkflowState:
    key: WorkflowKey
    snapshot: Dict[str, Any]
    aggregate: RunAggregate = field(default_factory=RunAggregate)
    boundary: Dict[str, Any] = field(default_factory=dict)
    rq1: Dict[str, Any] = field(default_factory=dict)
    rq2: Dict[str, Any] = field(default_factory=dict)
    # ``recovery_status`` is retained as the internal name used by the first
    # audit implementation.  It is emitted as the independent
    # ``workflow_id_status`` axis below.
    recovery_status: str = "missing_no_run_evidence"
    effective_workflow_id: str = ""
    recovered_workflow_id: Optional[str] = None
    query: Dict[str, Any] = field(default_factory=dict)
    query_coverage_status: str = "missing"
    run_evidence_status: str = "no_observed_runs"
    detector_valid: bool = False
    boundary_valid: bool = False
    coverage_confirmed: bool = False
    analysis_ready: bool = False
    provisional_analysis_included: bool = False
    final_analysis_included: bool = False
    retry_required: bool = False
    analysis_status: str = "query_status_missing"
    primary_exclusion_reason: str = ""
    recoverable_missing_data: bool = False
    # Backward-compatible alias for the old provisional analysis queue.  New
    # queue logic must use the explicit fields above.
    analysis_included: bool = False
    window_start: Optional[datetime] = None


def load_repositories(path: Path) -> Tuple[Dict[str, Repository], Dict[str, Set[str]]]:
    by_id: Dict[str, Repository] = {}
    aliases: Dict[str, Set[str]] = defaultdict(set)
    for record in load_jsonl(path):
        repository_id = normalize_id(record.get("repository_id"))
        if not repository_id:
            continue
        canonical_name = str(record.get("canonical_name") or "").strip()
        alias_values = {
            str(value).strip()
            for value in [canonical_name, *(record.get("repository_aliases") or [])]
            if str(value or "").strip()
        }
        current = by_id.get(repository_id)
        if current is None:
            current = Repository(
                repository_id=repository_id,
                canonical_name=canonical_name,
                aliases=tuple(sorted(alias_values, key=str.lower)),
                default_branch=str(record.get("default_branch") or "").strip(),
                primary_ecosystem=str(record.get("primary_ecosystem") or "Other").strip()
                or "Other",
            )
            by_id[repository_id] = current
        else:
            merged_aliases = set(current.aliases) | alias_values
            current.aliases = tuple(sorted(merged_aliases, key=str.lower))
            if not current.canonical_name and canonical_name:
                current.canonical_name = canonical_name
            if not current.default_branch:
                current.default_branch = str(record.get("default_branch") or "").strip()
            if not current.primary_ecosystem or current.primary_ecosystem == "Other":
                current.primary_ecosystem = (
                    str(record.get("primary_ecosystem") or "Other").strip() or "Other"
                )
        for alias in alias_values:
            aliases[normalize_alias(alias)].add(repository_id)
    return by_id, aliases


def resolve_repository_id(
    record: Mapping[str, Any],
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
) -> Optional[str]:
    repository_id = normalize_id(record.get("repository_id"))
    if repository_id and repository_id in repositories:
        return repository_id
    if repository_id and not record.get("repository_name"):
        return repository_id
    name = normalize_alias(
        record.get("canonical_name")
        or record.get("repository_name")
        or record.get("repository")
    )
    candidates = aliases.get(name, set())
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def workflow_key_from_record(
    record: Mapping[str, Any],
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
) -> Optional[WorkflowKey]:
    repository_id = resolve_repository_id(record, repositories, aliases)
    path = normalize_path(record.get("workflow_path") or record.get("path"))
    if not repository_id or not path:
        return None
    return repository_id, path


def load_workflow_states(
    snapshots_path: Path,
    boundaries_path: Path,
    rq1_path: Path,
    rq2_path: Path,
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
) -> Tuple[List[WorkflowState], Dict[WorkflowKey, WorkflowState]]:
    snapshots: Dict[WorkflowKey, Dict[str, Any]] = {}
    order: List[WorkflowKey] = []
    for record in load_jsonl(snapshots_path):
        key = workflow_key_from_record(record, repositories, aliases)
        if key is None:
            continue
        if key in snapshots:
            raise ValueError(f"Duplicate canonical workflow_key in {snapshots_path}: {workflow_key_text(key)}")
        snapshot = dict(record)
        snapshot["workflow_path"] = key[1]
        snapshot["repository_id"] = key[0]
        snapshots[key] = snapshot
        order.append(key)

    rq1: Dict[WorkflowKey, Dict[str, Any]] = {}
    for record in load_jsonl(rq1_path):
        key = workflow_key_from_record(record, repositories, aliases)
        if key is not None:
            rq1[key] = dict(record)

    boundaries: Dict[WorkflowKey, Dict[str, Any]] = {}
    for record in load_jsonl(boundaries_path):
        key = workflow_key_from_record(record, repositories, aliases)
        if key is not None:
            boundaries[key] = dict(record)

    rq2: Dict[WorkflowKey, Dict[str, Any]] = {}
    for record in load_jsonl(rq2_path):
        key = workflow_key_from_record(record, repositories, aliases)
        if key is not None:
            rq2[key] = dict(record)

    states: List[WorkflowState] = []
    by_key: Dict[WorkflowKey, WorkflowState] = {}
    for key in order:
        state = WorkflowState(
            key=key,
            snapshot=snapshots[key],
            boundary=boundaries.get(key, {}),
            rq1=rq1.get(key, {}),
            rq2=rq2.get(key, {}),
        )
        states.append(state)
        by_key[key] = state
    return states, by_key


def infer_status_window_days(path: Path, *records: Mapping[str, Any]) -> int:
    for record in records:
        for field_name in ("query_window_days", "window_days"):
            value = record.get(field_name)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
    match = re.search(r"(?<!\d)(\d+)d(?!\d)", str(path).replace("\\", "/"))
    if match:
        return int(match.group(1))
    return DEFAULT_STATUS_WINDOW_DAYS


def make_status_candidate(
    path: Path,
    repository_record: Mapping[str, Any],
    workflow_record: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    workflow = dict(workflow_record or {})
    query_window_days = infer_status_window_days(path, repository_record, workflow)
    raw_complete = workflow.get(
        "pagination_complete",
        workflow.get("complete", repository_record.get("pagination_complete", repository_record.get("complete"))),
    )
    status = str(workflow.get("status") or repository_record.get("status") or "").strip().lower()
    http_status = workflow.get("http_status", repository_record.get("http_status"))
    error = workflow.get("error") or repository_record.get("error")
    if status in {"error", "forbidden", "not_found"} or http_status not in (None, ""):
        axis = "api_unresolved"
    elif raw_complete is True and status in {"ok", "no_runs"}:
        axis = "complete" if query_window_days >= DEFAULT_WINDOW_DAYS else "unconfirmed"
    elif raw_complete is False:
        axis = "incomplete" if query_window_days == DEFAULT_WINDOW_DAYS else "unconfirmed"
    else:
        axis = "unconfirmed"
    return {
        "axis": axis,
        "status": status or None,
        "complete": raw_complete,
        "query_window_days": query_window_days,
        "pages": workflow.get("pages", repository_record.get("pages")),
        "records": workflow.get("records", repository_record.get("records")),
        "fetched_at": repository_record.get("fetched_at"),
        "error": error,
        "http_status": http_status,
        "termination_reason": workflow.get(
            "termination_reason", repository_record.get("termination_reason")
        ),
        "last_page_size": workflow.get(
            "last_page_size", repository_record.get("last_page_size")
        ),
        "source": source_label(path),
        "workflow_id": normalize_id(workflow.get("workflow_id")) or None,
        "workflow_path": normalize_path(workflow.get("workflow_path")),
    }


def load_query_statuses(
    paths: Sequence[Path],
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
) -> Tuple[Dict[WorkflowKey, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    by_workflow: Dict[WorkflowKey, List[Dict[str, Any]]] = defaultdict(list)
    by_repository: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for path in paths:
        if not path.exists():
            continue
        for repository_record in load_jsonl(path):
            repository_id = resolve_repository_id(repository_record, repositories, aliases)
            if not repository_id:
                continue
            repository_candidate = make_status_candidate(path, repository_record, None)
            repository_candidate["repository_id"] = repository_id
            by_repository[repository_id].append(repository_candidate)
            for workflow_record in repository_record.get("workflow_statuses") or []:
                if not isinstance(workflow_record, dict):
                    continue
                path_value = normalize_path(workflow_record.get("workflow_path"))
                if not path_value:
                    continue
                key = repository_id, path_value
                candidate = make_status_candidate(path, repository_record, workflow_record)
                candidate["repository_id"] = repository_id
                by_workflow[key].append(candidate)
    return by_workflow, by_repository


def select_query_status(
    key: WorkflowKey,
    by_workflow: Mapping[WorkflowKey, Sequence[Mapping[str, Any]]],
    by_repository: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    candidates = list(by_workflow.get(key, []))
    # Repository-level API failures are useful even when a fallback query did
    # not emit a workflow row.  Repository-level success is deliberately not
    # used as proof for an individual workflow without a workflow_status row.
    candidates.extend(
        candidate
        for candidate in by_repository.get(key[0], [])
        if candidate.get("axis") == "api_unresolved"
    )
    if not candidates:
        return {
            "query_status": "missing",
            "query_coverage_status": "missing",
            "coverage_confirmed": False,
            "query_status_source": None,
            "query_window_days": None,
            "query_pages": None,
            "query_records": None,
            "query_fetched_at": None,
            "query_error": None,
            "query_observed_status": None,
            "query_termination_reason": None,
            "query_last_page_size": None,
        }

    complete = [candidate for candidate in candidates if candidate.get("axis") == "complete"]
    direct_incomplete = [
        candidate for candidate in candidates if candidate.get("axis") == "incomplete"
    ]
    api_errors = [
        candidate for candidate in candidates if candidate.get("axis") == "api_unresolved"
    ]
    # Prefer direct 30-day evidence, then a complete longer query.  The
    # lexical source tie-breaker keeps repeated audits deterministic.
    if complete:
        selected = sorted(
            complete,
            key=lambda item: (
                0 if item.get("query_window_days") == DEFAULT_WINDOW_DAYS else 1,
                int(item.get("query_window_days") or 0),
                str(item.get("source") or ""),
            ),
        )[0]
        query_status = "complete"
    elif direct_incomplete:
        selected = sorted(
            direct_incomplete,
            key=lambda item: (str(item.get("source") or ""),),
        )[0]
        query_status = "incomplete"
    elif api_errors:
        selected = sorted(
            api_errors,
            key=lambda item: (
                0 if item.get("query_window_days") == DEFAULT_WINDOW_DAYS else 1,
                str(item.get("source") or ""),
            ),
        )[0]
        query_status = "api_unresolved"
    else:
        # A longer-window query that hit max_pages does not prove that the
        # narrower 30-day query was incomplete.  Keep this axis unresolved.
        selected = sorted(
            candidates,
            key=lambda item: (
                int(item.get("query_window_days") or 0),
                str(item.get("source") or ""),
            ),
        )[0]
        query_status = "missing"

    return {
        "query_status": query_status,
        "query_coverage_status": query_status,
        "coverage_confirmed": query_status == "complete",
        "query_status_source": selected.get("source"),
        "query_window_days": selected.get("query_window_days"),
        "query_pages": selected.get("pages"),
        "query_records": selected.get("records"),
        "query_fetched_at": selected.get("fetched_at"),
        "query_error": selected.get("error"),
        "query_observed_status": selected.get("status"),
        "query_termination_reason": selected.get("termination_reason"),
        "query_last_page_size": selected.get("last_page_size"),
    }


def load_integrated_coverage_evidence(
    path: Optional[Path],
) -> Dict[WorkflowKey, Dict[str, Any]]:
    """Load Phase 1.2 workflow-level evidence, if supplied."""

    if path is None or not path.exists():
        return {}
    result: Dict[WorkflowKey, Dict[str, Any]] = {}
    for record in load_jsonl(path):
        repository_id = normalize_id(record.get("repository_id"))
        workflow_path = normalize_path(record.get("workflow_path"))
        if not repository_id or not workflow_path:
            continue
        key = repository_id, workflow_path
        if key in result:
            raise ValueError(f"Duplicate integrated coverage evidence key in {path}: {key}")
        result[key] = record
    return result


def apply_integrated_coverage_evidence(
    query: Mapping[str, Any], evidence: Mapping[str, Any]
) -> Dict[str, Any]:
    """Replace the query axis with a local evidence decision.

    The evidence file is already the result of the conservative Phase 1.2
    selection.  Run evidence and identity axes remain untouched in the audit.
    """

    result = dict(query)
    status = str(evidence.get("query_coverage_status") or "missing")
    result.update(
        {
            "query_status": status,
            "query_coverage_status": status,
            "coverage_confirmed": bool(evidence.get("coverage_confirmed")),
            "query_status_source": evidence.get("evidence_source_file")
            or evidence.get("evidence_source"),
            "query_window_days": evidence.get("query_window_days"),
            "query_pages": evidence.get("pages"),
            "query_records": evidence.get("records"),
            "query_fetched_at": evidence.get("fetched_at"),
            "query_error": evidence.get("query_error"),
            "query_observed_status": evidence.get("status"),
            "query_termination_reason": evidence.get("termination_reason"),
            "query_last_page_size": evidence.get("last_page_size"),
            "query_evidence_source_type": evidence.get("evidence_source_type"),
            "query_evidence_local_recovery": bool(evidence.get("local_recovery")),
            "query_evidence_unconfirmed_reason": evidence.get(
                "coverage_unconfirmed_reason"
            ),
            "query_analysis_ready_before_overlay": evidence.get("analysis_ready"),
            "query_latest_acquisition_status": evidence.get(
                "latest_acquisition_status"
            ),
        }
    )
    return result


def boundary_window_start(
    boundary: Mapping[str, Any], cutoff: datetime, window_days: int
) -> Optional[datetime]:
    status = str(boundary.get("boundary_status") or "")
    if status not in VERIFIED_BOUNDARY_STATUSES:
        return None
    last_change = parse_timestamp(boundary.get("last_change_at"))
    if last_change is None:
        return None
    return max(last_change, cutoff - timedelta(days=window_days))


def acquisition_source(run: Mapping[str, Any]) -> str:
    source = str(run.get("source") or "").strip()
    if source:
        return source
    merged = str(run.get("_merged_source") or "").replace("\\", "/").lower()
    if "supplement" in merged or "targeted" in merged:
        return "supplemental_local_source"
    if merged:
        return "canonical_local_source"
    return "unknown"


def run_workflow_id(run: Mapping[str, Any]) -> str:
    return normalize_id(run.get("workflow_id"))


def identity_matches(state: WorkflowState, run: Mapping[str, Any], effective_id: str = "") -> bool:
    run_id_value = run_workflow_id(run)
    snapshot_id = normalize_id(state.snapshot.get("workflow_id"))
    expected = effective_id or snapshot_id
    if expected:
        return run_id_value == expected
    # A missing snapshot ID is intentionally path-based only until the unique
    # candidate-ID recovery decision is made.
    return True


def run_is_completed_default_event(
    run: Mapping[str, Any], repository: Repository
) -> Tuple[bool, bool, bool]:
    completed = str(run.get("status") or "").strip().lower() == "completed"
    default_branch = completed and str(run.get("head_branch") or "") == repository.default_branch
    supported_event = default_branch and str(run.get("event") or "").strip().lower() in EVENTS
    return completed, default_branch, supported_event


def run_in_window(run: Mapping[str, Any], cutoff: datetime, window_days: int) -> Optional[datetime]:
    created_at = parse_timestamp(run.get("created_at"))
    if created_at is None:
        return None
    if not (cutoff - timedelta(days=window_days) <= created_at <= cutoff):
        return None
    return created_at


def run_in_version_interval(
    created_at: Optional[datetime],
    window_start: Optional[datetime],
    cutoff: datetime,
) -> bool:
    return created_at is not None and window_start is not None and window_start <= created_at <= cutoff


def audit_run_aggregates(
    runs_path: Path,
    states_by_key: Mapping[WorkflowKey, WorkflowState],
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
    cutoff: datetime,
    window_days: int,
) -> Dict[str, int]:
    """Stream the canonical run file once and build all workflow counters."""

    seen_run_keys: Set[Tuple[str, str, str]] = set()
    stats: Counter[str] = Counter()
    for run in load_jsonl(runs_path):
        key = workflow_key_from_record(run, repositories, aliases)
        if key is None or key not in states_by_key:
            stats["runs_unmatched_canonical_workflow"] += 1
            continue
        state = states_by_key[key]
        repository = repositories.get(key[0])
        if repository is None:
            stats["runs_missing_repository_metadata"] += 1
            continue
        created_at = parse_timestamp(run.get("created_at"))
        if created_at is None:
            stats["runs_missing_created_at"] += 1
            continue
        if created_at > cutoff:
            state.aggregate.post_cutoff_records += 1
            stats["post_cutoff_runs"] += 1
            continue
        primary_key = run_primary_key(run, key[0])
        if primary_key is not None:
            if primary_key in seen_run_keys:
                state.aggregate.duplicate_run_keys += 1
                stats["duplicate_run_keys"] += 1
                continue
            seen_run_keys.add(primary_key)
        else:
            state.aggregate.runs_missing_id += 1

        completed, default_branch, supported_event = run_is_completed_default_event(run, repository)

        # Recovery evidence deliberately uses all locally collected runs up to
        # the cutoff, not only the 30-day analysis window.
        if completed and default_branch and supported_event:
            state.aggregate.evidence_run_count += 1
            candidate_id = run_workflow_id(run)
            if candidate_id:
                state.aggregate.candidate_ids.add(candidate_id)

        if not identity_matches(state, run):
            stats["runs_identity_mismatch"] += 1
            continue
        window_created_at = run_in_window(run, cutoff, window_days)
        if window_created_at is None:
            continue
        state.aggregate.raw_window_run_count += 1
        state.aggregate.acquisition_sources[acquisition_source(run)] += 1
        if not completed:
            continue
        state.aggregate.completed_run_count += 1
        if not default_branch:
            continue
        state.aggregate.default_branch_run_count += 1
        if not supported_event:
            continue
        state.aggregate.supported_event_run_count += 1
        if not run_in_version_interval(window_created_at, state.window_start, cutoff):
            continue
        state.aggregate.version_interval_run_count += 1
        candidate_id = run_workflow_id(run)
        if normalize_id(state.snapshot.get("workflow_id")):
            state.aggregate.qualifying_run_count += 1
        elif candidate_id:
            state.aggregate.candidate_version_interval_counts[candidate_id] += 1
    stats["seen_unique_run_keys"] = len(seen_run_keys)
    return dict(stats)


def finalize_recovery_and_counts(states: Sequence[WorkflowState]) -> None:
    for state in states:
        snapshot_id = normalize_id(state.snapshot.get("workflow_id"))
        candidates = sorted(state.aggregate.candidate_ids)
        if snapshot_id:
            state.recovery_status = "present"
            state.effective_workflow_id = snapshot_id
            state.recovered_workflow_id = None
            # For an ID-present snapshot, the aggregate already used exact ID
            # matching and therefore is the formal qualifying count.
            continue
        if len(candidates) == 1:
            state.recovery_status = "recovered_unique_run_id"
            state.effective_workflow_id = candidates[0]
            state.recovered_workflow_id = candidates[0]
            state.aggregate.qualifying_run_count = state.aggregate.candidate_version_interval_counts[
                candidates[0]
            ]
        elif len(candidates) > 1:
            state.recovery_status = "ambiguous_multiple_run_ids"
            state.effective_workflow_id = ""
            state.recovered_workflow_id = None
            state.aggregate.qualifying_run_count = 0
        else:
            state.recovery_status = "missing_no_run_evidence"
            state.effective_workflow_id = ""
            state.recovered_workflow_id = None
            state.aggregate.qualifying_run_count = 0


def classify_state(state: WorkflowState) -> None:
    """Populate the independent audit axes and derived queue flags.

    The old implementation used ``analysis_status`` as a priority-ordered
    explanation.  That made an unresolved query disappear behind an already
    observed run, and made a recovered ID look as if it needed an identity
    retry even when the local evidence was sufficient.  The fields below are
    deliberately calculated independently; ``analysis_status`` is retained
    only as a human-readable legacy classification and is never used to build
    either queue.
    """

    detector_valid = bool(state.rq1) and bool(state.rq1.get("eligible")) and bool(
        str(state.rq1.get("detector_state_key") or "").strip()
    )
    boundary_valid = state.window_start is not None
    query_status = str(
        state.query.get("query_coverage_status")
        or state.query.get("query_status")
        or "missing"
    )
    terminal_query = query_status in {"terminal_exclusion", "access_denied"}
    original_qualifying = json_int(state.rq2.get("qualifying_run_count"))
    aggregate = state.aggregate

    state.detector_valid = detector_valid
    state.boundary_valid = boundary_valid
    state.query_coverage_status = query_status
    state.coverage_confirmed = query_status == "complete"

    # Keep identity and run evidence separate.  A mismatch between the
    # canonical RQ2 claim and locally linked qualifying runs is useful audit
    # evidence, but it does not invalidate an otherwise usable unique ID.
    if aggregate.qualifying_run_count > 0 and original_qualifying == 0:
        state.run_evidence_status = "run_present_but_not_linked"
    elif aggregate.qualifying_run_count == 0 and original_qualifying > 0:
        state.run_evidence_status = "run_present_but_not_linked"
    elif aggregate.qualifying_run_count > 0:
        state.run_evidence_status = "has_qualifying_runs"
    elif aggregate.raw_window_run_count > 0 or json_int(state.query.get("query_records")) > 0:
        state.run_evidence_status = "only_out_of_scope_runs"
    else:
        state.run_evidence_status = "no_observed_runs"

    # This is the formal provisional eligibility rule.  A unique local
    # recovery is a resolved identity for this purpose; it is not itself a
    # reason to retry.
    state.analysis_ready = bool(
        detector_valid
        and boundary_valid
        and state.effective_workflow_id
        and aggregate.qualifying_run_count > 0
    )
    state.provisional_analysis_included = state.analysis_ready
    state.final_analysis_included = bool(state.analysis_ready and state.coverage_confirmed)

    # Coverage is an independent evidence axis.  The retry queue is therefore
    # based on the query axis and the two validity gates only, exactly as in
    # the Phase 1.1 specification.
    state.retry_required = bool(
        detector_valid
        and boundary_valid
        and not state.coverage_confirmed
        and not terminal_query
    )
    state.analysis_included = state.provisional_analysis_included

    if not detector_valid:
        state.analysis_status = "detector_invalid"
        state.primary_exclusion_reason = "workflow_not_in_valid_rq1_detector_cohort"
    elif not boundary_valid:
        state.analysis_status = "boundary_unresolved"
        state.primary_exclusion_reason = (
            str(state.boundary.get("boundary_status") or "missing_boundary_record")
        )
    elif query_status == "terminal_exclusion":
        state.analysis_status = "terminal_exclusion"
        state.primary_exclusion_reason = "workflow_endpoint_terminally_unavailable"
    elif query_status == "access_denied":
        state.analysis_status = "access_denied"
        state.primary_exclusion_reason = "repository_or_workflow_access_denied"
    elif state.run_evidence_status == "run_present_but_not_linked":
        state.analysis_status = "run_present_but_not_linked"
        if aggregate.qualifying_run_count > 0 and original_qualifying == 0:
            state.primary_exclusion_reason = (
                "local_qualifying_run_not_present_in_original_rq2_cohort"
            )
        else:
            state.primary_exclusion_reason = (
                "original_rq2_cohort_claims_run_missing_from_canonical_run_file"
            )
    elif state.run_evidence_status == "has_qualifying_runs":
        state.analysis_status = "has_qualifying_runs"
        state.primary_exclusion_reason = "has_qualifying_runs"
    elif state.run_evidence_status == "only_out_of_scope_runs":
        state.analysis_status = "only_out_of_scope_runs"
        state.primary_exclusion_reason = (
            "all_observed_runs_excluded_by_status_branch_event_or_version_interval"
        )
    elif state.coverage_confirmed:
        # A complete query with no observed runs is a substantive conclusion,
        # even when the snapshot has no workflow ID to recover.
        state.analysis_status = "confirmed_no_qualifying_run"
        state.primary_exclusion_reason = "complete_query_returned_zero_observed_runs"
    elif query_status == "incomplete":
        state.analysis_status = "query_incomplete"
        state.primary_exclusion_reason = "30_day_query_pagination_incomplete"
    elif query_status == "api_unresolved":
        state.analysis_status = "api_unresolved"
        state.primary_exclusion_reason = "API_query_failed_or_workflow_not_found"
    else:
        state.analysis_status = "query_status_missing"
        if state.query.get("query_window_days", 0) and int(
            state.query.get("query_window_days") or 0
        ) > DEFAULT_WINDOW_DAYS:
            state.primary_exclusion_reason = (
                "longer_window_query_incomplete_does_not_prove_30_day_coverage"
            )
        else:
            state.primary_exclusion_reason = "no_complete_query_status_evidence"

    state.recoverable_missing_data = bool(
        state.retry_required
        or state.run_evidence_status == "run_present_but_not_linked"
        or state.recovery_status == "ambiguous_multiple_run_ids"
    )


def provenance_fields(
    detector_version: str,
    taxonomy_version: str,
    cutoff_text: str,
    window_days: int,
    input_hashes: Mapping[str, Optional[str]],
) -> Dict[str, Any]:
    return {
        "detector_version": detector_version,
        "taxonomy_version": taxonomy_version,
        "cutoff": cutoff_text,
        "window_days": window_days,
        "input_file_sha256": dict(sorted(input_hashes.items())),
    }


def recovery_record(state: WorkflowState, provenance: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "workflow_key": workflow_key_text(state.key),
        "repository_id": state.key[0],
        "workflow_path": state.key[1],
        "old_workflow_id": normalize_id(state.snapshot.get("workflow_id")) or None,
        "recovered_workflow_id": state.recovered_workflow_id,
        "candidate_ids": sorted(state.aggregate.candidate_ids),
        "evidence_run_count": state.aggregate.evidence_run_count,
        "workflow_id_status": state.recovery_status,
        "recovery_rule": RECOVERY_RULE,
        **provenance,
    }


def audit_record(
    state: WorkflowState,
    repositories: Mapping[str, Repository],
    provenance: Mapping[str, Any],
) -> Dict[str, Any]:
    repository = repositories.get(state.key[0])
    aggregate = state.aggregate
    query = state.query
    snapshot = state.snapshot
    boundary = state.boundary
    rq2 = state.rq2
    return {
        "workflow_key": workflow_key_text(state.key),
        "repository_id": state.key[0],
        "repository_name": repository.canonical_name if repository else snapshot.get("canonical_name"),
        "workflow_path": state.key[1],
        "default_branch": repository.default_branch if repository else snapshot.get("default_branch"),
        "primary_ecosystem": repository.primary_ecosystem if repository else "Other",
        "workflow_id": state.effective_workflow_id or None,
        "old_workflow_id": normalize_id(snapshot.get("workflow_id")) or None,
        "recovered_workflow_id": state.recovered_workflow_id,
        "workflow_id_status": state.recovery_status,
        "candidate_workflow_ids": sorted(aggregate.candidate_ids),
        "workflow_id_evidence_run_count": aggregate.evidence_run_count,
        "detector_status": "valid" if state.rq1.get("eligible") else "invalid",
        "detector_valid": state.detector_valid,
        "detector_state_key": state.rq1.get("detector_state_key"),
        "boundary_status": boundary.get("boundary_status"),
        "boundary_valid": state.boundary_valid,
        "boundary_last_change_at": boundary.get("last_change_at"),
        "boundary_commit_sha": boundary.get("boundary_commit_sha"),
        "boundary_source": boundary.get("boundary_source"),
        "query_status": query.get("query_status"),
        "query_coverage_status": state.query_coverage_status,
        "coverage_confirmed": state.coverage_confirmed,
        "query_status_source": query.get("query_status_source"),
        "query_window_days": query.get("query_window_days"),
        "query_pages": query.get("query_pages"),
        "query_records": query.get("query_records"),
        "query_fetched_at": query.get("query_fetched_at"),
        "query_error": query.get("query_error"),
        "query_observed_status": query.get("query_observed_status"),
        "query_termination_reason": query.get("query_termination_reason"),
        "query_last_page_size": query.get("query_last_page_size"),
        "raw_window_run_count": aggregate.raw_window_run_count,
        "completed_run_count": aggregate.completed_run_count,
        "default_branch_run_count": aggregate.default_branch_run_count,
        "supported_event_run_count": aggregate.supported_event_run_count,
        "version_interval_run_count": aggregate.version_interval_run_count,
        "qualifying_run_count": aggregate.qualifying_run_count,
        "original_rq2_run_status": rq2.get("run_status"),
        "original_rq2_qualifying_run_count": json_int(rq2.get("qualifying_run_count")),
        "window_start": state.window_start.isoformat().replace("+00:00", "Z") if state.window_start else None,
        "run_evidence_status": state.run_evidence_status,
        "analysis_status": state.analysis_status,
        "primary_exclusion_reason": state.primary_exclusion_reason,
        "recoverable_missing_data": state.recoverable_missing_data,
        "analysis_ready": state.analysis_ready,
        "provisional_analysis_included": state.provisional_analysis_included,
        "final_analysis_included": state.final_analysis_included,
        "retry_required": state.retry_required,
        "analysis_included": state.analysis_included,
        "acquisition_sources": dict(sorted(aggregate.acquisition_sources.items())),
        **provenance,
    }


def analysis_workflow_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        **dict(record),
        "analysis_queue": "provisional_rq2_analysis_workflows",
        "analysis_queue_is_provisional": True,
    }


def retry_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(record)
    result["retry_queue"] = "rq2_retry_targets"
    query_status = str(record.get("query_coverage_status") or "missing")
    if record.get("analysis_ready"):
        result["retry_reason"] = "analysis_ready_but_query_coverage_unconfirmed"
    else:
        result["retry_reason"] = f"query_coverage_{query_status}"
    result["retry_reason_detail"] = record.get("primary_exclusion_reason")
    return result


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), ensure_ascii=False, allow_nan=False) + "\n")
            count += 1
    return count


def build_cross_counts(
    audit_records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    by_repository: Dict[str, Dict[str, Any]] = {}
    by_ecosystem: Dict[str, Dict[str, Any]] = {}
    by_source: Dict[str, Dict[str, Any]] = {}
    by_ecosystem_source: Dict[str, Dict[str, Any]] = {}

    def bump(bucket: Dict[str, Any], record: Mapping[str, Any]) -> None:
        bucket["workflow_count"] = int(bucket.get("workflow_count", 0)) + 1
        bucket["qualifying_workflow_count"] = int(
            bucket.get("qualifying_workflow_count", 0)
        ) + int(bool(record.get("provisional_analysis_included")))
        bucket["provisional_analysis_workflow_count"] = int(
            bucket.get("provisional_analysis_workflow_count", 0)
        ) + int(bool(record.get("provisional_analysis_included")))
        bucket["final_analysis_workflow_count"] = int(
            bucket.get("final_analysis_workflow_count", 0)
        ) + int(bool(record.get("final_analysis_included")))
        bucket["retry_workflow_count"] = int(bucket.get("retry_workflow_count", 0)) + int(
            bool(record.get("retry_required"))
        )
        bucket["qualifying_run_count"] = int(bucket.get("qualifying_run_count", 0)) + json_int(
            record.get("qualifying_run_count")
        )
        statuses = bucket.setdefault("analysis_status_counts", Counter())
        statuses[str(record.get("analysis_status") or "")] += 1
        query_statuses = bucket.setdefault("query_coverage_status_counts", Counter())
        query_statuses[str(record.get("query_coverage_status") or "missing")] += 1
        id_statuses = bucket.setdefault("workflow_id_status_counts", Counter())
        id_statuses[str(record.get("workflow_id_status") or "missing_no_run_evidence")] += 1
        run_statuses = bucket.setdefault("run_evidence_status_counts", Counter())
        run_statuses[str(record.get("run_evidence_status") or "no_observed_runs")] += 1

    for record in audit_records:
        repository_id = str(record.get("repository_id") or "")
        ecosystem = str(record.get("primary_ecosystem") or "Other")
        repo_bucket = by_repository.setdefault(
            repository_id,
            {
                "repository_name": record.get("repository_name"),
                "primary_ecosystem": ecosystem,
            },
        )
        bump(repo_bucket, record)
        eco_bucket = by_ecosystem.setdefault(ecosystem, {})
        bump(eco_bucket, record)
        sources = record.get("acquisition_sources") or {}
        if not sources:
            sources = {"no_local_run_record": 1}
        for source in sources:
            source_bucket = by_source.setdefault(source, {})
            bump(source_bucket, record)
            combined = by_ecosystem_source.setdefault(f"{ecosystem}|{source}", {})
            bump(combined, record)

    def finalize(value: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in sorted(value):
            item = dict(value[key])
            for counter_name in (
                "analysis_status_counts",
                "query_coverage_status_counts",
                "workflow_id_status_counts",
                "run_evidence_status_counts",
            ):
                if isinstance(item.get(counter_name), Counter):
                    item[counter_name] = dict(sorted(item[counter_name].items()))
            result[key] = item
        return result

    return {
        "by_repository": finalize(by_repository),
        "by_ecosystem": finalize(by_ecosystem),
        "by_acquisition_source": finalize(by_source),
        "by_ecosystem_and_acquisition_source": finalize(by_ecosystem_source),
    }


def build_query_id_qualifying_cross_counts(
    audit_records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Cross query coverage, workflow-ID status, and local qualifying runs."""

    counts: Dict[str, Dict[str, Any]] = {}
    for record in audit_records:
        query_status = str(record.get("query_coverage_status") or "missing")
        id_status = str(record.get("workflow_id_status") or "missing_no_run_evidence")
        qualifying_axis = (
            "has_qualifying_runs"
            if json_int(record.get("qualifying_run_count")) > 0
            else "no_qualifying_runs"
        )
        key = f"{query_status}|{id_status}|{qualifying_axis}"
        bucket = counts.setdefault(
            key,
            {
                "query_coverage_status": query_status,
                "workflow_id_status": id_status,
                "qualifying_run_axis": qualifying_axis,
                "workflow_count": 0,
                "qualifying_run_count": 0,
                "provisional_analysis_workflow_count": 0,
                "final_analysis_workflow_count": 0,
                "retry_workflow_count": 0,
            },
        )
        bucket["workflow_count"] += 1
        bucket["qualifying_run_count"] += json_int(record.get("qualifying_run_count"))
        bucket["provisional_analysis_workflow_count"] += int(
            bool(record.get("provisional_analysis_included"))
        )
        bucket["final_analysis_workflow_count"] += int(
            bool(record.get("final_analysis_included"))
        )
        bucket["retry_workflow_count"] += int(bool(record.get("retry_required")))
    return {key: counts[key] for key in sorted(counts)}


def analysis_run_record(
    run: Mapping[str, Any],
    state: WorkflowState,
    provenance: Mapping[str, Any],
    queue_name: str,
) -> Dict[str, Any]:
    result = dict(run)
    repository_id = state.key[0]
    result["repository_id"] = repository_id
    result["workflow_path"] = state.key[1]
    result["workflow_id"] = state.effective_workflow_id
    result["workflow_key"] = workflow_key_text(state.key)
    result["run_id"] = run_id(run)
    result["run_attempt"] = run_attempt(run)
    result["rq2_qualifying"] = True
    result["analysis_workflow_status"] = state.analysis_status
    result["workflow_id_status"] = state.recovery_status
    result["query_coverage_status"] = state.query_coverage_status
    result["run_evidence_status"] = state.run_evidence_status
    result["detector_valid"] = state.detector_valid
    result["boundary_valid"] = state.boundary_valid
    result["analysis_ready"] = state.analysis_ready
    result["coverage_confirmed"] = state.coverage_confirmed
    result["provisional_analysis_included"] = state.provisional_analysis_included
    result["final_analysis_included"] = state.final_analysis_included
    result["retry_required"] = state.retry_required
    result["analysis_queue"] = queue_name
    result["acquisition_source"] = acquisition_source(run)
    result.update(provenance)
    return result


def write_analysis_runs(
    runs_path: Path,
    output_path: Path,
    states_by_key: Mapping[WorkflowKey, WorkflowState],
    repositories: Mapping[str, Repository],
    aliases: Mapping[str, Set[str]],
    cutoff: datetime,
    window_days: int,
    provenance: Mapping[str, Any],
    inclusion_field: str,
    queue_name: str,
) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    seen: Set[Tuple[str, str, str]] = set()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for run in load_jsonl(runs_path):
            key = workflow_key_from_record(run, repositories, aliases)
            if key is None or key not in states_by_key:
                continue
            state = states_by_key[key]
            if not bool(getattr(state, inclusion_field)):
                continue
            repository = repositories.get(key[0])
            if repository is None:
                continue
            created_at = run_in_window(run, cutoff, window_days)
            if created_at is None:
                continue
            if not identity_matches(state, run, state.effective_workflow_id):
                continue
            completed, default_branch, supported_event = run_is_completed_default_event(run, repository)
            if not (completed and default_branch and supported_event):
                continue
            if not run_in_version_interval(created_at, state.window_start, cutoff):
                continue
            primary_key = run_primary_key(run, key[0])
            if primary_key is None:
                counts["runs_missing_primary_key"] += 1
                continue
            if primary_key in seen:
                counts["duplicate_analysis_run_keys"] += 1
                continue
            seen.add(primary_key)
            handle.write(
                json.dumps(
                    analysis_run_record(run, state, provenance, queue_name),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )
            counts["analysis_runs"] += 1
    counts["analysis_run_primary_keys"] = len(seen)
    return dict(counts)


def load_manifest(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Manifest is not an object: {path}")
    return value


def audit(
    manifest_path: Path,
    repositories_path: Path,
    snapshots_path: Path,
    rq1_path: Path,
    boundaries_path: Path,
    runs_path: Path,
    rq2_path: Path,
    status_paths: Sequence[Path],
    output_dir: Path,
    cutoff_text: str,
    window_days: int,
    coverage_evidence_path: Optional[Path] = None,
) -> Dict[str, Any]:
    cutoff = parse_timestamp(cutoff_text)
    if cutoff is None:
        raise ValueError(f"Invalid cutoff: {cutoff_text}")
    manifest = load_manifest(manifest_path)
    manifest_cutoff = str(manifest.get("study_cutoff") or "").strip()
    if manifest_cutoff and manifest_cutoff != cutoff_text:
        raise ValueError(
            f"Cutoff mismatch: manifest={manifest_cutoff}, requested={cutoff_text}"
        )

    required_inputs = [
        manifest_path,
        repositories_path,
        snapshots_path,
        rq1_path,
        boundaries_path,
        runs_path,
        rq2_path,
        *status_paths,
    ]
    if coverage_evidence_path is not None:
        required_inputs.append(coverage_evidence_path)
    input_hashes: Dict[str, Optional[str]] = {}
    for path in required_inputs:
        label = source_label(path)
        input_hashes[label] = sha256_file(path) if path.exists() else None

    repositories, aliases = load_repositories(repositories_path)
    states, states_by_key = load_workflow_states(
        snapshots_path,
        boundaries_path,
        rq1_path,
        rq2_path,
        repositories,
        aliases,
    )
    for state in states:
        state.window_start = boundary_window_start(state.boundary, cutoff, window_days)
    status_by_workflow, status_by_repository = load_query_statuses(
        status_paths, repositories, aliases
    )
    integrated_evidence = load_integrated_coverage_evidence(coverage_evidence_path)
    for state in states:
        query = select_query_status(state.key, status_by_workflow, status_by_repository)
        evidence = integrated_evidence.get(state.key)
        state.query = (
            apply_integrated_coverage_evidence(query, evidence)
            if evidence is not None
            else query
        )

    run_scan_stats = audit_run_aggregates(
        runs_path,
        states_by_key,
        repositories,
        aliases,
        cutoff,
        window_days,
    )
    finalize_recovery_and_counts(states)
    for state in states:
        classify_state(state)

    detector = manifest.get("detector") or {}
    detector_version = str(detector.get("detector_version") or "unknown")
    taxonomy_version = str(detector.get("taxonomy_version") or "unknown")
    provenance = provenance_fields(
        detector_version,
        taxonomy_version,
        cutoff_text,
        window_days,
        input_hashes,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_rows = [audit_record(state, repositories, provenance) for state in states]
    resolution_rows = [recovery_record(state, provenance) for state in states]
    recovery_rows = [
        row
        for row, state in zip(resolution_rows, states)
        if state.recovery_status
        in {"recovered_unique_run_id", "ambiguous_multiple_run_ids"}
    ]
    provisional_analysis_rows = [
        analysis_workflow_record(row)
        for row, state in zip(audit_rows, states)
        if state.provisional_analysis_included
    ]
    final_analysis_rows = [
        {
            **dict(row),
            "analysis_queue": "final_rq2_analysis_workflows",
            "analysis_queue_is_provisional": False,
        }
        for row, state in zip(audit_rows, states)
        if state.final_analysis_included
    ]
    retry_rows = [
        retry_record(row)
        for row, state in zip(audit_rows, states)
        if state.retry_required
    ]

    audit_count = write_jsonl(output_dir / "rq2_coverage_audit.jsonl", audit_rows)
    recovery_count = write_jsonl(output_dir / "workflow_id_recovery.jsonl", recovery_rows)
    resolution_count = write_jsonl(output_dir / "workflow_id_resolution.jsonl", resolution_rows)
    analysis_workflow_count = write_jsonl(
        output_dir / "rq2_analysis_workflows.jsonl", provisional_analysis_rows
    )
    final_analysis_workflow_count = write_jsonl(
        output_dir / "rq2_final_analysis_workflows.jsonl", final_analysis_rows
    )
    retry_count = write_jsonl(output_dir / "rq2_retry_targets.jsonl", retry_rows)
    provisional_analysis_run_stats = write_analysis_runs(
        runs_path,
        output_dir / "rq2_analysis_runs.jsonl",
        states_by_key,
        repositories,
        aliases,
        cutoff,
        window_days,
        provenance,
        "provisional_analysis_included",
        "provisional_rq2_analysis_runs",
    )
    final_analysis_run_stats = write_analysis_runs(
        runs_path,
        output_dir / "rq2_final_analysis_runs.jsonl",
        states_by_key,
        repositories,
        aliases,
        cutoff,
        window_days,
        provenance,
        "final_analysis_included",
        "final_rq2_analysis_runs",
    )

    status_counts = Counter(state.analysis_status for state in states)
    query_counts = Counter(state.query_coverage_status for state in states)
    recovery_counts = Counter(state.recovery_status for state in states)
    run_evidence_counts = Counter(state.run_evidence_status for state in states)
    boundary_counts = Counter(
        "verified" if state.window_start is not None else "unresolved" for state in states
    )
    retry_repositories = sorted({state.key[0] for state in states if state.retry_required})
    qualifying_workflow_count = sum(1 for state in states if state.aggregate.qualifying_run_count > 0)
    qualifying_run_count = sum(
        state.aggregate.qualifying_run_count
        for state in states
        if state.provisional_analysis_included
    )
    qualifying_run_count_before_analysis_gates = sum(
        state.aggregate.qualifying_run_count for state in states
    )
    provisional_workflow_count = sum(
        1 for state in states if state.provisional_analysis_included
    )
    final_workflow_count = sum(1 for state in states if state.final_analysis_included)
    coverage_confirmed_workflow_count = sum(
        1 for state in states if state.coverage_confirmed
    )
    coverage_confirmed_run_count = sum(
        state.aggregate.qualifying_run_count
        for state in states
        if state.coverage_confirmed
    )
    coverage_confirmed_analysis_scope_workflow_count = sum(
        1
        for state in states
        if state.coverage_confirmed
        and (
            bool(state.query.get("query_analysis_ready_before_overlay"))
            or state.query.get("query_latest_acquisition_status")
            in {"complete", "no_qualifying_runs"}
        )
    )
    terminal_exclusion_workflow_count = status_counts.get("terminal_exclusion", 0)
    access_denied_workflow_count = status_counts.get("access_denied", 0)
    provisional_complete_workflow_count = sum(
        1
        for state in states
        if state.provisional_analysis_included and state.coverage_confirmed
    )
    provisional_complete_run_count = sum(
        state.aggregate.qualifying_run_count
        for state in states
        if state.provisional_analysis_included and state.coverage_confirmed
    )
    provisional_missing_workflow_count = sum(
        1
        for state in states
        if state.provisional_analysis_included and state.query_coverage_status == "missing"
    )
    provisional_api_workflow_count = sum(
        1
        for state in states
        if state.provisional_analysis_included
        and state.query_coverage_status == "api_unresolved"
    )
    valid_boundary_missing_id_unconfirmed = sum(
        1
        for state in states
        if state.detector_valid
        and state.boundary_valid
        and state.recovery_status == "missing_no_run_evidence"
        and not state.coverage_confirmed
    )
    retry_overlap_states = [
        state
        for state in states
        if state.provisional_analysis_included and state.retry_required
    ]
    retry_overlap_query_counts = Counter(
        state.query_coverage_status for state in retry_overlap_states
    )
    retry_overlap_id_counts = Counter(state.recovery_status for state in retry_overlap_states)
    retry_overlap_run_counts = Counter(state.run_evidence_status for state in retry_overlap_states)
    cross_counts = build_cross_counts(audit_rows)
    query_id_qualifying_cross_counts = build_query_id_qualifying_cross_counts(audit_rows)

    phase3_1 = coverage_evidence_path is not None and (
        "phase3_1" in str(coverage_evidence_path).replace("\\", "/")
        or "phase3.1" in str(coverage_evidence_path)
    )
    phase1_2_1 = coverage_evidence_path is not None and (
        "phase1_2_1" in str(coverage_evidence_path).replace("\\", "/")
        or "phase1.2.1" in str(coverage_evidence_path)
    )
    summary: Dict[str, Any] = {
        "schema_version": (
            "rq2-canonical-coverage-audit-v2-phase3.1"
            if phase3_1
            else
            "rq2-canonical-coverage-audit-v2-phase1.2.1"
            if phase1_2_1
            else "rq2-canonical-coverage-audit-v2-phase1.2"
            if coverage_evidence_path is not None
            else "rq2-canonical-coverage-audit-v2-phase1.1"
        ),
        "audit_stage": (
            "phase3.1_final_coverage_and_analysis_queue"
            if phase3_1
            else
            "phase1.2.1_local_query_evidence_conflict_corrected"
            if phase1_2_1
            else "phase1.2_local_query_evidence_integrated"
            if coverage_evidence_path is not None
            else "phase1.1_local_read_only"
        ),
        "external_fetch_performed": False,
        "integrated_coverage_evidence": (
            source_label(coverage_evidence_path)
            if coverage_evidence_path is not None
            else None
        ),
        "canonical_workflows": len(states),
        "rq1_valid_workflows": sum(
            1 for state in states if state.rq1.get("eligible") and state.rq1.get("detector_state_key")
        ),
        "workflow_id_present": recovery_counts.get("present", 0),
        "workflow_id_recovered": recovery_counts.get("recovered_unique_run_id", 0),
        "workflow_id_ambiguous": recovery_counts.get("ambiguous_multiple_run_ids", 0),
        "workflow_id_unresolved": recovery_counts.get("missing_no_run_evidence", 0),
        "verified_boundaries": boundary_counts.get("verified", 0),
        "has_qualifying_runs": run_evidence_counts.get("has_qualifying_runs", 0),
        "qualifying_workflows_total": qualifying_workflow_count,
        "qualifying_runs": qualifying_run_count,
        "qualifying_runs_before_analysis_gates": qualifying_run_count_before_analysis_gates,
        "confirmed_no_qualifying_run": status_counts.get("confirmed_no_qualifying_run", 0),
        "only_out_of_scope_runs": status_counts.get("only_out_of_scope_runs", 0),
        "run_present_but_not_linked": status_counts.get("run_present_but_not_linked", 0),
        "query_incomplete": status_counts.get("query_incomplete", 0),
        "api_unresolved": status_counts.get("api_unresolved", 0),
        "query_status_missing": status_counts.get("query_status_missing", 0),
        "coverage_confirmed_workflows": coverage_confirmed_workflow_count,
        "coverage_confirmed_runs": coverage_confirmed_run_count,
        "coverage_confirmed_analysis_scope_workflows": (
            coverage_confirmed_analysis_scope_workflow_count
        ),
        "terminal_exclusion_workflows": terminal_exclusion_workflow_count,
        "access_denied_workflows": access_denied_workflow_count,
        "retry_target_workflows": retry_count,
        "retry_target_repositories": len(retry_repositories),
        # Compatibility aliases retain the old names but explicitly refer to
        # the provisional queue.  The final queue is reported separately.
        "analysis_workflows": analysis_workflow_count,
        "analysis_runs": provisional_analysis_run_stats.get("analysis_runs", 0),
        "provisional_analysis_workflows": provisional_workflow_count,
        "provisional_analysis_runs": provisional_analysis_run_stats.get("analysis_runs", 0),
        "coverage_confirmed_final_workflows": final_workflow_count,
        "coverage_confirmed_final_runs": final_analysis_run_stats.get("analysis_runs", 0),
        "provisional_query_complete_workflows": provisional_complete_workflow_count,
        "provisional_query_complete_runs": provisional_complete_run_count,
        "provisional_query_missing_workflows": provisional_missing_workflow_count,
        "provisional_api_unresolved_workflows": provisional_api_workflow_count,
        "valid_boundary_missing_id_unconfirmed_workflows": valid_boundary_missing_id_unconfirmed,
        "retry_overlap_workflows": len(retry_overlap_states),
        "retry_overlap_recovered_id_workflows": retry_overlap_id_counts.get(
            "recovered_unique_run_id", 0
        ),
        "retry_overlap_counts": {
            "by_query_coverage_status": dict(sorted(retry_overlap_query_counts.items())),
            "by_workflow_id_status": dict(sorted(retry_overlap_id_counts.items())),
            "by_run_evidence_status": dict(sorted(retry_overlap_run_counts.items())),
            "reason": "query_coverage_unconfirmed_only",
        },
        "analysis_status_counts": dict(sorted(status_counts.items())),
        "query_status_counts": dict(sorted(query_counts.items())),
        "query_coverage_status_counts": dict(sorted(query_counts.items())),
        "workflow_id_status_counts": dict(sorted(recovery_counts.items())),
        "run_evidence_status_counts": dict(sorted(run_evidence_counts.items())),
        "boundary_axis_counts": dict(sorted(boundary_counts.items())),
        "run_scan_stats": run_scan_stats,
        "analysis_run_stats": provisional_analysis_run_stats,
        "provisional_analysis_run_stats": provisional_analysis_run_stats,
        "final_analysis_run_stats": final_analysis_run_stats,
        "output_record_counts": {
            "rq2_coverage_audit": audit_count,
            "workflow_id_recovery": recovery_count,
            "workflow_id_resolution": resolution_count,
            "rq2_analysis_workflows": analysis_workflow_count,
            "rq2_final_analysis_workflows": final_analysis_workflow_count,
            "rq2_analysis_runs": provisional_analysis_run_stats.get("analysis_runs", 0),
            "rq2_final_analysis_runs": final_analysis_run_stats.get("analysis_runs", 0),
            "rq2_retry_targets": retry_count,
        },
        "expected_counts_from_manifest": {
            "canonical_workflows": ((manifest.get("counts") or {}).get("workflow_snapshots")),
            "rq1_workflow_cohort": ((manifest.get("counts") or {}).get("rq1_workflow_cohort")),
            "canonical_run_records": ((manifest.get("counts") or {}).get("runs_written")),
        },
        "cross_counts": cross_counts,
        "query_id_qualifying_cross_counts": query_id_qualifying_cross_counts,
        "input_files": [source_label(path) for path in required_inputs],
        **provenance,
    }
    (output_dir / "rq2_coverage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit canonical RQ2 run coverage without GitHub API calls")
    parser.add_argument("--manifest", type=Path, default=CANONICAL_DIR / "dataset_manifest.json")
    parser.add_argument("--repositories", type=Path, default=CANONICAL_DIR / "repositories.jsonl")
    parser.add_argument("--snapshots", type=Path, default=CANONICAL_DIR / "workflow_snapshots.jsonl")
    parser.add_argument("--rq1", type=Path, default=CANONICAL_DIR / "rq1_cohort.jsonl")
    parser.add_argument("--boundaries", type=Path, default=CANONICAL_DIR / "workflow_boundaries.jsonl")
    parser.add_argument("--runs", type=Path, default=CANONICAL_DIR / "workflow_runs.jsonl")
    parser.add_argument("--rq2", type=Path, default=CANONICAL_DIR / "rq2_cohort.jsonl")
    parser.add_argument(
        "--status-file",
        type=Path,
        action="append",
        default=None,
        help="repository_fetch_status.jsonl; may be repeated for multiple query windows",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--coverage-evidence",
        type=Path,
        default=None,
        help="Phase 1.2 local query-coverage evidence JSONL; no network access is performed",
    )
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    args = parser.parse_args()
    status_paths = args.status_file if args.status_file is not None else [DEFAULT_STATUS_FILE]
    audit(
        args.manifest,
        args.repositories,
        args.snapshots,
        args.rq1,
        args.boundaries,
        args.runs,
        args.rq2,
        status_paths,
        args.output_dir,
        args.cutoff,
        args.window_days,
        args.coverage_evidence,
    )


if __name__ == "__main__":
    main()
