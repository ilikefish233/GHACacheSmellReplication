"""Supplement only the workflow-history boundaries required by RQ2.

RQ2 does not need the complete history of every workflow.  For a current
workflow that has at least one eligible run in the fixed observation window,
we only need the latest commit that changed that workflow file on or before
the study cutoff.  Existing history records are reused; GitHub is queried only
for candidate identities whose effective boundary is missing or whose latest
known hash does not match the current snapshot.

The output is independent of the raw dataset and is safe to resume.  It is
later merged with existing history before the interval matcher is run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
DEFAULT_WORKFLOWS = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_input_merged"
    / "workflow_files_enriched.jsonl"
)
DEFAULT_RUNS = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_input_merged"
    / "workflow_runs.jsonl"
)
DEFAULT_REPOS = REPOSITORY_ROOT / "data_26_6_9_complete_20260609" / "repo_info.jsonl"
DEFAULT_HISTORY = REPOSITORY_ROOT / "data_26_6_9_complete_20260609" / "workflow_changes.jsonl"
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_history_supplement"
)
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_EVENTS = frozenset({"push", "schedule", "workflow_dispatch"})

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


def workflow_key(record: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = str(record.get("repository_name") or "").strip().lower()
    path = str(record.get("workflow_path") or record.get("path") or "").strip()
    if not repository or not path:
        return None
    return repository, path


def load_default_branches(path: Path) -> Dict[str, str]:
    branches: Dict[str, str] = {}
    for record in load_jsonl(path):
        repo = record.get("repo") or {}
        branch = str(repo.get("defaultBranch") or "").strip()
        if not branch:
            continue
        for value in (record.get("_id"), repo.get("name")):
            repository = str(value or "").strip().lower()
            if repository:
                branches.setdefault(repository, branch)
    return branches


def load_current_workflows(path: Path) -> Dict[WorkflowKey, Dict[str, Any]]:
    workflows: Dict[WorkflowKey, Dict[str, Any]] = {}
    for record in load_jsonl(path):
        key = workflow_key(record)
        if key is not None and str(record.get("content_hash") or "").strip():
            workflows[key] = record
    return workflows


def load_history_latest(
    paths: Sequence[Path],
    cutoff: datetime,
) -> Dict[WorkflowKey, Dict[str, Any]]:
    latest: Dict[WorkflowKey, Dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for record in load_jsonl(path):
            key = workflow_key(record)
            commit_date = parse_timestamp(record.get("commit_date"))
            content_hash = str(record.get("content_hash") or "").strip().lower()
            commit_sha = str(record.get("commit_sha") or "").strip()
            if key is None or commit_date is None or commit_date > cutoff or not content_hash or not commit_sha:
                continue
            candidate = {
                **record,
                "content_hash": content_hash,
                "commit_date_parsed": commit_date,
            }
            previous = latest.get(key)
            if previous is None or (
                candidate["commit_date_parsed"],
                str(candidate.get("commit_sha") or ""),
            ) > (
                previous["commit_date_parsed"],
                str(previous.get("commit_sha") or ""),
            ):
                latest[key] = candidate
    return latest


def load_candidate_workflows(
    workflows: Mapping[WorkflowKey, Mapping[str, Any]],
    runs_path: Path,
    branches: Mapping[str, str],
    cutoff: datetime,
    window_days: float,
    events: Sequence[str],
) -> Tuple[Dict[WorkflowKey, Dict[str, Any]], Counter]:
    window_start = cutoff - timedelta(days=window_days)
    allowed_events = {str(value).strip().lower() for value in events if str(value).strip()}
    candidate_ids: Dict[WorkflowKey, set[str]] = defaultdict(set)
    stats: Counter = Counter()
    for record in load_jsonl(runs_path):
        if str(record.get("status") or "").strip().lower() != "completed":
            continue
        key = workflow_key(record)
        if key is None or key not in workflows:
            continue
        created_at = parse_timestamp(record.get("created_at"))
        if created_at is None or not (window_start <= created_at <= cutoff):
            continue
        if str(record.get("head_branch") or "") != branches.get(key[0]):
            continue
        event = str(record.get("event") or "").strip().lower()
        if allowed_events and event not in allowed_events:
            continue
        workflow_id = str(record.get("workflow_id") or "").strip()
        if not workflow_id:
            stats["candidate_runs_missing_workflow_id"] += 1
            continue
        current_id = str(workflows[key].get("workflow_id") or "").strip()
        if current_id and current_id != workflow_id:
            stats["candidate_run_workflow_id_mismatch"] += 1
            continue
        candidate_ids[key].add(workflow_id)
        stats["candidate_runs"] += 1

    candidates: Dict[WorkflowKey, Dict[str, Any]] = {}
    for key, ids in candidate_ids.items():
        if len(ids) != 1:
            stats["ambiguous_candidate_workflow_ids"] += 1
            continue
        record = dict(workflows[key])
        # The current workflow index does not always repeat repository metadata.
        # Carry the branch selected during the candidate-run audit into every
        # target; otherwise the fetcher would silently fall back to ``main``
        # for repositories whose default branch is ``master``/``develop``/etc.
        record["default_branch"] = branches.get(key[0], "")
        if not str(record.get("workflow_id") or "").strip():
            record["workflow_id"] = next(iter(ids))
            record["rq2_workflow_id_source"] = "unique_window_run_id"
        candidates[key] = record
    stats["candidate_workflows"] = len(candidates)
    stats["candidate_repositories"] = len({key[0] for key in candidates})
    return candidates, stats


def choose_targets(
    candidates: Mapping[WorkflowKey, Dict[str, Any]],
    history_latest: Mapping[WorkflowKey, Mapping[str, Any]],
) -> Tuple[list[Dict[str, Any]], Counter]:
    targets: list[Dict[str, Any]] = []
    stats: Counter = Counter()
    for key, current in candidates.items():
        latest = history_latest.get(key)
        if latest is not None and str(latest.get("content_hash") or "").strip().lower() == str(current.get("content_hash") or "").strip().lower():
            stats["history_already_ready"] += 1
            continue
        if latest is None:
            stats["history_missing"] += 1
            reason = "missing_history"
        else:
            stats["history_hash_mismatch"] += 1
            reason = "latest_history_hash_mismatch"
        targets.append({
            "repository_name": key[0],
            "workflow_path": key[1],
            "workflow_id": current.get("workflow_id"),
            "content_hash": str(current.get("content_hash") or "").strip().lower(),
            "file_sha": str(current.get("file_sha") or "").strip(),
            "content_file": str(current.get("content_file") or "").strip(),
            "snapshot_commit_sha": str(current.get("snapshot_commit_sha") or "").strip(),
            "snapshot_commit_date": str(current.get("snapshot_commit_date") or "").strip(),
            "default_branch": str(current.get("default_branch") or "").strip(),
            "reason": reason,
        })
    stats["target_workflows"] = len(targets)
    stats["target_repositories"] = len({str(item["repository_name"]).lower() for item in targets})
    return sorted(targets, key=lambda item: (item["repository_name"], item["workflow_path"])), stats


def decode_contents(payload: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        raw = base64.b64decode(content.replace("\n", ""))
    except (ValueError, TypeError):
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            return text, hashlib.sha256(text.encode("utf-8")).hexdigest()
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def fetch_one(client: Any, target: Mapping[str, Any], cutoff: datetime) -> Dict[str, Any]:
    repository = str(target["repository_name"])
    path = str(target["workflow_path"])
    branch = str(target.get("default_branch") or "main")
    base_url = f"https://api.github.com/repos/{quote(repository, safe='/')}"
    commits = client.get_json(
        f"{base_url}/commits",
        params={
            "path": path,
            "sha": branch,
            "until": timestamp_text(cutoff),
            "per_page": 1,
            "page": 1,
        },
    )
    common = {
        "repository_name": repository,
        "workflow_path": path,
        "workflow_id": target.get("workflow_id"),
        "current_content_hash": target.get("content_hash"),
        "current_file_sha": target.get("file_sha"),
        "current_content_file": target.get("content_file"),
        "target_reason": target.get("reason"),
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not isinstance(commits, list) or not commits:
        return {**common, "status": "no_commits_before_cutoff"}
    commit = commits[0] if isinstance(commits[0], dict) else {}
    commit_sha = str(commit.get("sha") or "").strip()
    commit_data = commit.get("commit") or {}
    committer = commit_data.get("committer") or {}
    author = commit_data.get("author") or {}
    commit_date = parse_timestamp(committer.get("date") or author.get("date"))
    if not commit_sha or commit_date is None:
        return {**common, "status": "error", "error": "commit metadata incomplete"}

    # Supplement snapshots contain the blob SHA at the cutoff commit.  If the
    # latest path-changing commit is not after that snapshot commit, the file
    # content is unchanged between them, so no second API request is needed.
    snapshot_date = parse_timestamp(target.get("snapshot_commit_date"))
    snapshot_sha = str(target.get("snapshot_commit_sha") or "").strip()
    if snapshot_sha and snapshot_date is not None and snapshot_date <= cutoff and commit_date <= snapshot_date:
        return {
            **common,
            "status": "ok",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
            "content_hash": target.get("content_hash"),
            "content_file": target.get("content_file"),
            "verification": "cutoff_snapshot_blob_transitive",
        }

    contents = client.get_json(
        f"{base_url}/contents/{quote(path, safe='/')}",
        params={"ref": commit_sha},
    )
    if not isinstance(contents, dict):
        return {
            **common,
            "status": "error",
            "error": "workflow content unavailable at latest pre-cutoff commit",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
        }
    blob_sha = str(contents.get("sha") or "").strip()
    decoded = decode_contents(contents)
    actual_hash = decoded[1] if decoded is not None else ""
    current_blob_sha = str(target.get("file_sha") or "").strip()
    current_hash = str(target.get("content_hash") or "").strip().lower()
    if (current_blob_sha and blob_sha and current_blob_sha == blob_sha) or (actual_hash and actual_hash == current_hash):
        return {
            **common,
            "status": "ok",
            "commit_sha": commit_sha,
            "commit_date": timestamp_text(commit_date),
            "content_hash": target.get("content_hash"),
            "content_file": target.get("content_file"),
            "verified_blob_sha": blob_sha,
            "verification": "contents_at_commit",
        }
    return {
        **common,
        "status": "current_snapshot_mismatch",
        "commit_sha": commit_sha,
        "commit_date": timestamp_text(commit_date),
        "verified_blob_sha": blob_sha,
        "verified_content_hash": actual_hash,
        "error": "current snapshot is not the workflow content at the cutoff",
    }


def run_fetch(
    targets: Sequence[Dict[str, Any]],
    output_dir: Path,
    cutoff: datetime,
    workers: int,
    resume: bool,
) -> Dict[str, Any]:
    from src.utils.github_client import GitHubClient

    output_dir.mkdir(parents=True, exist_ok=True)
    record_path = output_dir / "workflow_changes.jsonl"
    status_path = output_dir / "workflow_history_fetch_status.jsonl"
    completed = load_completed_keys(status_path) if resume else set()
    pending = [target for target in targets if workflow_key(target) not in completed]
    client = GitHubClient(log_file=output_dir / "github_api.log")
    record_lock = threading.Lock()
    stats: Counter = Counter({"requested": len(targets), "resumed": len(targets) - len(pending)})

    with record_path.open("a", encoding="utf-8", newline="\n") as record_handle, status_path.open("a", encoding="utf-8", newline="\n") as status_handle:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(fetch_one, client, target, cutoff): target
                for target in pending
            }
            for index, future in enumerate(as_completed(futures), start=1):
                target = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve per-target failure
                    result = {
                        "repository_name": target["repository_name"],
                        "workflow_path": target["workflow_path"],
                        "workflow_id": target.get("workflow_id"),
                        "status": "error",
                        "error": str(exc),
                    }
                with record_lock:
                    status_handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                    if result.get("status") == "ok":
                        record_handle.write(json.dumps({
                            "_id": f"{result['repository_name']}_{result['workflow_path']}_{result['commit_sha'][:12]}",
                            "repository_name": result["repository_name"],
                            "workflow_path": result["workflow_path"],
                            "workflow_id": result.get("workflow_id"),
                            "commit_sha": result["commit_sha"],
                            "commit_date": result["commit_date"],
                            "content_hash": result["content_hash"],
                            "content_file": result.get("content_file"),
                            "prev_content_hash": None,
                            "is_change": True,
                            "source": "github_latest_workflow_change_at_cutoff",
                            "verification": result.get("verification"),
                            "fetched_at": result.get("fetched_at"),
                        }, ensure_ascii=False) + "\n")
                    status_handle.flush()
                    record_handle.flush()
                stats[f"status_{result.get('status', 'error')}"] += 1
                if index % 100 == 0 or index == len(pending):
                    print(
                        f"补抓工作流边界 {index}/{len(pending)}: "
                        f"有效 {stats['status_ok']}, 不匹配 {stats['status_current_snapshot_mismatch']}, "
                        f"错误 {stats['status_error']}",
                        flush=True,
                    )
    stats["pending"] = len(pending)
    stats["api_calls"] = client.api_calls
    return dict(stats)


def prepare_targets(
    workflows_path: Path,
    runs_path: Path,
    repos_path: Path,
    history_paths: Sequence[Path],
    cutoff: datetime,
    window_days: float,
    events: Sequence[str],
    output_dir: Path,
) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    workflows = load_current_workflows(workflows_path)
    branches = load_default_branches(repos_path)
    candidates, candidate_stats = load_candidate_workflows(
        workflows, runs_path, branches, cutoff, window_days, events
    )
    history_latest = load_history_latest(history_paths, cutoff)
    targets, target_stats = choose_targets(candidates, history_latest)
    metadata = {
        "workflow_snapshot_source": str(workflows_path),
        "run_source": str(runs_path),
        "repository_source": str(repos_path),
        "history_sources": [str(path) for path in history_paths],
        "study_cutoff": timestamp_text(cutoff),
        "window_days": window_days,
        "window_start": timestamp_text(cutoff - timedelta(days=window_days)),
        "events": sorted(str(value).strip().lower() for value in events if str(value).strip()),
        **dict(candidate_stats),
        "history_records_indexed": len(history_latest),
        **dict(target_stats),
        "selection_rule": (
            "candidate workflow identities have at least one completed default-branch "
            "stable-event run in the fixed window; target only missing or hash-mismatched "
            "latest workflow-change boundaries"
        ),
        "external_fetch_performed": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "history_targets.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for target in targets:
            handle.write(json.dumps(target, ensure_ascii=False) + "\n")
    (output_dir / "history_target_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return targets, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Supplement only missing RQ2 workflow history boundaries")
    parser.add_argument("--workflows", type=Path, default=DEFAULT_WORKFLOWS)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--repos", type=Path, default=DEFAULT_REPOS)
    parser.add_argument("--history", type=Path, action="append", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--window-days", type=float, default=30.0)
    parser.add_argument("--events", default=",".join(sorted(DEFAULT_EVENTS)))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    cutoff = parse_timestamp(args.cutoff)
    if cutoff is None:
        raise SystemExit(f"Invalid cutoff timestamp: {args.cutoff}")
    history_paths = args.history or [DEFAULT_HISTORY]
    events = tuple(value.strip().lower() for value in args.events.split(",") if value.strip())
    targets, metadata = prepare_targets(
        args.workflows,
        args.runs,
        args.repos,
        history_paths,
        cutoff,
        args.window_days,
        events,
        args.output_dir,
    )
    if args.limit is not None:
        targets = targets[:max(0, args.limit)]
        metadata["limit"] = args.limit
        metadata["targets_after_limit"] = len(targets)
        with (args.output_dir / "history_targets.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for target in targets:
                handle.write(json.dumps(target, ensure_ascii=False) + "\n")
        (args.output_dir / "history_target_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("dry-run：未访问 GitHub API")
        return
    fetch_stats = run_fetch(
        targets,
        args.output_dir,
        cutoff,
        max(1, args.workers),
        resume=not args.no_resume,
    )
    metadata.update(fetch_stats)
    metadata["external_fetch_performed"] = True
    (args.output_dir / "history_supplement_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(fetch_stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
