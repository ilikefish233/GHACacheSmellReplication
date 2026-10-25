# scripts/data_update/fetch_all.py
"""
按仓库遍历，统一采集仓库信息、工作流运行记录、工作流文件、工作流变更历史。

输入: data/filtered_repositories.json
输出: data_26_6_9/
        repo_info.jsonl           — 仓库最新信息
        workflow_runs.jsonl       — 工作流运行记录
        workflow_files.jsonl      — 工作流文件映射
        workflow_changes.jsonl    — 工作流变更历史
        workflows/*.yml           — 工作流 YAML 文件（按内容哈希去重）

API 调用优化:
  1. commits 计数与最新 commit 合并为一次请求（省 1 次/仓库）
  2. labels/branches/releases/contributors/pulls 计数默认跳过，保留原值（省 5 次/仓库）
  3. 步骤 3 用 Blob API 替代 Contents API 获取文件内容（更轻量，无目录结构冗余）
  4. 步骤 4 用 Git Tree API 批量获取 blob SHA，替代逐 commit detail 请求
  5. blob SHA 缓存：相同内容不重复请求，只对实际变化的 blob 请求内容
  6. 早期跳过：已归档/无工作流文件时跳过后续步骤
  7. 最新版本文件内容在步骤 3 和 4 间复用
"""
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[2]


import json
import hashlib
import re
import sys
import time
import logging
import base64
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from math import ceil
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.loaders.json_loader import JSONLoader
from src.utils.github_client import GitHubClient
from src.utils.checkpoint import CheckpointManager
from scripts.data_update.filter_config import (
    FILTER_ARCHIVED_REPOS, FILTER_BY_RECENT_RUNS,
    RECENT_RUNS_DAYS, RECENT_RUNS_MIN_COUNT,
    FILTER_WF_WITHOUT_RUNS,
)


# ─── 配置 ─────────────────────────────────────────────

INPUT_FILE = Path(str(_PORTABLE_PROJECT_ROOT / 'data/filtered_repositories.json'))
OUTPUT_DIR = Path(str(_PORTABLE_PROJECT_ROOT / 'data_26_6_9'))

MAX_RUN_PAGES = 20          # 每个仓库最多获取 20 页 runs
MAX_RUNS_PER_WORKFLOW = 5   # 每个工作流文件最多保留最近 N 条运行记录
MAX_COMMITS_PER_FILE = 50   # 每个工作流文件最多获取 50 个 commit
MAX_WORKERS = 4             # 并发工作线程数

# 可选数据开关（关闭可省 5 次 API 调用/仓库）
FETCH_OPTIONAL_COUNTS = False  # labels, branches, releases, contributors, PRs
SKIP_WORKFLOW_CHANGES = False   # 跳过工作流变更历史，后续单独补全


# ─── 文件写入器 ────────────────────────────────────────

class JsonlWriter:
    """线程安全的缓冲 JSONL 写入器，保持文件句柄打开，定期 flush"""

    def __init__(self, file_path: Path, flush_interval: int = 100):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._buffer: List[str] = []
        self._flush_interval = flush_interval
        self._file = open(self.file_path, 'a', encoding='utf-8', newline='\n')

    def write(self, record: dict):
        line = json.dumps(record, ensure_ascii=False) + '\n'
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= self._flush_interval:
                self._flush()

    def _flush(self):
        if self._buffer:
            self._file.writelines(self._buffer)
            self._buffer.clear()
            self._file.flush()

    def flush(self):
        with self._lock:
            self._flush()

    def close(self):
        with self._lock:
            self._flush()
            self._file.close()


# ─── 工作流文件管理器 ──────────────────────────────────

