"""Fetch workflow YAML contents for validated pre-cutoff commits.

Only snapshot-repair targets produced by
``build_rq2_snapshot_repair_targets.py`` are accepted.  This supplements the
stored post-cutoff YAML with the actual blob at the latest path-changing
commit on or before the fixed RQ2 cutoff.  It does not collect workflow runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.supplement_rq2_latest_version_history_graphql import (  # noqa: E402
    GraphQLClient,
    classify_field_error,
    error_text,
    load_jsonl,
    parse_timestamp,
    timestamp_text,
    workflow_key,
    split_repository,
)


DEFAULT_TARGETS = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_snapshot_repair_targets"
    / "mismatch_targets.jsonl"
)
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_snapshot_repair_contents_graphql"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"


def build_query(targets: list[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    definitions: list[str] = []
    variables: Dict[str, Any] = {}
    fields: list[str] = []
    for index, target in enumerate(targets):
        repository = split_repository(target.get("repository_name"))
        if repository is None:
            raise ValueError(f"Invalid repository: {target.get('repository_name')}")
        definitions.extend([
            f"$owner{index}: String!",
            f"$name{index}: String!",
            f"$expression{index}: String!",
        ])
        variables[f"owner{index}"], variables[f"name{index}"] = repository
        variables[f"expression{index}"] = (
            f"{target.get('cutoff_commit_sha')}:{target.get('workflow_path')}"
        )
        fields.append(
            f"""b{index}: repository(owner: $owner{index}, name: $name{index}) {{
                object(expression: $expression{index}) {{
                    ... on Blob {{ oid text }}
                }}
            }}"""
        )
    fields.append("rateLimit { cost remaining resetAt }")
    return (
        "query(" + ", ".join(definitions) + ") { " + " ".join(fields) + " }",
        variables,
    )


def common_status(target: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "repository_name": target.get("repository_name"),
        "workflow_path": target.get("workflow_path"),
        "workflow_id": target.get("workflow_id"),
        "default_branch": target.get("default_branch"),
        "cutoff_commit_sha": target.get("cutoff_commit_sha"),
        "cutoff_commit_date": target.get("cutoff_commit_date"),
        "stored_snapshot_blob_sha": target.get("stored_snapshot_blob_sha"),
        "stored_content_file": target.get("content_file"),
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def parse_result(
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    alias: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    base = common_status(target)
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get(alias) if isinstance(data, dict) else None
    obj = repository.get("object") if isinstance(repository, dict) else None
    blob_sha = str(obj.get("oid") or "").strip() if isinstance(obj, dict) else ""
    text = obj.get("text") if isinstance(obj, dict) else None
    if not blob_sha or not isinstance(text, str):
        message = error_text(payload.get("errors"))
        status = classify_field_error(message) if message else "error"
        return {
            **base,
            "status": status,
            "error": message or "cutoff workflow blob/text unavailable",
        }, None
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result = {
        **base,
        "status": "ok",
        "blob_sha": blob_sha,
        "content_hash": content_hash,
        "content": text,
    }
    return result, result


def load_completed(path: Path) -> set[Tuple[str, str]]:
    completed = set()
    if not path.exists():
        return completed
    for record in load_jsonl(path):
        if record.get("status") not in {"ok", "not_found", "forbidden"}:
            continue
        key = workflow_key(record)
        if key is not None:
            completed.add(key)
    return completed


def fetch(targets: list[Dict[str, Any]], output_dir: Path, batch_size: int, resume: bool) -> Dict[str, Any]:
    from scripts.supplement_workflow_snapshots import load_token

    token = load_token(REPOSITORY_ROOT)
    if not token:
        raise RuntimeError("未找到 GitHub Token，请检查 GITHUB_TOKEN 或 config/.env")
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / "workflow_cutoff_content_status.jsonl"
    content_path = output_dir / "workflow_cutoff_contents.jsonl"
    completed = load_completed(status_path) if resume else set()
    pending = [target for target in targets if workflow_key(target) not in completed]
    client = GraphQLClient(token)
    stats: Counter = Counter({"requested": len(targets), "resumed": len(targets) - len(pending), "batches": 0})
    with status_path.open("a", encoding="utf-8", newline="\n") as status_handle, content_path.open("a", encoding="utf-8", newline="\n") as content_handle:
        for offset in range(0, len(pending), max(1, batch_size)):
            batch = pending[offset:offset + max(1, batch_size)]
            stats["batches"] += 1
            query, variables = build_query(batch)
            payload = client.request(query, variables)
            statuses: list[Dict[str, Any]] = []
            content_records: list[Dict[str, Any]] = []
            for index, target in enumerate(batch):
                status, content_record = parse_result(target, payload, f"b{index}")
                statuses.append(status)
                if content_record is not None:
                    content_records.append(content_record)
            statuses.sort(key=lambda value: (str(value.get("repository_name") or ""), str(value.get("workflow_path") or "")))
            content_by_key = {workflow_key(record): record for record in content_records}
            for status in statuses:
                status_handle.write(json.dumps({key: value for key, value in status.items() if key != "content"}, ensure_ascii=False) + "\n")
                record = content_by_key.get(workflow_key(status))
                if record is not None:
                    content_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                stats[f"status_{status.get('status', 'error')}"] += 1
            status_handle.flush()
            content_handle.flush()
            if stats["batches"] % 10 == 0 or offset + len(batch) == len(pending):
                print(
                    f"GraphQL补抓截止日工作流内容 {offset + len(batch)}/{len(pending)}: "
                    f"有效 {stats['status_ok']}, 错误 {stats['status_error']}, "
                    f"API {client.api_calls}, cost {client.total_cost}",
                    flush=True,
                )
    stats["pending"] = len(pending)
    stats["api_calls"] = client.api_calls
    stats["graphql_total_cost"] = client.total_cost
    stats["last_rate_limit"] = client.last_rate_limit
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch validated RQ2 cutoff workflow YAML contents")
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    targets = list(load_jsonl(args.targets))
    start = max(0, args.offset)
    selected = targets[start:] if args.limit is None else targets[start:start + max(0, args.limit)]
    print(json.dumps({
        "targets_total": len(targets),
        "offset": start,
        "targets_selected": len(selected),
        "batch_size": max(1, args.batch_size),
        "output_dir": str(args.output_dir),
    }, ensure_ascii=False, indent=2))
    stats = fetch(selected, args.output_dir, max(1, args.batch_size), resume=not args.no_resume)
    metadata = {
        "targets": str(args.targets),
        "output_dir": str(args.output_dir),
        "batch_size": max(1, args.batch_size),
        "targets_total": len(targets),
        "offset": start,
        "targets_selected": len(selected),
        "external_fetch_performed": True,
        **stats,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "cutoff_content_supplement_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
