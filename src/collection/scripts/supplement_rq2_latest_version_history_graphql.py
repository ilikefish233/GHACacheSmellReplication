"""Supplement only missing RQ2 workflow-version boundaries through GraphQL.

The REST history collector is intentionally kept for reproducibility, but one
request per workflow is unnecessarily slow for this targeted supplement.  This
module batches independent repository/path history queries and only performs a
second blob lookup when the existing cutoff snapshot cannot prove that the
current content was already in effect.

It consumes the target list produced by
``supplement_rq2_latest_version_history.py --dry-run``.  It does not collect
run records and it never changes the input files.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import requests


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.supplement_rq2_latest_version_history import (  # noqa: E402
    parse_timestamp,
    timestamp_text,
    workflow_key,
)
from scripts.supplement_workflow_snapshots import load_token  # noqa: E402


DEFAULT_TARGETS = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_history_targets_final"
    / "history_targets.jsonl"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_history_graphql_final"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

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


def load_completed_keys(path: Path) -> set[WorkflowKey]:
    completed: set[WorkflowKey] = set()
    if not path.exists():
        return completed
    for record in load_jsonl(path):
        if str(record.get("status") or "") not in {
            "ok",
            "current_snapshot_mismatch",
            "no_commits_before_cutoff",
            "not_found",
            "forbidden",
        }:
            continue
        key = workflow_key(record)
        if key is not None:
            completed.add(key)
    return completed


def split_repository(value: Any) -> Optional[Tuple[str, str]]:
    parts = str(value or "").strip().split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def qualified_branch(value: Any) -> str:
    branch = str(value or "main").strip()
    if branch.startswith("refs/heads/"):
        return branch
    return f"refs/heads/{branch}"


def build_history_query(targets: Sequence[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Build one GraphQL query containing one history field per target."""

    variable_defs = ["$until: GitTimestamp!"]
    variables: Dict[str, Any] = {"until": None}
    fields: list[str] = []
    for index, target in enumerate(targets):
        alias = f"w{index}"
        variable_defs.extend([
            f"$owner{index}: String!",
            f"$name{index}: String!",
            f"$path{index}: String!",
        ])
        repository = split_repository(target.get("repository_name"))
        if repository is None:
            raise ValueError(f"Invalid repository name: {target.get('repository_name')}")
        variables[f"owner{index}"] = repository[0]
        variables[f"name{index}"] = repository[1]
        variables[f"path{index}"] = str(target.get("workflow_path") or "")
        branch = str(target.get("default_branch") or "").strip()
        if branch:
            variable_defs.append(f"$ref{index}: String!")
            variables[f"ref{index}"] = qualified_branch(branch)
            ref_selector = f"ref(qualifiedName: $ref{index})"
        else:
            # Some current workflow identities have no local repo-info record.
            # Resolve the repository's actual default branch through GitHub's
            # defaultBranchRef instead of guessing ``main``.
            ref_selector = "defaultBranchRef"
        fields.append(
            f"""{alias}: repository(owner: $owner{index}, name: $name{index}) {{
                {ref_selector} {{
                    target {{
                        ... on Commit {{
                            history(first: 1, path: $path{index}, until: $until) {{
                                nodes {{ oid committedDate }}
                            }}
                        }}
                    }}
                }}
            }}"""
        )
    fields.append("rateLimit { cost remaining resetAt }")
    query = "query(" + ", ".join(variable_defs) + ") { " + " ".join(fields) + " }"
    return query, variables


