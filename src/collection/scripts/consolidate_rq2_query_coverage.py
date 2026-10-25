"""Consolidate local RQ2 query-coverage evidence without network access.

This module deliberately treats run records and query-completion evidence as
different things.  A local run can establish that a workflow was observed,
but it cannot establish that the corresponding query reached the end of its
pagination.  Only explicit completion/termination evidence with a verifiable
scope is allowed to recover ``coverage_confirmed``.
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

# Keep both ``python scripts/file.py`` and ``python -m scripts.file`` usable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_rq2_canonical_coverage import (
    DEFAULT_CUTOFF,
    DEFAULT_OUTPUT_DIR as PHASE1_OUTPUT_DIR,
    normalize_id,
    normalize_path,
    parse_timestamp,
    source_label,
)


DEFAULT_OUTPUT_DIR = ROOT / "output_canonical_rq2_query_coverage_phase1_2_1_20260919"
DEFAULT_SOURCE_DIRS = [
    ROOT / "output_full_logical_20260831" / "rq2_default_branch_run_supplement_20260609",
    ROOT
    / "output_full_logical_20260831"
    / "rq2_default_branch_run_supplement_20260609_exposed",
    ROOT
    / "output_full_logical_20260831"
    / "rq2_default_branch_run_supplement_20260609_v2",
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_run_supplement_window_20260609",
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_run_supplement_recheck_normalized",
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_targeted_run_supplement_missing_60d_90d_20260917",
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_run_status_unmatched_90d_20260918",
]
DEFAULT_AUDIT_PATH = PHASE1_OUTPUT_DIR / "rq2_coverage_audit.jsonl"
DEFAULT_RUNS_PATH = ROOT / "data_26_6_9_canonical_20260609" / "workflow_runs.jsonl"
RQ2_WINDOW_DAYS = 30
PAGE_SIZE = 100
EVENT_STATUS_OK = {"ok", "no_runs"}
API_FAILURE_STATUS = {"error", "forbidden", "not_found", "unresolved"}
WORKFLOW_KEY = Tuple[str, str]


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            yield value


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_local_path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace("\\", "/")
    candidate = Path(raw)
    if candidate.exists():
        return candidate.resolve()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if candidate.exists():
        return candidate.resolve()
    return None


def parse_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def timestamp_text(value: Any) -> Optional[str]:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def iso_sort_key(value: Any) -> str:
    return timestamp_text(value) or ""


@dataclass
class TargetScope:
    repository_id: str
    target_file: Path
    filters: Tuple[Dict[str, Any], ...] = ()

    @property
    def scope_type(self) -> str:
        if not self.filters:
            return "repository_wide_unfiltered"
        if all(normalize_id(item.get("workflow_id")) for item in self.filters):
            return "workflow_specific_batch"
        return "repository_wide_path_filtered"

    @property
    def paths(self) -> Set[str]:
        return {
            normalize_path(item.get("workflow_path"))
            for item in self.filters
            if normalize_path(item.get("workflow_path"))
        }

    @property
    def ids_by_path(self) -> Dict[str, str]:
        return {
            normalize_path(item.get("workflow_path")): normalize_id(item.get("workflow_id"))
            for item in self.filters
            if normalize_path(item.get("workflow_path"))
        }


@dataclass
class SourceDescriptor:
    source_dir: Path
    status_path: Optional[Path]
    target_path: Optional[Path]
    metadata_paths: Tuple[Path, ...]
    metadata: Dict[str, Any]
    source_hashes: Dict[str, str]
    scopes: Dict[str, TargetScope]
    query_start: Optional[str]
    query_cutoff: Optional[str]
    query_window_days: Optional[int]
    branch_scope: Optional[str]
    workflow_query_scope: Optional[str]
    default_branches: Dict[str, str]

    @property
    def label(self) -> str:
        return source_label(self.source_dir)

    @property
    def source_files(self) -> List[Dict[str, str]]:
        paths: List[Tuple[str, Path]] = []
        if self.status_path is not None:
            paths.append(("status", self.status_path))
        if self.target_path is not None:
            paths.append(("target", self.target_path))
        paths.extend(("metadata", path) for path in self.metadata_paths)
        result = []
        for role, path in sorted(paths, key=lambda item: (source_label(item[1]), item[0])):
            result.append(
                {
                    "role": role,
                    "path": source_label(path),
                    "sha256": self.source_hashes[source_label(path)],
                }
            )
        return result


def choose_target_path(source_dir: Path, metadata: Mapping[str, Any]) -> Optional[Path]:
    linked = resolve_local_path(metadata.get("targets"))
    if linked is not None:
        return linked
    local = source_dir / "target_repositories.jsonl"
    return local.resolve() if local.exists() else None


def build_source_descriptor(source_dir: Path) -> SourceDescriptor:
    source_dir = source_dir.resolve()
    metadata_paths = tuple(
        path.resolve()
        for path in sorted(source_dir.glob("*.json"))
        if path.name
        in {
            "target_metadata.json",
            "supplemental_run_metadata.json",
            "workflow_run_classification_metadata.json",
        }
    )
    metadata: Dict[str, Any] = {}
    for path in metadata_paths:
        metadata.update(load_json(path))
    status_path = source_dir / "repository_fetch_status.jsonl"
    if not status_path.exists():
        status_path = None
    target_path = choose_target_path(source_dir, metadata)
    source_hashes: Dict[str, str] = {}
    for path in [status_path, target_path, *metadata_paths]:
        if path is not None and path.exists():
            source_hashes[source_label(path)] = sha256_file(path)

    scopes: Dict[str, TargetScope] = {}
    if target_path is not None:
        for record in load_jsonl(target_path):
            repository_id = normalize_id(record.get("repository_id"))
            if not repository_id:
                continue
            filters = tuple(
                dict(item)
                for item in (record.get("workflow_filters") or [])
                if isinstance(item, dict)
            )
            scopes[repository_id] = TargetScope(repository_id, target_path, filters)

    default_branches: Dict[str, str] = {}
    if target_path is not None:
        for record in load_jsonl(target_path):
            repository_id = normalize_id(record.get("repository_id"))
            default_branch = str(record.get("default_branch") or "").strip()
            if repository_id and default_branch:
                default_branches[repository_id] = default_branch

    query_start = timestamp_text(
        metadata.get("window_start") or metadata.get("query_start")
    )
    query_cutoff = timestamp_text(
        metadata.get("as_of") or metadata.get("cutoff") or metadata.get("study_cutoff")
    )
    query_window_days = parse_int(metadata.get("window_days"))
    if query_window_days is None and query_start and query_cutoff:
        start = parse_timestamp(query_start)
        cutoff = parse_timestamp(query_cutoff)
        if start is not None and cutoff is not None:
            delta_days = (cutoff - start).total_seconds() / 86400
            if delta_days >= 29:
                query_window_days = round(delta_days)
    branch_scope = str(metadata.get("branch_scope") or "").strip() or None
    workflow_query_scope = str(metadata.get("workflow_query_scope") or "").strip() or None
    return SourceDescriptor(
        source_dir=source_dir,
        status_path=status_path,
        target_path=target_path,
        metadata_paths=metadata_paths,
        metadata=metadata,
        source_hashes=source_hashes,
        scopes=scopes,
        query_start=query_start,
        query_cutoff=query_cutoff,
        query_window_days=query_window_days,
        branch_scope=branch_scope,
        workflow_query_scope=workflow_query_scope,
        default_branches=default_branches,
    )


def read_audit(path: Path) -> Tuple[List[Dict[str, Any]], Dict[WORKFLOW_KEY, Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    by_key: Dict[WORKFLOW_KEY, Dict[str, Any]] = {}
    for row in load_jsonl(path):
        key = normalize_id(row.get("repository_id")), normalize_path(row.get("workflow_path"))
        if not key[0] or not key[1]:
            continue
        if key in by_key:
            raise ValueError(f"Duplicate workflow key in audit: {key}")
        rows.append(row)
        by_key[key] = row
    return rows, by_key


def source_file_for_status(descriptor: SourceDescriptor) -> Optional[str]:
    return source_label(descriptor.status_path) if descriptor.status_path else None


def query_window_sufficient(descriptor: SourceDescriptor) -> Tuple[bool, str]:
    if descriptor.query_window_days is not None:
        if descriptor.query_window_days >= RQ2_WINDOW_DAYS:
            return True, "metadata_window_covers_rq2_30_days"
        return False, "query_window_shorter_than_rq2_30_days"
    if descriptor.query_start and descriptor.query_cutoff:
        start = parse_timestamp(descriptor.query_start)
        cutoff = parse_timestamp(descriptor.query_cutoff)
        if start is not None and cutoff is not None:
            if (cutoff - start).total_seconds() >= RQ2_WINDOW_DAYS * 86400:
                return True, "metadata_start_cutoff_covers_rq2_30_days"
    # A query with no lower bound and an explicit cutoff covers every earlier
    # date, but only when the metadata says what cutoff was used.
    if descriptor.query_cutoff and str(descriptor.metadata.get("branch_scope") or "").strip():
        return True, "open_ended_before_metadata_cutoff"
    return False, "query_window_or_cutoff_not_verifiable"


def is_mixed_query_scope(descriptor: SourceDescriptor) -> bool:
    """Return whether metadata describes workflow-specific plus fallback calls."""

    text = str(descriptor.workflow_query_scope or "").strip().lower()
    workflow_specific = "workflow-specific" in text or "workflow specific" in text
    fallback = any(token in text for token in ("fallback", "filtered", "otherwise"))
    return workflow_specific and fallback


def repository_wide_endpoint_proven(descriptor: SourceDescriptor) -> bool:
    """Return whether metadata explicitly identifies a repository-wide call.

    An empty target filter is not enough: older batches can combine several
    endpoint strategies while exposing only a repository-level target file.
    Directory names and target selection rules are intentionally not used as
    endpoint evidence.
    """

    text = str(descriptor.workflow_query_scope or "").strip().lower()
    return (
        not is_mixed_query_scope(descriptor)
        and ("repository-wide" in text or "repository wide" in text)
        and ("endpoint" in text or "query" in text)
    )


def normal_termination_proven(
    explicit_complete: Any,
    pages: Any,
    records: Any,
    termination_reason: Any,
) -> bool:
    if explicit_complete is True:
        return True
    reason = str(termination_reason or "").strip().lower()
    if reason in {
        "empty_page",
        "short_page",
        "normal_termination",
        "pagination_complete",
        "completed",
        "complete",
    }:
        return True
    # A legacy first-page empty result is sufficient; a multi-page zero-record
    # result is not, because records may have been counted after path filtering.
    return parse_int(pages) == 1 and parse_int(records) == 0


def status_fields(
    status_record: Mapping[str, Any],
    workflow_record: Optional[Mapping[str, Any]],
    descriptor: SourceDescriptor,
    endpoint_type: str,
    exact_scope: bool,
    scope_reason: str,
    coverage_scope_allowed: Optional[bool] = None,
) -> Dict[str, Any]:
    item = dict(workflow_record or {})
    status = str(item.get("status") or status_record.get("status") or "").strip().lower()
    explicit_complete = item.get(
        "pagination_complete",
        item.get("complete", status_record.get("pagination_complete", status_record.get("complete"))),
    )
    pages = item.get("pages", status_record.get("pages"))
    records = item.get("records", status_record.get("records"))
    termination_reason = item.get(
        "termination_reason", status_record.get("termination_reason")
    )
    last_page_size = item.get("last_page_size", status_record.get("last_page_size"))
    fetched_at = item.get("fetched_at", status_record.get("fetched_at"))
    error = item.get("error", status_record.get("error"))
    default_branch = str(
        item.get("default_branch")
        or status_record.get("default_branch")
        or descriptor.default_branches.get(normalize_id(status_record.get("repository_id")))
        or ""
    ).strip() or None
    window_ok, window_reason = query_window_sufficient(descriptor)
    if coverage_scope_allowed is None:
        coverage_scope_allowed = exact_scope

    reasons: List[str] = []
    coverage_confirmed = False
    if status in API_FAILURE_STATUS:
        reasons.append("api_error_or_workflow_not_found")
    elif not coverage_scope_allowed:
        reasons.append(scope_reason)
    elif not window_ok:
        reasons.append(window_reason)
    elif explicit_complete is True and status in EVENT_STATUS_OK:
        coverage_confirmed = True
        termination_reason = termination_reason or "explicit_complete_without_termination_detail"
    elif explicit_complete is False:
        if descriptor.query_window_days is not None and descriptor.query_window_days > RQ2_WINDOW_DAYS:
            reasons.append("longer_window_incomplete_does_not_prove_30_day_coverage")
        else:
            reasons.append("pagination_incomplete")
    elif status == "no_runs" and records in (0, "0"):
        # Older collectors did not persist pagination_complete.  An explicit
        # no_runs record is usable only with target scope, cutoff metadata and
        # a provable normal termination.  In particular, pages>1 with zero
        # records is not enough because records may be path-filtered.
        if normal_termination_proven(
            explicit_complete, pages, records, termination_reason
        ):
            coverage_confirmed = True
            termination_reason = termination_reason or "legacy_no_runs_empty_result"
            reasons.append("legacy_no_runs_completion_rule")
        else:
            reasons.append("no_runs_pagination_not_proven")
    elif status == "ok":
        reasons.append("status_ok_without_explicit_pagination_completion")
    else:
        reasons.append("status_does_not_prove_query_completion")

    normalized_status = "complete" if coverage_confirmed else (
        "api_unresolved" if status in API_FAILURE_STATUS else "missing"
    )
    return {
        "status": status or None,
        "pages": parse_int(pages),
        "records": parse_int(records),
        "pagination_complete": explicit_complete if isinstance(explicit_complete, bool) else None,
        "termination_reason": termination_reason,
        "last_page_size": parse_int(last_page_size),
        "fetched_at": timestamp_text(fetched_at),
        "error": error,
        "query_start": descriptor.query_start,
        "query_cutoff": descriptor.query_cutoff,
        "query_window_days": descriptor.query_window_days,
        "query_default_branch": default_branch,
        "query_coverage_status": normalized_status,
        "coverage_confirmed": coverage_confirmed,
        "coverage_confirmation_reason": (
            "explicit_complete_and_scope_window_sufficient"
            if coverage_confirmed and not reasons
            else (
                "legacy_no_runs_with_explicit_scope_and_cutoff"
                if coverage_confirmed
                else None
            )
        ),
        "coverage_unconfirmed_reason": None if coverage_confirmed else ";".join(reasons),
        "query_endpoint_type": endpoint_type,
        "query_restricted_to_exact_workflow_id_or_path": exact_scope,
        "coverage_scope_allowed": bool(coverage_scope_allowed),
        "scope_reason": scope_reason,
    }


def candidate_base(
    descriptor: SourceDescriptor,
    status_record: Mapping[str, Any],
    workflow_record: Optional[Mapping[str, Any]],
    endpoint_type: str,
    exact_scope: bool,
    scope_reason: str,
    workflow_path: Optional[str],
    workflow_id: Optional[str],
    evidence_applicable: Optional[bool] = None,
    coverage_scope_allowed: Optional[bool] = None,
) -> Dict[str, Any]:
    fields = status_fields(
        status_record,
        workflow_record,
        descriptor,
        endpoint_type,
        exact_scope,
        scope_reason,
        coverage_scope_allowed,
    )
    if evidence_applicable is None:
        evidence_applicable = exact_scope
    result = {
        "candidate_source_type": "historical_query_status",
        "evidence_source": descriptor.label,
        "evidence_source_file": source_file_for_status(descriptor),
        "evidence_source_file_sha256": (
            descriptor.source_hashes.get(source_file_for_status(descriptor) or "")
        ),
        "workflow_id": workflow_id,
        "workflow_path": workflow_path,
        "evidence_applicable": evidence_applicable,
        "evidence_decision": "unselected",
        "rejection_reasons": [],
        **fields,
    }
    if not (coverage_scope_allowed if coverage_scope_allowed is not None else exact_scope):
        result["rejection_reasons"].append(scope_reason)
    return result


def add_candidate(
    candidates: Dict[WORKFLOW_KEY, List[Dict[str, Any]]],
    key: WORKFLOW_KEY,
    candidate: Dict[str, Any],
) -> None:
    candidate["repository_id"] = key[0]
    candidate["workflow_path"] = key[1]
    candidates[key].append(candidate)


def workflow_status_scope(
    descriptor: SourceDescriptor,
    scope: Optional[TargetScope],
    workflow_record: Mapping[str, Any],
) -> Tuple[str, bool, str, Optional[bool]]:
    """Resolve the scope of one explicit workflow-level status row."""

    path = normalize_path(workflow_record.get("workflow_path"))
    workflow_id = normalize_id(workflow_record.get("workflow_id"))
    if scope is None:
        return (
            "workflow_status_scope_unknown",
            False,
            "workflow_status_scope_metadata_missing",
            False,
        )

    mixed = is_mixed_query_scope(descriptor)
    target_paths = scope.paths
    target_id = scope.ids_by_path.get(path, "")
    if target_paths and path not in target_paths:
        return (
            "scope_unclear",
            False,
            "workflow_status_path_not_in_target_scope",
            False,
        )
    if target_id and workflow_id and target_id != workflow_id:
        return (
            "scope_unclear",
            False,
            "workflow_status_id_does_not_match_target_scope",
            False,
        )

    if workflow_id and (target_id or mixed or scope.scope_type == "workflow_specific_batch"):
        return (
            "workflow_specific",
            True,
            "workflow_status_row_matches_workflow_id_and_path",
            True,
        )
    if path and (
        scope.scope_type == "repository_wide_path_filtered"
        or mixed
        or not scope.filters
    ):
        return (
            "repository_wide_path_filtered",
            True,
            "workflow_status_row_is_path_specific",
            True,
        )
    return (
        "scope_unclear",
        False,
        "workflow_status_endpoint_scope_not_reconstructable",
        False,
    )


def build_candidates(
    audit_by_key: Mapping[WORKFLOW_KEY, Mapping[str, Any]],
    descriptor: SourceDescriptor,
    candidates: Dict[WORKFLOW_KEY, List[Dict[str, Any]]],
    stats: Counter[str],
    eligible_keys: Optional[Set[WORKFLOW_KEY]] = None,
) -> None:
    if descriptor.status_path is None:
        return
    audit_by_repo: Dict[str, List[WORKFLOW_KEY]] = defaultdict(list)
    for key in audit_by_key:
        audit_by_repo[key[0]].append(key)
    for status_record in load_jsonl(descriptor.status_path):
        repository_id = normalize_id(status_record.get("repository_id"))
        if not repository_id:
            continue
        scope = descriptor.scopes.get(repository_id)
        workflow_statuses = status_record.get("workflow_statuses") or []
        if workflow_statuses:
            for workflow_record in workflow_statuses:
                if not isinstance(workflow_record, dict):
                    continue
                path = normalize_path(workflow_record.get("workflow_path"))
                if not path:
                    continue
                key = (repository_id, path)
                if key not in audit_by_key or (
                    eligible_keys is not None and key not in eligible_keys
                ):
                    stats["workflow_status_rows_not_in_audit"] += 1
                    continue
                workflow_id = normalize_id(workflow_record.get("workflow_id")) or None
                (
                    endpoint_type,
                    exact_scope,
                    scope_reason,
                    coverage_scope_allowed,
                ) = workflow_status_scope(descriptor, scope, workflow_record)
                candidate = candidate_base(
                    descriptor,
                    status_record,
                    workflow_record,
                    endpoint_type,
                    exact_scope,
                    scope_reason,
                    path,
                    workflow_id,
                    coverage_scope_allowed=coverage_scope_allowed,
                )
                add_candidate(candidates, key, candidate)
                stats["workflow_status_candidates"] += 1
            continue

        if scope is None:
            stats["repository_status_scope_unknown"] += 1
            continue
        if (
            scope.scope_type == "repository_wide_unfiltered"
            and repository_wide_endpoint_proven(descriptor)
        ):
            # This is the only repository-level status that may be applied to
            # every workflow in the repository.
            for key in audit_by_repo.get(repository_id, []):
                if eligible_keys is not None and key not in eligible_keys:
                    continue
                candidate = candidate_base(
                    descriptor,
                    status_record,
                    None,
                    "repository_wide_unfiltered",
                    False,
                    "target_metadata_proves_unfiltered_repository_query",
                    key[1],
                    None,
                    evidence_applicable=True,
                    coverage_scope_allowed=True,
                )
                add_candidate(candidates, key, candidate)
                stats["repository_wide_unfiltered_candidates"] += 1
            continue

        # Preserve the rejected aggregate evidence at workflow level, but do
        # not let it prove coverage for any individual path.
        target_paths = scope.paths
        affected = [
            key
            for key in audit_by_repo.get(repository_id, [])
            if not target_paths or key[1] in target_paths
        ]
        if eligible_keys is not None:
            affected = [key for key in affected if key in eligible_keys]
        if scope.scope_type == "workflow_specific_batch" or is_mixed_query_scope(descriptor):
            endpoint_type = "workflow_specific_batch_aggregate"
            reason = (
                "mixed_workflow_specific_fallback_batch_has_no_per_workflow_status_row"
                if is_mixed_query_scope(descriptor)
                else "workflow_specific_batch_has_no_per_workflow_status_row"
            )
        elif scope.scope_type == "repository_wide_unfiltered":
            endpoint_type = "scope_unclear"
            reason = "repository_wide_endpoint_scope_not_proven_by_metadata"
        else:
            endpoint_type = "repository_wide_path_filtered_aggregate"
            reason = "repository_path_filtered_status_has_no_per_workflow_status_row"
        for key in affected:
            candidate = candidate_base(
                descriptor,
                status_record,
                None,
                endpoint_type,
                False,
                reason,
                key[1],
                None,
            )
            add_candidate(candidates, key, candidate)
            stats["scope_unclear_candidates"] += 1


def audit_input_source_hash(row: Mapping[str, Any], source: Any) -> Optional[str]:
    source_text = str(source or "").replace("\\", "/")
    if not source_text:
        return None
    hashes = row.get("input_file_sha256") or {}
    if not isinstance(hashes, Mapping):
        return None
    for path, digest in hashes.items():
        if str(path).replace("\\", "/") == source_text:
            return str(digest)
    return None


def derive_query_start(query_cutoff: Any, query_window_days: Any) -> Optional[str]:
    cutoff = parse_timestamp(query_cutoff)
    days = parse_int(query_window_days)
    if cutoff is None or days is None or days < 0:
        return None
    return (cutoff - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def current_audit_candidate(
    row: Mapping[str, Any],
    audit_path: Path,
    audit_hash: str,
) -> Dict[str, Any]:
    query_status = str(row.get("query_coverage_status") or row.get("query_status") or "missing")
    query_source = row.get("query_status_source")
    query_source_hash = audit_input_source_hash(row, query_source)
    query_cutoff = row.get("cutoff")
    query_window_days = row.get("query_window_days")
    query_start = derive_query_start(query_cutoff, query_window_days)
    source_file = str(query_source or source_label(audit_path)).replace("\\", "/")
    source_file_hash = query_source_hash or audit_hash
    source_files = [
        {"role": "current_audit", "path": source_label(audit_path), "sha256": audit_hash}
    ]
    if query_source_hash:
        source_files.append(
            {"role": "derived_status", "path": source_file, "sha256": query_source_hash}
        )
    return {
        "candidate_source_type": "current_audit_baseline",
        "evidence_source": source_label(audit_path),
        "evidence_source_file": source_file,
        "evidence_source_file_sha256": source_file_hash,
        "evidence_source_files": source_files,
        "derived_from_audit_baseline": True,
        "derived_status_source_file": source_file if query_source_hash else None,
        "derived_status_source_file_sha256": query_source_hash,
        "workflow_id": normalize_id(row.get("workflow_id")) or None,
        "workflow_path": normalize_path(row.get("workflow_path")),
        "status": row.get("query_observed_status"),
        "error": row.get("query_error"),
        "pages": row.get("query_pages"),
        "records": row.get("query_records"),
        "pagination_complete": None,
        "termination_reason": row.get("query_termination_reason"),
        "last_page_size": row.get("query_last_page_size"),
        "fetched_at": row.get("query_fetched_at"),
        "query_start": query_start,
        "query_cutoff": query_cutoff,
        "query_window_days": query_window_days,
        "query_default_branch": str(row.get("default_branch") or "").strip() or None,
        "query_coverage_status": query_status,
        "coverage_confirmed": bool(row.get("coverage_confirmed")),
        "coverage_confirmation_reason": (
            "inherited_from_current_phase1_audit" if row.get("coverage_confirmed") else None
        ),
        "coverage_unconfirmed_reason": (
            None
            if row.get("coverage_confirmed")
            else "current_audit_scope_not_reconstructed"
        ),
        "query_endpoint_type": "current_audit_baseline",
        "query_restricted_to_exact_workflow_id_or_path": False,
        "coverage_scope_allowed": bool(row.get("coverage_confirmed")),
        "scope_reason": "current_audit_baseline_requires_source_scope_evidence",
        "evidence_applicable": bool(row.get("coverage_confirmed")),
        "evidence_decision": "unselected",
        "rejection_reasons": (
            [] if row.get("coverage_confirmed") else ["current_audit_scope_not_reconstructed"]
        ),
    }


def candidate_status_fingerprint(candidate: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Fingerprint query facts before endpoint resolution."""

    return (
        str(candidate.get("evidence_source_file_sha256") or ""),
        str(candidate.get("repository_id") or ""),
        normalize_path(candidate.get("workflow_path")),
        normalize_id(candidate.get("workflow_id")),
        timestamp_text(candidate.get("query_start")) or str(candidate.get("query_start") or ""),
        timestamp_text(candidate.get("query_cutoff")) or str(candidate.get("query_cutoff") or ""),
        str(candidate.get("status") or "").lower(),
        timestamp_text(candidate.get("fetched_at")) or str(candidate.get("fetched_at") or ""),
    )


