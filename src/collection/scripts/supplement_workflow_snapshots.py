"""Supplement workflow YAML snapshots at the commit that triggered a run.

The primary ``workflow_files.jsonl`` collection is a current/default-branch
snapshot fetched after the study period.  It therefore cannot represent a
workflow revision that existed only in a historical run, and it can also
contain a repository alias that has since been renamed.  This utility builds
the missing portion of the RQ2 input from completed default-branch runs and
fetches the YAML at each run's ``head_sha``.  Additional run files can be
provided as well; they are merged before candidate de-duplication so that
supplementary run collection does not silently omit a workflow version.

The output is deliberately separate from the primary data directory.  It is
safe to resume, and every record retains the run provenance and the exact
commit reference used for the download.

Examples::

    python scripts/supplement_workflow_snapshots.py --dry-run
    python scripts/supplement_workflow_snapshots.py --limit 20 --source contents
    python scripts/supplement_workflow_snapshots.py --source raw --workers 8
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPOSITORY_ROOT / 'data_26_6_9'
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT
    / 'output_full_logical_20260831'
    / 'rq2_snapshot_supplement_20260609'
)
DEFAULT_AS_OF = '2026-06-09T23:59:59Z'
WORKFLOW_PREFIX = '.github/workflows/'
MAX_PROVENANCE_RUN_IDS = 50


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


def safe_name(value: str) -> str:
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
    return value.strip(' .') or 'workflow'


@dataclass(frozen=True)
class RepositoryInfo:
    repository_id: str
    canonical_name: str
    default_branch: str
    aliases: Tuple[str, ...]


def load_repositories(path: Path) -> Tuple[Dict[str, RepositoryInfo], Dict[str, RepositoryInfo]]:
    """Return ``alias -> repo`` and ``id -> repo`` maps.

    Both the original ``_id`` and the API-returned ``repo.name`` are retained
    because the raw run data contains both old and current repository labels.
    """

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
            'canonical_name': api_name or original_name,
            'default_branch': default_branch,
            'aliases': set(),
        })
        if api_name and not item['canonical_name']:
            item['canonical_name'] = api_name
        if original_name:
            item['aliases'].add(original_name)
        if api_name:
            item['aliases'].add(api_name)
    for repository_id, item in pending.items():
        canonical_name = str(item['canonical_name'] or '')
        if not canonical_name:
            continue
        by_id[repository_id] = RepositoryInfo(
            repository_id=repository_id,
            canonical_name=canonical_name,
            default_branch=str(item['default_branch']),
            aliases=tuple(sorted(item['aliases'])),
        )
    by_alias: Dict[str, RepositoryInfo] = {}
    for repo in by_id.values():
        by_alias[normalize_repo(repo.canonical_name)] = repo
        for alias in repo.aliases:
            by_alias[normalize_repo(alias)] = repo
    return by_alias, by_id


def load_existing_snapshot_keys(
    path: Path,
    data_dir: Path,
    by_alias: Mapping[str, RepositoryInfo],
) -> Tuple[set[Tuple[str, str]], int]:
    """Load snapshot identities with an existing local content file.

    A JSONL index entry whose content file is missing is intentionally not
    treated as complete; it is eligible for supplementation as well.
    """

    keys: set[Tuple[str, str]] = set()
    invalid_content_files = 0
    for entry in load_jsonl(path):
        repository = by_alias.get(normalize_repo(entry.get('repository_name')))
        workflow_path = str(entry.get('workflow_path') or '')
        content_file = str(entry.get('content_file') or '')
        if repository is None or not workflow_path or not content_file:
            continue
        content_path = data_dir / Path(content_file)
        if not content_path.exists():
            invalid_content_files += 1
            continue
        keys.add((repository.repository_id, workflow_path))
    return keys, invalid_content_files


def load_target_repository_ids(
    path: Optional[Path],
    by_alias: Mapping[str, RepositoryInfo],
    by_id: Mapping[str, RepositoryInfo],
) -> Optional[set[str]]:
    """Load an optional repository allow-list used to scope supplementation."""

    if path is None:
        return None
    target_ids: set[str] = set()
    with path.open('r', encoding='utf-8') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                value = line
            if isinstance(value, dict):
                repository_id = str(value.get('repository_id') or '')
                repository_name = value.get('repository_name')
                if repository_id and repository_id in by_id:
                    target_ids.add(repository_id)
                    continue
            else:
                repository_name = value
            repository = by_alias.get(normalize_repo(repository_name))
            if repository is not None:
                target_ids.add(repository.repository_id)
    return target_ids


def build_candidates(
    data_dir: Path,
    as_of: datetime,
    window_start: Optional[datetime],
    include_all_branches: bool = False,
    include_existing_identities: bool = False,
    run_sources: Optional[Sequence[Path]] = None,
    target_repository_ids: Optional[set[str]] = None,
    max_runs_per_workflow: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    by_alias, by_id = load_repositories(data_dir / 'repo_info.jsonl')
    existing_keys, invalid_content_files = load_existing_snapshot_keys(
        data_dir / 'workflow_files.jsonl', data_dir, by_alias
    )

    candidates: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    eligible_runs_by_workflow: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    sources = list(run_sources or [data_dir / 'workflow_runs.jsonl'])
    stats: Dict[str, Any] = {
        'raw_run_records': 0,
        'completed_run_records': 0,
        'excluded_after_as_of': 0,
        'excluded_before_window_start': 0,
        'excluded_non_default_branch': 0,
        'excluded_missing_repository_mapping': 0,
        'excluded_non_workflow_path': 0,
        'excluded_missing_head_sha': 0,
        'excluded_existing_snapshot_identity': 0,
        'excluded_target_repository': 0,
        'eligible_run_records_before_workflow_cap': 0,
        'excluded_by_workflow_cap': 0,
        'eligible_run_records': 0,
        'unique_snapshot_candidates': 0,
        'unique_workflow_identities': 0,
        'repositories': 0,
        'invalid_existing_content_files': invalid_content_files,
        'as_of': timestamp_text(as_of),
        'window_start': timestamp_text(window_start),
        'branch_scope': 'all branches' if include_all_branches else 'default branch only',
        'include_existing_snapshot_identities': include_existing_identities,
        'run_sources': [str(source) for source in sources],
        'target_repository_count': len(target_repository_ids) if target_repository_ids is not None else None,
        'max_runs_per_workflow': max_runs_per_workflow,
    }

    def add_candidate(entry: Dict[str, Any], repository: RepositoryInfo,
                      created_at: datetime, workflow_path: str, head_sha: str) -> None:
        candidate_key = (repository.repository_id, workflow_path, head_sha)
        candidate = candidates.get(candidate_key)
        if candidate is None:
            candidate = {
                'repository_id': repository.repository_id,
                'repository_name': repository.canonical_name,
                'source_repository_name': entry.get('repository_name'),
                'default_branch': repository.default_branch,
                'workflow_path': workflow_path,
                'head_sha': head_sha,
                'workflow_id': entry.get('workflow_id'),
                'matched_run_ids': [],
                'matched_run_count': 0,
                'run_created_at_min': timestamp_text(created_at),
                'run_created_at_max': timestamp_text(created_at),
            }
            candidates[candidate_key] = candidate
        candidate['matched_run_count'] += 1
        run_id = entry.get('run_id')
        if len(candidate['matched_run_ids']) < MAX_PROVENANCE_RUN_IDS and run_id not in (None, ''):
            candidate['matched_run_ids'].append({
                'run_id': run_id,
                'run_attempt': entry.get('run_attempt') or 1,
                'created_at': entry.get('created_at'),
            })
        current_min = parse_timestamp(candidate['run_created_at_min'])
        current_max = parse_timestamp(candidate['run_created_at_max'])
        if current_min is None or created_at < current_min:
            candidate['run_created_at_min'] = timestamp_text(created_at)
        if current_max is None or created_at > current_max:
            candidate['run_created_at_max'] = timestamp_text(created_at)

    for source in sources:
        if not source.exists():
            raise FileNotFoundError(source)
        for entry in load_jsonl(source):
            stats['raw_run_records'] += 1
            if str(entry.get('status') or '').lower() != 'completed':
                continue
            stats['completed_run_records'] += 1
            created_at = parse_timestamp(entry.get('created_at'))
            if created_at is None or created_at > as_of:
                stats['excluded_after_as_of'] += 1
                continue
            if window_start is not None and created_at < window_start:
                stats['excluded_before_window_start'] += 1
                continue

            repository = by_alias.get(normalize_repo(entry.get('repository_name')))
            if repository is None:
                stats['excluded_missing_repository_mapping'] += 1
                continue
            if target_repository_ids is not None and repository.repository_id not in target_repository_ids:
                stats['excluded_target_repository'] += 1
                continue
            if not include_all_branches and entry.get('head_branch') != repository.default_branch:
                stats['excluded_non_default_branch'] += 1
                continue

            workflow_path = str(entry.get('workflow_path') or '')
            if not workflow_path.startswith(WORKFLOW_PREFIX):
                stats['excluded_non_workflow_path'] += 1
                continue
            head_sha = str(entry.get('head_sha') or '')
            if not head_sha:
                stats['excluded_missing_head_sha'] += 1
                continue
            identity_key = (repository.repository_id, workflow_path)
            if identity_key in existing_keys and not include_existing_identities:
                stats['excluded_existing_snapshot_identity'] += 1
                continue

            if max_runs_per_workflow is None:
                add_candidate(entry, repository, created_at, workflow_path, head_sha)
            else:
                workflow_key = (repository.repository_id, workflow_path)
                eligible_runs_by_workflow.setdefault(workflow_key, []).append({
                    'entry': entry,
                    'repository': repository,
                    'created_at': created_at,
                    'workflow_path': workflow_path,
                    'head_sha': head_sha,
                })

    if max_runs_per_workflow is not None:
        cap = max(1, max_runs_per_workflow)
        stats['eligible_run_records_before_workflow_cap'] = sum(
            len(items) for items in eligible_runs_by_workflow.values()
        )
        for items in eligible_runs_by_workflow.values():
            ordered = sorted(
                items,
                key=lambda item: (
                    item['created_at'],
                    str(item['entry'].get('run_id') or ''),
                    str(item['entry'].get('run_attempt') or '1'),
                ),
                reverse=True,
            )
            selected: List[Dict[str, Any]] = []
            seen_run_keys: set[Tuple[str, str]] = set()
            for item in ordered:
                entry = item['entry']
                run_id = str(entry.get('run_id') or '')
                run_attempt = str(entry.get('run_attempt') or '1')
                run_key = (run_id, run_attempt)
                if run_id and run_key in seen_run_keys:
                    continue
                if run_id:
                    seen_run_keys.add(run_key)
                selected.append(item)
                if len(selected) >= cap:
                    break
            stats['excluded_by_workflow_cap'] += max(0, len(items) - len(selected))
            for item in selected:
                add_candidate(
                    item['entry'], item['repository'], item['created_at'],
                    item['workflow_path'], item['head_sha'],
                )

    result = sorted(
        candidates.values(),
        key=lambda item: (
            str(item.get('repository_name') or '').lower(),
            str(item.get('workflow_path') or ''),
            str(item.get('head_sha') or ''),
        ),
    )
    stats['eligible_run_records'] = sum(item['matched_run_count'] for item in result)
    stats['unique_snapshot_candidates'] = len(result)
    stats['unique_workflow_identities'] = len({
        (item['repository_id'], item['workflow_path']) for item in result
    })
    stats['repositories'] = len({item['repository_id'] for item in result})
    return result, stats


def load_token(repository_root: Path) -> Optional[str]:
    for variable in ('GITHUB_TOKEN', 'GH_TOKEN'):
        value = os.environ.get(variable)
        if value:
            return value.strip()
    env_path = repository_root / 'config' / '.env'
    if not env_path.exists():
        return None
    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in ('GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_API_TOKEN'):
            return value.strip().strip('"\'')
    return None


@dataclass
class FetchResult:
    candidate: Dict[str, Any]
    content: Optional[bytes]
    response_sha: Optional[str]
    status: str
    error: Optional[str] = None
    http_status: Optional[int] = None
    resolved_repository_name: Optional[str] = None


class GitHubFetcher:
    def __init__(self, token: Optional[str], source: str, timeout: int = 45, retries: int = 4):
        self.token = token
        self.source = source
        self.timeout = timeout
        self.retries = retries

    def _request(self, url: str) -> Tuple[bytes, Optional[str]]:
        headers = {
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'gha-log-analysis-workflow-supplement/1.0',
        }
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            request = Request(url, headers=headers, method='GET')
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    return body, response.headers.get('X-RateLimit-Reset')
            except HTTPError as exc:
                last_error = exc
                retryable = exc.code in {403, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.retries:
                    raise
                retry_after = exc.headers.get('Retry-After')
                delay = 2 ** attempt
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(int(retry_after), 120))
                reset = exc.headers.get('X-RateLimit-Reset')
                if exc.code in {403, 429} and reset and reset.isdigit():
                    delay = max(delay, min(max(int(reset) - int(time.time()), 1), 120))
                time.sleep(delay)
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError(str(last_error) if last_error else 'GitHub request failed')

    def fetch(self, candidate: Dict[str, Any]) -> FetchResult:
        repository = str(candidate['repository_name'])
        source_repository = str(candidate.get('source_repository_name') or '')
        sha = str(candidate['head_sha'])
        workflow_path = str(candidate['workflow_path'])
        repositories = [repository]
        if source_repository and source_repository.lower() != repository.lower():
            repositories.append(source_repository)
        last_http_status: Optional[int] = None
        for resolved_repository in repositories:
            if self.source == 'raw':
                url = (
                    f'https://raw.githubusercontent.com/{quote(resolved_repository, safe="/")}/'
                    f'{quote(sha, safe="")}/{quote(workflow_path, safe="/")}'
                )
            else:
                path = quote(workflow_path, safe='/')
                query = urlencode({'ref': sha})
                url = (
                    f'https://api.github.com/repos/{quote(resolved_repository, safe="/")}/'
                    f'contents/{path}?{query}'
                )
            try:
                body, _ = self._request(url)
                response_sha: Optional[str] = None
                content = body
                if self.source == 'contents':
                    payload = json.loads(body.decode('utf-8'))
                    if payload.get('type') != 'file' or not payload.get('content'):
                        return FetchResult(candidate, None, None, 'not_file', 'GitHub response is not a file')
                    response_sha = str(payload.get('sha') or '') or None
                    encoded = str(payload['content']).replace('\n', '')
                    content = base64.b64decode(encoded)
                return FetchResult(
                    candidate, content, response_sha, 'ok',
                    resolved_repository_name=resolved_repository,
                )
            except HTTPError as exc:
                last_http_status = exc.code
                if exc.code == 404 and resolved_repository != repositories[-1]:
                    continue
                return FetchResult(candidate, None, None, 'http_error', f'HTTP {exc.code}', exc.code)
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                return FetchResult(candidate, None, None, 'error', str(exc))
        return FetchResult(
            candidate, None, None, 'http_error',
            f'HTTP {last_http_status or 404}', last_http_status or 404,
        )


def load_success_keys(path: Path) -> set[Tuple[str, str, str]]:
    if not path.exists():
        return set()
    keys: set[Tuple[str, str, str]] = set()
    for entry in load_jsonl(path):
        if entry.get('status') != 'ok':
            continue
        keys.add((
            str(entry.get('repository_id') or ''),
            str(entry.get('workflow_path') or ''),
            str(entry.get('head_sha') or ''),
        ))
    return keys


def load_failure_keys(path: Path) -> set[Tuple[str, str, str]]:
    if not path.exists():
        return set()
    keys: set[Tuple[str, str, str]] = set()
    for entry in load_jsonl(path):
        keys.add((
            str(entry.get('repository_id') or ''),
            str(entry.get('workflow_path') or ''),
            str(entry.get('head_sha') or ''),
        ))
    return keys


def write_cumulative_metadata(
    output_dir: Path,
    candidate_stats: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    source: str,
) -> Dict[str, Any]:
    """Write a cumulative summary across all resumable fetch attempts."""

    success_keys = load_success_keys(output_dir / 'supplemental_workflow_snapshots.jsonl')
    failure_keys = load_failure_keys(output_dir / 'supplemental_workflow_snapshot_failures.jsonl')
    candidate_keys = {
        (
            str(candidate.get('repository_id') or ''),
            str(candidate.get('workflow_path') or ''),
            str(candidate.get('head_sha') or ''),
        )
        for candidate in candidates
    }
    unresolved = candidate_keys - success_keys
    metadata = {
        **dict(candidate_stats),
        'candidate_records_in_current_manifest': len(candidate_keys),
        'successful_records_total': len(success_keys & candidate_keys),
        'failure_records_total_deduplicated': len(failure_keys & candidate_keys),
        'unresolved_candidate_records': len(unresolved),
        'coverage_percent': (
            len(success_keys & candidate_keys) / len(candidate_keys) * 100.0
            if candidate_keys else 0.0
        ),
        'source': f'github_{source}_at_run_head_sha',
        'success_records_path': str(output_dir / 'supplemental_workflow_snapshots.jsonl'),
        'failure_records_path': str(output_dir / 'supplemental_workflow_snapshot_failures.jsonl'),
    }
    write_json(output_dir / 'supplement_metadata.json', metadata)
    return metadata


def content_path_for(output_dir: Path, candidate: Mapping[str, Any], content_hash: str) -> Path:
    repository = safe_name(str(candidate.get('repository_name') or 'repository'))
    workflow_stem = safe_name(Path(str(candidate.get('workflow_path') or 'workflow.yml')).name)
    sha = str(candidate.get('head_sha') or '')[:12] or 'unknown'
    return output_dir / 'workflows' / repository / f'{workflow_stem}_{sha}_{content_hash[:12]}.yml'


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def run_fetch(
    candidates: Sequence[Dict[str, Any]],
    output_dir: Path,
    token: Optional[str],
    source: str,
    workers: int,
    resume: bool,
    progress_every: int,
    batch_size: int = 2000,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    success_path = output_dir / 'supplemental_workflow_snapshots.jsonl'
    failure_path = output_dir / 'supplemental_workflow_snapshot_failures.jsonl'
    existing_successes = load_success_keys(success_path) if resume else set()
    candidates = [
        candidate for candidate in candidates
        if (
            str(candidate.get('repository_id') or ''),
            str(candidate.get('workflow_path') or ''),
            str(candidate.get('head_sha') or ''),
        ) not in existing_successes
    ]
    mode = 'a'
    stats: Dict[str, Any] = {
        'requested_candidates': len(candidates),
        'resumed_successes': len(existing_successes),
        'successful': 0,
        'failed': 0,
        'not_files': 0,
        'source': source,
    }
    fetcher = GitHubFetcher(token, source)
    with success_path.open(mode, encoding='utf-8') as success_handle, failure_path.open(mode, encoding='utf-8') as failure_handle:
        completed_total = 0
        effective_batch_size = max(1, batch_size)
        for start in range(0, len(candidates), effective_batch_size):
            batch = candidates[start:start + effective_batch_size]
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                futures = {
                    executor.submit(fetcher.fetch, candidate): candidate
                    for candidate in batch
                }
                for future in as_completed(futures):
                    completed_total += 1
                    result = future.result()
                    candidate = result.candidate
                    key_data = {
                        'repository_id': candidate.get('repository_id'),
                        'repository_name': candidate.get('repository_name'),
                        'source_repository_name': candidate.get('source_repository_name'),
                        'default_branch': candidate.get('default_branch'),
                        'workflow_path': candidate.get('workflow_path'),
                        'head_sha': candidate.get('head_sha'),
                        'workflow_id': candidate.get('workflow_id'),
                        'matched_run_ids': candidate.get('matched_run_ids') or [],
                        'matched_run_count': candidate.get('matched_run_count', 0),
                        'run_created_at_min': candidate.get('run_created_at_min'),
                        'run_created_at_max': candidate.get('run_created_at_max'),
                        'snapshot_as_of': candidate.get('snapshot_as_of'),
                        'source': f'github_{source}_at_run_head_sha',
                    }
                    if result.status == 'ok' and result.content is not None:
                        content_hash = hashlib.sha256(result.content).hexdigest()
                        target = content_path_for(output_dir, candidate, content_hash)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            target.write_bytes(result.content)
                        record = {
                            **key_data,
                            'status': 'ok',
                            'resolved_repository_name': result.resolved_repository_name,
                            'content_hash': content_hash,
                            'file_sha': result.response_sha,
                            'content_file': str(target.relative_to(output_dir)).replace('\\', '/'),
                        }
                        success_handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                        success_handle.flush()
                        stats['successful'] += 1
                    else:
                        record = {
                            **key_data,
                            'status': result.status,
                            'http_status': result.http_status,
                            'error': result.error,
                        }
                        failure_handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                        failure_handle.flush()
                        stats['failed'] += 1
                        if result.status == 'not_file':
                            stats['not_files'] += 1
                    if progress_every > 0 and completed_total % progress_every == 0:
                        print(
                            f'补充快照 {completed_total}/{len(candidates)}: '
                            f'成功 {stats["successful"]}, 失败 {stats["failed"]}',
                            flush=True,
                        )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Supplement missing workflow YAMLs at run head_sha for a fixed study window'
    )
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--as-of', default=DEFAULT_AS_OF, help='Inclusive UTC run cutoff')
    parser.add_argument('--window-start', default=None, help='Optional inclusive UTC lower bound')
    parser.add_argument('--limit', type=int, default=None, help='Only fetch the first N candidates')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--batch-size', type=int, default=2000,
                        help='number of candidates submitted at once; bounds memory use')
    parser.add_argument('--progress-every', type=int, default=20)
    parser.add_argument('--source', choices=['contents', 'raw'], default='raw')
    parser.add_argument('--all-branches', action='store_true')
    parser.add_argument('--include-existing-identities', action='store_true',
                        help='also fetch exact head_sha snapshots for identities already in workflow_files.jsonl')
    parser.add_argument('--additional-runs', type=Path, action='append', default=[],
                        help='additional workflow-runs JSONL files merged before candidate de-duplication')
    parser.add_argument('--target-repositories-file', type=Path, default=None,
                        help='optional JSONL/text repository allow-list')
    parser.add_argument('--max-runs-per-workflow', type=int, default=None,
                        help='keep only the latest N completed runs per repository/workflow before snapshot de-duplication')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--finalize', action='store_true',
                        help='rebuild cumulative metadata from existing fetch outputs without network calls')
    parser.add_argument('--no-resume', action='store_true')
    args = parser.parse_args()

    as_of = parse_timestamp(args.as_of)
    if as_of is None:
        raise SystemExit('--as-of must be an ISO-8601 timestamp')
    window_start = parse_timestamp(args.window_start) if args.window_start else None
    if args.window_start and window_start is None:
        raise SystemExit('--window-start must be an ISO-8601 timestamp')
    if window_start is not None and window_start > as_of:
        raise SystemExit('--window-start must not be later than --as-of')

    by_alias, by_id = load_repositories(args.data_dir / 'repo_info.jsonl')
    target_repository_ids = load_target_repository_ids(
        args.target_repositories_file, by_alias, by_id,
    )
    candidates, candidate_stats = build_candidates(
        args.data_dir,
        as_of,
        window_start,
        include_all_branches=args.all_branches,
        include_existing_identities=args.include_existing_identities,
        run_sources=[args.data_dir / 'workflow_runs.jsonl', *args.additional_runs],
        target_repository_ids=target_repository_ids,
        max_runs_per_workflow=args.max_runs_per_workflow,
    )
    if args.limit is not None:
        candidates = candidates[:max(0, args.limit)]
    for candidate in candidates:
        candidate['snapshot_as_of'] = timestamp_text(as_of)
    candidate_stats['limit'] = args.limit
    candidate_stats['candidates_after_limit'] = len(candidates)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / 'candidate_metadata.json', candidate_stats)
    with (args.output_dir / 'snapshot_candidates.jsonl').open('w', encoding='utf-8') as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate, ensure_ascii=False) + '\n')
    print(json.dumps(candidate_stats, ensure_ascii=False, indent=2))
    print(f'候选清单: {args.output_dir / "snapshot_candidates.jsonl"}')

    if args.dry_run:
        return
    if args.finalize:
        metadata = write_cumulative_metadata(
            args.output_dir, candidate_stats, candidates, args.source,
        )
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(f'补充元数据: {args.output_dir / "supplement_metadata.json"}')
        return
    token = load_token(REPOSITORY_ROOT)
    if not token and args.source == 'contents':
        print('警告：未发现 GitHub token，REST Contents API 可能受到低速率限制。', file=sys.stderr)
    fetch_stats = run_fetch(
        candidates,
        args.output_dir,
        token,
        args.source,
        max(1, args.workers),
        resume=not args.no_resume,
        progress_every=args.progress_every,
        batch_size=max(1, args.batch_size),
    )
    metadata = write_cumulative_metadata(
        args.output_dir,
        {**candidate_stats, **fetch_stats},
        candidates,
        args.source,
    )
    print(json.dumps(fetch_stats, ensure_ascii=False, indent=2))
    print(f'补充元数据: {args.output_dir / "supplement_metadata.json"}')


if __name__ == '__main__':
    main()
