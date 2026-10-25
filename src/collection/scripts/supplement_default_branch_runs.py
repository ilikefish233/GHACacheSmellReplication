"""补充默认分支工作流运行记录，供 RQ2 的完整覆盖分析使用。

``workflow_runs.jsonl`` 只覆盖了数据采集时已经发现的运行。该脚本针对
``repo_info.jsonl`` 中没有可用默认分支工作流运行（或运行中的工作流路径、
``head_sha`` 不完整）的仓库，调用 GitHub Actions API 获取截止时间前的
完成运行。脚本可断点续传，输出与原始运行文件分离，不修改原始数据。

每条补充运行保留 ``workflow_id``、``workflow_path`` 和 ``head_sha``，之后
由 ``prepare_rq2_supplemented.py`` 获取对应提交的 YAML。所有时间和分支
过滤都在本地再次校验，避免 API 查询条件失效时引入窗口外数据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from scripts.supplement_workflow_snapshots import load_token  # noqa: E402


DEFAULT_DATA_DIR = REPOSITORY_ROOT / 'data_26_6_9'
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT
    / 'output_full_logical_20260831'
    / 'rq2_default_branch_run_supplement_20260609'
)
WORKFLOW_PREFIX = '.github/workflows/'
# ``forbidden`` is deliberately not terminal: a 403 from the core API can be
# a transient rate-limit response.  It must be retried on a later invocation
# instead of permanently excluding that repository from RQ2.
TERMINAL_STATUSES = {'ok', 'no_runs', 'not_found'}


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON at {path}:{line_number}: {exc}') from exc
            if isinstance(value, dict):
                yield value


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(
            value.strip().replace('Z', '+00:00')
        ).astimezone(timezone.utc)
    except ValueError:
        return None


def timestamp_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat().replace('+00:00', 'Z')


def normalize_repo(value: Any) -> str:
    return str(value or '').strip().lower()


@dataclass(frozen=True)
class RepositoryInfo:
    repository_id: str
    repository_name: str
    default_branch: str
    aliases: Tuple[str, ...]


def load_repositories(path: Path) -> Tuple[Dict[str, RepositoryInfo], Dict[str, RepositoryInfo]]:
    by_id: Dict[str, RepositoryInfo] = {}
    pending: Dict[str, Dict[str, Any]] = {}
    for entry in load_jsonl(path):
        repo = entry.get('repo') or {}
        repository_id = str(repo.get('id') or '')
        original_name = str(entry.get('_id') or '')
        api_name = str(repo.get('name') or '')
        default_branch = str(repo.get('defaultBranch') or '')
        if not repository_id or not default_branch:
            continue
        item = pending.setdefault(repository_id, {
            'repository_name': api_name or original_name,
            'default_branch': default_branch,
            'aliases': set(),
        })
        if api_name:
            item['repository_name'] = api_name
        if original_name:
            item['aliases'].add(original_name)
        if api_name:
            item['aliases'].add(api_name)
    for repository_id, item in pending.items():
        repository_name = str(item['repository_name'] or '')
        if not repository_name:
            continue
        by_id[repository_id] = RepositoryInfo(
            repository_id=repository_id,
            repository_name=repository_name,
            default_branch=str(item['default_branch']),
            aliases=tuple(sorted(item['aliases'])),
        )
    by_alias: Dict[str, RepositoryInfo] = {}
    for repository in by_id.values():
        by_alias[normalize_repo(repository.repository_name)] = repository
        for alias in repository.aliases:
            by_alias[normalize_repo(alias)] = repository
    return by_id, by_alias


def load_repository_filter(
    path: Optional[Path],
    by_id: Mapping[str, RepositoryInfo],
    by_alias: Mapping[str, RepositoryInfo],
) -> Optional[set[str]]:
    """Load an optional repository allow-list from JSONL or plain text.

    The static scan post-processing line writes one repository name per line,
    while JSONL records from other stages may contain ``repository_id`` or
    ``repository_name``.  Both forms are accepted so the supplement remains
    reusable without coupling it to one result-file shape.
    """
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(path)
    selected: set[str] = set()
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        value: Any = line
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            pass
        if isinstance(value, dict):
            repository_id = str(value.get('repository_id') or '')
            repository_name = normalize_repo(value.get('repository_name'))
            if repository_id in by_id:
                selected.add(repository_id)
            elif repository_name in by_alias:
                selected.add(by_alias[repository_name].repository_id)
        else:
            token = str(value).strip()
            if token in by_id:
                selected.add(token)
            elif normalize_repo(token) in by_alias:
                selected.add(by_alias[normalize_repo(token)].repository_id)
    return selected


def load_repository_workflow_filters(
    path: Optional[Path],
    by_id: Mapping[str, RepositoryInfo],
    by_alias: Mapping[str, RepositoryInfo],
) -> Dict[str, List[Dict[str, Any]]]:
    """Load workflow IDs associated with each repository in an allow-list."""
    if path is None:
        return {}
    filters: Dict[str, List[Dict[str, Any]]] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        repository_id = str(value.get('repository_id') or '')
        if repository_id not in by_id:
            repository = by_alias.get(normalize_repo(value.get('repository_name')))
            repository_id = repository.repository_id if repository else ''
        if not repository_id:
            continue
        workflow_values = value.get('workflow_filters') or []
        normalized: List[Dict[str, Any]] = []
        has_unresolved_workflow = False
        for workflow in workflow_values:
            if not isinstance(workflow, dict):
                continue
            workflow_id = str(workflow.get('workflow_id') or '').strip()
            workflow_path = str(workflow.get('workflow_path') or '').strip()
            if workflow_id:
                normalized.append({
                    'workflow_id': workflow_id,
                    'workflow_path': workflow_path,
                })
            elif workflow_path:
                has_unresolved_workflow = True
        # If even one exposed workflow lacks an ID, use the repository-wide
        # endpoint and filter returned runs by path.  Otherwise a mixed mode
        # would silently omit the ID-less exposed workflow.
        if normalized and not has_unresolved_workflow:
            filters[repository_id] = normalized
    return filters


def load_repository_workflow_paths(
    path: Optional[Path],
    by_id: Mapping[str, RepositoryInfo],
    by_alias: Mapping[str, RepositoryInfo],
) -> Dict[str, Set[str]]:
    """Load exposed workflow paths for repository-wide fallback queries."""
    if path is None:
        return {}
    paths: Dict[str, Set[str]] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        repository_id = str(value.get('repository_id') or '')
        if repository_id not in by_id:
            repository = by_alias.get(normalize_repo(value.get('repository_name')))
            repository_id = repository.repository_id if repository else ''
        if not repository_id:
            continue
        workflow_paths = {
            str(workflow.get('workflow_path') or '').strip()
            for workflow in (value.get('workflow_filters') or [])
            if isinstance(workflow, dict) and str(workflow.get('workflow_path') or '').strip()
        }
        if workflow_paths:
            paths[repository_id] = workflow_paths
    return paths


def run_key(entry: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    run_id = entry.get('run_id')
    if run_id in (None, ''):
        run_id = entry.get('id')
    if run_id in (None, ''):
        return None
    return str(run_id), str(entry.get('run_attempt') or 1)


def load_run_keys(paths: Sequence[Path]) -> set[Tuple[str, str]]:
    keys: set[Tuple[str, str]] = set()
    for path in paths:
        if not path.exists():
            continue
        for entry in load_jsonl(path):
            key = run_key(entry)
            if key is not None:
                keys.add(key)
    return keys


def load_terminal_repository_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    latest: Dict[str, str] = {}
    for entry in load_jsonl(path):
        repository_id = str(entry.get('repository_id') or '')
        if repository_id:
            latest[repository_id] = str(entry.get('status') or '')
    return {
        repository_id for repository_id, status in latest.items()
        if status in TERMINAL_STATUSES
    }


def load_retry_repository_ids(path: Path) -> set[str]:
    """Return repositories whose latest fetch ended in a retryable status."""
    if not path.exists():
        return set()
    latest: Dict[str, str] = {}
    for entry in load_jsonl(path):
        repository_id = str(entry.get('repository_id') or '')
        if repository_id:
            latest[repository_id] = str(entry.get('status') or '')
    return {
        repository_id for repository_id, status in latest.items()
        if status in {'error', 'forbidden'}
    }


def has_usable_default_run(
    entry: Mapping[str, Any],
    repository: RepositoryInfo,
    as_of: datetime,
) -> bool:
    if str(entry.get('status') or '').lower() != 'completed':
        return False
    created_at = parse_timestamp(entry.get('created_at'))
    if created_at is None or created_at > as_of:
        return False
    if str(entry.get('head_branch') or '') != repository.default_branch:
        return False
    workflow_path = str(entry.get('workflow_path') or entry.get('path') or '')
    return workflow_path.startswith(WORKFLOW_PREFIX) and bool(
        str(entry.get('head_sha') or '')
    )


def build_targets(
    data_dir: Path,
    run_sources: Sequence[Path],
    as_of: datetime,
    include_all: bool,
    requested_repository_ids: Optional[set[str]] = None,
    force_repository_ids: Optional[set[str]] = None,
) -> Tuple[List[RepositoryInfo], Dict[str, Any]]:
    by_id, by_alias = load_repositories(data_dir / 'repo_info.jsonl')
    covered: set[str] = set()
    source_stats = {str(path): 0 for path in run_sources}
    for path in run_sources:
        if not path.exists():
            continue
        for entry in load_jsonl(path):
            repository = by_alias.get(normalize_repo(entry.get('repository_name')))
            if repository is None:
                continue
            if has_usable_default_run(entry, repository, as_of):
                covered.add(repository.repository_id)
                source_stats[str(path)] += 1
    if include_all:
        targets = list(by_id.values())
    else:
        forced = force_repository_ids or set()
        targets = [
            repository for repository in by_id.values()
            if repository.repository_id not in covered
            or repository.repository_id in forced
        ]
    if requested_repository_ids is not None:
        targets = [
            repository for repository in targets
            if repository.repository_id in requested_repository_ids
        ]
    targets.sort(key=lambda repository: (repository.repository_name.lower(), repository.repository_id))
    stats = {
        'repo_info_repositories': len(by_id),
        'covered_by_existing_usable_default_runs': len(covered),
        'target_repositories': len(targets),
        'requested_repository_filter_count': (
            len(requested_repository_ids) if requested_repository_ids is not None else None
        ),
        'forced_retry_repositories': len(force_repository_ids or set()),
        'existing_source_usable_run_counts': source_stats,
        'selection_rule': (
            'target repositories without a completed default-branch run before '
            'the fixed cutoff with a standard workflow_path and head_sha'
        ),
    }
    return targets, stats


def duration_seconds(run: Mapping[str, Any]) -> Optional[float]:
    started = parse_timestamp(run.get('run_started_at'))
    updated = parse_timestamp(run.get('updated_at'))
    if started is None or updated is None:
        return None
    value = (updated - started).total_seconds()
    return max(0.0, value)


def normalize_run(repository: RepositoryInfo, run: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    run_id = run.get('id')
    path = str(run.get('path') or '')
    head_sha = str(run.get('head_sha') or '')
    created_at = str(run.get('created_at') or '')
    if run_id in (None, '') or not path or not head_sha or not created_at:
        return None
    return {
        '_id': f"{repository.repository_id}:{run_id}:{run.get('run_attempt') or 1}",
        'repository_name': repository.repository_name,
        'repository_id': repository.repository_id,
        'workflow_id': run.get('workflow_id'),
        'workflow_path': path,
        'workflow_name': run.get('name'),
        'run_id': run_id,
        'run_number': run.get('run_number'),
        'run_attempt': run.get('run_attempt') or 1,
        'status': str(run.get('status') or '').lower(),
        'conclusion': str(run.get('conclusion') or '').lower(),
        'event': str(run.get('event') or '').lower(),
        'head_branch': run.get('head_branch'),
        'head_sha': head_sha,
        'created_at': created_at,
        'updated_at': run.get('updated_at'),
        'run_started_at': run.get('run_started_at'),
        'duration_seconds': duration_seconds(run),
        'url': run.get('url'),
        'html_url': run.get('html_url'),
        'source': 'github_actions_api_default_branch',
    }


@dataclass
class RepositoryFetchResult:
    repository: RepositoryInfo
    status: str
    records: List[Dict[str, Any]]
    pages: int
    error: Optional[str] = None
    http_status: Optional[int] = None
    workflow_statuses: List[Dict[str, Any]] = None
    pagination_complete: Optional[bool] = None
    termination_reason: Optional[str] = None
    last_page_size: Optional[int] = None
    # Optional raw API objects for the unified acquisition executor.  This is
    # appended with a default to preserve the historical call signature.
    raw_records: Optional[List[Dict[str, Any]]] = None
    rate_limit_headers: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class EndpointFetchResult:
    """Result of one workflow or repository-level runs endpoint query.

    ``pagination_complete`` is intentionally independent from ``pages``.  A
    query which ends on a short final page is complete even when that page is
    the configured ``max_pages`` page.
    """

    records: List[Dict[str, Any]]
    pages: int
    status: str
    error: Optional[str]
    http_status: Optional[int]
    pagination_complete: bool
    termination_reason: str
    last_page_size: int
    raw_records: Optional[List[Dict[str, Any]]] = None
    rate_limit_headers: Optional[Dict[str, str]] = None


class GitHubActionsClient:
    def __init__(
        self,
        token: str,
        timeout: int = 45,
        retries: int = 3,
        request_observer: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ):
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.request_observer = request_observer

    def _observe_request(self, event: Mapping[str, Any]) -> None:
        """Emit token-free request metadata without changing fetch behavior."""
        if self.request_observer is None:
            return
        try:
            self.request_observer(dict(event))
        except Exception:
            # Observability must never interrupt the historical supplement.
            return

    def _get_json(self, url: str) -> Tuple[Dict[str, Any], Mapping[str, str]]:
        headers = {
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'gha-log-analysis-default-run-supplement/1.0',
            'Authorization': f'Bearer {self.token}',
        }
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            request = Request(url, headers=headers, method='GET')
            started_at = timestamp_text(datetime.now(timezone.utc))
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode('utf-8'))
                    self._observe_request({
                        'event_type': 'http_response',
                        'url': url,
                        'attempt': attempt + 1,
                        'http_status': int(getattr(response, 'status', 200) or 200),
                        'started_at': started_at,
                        'finished_at': timestamp_text(datetime.now(timezone.utc)),
                        'rate_limit_headers': {
                            name: response.headers.get(name)
                            for name in (
                                'X-RateLimit-Limit', 'X-RateLimit-Remaining',
                                'X-RateLimit-Reset', 'X-RateLimit-Used',
                                'X-RateLimit-Resource', 'Retry-After',
                            )
                            if response.headers.get(name) is not None
                        },
                    })
                    return payload, response.headers
            except HTTPError as exc:
                last_error = exc
                self._observe_request({
                    'event_type': 'http_response',
                    'url': url,
                    'attempt': attempt + 1,
                    'http_status': int(exc.code),
                    'started_at': started_at,
                    'finished_at': timestamp_text(datetime.now(timezone.utc)),
                    'error': f'HTTP {exc.code}',
                    'retry_after': exc.headers.get('Retry-After'),
                    'rate_limit_headers': {
                        name: exc.headers.get(name)
                        for name in (
                            'X-RateLimit-Limit', 'X-RateLimit-Remaining',
                            'X-RateLimit-Reset', 'X-RateLimit-Used',
                            'X-RateLimit-Resource', 'Retry-After',
                        )
                        if exc.headers.get(name) is not None
                    },
                })
                # A 403 with remaining core quota is normally repository
                # permission/SAML/access denial rather than a rate limit.
                # Retrying it blocks the whole batch without adding coverage;
                # the caller records it as ``forbidden`` for explicit review.
                if exc.code == 403:
                    remaining = exc.headers.get('X-RateLimit-Remaining')
                    if remaining and remaining.isdigit() and int(remaining) > 0:
                        raise
                if exc.code not in {403, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise
                retry_after = exc.headers.get('Retry-After')
                delay = 2 ** attempt
                if retry_after and retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                reset = exc.headers.get('X-RateLimit-Reset')
                if reset and reset.isdigit():
                    delay = max(delay, int(reset) - int(time.time()) + 2)
                time.sleep(min(max(delay, 2), 60))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                self._observe_request({
                    'event_type': 'request_error',
                    'url': url,
                    'attempt': attempt + 1,
                    'started_at': started_at,
                    'finished_at': timestamp_text(datetime.now(timezone.utc)),
                    'error': type(exc).__name__ + ': ' + str(exc),
                })
                if attempt >= self.retries:
                    raise
                time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(str(last_error) if last_error else 'GitHub API request failed')

    def _fetch_endpoint(
        self,
        endpoint: str,
        repository: RepositoryInfo,
        as_of: datetime,
        window_start: Optional[datetime],
        max_pages: int,
        allowed_workflow_paths: Optional[Set[str]] = None,
    ) -> EndpointFetchResult:
        """Fetch completed runs from one repository or workflow endpoint."""
        records: List[Dict[str, Any]] = []
        raw_records: List[Dict[str, Any]] = []
        pages = 0
        last_page_size = 0
        rate_limit_headers: Dict[str, str] = {}
        # GitHub's workflow-runs endpoint returns an empty result for an
        # open-ended range such as ``..2026-06-09``.  Use the RQ2 lower bound
        # when supplied so repositories with older runs are not re-downloaded.
        created_start = (
            window_start.date().isoformat() if window_start is not None else '2000-01-01'
        )
        query = urlencode({
            'branch': repository.default_branch,
            'status': 'completed',
            'created': f'{created_start}..{as_of.date().isoformat()}',
            'per_page': 100,
        })
        for page in range(1, max_pages + 1):
            url = f'{endpoint}?{query}&page={page}'
            try:
                payload, _ = self._get_json(url)
            except HTTPError as exc:
                rate_limit_headers = {
                    name: str(exc.headers.get(name))
                    for name in (
                        'X-RateLimit-Limit', 'X-RateLimit-Remaining',
                        'X-RateLimit-Reset', 'X-RateLimit-Used',
                        'X-RateLimit-Resource', 'Retry-After',
                    )
                    if exc.headers.get(name) is not None
                }
                if exc.code in {403, 404, 410, 451}:
                    status = 'forbidden' if exc.code == 403 else 'not_found'
                    return EndpointFetchResult(
                        records,
                        pages,
                        status,
                        f'HTTP {exc.code}',
                        exc.code,
                        False,
                        'api_error',
                        last_page_size,
                        raw_records,
                        rate_limit_headers,
                    )
                return EndpointFetchResult(
                    records,
                    pages,
                    'error',
                    f'HTTP {exc.code}',
                    exc.code,
                    False,
                    'api_error',
                    last_page_size,
                    raw_records,
                    rate_limit_headers,
                )
            except Exception as exc:
                return EndpointFetchResult(
                    records,
                    pages,
                    'error',
                    str(exc),
                    None,
                    False,
                    'api_error',
                    last_page_size,
                )

            pages += 1
            runs = payload.get('workflow_runs') or []
            raw_records.extend(
                dict(run) for run in runs if isinstance(run, Mapping)
            )
            last_page_size = len(runs)
            self._observe_request({
                'event_type': 'page_result',
                'url': url,
                'page': page,
                'record_count': len(runs),
                'last_page_size': len(runs),
            })
            if not runs:
                return EndpointFetchResult(
                    records,
                    pages,
                    'ok' if records else 'no_runs',
                    None,
                    None,
                    True,
                    'empty_page',
                    last_page_size,
                    raw_records,
                )
            for run in runs:
                created_at = parse_timestamp(run.get('created_at'))
                if created_at is None or created_at > as_of or (
                    window_start is not None and created_at < window_start
                ):
                    continue
                if str(run.get('head_branch') or '') != repository.default_branch:
                    continue
                normalized = normalize_run(repository, run)
                if (
                    normalized is not None
                    and normalized['workflow_path'].startswith(WORKFLOW_PREFIX)
                    and (
                        allowed_workflow_paths is None
                        or normalized['workflow_path'] in allowed_workflow_paths
                    )
                ):
                    records.append(normalized)
            if len(runs) < 100:
                return EndpointFetchResult(
                    records,
                    pages,
                    'ok' if records else 'no_runs',
                    None,
                    None,
                    True,
                    'short_page',
                    last_page_size,
                    raw_records,
                )
        return EndpointFetchResult(
            records,
            pages,
            'ok' if records else 'no_runs',
            None,
            None,
            False,
            'max_pages_reached',
            last_page_size,
            raw_records,
        )

    def fetch_repository(
        self,
        repository: RepositoryInfo,
        as_of: datetime,
        window_start: Optional[datetime],
        max_pages: int,
        workflow_filters: Optional[Sequence[Mapping[str, Any]]] = None,
        workflow_paths: Optional[Set[str]] = None,
    ) -> RepositoryFetchResult:
        """Fetch runs, using workflow-specific endpoints when available.

        Static exposure records carry the workflow IDs.  Querying those IDs
        avoids downloading unrelated runs from a high-traffic repository and
        makes the supplementation line target the same workflow identities as
        the static line.  Repositories without IDs retain the repository-wide
        fallback for compatibility with older allow-lists.
        """
        repository_endpoint = (
            f"https://api.github.com/repos/{quote(repository.repository_name, safe='/')}"
            '/actions/runs'
        )
        filters = [
            workflow for workflow in (workflow_filters or [])
            if str(workflow.get('workflow_id') or '').strip()
        ]
        if not filters:
            endpoint_result = self._fetch_endpoint(
                repository_endpoint,
                repository,
                as_of,
                window_start,
                max_pages,
                allowed_workflow_paths=workflow_paths,
            )
            records = endpoint_result.records
            pages = endpoint_result.pages
            status = endpoint_result.status
            error = endpoint_result.error
            http_status = endpoint_result.http_status
            returned_paths = {
                str(record.get('workflow_path') or '')
                for record in records
                if str(record.get('workflow_path') or '')
            }
            workflow_statuses = [
                {
                    'workflow_id': None,
                    'workflow_path': path,
                    'status': (
                        'ok' if path in returned_paths
                        else ('unresolved' if status in {'error', 'forbidden', 'not_found'}
                              or not endpoint_result.pagination_complete else 'no_runs')
                    ),
                    'records': sum(
                        1 for record in records
                        if str(record.get('workflow_path') or '') == path
                    ),
                    'pages': pages,
                    'complete': endpoint_result.pagination_complete,
                    'pagination_complete': endpoint_result.pagination_complete,
                    'termination_reason': endpoint_result.termination_reason,
                    'last_page_size': endpoint_result.last_page_size,
                    }
                for path in sorted(workflow_paths or set())
            ]
            return RepositoryFetchResult(
                repository, status, records, pages, error, http_status,
                workflow_statuses,
                endpoint_result.pagination_complete,
                endpoint_result.termination_reason,
                endpoint_result.last_page_size,
                endpoint_result.raw_records or [],
                endpoint_result.rate_limit_headers or {},
            )

        records: List[Dict[str, Any]] = []
        raw_records: List[Dict[str, Any]] = []
        seen_runs: set[Tuple[str, str]] = set()
        total_pages = 0
        workflow_statuses: List[Dict[str, Any]] = []
        for workflow in filters:
            workflow_id = str(workflow['workflow_id'])
            workflow_path = str(workflow.get('workflow_path') or '')
            endpoint = (
                f"https://api.github.com/repos/{quote(repository.repository_name, safe='/')}"
                f"/actions/workflows/{quote(workflow_id, safe='')}/runs"
            )
            endpoint_result = self._fetch_endpoint(
                endpoint, repository, as_of, window_start, max_pages,
            )
            fetched = endpoint_result.records
            raw_records.extend(endpoint_result.raw_records or [])
            pages = endpoint_result.pages
            status = endpoint_result.status
            error = endpoint_result.error
            http_status = endpoint_result.http_status
            rate_limit_headers = endpoint_result.rate_limit_headers or {}
            total_pages += pages
            workflow_statuses.append({
                'workflow_id': workflow_id,
                'workflow_path': workflow_path,
                'status': status,
                'records': len(fetched),
                'pages': pages,
                'complete': endpoint_result.pagination_complete,
                'pagination_complete': endpoint_result.pagination_complete,
                'termination_reason': endpoint_result.termination_reason,
                'last_page_size': endpoint_result.last_page_size,
                'error': error,
                'http_status': http_status,
            })
            if status == 'forbidden' or status == 'error':
                return RepositoryFetchResult(
                    repository, status, records + fetched, total_pages, error, http_status,
                    workflow_statuses,
                    endpoint_result.pagination_complete,
                    endpoint_result.termination_reason,
                    endpoint_result.last_page_size,
                    raw_records=raw_records,
                    rate_limit_headers=rate_limit_headers,
                )
            if status == 'not_found':
                # A workflow can have been deleted or renamed after the
                # static snapshot.  Continue with the other exposed IDs.
                continue
            for record in fetched:
                key = run_key(record)
                if key is None or key not in seen_runs:
                    records.append(record)
                    if key is not None:
                        seen_runs.add(key)
        all_workflows_complete = (
            len(workflow_statuses) == len(filters)
            and all(
                item.get('pagination_complete', False)
                for item in workflow_statuses
            )
        )
        termination_reason = (
            'all_workflows_complete'
            if all_workflows_complete
            else 'workflow_query_incomplete'
        )
        last_page_size = (
            int(workflow_statuses[-1].get('last_page_size') or 0)
            if workflow_statuses
            else 0
        )
        return RepositoryFetchResult(
            repository,
            'ok' if records else 'no_runs',
            records,
            total_pages,
            None,
            None,
            workflow_statuses,
            all_workflows_complete,
            termination_reason,
            last_page_size,
            raw_records=raw_records,
            rate_limit_headers=rate_limit_headers if 'rate_limit_headers' in locals() else {},
        )


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def fetch_targets(
    targets: Sequence[RepositoryInfo],
    output_dir: Path,
    run_sources: Sequence[Path],
    as_of: datetime,
    window_start: Optional[datetime],
    token: str,
    workers: int,
    max_pages: int,
    progress_every: int,
    resume: bool,
    workflow_filters_by_repository: Optional[
        Mapping[str, Sequence[Mapping[str, Any]]]
    ] = None,
    workflow_paths_by_repository: Optional[Mapping[str, Set[str]]] = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_output = output_dir / 'supplemental_workflow_runs.jsonl'
    status_output = output_dir / 'repository_fetch_status.jsonl'
    terminal_ids = load_terminal_repository_ids(status_output) if resume else set()
    existing_keys = load_run_keys([*run_sources, run_output]) if resume else set()
    pending = [
        repository for repository in targets
        if repository.repository_id not in terminal_ids
    ]
    stats: Dict[str, Any] = {
        'target_repositories': len(targets),
        'resumed_terminal_repositories': len(terminal_ids),
        'pending_repositories': len(pending),
        'repositories_ok': 0,
        'repositories_no_runs': 0,
        'repositories_not_found': 0,
        'repositories_forbidden': 0,
        'repositories_error': 0,
        'api_pages': 0,
        'runs_fetched': 0,
        'runs_written': 0,
        'runs_deduplicated_against_existing_sources': 0,
    }
    client = GitHubActionsClient(token)
    with run_output.open('a', encoding='utf-8') as run_handle, status_output.open('a', encoding='utf-8') as status_handle:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    client.fetch_repository,
                    repository,
                    as_of,
                    window_start,
                    max_pages,
                    (workflow_filters_by_repository or {}).get(repository.repository_id),
                    (workflow_paths_by_repository or {}).get(repository.repository_id),
                ): repository
                for repository in pending
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                stats['api_pages'] += result.pages
                stats['runs_fetched'] += len(result.records)
                for record in result.records:
                    key = run_key(record)
                    if key is None or key in existing_keys:
                        stats['runs_deduplicated_against_existing_sources'] += 1
                        continue
                    run_handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                    existing_keys.add(key)
                    stats['runs_written'] += 1
                status_record = {
                    'repository_id': result.repository.repository_id,
                    'repository_name': result.repository.repository_name,
                    'default_branch': result.repository.default_branch,
                    'status': result.status,
                    'pages': result.pages,
                    'records': len(result.records),
                    'error': result.error,
                    'http_status': result.http_status,
                    'pagination_complete': result.pagination_complete,
                    'termination_reason': result.termination_reason,
                    'last_page_size': result.last_page_size,
                    'workflow_statuses': result.workflow_statuses or [],
                    'fetched_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                }
                status_handle.write(json.dumps(status_record, ensure_ascii=False) + '\n')
                run_handle.flush()
                status_handle.flush()
                stats[f"repositories_{result.status}"] = stats.get(
                    f"repositories_{result.status}", 0
                ) + 1
                if progress_every > 0 and completed % progress_every == 0:
                    print(
                        f"补充默认分支运行 {completed}/{len(pending)}: "
                        f"写入运行 {stats['runs_written']}",
                        flush=True,
                    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description='Supplement default-branch workflow runs from GitHub API')
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--as-of', default='2026-06-09T23:59:59Z')
    parser.add_argument('--window-start', default=None,
                        help='optional inclusive UTC lower bound; limits API queries to the RQ2 window')
    parser.add_argument('--additional-runs', type=Path, action='append', default=[])
    parser.add_argument('--include-all-repositories', action='store_true',
                        help='query all repositories instead of only those lacking usable runs')
    parser.add_argument('--target-repositories-file', type=Path, default=None,
                        help='optional JSONL/plain-text allow-list of repositories to query')
    parser.add_argument('--offset', type=int, default=0,
                        help='skip this many selected targets before applying --limit')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--progress-every', type=int, default=100)
    parser.add_argument('--no-resume', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    as_of = parse_timestamp(args.as_of)
    if as_of is None:
        raise SystemExit('--as-of must be an ISO-8601 timestamp')
    window_start = parse_timestamp(args.window_start) if args.window_start else None
    if args.window_start and window_start is None:
        raise SystemExit('--window-start must be an ISO-8601 timestamp')
    if window_start is not None and window_start > as_of:
        raise SystemExit('--window-start must not be later than --as-of')
    run_sources = [args.data_dir / 'workflow_runs.jsonl', *args.additional_runs]
    by_id, by_alias = load_repositories(args.data_dir / 'repo_info.jsonl')
    requested_repository_ids = load_repository_filter(
        args.target_repositories_file, by_id, by_alias
    )
    retry_repository_ids = load_retry_repository_ids(
        args.output_dir / 'repository_fetch_status.jsonl'
    )
    workflow_filters_by_repository = load_repository_workflow_filters(
        args.target_repositories_file, by_id, by_alias
    )
    workflow_paths_by_repository = load_repository_workflow_paths(
        args.target_repositories_file, by_id, by_alias
    )
    targets, target_stats = build_targets(
        args.data_dir,
        run_sources,
        as_of,
        include_all=args.include_all_repositories,
        requested_repository_ids=requested_repository_ids,
        force_repository_ids=retry_repository_ids,
    )
    offset = max(0, args.offset)
    if args.limit is not None:
        targets = targets[offset:offset + max(0, args.limit)]
    elif offset:
        targets = targets[offset:]
    target_stats['offset'] = offset
    target_stats['limit'] = args.limit
    target_stats['targets_after_limit'] = len(targets)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / 'target_metadata.json', target_stats)
    with (args.output_dir / 'target_repositories.jsonl').open('w', encoding='utf-8') as handle:
        for repository in targets:
            handle.write(json.dumps({
                'repository_id': repository.repository_id,
                'repository_name': repository.repository_name,
                'default_branch': repository.default_branch,
                'workflow_filters': workflow_filters_by_repository.get(
                    repository.repository_id, []
                ),
            }, ensure_ascii=False) + '\n')
    print(json.dumps(target_stats, ensure_ascii=False, indent=2))
    if args.dry_run:
        return

    token = load_token(REPOSITORY_ROOT)
    if not token:
        raise SystemExit('未发现 GITHUB_TOKEN/GH_TOKEN 或 config/.env 中的 GitHub token')
    fetch_stats = fetch_targets(
        targets,
        args.output_dir,
        run_sources,
        as_of,
        window_start,
        token,
        args.workers,
        args.max_pages,
        args.progress_every,
        resume=not args.no_resume,
        workflow_filters_by_repository=workflow_filters_by_repository,
        workflow_paths_by_repository=workflow_paths_by_repository,
    )
    metadata = {
        **target_stats,
        **fetch_stats,
        'as_of': timestamp_text(as_of),
        'window_start': timestamp_text(window_start),
        'branch_scope': 'default branch only',
        'workflow_query_scope': (
            'workflow-specific endpoints where all exposed workflow IDs are known; '
            'otherwise repository-wide endpoint filtered by exposed workflow paths'
            if workflow_paths_by_repository else 'repository-wide endpoint fallback'
        ),
        'run_sources': [str(path) for path in run_sources],
        'output_runs': str(args.output_dir / 'supplemental_workflow_runs.jsonl'),
        'output_status': str(args.output_dir / 'repository_fetch_status.jsonl'),
        'matching_note': (
            'The returned workflow_path and head_sha are retained from the API; '
            'the YAML at head_sha is fetched in the separate snapshot supplement stage.'
        ),
    }
    write_json(args.output_dir / 'supplemental_run_metadata.json', metadata)
    print(json.dumps(fetch_stats, ensure_ascii=False, indent=2))
    print(f"补充运行元数据: {args.output_dir / 'supplemental_run_metadata.json'}")


if __name__ == '__main__':
    main()