def candidate_fingerprint(candidate: Mapping[str, Any]) -> Tuple[Any, ...]:
    endpoint = str(
        candidate.get("_dedupe_endpoint_type")
        or candidate.get("query_endpoint_type")
        or ""
    )
    return candidate_status_fingerprint(candidate) + (endpoint,)


def conflict_link_fingerprint(candidate: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Facts used to link an audit-derived row back to raw query evidence.

    The workflow ID is deliberately omitted here.  A path-specific raw query
    may not carry an ID even though the canonical audit later resolves one;
    that ID difference must not let the derived audit row mask a raw
    ``no_runs`` conflict.
    """

    return (
        str(candidate.get("evidence_source_file_sha256") or ""),
        str(candidate.get("repository_id") or ""),
        normalize_path(candidate.get("workflow_path")),
        timestamp_text(candidate.get("query_start"))
        or str(candidate.get("query_start") or ""),
        timestamp_text(candidate.get("query_cutoff"))
        or str(candidate.get("query_cutoff") or ""),
        str(candidate.get("status") or "").lower(),
        timestamp_text(candidate.get("fetched_at"))
        or str(candidate.get("fetched_at") or ""),
    )


def deduplicate_candidates(
    candidates_by_key: Dict[WORKFLOW_KEY, List[Dict[str, Any]]]
) -> Counter[str]:
    """Mark derived/current duplicate evidence without dropping provenance."""

    stats: Counter[str] = Counter()
    for key, candidates in candidates_by_key.items():
        for index, candidate in enumerate(candidates):
            candidate["_candidate_uid"] = f"{key[0]}|{key[1]}|{index}"
            candidate["repository_id"] = key[0]
            candidate["workflow_path"] = key[1]

        historical = [
            candidate
            for candidate in candidates
            if candidate.get("candidate_source_type") == "historical_query_status"
        ]
        by_status_fingerprint: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
        for candidate in historical:
            by_status_fingerprint[candidate_status_fingerprint(candidate)].append(candidate)

        # The current audit does not retain the original endpoint type.  If a
        # matching raw status record is present, recover that endpoint solely
        # for the duplicate fingerprint; the raw status row remains primary.
        for candidate in candidates:
            if candidate.get("candidate_source_type") != "current_audit_baseline":
                continue
            matches = by_status_fingerprint.get(candidate_status_fingerprint(candidate), [])
            endpoints = {
                str(item.get("query_endpoint_type") or "")
                for item in matches
                if item.get("query_endpoint_type")
                not in {"current_audit_baseline", "scope_unclear"}
            }
            if len(endpoints) == 1:
                candidate["_dedupe_endpoint_type"] = next(iter(endpoints))

        groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            groups[candidate_fingerprint(candidate)].append(candidate)
        for fingerprint, group in groups.items():
            if len(group) < 2:
                continue
            ordered = sorted(
                group,
                key=lambda item: (
                    1 if item.get("candidate_source_type") == "current_audit_baseline" else 0,
                    str(item.get("evidence_source_file") or ""),
                    str(item.get("evidence_source") or ""),
                    str(item.get("query_endpoint_type") or ""),
                ),
            )
            primary = ordered[0]
            for duplicate in ordered[1:]:
                duplicate["_duplicate_derived_evidence"] = True
                duplicate["_duplicate_of_candidate_uid"] = primary.get("_candidate_uid")
                duplicate["_duplicate_fingerprint"] = list(fingerprint)
                duplicate["_conflict_classification"] = "duplicate_derived_evidence"
                stats["duplicate_derived_evidence_records"] += 1
                stats[f"duplicate_derived_evidence_{duplicate.get('candidate_source_type')}"] += 1
        if any(item.get("_duplicate_derived_evidence") for item in candidates):
            stats["duplicate_derived_evidence_workflows"] += 1
    return stats


def attach_no_runs_run_evidence(
    candidates_by_key: Mapping[WORKFLOW_KEY, Sequence[Dict[str, Any]]],
    runs_path: Optional[Path],
) -> Counter[str]:
    """Stream canonical runs and evaluate exact query predicates for no_runs."""

    stats: Counter[str] = Counter()
    by_repo: Dict[str, Dict[Tuple[Any, ...], List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    by_path: Dict[WORKFLOW_KEY, Dict[Tuple[Any, ...], List[Dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    all_specs: List[Dict[str, Any]] = []
    for key, candidates in candidates_by_key.items():
        for candidate in candidates:
            if candidate.get("_duplicate_derived_evidence"):
                continue
            if not (
                candidate.get("coverage_confirmed")
                and candidate.get("evidence_applicable")
                and str(candidate.get("status") or "").lower() == "no_runs"
            ):
                continue
            endpoint = str(candidate.get("query_endpoint_type") or "")
            flags: Dict[str, Any] = {
                "matching_run_count": 0,
                "same_scope_in_window_count": 0,
                "same_identity_in_window_count": 0,
                "other_branch_count": 0,
                "different_workflow_id_count": 0,
                "outside_query_window_count": 0,
                "incomplete_same_identity_count": 0,
                "scope_unresolved": False,
            }
            spec = {"candidate": candidate, "key": key, "flags": flags}
            candidate["_run_evidence"] = flags
            start = parse_timestamp(candidate.get("query_start"))
            cutoff = parse_timestamp(candidate.get("query_cutoff"))
            branch = str(candidate.get("query_default_branch") or "").strip()
            workflow_id = normalize_id(candidate.get("workflow_id"))
            if endpoint not in {
                "workflow_specific",
                "repository_wide_path_filtered",
                "repository_wide_unfiltered",
            }:
                flags["scope_unresolved"] = True
            if not branch or cutoff is None:
                flags["scope_unresolved"] = True
            if endpoint == "workflow_specific" and not workflow_id:
                flags["scope_unresolved"] = True
            spec.update(
                {
                    "endpoint": endpoint,
                    "start": start,
                    "cutoff": cutoff,
                    "branch": branch,
                    "workflow_id": workflow_id,
                }
            )
            all_specs.append(spec)
            if flags["scope_unresolved"]:
                continue
            group_key = (
                endpoint,
                workflow_id,
                branch,
                start.isoformat() if start else "",
                cutoff.isoformat() if cutoff else "",
            )
            if endpoint == "repository_wide_unfiltered":
                by_repo[key[0]][group_key].append(spec)
            else:
                by_path[key][group_key].append(spec)

    stats["no_runs_specs"] = len(all_specs)
    if runs_path is None or not runs_path.exists():
        for spec in all_specs:
            if spec["flags"]["scope_unresolved"]:
                stats["scope_unresolved_no_runs_specs"] += 1
        return stats

    for run in load_jsonl(runs_path):
        repository_id = normalize_id(run.get("repository_id"))
        if not repository_id:
            continue
        workflow_path = normalize_path(run.get("workflow_path"))
        groups = list(by_repo.get(repository_id, {}).values()) + list(
            by_path.get((repository_id, workflow_path), {}).values()
        )
        if not groups:
            continue
        created = parse_timestamp(run.get("created_at"))
        if created is None:
            continue
        run_status = str(run.get("status") or "").strip().lower()
        run_branch = str(run.get("head_branch") or "").strip()
        run_workflow_id = normalize_id(run.get("workflow_id"))
        for group in groups:
            spec = group[0]
            flags = spec["flags"]
            endpoint = spec["endpoint"]
            path_match = endpoint == "repository_wide_unfiltered" or workflow_path == spec["key"][1]
            if not path_match:
                continue
            id_match = endpoint != "workflow_specific" or run_workflow_id == spec["workflow_id"]
            same_identity = path_match and id_match
            start = spec["start"]
            cutoff = spec["cutoff"]
            in_window = (start is None or created >= start) and (cutoff is None or created <= cutoff)
            completed = run_status == "completed"
            branch_match = run_branch == spec["branch"]
            if completed and in_window and path_match:
                flags["same_scope_in_window_count"] += 1
            if completed and in_window and same_identity:
                flags["same_identity_in_window_count"] += 1
                if branch_match:
                    flags["matching_run_count"] += 1
                else:
                    flags["other_branch_count"] += 1
            elif (
                completed
                and in_window
                and path_match
                and endpoint == "workflow_specific"
                and not id_match
                and branch_match
            ):
                flags["different_workflow_id_count"] += 1
            elif completed and not in_window and same_identity and branch_match:
                flags["outside_query_window_count"] += 1
            elif not completed and in_window and same_identity:
                flags["incomplete_same_identity_count"] += 1
            delta_key = None
            if completed and in_window and same_identity and branch_match:
                delta_key = "matching_run_count"
            elif completed and in_window and same_identity and not branch_match:
                delta_key = "other_branch_count"
            elif (
                completed
                and in_window
                and path_match
                and endpoint == "workflow_specific"
                and not id_match
                and branch_match
            ):
                delta_key = "different_workflow_id_count"
            elif completed and not in_window and same_identity and branch_match:
                delta_key = "outside_query_window_count"
            elif not completed and in_window and same_identity:
                delta_key = "incomplete_same_identity_count"
            same_scope_delta = int(completed and in_window and path_match)
            same_identity_delta = int(completed and in_window and same_identity)
            for grouped_spec in group[1:]:
                grouped_flags = grouped_spec["flags"]
                grouped_flags["same_scope_in_window_count"] += same_scope_delta
                grouped_flags["same_identity_in_window_count"] += same_identity_delta
                if delta_key:
                    grouped_flags[delta_key] += 1

    return stats


def classify_no_runs_candidate(candidate: Mapping[str, Any]) -> Optional[str]:
    flags = candidate.get("_run_evidence") or {}
    if flags.get("scope_unresolved"):
        return "scope_unresolved"
    if flags.get("matching_run_count", 0):
        return "true_no_runs_vs_matching_run"
    if flags.get("other_branch_count", 0):
        return "other_branch_not_conflict"
    if flags.get("different_workflow_id_count", 0):
        return "different_workflow_id_not_conflict"
    if flags.get("outside_query_window_count", 0):
        return "outside_query_window_not_conflict"
    return None


def classification_reason(classification: str) -> str:
    return {
        "true_no_runs_vs_matching_run": (
            "no_runs evidence conflicts with a completed matching run predicate"
        ),
        "other_branch_not_conflict": (
            "observed completed run is outside the recorded default-branch predicate"
        ),
        "different_workflow_id_not_conflict": (
            "observed completed run has a different workflow ID"
        ),
        "outside_query_window_not_conflict": (
            "observed completed run is outside the recorded query window"
        ),
        "scope_unresolved": "query endpoint or matching scope cannot be reconstructed",
        "duplicate_derived_evidence": (
            "current-audit evidence duplicates the raw status evidence"
        ),
    }.get(classification, classification)


def candidate_sort_key(candidate: Mapping[str, Any]) -> Tuple[int, int, int, str, str, str]:
    return (
        int(bool(candidate.get("coverage_confirmed"))),
        int(bool(candidate.get("evidence_applicable"))),
        parse_int(candidate.get("query_window_days")) or 0,
        iso_sort_key(candidate.get("fetched_at")),
        str(candidate.get("evidence_source") or ""),
        str(candidate.get("query_endpoint_type") or ""),
    )


def finalize_evidence(
    rows: Sequence[Mapping[str, Any]],
    candidates_by_key: Mapping[WORKFLOW_KEY, Sequence[Dict[str, Any]]],
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Counter[str],
]:
    final_rows: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for row in sorted(rows, key=lambda item: (str(item.get("repository_id")), normalize_path(item.get("workflow_path")))):
        key = normalize_id(row.get("repository_id")), normalize_path(row.get("workflow_path"))
        candidates = [dict(item) for item in candidates_by_key.get(key, [])]
        # The same evidence can be emitted through duplicate status rows; keep
        # all provenance but make selection deterministic.
        candidates.sort(key=lambda item: (
            str(item.get("evidence_source") or ""),
            str(item.get("workflow_path") or ""),
            str(item.get("status") or ""),
            str(item.get("fetched_at") or ""),
            str(item.get("query_endpoint_type") or ""),
        ))
        preclassified: Dict[int, Optional[str]] = {}
        true_conflict_links: Set[Tuple[Any, ...]] = set()
        for candidate in candidates:
            classification = candidate.get("_conflict_classification")
            if (
                classification is None
                and str(candidate.get("status") or "").lower() == "no_runs"
                and candidate.get("coverage_confirmed")
            ):
                classification = classify_no_runs_candidate(candidate)
            preclassified[id(candidate)] = classification
            if classification == "true_no_runs_vs_matching_run":
                true_conflict_links.add(conflict_link_fingerprint(candidate))
        for candidate in candidates:
            reasons = list(candidate.get("rejection_reasons") or [])
            classification = preclassified.get(id(candidate))
            # A current-audit row can carry the same raw status hash and query
            # facts while adding a canonical workflow ID that was not part of
            # the path-specific API request.  Treat it as a derived reference
            # to the raw true conflict, rather than allowing it to re-adopt
            # the contradicted complete state.
            if (
                candidate.get("candidate_source_type") == "current_audit_baseline"
                and classification in {None, "scope_unresolved"}
                and conflict_link_fingerprint(candidate) in true_conflict_links
            ):
                classification = "true_no_runs_vs_matching_run"
            if classification:
                candidate["_conflict_classification"] = classification
                conflicts.append(
                    {
                        "workflow_key": f"{key[0]}|{key[1]}",
                        "repository_id": key[0],
                        "workflow_path": key[1],
                        "conflict_type": classification,
                        "is_conflict": classification == "true_no_runs_vs_matching_run",
                        "reason": classification_reason(classification),
                        "evidence": dict(candidate),
                    }
                )
                stats[f"conflict_class_{classification}_records"] += 1
                if classification == "true_no_runs_vs_matching_run":
                    reasons.append("no_runs_conflicts_with_matching_run_predicate")
                    candidate["conflict_type"] = classification
                    candidate["evidence_applicable"] = False
                    candidate["coverage_confirmed"] = False
                    candidate["query_coverage_status"] = "missing"
                    candidate["coverage_unconfirmed_reason"] = ";".join(reasons)
                    stats["true_conflict_records"] += 1
                elif classification == "scope_unresolved":
                    reasons.append("no_runs_scope_or_query_predicate_unresolved")
                candidate["rejection_reasons"] = reasons
            elif candidate.get("_duplicate_derived_evidence"):
                conflicts.append(
                    {
                        "workflow_key": f"{key[0]}|{key[1]}",
                        "repository_id": key[0],
                        "workflow_path": key[1],
                        "conflict_type": "duplicate_derived_evidence",
                        "is_conflict": False,
                        "reason": "current-audit baseline duplicates raw status evidence",
                        "evidence": dict(candidate),
                    }
                )
                stats["conflict_class_duplicate_derived_evidence_records"] += 1
            candidate["rejection_reasons"] = reasons
        usable = [
            item
            for item in candidates
            if item.get("evidence_applicable")
            and not item.get("conflict_type")
            and not item.get("_duplicate_derived_evidence")
        ]
        confirmed = [item for item in usable if item.get("coverage_confirmed")]
        if confirmed:
            selected = sorted(confirmed, key=candidate_sort_key, reverse=True)[0]
        else:
            selected = sorted(candidates, key=candidate_sort_key, reverse=True)[0]
        final_confirmed = bool(selected.get("coverage_confirmed") and selected.get("evidence_applicable"))
        final_status = "complete" if final_confirmed else str(
            selected.get("query_coverage_status") or "missing"
        )
        if final_status == "complete" and not final_confirmed:
            final_status = "missing"
        source_type = str(selected.get("candidate_source_type") or "")
        local_recovery = bool(
            final_confirmed
            and not bool(row.get("coverage_confirmed"))
            and source_type != "current_audit_baseline"
        )
        if local_recovery:
            stats["local_recovery_workflows"] += 1
        if final_confirmed:
            stats["coverage_confirmed_workflows"] += 1
        if not final_confirmed:
            stats["still_unconfirmed_workflows"] += 1
        if selected.get("query_coverage_status") == "api_unresolved":
            stats["final_api_unresolved_workflows"] += 1
        if final_status == "missing":
            stats["final_query_missing_workflows"] += 1
        selected["evidence_decision"] = "adopted"
        for candidate in candidates:
            if candidate is not selected:
                if candidate.get("_duplicate_derived_evidence"):
                    candidate["evidence_decision"] = "rejected_duplicate_derived_evidence"
                elif candidate.get("conflict_type"):
                    candidate["evidence_decision"] = "rejected_conflict"
                elif not candidate.get("evidence_applicable"):
                    candidate["evidence_decision"] = "rejected_scope"
                elif not candidate.get("coverage_confirmed"):
                    candidate["evidence_decision"] = "rejected_incomplete"
                else:
                    candidate["evidence_decision"] = "rejected_lower_priority"
            candidate["workflow_key"] = f"{key[0]}|{key[1]}"
            candidate["repository_id"] = key[0]
            candidate["workflow_path"] = key[1]
            all_rows.append(candidate)
        rejected_reasons = sorted(
            {
                reason
                for candidate in candidates
                for reason in (candidate.get("rejection_reasons") or [])
            }
        )
        final = {
            "workflow_key": f"{key[0]}|{key[1]}",
            "repository_id": key[0],
            "workflow_path": key[1],
            "previous_query_coverage_status": row.get("query_coverage_status")
            or row.get("query_status")
            or "missing",
            "previous_coverage_confirmed": bool(row.get("coverage_confirmed")),
            "query_coverage_status": final_status,
            "coverage_confirmed": final_confirmed,
            "local_recovery": local_recovery,
            "evidence_source_type": source_type,
            "evidence_source": selected.get("evidence_source"),
            "evidence_source_file": selected.get("evidence_source_file"),
            "evidence_source_file_sha256": (
                selected.get("evidence_source_file_sha256")
                or next(
                    (
                        item.get("sha256")
                        for item in (selected.get("evidence_source_files") or [])
                        if item.get("role") == "status"
                    ),
                    None,
                )
            ),
            "evidence_source_files": selected.get("evidence_source_files") or [],
            "query_endpoint_type": selected.get("query_endpoint_type"),
            "query_restricted_to_exact_workflow_id_or_path": bool(
                selected.get("query_restricted_to_exact_workflow_id_or_path")
            ),
            "coverage_scope_allowed": bool(selected.get("coverage_scope_allowed")),
            "query_start": selected.get("query_start"),
            "query_cutoff": selected.get("query_cutoff"),
            "query_window_days": selected.get("query_window_days"),
            "query_default_branch": selected.get("query_default_branch"),
            "status": selected.get("status"),
            "query_error": selected.get("error"),
            "pages": selected.get("pages"),
            "records": selected.get("records"),
            "pagination_complete": selected.get("pagination_complete"),
            "termination_reason": selected.get("termination_reason"),
            "last_page_size": selected.get("last_page_size"),
            "fetched_at": selected.get("fetched_at"),
            "coverage_confirmation_reason": (
                selected.get("coverage_confirmation_reason") if final_confirmed else None
            ),
            "coverage_unconfirmed_reason": (
                None
                if final_confirmed
                else selected.get("coverage_unconfirmed_reason")
                or "no_usable_complete_local_query_evidence"
            ),
            # ``evidence_all`` intentionally retains duplicate provenance, but
            # workflow-level candidate counts must not count an audit-derived
            # copy a second time.
            "candidate_count": sum(
                1 for item in candidates
                if not item.get("_duplicate_derived_evidence")
            ),
            "conflict_count": sum(1 for item in candidates if item.get("conflict_type")),
            "rejected_evidence_reasons": rejected_reasons,
            "detector_valid": bool(row.get("detector_valid")),
            "boundary_valid": bool(row.get("boundary_valid")),
            "analysis_ready": bool(row.get("analysis_ready")),
            "provisional_analysis_included": bool(row.get("provisional_analysis_included")),
            "final_analysis_included_before": bool(row.get("final_analysis_included")),
            "retry_required_before": bool(row.get("retry_required")),
            "raw_window_run_count": int(row.get("raw_window_run_count") or 0),
            "qualifying_run_count": int(row.get("qualifying_run_count") or 0),
            "current_run_count": int(row.get("raw_window_run_count") or 0),
            "current_qualifying_run_count": int(row.get("qualifying_run_count") or 0),
        }
        final_rows.append(final)
    all_rows.sort(
        key=lambda item: (
            str(item.get("workflow_key") or ""),
            str(item.get("evidence_source") or ""),
            str(item.get("evidence_decision") or ""),
            str(item.get("status") or ""),
        )
    )
    conflicts.sort(
        key=lambda item: (
            str(item.get("workflow_key") or ""),
            str(item.get("conflict_type") or ""),
            str(item.get("evidence", {}).get("evidence_source") or ""),
        )
    )
    return final_rows, all_rows, conflicts, stats


def retry_rows_after_local_recovery(
    final_evidence: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    result = []
    for evidence in final_evidence:
        if not evidence.get("detector_valid") or not evidence.get("boundary_valid"):
            continue
        if evidence.get("coverage_confirmed"):
            continue
        row = dict(evidence)
        row["retry_queue"] = "rq2_retry_targets_after_local_recovery"
        row["retry_reason"] = evidence.get("coverage_unconfirmed_reason") or "local_query_coverage_unconfirmed"
        row["current_run_count"] = int(evidence.get("raw_window_run_count") or 0)
        row["current_qualifying_run_count"] = int(evidence.get("qualifying_run_count") or 0)
        result.append(row)
    return sorted(result, key=lambda item: str(item.get("workflow_key") or ""))


def strip_private_fields(value: Any) -> Any:
    """Remove internal bookkeeping keys recursively before serialization."""

    if isinstance(value, Mapping):
        return {
            key: strip_private_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [strip_private_fields(item) for item in value]
    if isinstance(value, tuple):
        return [strip_private_fields(item) for item in value]
    return value


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            clean = strip_private_fields(dict(row))
            handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def consolidate(
    audit_path: Path,
    source_dirs: Sequence[Path],
    output_dir: Path,
    runs_path: Optional[Path] = None,
) -> Dict[str, Any]:
    audit_path = audit_path.resolve()
    output_dir = output_dir.resolve()
    rows, audit_by_key = read_audit(audit_path)
    audit_hash = sha256_file(audit_path)
    retry_keys: Set[WORKFLOW_KEY] = {
        (
            normalize_id(row.get("repository_id")),
            normalize_path(row.get("workflow_path")),
        )
        for row in rows
        if row.get("retry_required")
    }
    current_complete_keys: Set[WORKFLOW_KEY] = {
        (
            normalize_id(row.get("repository_id")),
            normalize_path(row.get("workflow_path")),
        )
        for row in rows
        if row.get("coverage_confirmed")
    }
    descriptors = [build_source_descriptor(path) for path in sorted(set(source_dirs), key=lambda p: str(p.resolve())) if path.exists()]
    candidates: Dict[WORKFLOW_KEY, List[Dict[str, Any]]] = defaultdict(list)
    source_stats: Dict[str, Counter[str]] = {}
    for descriptor in descriptors:
        stats = Counter()
        eligible_keys = retry_keys
        # Reconstruct the exact/path-level status rows behind the existing
        # Phase 1 complete decisions from the 90-day status source.  Other
        # historical sources are only needed for the original retry set.
        if descriptor.source_dir.name == "rq2_run_status_unmatched_90d_20260918":
            eligible_keys = retry_keys | current_complete_keys
        build_candidates(audit_by_key, descriptor, candidates, stats, eligible_keys)
        source_stats[descriptor.label] = stats
    for row in rows:
        key = normalize_id(row.get("repository_id")), normalize_path(row.get("workflow_path"))
        current = current_audit_candidate(row, audit_path, audit_hash)
        current["repository_id"] = key[0]
        current["workflow_path"] = key[1]
        candidates[key].append(current)
    dedup_stats = deduplicate_candidates(candidates)
    if runs_path is not None:
        resolved_runs_path = runs_path.resolve()
    else:
        # Unit fixtures outside the workspace must not accidentally scan the
        # production canonical run file.  The CLI always passes --runs.
        resolved_runs_path = (
            DEFAULT_RUNS_PATH
            if audit_path.parent.is_relative_to(ROOT)
            else Path("")
        )
        if resolved_runs_path == Path(""):
            resolved_runs_path = audit_path.parent / "__no_canonical_runs_for_fixture__.jsonl"
    run_stats = attach_no_runs_run_evidence(candidates, resolved_runs_path)
    final_rows, all_rows, conflicts, final_stats = finalize_evidence(rows, candidates)
    final_stats.update(dedup_stats)
    final_stats.update(run_stats)
    retry_rows = retry_rows_after_local_recovery(final_rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_counts = {
        "rq2_query_coverage_evidence": write_jsonl(
            output_dir / "rq2_query_coverage_evidence.jsonl", final_rows
        ),
        "rq2_query_coverage_evidence_all": write_jsonl(
            output_dir / "rq2_query_coverage_evidence_all.jsonl", all_rows
        ),
        "rq2_query_coverage_conflicts": write_jsonl(
            output_dir / "rq2_query_coverage_conflicts.jsonl", conflicts
        ),
        "rq2_retry_targets_after_local_recovery": write_jsonl(
            output_dir / "rq2_retry_targets_after_local_recovery.jsonl", retry_rows
        ),
    }
    retry_repositories = sorted({str(row.get("repository_id")) for row in retry_rows})
    recovered_repositories = sorted(
        {
            str(row.get("repository_id"))
            for row in final_rows
            if row.get("local_recovery")
        }
    )
    by_source = Counter(
        str(row.get("evidence_source") or "unknown")
        for row in final_rows
        if row.get("local_recovery")
    )
    by_source_type = Counter(
        str(row.get("evidence_source_type") or "unknown")
        for row in final_rows
        if row.get("local_recovery")
    )
    source_summary = {}
    for descriptor in descriptors:
        source_summary[descriptor.label] = {
            "source_files": descriptor.source_files,
            "query_start": descriptor.query_start,
            "query_cutoff": descriptor.query_cutoff,
            "query_window_days": descriptor.query_window_days,
            "branch_scope": descriptor.branch_scope,
            "workflow_query_scope": descriptor.workflow_query_scope,
            "target_scope_counts": dict(
                Counter(scope.scope_type for scope in descriptor.scopes.values())
            ),
            "candidate_counts": dict(sorted(source_stats[descriptor.label].items())),
        }
    before_retry = Counter(
        str(row.get("previous_query_coverage_status") or "missing")
        for row in final_rows
        if row.get("retry_required_before")
    )
    original_retry_keys = {
        str(row.get("workflow_key"))
        for row in final_rows
        if row.get("retry_required_before")
    }
    original_retry_repositories = {
        str(row.get("repository_id"))
        for row in final_rows
        if row.get("retry_required_before")
    }
    original_retry_remaining = {
        str(row.get("workflow_key"))
        for row in final_rows
        if row.get("retry_required_before") and not row.get("coverage_confirmed")
    }
    recovery_from_original_retry = sum(
        1
        for row in final_rows
        if row.get("retry_required_before") and row.get("local_recovery")
    )
    after_retry = Counter(
        str(row.get("query_coverage_status") or "missing") for row in retry_rows
    )
    conflict_workflows = sorted(
        {str(row.get("workflow_key") or "") for row in conflicts if row.get("workflow_key")}
    )
    scope_unclear_workflows = sorted(
        {
            str(row.get("workflow_key") or "")
            for row in all_rows
            if row.get("workflow_key")
            and any(
                "scope" in str(reason) or "path_filtered" in str(reason)
                for reason in row.get("rejection_reasons") or []
            )
        }
    )
    class_counts = Counter(str(row.get("conflict_type") or "") for row in conflicts)
    class_workflows: Dict[str, Set[str]] = defaultdict(set)
    for item in conflicts:
        class_workflows[str(item.get("conflict_type") or "")].add(
            str(item.get("workflow_key") or "")
        )
    true_conflict_workflows = class_workflows.get("true_no_runs_vs_matching_run", set())
    pseudo_classes = {
        "other_branch_not_conflict",
        "different_workflow_id_not_conflict",
        "outside_query_window_not_conflict",
    }
    downgraded_reasons = Counter()
    true_by_key = {
        key: "true_no_runs_vs_matching_run"
        for key in true_conflict_workflows
    }
    for row in final_rows:
        if row.get("previous_coverage_confirmed") and not row.get("coverage_confirmed"):
            key = str(row.get("workflow_key") or "")
            downgraded_reasons[true_by_key.get(key, "other") or "other"] += 1
    summary: Dict[str, Any] = {
        "schema_version": "rq2-query-coverage-consolidation-v1-phase1.2.1",
        "audit_stage": "phase1.2.1_local_query_evidence_conflict_correction",
        "external_fetch_performed": False,
        "canonical_inputs_modified": False,
        "statistical_models_rerun": False,
        "audit_input": source_label(audit_path),
        "audit_input_sha256": audit_hash,
        "canonical_runs_input": source_label(resolved_runs_path),
        "canonical_runs_input_sha256": (
            sha256_file(resolved_runs_path) if resolved_runs_path.exists() else None
        ),
        "source_directories": [descriptor.label for descriptor in descriptors],
        "source_summary": source_summary,
        "workflow_count": len(rows),
        # ``evidence_all`` keeps every provenance record, including derived
        # duplicates, while the headline candidate count is deduplicated.
        "evidence_candidate_records": sum(
            1
            for row in all_rows
            if not row.get("_duplicate_derived_evidence")
        ),
        "evidence_candidate_records_all": len(all_rows),
        "evidence_final_workflows": len(final_rows),
        "coverage_confirmed_workflows": final_stats.get("coverage_confirmed_workflows", 0),
        "local_recovery_workflows": final_stats.get("local_recovery_workflows", 0),
        "local_recovery_repositories": len(recovered_repositories),
        "local_recovery_source_counts": dict(sorted(by_source.items())),
        "local_recovery_source_type_counts": dict(sorted(by_source_type.items())),
        "original_retry_workflows": len(original_retry_keys),
        "original_retry_repositories": len(original_retry_repositories),
        "local_recovery_from_original_retry_workflows": recovery_from_original_retry,
        "original_retry_remaining_unconfirmed_workflows": len(original_retry_remaining),
        "retry_added_after_conflict_workflows": len(
            {str(row.get("workflow_key")) for row in retry_rows}
            - original_retry_keys
        ),
        "conflict_records": class_counts.get("true_no_runs_vs_matching_run", 0),
        "conflict_workflows": len(true_conflict_workflows),
        "conflict_type_counts": {
            "true_no_runs_vs_matching_run": class_counts.get(
                "true_no_runs_vs_matching_run", 0
            )
        },
        "conflict_class_records": len(conflicts),
        "conflict_class_counts": dict(sorted(class_counts.items())),
        "conflict_class_workflows": {
            key: len(value) for key, value in sorted(class_workflows.items())
        },
        "true_conflict_workflows": len(true_conflict_workflows),
        "pseudo_conflict_records": sum(class_counts.get(key, 0) for key in pseudo_classes),
        "pseudo_conflict_workflows": len(
            set().union(*(class_workflows.get(key, set()) for key in pseudo_classes))
        ),
        "duplicate_derived_evidence_records": class_counts.get(
            "duplicate_derived_evidence", 0
        ),
        "duplicate_derived_evidence_workflows": len(
            class_workflows.get("duplicate_derived_evidence", set())
        ),
        "phase1_complete_downgraded_workflows": sum(downgraded_reasons.values()),
        "phase1_complete_downgrade_reasons": dict(sorted(downgraded_reasons.items())),
        "scope_unclear_records": sum(
            1
            for row in all_rows
            if any("scope" in str(reason) or "path_filtered" in str(reason) for reason in row.get("rejection_reasons") or [])
        ),
        "scope_unclear_workflows": len(scope_unclear_workflows),
        "still_unconfirmed_workflows": len(retry_rows),
        "still_unconfirmed_repositories": len(retry_repositories),
        "retry_query_status_before": dict(sorted(before_retry.items())),
        "retry_query_status_after": dict(sorted(after_retry.items())),
        "retry_query_missing_after": after_retry.get("missing", 0),
        "retry_api_unresolved_after": after_retry.get("api_unresolved", 0),
        "output_record_counts": output_counts,
        "input_file_sha256": {
            source_label(audit_path): audit_hash,
            **{
                label: sha
                for descriptor in descriptors
                for label, sha in descriptor.source_hashes.items()
            },
            source_label(resolved_runs_path): (
                sha256_file(resolved_runs_path) if resolved_runs_path.exists() else None
            ),
        },
    }
    (output_dir / "rq2_query_coverage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidate local RQ2 query-completion evidence without GitHub API calls"
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--source-dir", type=Path, action="append", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS_PATH)
    args = parser.parse_args()
    summary = consolidate(
        args.audit,
        args.source_dir if args.source_dir is not None else DEFAULT_SOURCE_DIRS,
        args.output_dir,
        args.runs,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
