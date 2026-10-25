# scripts/data_update/fetch_workflow_changes.py
"""
补全工作流变更历史数据。

读取已有的 workflow_files.jsonl，找出缺少变更记录的仓库，
对这些仓库执行步骤 4（工作流变更历史采集）。

用法:
  python -m scripts.data_update.fetch_workflow_changes
  python -m scripts.data_update.fetch_workflow_changes --workers 4
  python -m scripts.data_update.fetch_workflow_changes --repo owner/repo
  python -m scripts.data_update.fetch_workflow_changes --force
"""
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[2]


import json
import hashlib
import base64
import sys
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.github_client import GitHubClient
from src.utils.checkpoint import CheckpointManager


# ─── 配置 ─────────────────────────────────────────────

OUTPUT_DIR = Path(str(_PORTABLE_PROJECT_ROOT / 'data_26_6_9'))
MAX_COMMITS_PER_FILE = 50   # 每个工作流文件最多获取 50 个 commit
MAX_WORKERS = 4             # 并发工作线程数


# ─── 文件写入器 ────────────────────────────────────────

class JsonlWriter:
    """线程安全的缓冲 JSONL 写入器"""

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
    """管理工作流 YAML 文件，与 fetch_all.py 共享同一套索引"""

    INDEX_FILE = '_hash_index.json'

    def __init__(self, workflows_dir: Path):
        self.workflows_dir = workflows_dir
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.hashes: Dict[str, Path] = {}
        self.blob_to_hash: Dict[str, str] = {}
        self.version_counters: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self):
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
                    if '|' in key_str:
                        repo_name, wf_path = key_str.split('|', 1)
                        self.version_counters[(repo_name, wf_path)] = seq
                print(f"从索引加载: {len(self.hashes)} 个文件, {len(self.blob_to_hash)} 个 blob 映射")
                return
            except Exception:
                pass

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
        key = (repo_name, wf_path)
        current = self.version_counters.get(key, 0)
        current += 1
        self.version_counters[key] = current
        return current

    def _make_file_path(self, repo_name: str, wf_path: str, version: int) -> Path:
        owner, repo = repo_name.split('/', 1)
        wf_filename = Path(wf_path).stem
        filename = f"{wf_filename}_{version:03d}.yml"
        return self.workflows_dir / owner / repo / filename

    def save(self, content: str, repo_name: str = '', wf_path: str = '') -> Tuple[str, Path]:
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
        with self._lock:
            return self.hashes.get(content_hash)

    def map_blob(self, blob_sha: str, content_hash: str):
        with self._lock:
            self.blob_to_hash[blob_sha] = content_hash

    def lookup_blob(self, blob_sha: str) -> Optional[str]:
        with self._lock:
            return self.blob_to_hash.get(blob_sha)

    @property
    def unique_count(self):
        return len(self.hashes)


# ─── 核心采集器 ────────────────────────────────────────

