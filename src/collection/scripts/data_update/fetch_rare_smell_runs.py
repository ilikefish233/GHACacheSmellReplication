"""补充指定缓存异味工作流的历史运行记录。

原始采集流程为了控制 API 成本，每个工作流文件只保留最近 5 条运行记录。
本脚本针对 ``--smells`` 指定的异味涉及的工作流文件，按 workflow_id 获取更多运行，
并将结果写入独立 JSONL 文件。原始 ``workflow_runs.jsonl`` 不会被覆盖；默认值仍保留
为上一轮稀有异味补充所使用的四类异味，以兼容历史命令。

补充记录可通过 ``prepare_rq2_versioned.py --runs-file`` 与原始运行记录合并
后进行相同的默认分支、版本区间匹配。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.utils.github_client import GitHubClient  # noqa: E402


DEFAULT_SCAN_DIR = (
    REPOSITORY_ROOT / 'output_full_logical_20260831' / 'rq2_versioned_default_improved'
)
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / 'output_full_logical_20260831' / 'rq2_rare_run_supplement'
)
DEFAULT_SMELLS = (
    'cache_position_error',
    'conflicting_cache_sources',
    'static_cache_key',
    'overly_specific_cache_key',
)


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


def normalize_smell_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value == 'disposable_cache_key':
        return 'overly_specific_cache_key'
    return value or None


def detector_smell_types(entry: Mapping[str, Any]) -> Set[str]:
    detector = entry.get('detector') or {}
    result: Set[str] = set()
    by_type = detector.get('smells_by_type') or {}
    if isinstance(by_type, Mapping):
        for value in by_type:
            normalized = normalize_smell_type(value)
            if normalized:
                result.add(normalized)
    for smell in detector.get('smells') or []:
        if isinstance(smell, Mapping):
            normalized = normalize_smell_type(smell.get('type'))
            if normalized:
                result.add(normalized)
    return result


def build_target_manifest(
    scan_dir: Path,
    smells: Set[str],
) -> List[Dict[str, Any]]:
    target_smells: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    target_versions: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    scan_path = scan_dir / 'version_scan_results.jsonl'
    for entry in load_jsonl(scan_path):
        if entry.get('detector', {}).get('error'):
            continue
        repository = str(entry.get('repository_name') or '')
        workflow_path = str(entry.get('workflow_path') or '')
        if not repository or not workflow_path:
            continue
        hit = detector_smell_types(entry) & smells
        if not hit:
            continue
        identity = (repository, workflow_path)
        target_smells[identity].update(hit)
        version_id = str(entry.get('version_id') or '')
        if version_id:
            target_versions[identity].add(version_id)

    workflow_id_candidates: Dict[Tuple[str, str], Counter[str]] = defaultdict(Counter)
    run_path = scan_dir / 'run_records.jsonl'
    for entry in load_jsonl(run_path):
        identity = (
            str(entry.get('repository_name') or ''),
            str(entry.get('workflow_path') or ''),
        )
        if identity not in target_smells:
            continue
        workflow_id = entry.get('workflow_id')
        if workflow_id not in (None, ''):
            workflow_id_candidates[identity][str(workflow_id)] += 1

    manifest: List[Dict[str, Any]] = []
    for identity in sorted(target_smells):
        repository, workflow_path = identity
        candidates = workflow_id_candidates.get(identity, Counter())
        manifest.append({
            'repository_name': repository,
            'workflow_path': workflow_path,
            'workflow_ids': [value for value, _ in candidates.most_common()],
            'smells': sorted(target_smells[identity]),
            'version_count': len(target_versions[identity]),
            'existing_matched_runs': sum(candidates.values()),
        })
    return manifest


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def duration_seconds(run: Mapping[str, Any]) -> Optional[float]:
    started = parse_timestamp(run.get('run_started_at') or run.get('created_at'))
    updated = parse_timestamp(run.get('updated_at'))
    if started is None or updated is None:
        return None
    return (updated - started).total_seconds()


def as_record(
    repository_name: str,
    workflow_path: str,
    workflow_id: str,
    run: Mapping[str, Any],
) -> Dict[str, Any]:
    actor = run.get('actor') or {}
    actor_login = actor.get('login', '') if isinstance(actor, Mapping) else ''
    run_number = run.get('run_number', 0)
    run_attempt = run.get('run_attempt') or 1
    run_id = run.get('id')
    return {
        '_id': f'{repository_name}_{workflow_path}_{run_number}_{run_attempt}',
        'repository_name': repository_name,
        'run_id': run_id,
        'workflow_id': run.get('workflow_id') or workflow_id,
        'workflow_path': workflow_path,
        'workflow_name': run.get('name', ''),
        'run_number': run_number,
        'run_attempt': run_attempt,
        'status': run.get('status', ''),
        'conclusion': run.get('conclusion', ''),
        'event': run.get('event', ''),
        'head_branch': run.get('head_branch', ''),
        'head_sha': run.get('head_sha', ''),
        'created_at': run.get('created_at', ''),
        'updated_at': run.get('updated_at', ''),
        'run_started_at': run.get('run_started_at', ''),
        'duration_seconds': duration_seconds(run),
        'actor': actor_login,
        'url': run.get('url', ''),
        'html_url': run.get('html_url', ''),
        'fetched_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        '_supplemental_source': 'github_actions_workflow_runs_api',
    }


def fetch_target(
    item: Mapping[str, Any],
    max_runs_per_workflow: int,
    max_pages_per_workflow: Optional[int],
    created_before: Optional[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, int]:
    """Fetch one target identity using a private client for safe concurrency."""
    client = GitHubClient()
    repository = str(item.get('repository_name') or '')
    workflow_path = str(item.get('workflow_path') or '')
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    requests = 0
    workflow_targets = 0
    per_page = min(100, max(1, max_runs_per_workflow))

    for workflow_id_value in item.get('workflow_ids') or []:
        workflow_id = str(workflow_id_value)
        workflow_targets += 1
        collected_for_id = 0
        page = 1
        while collected_for_id < max_runs_per_workflow:
            if max_pages_per_workflow is not None and page > max_pages_per_workflow:
                break
            url = (
                f'https://api.github.com/repos/{repository}/actions/'
                f'workflows/{workflow_id}/runs'
            )
            params = {'per_page': per_page, 'page': page}
            if created_before:
                params['created'] = f'1970-01-01..{created_before[:10]}'
            response = client.get(url, params=params)
            requests += 1
            if response is None or response.status_code != 200:
                failures.append({
                    'repository_name': repository,
                    'workflow_path': workflow_path,
                    'workflow_id': workflow_id,
                    'page': page,
                    'status_code': response.status_code if response else None,
                })
                break
            payload = response.json()
            runs = payload.get('workflow_runs') or []
            if not runs:
                break
            for run in runs:
                if collected_for_id >= max_runs_per_workflow:
                    break
                records.append(as_record(repository, workflow_path, workflow_id, run))
                collected_for_id += 1
            if len(runs) < per_page:
                break
            page += 1
    return records, failures, requests, workflow_targets


def fetch_runs(
    manifest: Sequence[Mapping[str, Any]],
    output_dir: Path,
    max_runs_per_workflow: int,
    max_pages_per_workflow: Optional[int],
    workers: int = 4,
    created_before: Optional[str] = None,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / 'target_workflows.jsonl'
    with manifest_path.open('w', encoding='utf-8') as handle:
        for item in manifest:
            handle.write(json.dumps(item, ensure_ascii=False) + '\n')

    fetched_raw: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    failures: List[Dict[str, Any]] = []
    requests = 0
    workflow_targets = sum(len(item.get('workflow_ids') or []) for item in manifest)
    worker_count = max(1, int(workers))
    completed_targets = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                fetch_target,
                item,
                max_runs_per_workflow,
                max_pages_per_workflow,
                created_before,
            ): item
            for item in manifest
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                records, target_failures, request_count, _ = future.result()
            except Exception as exc:  # preserve one target failure
                records = []
                request_count = 0
                target_failures = [{
                    'repository_name': item.get('repository_name'),
                    'workflow_path': item.get('workflow_path'),
                    'workflow_ids': item.get('workflow_ids') or [],
                    'error': str(exc),
                }]
            fetched_raw.extend(records)
            failures.extend(target_failures)
            requests += request_count
            completed_targets += 1
            print(
                f'补充目标 {completed_targets}/{len(manifest)}，已获取 '
                f'{len(fetched_raw)} 条运行记录，API 请求 {requests}',
                flush=True,
            )

    fetched: List[Dict[str, Any]] = []
    for record in fetched_raw:
        key = (
            str(record.get('repository_name') or ''),
            str(record.get('run_id') or record.get('_id')),
            str(record.get('run_attempt') or 1),
        )
        if key in seen:
            continue
        seen.add(key)
        fetched.append(record)
    fetched.sort(key=lambda value: (
        str(value.get('repository_name') or ''),
        str(value.get('workflow_path') or ''),
        str(value.get('created_at') or ''),
        str(value.get('run_id') or ''),
        str(value.get('run_attempt') or 1),
    ))

    records_path = output_dir / 'supplemental_workflow_runs.jsonl'
    with records_path.open('w', encoding='utf-8') as handle:
        for record in fetched:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')

    metadata = {
        'target_workflows': len(manifest),
        'workflow_id_targets': workflow_targets,
        'max_runs_per_workflow_id': max_runs_per_workflow,
        'max_pages_per_workflow_id': max_pages_per_workflow,
        'workers': worker_count,
        'created_before': created_before,
        'api_requests': requests,
        'supplemental_runs_written': len(fetched),
        'fetch_failures': len(failures),
        'failures': failures,
        'records_path': str(records_path),
        'warning': (
            '这些记录是补充数据；分析时应与原始 workflow_runs.jsonl 合并并按 '
            '(repository_name, run_id, run_attempt) 去重。'
        ),
    }
    (output_dir / 'fetch_metadata.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(json.dumps({key: metadata[key] for key in (
        'target_workflows',
        'workflow_id_targets',
        'api_requests',
        'supplemental_runs_written',
        'fetch_failures',
        'records_path',
    )}, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Fetch additional runs for specified cache-smell workflow files'
    )
    parser.add_argument('--scan-dir', type=Path, default=DEFAULT_SCAN_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--max-runs-per-workflow-id', type=int, default=100)
    parser.add_argument('--max-pages-per-workflow-id', type=int, default=None)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument(
        '--created-before',
        default=None,
        help='Optional inclusive UTC date (YYYY-MM-DD) for the study-window upper bound',
    )
    parser.add_argument(
        '--smells',
        default=','.join(DEFAULT_SMELLS),
        help='Comma-separated normalized smell types',
    )
    args = parser.parse_args()
    if args.max_runs_per_workflow_id <= 0:
        raise SystemExit('--max-runs-per-workflow-id must be positive')
    if args.workers <= 0:
        raise SystemExit('--workers must be positive')
    smells = {
        value.strip() for value in str(args.smells).split(',') if value.strip()
    }
    manifest = build_target_manifest(args.scan_dir, smells)
    if not manifest:
        raise SystemExit('No target workflows found')
    fetch_runs(
        manifest,
        args.output_dir,
        args.max_runs_per_workflow_id,
        args.max_pages_per_workflow_id,
        args.workers,
        args.created_before,
    )


if __name__ == '__main__':
    main()
