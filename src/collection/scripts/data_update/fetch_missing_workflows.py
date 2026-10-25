"""补抓符合既有筛选标准、但缺少工作流索引的仓库。

本脚本只补抓默认分支在指定时间截点之前的最新工作流 YAML，不重新采集运行记录、
仓库元数据或工作流变更历史。输出写入独立目录，便于审计和后续合并，避免覆盖主数据。

仓库纳入条件与 ``scripts/filter_repositories.py`` 保持一致：

* ``selected == True`` 且 ``processed == True``；
* 非 fork、未归档；
* ``stargazers > 100``、``total_runs_90d > 50``、``commits > 100``；
* 不在现有 ``workflow_files.jsonl`` 的仓库集合中；
* ``nb_workflows > 0``，避免对元数据已明确没有工作流的仓库发起无效请求。

时间一致性：

* 通过默认分支的 commits API 加 ``until`` 参数定位 ``as_of`` 之前的最新提交；
* 从该提交对应的 Git tree 获取 ``.github/workflows/*.yml|yaml``；
* 输出记录的 ``fetched_at`` 使用逻辑快照时间 ``as_of``，实际采集时间另存为
  ``retrieved_at``，供后续 RQ2 匹配和审计使用。

示例：

    python scripts/data_update/fetch_missing_workflows.py --dry-run
    python scripts/data_update/fetch_missing_workflows.py --workers 4
"""

from __future__ import annotations
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[2]


import argparse
import base64
import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import sys