class WorkflowChangesFetcher:
    """补全工作流变更历史数据"""

    def __init__(self, client: GitHubClient, output_dir: Path):
        self.client = client
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.writers = {
            'workflow_changes': JsonlWriter(output_dir / 'workflow_changes.jsonl'),
        }

        self.wf_manager = WorkflowFileManager(output_dir / 'workflows')

        self.tree_cache: Dict[str, Dict[str, str]] = {}
        self._tree_cache_lock = threading.Lock()

        self.checkpoint = CheckpointManager(
            output_dir / 'checkpoints' / 'fetch_workflow_changes'
        )

        self.log_file = output_dir / 'fetch_workflow_changes.log'
        self._setup_logging()

        self._stats_lock = threading.Lock()
        self.stats = {
            'total_repos': 0,
            'repos_success': 0,
            'repos_skipped': 0,
            'repos_errors': 0,
            'workflow_changes_saved': 0,
            'api_saved_by_cache': 0,
            'api_saved_by_tree_cache': 0,
        }

        self._failed_lock = threading.Lock()
        self.failed_repos: Dict[str, Dict] = {}

    def _setup_logging(self):
        self.logger = logging.getLogger('WorkflowChangesFetcher')
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

    def _inc_stats(self, key: str, delta: int = 1):
        with self._stats_lock:
            self.stats[key] += delta

    def _get_stats(self, key: str) -> int:
        with self._stats_lock:
            return self.stats[key]

    def _save_state(self):
        self.checkpoint.save()
        self.wf_manager._save_index()
        with self._failed_lock:
            if self.failed_repos:
                failed_file = self.output_dir / 'failed_repos_changes.json'
                with open(failed_file, 'w', encoding='utf-8') as f:
                    json.dump(self.failed_repos, f, indent=2, ensure_ascii=False)

    # ─── Blob API ──────────────────────────────────────

    def _fetch_file_at_commit_via_blob(self, git_url: str) -> Optional[str]:
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

    # ─── Git Tree API 批量获取 blob SHA ────────────────

    def _batch_get_blob_shas(self, repo_name: str, commit_shas: List[str],
                              wf_path: str,
                              local_tree_cache: Dict[str, Dict[str, str]] = None) -> Dict[str, Optional[str]]:
        tree_cache = local_tree_cache if local_tree_cache is not None else self.tree_cache
        blob_sha_map = {}
        uncached_shas = [sha for sha in commit_shas if sha not in tree_cache]
        self._inc_stats('api_saved_by_tree_cache', len(commit_shas) - len(uncached_shas))

        for commit_sha in uncached_shas:
            tree_data = self.client.get_json(
                f'https://api.github.com/repos/{repo_name}/git/trees/{commit_sha}',
                params={'recursive': '1'}
            )

            if not tree_data or 'tree' not in tree_data:
                tree_cache[commit_sha] = {}
                continue

            wf_files_map = {}
            for item in tree_data['tree']:
                if (item.get('type') == 'blob'
                        and item.get('path', '').startswith('.github/workflows/')):
                    wf_files_map[item['path']] = item.get('sha')

            tree_cache[commit_sha] = wf_files_map

            if tree_data.get('truncated') and wf_path not in wf_files_map:
                commit_detail = self.client.get_json(
                    f'https://api.github.com/repos/{repo_name}/commits/{commit_sha}'
                )
                if commit_detail:
                    for f in commit_detail.get('files', []):
                        if f.get('filename') == wf_path:
                            wf_files_map[wf_path] = f.get('sha')
                            break

        for commit_sha in commit_shas:
            blob_sha_map[commit_sha] = tree_cache.get(commit_sha, {}).get(wf_path)

        return blob_sha_map

    def _get_content_by_blob_sha(self, repo_name: str, blob_sha: str,
                                  wf_path: str = '') -> Optional[Tuple[str, str]]:
        cached_hash = self.wf_manager.lookup_blob(blob_sha)
        if cached_hash:
            self._inc_stats('api_saved_by_cache')
            wf_file = self.wf_manager.hashes.get(cached_hash)
            if wf_file and wf_file.exists():
                content = wf_file.read_text(encoding='utf-8')
                return content, cached_hash

        blob_url = f'https://api.github.com/repos/{repo_name}/git/blobs/{blob_sha}'
        content = self._fetch_file_at_commit_via_blob(blob_url)
        if content:
            content_hash, _ = self.wf_manager.save(content, repo_name, wf_path)
            self.wf_manager.map_blob(blob_sha, content_hash)
            return content, content_hash

        return None

    # ─── 工作流变更历史采集 ────────────────────────────

    def fetch_workflow_changes_for_repo(self, repo_name: str, default_branch: str,
                                        wf_files_info: List[dict],
                                        path_to_workflow_id: Dict[str, int] = None):
        """对单个仓库的所有工作流文件采集变更历史"""
        local_tree_cache: Dict[str, Dict[str, str]] = {}

        for wf_info in wf_files_info:
            wf_path = wf_info['workflow_path']
            latest_content_hash = wf_info.get('content_hash', '')
            latest_blob_sha = wf_info.get('file_sha', '')

            # 从已保存的文件中读取最新版本内容
            latest_content = None
            if latest_content_hash:
                wf_file = self.wf_manager.lookup_by_hash(latest_content_hash)
                if wf_file and wf_file.exists():
                    latest_content = wf_file.read_text(encoding='utf-8')

            # 获取 commit 历史
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

            # 批量获取每个 commit 对应文件的 blob SHA
            commit_shas = [m['sha'] for m in commit_meta]
            blob_sha_map = self._batch_get_blob_shas(repo_name, commit_shas, wf_path,
                                                      local_tree_cache=local_tree_cache)

            # 遍历 commit，只对 blob SHA 变化的请求内容
            prev_hash = None
            change_count = 0

            for i, meta in enumerate(commit_meta):
                commit_sha = meta['sha']
                blob_sha = blob_sha_map.get(commit_sha)

                # 复用最新版本（最后一个 commit 且 blob SHA 匹配）
                if (i == len(commit_meta) - 1 and latest_content
                        and (blob_sha is None or blob_sha == latest_blob_sha)):
                    content = latest_content
                    content_hash = latest_content_hash
                    blob_sha = latest_blob_sha or blob_sha
                elif blob_sha:
                    result = self._get_content_by_blob_sha(repo_name, blob_sha, wf_path)
                    if result is None:
                        continue
                    content, content_hash = result
                else:
                    content = self.client.get_file_content(repo_name, wf_path, ref=commit_sha)
                    if content is None:
                        continue
                    content_hash, _ = self.wf_manager.save(content, repo_name, wf_path)

                # 只记录实际变更
                is_change = (content_hash != prev_hash) if prev_hash is not None else True

                if is_change:
                    change_count += 1
                    self._inc_stats('workflow_changes_saved')

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

    # ─── 仓库处理主流程 ────────────────────────────────

    def process_repo(self, repo_name: str, default_branch: str,
                     wf_files_info: List[dict],
                     path_to_workflow_id: Dict[str, int] = None) -> bool:
        self._inc_stats('total_repos')

        if self.checkpoint.is_processed(repo_name):
            self._inc_stats('repos_skipped')
            return True

        print(f"\n[{self._get_stats('total_repos')}] {repo_name} ({len(wf_files_info)} 个工作流文件)")

        try:
            self.fetch_workflow_changes_for_repo(
                repo_name, default_branch, wf_files_info, path_to_workflow_id
            )

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
        self.log("工作流变更补全统计")
        self.log("=" * 60)
        self.log(f"仓库总数: {self.stats['total_repos']}")
        self.log(f"成功: {self.stats['repos_success']}")
        self.log(f"跳过(已处理): {self.stats['repos_skipped']}")
        self.log(f"错误: {self.stats['repos_errors']}")
        self.log(f"工作流变更总数: {self.stats['workflow_changes_saved']}")
        self.log(f"唯一YAML文件: {self.wf_manager.unique_count}")
        self.log(f"blob缓存命中: {self.stats['api_saved_by_cache']}")
        self.log(f"tree缓存命中: {self.stats['api_saved_by_tree_cache']}")
        self.log(f"API调用数: {self.client.api_calls}")