class WorkflowFileManager:
    """
    管理工作流 YAML 文件，按仓库路径+序号命名，体现时间顺序。
    线程安全，支持持久化 hash 索引避免启动重算。

    文件命名: workflows/{owner}/{repo}/{filename}_{序号}.yml
    例如: workflows/pytables/pytables/wheels_001.yml
          workflows/pytables/pytables/wheels_002.yml

    同时维护 content_hash → 文件路径的映射，用于去重和缓存查找。
    """

    INDEX_FILE = '_hash_index.json'

    def __init__(self, workflows_dir: Path):
        self.workflows_dir = workflows_dir
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        # content_hash → 文件路径（相对路径字符串）
        self.hashes: Dict[str, Path] = {}
        # git blob SHA → content_hash 映射，避免重复请求已知内容
        self.blob_to_hash: Dict[str, str] = {}
        # (repo_name, wf_path) → 当前最大序号，用于递增命名
        self.version_counters: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self):
        """加载已有文件索引，优先从持久化索引加载，否则扫描文件重建"""
        index_file = self.workflows_dir / self.INDEX_FILE
        if index_file.exists():
            try:
                with open(index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for h, rel_path in data.get('hashes', {}).items():
                    p = self.workflows_dir / rel_path
                    if p.exists():
                        self.hashes[h] = p
                for blob, h in data.get('blob_to_hash', {}).items():
                    self.blob_to_hash[blob] = h
                for key_str, seq in data.get('version_counters', {}).items():
                    # key 格式: "owner/repo|.github/workflows/ci.yml"
                    if '|' in key_str:
                        repo_name, wf_path = key_str.split('|', 1)
                        self.version_counters[(repo_name, wf_path)] = seq
                print(f"从索引加载: {len(self.hashes)} 个文件, {len(self.blob_to_hash)} 个 blob 映射")
                return
            except Exception:
                pass

        # 无索引文件，扫描重建
        for wf_file in self.workflows_dir.rglob('*.yml'):
            try:
                content = wf_file.read_text(encoding='utf-8')
                h = hashlib.sha256(content.encode('utf-8')).hexdigest()
                self.hashes[h] = wf_file
            except Exception:
                pass

            rel = wf_file.relative_to(self.workflows_dir)
            parts = rel.parts
            if len(parts) >= 3:
                owner, repo = parts[0], parts[1]
                repo_name = f"{owner}/{repo}"
                stem = wf_file.stem
                if '_' in stem:
                    try:
                        seq = int(stem.rsplit('_', 1)[-1])
                        wf_filename = stem.rsplit('_', 1)[0]
                        wf_path = f".github/workflows/{wf_filename}.yml"
                        key = (repo_name, wf_path)
                        if key not in self.version_counters or seq > self.version_counters[key]:
                            self.version_counters[key] = seq
                    except ValueError:
                        pass

        print(f"扫描重建索引: {len(self.hashes)} 个文件")
        self._save_index()

    def _save_index(self):
        """持久化 hash 索引到磁盘"""
        index_file = self.workflows_dir / self.INDEX_FILE
        data = {
            'hashes': {
                h: str(p.relative_to(self.workflows_dir)).replace('\\', '/')
                for h, p in self.hashes.items()
            },
            'blob_to_hash': self.blob_to_hash,
            'version_counters': {
                f"{k[0]}|{k[1]}": v
                for k, v in self.version_counters.items()
            },
        }
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def content_hash(self, content: str) -> str:
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def _next_version(self, repo_name: str, wf_path: str) -> int:
        """获取下一个版本序号"""
        key = (repo_name, wf_path)
        current = self.version_counters.get(key, 0)
        current += 1
        self.version_counters[key] = current
        return current

    def _make_file_path(self, repo_name: str, wf_path: str, version: int) -> Path:
        """构建文件路径: workflows/{owner}/{repo}/{filename}_{序号}.yml"""
        owner, repo = repo_name.split('/', 1)
        # 从 .github/workflows/ci.yml 提取 ci
        wf_filename = Path(wf_path).stem  # ci (不含扩展名)
        filename = f"{wf_filename}_{version:03d}.yml"
        return self.workflows_dir / owner / repo / filename

    def save(self, content: str, repo_name: str = '', wf_path: str = '') -> Tuple[str, Path]:
        """
        保存工作流文件，返回 (内容哈希, 文件路径)。
        如内容已存在（hash 相同）则跳过写入但仍返回路径。
        线程安全。
        """
        h = self.content_hash(content)
        with self._lock:
            if h in self.hashes:
                return h, self.hashes[h]

            version = self._next_version(repo_name, wf_path)
            wf_file = self._make_file_path(repo_name, wf_path, version)
            wf_file.parent.mkdir(parents=True, exist_ok=True)
            wf_file.write_text(content, encoding='utf-8')
            self.hashes[h] = wf_file
            return h, wf_file

    def lookup_by_hash(self, content_hash: str) -> Optional[Path]:
        """通过内容哈希查找文件路径"""
        with self._lock:
            return self.hashes.get(content_hash)

    def map_blob(self, blob_sha: str, content_hash: str):
        """记录 git blob SHA -> content_hash 映射，线程安全"""
        with self._lock:
            self.blob_to_hash[blob_sha] = content_hash

    def lookup_blob(self, blob_sha: str) -> Optional[str]:
        """通过 git blob SHA 查找已知的 content_hash"""
        with self._lock:
            return self.blob_to_hash.get(blob_sha)

    @property
    def unique_count(self):
        return len(self.hashes)


# ─── 核心采集器 ────────────────────────────────────────

class RepoDataFetcher:
    """按仓库遍历，统一采集所有数据"""

    def __init__(self, client: GitHubClient, output_dir: Path):
        self.client = client
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.writers = {
            'repo_info': JsonlWriter(output_dir / 'repo_info.jsonl'),
            'workflow_runs': JsonlWriter(output_dir / 'workflow_runs.jsonl'),
            'workflow_files': JsonlWriter(output_dir / 'workflow_files.jsonl'),
            'workflow_changes': JsonlWriter(output_dir / 'workflow_changes.jsonl'),
            'filtered_out': JsonlWriter(output_dir / 'filtered_out.jsonl'),
        }

        self.wf_manager = WorkflowFileManager(output_dir / 'workflows')

        # 仓库级别的 Git Tree 缓存: {commit_sha: {wf_path: blob_sha}}
        # 同一仓库的多个工作流文件可共享 tree 结果，避免重复请求
        self.tree_cache: Dict[str, Dict[str, str]] = {}
        self._tree_cache_lock = threading.Lock()

        self.checkpoint = CheckpointManager(
            output_dir / 'checkpoints' / 'fetch_all'
        )

        self.log_file = output_dir / 'fetch_all.log'
        self._setup_logging()

        self._stats_lock = threading.Lock()
        self.stats = {
            'total_repos': 0,
            'repos_success': 0,
            'repos_skipped': 0,
            'repos_no_workflows': 0,
            'repos_archived': 0,
            'repos_filtered_runs': 0,
            'repos_errors': 0,
            'wf_filtered_no_runs': 0,
            'runs_saved': 0,
            'workflow_files_saved': 0,
            'workflow_changes_saved': 0,
            'api_saved_by_cache': 0,
            'api_saved_by_tree_cache': 0,
        }

        self._failed_lock = threading.Lock()
        self.failed_repos: Dict[str, Dict] = {}
        self._load_failed()

    def _setup_logging(self):
        self.logger = logging.getLogger('RepoDataFetcher')
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            fh = logging.FileHandler(self.log_file, encoding='utf-8')
            fh.setLevel(logging.INFO)
            fh.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
            self.logger.addHandler(fh)

    def log(self, msg: str, level: str = 'info'):
        print(msg)
        if level == 'error':
            self.logger.error(msg)
        else:
            self.logger.info(msg)

    def _load_failed(self):
        failed_file = self.output_dir / 'failed_repos.json'
        if failed_file.exists():
            try:
                with open(failed_file, 'r', encoding='utf-8') as f:
                    self.failed_repos = json.load(f)
                self.log(f"加载失败记录: {len(self.failed_repos)} 条")
            except json.JSONDecodeError:
                backup = failed_file.with_suffix('.json.bak')
                if backup.exists():
                    backup.unlink()
                failed_file.rename(backup)
                self.failed_repos = {}

    def _save_failed(self):
        with self._failed_lock:
            if self.failed_repos:
                failed_file = self.output_dir / 'failed_repos.json'
                with open(failed_file, 'w', encoding='utf-8') as f:
                    json.dump(self.failed_repos, f, indent=2, ensure_ascii=False)

    def _save_state(self):
        self.checkpoint.save()
        self._save_failed()
        self.wf_manager._save_index()

    # ─── 步骤 1: 仓库信息 ─────────────────────────────

    def fetch_repo_info(self, repo_name: str, old_record: dict) -> Optional[dict]:
        """
        获取仓库基本信息。
        优化: commits 计数与最新 commit 合并为一次请求。
        可选数据(labels/branches/releases/contributors/PRs)默认跳过。
        """
        extra = {'Accept': 'application/vnd.github.mercy-preview+json'}
        repo_data = self.client.get_json(
            f'https://api.github.com/repos/{repo_name}',
            extra_headers=extra
        )
        if repo_data is None:
            return None

        default_branch = repo_data.get('default_branch', 'main')

        # ── 必须的附加信息 ──
        languages = self.client.get_json(
            f'https://api.github.com/repos/{repo_name}/languages'
        ) or {}

        # ── commits 计数 + 最新 commit（合并为一次请求）──
        commits_count = 0
        last_commit_date, last_commit_sha = '', ''
        commits_resp = self.client.get(
            f'https://api.github.com/repos/{repo_name}/commits',
            params={'per_page': 1, 'sha': default_branch}
        )
        if commits_resp and commits_resp.status_code == 200:
            # 从 Link header 获取计数
            link_header = commits_resp.headers.get('Link', '')
            if link_header:
                last_match = re.search(r'page=(\d+)>; rel="last"', link_header)
                if last_match:
                    commits_count = int(last_match.group(1))
            # 从响应体获取最新 commit
            commits_data = commits_resp.json()
            if isinstance(commits_data, list) and commits_data:
                c = commits_data[0]
                last_commit_date = c.get('commit', {}).get('committer', {}).get('date', '')
                last_commit_sha = c.get('sha', '')
                if commits_count == 0:
                    commits_count = len(commits_data)

        # ── 可选附加信息（默认跳过，省 5 次 API 调用）──
        old_repo = old_record.get('repo', {})

        if FETCH_OPTIONAL_COUNTS:
            labels_items = self.client.get_paginated(
                f'https://api.github.com/repos/{repo_name}/labels', per_page=100
            )
            labels = [item['name'] for item in labels_items if isinstance(item, dict) and 'name' in item]
            branches = self.client.get_count_from_link_header(
                f'https://api.github.com/repos/{repo_name}/branches'
            )
            releases = self.client.get_count_from_link_header(
                f'https://api.github.com/repos/{repo_name}/releases'
            )
            contributors = self.client.get_count_from_link_header(
                f'https://api.github.com/repos/{repo_name}/contributors',
                params={'anon': 'true'}
            )
            total_prs = self.client.get_count_from_link_header(
                f'https://api.github.com/repos/{repo_name}/pulls',
                params={'state': 'all'}
            )
            open_prs = self.client.get_count_from_link_header(
                f'https://api.github.com/repos/{repo_name}/pulls',
                params={'state': 'open'}
            )
        else:
            labels = old_repo.get('labels', [])
            branches = old_repo.get('branches', 0)
            releases = old_repo.get('releases', 0)
            contributors = old_repo.get('contributors', 0)
            total_prs = old_repo.get('totalPullRequests', 0)
            open_prs = old_repo.get('openPullRequests', 0)

        # ── 构建记录 ──
        repo_info = {
            'id': repo_data.get('id'),
            'name': repo_data.get('full_name'),
            'isFork': repo_data.get('fork', False),
            'isArchived': repo_data.get('archived', False),
            'commits': commits_count or old_repo.get('commits', 0),
            'branches': branches or old_repo.get('branches', 0),
            'defaultBranch': default_branch,
            'releases': releases or old_repo.get('releases', 0),
            'contributors': contributors or old_repo.get('contributors', 0),
            'license': repo_data.get('license', {}).get('spdx_id', '') if repo_data.get('license') else '',
            'watchers': repo_data.get('watchers_count', 0),
            'stargazers': repo_data.get('stargazers_count', 0),
            'forks': repo_data.get('forks_count', 0),
            'size': repo_data.get('size', 0),
            'createdAt': repo_data.get('created_at', ''),
            'pushedAt': repo_data.get('pushed_at', ''),
            'updatedAt': repo_data.get('updated_at', ''),
            'homepage': repo_data.get('homepage', '') or '',
            'mainLanguage': repo_data.get('language', ''),
            'totalIssues': repo_data.get('open_issues_count', 0) + total_prs,
            'openIssues': repo_data.get('open_issues_count', 0),
            'totalPullRequests': total_prs or old_repo.get('totalPullRequests', 0),
            'openPullRequests': open_prs or old_repo.get('openPullRequests', 0),
            'blankLines': old_repo.get('blankLines', 0),
            'codeLines': old_repo.get('codeLines', 0),
            'commentLines': old_repo.get('commentLines', 0),
            'metrics': old_repo.get('metrics', []),
            'lastCommit': last_commit_date or old_repo.get('lastCommit', ''),
            'lastCommitSHA': last_commit_sha or old_repo.get('lastCommitSHA', ''),
            'hasWiki': repo_data.get('has_wiki', False),
            'languages': languages,
            'labels': labels,
            'topics': repo_data.get('topics', []),
        }

        record = {
            '_id': old_record['_id'],
            'total_runs_90d': old_record.get('total_runs_90d', 0),
            'repo': repo_info,
            'etag': old_record.get('etag', ''),
            'selected': old_record.get('selected', False),
            'processed': old_record.get('processed', False),
            'nb_runs': old_record.get('nb_runs', 0),
            'nb_workflows': old_record.get('nb_workflows', 0),
            'fetched_at': datetime.now().isoformat(),
        }

        self.writers['repo_info'].write(record)
        return repo_data

    # ─── 步骤 2: 工作流运行记录 ────────────────────────

    def fetch_workflow_runs(self, repo_name: str) -> Tuple[int, Dict[str, int]]:
        """
        获取工作流运行记录，每个工作流文件只保留最近 MAX_RUNS_PER_WORKFLOW 条。
        运行记录按时间倒序返回，因此按 workflow_path 分组后取前 N 条即可。

        优化: 当所有工作流文件都已收集够 N 条时提前停止分页，
              避免请求不必要的后续页面。

        返回: (保存条数, {workflow_path: workflow_id} 映射)
        """
        base_url = f'https://api.github.com/repos/{repo_name}/actions/runs'

        first_page = self.client.get_json(base_url, params={'per_page': 100, 'page': 1})
        if first_page is None:
            return -1

        total_count = first_page.get('total_count', 0)
        if total_count == 0:
            return 0

        page_count = min(ceil(total_count / 100), MAX_RUN_PAGES)

        # 按工作流路径分组收集运行记录
        runs_by_workflow: Dict[str, List[dict]] = {}
        # 同时收集 workflow_path → workflow_id 映射（供步骤3关联）
        path_to_workflow_id: Dict[str, int] = {}
        saved = 0

        def process_runs_page(runs_data: dict) -> bool:
            """处理一页运行记录，返回是否需要继续分页"""
            nonlocal saved
            for run in runs_data.get('workflow_runs', []):
                workflow_path = run.get('path', '')

                # 该工作流文件已收集够，跳过
                if len(runs_by_workflow.get(workflow_path, [])) >= MAX_RUNS_PER_WORKFLOW:
                    continue

                created_at = run.get('created_at', '')
                updated_at = run.get('updated_at', '')
                run_started_at = run.get('run_started_at', '')
                run_number = run.get('run_number', 0)
                run_attempt = run.get('run_attempt', 1)

                duration = None
                start_str = run_started_at or created_at
                if start_str and updated_at:
                    try:
                        from datetime import datetime as dt
                        start = dt.fromisoformat(start_str.replace('Z', '+00:00'))
                        end = dt.fromisoformat(updated_at.replace('Z', '+00:00'))
                        duration = (end - start).total_seconds()
                    except (ValueError, TypeError):
                        pass

                record = {
                    '_id': f"{repo_name}_{workflow_path}_{run_number}_{run_attempt}",
                    'repository_name': repo_name,
                    'run_id': run.get('id'),
                    'workflow_id': run.get('workflow_id'),
                    'workflow_path': workflow_path,
                    'workflow_name': run.get('name', ''),
                    'run_number': run_number,
                    'run_attempt': run_attempt,
                    'status': run.get('status', ''),
                    'conclusion': run.get('conclusion', ''),
                    'event': run.get('event', ''),
                    'head_branch': run.get('head_branch', ''),
                    'head_sha': run.get('head_sha', ''),
                    'created_at': created_at,
                    'updated_at': updated_at,
                    'run_started_at': run_started_at,
                    'duration_seconds': duration,
                    'actor': run.get('actor', {}).get('login', '') if run.get('actor') else '',
                    'url': run.get('url', ''),
                    'html_url': run.get('html_url', ''),
                    'fetched_at': datetime.now().isoformat(),
                }

                if workflow_path not in runs_by_workflow:
                    runs_by_workflow[workflow_path] = []
                runs_by_workflow[workflow_path].append(record)

                # 记录 workflow_path → workflow_id 映射
                if workflow_path and run.get('workflow_id') and workflow_path not in path_to_workflow_id:
                    path_to_workflow_id[workflow_path] = run['workflow_id']

            # 检查是否所有工作流文件都已收集够
            # 注意: 无法提前知道总共有多少个工作流文件，所以不能完全提前退出
            # 但如果当前页所有 run 都被跳过了，说明已收集够
            return True

        process_runs_page(first_page)

        for page in range(2, page_count + 1):
            # 如果当前页没有新增任何记录，说明所有工作流文件都已收集够
            before_count = sum(len(v) for v in runs_by_workflow.values())
            page_data = self.client.get_json(base_url, params={'per_page': 100, 'page': page})
            if page_data is None:
                break
            process_runs_page(page_data)
            after_count = sum(len(v) for v in runs_by_workflow.values())
            if after_count == before_count:
                # 本页没有新增记录，后续页也不会有，提前停止
                break

        # 写入每个工作流文件的前 N 条记录
        for wf_path, runs in runs_by_workflow.items():
            for run in runs[:MAX_RUNS_PER_WORKFLOW]:
                self.writers['workflow_runs'].write(run)
                saved += 1

        return saved, path_to_workflow_id, runs_by_workflow

    # ─── 步骤 3: 工作流文件最新版本 ────────────────────

    def fetch_workflow_files(self, repo_name: str, default_branch: str,
                              path_to_workflow_id: Dict[str, int] = None) -> List[dict]:
        """
        获取工作流文件最新版本。
        优化: 列目录时 Contents API 已返回 git blob SHA，
              后续步骤 4 可通过 blob SHA 判断内容是否变化，避免重复获取。
        """
        url = f'https://api.github.com/repos/{repo_name}/contents/.github/workflows'
        data = self.client.get_json(url, params={'ref': default_branch})

        if not data:
            return []

        if isinstance(data, dict):
            if data.get('type') == 'file':
                data = [data]
            elif data.get('message', '').lower().startswith('not found'):
                return []
            else:
                return []

        file_entries = []
        for item in data:
            if not isinstance(item, dict) or item.get('type') != 'file':
                continue
            path = item.get('path', '')
            if path.endswith('.yml') or path.endswith('.yaml'):
                file_entries.append({
                    'path': path,
                    'sha': item.get('sha', ''),       # git blob SHA
                    'size': item.get('size', 0),
                    'git_url': item.get('git_url', ''),  # blob API URL
                })

        if not file_entries:
            return []

        results = []
        for entry in file_entries:
            # 优先用 Blob API 获取内容（比 Contents API 更轻量，无目录结构冗余）
            content = None
            if entry['git_url']:
                content = self._fetch_file_at_commit_via_blob(entry['git_url'])
            if content is None:
                content = self.client.get_file_content(repo_name, entry['path'], default_branch)
            if content is None:
                continue

            content_hash, wf_path_file = self.wf_manager.save(content, repo_name, entry['path'])

            # 记录 blob SHA -> content_hash 映射，供步骤 4 复用
            if entry['sha']:
                self.wf_manager.map_blob(entry['sha'], content_hash)

            # content_file 使用相对于 output_dir 的路径
            rel_path = wf_path_file.relative_to(self.output_dir)
            record = {
                '_id': f"{repo_name}_{entry['path']}",
                'repository_name': repo_name,
                'workflow_path': entry['path'],
                'workflow_id': (path_to_workflow_id or {}).get(entry['path']),
                'file_sha': entry['sha'],
                'content_hash': content_hash,
                'content_file': str(rel_path).replace('\\', '/'),
                'size': entry['size'],
                'fetched_at': datetime.now().isoformat(),
            }
            self.writers['workflow_files'].write(record)
            self._inc_stats('workflow_files_saved')

            results.append({
                **entry,
                'content': content,
                'content_hash': content_hash,
            })

        return results

    # ─── 步骤 4: 工作流文件变更历史 ────────────────────

    def _fetch_file_at_commit_via_blob(self, git_url: str) -> Optional[str]:
        """
        通过 Git Blob API 获取文件内容（原始文本）。
        不负责保存，由调用方决定保存方式。
        返回 content 字符串或 None。
        """
        blob_data = self.client.get_json(git_url)
        if not blob_data or 'content' not in blob_data:
            return None

        raw_bytes = base64.b64decode(blob_data['content'])
        for encoding in ['utf-8', 'utf-8-sig', 'latin-1']:
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw_bytes.decode('utf-8', errors='replace')

    def _batch_get_blob_shas(self, repo_name: str, commit_shas: List[str],
                              wf_path: str,
                              local_tree_cache: Dict[str, Dict[str, str]] = None) -> Dict[str, Optional[str]]:
        """
        批量获取每个 commit 对应工作流文件的 blob SHA。
        使用 Git Tree API（递归模式），一次请求获取整个 commit tree，
        从中提取目标文件的 blob SHA。比逐 commit detail 请求高效得多。

        利用 local_tree_cache 缓存：同一仓库的多个工作流文件共享 tree 结果，
        避免对同一 commit 重复请求 tree。

        返回: {commit_sha: blob_sha} 映射
        """
        tree_cache = local_tree_cache if local_tree_cache is not None else self.tree_cache
        blob_sha_map = {}
        # 找出需要请求 tree 的 commit（未缓存的）
        uncached_shas = [sha for sha in commit_shas if sha not in tree_cache]
        self._inc_stats('api_saved_by_tree_cache', len(commit_shas) - len(uncached_shas))

        # 批量请求未缓存的 tree
        for commit_sha in uncached_shas:
            tree_data = self.client.get_json(
                f'https://api.github.com/repos/{repo_name}/git/trees/{commit_sha}',
                params={'recursive': '1'}
            )

            if not tree_data or 'tree' not in tree_data:
                tree_cache[commit_sha] = {}
                continue

            # 提取所有 .github/workflows/ 下的文件映射
            wf_files_map = {}
            for item in tree_data['tree']:
                if (item.get('type') == 'blob'
                        and item.get('path', '').startswith('.github/workflows/')):
                    wf_files_map[item['path']] = item.get('sha')

            tree_cache[commit_sha] = wf_files_map

            # 如果 tree 被截断，回退到 commit detail 查找目标文件
            if tree_data.get('truncated') and wf_path not in wf_files_map:
                commit_detail = self.client.get_json(
                    f'https://api.github.com/repos/{repo_name}/commits/{commit_sha}'
                )
                if commit_detail:
                    for f in commit_detail.get('files', []):
                        if f.get('filename') == wf_path:
                            wf_files_map[wf_path] = f.get('sha')
                            break

        # 从缓存中提取目标文件的 blob SHA
        for commit_sha in commit_shas:
            blob_sha_map[commit_sha] = tree_cache.get(commit_sha, {}).get(wf_path)

        return blob_sha_map

    def _get_content_by_blob_sha(self, repo_name: str, blob_sha: str,
                                  wf_path: str = '') -> Optional[Tuple[str, str]]:
        """
        通过 blob SHA 获取文件内容，优先使用缓存。
        返回 (content, content_hash) 或 None。
        """
        # 先查缓存
        cached_hash = self.wf_manager.lookup_blob(blob_sha)
        if cached_hash:
            self._inc_stats('api_saved_by_cache')
            wf_file = self.wf_manager.hashes.get(cached_hash)
            if wf_file and wf_file.exists():
                content = wf_file.read_text(encoding='utf-8')
                return content, cached_hash

        # 缓存未命中，通过 Blob API 获取
        blob_url = f'https://api.github.com/repos/{repo_name}/git/blobs/{blob_sha}'
        content = self._fetch_file_at_commit_via_blob(blob_url)
        if content:
            content_hash, _ = self.wf_manager.save(content, repo_name, wf_path)
            self.wf_manager.map_blob(blob_sha, content_hash)
            return content, content_hash

        return None

    def fetch_workflow_changes(self, repo_name: str, default_branch: str,
                               wf_files: List[dict],
                               path_to_workflow_id: Dict[str, int] = None,
                               local_tree_cache: Dict[str, Dict[str, str]] = None):
        """
        获取工作流文件变更历史。

        核心优化策略:
          1. Git Tree API 批量获取每个 commit 的文件 blob SHA
             （1次 tree 请求替代1次 commit detail，且 tree 更轻量）
          2. blob SHA 缓存：相同内容不重复请求，只对实际变化的 blob 请求内容
          3. 最新版本内容复用步骤 3 的结果
          4. 仅对 blob SHA 发生变化的 commit 请求文件内容

        API 调用对比（5个文件，每个10次变更，3次实际内容变化）:
          优化前: 5*10 commit detail + 5*10 blob/contents = ~100 次
          优化后: 5*10 tree + 5*3 blob = ~65 次（缓存命中率高时更少）
        """
        for wf in wf_files:
            wf_path = wf['path']
            latest_content = wf['content']
            latest_hash = wf['content_hash']
            latest_blob_sha = wf.get('sha', '')

            # 获取 commit 历史（已包含 sha/date/author/message）
            commits = self.client.get_paginated(
                f'https://api.github.com/repos/{repo_name}/commits',
                params={'path': wf_path, 'sha': default_branch},
                max_pages=5,
                per_page=100
            )
            if not commits:
                continue

            commits = commits[:MAX_COMMITS_PER_FILE]
            commits.reverse()  # 按时间正序

            # 收集 commit 元数据
            commit_meta = []
            for commit in commits:
                commit_data = commit.get('commit', {})
                author = commit_data.get('author', {})
                committer = commit_data.get('committer', {})
                message = commit_data.get('message', '').split('\n')[0][:200]
                commit_meta.append({
                    'sha': commit.get('sha', ''),
                    'date': committer.get('date', ''),
                    'author': author.get('name', ''),
                    'message': message,
                })

            # ── 批量获取每个 commit 对应文件的 blob SHA ──
            commit_shas = [m['sha'] for m in commit_meta]
            blob_sha_map = self._batch_get_blob_shas(repo_name, commit_shas, wf_path,
                                                      local_tree_cache=local_tree_cache)

            # ── 遍历 commit，只对 blob SHA 变化的请求内容 ──
            prev_hash = None
            prev_blob_sha = None
            change_count = 0

            for i, meta in enumerate(commit_meta):
                commit_sha = meta['sha']
                blob_sha = blob_sha_map.get(commit_sha)

                # 复用最新版本（最后一个 commit 且 blob SHA 匹配）
                if (i == len(commit_meta) - 1 and latest_content
                        and (blob_sha is None or blob_sha == latest_blob_sha)):
                    content = latest_content
                    content_hash = latest_hash
                    blob_sha = latest_blob_sha or blob_sha
                elif blob_sha:
                    # 有 blob SHA，通过缓存或 Blob API 获取内容
                    result = self._get_content_by_blob_sha(repo_name, blob_sha, wf_path)
                    if result is None:
                        continue
                    content, content_hash = result
                else:
                    # 无 blob SHA（tree 中未找到），回退到 Contents API
                    content = self.client.get_file_content(repo_name, wf_path, ref=commit_sha)
                    if content is None:
                        continue
                    content_hash, _ = self.wf_manager.save(content, repo_name, wf_path)

                # 只记录实际变更（通过 content_hash 判断）
                is_change = (content_hash != prev_hash) if prev_hash is not None else True

                if is_change:
                    change_count += 1
                    self._inc_stats('workflow_changes_saved')

                    # 查找 content_hash 对应的文件路径
                    wf_file_path = self.wf_manager.lookup_by_hash(content_hash)
                    if wf_file_path:
                        rel_path = wf_file_path.relative_to(self.output_dir)
                        content_file_str = str(rel_path).replace('\\', '/')
                    else:
                        content_file_str = ''

                    record = {
                        '_id': f"{repo_name}_{wf_path}_{commit_sha[:12]}",
                        'repository_name': repo_name,
                        'workflow_path': wf_path,
                        'workflow_id': (path_to_workflow_id or {}).get(wf_path),
                        'commit_sha': commit_sha,
                        'commit_date': meta['date'],
                        'commit_author': meta['author'],
                        'commit_message': meta['message'],
                        'content_hash': content_hash,
                        'content_file': content_file_str,
                        'prev_content_hash': prev_hash,
                        'is_change': True,
                        'fetched_at': datetime.now().isoformat(),
                    }
                    self.writers['workflow_changes'].write(record)
                    prev_hash = content_hash

            if change_count > 0:
                print(f"    {wf_path}: {change_count} 次变更")

    def _inc_stats(self, key: str, delta: int = 1):
        """线程安全地更新统计计数"""
        with self._stats_lock:
            self.stats[key] += delta

    def _get_stats(self, key: str) -> int:
        with self._stats_lock:
            return self.stats[key]

    # ─── 仓库处理主流程 ────────────────────────────────

    def process_repo(self, old_record: dict) -> bool:
        repo_name = old_record['_id']
        self._inc_stats('total_repos')

        if self.checkpoint.is_processed(repo_name):
            self._inc_stats('repos_skipped')
            return True

        print(f"\n[{self._get_stats('total_repos')}] {repo_name}")

        # 每个仓库独立的 tree 缓存（避免跨仓库冲突和内存泄漏）
        local_tree_cache: Dict[str, Dict[str, str]] = {}

        try:
            # ── 步骤 1: 仓库信息 ──
            repo_data = self.fetch_repo_info(repo_name, old_record)
            if repo_data is None:
                self._inc_stats('repos_errors')
                with self._failed_lock:
                    self.failed_repos[repo_name] = {
                        'fail_type': 'repo_info_failed',
                        'timestamp': datetime.now().isoformat(),
                    }
                return False

            default_branch = repo_data.get('default_branch', 'main')
            is_archived = repo_data.get('archived', False)

            print(f"  ✓ stars={repo_data.get('stargazers_count', 0)} "
                  f"forks={repo_data.get('forks_count', 0)} "
                  f"branch={default_branch}")

            # 早期跳过: 已归档
            if is_archived and FILTER_ARCHIVED_REPOS:
                self._inc_stats('repos_archived')
                self.log(f"  ⊘ 已归档，跳过后续步骤")
                self.writers['filtered_out'].write({
                    'type': 'repo_archived',
                    'repository_name': repo_name,
                    'reason': '仓库已归档(停止维护)',
                    'timestamp': datetime.now().isoformat(),
                })
                self.checkpoint.mark_processed(repo_name)
                self._inc_stats('repos_success')
                return True

            # ── 步骤 2: 工作流运行记录 ──
            runs_result = self.fetch_workflow_runs(repo_name)
            if isinstance(runs_result, tuple) and len(runs_result) == 3:
                runs_saved, path_to_workflow_id, runs_by_workflow = runs_result
            else:
                # 兼容旧返回值
                runs_saved = runs_result if isinstance(runs_result, int) else 0
                path_to_workflow_id = {}
                runs_by_workflow = {}
            if runs_saved >= 0:
                self._inc_stats('runs_saved', runs_saved)
                print(f"  ✓ runs: {runs_saved} 条")

            # 过滤: 90天内运行次数少于阈值
            if FILTER_BY_RECENT_RUNS and runs_by_workflow:
                from datetime import timedelta
                cutoff = datetime.now().astimezone() - timedelta(days=RECENT_RUNS_DAYS)
                recent_count = 0
                for wf_path, runs in runs_by_workflow.items():
                    for run in runs:
                        created_at = run.get('created_at', '')
                        if created_at:
                            try:
                                run_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                                if run_time >= cutoff:
                                    recent_count += 1
                            except (ValueError, TypeError):
                                pass
                if recent_count < RECENT_RUNS_MIN_COUNT:
                    self._inc_stats('repos_filtered_runs')
                    self.log(f"  ⊘ 90天内运行{recent_count}次 < {RECENT_RUNS_MIN_COUNT}，跳过")
                    self.writers['filtered_out'].write({
                        'type': 'repo_low_runs',
                        'repository_name': repo_name,
                        'reason': f'{RECENT_RUNS_DAYS}天内运行{recent_count}次 < {RECENT_RUNS_MIN_COUNT}',
                        'recent_runs': recent_count,
                        'timestamp': datetime.now().isoformat(),
                    })
                    self.checkpoint.mark_processed(repo_name)
                    self._inc_stats('repos_success')
                    return True

            # ── 步骤 3: 工作流文件最新版本 ──
            wf_files = self.fetch_workflow_files(repo_name, default_branch, path_to_workflow_id)

            if not wf_files:
                self._inc_stats('repos_no_workflows')
                print(f"  - 无工作流文件")
                self.checkpoint.mark_processed(repo_name)
                self._inc_stats('repos_success')
                return True

            # 过滤: 没有对应运行记录的工作流文件
            if FILTER_WF_WITHOUT_RUNS and runs_by_workflow:
                before_count = len(wf_files)
                wf_files_with_runs = []
                for wf in wf_files:
                    wf_path = wf['path']
                    if wf_path in runs_by_workflow and runs_by_workflow[wf_path]:
                        wf_files_with_runs.append(wf)
                    else:
                        self._inc_stats('wf_filtered_no_runs')
                        self.writers['filtered_out'].write({
                            'type': 'wf_no_runs',
                            'repository_name': repo_name,
                            'workflow_path': wf_path,
                            'reason': '工作流文件无对应运行记录',
                            'timestamp': datetime.now().isoformat(),
                        })
                if len(wf_files_with_runs) < before_count:
                    print(f"  ⊘ 过滤掉 {before_count - len(wf_files_with_runs)} 个无运行记录的工作流文件")
                wf_files = wf_files_with_runs

            if not wf_files:
                self._inc_stats('repos_no_workflows')
                print(f"  - 过滤后无工作流文件")
                self.checkpoint.mark_processed(repo_name)
                self._inc_stats('repos_success')
                return True

            print(f"  ✓ 工作流文件: {len(wf_files)} 个")

            # ── 步骤 4: 工作流文件变更历史 ──
            if not SKIP_WORKFLOW_CHANGES:
                self.fetch_workflow_changes(repo_name, default_branch, wf_files, path_to_workflow_id,
                                            local_tree_cache=local_tree_cache)
            else:
                print(f"  ⊘ 跳过工作流变更历史（--skip-workflow-changes）")

            # ── 完成 ──
            self.checkpoint.mark_processed(repo_name)
            self._inc_stats('repos_success')

            total = self._get_stats('total_repos')
            if total % 50 == 0:
                self._save_state()

            return True

        except Exception as e:
            self.log(f"  ❌ 异常: {repo_name} - {e}", level='error')
            self._inc_stats('repos_errors')
            with self._failed_lock:
                self.failed_repos[repo_name] = {
                    'fail_type': str(e),
                    'timestamp': datetime.now().isoformat(),
                }
            return False

    def print_stats(self):
        self.log("\n" + "=" * 60)
        self.log("数据采集统计")
        self.log("=" * 60)
        self.log(f"仓库总数: {self.stats['total_repos']}")
        self.log(f"成功: {self.stats['repos_success']}")
        self.log(f"跳过(已处理): {self.stats['repos_skipped']}")
        self.log(f"已归档(跳过后续): {self.stats['repos_archived']}")
        self.log(f"运行次数不足(过滤): {self.stats['repos_filtered_runs']}")
        self.log(f"无工作流文件: {self.stats['repos_no_workflows']}")
        self.log(f"错误: {self.stats['repos_errors']}")
        self.log(f"运行记录总数: {self.stats['runs_saved']}")
        self.log(f"工作流文件总数: {self.stats['workflow_files_saved']}")
        self.log(f"工作流变更总数: {self.stats['workflow_changes_saved']}")
        self.log(f"无运行记录的工作流(过滤): {self.stats['wf_filtered_no_runs']}")
        self.log(f"唯一YAML文件: {self.wf_manager.unique_count}")
        self.log(f"blob缓存命中: {self.stats['api_saved_by_cache']}")
        self.log(f"tree缓存命中: {self.stats['api_saved_by_tree_cache']}")
        self.log(f"API调用数: {self.client.api_calls}")


# ─── 入口 ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='按仓库遍历统一采集数据')
    parser.add_argument('--input', type=str, default=str(INPUT_FILE))
    parser.add_argument('--output-dir', type=str, default=str(OUTPUT_DIR))
    parser.add_argument('--with-optional', action='store_true',
                        help='获取可选数据(labels/branches/releases/contributors/PRs计数)')
    parser.add_argument('--skip-workflow-changes', action='store_true',
                        help='跳过工作流变更历史采集，后续用 fetch_workflow_changes.py 补全')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS,
                        help=f'并发工作线程数 (默认: {MAX_WORKERS})')
    args = parser.parse_args()

    global FETCH_OPTIONAL_COUNTS, SKIP_WORKFLOW_CHANGES
    FETCH_OPTIONAL_COUNTS = args.with_optional
    SKIP_WORKFLOW_CHANGES = args.skip_workflow_changes

    input_file = Path(args.input)
    output_dir = Path(args.output_dir)

    if not input_file.exists():
        print(f"错误: 输入文件不存在 {input_file}")
        return

    loader = JSONLoader(input_file)
    total = loader.count_records()
    print(f"输入文件: {input_file}")
    print(f"输出目录: {output_dir}")
    print(f"仓库总数: {total}")
    print(f"并发线程: {args.workers}")
    print(f"可选数据: {'开启' if FETCH_OPTIONAL_COUNTS else '关闭(省5次API/仓库)'}")
    print()
    print("输出文件:")
    print(f"  repo_info.jsonl          — 仓库最新信息")
    print(f"  workflow_runs.jsonl      — 工作流运行记录")
    print(f"  workflow_files.jsonl     — 工作流文件映射")
    print(f"  workflow_changes.jsonl   — 工作流变更历史")
    print(f"  filtered_out.jsonl       — 被过滤的仓库/文件记录")
    print(f"  workflows/{'{owner}'}/{'{repo}'}/ — 工作流 YAML 文件")

    client = GitHubClient(log_file=output_dir / 'fetch_all.log')
    fetcher = RepoDataFetcher(client, output_dir)

    try:
        # 按 star 数降序排列，优先处理高星仓库
        records = loader.load_all()
        records.sort(key=lambda r: r.get('repo', {}).get('stargazers', 0), reverse=True)
        print(f"已按 star 数降序排列\n")

        if args.workers > 1:
            # 并发处理
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(fetcher.process_repo, record): record
                           for record in records}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        repo_name = futures[future].get('_id', 'unknown')
                        print(f"  ❌ 线程异常: {repo_name} - {e}")
        else:
            # 单线程（调试用）
            for record in records:
                fetcher.process_repo(record)
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断，进度已保存")
    finally:
        # 刷新所有写入器
        for writer in fetcher.writers.values():
            writer.flush()
        fetcher._save_state()
        fetcher.print_stats()
        # 关闭写入器
        for writer in fetcher.writers.values():
            writer.close()
        print(f"\n输出目录: {output_dir}")


if __name__ == "__main__":
    main()