sys.path.insert(0, str(Path(__file__).parents[2]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.utils.checkpoint import CheckpointManager
from src.utils.github_client import GitHubClient
from scripts.data_update.fetch_all import JsonlWriter, WorkflowFileManager


DATA_DIR = Path(str(_PORTABLE_PROJECT_ROOT / 'data_26_6_9'))
DEFAULT_AS_OF = "2026-06-09T23:59:59Z"
DEFAULT_OUTPUT_DIR = DATA_DIR / "supplement_missing_workflows_20260609"


def parse_as_of(value: str) -> datetime:
    """解析 ISO-8601 时间并统一为 UTC。"""
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def jsonl_records(path: Path) -> Iterable[dict]:
    """流式读取 JSONL，单条坏记录不阻塞候选统计。"""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"警告: {path.name}:{line_number} JSON 解析失败: {exc}")


def load_existing_workflow_repositories(path: Path) -> Set[str]:
    """读取已有工作流索引中的仓库集合。"""
    repositories: Set[str] = set()
    for record in jsonl_records(path):
        repository = record.get("repository_name")
        if repository:
            repositories.add(str(repository))
    return repositories


def repository_is_eligible(record: dict) -> bool:
    """判断候选仓库是否满足既有仓库级筛选标准。"""
    repository = record.get("repo") or {}
    return (
        record.get("selected") is True
        and record.get("processed") is True
        and repository.get("isFork") is False
        and repository.get("isArchived") is False
        and float(repository.get("stargazers") or 0) > 100
        and float(record.get("total_runs_90d") or 0) > 50
        and float(repository.get("commits") or 0) > 100
    )


def load_candidates(data_dir: Path) -> Tuple[List[dict], dict]:
    """生成去重后的补抓候选仓库及统计信息。"""
    workflow_index = data_dir / "workflow_files.jsonl"
    repo_info = data_dir / "repo_info.jsonl"
    if not workflow_index.exists():
        raise FileNotFoundError(f"缺少现有工作流索引: {workflow_index}")
    if not repo_info.exists():
        raise FileNotFoundError(f"缺少仓库元数据: {repo_info}")

    existing_repositories = load_existing_workflow_repositories(workflow_index)
    seen: Set[str] = set()
    candidates: List[dict] = []
    stats = {
        "repo_info_records": 0,
        "repo_info_unique_repositories": 0,
        "existing_workflow_repositories": len(existing_repositories),
        "duplicate_repo_info_records": 0,
        "eligible_missing_repositories": 0,
        "eligible_missing_with_no_declared_workflows": 0,
        "eligible_missing_candidates": 0,
        "not_eligible_repositories": 0,
    }

    for record in jsonl_records(repo_info):
        stats["repo_info_records"] += 1
        repository_name = str(record.get("_id") or record.get("repo", {}).get("name") or "")
        if not repository_name:
            continue
        if repository_name in seen:
            stats["duplicate_repo_info_records"] += 1
            continue
        seen.add(repository_name)

        if repository_name in existing_repositories:
            continue

        if not repository_is_eligible(record):
            stats["not_eligible_repositories"] += 1
            continue

        if float(record.get("nb_workflows") or 0) <= 0:
            stats["eligible_missing_with_no_declared_workflows"] += 1
            continue

        stats["eligible_missing_repositories"] += 1
        repository = record.get("repo") or {}
        candidates.append(
            {
                "repository_name": repository_name,
                "default_branch": repository.get("defaultBranch") or "main",
                "stargazers": repository.get("stargazers") or 0,
                "commits": repository.get("commits") or 0,
                "total_runs_90d": record.get("total_runs_90d") or 0,
                "nb_workflows": record.get("nb_workflows") or 0,
                "selection": {
                    "selected": True,
                    "processed": True,
                    "isFork": False,
                    "isArchived": False,
                    "stargazers": "> 100",
                    "total_runs_90d": "> 50",
                    "commits": "> 100",
                    "nb_workflows": "> 0",
                },
            }
        )

    stats["repo_info_unique_repositories"] = len(seen)
    stats["eligible_missing_candidates"] = len(candidates)
    return candidates, stats


def write_candidates(path: Path, candidates: List[dict], stats: dict, as_of: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")
    summary = {
        **stats,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with path.with_name("candidate_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


def load_latest_statuses(path: Path) -> Dict[str, str]:
    """读取每个仓库最后一次补抓状态，用于只重试失败仓库。"""
    latest: Dict[str, str] = {}
    if not path.exists():
        return latest
    for record in jsonl_records(path):
        repository_name = record.get("repository_name")
        status = record.get("status")
        if repository_name and status:
            latest[str(repository_name)] = str(status)
    return latest


def rebuild_workflow_index_if_needed(output_dir: Path, force: bool = False) -> None:
    """为中断后尚未持久化索引的补充目录重建 hash/序号索引。"""
    workflows_dir = output_dir / "workflows"
    index_file = workflows_dir / WorkflowFileManager.INDEX_FILE
    if not workflows_dir.exists():
        return
    try:
        existing = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if existing.get("hashes") and not force:
        return

    hashes: Dict[str, str] = {}
    version_counters: Dict[str, int] = {}
    for workflow_file in workflows_dir.rglob("*.yml"):
        try:
            content = workflow_file.read_text(encoding="utf-8")
        except OSError:
            continue
        relative = workflow_file.relative_to(workflows_dir)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        hashes.setdefault(content_hash, str(relative).replace("\\", "/"))
        if len(relative.parts) >= 3:
            repository = f"{relative.parts[0]}/{relative.parts[1]}"
            match = re.match(r"^(?P<name>.+)_(?P<version>\d{3})$", workflow_file.stem)
            if match:
                workflow_path = f".github/workflows/{match.group('name')}.yml"
                key = f"{repository}|{workflow_path}"
                version_counters[key] = max(version_counters.get(key, 0), int(match.group("version")))

    index_file.write_text(
        json.dumps(
            {"hashes": hashes, "blob_to_hash": {}, "version_counters": version_counters},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"已重建补充工作流索引: {len(hashes)} 个内容哈希")


class MissingWorkflowFetcher:
    """仅抓取候选仓库默认分支在 as_of 时点的工作流文件。"""

    def __init__(self, output_dir: Path, as_of: datetime, workers: int = 4):
        self.output_dir = output_dir
        self.as_of = as_of
        self.as_of_text = as_of.isoformat().replace("+00:00", "Z")
        self.workers = workers
        # 每次启动都重建一次，确保上次中断后新增的 YAML 也进入索引。
        rebuild_workflow_index_if_needed(output_dir, force=True)
        self.client = GitHubClient(log_file=output_dir / "github_api.log")
        self.workflow_manager = WorkflowFileManager(output_dir / "workflows")
        self.workflow_writer = JsonlWriter(output_dir / "workflow_files.jsonl")
        self.status_writer = JsonlWriter(output_dir / "fetch_status.jsonl")
        self.checkpoint = CheckpointManager(output_dir / "checkpoints")
        self.lock = threading.Lock()
        self.stats = {
            "repositories_total": 0,
            "repositories_skipped_checkpoint": 0,
            "repositories_success": 0,
            "repositories_no_commit": 0,
            "repositories_no_workflows": 0,
            "repositories_errors": 0,
            "workflow_files_saved": 0,
            "workflow_files_failed": 0,
            "tree_fallbacks": 0,
        }

    def _record_status(self, repository_name: str, status: str, **extra) -> None:
        self.status_writer.write(
            {
                "repository_name": repository_name,
                "status": status,
                "as_of": self.as_of_text,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                **extra,
            }
        )

    def _latest_commit_before_cutoff(self, repository_name: str, branch: str) -> Optional[dict]:
        response = self.client.get_json(
            f"https://api.github.com/repos/{repository_name}/commits",
            params={
                "sha": branch,
                "until": self.as_of_text,
                "per_page": 1,
                "page": 1,
            },
        )
        if not isinstance(response, list) or not response:
            return None
        return response[0]

    def _tree_entries(self, repository_name: str, commit_sha: str) -> Optional[List[dict]]:
        tree = self.client.get_json(
            f"https://api.github.com/repos/{repository_name}/git/trees/{commit_sha}",
            params={"recursive": "1"},
        )
        if isinstance(tree, dict) and isinstance(tree.get("tree"), list):
            entries = tree["tree"]
            if tree.get("truncated"):
                with self.lock:
                    self.stats["tree_fallbacks"] += 1
                return None
            return entries
        return None

    def _contents_entries(self, repository_name: str, commit_sha: str) -> List[dict]:
        contents = self.client.get_json(
            f"https://api.github.com/repos/{repository_name}/contents/.github/workflows",
            params={"ref": commit_sha},
        )
        if not isinstance(contents, list):
            return []
        return [
            item
            for item in contents
            if isinstance(item, dict)
            and item.get("type") == "file"
            and str(item.get("path", "")).lower().endswith((".yml", ".yaml"))
        ]

    @staticmethod
    def _is_workflow_entry(entry: dict) -> bool:
        path = str(entry.get("path", ""))
        return (
            entry.get("type") == "blob"
            and path.startswith(".github/workflows/")
            and path.lower().endswith((".yml", ".yaml"))
        )

    def _decode_blob(self, repository_name: str, blob_sha: str) -> Optional[str]:
        blob = self.client.get_json(
            f"https://api.github.com/repos/{repository_name}/git/blobs/{blob_sha}"
        )
        if not isinstance(blob, dict) or blob.get("encoding") != "base64" or not blob.get("content"):
            return None
        try:
            raw = base64.b64decode(blob["content"])
        except (ValueError, TypeError):
            return None
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def process_repository(self, candidate: dict) -> bool:
        repository_name = candidate["repository_name"]
        if self.checkpoint.is_processed(repository_name):
            with self.lock:
                self.stats["repositories_skipped_checkpoint"] += 1
            return True

        branch = candidate.get("default_branch") or "main"
        try:
            commit = self._latest_commit_before_cutoff(repository_name, branch)
            if not commit:
                self._record_status(repository_name, "no_commit_before_cutoff", branch=branch)
                with self.lock:
                    self.stats["repositories_no_commit"] += 1
                self.checkpoint.mark_processed(repository_name)
                return True

            commit_sha = str(commit.get("sha") or "")
            commit_data = commit.get("commit") or {}
            commit_author = commit_data.get("author") or {}
            commit_date = commit_author.get("date") or (commit_data.get("committer") or {}).get("date")
            entries = self._tree_entries(repository_name, commit_sha)
            if entries is None:
                entries = self._contents_entries(repository_name, commit_sha)
            entries = [entry for entry in entries if self._is_workflow_entry(entry) or entry.get("path", "").lower().endswith((".yml", ".yaml"))]
            # Contents API entries do not have type=blob; tree entries are already scoped.
            entries = [entry for entry in entries if str(entry.get("path", "")).startswith(".github/workflows/")]

            if not entries:
                self._record_status(
                    repository_name,
                    "no_workflows_at_cutoff",
                    branch=branch,
                    commit_sha=commit_sha,
                    commit_date=commit_date,
                )
                with self.lock:
                    self.stats["repositories_no_workflows"] += 1
                self.checkpoint.mark_processed(repository_name)
                return True

            saved = 0
            for entry in entries:
                path = str(entry.get("path") or "")
                blob_sha = str(entry.get("sha") or "")
                if not path or not blob_sha:
                    with self.lock:
                        self.stats["workflow_files_failed"] += 1
                    continue
                content = self._decode_blob(repository_name, blob_sha)
                if content is None:
                    with self.lock:
                        self.stats["workflow_files_failed"] += 1
                    continue

                content_hash, content_file = self.workflow_manager.save(
                    content, repository_name, path
                )
                rel_path = content_file.relative_to(self.output_dir)
                self.workflow_writer.write(
                    {
                        "_id": f"{repository_name}_{path}",
                        "repository_name": repository_name,
                        "workflow_path": path,
                        "workflow_id": None,
                        "file_sha": blob_sha,
                        "content_hash": content_hash,
                        "content_file": str(rel_path).replace("\\", "/"),
                        "size": entry.get("size", 0),
                        # 逻辑快照时间用于后续运行匹配；retrieved_at 是真实抓取时间。
                        "fetched_at": self.as_of_text,
                        "snapshot_commit_sha": commit_sha,
                        "snapshot_commit_date": commit_date,
                        "default_branch": branch,
                        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
                saved += 1

            self._record_status(
                repository_name,
                "success" if saved else "workflow_content_failed",
                branch=branch,
                commit_sha=commit_sha,
                commit_date=commit_date,
                workflow_files=saved,
            )
            with self.lock:
                if saved:
                    self.stats["repositories_success"] += 1
                    self.stats["workflow_files_saved"] += saved
                else:
                    self.stats["repositories_errors"] += 1
            if saved:
                self.checkpoint.mark_processed(repository_name)
            return bool(saved)
        except Exception as exc:  # noqa: BLE001 - 单仓库失败不应中断批处理
            self._record_status(repository_name, "error", branch=branch, error=str(exc))
            with self.lock:
                self.stats["repositories_errors"] += 1
            return False

    def run(self, candidates: List[dict], limit: Optional[int] = None) -> dict:
        selected = candidates[:limit] if limit else candidates
        self.stats["repositories_total"] = len(selected)
        try:
            with ThreadPoolExecutor(max_workers=max(1, self.workers)) as executor:
                futures = [executor.submit(self.process_repository, candidate) for candidate in selected]
                for index, future in enumerate(as_completed(futures), 1):
                    future.result()
                    if index % 100 == 0:
                        with self.workflow_manager._lock:
                            self.workflow_manager._save_index()
                        self.checkpoint.set_metadata("completed", index)
                        self.checkpoint.save()
                        print(f"已完成 {index}/{len(selected)} 个候选仓库")
            self.checkpoint.set_metadata("completed", len(selected))
            self.checkpoint.set_metadata("as_of", self.as_of_text)
            with self.workflow_manager._lock:
                self.workflow_manager._save_index()
            self.checkpoint.save()
            return dict(self.stats)
        finally:
            self.workflow_writer.close()
            self.status_writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="补抓符合筛选标准但缺少工作流索引的仓库")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="现有主数据目录")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="独立补充输出目录")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="逻辑快照时间，默认 2026-06-09T23:59:59Z")
    parser.add_argument("--workers", type=int, default=4, help="并发仓库数")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个候选仓库，用于试运行")
    parser.add_argument("--dry-run", action="store_true", help="只生成候选清单，不访问 GitHub API")
    parser.add_argument("--retry-errors", action="store_true", help="只重试补抓状态为 error 的仓库")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    as_of = parse_as_of(args.as_of)
    candidates, stats = load_candidates(data_dir)
    if args.retry_errors:
        latest_statuses = load_latest_statuses(output_dir / "fetch_status.jsonl")
        candidates = [
            candidate
            for candidate in candidates
            if latest_statuses.get(candidate["repository_name"]) == "error"
        ]
        stats = {
            **stats,
            "retry_error_candidates": len(candidates),
        }
    candidate_path = output_dir / (
        "retry_error_repositories.jsonl" if args.retry_errors else "candidate_repositories.jsonl"
    )
    write_candidates(candidate_path, candidates, stats, as_of)

    print(json.dumps({**stats, "candidate_file": str(candidate_path)}, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("dry-run：未访问 GitHub API")
        return

    fetcher = MissingWorkflowFetcher(output_dir, as_of, workers=args.workers)
    result = fetcher.run(candidates, limit=args.limit)
    with (output_dir / "fetch_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                **result,
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "candidate_count": len(candidates),
                "limit": args.limit,
                "retry_errors": args.retry_errors,
                "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