# ─── 数据加载辅助函数 ──────────────────────────────────

def load_existing_changes(output_dir: Path) -> Set[str]:
    """加载已有变更记录的仓库名集合"""
    existing_repos: Set[str] = set()
    changes_file = output_dir / 'workflow_changes.jsonl'
    if not changes_file.exists():
        return existing_repos

    with open(changes_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                repo = record.get('repository_name', '')
                if repo:
                    existing_repos.add(repo)
            except json.JSONDecodeError:
                continue

    return existing_repos


def load_workflow_files(output_dir: Path) -> Dict[str, List[dict]]:
    """从 workflow_files.jsonl 加载每个仓库的工作流文件信息"""
    repos: Dict[str, List[dict]] = {}
    wf_file = output_dir / 'workflow_files.jsonl'
    if not wf_file.exists():
        return repos

    with open(wf_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                repo = record.get('repository_name', '')
                if repo:
                    if repo not in repos:
                        repos[repo] = []
                    repos[repo].append(record)
            except json.JSONDecodeError:
                continue

    return repos


def load_repo_default_branches(output_dir: Path) -> Dict[str, str]:
    """从 repo_info.jsonl 加载每个仓库的默认分支"""
    branches: Dict[str, str] = {}
    repo_info_file = output_dir / 'repo_info.jsonl'
    if not repo_info_file.exists():
        return branches

    with open(repo_info_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                repo_name = record.get('_id', '')
                repo = record.get('repo', {})
                default_branch = repo.get('defaultBranch', 'main')
                if repo_name:
                    branches[repo_name] = default_branch
            except json.JSONDecodeError:
                continue

    return branches


def load_workflow_ids(output_dir: Path) -> Dict[str, Dict[str, int]]:
    """从 workflow_files.jsonl 加载 workflow_path → workflow_id 映射"""
    result: Dict[str, Dict[str, int]] = {}
    wf_file = output_dir / 'workflow_files.jsonl'
    if not wf_file.exists():
        return result

    with open(wf_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                repo = record.get('repository_name', '')
                wf_path = record.get('workflow_path', '')
                wf_id = record.get('workflow_id')
                if repo and wf_path and wf_id is not None:
                    if repo not in result:
                        result[repo] = {}
                    result[repo][wf_path] = wf_id
            except json.JSONDecodeError:
                continue

    return result


def load_repository_filter(path: Path) -> Set[str]:
    """Load repository names from a JSONL supplement/candidate file."""

    repositories: Set[str] = set()
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            repository = record.get('repository_name') or record.get('_id')
            if repository:
                repositories.add(str(repository).strip().lower())
    return repositories


# ─── 入口 ─────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='补全工作流变更历史数据')
    parser.add_argument('--output-dir', type=str, default=str(OUTPUT_DIR))
    parser.add_argument('--workers', type=int, default=MAX_WORKERS,
                        help=f'并发工作线程数 (默认: {MAX_WORKERS})')
    parser.add_argument('--repo', type=str, default=None,
                        help='只补全指定仓库 (格式: owner/repo)')
    parser.add_argument('--force', action='store_true',
                        help='强制重新采集，即使已有变更记录')
    parser.add_argument('--repo-list', type=str, default=None,
                        help='仅处理该 JSONL 文件中的 repository_name（用于限定补抓范围）')
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    print(f"输出目录: {output_dir}")
    print(f"并发线程: {args.workers}")

    # 加载已有数据
    print("\n加载已有数据...")
    wf_files_by_repo = load_workflow_files(output_dir)
    default_branches = load_repo_default_branches(output_dir)
    workflow_ids = load_workflow_ids(output_dir)

    print(f"已有工作流文件的仓库: {len(wf_files_by_repo)} 个")

    if not args.force:
        existing_changes = load_existing_changes(output_dir)
        print(f"已有变更记录的仓库: {len(existing_changes)} 个")
        # 过滤掉已有变更记录的仓库
        repos_to_fetch = {
            repo: files for repo, files in wf_files_by_repo.items()
            if repo not in existing_changes
        }
    else:
        repos_to_fetch = wf_files_by_repo

    if args.repo_list:
        allowed = load_repository_filter(Path(args.repo_list))
        repos_to_fetch = {
            repo: files
            for repo, files in repos_to_fetch.items()
            if str(repo).strip().lower() in allowed
        }
        print(f"仓库过滤列表: {len(allowed)} 个")

    print(f"需要补全变更记录的仓库: {len(repos_to_fetch)} 个")

    if args.repo:
        if args.repo in repos_to_fetch:
            repos_to_fetch = {args.repo: repos_to_fetch[args.repo]}
            print(f"仅处理: {args.repo}")
        else:
            print(f"错误: 仓库 {args.repo} 不在待处理列表中")
            return

    if not repos_to_fetch:
        print("无需补全，所有仓库已有变更记录")
        return

    client = GitHubClient(log_file=output_dir / 'fetch_workflow_changes.log')
    fetcher = WorkflowChangesFetcher(client, output_dir)

    try:
        tasks = []
        for repo_name, wf_files_info in repos_to_fetch.items():
            default_branch = default_branches.get(repo_name, 'main')
            path_to_workflow_id = workflow_ids.get(repo_name, {})
            tasks.append((repo_name, default_branch, wf_files_info, path_to_workflow_id))

        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(fetcher.process_repo, repo, branch, wf_info, wf_ids): repo
                    for repo, branch, wf_info, wf_ids in tasks
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        repo_name = futures[future]
                        print(f"  ❌ 线程异常: {repo_name} - {e}")
        else:
            for repo, branch, wf_info, wf_ids in tasks:
                fetcher.process_repo(repo, branch, wf_info, wf_ids)

    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断，进度已保存")
    finally:
        for writer in fetcher.writers.values():
            writer.flush()
        fetcher._save_state()
        fetcher.print_stats()
        for writer in fetcher.writers.values():
            writer.close()
        print(f"\n输出目录: {output_dir}")


if __name__ == "__main__":
    main()