def build_blob_query(targets: Sequence[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Build one GraphQL query that resolves each commit:path blob OID."""

    variable_defs: list[str] = []
    variables: Dict[str, Any] = {}
    fields: list[str] = []
    for index, target in enumerate(targets):
        alias = f"b{index}"
        variable_defs.extend([
            f"$owner{index}: String!",
            f"$name{index}: String!",
            f"$expression{index}: String!",
        ])
        repository = split_repository(target.get("repository_name"))
        if repository is None:
            raise ValueError(f"Invalid repository name: {target.get('repository_name')}")
        variables[f"owner{index}"], variables[f"name{index}"] = repository
        variables[f"expression{index}"] = (
            f"{target['commit_sha']}:{target['workflow_path']}"
        )
        fields.append(
            f"""{alias}: repository(owner: $owner{index}, name: $name{index}) {{
                object(expression: $expression{index}) {{
                    ... on Blob {{ oid }}
                }}
            }}"""
        )
    fields.append("rateLimit { cost remaining resetAt }")
    query = "query(" + ", ".join(variable_defs) + ") { " + " ".join(fields) + " }"
    return query, variables


class GraphQLClient:
    def __init__(self, token: str, timeout: int = 60, retries: int = 4):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "gha-log-analysis-rq2-history/1.0",
        }
        self.timeout = timeout
        self.retries = retries
        self.api_calls = 0
        self.total_cost = 0
        self.last_rate_limit: Dict[str, Any] = {}

    def request(self, query: str, variables: Mapping[str, Any]) -> Dict[str, Any]:
        payload = {"query": query, "variables": dict(variables)}
        last_error: Optional[str] = None
        for attempt in range(self.retries + 1):
            self.api_calls += 1
            try:
                response = requests.post(
                    GRAPHQL_ENDPOINT,
                    headers=self.headers,
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise RuntimeError(last_error) from exc

            if response.status_code in {429, 502, 503, 504}:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if attempt < self.retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(float(retry_after), 60.0) if retry_after else min(2 ** attempt, 30)
                    except ValueError:
                        delay = min(2 ** attempt, 30)
                    time.sleep(delay)
                    continue
                raise RuntimeError(last_error)
            if response.status_code in {401, 403}:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            try:
                payload_value = response.json()
            except ValueError as exc:
                raise RuntimeError("GraphQL returned invalid JSON") from exc
            if not isinstance(payload_value, dict):
                raise RuntimeError("GraphQL returned a non-object response")
            rate = (payload_value.get("data") or {}).get("rateLimit") or {}
            if isinstance(rate, dict):
                self.last_rate_limit = rate
                try:
                    self.total_cost += int(rate.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
            return payload_value
        raise RuntimeError(last_error or "GraphQL request failed")


def error_text(errors: Any) -> str:
    if not isinstance(errors, list):
        return ""
    parts = []
    for error in errors:
        if isinstance(error, dict):
            parts.append(str(error.get("message") or ""))
    return "; ".join(part for part in parts if part)


def classify_field_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("resource not accessible", "forbidden", "requires authentication")):
        return "forbidden"
    if any(token in lowered for token in ("could not resolve", "not found", "unknown repository")):
        return "not_found"
    return "error"


def common_status(target: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "repository_name": target.get("repository_name"),
        "workflow_path": target.get("workflow_path"),
        "workflow_id": target.get("workflow_id"),
        "current_content_hash": target.get("content_hash"),
        "current_file_sha": target.get("file_sha"),
        "current_content_file": target.get("content_file"),
        "target_reason": target.get("reason"),
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def history_status(
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    alias: str,
    cutoff: datetime,
    time_only: bool = False,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Convert one history field into a status and optional blob-verification task."""

    base = common_status(target)
    data = payload.get("data") if isinstance(payload, dict) else None
    node = data.get(alias) if isinstance(data, dict) else None
    if node is None:
        message = error_text(payload.get("errors"))
        status = classify_field_error(message)
        return {
            **base,
            "status": status,
            "error": message or "repository/ref is unavailable",
        }, None
    ref = None
    if isinstance(node, dict):
        ref = node.get("ref") or node.get("defaultBranchRef")
    target_node = ref.get("target") if isinstance(ref, dict) else None
    history = target_node.get("history") if isinstance(target_node, dict) else None
    nodes = history.get("nodes") if isinstance(history, dict) else None
    if not isinstance(nodes, list) or not nodes:
        return {**base, "status": "no_commits_before_cutoff"}, None
    commit = nodes[0] if isinstance(nodes[0], dict) else {}
    commit_sha = str(commit.get("oid") or "").strip()
    commit_date = parse_timestamp(commit.get("committedDate"))
    if not commit_sha or commit_date is None:
        return {
            **base,
            "status": "error",
            "error": "commit metadata incomplete",
        }, None
    if commit_date > cutoff:
        return {
            **base,
            "status": "error",
            "error": "GraphQL returned a commit after the cutoff",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
        }, None

    if time_only:
        return {
            **base,
            "status": "ok",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
            "content_hash": target.get("content_hash"),
            "content_file": target.get("content_file"),
            "verification": "latest_commit_time_only",
        }, None

    snapshot_date = parse_timestamp(target.get("snapshot_commit_date"))
    snapshot_sha = str(target.get("snapshot_commit_sha") or "").strip()
    if snapshot_sha and snapshot_date is not None and snapshot_date <= cutoff and commit_date <= snapshot_date:
        return {
            **base,
            "status": "ok",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
            "content_hash": target.get("content_hash"),
            "content_file": target.get("content_file"),
            "verification": "cutoff_snapshot_blob_transitive",
        }, None

    task = {
        **dict(target),
        "commit_sha": commit_sha,
        "commit_date": timestamp_text(commit_date),
    }
    return {
        **base,
        "status": "pending_blob_verification",
        "commit_sha": commit_sha,
        "commit_date": timestamp_text(commit_date),
    }, task


def verify_blob_status(
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    alias: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    base = common_status(target)
    history_record = {
        **base,
        "commit_sha": target.get("commit_sha"),
        "commit_date": target.get("commit_date"),
    }
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get(alias) if isinstance(data, dict) else None
    obj = repository.get("object") if isinstance(repository, dict) else None
    blob_sha = str(obj.get("oid") or "").strip() if isinstance(obj, dict) else ""
    expected = str(target.get("file_sha") or "").strip()
    if not expected:
        return {
            **history_record,
            "status": "error",
            "error": "current workflow snapshot has no file_sha for blob verification",
        }, None
    if blob_sha and blob_sha == expected:
        result = {
            **history_record,
            "status": "ok",
            "content_hash": target.get("content_hash"),
            "content_file": target.get("content_file"),
            "verified_blob_sha": blob_sha,
            "verification": "graphql_blob_at_commit",
        }
        return result, result
    message = error_text(payload.get("errors"))
    if not blob_sha and message:
        status = classify_field_error(message)
        return {
            **history_record,
            "status": status,
            "error": message,
        }, None
    return {
        **history_record,
        "status": "current_snapshot_mismatch",
        "verified_blob_sha": blob_sha,
        "error": "current snapshot is not the workflow content at the cutoff",
    }, None


def history_record_from_status(status: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "_id": (
            f"{status['repository_name']}_{status['workflow_path']}_"
            f"{str(status['commit_sha'])[:12]}"
        ),
        "repository_name": status["repository_name"],
        "workflow_path": status["workflow_path"],
        "workflow_id": status.get("workflow_id"),
        "commit_sha": status["commit_sha"],
        "commit_date": status["commit_date"],
        "content_hash": status.get("content_hash") or status.get("current_content_hash"),
        "content_file": status.get("content_file") or status.get("current_content_file"),
        "prev_content_hash": None,
        "is_change": True,
        "source": "github_latest_workflow_change_at_cutoff_graphql",
        "verification": status.get("verification"),
        "fetched_at": status.get("fetched_at"),
    }


def fetch(
    targets: Sequence[Dict[str, Any]],
    output_dir: Path,
    cutoff: datetime,
    batch_size: int,
    resume: bool,
    time_only: bool = False,
) -> Dict[str, Any]:
    token = load_token(REPOSITORY_ROOT)
    if not token:
        raise RuntimeError("未找到 GitHub Token，请检查 GITHUB_TOKEN 或 config/.env")
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / "workflow_history_fetch_status.jsonl"
    record_path = output_dir / "workflow_changes.jsonl"
    completed = load_completed_keys(status_path) if resume else set()
    pending = [target for target in targets if workflow_key(target) not in completed]
    client = GraphQLClient(token)
    stats: Counter = Counter({
        "requested": len(targets),
        "resumed": len(targets) - len(pending),
        "batches": 0,
        "history_api_calls": 0,
        "blob_api_calls": 0,
    })

    with status_path.open("a", encoding="utf-8", newline="\n") as status_handle, \
            record_path.open("a", encoding="utf-8", newline="\n") as record_handle:
        for offset in range(0, len(pending), max(1, batch_size)):
            batch = pending[offset:offset + max(1, batch_size)]
            stats["batches"] += 1
            query, variables = build_history_query(batch)
            variables["until"] = timestamp_text(cutoff)
            try:
                payload = client.request(query, variables)
            except RuntimeError as exc:
                # A transient 5xx response should not discard all progress.
                # Emit retryable error rows for this batch; ``load_completed_keys``
                # deliberately does not treat ``error`` as terminal, so a later
                # invocation resumes exactly from these targets.
                error_text_value = str(exc)
                for target in batch:
                    status_handle.write(json.dumps({
                        **common_status(target),
                        "status": "error",
                        "error": error_text_value,
                        "retryable": True,
                    }, ensure_ascii=False) + "\n")
                    stats["status_error"] += 1
                status_handle.flush()
                print(
                    f"GraphQL批次失败，已标记可重试 {offset + len(batch)}/{len(pending)}: "
                    f"{error_text_value[:180]}",
                    flush=True,
                )
                time.sleep(2)
                continue
            stats["history_api_calls"] += 1
            blob_tasks: list[Dict[str, Any]] = []
            statuses: list[Dict[str, Any]] = []
            for index, target in enumerate(batch):
                status, task = history_status(
                    target,
                    payload,
                    f"w{index}",
                    cutoff,
                    time_only=time_only,
                )
                if task is not None:
                    blob_tasks.append(task)
                else:
                    statuses.append(status)

            if blob_tasks:
                blob_query, blob_variables = build_blob_query(blob_tasks)
                blob_payload = client.request(blob_query, blob_variables)
                stats["blob_api_calls"] += 1
                for index, task in enumerate(blob_tasks):
                    status, record = verify_blob_status(task, blob_payload, f"b{index}")
                    statuses.append(status)
                    if record is not None:
                        record_handle.write(json.dumps(history_record_from_status(record), ensure_ascii=False) + "\n")

            # Sort only the emitted status rows so output remains easy to audit.
            statuses.sort(key=lambda value: (str(value.get("repository_name") or ""), str(value.get("workflow_path") or "")))
            for status in statuses:
                status_handle.write(json.dumps(status, ensure_ascii=False) + "\n")
                if status.get("status") == "ok" and not any(
                    str(status.get("verification") or "").startswith(prefix)
                    for prefix in ("graphql_blob_at_commit",)
                ):
                    record_handle.write(json.dumps(history_record_from_status(status), ensure_ascii=False) + "\n")
                stats[f"status_{status.get('status', 'error')}"] += 1
            status_handle.flush()
            record_handle.flush()
            if stats["batches"] % 10 == 0 or offset + len(batch) == len(pending):
                print(
                    f"GraphQL补抓工作流边界 {offset + len(batch)}/{len(pending)}: "
                    f"有效 {stats['status_ok']}, 不匹配 {stats['status_current_snapshot_mismatch']}, "
                    f"无提交 {stats['status_no_commits_before_cutoff']}, "
                    f"API {client.api_calls}, cost {client.total_cost}",
                    flush=True,
                )

    stats["pending"] = len(pending)
    stats["time_only"] = bool(time_only)
    stats["api_calls"] = client.api_calls
    stats["graphql_total_cost"] = client.total_cost
    stats["last_rate_limit"] = client.last_rate_limit
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-fetch missing RQ2 workflow history boundaries with GraphQL")
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--time-only",
        action="store_true",
        help=(
            "only retain the latest workflow-file commit time and SHA; "
            "skip blob verification and YAML/content retrieval"
        ),
    )
    args = parser.parse_args()

    cutoff = parse_timestamp(args.cutoff)
    if cutoff is None:
        raise SystemExit(f"Invalid cutoff timestamp: {args.cutoff}")
    targets = list(load_jsonl(args.targets))
    start = max(0, args.offset)
    if args.limit is None:
        selected = targets[start:]
    else:
        selected = targets[start:start + max(0, args.limit)]
    print(json.dumps({
        "target_path": str(args.targets),
        "targets_total": len(targets),
        "offset": start,
        "targets_selected": len(selected),
        "batch_size": max(1, args.batch_size),
        "study_cutoff": timestamp_text(cutoff),
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, indent=2))
    stats = fetch(
        selected,
        args.output_dir,
        cutoff,
        max(1, args.batch_size),
        resume=not args.no_resume,
        time_only=args.time_only,
    )
    metadata = {
        "target_path": str(args.targets),
        "study_cutoff": timestamp_text(cutoff),
        "batch_size": max(1, args.batch_size),
        "targets_total": len(targets),
        "offset": start,
        "targets_selected": len(selected),
        "external_fetch_performed": True,
        "history_match_mode": "latest_commit_time_only" if args.time_only else "commit_time_plus_blob_verification",
        **stats,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "history_supplement_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
