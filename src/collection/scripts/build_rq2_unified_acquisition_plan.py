"""Build a deterministic, repository-level RQ2 acquisition plan.

This module is deliberately local-only.  It consumes the Phase 1.2.1 retry
queue, the local coverage evidence/conflict products, canonical metadata and
historical fetch-status files, then writes a plan.  It does not construct an
API client, read credentials, or write anywhere under the canonical input
directory.

The plan has two levels:

* one repository row with nested workflow targets and exactly one top-level
  ``plan_status``; and
* one flattened workflow row, which is the audit-friendly one-to-one mapping
  used by the execution stage.

The distinction is important for mixed repositories.  A repository can have
one top-level ``access_probe_required`` status while an individual target in
that repository is a normal ``fetch_required`` target.  The top-level status
describes the first repository action; the workflow-level action describes the
evidence that still needs to be obtained for that target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data_26_6_9_canonical_20260609"
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_QUERY_START = "2026-05-10T23:59:59Z"
DEFAULT_WINDOW_DAYS = 30
PLAN_SCHEMA_VERSION = "rq2-unified-acquisition-plan-v1-phase2.0"
PLAN_RULES_VERSION = "rq2-phase2.0-repository-plan-rules-2026-09-19"
COMPLETION_CRITERIA = {
    "window_complete": "query covers 2026-05-10T23:59:59Z through 2026-06-09T23:59:59Z",
    "pagination": "every request sequence terminates on an empty or short page and never reaches the protective maximum",
    "workflow_specific": "workflow-specific mode gives every target workflow ID a terminal response",
    "repository_wide": "repository-wide mode terminates as a complete repository query",
    "deduplication": "all raw runs are deduplicated before target mapping",
    "target_resolution": "each target ends as complete, no qualifying runs, or an explicit terminal exclusion",
}

DEFAULT_RETRY_TARGETS = (
    ROOT
    / "output_canonical_rq2_coverage_audit_phase1_2_1_20260919"
    / "rq2_retry_targets.jsonl"
)
DEFAULT_COVERAGE_EVIDENCE = (
    ROOT
    / "output_canonical_rq2_query_coverage_phase1_2_1_20260919"
    / "rq2_query_coverage_evidence.jsonl"
)
DEFAULT_COVERAGE_CONFLICTS = (
    ROOT
    / "output_canonical_rq2_query_coverage_phase1_2_1_20260919"
    / "rq2_query_coverage_conflicts.jsonl"
)
DEFAULT_COVERAGE_SUMMARY = (
    ROOT
    / "output_canonical_rq2_query_coverage_phase1_2_1_20260919"
    / "rq2_query_coverage_summary.json"
)
DEFAULT_REPOSITORIES = CANONICAL_DIR / "repositories.jsonl"
DEFAULT_SNAPSHOTS = CANONICAL_DIR / "workflow_snapshots.jsonl"
DEFAULT_RUNS = CANONICAL_DIR / "workflow_runs.jsonl"
DEFAULT_OUTPUT_DIR = ROOT / "output_rq2_unified_acquisition_plan_phase2_0_20260919"

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

WorkflowKey = Tuple[str, str]


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
        raise ValueError(f"Expected JSON object at {path}")
    return value


def norm_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def norm_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def workflow_key(repository_id: Any, workflow_path: Any) -> WorkflowKey:
    return norm_id(repository_id), norm_path(workflow_path)


def workflow_key_text(key: WorkflowKey) -> str:
    return f"{key[0]}|{key[1]}"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sort_id(value: Any) -> Tuple[int, Any]:
    text = norm_id(value)
    if text.isdigit():
        return 0, int(text)
    return 1, text


def source_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_nonempty_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def resolve_path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    candidate = Path(str(value).strip().replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve() if candidate.exists() else None


def ensure_required(path: Path) -> Path:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(stable_json(row) + "\n")


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def compact_file_ref(path: Path, role: str, hash_cache: Dict[str, str]) -> Dict[str, str]:
    path = path.resolve()
    label = source_label(path)
    if label not in hash_cache:
        hash_cache[label] = sha256_file(path)
    return {"path": label, "role": role, "sha256": hash_cache[label]}


def dedup_file_refs(refs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    by_path: Dict[str, Dict[str, Any]] = {}
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path:
            continue
        item = by_path.setdefault(
            path,
            {"path": path, "sha256": str(ref.get("sha256") or ""), "roles": []},
        )
        sha = str(ref.get("sha256") or "")
        if sha and not item.get("sha256"):
            item["sha256"] = sha
        role = str(ref.get("role") or "")
        if role and role not in item["roles"]:
            item["roles"].append(role)
    for item in by_path.values():
        item["roles"] = sorted(item["roles"])
    return [by_path[key] for key in sorted(by_path)]


def status_code(record: Mapping[str, Any]) -> Optional[int]:
    for field in ("http_status", "status_code", "http_code"):
        value = record.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    error = str(record.get("error") or record.get("query_error") or "")
    # Do not search for bare digit substrings: transport errors such as
    # ``IncompleteRead(1404625 bytes read, 14992 more expected)`` can contain
    # the characters ``404`` without being an HTTP 404 response.
    match = re.search(
        r"(?:https?[^ ]*\s+|http\s*(?:error|status)?\s*[:=]?\s*|status(?:_code)?\s*[:=]?\s*)"
        r"(403|404|410|451)\b",
        error,
        flags=re.IGNORECASE,
    )
    if match:
        return int(match.group(1))
    return None


def is_forbidden(record: Mapping[str, Any]) -> bool:
    return status_code(record) == 403 or str(
        record.get("status") or record.get("query_observed_status") or ""
    ).lower() in {"forbidden", "access_denied"}


def is_terminal(record: Mapping[str, Any]) -> bool:
    code = status_code(record)
    if code in {404, 410, 451}:
        return True
    status = str(record.get("status") or record.get("query_observed_status") or "").lower()
    if status in {"not_found", "gone", "unavailable_for_legal_reasons", "deleted"}:
        return True
    text = str(record.get("error") or record.get("query_error") or "").lower()
    return any(
        marker in text
        for marker in (
            "repository deleted",
            "repo deleted",
            "workflow not found",
            "workflow inaccessible",
            "repository not found",
        )
    )


def is_page_cap(record: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(record.get(field) or "").lower()
        for field in ("error", "query_error", "termination_reason")
    )
    return "max_page" in text or "max page" in text or "pagination_limit" in text


def numeric_pages(record: Mapping[str, Any]) -> int:
    for field in ("pages", "query_pages"):
        value = record.get(field)
        if value in (None, ""):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def infer_scope_type(target: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    explicit = str(
        target.get("endpoint_type")
        or target.get("query_endpoint_type")
        or target.get("workflow_query_scope_type")
        or ""
    ).strip().lower()
    if explicit in {"repository_wide_unfiltered", "repository-wide-unfiltered"}:
        return "repository_wide_unfiltered"
    if explicit in {"repository_wide_path_filtered", "repository-wide-path-filtered"}:
        return "repository_wide_path_filtered"
    if explicit in {"workflow_specific", "workflow_specific_batch", "workflow-specific"}:
        return "workflow_specific_batch"

    filters = [item for item in (target.get("workflow_filters") or []) if isinstance(item, dict)]
    if filters:
        has_id = [bool(norm_id(item.get("workflow_id"))) for item in filters]
        has_path = [bool(norm_path(item.get("workflow_path"))) for item in filters]
        if all(has_id) and all(has_path):
            return "workflow_specific_batch"
        if all(has_path) and not any(has_id):
            return "repository_wide_path_filtered"
        return "scope_unclear"

    scope_text = str(metadata.get("workflow_query_scope") or "").lower()
    if "repository-wide endpoint unfiltered" in scope_text:
        return "repository_wide_unfiltered"
    if scope_text.strip() == "repository-wide endpoint fallback":
        return "repository_wide_unfiltered"
    if "repository-wide unfiltered" in scope_text and "workflow-specific" not in scope_text:
        return "repository_wide_unfiltered"
    # A mixed declaration without a workflow-level result cannot prove that an
    # empty target filter means an unfiltered repository query.
    return "scope_unclear"


def target_filter_keys(target: Mapping[str, Any]) -> Set[WorkflowKey]:
    keys: Set[WorkflowKey] = set()
    for item in (target.get("workflow_filters") or []):
        if not isinstance(item, dict):
            continue
        rid = norm_id(target.get("repository_id"))
        path = norm_path(item.get("workflow_path"))
        if rid and path:
            keys.add((rid, path))
    return keys


def choose_record(records: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not records:
        return None
    # The final evidence file should be one-row-per-workflow, but choosing a
    # canonical maximum keeps synthetic/legacy inputs order-independent.
    return dict(
        sorted(
            (dict(record) for record in records),
            key=lambda value: stable_json(value),
        )[-1]
    )


def load_canonical_context(
    repositories_path: Path,
    snapshots_path: Path,
    runs_path: Path,
    hash_cache: Dict[str, str],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[WorkflowKey, Set[str]], Dict[str, Any]]:
    repositories: Dict[str, Dict[str, Any]] = {}
    for row in load_jsonl(repositories_path):
        rid = norm_id(row.get("repository_id") or row.get("id"))
        if not rid:
            continue
        current = repositories.get(rid)
        candidate = {
            "repository_id": rid,
            "repository_name": str(
                row.get("canonical_name") or row.get("repository_name") or row.get("name") or ""
            ),
            "default_branch": str(row.get("default_branch") or ""),
            "primary_ecosystem": str(row.get("primary_ecosystem") or ""),
        }
        if current is None or stable_json(candidate) < stable_json(current):
            repositories[rid] = candidate

    snapshot_ids: Dict[WorkflowKey, Set[str]] = defaultdict(set)
    snapshot_rows = 0
    for row in load_jsonl(snapshots_path):
        snapshot_rows += 1
        key = workflow_key(row.get("repository_id"), row.get("workflow_path"))
        wid = norm_id(row.get("workflow_id"))
        if key[0] and key[1] and wid:
            snapshot_ids[key].add(wid)

    # The full run file is intentionally not materialized.  Its hash and row
    # count provide input provenance; qualifying-run counts remain the values
    # audited in the Phase 1.2.1 retry row.
    run_rows = count_nonempty_lines(runs_path)
    files = {
        "canonical_repositories": compact_file_ref(
            repositories_path, "canonical_repositories", hash_cache
        ),
        "canonical_workflow_snapshots": compact_file_ref(
            snapshots_path, "canonical_workflow_snapshots", hash_cache
        ),
        "canonical_workflow_runs": compact_file_ref(
            runs_path, "canonical_workflow_runs", hash_cache
        ),
    }
    return repositories, snapshot_ids, {
        "files": files,
        "repository_records": len(repositories),
        "workflow_snapshot_records": snapshot_rows,
        "workflow_snapshot_keys_with_ids": sum(bool(value) for value in snapshot_ids.values()),
        "workflow_run_records": run_rows,
    }


def load_source_histories(
    source_dirs: Sequence[Path],
    hash_cache: Dict[str, str],
) -> Tuple[
    Dict[str, List[Dict[str, Any]]],
    Dict[WorkflowKey, List[Dict[str, Any]]],
    Dict[str, Set[str]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
]:
    """Load historical repository status and target-scope evidence.

    Returns repository events, workflow-detail events, source scopes, source
    file references by repository, and the source descriptor list.  Status
    rows are copied only as compact evidence; no supplemental run payload is
    read here.
    """

    repository_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    workflow_events: Dict[WorkflowKey, List[Dict[str, Any]]] = defaultdict(list)
    repo_scopes: Dict[str, Set[str]] = defaultdict(set)
    repo_files: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    descriptors: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    normalized_dirs = sorted({path.resolve() for path in source_dirs}, key=source_label)
    for source_dir in normalized_dirs:
        if not source_dir.exists():
            continue
        metadata: Dict[str, Any] = {}
        metadata_paths = sorted(source_dir.glob("*.json"), key=lambda p: source_label(p))
        metadata_refs: List[Dict[str, Any]] = []
        for metadata_path in metadata_paths:
            try:
                value = load_json(metadata_path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            metadata.update(value)
            metadata_refs.append(
                compact_file_ref(metadata_path, "historical_metadata", hash_cache)
            )

        target_path = resolve_path(metadata.get("targets"))
        if target_path is None:
            local_target = source_dir / "target_repositories.jsonl"
            if local_target.exists():
                target_path = local_target.resolve()
        status_path = source_dir / "repository_fetch_status.jsonl"
        if not status_path.exists():
            continue

        source_refs = list(metadata_refs)
        source_refs.append(compact_file_ref(status_path, "historical_repository_status", hash_cache))
        if target_path is not None and target_path.exists():
            source_refs.append(compact_file_ref(target_path, "historical_target_repositories", hash_cache))
        source_refs = dedup_file_refs(source_refs)
        source_label_text = source_label(source_dir)

        target_by_repo: Dict[str, Dict[str, Any]] = {}
        if target_path is not None and target_path.exists():
            for target in load_jsonl(target_path):
                rid = norm_id(target.get("repository_id"))
                if not rid:
                    continue
                target_by_repo[rid] = dict(target)
                scope = infer_scope_type(target, metadata)
                repo_scopes[rid].add(scope)
                repo_files[rid].extend(source_refs)

        for row in load_jsonl(status_path):
            rid = norm_id(row.get("repository_id"))
            if not rid:
                continue
            target = target_by_repo.get(rid)
            scope = infer_scope_type(target, metadata) if target is not None else "scope_unclear"
            repo_scopes[rid].add(scope)
            repo_files[rid].extend(source_refs)
            base = {
                "source_directory": source_label_text,
                "source_status_file": source_label(status_path),
                "status": str(row.get("status") or ""),
                "http_status": status_code(row),
                "error": row.get("error"),
                "pages": numeric_pages(row),
                "records": row.get("records"),
                "fetched_at": row.get("fetched_at"),
                "default_branch": row.get("default_branch"),
                "scope_type": scope,
                "source_files": source_refs,
            }
            details = [
                item for item in (row.get("workflow_statuses") or []) if isinstance(item, dict)
            ]
            if not details:
                repository_events[rid].append(base)
                continue

            # A repository-level 403/error/not-found still applies to every
            # target in the failed request.  Successful/no_runs rows are used
            # only through their exact workflow detail records.
            parent_status = str(row.get("status") or "").lower()
            if parent_status in {"forbidden", "error", "unresolved", "not_found"}:
                repository_events[rid].append(base)
            for detail in details:
                path = norm_path(detail.get("workflow_path"))
                if not path:
                    continue
                event = dict(base)
                event.update(
                    {
                        "status": str(detail.get("status") or base["status"]),
                        "workflow_id": norm_id(detail.get("workflow_id")),
                        "workflow_path": path,
                        "pages": numeric_pages(detail),
                        "records": detail.get("records"),
                        "complete": detail.get("complete")
                        if "complete" in detail
                        else detail.get("pagination_complete"),
                        "pagination_complete": detail.get("pagination_complete"),
                        "termination_reason": detail.get("termination_reason"),
                        "last_page_size": detail.get("last_page_size"),
                        "scope_type": (
                            "workflow_status_detail"
                            if scope == "scope_unclear"
                            else scope
                        ),
                    }
                )
                workflow_events[(rid, path)].append(event)

        descriptors[source_label_text].append(
            {
                "source_directory": source_label_text,
                "source_files": source_refs,
                "metadata": metadata,
                "scope_by_repository": {
                    rid: sorted(scopes) for rid, scopes in target_by_repo.items()
                },
            }
        )

    return repository_events, workflow_events, repo_scopes, repo_files, descriptors


def applicable_history_events(
    repository_id: str,
    path: str,
    workflow_id: str,
    repository_events: Mapping[str, Sequence[Mapping[str, Any]]],
    workflow_events: Mapping[WorkflowKey, Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    direct = [dict(item) for item in workflow_events.get((repository_id, path), [])]
    result: List[Dict[str, Any]] = list(direct)
    for event in repository_events.get(repository_id, []):
        event_path = norm_path(event.get("workflow_path"))
        event_id = norm_id(event.get("workflow_id"))
        if event_path:
            if event_path == path and (not event_id or not workflow_id or event_id == workflow_id):
                result.append(dict(event))
            continue
        # Repository-level failure/access evidence is useful even when the
        # target scope was not reconstructed.  Successful/no_runs rows are
        # only broadcast when the historical query itself was proven
        # repository-wide and unfiltered.
        if is_forbidden(event) or is_terminal(event):
            result.append(dict(event))
        elif event.get("scope_type") == "repository_wide_unfiltered":
            result.append(dict(event))
    unique: Dict[str, Dict[str, Any]] = {}
    for event in result:
        unique[stable_json(event)] = event
    return [unique[key] for key in sorted(unique)]


def compact_history_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    fields = (
        "source_directory",
        "source_status_file",
        "status",
        "http_status",
        "error",
        "pages",
        "records",
        "fetched_at",
        "default_branch",
        "scope_type",
        "workflow_id",
        "workflow_path",
        "complete",
        "pagination_complete",
        "termination_reason",
        "last_page_size",
    )
    return {field: event.get(field) for field in fields if field in event}


def load_retry_rows(path: Path) -> Dict[WorkflowKey, Dict[str, Any]]:
    grouped: Dict[WorkflowKey, List[Dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(path):
        key = workflow_key(row.get("repository_id"), row.get("workflow_path"))
        if key[0] and key[1]:
            grouped[key].append(dict(row))
    result: Dict[WorkflowKey, Dict[str, Any]] = {}
    for key, rows in grouped.items():
        selected = choose_record(rows)
        if selected is not None:
            result[key] = selected
    return result


def load_evidence(path: Path) -> Dict[WorkflowKey, Dict[str, Any]]:
    grouped: Dict[WorkflowKey, List[Dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(path):
        key = workflow_key(row.get("repository_id"), row.get("workflow_path"))
        if key[0] and key[1]:
            grouped[key].append(dict(row))
    result: Dict[WorkflowKey, Dict[str, Any]] = {}
    for key, rows in grouped.items():
        selected = choose_record(rows)
        if selected is not None:
            result[key] = selected
    return result


def load_conflicts(path: Path) -> Dict[WorkflowKey, List[Dict[str, Any]]]:
    grouped: Dict[WorkflowKey, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in load_jsonl(path):
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        rid = norm_id(row.get("repository_id") or evidence.get("repository_id"))
        path_value = norm_path(row.get("workflow_path") or evidence.get("workflow_path"))
        key = (rid, path_value)
        if not rid or not path_value:
            continue
        compact = {
            "conflict_type": row.get("conflict_type"),
            "is_conflict": bool(row.get("is_conflict")),
            "reason": row.get("reason"),
            "evidence_decision": evidence.get("evidence_decision"),
            "evidence_source": evidence.get("evidence_source"),
            "evidence_source_file": evidence.get("evidence_source_file"),
            "evidence_source_file_sha256": evidence.get("evidence_source_file_sha256"),
            "status": evidence.get("status"),
            "query_error": evidence.get("query_error"),
        }
        grouped[key][stable_json(compact)] = compact
    return {
        key: [value[stable] for stable in sorted(value)]
        for key, value in grouped.items()
    }


def effective_workflow_id(
    row: Mapping[str, Any], snapshot_ids: Mapping[WorkflowKey, Set[str]], key: WorkflowKey
) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
    original = norm_id(row.get("workflow_id"))
    recovered = norm_id(row.get("recovered_workflow_id"))
    old = norm_id(row.get("old_workflow_id"))
    sources: List[Dict[str, Any]] = []
    if original:
        sources.append({"source": "retry_target.workflow_id", "workflow_id": original})
        return original, "retry_target.workflow_id", sources
    if recovered:
        sources.append(
            {"source": "retry_target.recovered_workflow_id", "workflow_id": recovered}
        )
        return recovered, "retry_target.recovered_workflow_id", sources
    if old:
        sources.append({"source": "retry_target.old_workflow_id", "workflow_id": old})
        return old, "retry_target.old_workflow_id", sources
    candidates = sorted(snapshot_ids.get(key, set()), key=sort_id)
    for candidate in candidates:
        sources.append({"source": "canonical_workflow_snapshot.workflow_id", "workflow_id": candidate})
    if len(candidates) == 1:
        return candidates[0], "canonical_workflow_snapshot.workflow_id", sources
    return "", None, sources


def current_run_count(row: Mapping[str, Any]) -> int:
    for field in ("raw_window_run_count", "current_run_count", "query_records"):
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def current_qualifying_run_count(row: Mapping[str, Any]) -> int:
    for field in (
        "qualifying_run_count",
        "original_rq2_qualifying_run_count",
        "current_qualifying_run_count",
    ):
        value = row.get(field)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def bool_value(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def build_common_sources(
    input_refs: Mapping[str, Mapping[str, Any]],
    role_prefix: str = "phase2_input",
) -> List[Dict[str, Any]]:
    refs = []
    for name, ref in sorted(input_refs.items()):
        refs.append(
            {
                "path": ref["path"],
                "sha256": ref["sha256"],
                "role": f"{role_prefix}:{name}",
            }
        )
    return refs


def choose_plan_status(actions: Sequence[str]) -> str:
    unique = set(actions)
    if unique == {"already_resolved"}:
        return "already_resolved"
    if "fetch_required" in unique:
        return "fetch_required"
    if "access_probe_required" in unique:
        return "access_probe_required"
    if "terminal_review" in unique:
        return "terminal_review"
    return "already_resolved"


def make_endpoint_plan(
    *,
    top_level_status: str,
    targets: Sequence[Mapping[str, Any]],
    historical_max_pages: int,
    history_hit_page_cap: bool,
    all_workflow_ids_known: bool,
    historical_scope_types: Sequence[str],
) -> Dict[str, Any]:
    actionable = top_level_status in {"fetch_required", "access_probe_required"}
    if not actionable:
        return {
            "primary_endpoint_strategy": None,
            "fallback_endpoint_strategy": None,
            "fallback_trigger": None,
            "estimated_min_requests": 0,
            "estimated_max_requests": 0,
            "historical_max_pages": historical_max_pages,
            "history_hit_protective_max_pages": history_hit_page_cap,
            "all_workflow_ids_known": all_workflow_ids_known,
            "historical_scope_types": list(sorted(historical_scope_types)),
        }

    target_count = len(targets)
    use_specific = history_hit_page_cap and all_workflow_ids_known
    base_primary = (
        "workflow-specific-30d-by-id"
        if use_specific
        else "repository-wide-unfiltered-30d-default-branch"
    )
    if top_level_status == "access_probe_required":
        primary = f"access-probe-once-then-{base_primary}"
        probe_cost = 1
    else:
        primary = base_primary
        probe_cost = 0

    if use_specific:
        fallback = "repository-wide-unfiltered-30d-default-branch"
        trigger = (
            "after the controlled probe (if applicable), if any workflow-specific target "
            "lacks a terminal response or its endpoint fails"
        )
        per_target = []
        for target in targets:
            pages = int(target.get("historical_workflow_specific_max_pages") or 0)
            if pages <= 0:
                pages = 1
            if target.get("workflow_specific_hit_page_cap"):
                pages += 1
            per_target.append(pages)
        minimum = target_count + probe_cost
        maximum = sum(per_target) + probe_cost
    else:
        if all_workflow_ids_known:
            fallback = "workflow-specific-30d-by-id"
            trigger = (
                "only if the repository-wide query reaches its protective page limit or "
                "cannot terminate normally; do not launch both modes unconditionally"
            )
        else:
            fallback = "repository-wide-unfiltered-30d-resume-with-explicit-page-budget"
            trigger = (
                "only if the repository-wide query fails or reaches its protective page limit; "
                "workflow-specific fallback is forbidden while any target ID is missing"
            )
        minimum = 1 + probe_cost
        maximum = max(1, historical_max_pages)
        if history_hit_page_cap:
            maximum += 1
        maximum += probe_cost

    return {
        "primary_endpoint_strategy": primary,
        "fallback_endpoint_strategy": fallback,
        "fallback_trigger": trigger,
        "estimated_min_requests": minimum,
        "estimated_max_requests": max(minimum, maximum),
        "historical_max_pages": historical_max_pages,
        "history_hit_protective_max_pages": history_hit_page_cap,
        "all_workflow_ids_known": all_workflow_ids_known,
        "historical_scope_types": list(sorted(historical_scope_types)),
    }


def build_plan(
    retry_targets_path: Path,
    coverage_evidence_path: Path,
    coverage_conflicts_path: Path,
    coverage_summary_path: Path,
    repositories_path: Path,
    snapshots_path: Path,
    runs_path: Path,
    source_dirs: Sequence[Path],
    output_dir: Path,
) -> Dict[str, Any]:
    """Build all Phase 2.0 outputs and return the deterministic summary."""

    input_paths = {
        "retry_targets": ensure_required(retry_targets_path),
        "coverage_evidence": ensure_required(coverage_evidence_path),
        "coverage_conflicts": ensure_required(coverage_conflicts_path),
        "coverage_summary": ensure_required(coverage_summary_path),
        "canonical_repositories": ensure_required(repositories_path),
        "canonical_workflow_snapshots": ensure_required(snapshots_path),
        "canonical_workflow_runs": ensure_required(runs_path),
    }
    resolved_sources = [ensure_required(path) for path in source_dirs]
    output_dir = output_dir.resolve()
    if is_relative_to(output_dir, CANONICAL_DIR):
        raise ValueError(f"Refusing to write a plan below canonical input directory: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    hash_cache: Dict[str, str] = {}
    input_refs: Dict[str, Dict[str, Any]] = {}
    for name, path in input_paths.items():
        input_refs[name] = compact_file_ref(path, f"phase2_input:{name}", hash_cache)

    repositories, snapshot_ids, canonical_stats = load_canonical_context(
        input_paths["canonical_repositories"],
        input_paths["canonical_workflow_snapshots"],
        input_paths["canonical_workflow_runs"],
        hash_cache,
    )
    retry_rows = load_retry_rows(input_paths["retry_targets"])
    evidence_rows = load_evidence(input_paths["coverage_evidence"])
    conflicts = load_conflicts(input_paths["coverage_conflicts"])
    coverage_summary = load_json(input_paths["coverage_summary"])

    (
        repository_events,
        workflow_events,
        repo_scopes,
        repo_history_files,
        source_descriptors,
    ) = load_source_histories(resolved_sources, hash_cache)

    # Add all historical files to the manifest, even if a source contains no
    # row for a current retry repository.  This makes the provenance complete
    # and independent of target ordering.
    manifest_refs = list(input_refs.values())
    for descriptor_list in source_descriptors.values():
        for descriptor in descriptor_list:
            manifest_refs.extend(descriptor.get("source_files", []))
    for refs in repo_history_files.values():
        manifest_refs.extend(refs)
    manifest_refs = dedup_file_refs(manifest_refs)

    common_refs = build_common_sources(input_refs)
    conflict_file_ref = input_refs["coverage_conflicts"]
    evidence_file_ref = input_refs["coverage_evidence"]

    workflow_rows: List[Dict[str, Any]] = []
    by_repository: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    workflow_validation_errors: List[str] = []

    for key in sorted(retry_rows, key=lambda value: (sort_id(value[0]), value[1])):
        row = retry_rows[key]
        rid, path = key
        evidence = evidence_rows.get(key, {})
        repository = repositories.get(rid, {})
        effective_id, id_source, id_sources = effective_workflow_id(row, snapshot_ids, key)
        history = applicable_history_events(
            rid, path, effective_id, repository_events, workflow_events
        )
        history_compact = [compact_history_event(event) for event in history]
        current_status_record = {
            "status": evidence.get("status") or row.get("query_observed_status"),
            "query_error": evidence.get("query_error") or row.get("query_error"),
            "http_status": status_code(evidence) if evidence else None,
            "query_observed_status": evidence.get("status") or row.get("query_observed_status"),
        }
        current_forbidden = is_forbidden(current_status_record)
        current_terminal = is_terminal(current_status_record)
        history_forbidden = any(is_forbidden(item) for item in history)
        history_terminal = any(
            is_terminal(item)
            and item.get("scope_type") != "scope_unclear"
            for item in history
        )

        coverage_confirmed = bool_value(row.get("coverage_confirmed")) or bool_value(
            evidence.get("coverage_confirmed")
        )
        analysis_ready = bool_value(row.get("analysis_ready"))
        if coverage_confirmed:
            action = "already_resolved"
        elif current_terminal or history_terminal:
            action = "terminal_review"
        elif current_forbidden or history_forbidden:
            action = "access_probe_required"
        else:
            action = "fetch_required"

        conflict_records = conflicts.get(key, [])
        conflict_types = sorted(
            {
                str(item.get("conflict_type"))
                for item in conflict_records
                if item.get("conflict_type")
            }
        )
        true_conflict = "true_no_runs_vs_matching_run" in conflict_types
        scope_unclear = "scope_unresolved" in conflict_types or any(
            item.get("scope_type") == "scope_unclear" for item in history
        )
        retry_reasons = []
        original_reason = row.get("retry_reason")
        if original_reason:
            retry_reasons.append(str(original_reason))
        if true_conflict:
            retry_reasons.append("true_conflict_requires_authoritative_requery")
        if scope_unclear:
            retry_reasons.append("scope_unclear_requires_explicit_endpoint_scope")
        if action == "access_probe_required":
            retry_reasons.append("historical_forbidden_or_http_403")
        if action == "terminal_review":
            retry_reasons.append("terminal_http_or_repository_status")
        if action == "already_resolved":
            retry_reasons.append("retry_input_contains_sufficient_complete_evidence")
        retry_reasons = sorted(set(retry_reasons))

        historical_pages = [numeric_pages(event) for event in history]
        historical_max_pages = max(historical_pages or [0])
        workflow_specific_pages = [
            numeric_pages(event)
            for event in history
            if event.get("scope_type") == "workflow_specific_batch"
        ]
        workflow_specific_cap = any(
            is_page_cap(event)
            for event in history
            if event.get("scope_type") == "workflow_specific_batch"
        )
        repo_wide_cap = any(
            is_page_cap(event)
            and event.get("scope_type")
            in {"repository_wide_unfiltered", "repository_wide_path_filtered"}
            for event in history
        )
        evidence_endpoint = str(evidence.get("query_endpoint_type") or "")
        if evidence_endpoint in {"repository_wide_unfiltered", "repository_wide_path_filtered"} and is_page_cap(evidence):
            repo_wide_cap = True
        if evidence_endpoint == "workflow_specific" and is_page_cap(evidence):
            workflow_specific_cap = True
        if evidence_endpoint in {"workflow_specific", "workflow_specific_batch"}:
            workflow_specific_pages.append(numeric_pages(evidence))
        if evidence_endpoint in {"repository_wide_unfiltered", "repository_wide_path_filtered"}:
            historical_max_pages = max(historical_max_pages, numeric_pages(evidence))

        historical_statuses = sorted(
            {
                str(event.get("status"))
                for event in history
                if event.get("status") not in (None, "")
            }
        )
        historical_errors = sorted(
            {
                str(value)
                for event in history
                for value in (event.get("error"),)
                if value not in (None, "")
            }
        )
        terminal_reasons = sorted(
            {
                str(event.get("error") or event.get("status"))
                for event in history
                if is_terminal(event)
            }
            | (
                {str(current_status_record.get("query_error"))}
                if current_terminal and current_status_record.get("query_error")
                else set()
            )
        )

        target_sources = list(common_refs)
        target_sources.append(
            {
                "path": evidence_file_ref["path"],
                "sha256": evidence_file_ref["sha256"],
                "role": "phase1_2_1_coverage_evidence",
            }
        )
        target_sources.append(
            {
                "path": conflict_file_ref["path"],
                "sha256": conflict_file_ref["sha256"],
                "role": "phase1_2_1_conflict_report",
            }
        )
        target_sources.extend(repo_history_files.get(rid, []))
        target_sources = dedup_file_refs(target_sources)

        repo_name = str(
            repository.get("repository_name")
            or row.get("repository_name")
            or ""
        )
        default_branch = str(
            repository.get("default_branch")
            or row.get("default_branch")
            or ""
        )
        target = {
            "workflow_key": workflow_key_text(key),
            "repository_id": rid,
            "repository_name": repo_name,
            "default_branch": default_branch,
            "cutoff": DEFAULT_CUTOFF,
            "query_start": DEFAULT_QUERY_START,
            "query_window_days": DEFAULT_WINDOW_DAYS,
            "workflow_path": path,
            "workflow_id": effective_id or None,
            "workflow_id_source": id_source,
            "workflow_id_sources": id_sources,
            "workflow_id_status": row.get("workflow_id_status"),
            "candidate_workflow_ids": sorted(
                {norm_id(value) for value in (row.get("candidate_workflow_ids") or []) if norm_id(value)},
                key=sort_id,
            ),
            "query_coverage_status": row.get("query_coverage_status")
            or row.get("query_status"),
            "current_run_count": current_run_count(row),
            "current_qualifying_run_count": current_qualifying_run_count(row),
            "analysis_ready": analysis_ready,
            "coverage_confirmed": coverage_confirmed,
            "retry_required": action not in {"already_resolved", "terminal_review"},
            "retry_reason": retry_reasons[0] if retry_reasons else None,
            "retry_reasons": retry_reasons,
            "true_conflict": true_conflict,
            "true_conflict_info": {
                "conflict_types": conflict_types,
                "records": conflict_records,
            },
            "scope_unclear": scope_unclear,
            "historical_api_statuses": history_compact,
            "historical_statuses": historical_statuses,
            "historical_api_errors": historical_errors,
            "terminal_reasons": terminal_reasons,
            "historical_workflow_specific_max_pages": max(workflow_specific_pages or [0]),
            "workflow_specific_hit_page_cap": workflow_specific_cap,
            "current_query_endpoint_type": evidence.get("query_endpoint_type"),
            "current_query_page_cap": is_page_cap(evidence),
            "workflow_plan_status": action,
            "source_files": target_sources,
        }
        by_repository[rid].append(target)

    if len(workflow_rows) != 0:
        raise AssertionError("workflow rows must be built from repository groups")

    # Build one repository row per retry repository.  The nested target list is
    # sorted and all derived lists are normalized before serialization.
    repository_rows: List[Dict[str, Any]] = []
    flat_rows: List[Dict[str, Any]] = []
    access_rows: List[Dict[str, Any]] = []
    terminal_rows: List[Dict[str, Any]] = []
    resolved_rows: List[Dict[str, Any]] = []
    endpoint_repo_counts = Counter()
    endpoint_workflow_counts = Counter()
    request_min_total = 0
    request_max_total = 0
    request_by_status: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"repositories": 0, "workflows": 0, "estimated_min_requests": 0, "estimated_max_requests": 0}
    )

    for rid in sorted(by_repository, key=sort_id):
        targets = sorted(by_repository[rid], key=lambda item: item["workflow_path"])
        actions = [str(target["workflow_plan_status"]) for target in targets]
        top_status = choose_plan_status(actions)
        for target in targets:
            target["repository_plan_status"] = top_status

        repository = repositories.get(rid, {})
        repo_name = str(
            repository.get("repository_name")
            or targets[0].get("repository_name")
            or ""
        )
        default_branch = str(
            repository.get("default_branch")
            or targets[0].get("default_branch")
            or ""
        )
        missing_ids = sum(not target.get("workflow_id") for target in targets)
        all_ids_known = missing_ids == 0
        repo_history = repository_events.get(rid, [])
        scope_types = set(repo_scopes.get(rid, set()))
        scope_types.update(
            event.get("scope_type")
            for target in targets
            for event in target.get("historical_api_statuses", [])
            if event.get("scope_type")
        )
        historical_max_pages = max(
            [numeric_pages(event) for event in repo_history]
            + [int(target.get("historical_workflow_specific_max_pages") or 0) for target in targets]
            + [0]
        )
        history_hit_cap = any(is_page_cap(event) for event in repo_history)
        history_hit_cap = history_hit_cap or any(
            target.get("workflow_specific_hit_page_cap") for target in targets
        )
        history_hit_cap = history_hit_cap or any(
            target.get("current_query_page_cap")
            and target.get("current_query_endpoint_type")
            in {"repository_wide_unfiltered", "repository_wide_path_filtered"}
            for target in targets
        )
        # A current repository-wide/path-filtered max-page evidence is a
        # repository-wide cap even when the historical status row is absent.
        for target in targets:
            for event in target.get("historical_api_statuses", []):
                if event.get("scope_type") in {
                    "repository_wide_unfiltered",
                    "repository_wide_path_filtered",
                } and is_page_cap(event):
                    history_hit_cap = True

        endpoint = make_endpoint_plan(
            top_level_status=top_status,
            targets=targets,
            historical_max_pages=historical_max_pages,
            history_hit_page_cap=history_hit_cap,
            all_workflow_ids_known=all_ids_known,
            historical_scope_types=sorted(scope_types),
        )
        if endpoint["primary_endpoint_strategy"]:
            if "workflow-specific" in endpoint["primary_endpoint_strategy"]:
                endpoint_repo_counts["workflow_specific"] += 1
                endpoint_workflow_counts["workflow_specific"] += len(targets)
            else:
                endpoint_repo_counts["repository_wide"] += 1
                endpoint_workflow_counts["repository_wide"] += len(targets)

        for target in targets:
            target.update(
                {
                    "primary_endpoint_strategy": endpoint["primary_endpoint_strategy"],
                    "fallback_endpoint_strategy": endpoint["fallback_endpoint_strategy"],
                    "fallback_trigger": endpoint["fallback_trigger"],
                    "estimated_min_requests": endpoint["estimated_min_requests"],
                    "estimated_max_requests": endpoint["estimated_max_requests"],
                    "historical_max_pages": endpoint["historical_max_pages"],
                }
            )
            flat_rows.append(dict(target))
            if target["workflow_plan_status"] == "access_probe_required":
                access_rows.append(dict(target))
            elif target["workflow_plan_status"] == "terminal_review":
                terminal_rows.append(dict(target))
            elif target["workflow_plan_status"] == "already_resolved":
                resolved_rows.append(dict(target))

        repo_sources = list(common_refs) + repo_history_files.get(rid, [])
        repo_sources = dedup_file_refs(repo_sources)
        historical_statuses = sorted(
            {
                str(event.get("status"))
                for event in repo_history
                if event.get("status") not in (None, "")
            }
        )
        historical_errors = sorted(
            {
                str(event.get("error"))
                for event in repo_history
                if event.get("error") not in (None, "")
            }
        )
        true_conflict_targets = [target for target in targets if target["true_conflict"]]
        repo_row = {
            "repository_id": rid,
            "repository_name": repo_name,
            "default_branch": default_branch,
            "cutoff": DEFAULT_CUTOFF,
            "query_start": DEFAULT_QUERY_START,
            "query_window_days": DEFAULT_WINDOW_DAYS,
            "completion_criteria": COMPLETION_CRITERIA,
            "plan_status": top_status,
            "target_workflow_count": len(targets),
            "target_workflow_keys": [target["workflow_key"] for target in targets],
            "workflows": targets,
            "workflow_ids_complete": all_ids_known,
            "missing_workflow_id_count": missing_ids,
            "true_conflict_workflow_count": len(true_conflict_targets),
            "historical_api_statuses": sorted(
                [compact_history_event(event) for event in repo_history],
                key=stable_json,
            ),
            "historical_statuses": historical_statuses,
            "historical_api_errors": historical_errors,
            "historical_scope_types": sorted(scope_types),
            "repository_history_source_count": len(repo_history),
            **endpoint,
            "source_files": repo_sources,
        }
        repository_rows.append(repo_row)

        if top_status in {"fetch_required", "access_probe_required"}:
            request_min_total += int(endpoint["estimated_min_requests"])
            request_max_total += int(endpoint["estimated_max_requests"])
            bucket = request_by_status[top_status]
            bucket["repositories"] += 1
            bucket["workflows"] += len(targets)
            bucket["estimated_min_requests"] += int(endpoint["estimated_min_requests"])
            bucket["estimated_max_requests"] += int(endpoint["estimated_max_requests"])

    repository_rows.sort(key=lambda row: sort_id(row["repository_id"]))
    flat_rows.sort(key=lambda row: (sort_id(row["repository_id"]), row["workflow_path"]))
    access_rows.sort(key=lambda row: (sort_id(row["repository_id"]), row["workflow_path"]))
    terminal_rows.sort(key=lambda row: (sort_id(row["repository_id"]), row["workflow_path"]))
    resolved_rows.sort(key=lambda row: (sort_id(row["repository_id"]), row["workflow_path"]))

    retry_repository_count = len(repository_rows)
    retry_workflow_count = len(flat_rows)
    if retry_workflow_count != len(retry_rows):
        workflow_validation_errors.append(
            f"retry row count {len(retry_rows)} != plan row count {retry_workflow_count}"
        )
    if len({row["workflow_key"] for row in flat_rows}) != retry_workflow_count:
        workflow_validation_errors.append("flattened workflow plan contains duplicate workflow keys")
    if len({row["repository_id"] for row in repository_rows}) != retry_repository_count:
        workflow_validation_errors.append("repository plan contains duplicate repository IDs")
    nested_workflow_keys = {
        target["workflow_key"]
        for repo in repository_rows
        for target in repo["workflows"]
    }
    for row in flat_rows:
        if row["workflow_key"] not in nested_workflow_keys:
            workflow_validation_errors.append(f"orphan workflow row {row['workflow_key']}")

    summary = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_rules_version": PLAN_RULES_VERSION,
        "stage": "phase2.0_repository_unified_acquisition_plan",
        "fixed_cutoff": DEFAULT_CUTOFF,
        "fixed_query_start": DEFAULT_QUERY_START,
        "window_days": DEFAULT_WINDOW_DAYS,
        "completion_criteria": COMPLETION_CRITERIA,
        "randomness": {"random_sampling": False, "seed": None},
        "external_fetch_performed": False,
        "canonical_inputs_modified": False,
        "statistical_models_rerun": False,
        "retry_workflows": retry_workflow_count,
        "retry_repositories": retry_repository_count,
        "repository_plan_status_counts": dict(
            sorted(Counter(row["plan_status"] for row in repository_rows).items())
        ),
        "workflow_plan_status_counts": dict(
            sorted(Counter(row["workflow_plan_status"] for row in flat_rows).items())
        ),
        "fetch_required_repositories": sum(
            row["plan_status"] == "fetch_required" for row in repository_rows
        ),
        "fetch_required_workflows": sum(
            row["workflow_plan_status"] == "fetch_required" for row in flat_rows
        ),
        "access_probe_repositories": sum(
            row["plan_status"] == "access_probe_required" for row in repository_rows
        ),
        "access_probe_workflows": len(access_rows),
        "terminal_review_repositories": sum(
            row["plan_status"] == "terminal_review" for row in repository_rows
        ),
        "terminal_review_workflows": len(terminal_rows),
        "already_resolved_repositories": sum(
            row["plan_status"] == "already_resolved" for row in repository_rows
        ),
        "already_resolved_workflows": len(resolved_rows),
        "repository_wide_plan_repositories": endpoint_repo_counts["repository_wide"],
        "workflow_specific_plan_repositories": endpoint_repo_counts["workflow_specific"],
        "repository_wide_plan_workflows": endpoint_workflow_counts["repository_wide"],
        "workflow_specific_plan_workflows": endpoint_workflow_counts["workflow_specific"],
        "estimated_min_requests": request_min_total,
        "estimated_max_requests": request_max_total,
        "estimated_requests_by_top_level_status": {
            key: request_by_status[key]
            for key in sorted(request_by_status)
        },
        "missing_workflow_id_targets": sum(
            not row.get("workflow_id") for row in flat_rows
        ),
        "true_conflict_requery_workflows": sum(
            bool(row.get("true_conflict")) for row in flat_rows
        ),
        "scope_unclear_workflows": sum(
            bool(row.get("scope_unclear")) for row in flat_rows
        ),
        "canonical_context": canonical_stats,
        "coverage_summary_input_stage": coverage_summary.get("audit_stage"),
        "coverage_summary_input_retry_workflows": coverage_summary.get(
            "retry_target_workflows"
        )
        or coverage_summary.get("still_unconfirmed_workflows"),
        "coverage_summary_input_retry_repositories": coverage_summary.get(
            "retry_target_repositories"
        )
        or coverage_summary.get("still_unconfirmed_repositories"),
        "source_directories": sorted(
            source_label(path.resolve()) for path in resolved_sources
        ),
        "source_descriptor_count": len(source_descriptors),
        "validation": {
            "all_retry_workflows_exactly_once": retry_workflow_count == len(retry_rows)
            and len({row["workflow_key"] for row in flat_rows}) == retry_workflow_count,
            "all_retry_repositories_exactly_once": retry_repository_count
            == len({row["repository_id"] for row in repository_rows}),
            "repository_workflow_references_consistent": not workflow_validation_errors,
            "no_illegal_workflow_endpoint_for_missing_id": all(
                row.get("workflow_id")
                or "workflow-specific" not in str(row.get("primary_endpoint_strategy") or "")
                for row in flat_rows
            ),
            "repository_workflow_endpoint_modes_are_mutually_exclusive": all(
                not (
                    "repository-wide" in str(row.get("primary_endpoint_strategy") or "")
                    and "workflow-specific" in str(row.get("primary_endpoint_strategy") or "")
                )
                for row in repository_rows
            ),
            "errors": workflow_validation_errors,
        },
    }

    # Output rows first.  The manifest is written last and excludes its own
    # hash, so repeated runs are byte-identical without a self-referential hash.
    output_rows = {
        "rq2_acquisition_plan_repositories.jsonl": repository_rows,
        "rq2_acquisition_plan_workflows.jsonl": flat_rows,
        "rq2_access_probe_targets.jsonl": access_rows,
        "rq2_terminal_review_targets.jsonl": terminal_rows,
        "rq2_already_resolved_anomalies.jsonl": resolved_rows,
    }
    for filename, rows in output_rows.items():
        write_jsonl(output_dir / filename, rows)
    write_json(output_dir / "rq2_acquisition_plan_summary.json", summary)

    output_hashes: Dict[str, str] = {}
    for filename in sorted(output_rows):
        path = output_dir / filename
        output_hashes[filename] = sha256_file(path)
    summary_path = output_dir / "rq2_acquisition_plan_summary.json"
    output_hashes[summary_path.name] = sha256_file(summary_path)

    manifest = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_rules_version": PLAN_RULES_VERSION,
        "stage": "phase2.0_repository_unified_acquisition_plan",
        "fixed_cutoff": DEFAULT_CUTOFF,
        "fixed_query_start": DEFAULT_QUERY_START,
        "window_days": DEFAULT_WINDOW_DAYS,
        "completion_criteria": COMPLETION_CRITERIA,
        "randomness": {"random_sampling": False, "seed": None},
        "external_fetch_performed": False,
        "canonical_inputs_modified": False,
        "statistical_models_rerun": False,
        "input_files": manifest_refs,
        "source_directories": sorted(source_label(path.resolve()) for path in resolved_sources),
        "output_files": [
            {"path": name, "sha256": output_hashes[name]}
            for name in sorted(output_hashes)
        ],
        "manifest_self_hash": "excluded_from_manifest_self_reference",
        "counts": {
            "retry_workflows": retry_workflow_count,
            "retry_repositories": retry_repository_count,
            "fetch_required_repositories": summary["fetch_required_repositories"],
            "access_probe_repositories": summary["access_probe_repositories"],
            "terminal_review_repositories": summary["terminal_review_repositories"],
            "already_resolved_repositories": summary["already_resolved_repositories"],
        },
    }
    write_json(output_dir / "rq2_acquisition_plan_manifest.json", manifest)
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-targets", type=Path, default=DEFAULT_RETRY_TARGETS)
    parser.add_argument("--coverage-evidence", type=Path, default=DEFAULT_COVERAGE_EVIDENCE)
    parser.add_argument("--coverage-conflicts", type=Path, default=DEFAULT_COVERAGE_CONFLICTS)
    parser.add_argument("--coverage-summary", type=Path, default=DEFAULT_COVERAGE_SUMMARY)
    parser.add_argument("--repositories", type=Path, default=DEFAULT_REPOSITORIES)
    parser.add_argument("--workflow-snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--workflow-runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--source-dir", type=Path, action="append", dest="source_dirs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    source_dirs = args.source_dirs if args.source_dirs else DEFAULT_SOURCE_DIRS
    summary = build_plan(
        retry_targets_path=args.retry_targets,
        coverage_evidence_path=args.coverage_evidence,
        coverage_conflicts_path=args.coverage_conflicts,
        coverage_summary_path=args.coverage_summary,
        repositories_path=args.repositories,
        snapshots_path=args.workflow_snapshots,
        runs_path=args.workflow_runs,
        source_dirs=source_dirs,
        output_dir=args.output_dir,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
