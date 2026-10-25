"""Fetch current GitHub Actions job/step metadata for matched RQ2 runs.

This utility deliberately fetches metadata from the current GitHub Actions API
using the already version-matched ``run_records.jsonl``.  It does not use the
historical local log archive and does not modify any source dataset.

The default mode is a deterministic, coverage-aware 2,000-run RQ2 pilot.  The pilot is
intended to measure API availability, attempt matching, response sizes, and
failure/step metadata coverage before a larger collection is started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.utils.github_client import GitHubClient  # noqa: E402


DEFAULT_RUN_RECORDS = REPOSITORY_ROOT / 'output_full_logical_20260915_complete' / 'rq2_latest_version_interval_20260609_strict_20260915' / 'run_records.jsonl'
DEFAULT_VERSION_RECORDS = REPOSITORY_ROOT / 'output_full_logical_20260915_complete' / 'rq2_latest_version_interval_20260609_strict_20260915' / 'version_records.jsonl'
DEFAULT_SCAN_RESULTS = REPOSITORY_ROOT / 'output_full_logical_20260915_complete' / 'rq2_latest_version_interval_20260609_strict_20260915' / 'version_scan_results.jsonl'
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT
    / 'output_full_logical_20260915_complete'
    / 'rq2_job_resource_pilot_2000_20260922'
)

TECHNICAL_FAILURES = {'failure', 'startup_failure', 'timed_out'}
OTHER_NON_SUCCESS = {
    'cancelled',
    'skipped',
    'action_required',
    'neutral',
    'stale',
}

FORMAL_ENDPOINT_DIR = 'rq2_job_resource_sample_k2_20260923'


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


def run_key(record: Mapping[str, Any]) -> Optional[Tuple[str, str, str]]:
    repository = str(record.get('repository_name') or '').strip()
    run_id = record.get('run_id')
    if not repository or run_id in (None, ''):
        return None
    return repository, str(run_id), str(record.get('run_attempt') or 1)


def conclusion_bucket(value: Any) -> str:
    conclusion = str(value or '').strip().lower()
    if conclusion == 'success':
        return 'success'
    if conclusion in TECHNICAL_FAILURES:
        return 'technical_failure'
    if conclusion in OTHER_NON_SUCCESS:
        return 'other_non_success'
    return 'unknown'


def grouped_run_records(path: Path) -> Tuple[Dict[Tuple[str, str, str], List[Dict[str, Any]]], int]:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    raw_count = 0
    for record in load_jsonl(path):
        raw_count += 1
        key = run_key(record)
        if key is not None:
            grouped[key].append(record)
    return grouped, raw_count


def choose_sample(
    grouped: Mapping[Tuple[str, str, str], Sequence[Mapping[str, Any]]],
    sample_size: int,
    seed: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Select a deterministic sample with technical failures intentionally covered."""
    rng = random.Random(seed)
    by_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for key, records in grouped.items():
        primary = dict(records[0])
        primary['_run_key'] = list(key)
        primary['_duplicate_record_count'] = len(records)
        primary['_sample_bucket'] = conclusion_bucket(primary.get('conclusion'))
        by_bucket[primary['_sample_bucket']].append(primary)

    # Keep technical failures visible while retaining successful and other
    # non-success controls.  Targets are scaled for small datasets.
    nominal = {
        'success': 500,
        'technical_failure': 250,
        'other_non_success': 200,
        'unknown': 50,
    }
    if sample_size != 1000:
        nominal = {
            bucket: int(round(sample_size * count / 1000))
            for bucket, count in nominal.items()
        }
        assigned = sum(nominal.values())
        nominal['success'] += sample_size - assigned

    selected: List[Dict[str, Any]] = []
    actual: Dict[str, int] = {}
    remaining: Dict[str, List[Dict[str, Any]]] = {}
    for bucket in ('technical_failure', 'success', 'other_non_success', 'unknown'):
        candidates = list(by_bucket.get(bucket, []))
        rng.shuffle(candidates)
        take = min(nominal.get(bucket, 0), len(candidates))
        selected.extend(candidates[:take])
        actual[bucket] = take
        remaining[bucket] = candidates[take:]

    # Fill any shortfall from unused records, preserving deterministic order.
    shortfall = max(0, sample_size - len(selected))
    if shortfall:
        fallback: List[Dict[str, Any]] = []
        for bucket in ('success', 'technical_failure', 'other_non_success', 'unknown'):
            fallback.extend(remaining[bucket])
        rng.shuffle(fallback)
        for item in fallback[:shortfall]:
            selected.append(item)
            actual[item['_sample_bucket']] = actual.get(item['_sample_bucket'], 0) + 1

    rng.shuffle(selected)
    return selected, {
        'requested_sample_size': sample_size,
        'actual_sample_size': len(selected),
        'target_by_bucket': nominal,
        'actual_by_bucket': actual,
        'available_unique_runs_by_bucket': {
            bucket: len(items) for bucket, items in by_bucket.items()
        },
        'random_seed': seed,
    }


def scan_smells_by_version(path: Path) -> Dict[str, List[str]]:
    """Load cache-smell labels without using any historical/RQ3 inputs."""
    labels: Dict[str, List[str]] = {}
    if not path or not path.exists():
        return labels
    for item in load_jsonl(path):
        version_id = str(item.get('version_id') or item.get('logical_version_id') or '').strip()
        detector = item.get('detector') or {}
        smells = detector.get('smells') if isinstance(detector, Mapping) else []
        types = sorted({str(s.get('type')).strip() for s in (smells or []) if isinstance(s, Mapping) and s.get('type')})
        if version_id:
            labels[version_id] = types
    return labels


def choose_rq2_pilot_sample(
    grouped: Mapping[Tuple[str, str, str], Sequence[Mapping[str, Any]]],
    scan_labels: Mapping[str, Sequence[str]],
    sample_size: int,
    seed: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Deterministic coverage-first, repository-capped RQ2 pilot sampling."""
    rng = random.Random(seed)
    candidates: List[Dict[str, Any]] = []
    for key, records in grouped.items():
        item = dict(records[0])
        item['_run_key'] = list(key)
        item['_duplicate_record_count'] = len(records)
        item['_sample_bucket'] = conclusion_bucket(item.get('conclusion'))
        item['_cache_smell_types'] = list(scan_labels.get(str(item.get('version_id') or item.get('logical_version_id') or ''), []))
        item['_has_cache_smell'] = bool(item['_cache_smell_types'])
        candidates.append(item)
    rng.shuffle(candidates)
    selected: List[Dict[str, Any]] = []
    selected_keys: set[Tuple[str, str, str]] = set()
    repo_counts: Counter[str] = Counter()
    max_per_repo = max(2, math.ceil(sample_size / max(len({str(x.get('repository_name')) for x in candidates}), 1)))
    max_per_repo = min(max_per_repo, 4)

    def add(item: Dict[str, Any]) -> bool:
        key = run_key(item)
        repo = str(item.get('repository_name') or '')
        if key is None or key in selected_keys or repo_counts[repo] >= max_per_repo or len(selected) >= sample_size:
            return False
        selected.append(item)
        selected_keys.add(key)
        repo_counts[repo] += 1
        return True

    # Seed every formal cache smell, both smell strata, required events, and outcomes.
    requirements: List[Any] = []
    all_smells = sorted({s for item in candidates for s in item['_cache_smell_types']})
    requirements.extend(('smell', s) for s in all_smells)
    requirements.extend([('smell', None), ('bucket', 'success'), ('bucket', 'technical_failure'), ('event', 'push'), ('event', 'schedule'), ('event', 'workflow_dispatch')])
    for kind, value in requirements:
        pool = [x for x in candidates if (value in x['_cache_smell_types'] if kind == 'smell' and value else (not x['_has_cache_smell'] if kind == 'smell' else (x.get('event') == value if kind == 'event' else x['_sample_bucket'] == value)))]
        rng.shuffle(pool)
        for item in pool:
            if add(item):
                break

    # Fill by randomized repository round-robin to reduce over-contribution.
    remaining = [x for x in candidates if run_key(x) not in selected_keys]
    while len(selected) < sample_size and remaining:
        by_repo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for item in remaining:
            by_repo[str(item.get('repository_name') or '')].append(item)
        repos = list(by_repo)
        rng.shuffle(repos)
        progressed = False
        for repo in repos:
            pool = by_repo[repo]
            rng.shuffle(pool)
            if add(pool[0]):
                progressed = True
        remaining = [x for x in remaining if run_key(x) not in selected_keys]
        if not progressed:
            break
    rng.shuffle(selected)
    return selected, {
        'requested_sample_size': sample_size,
        'actual_sample_size': len(selected),
        'random_seed': seed,
        'max_runs_per_repository': max_per_repo,
        'sampling_method': 'coverage-first then randomized repository round-robin',
        'available_unique_runs': len(candidates),
        'available_unique_repositories': len({x.get('repository_name') for x in candidates}),
        'available_cache_smell_types': all_smells,
    }


def choose_bucket_sample(
    grouped: Mapping[Tuple[str, str, str], Sequence[Mapping[str, Any]]],
    bucket: str,
    sample_size: int,
    seed: int,
    all_in_bucket: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Select one bucket for a staged expansion after the pilot."""
    rng = random.Random(seed)
    candidates: List[Dict[str, Any]] = []
    for key, records in grouped.items():
        primary = dict(records[0])
        item_bucket = conclusion_bucket(primary.get('conclusion'))
        if bucket != 'all' and item_bucket != bucket:
            continue
        primary['_run_key'] = list(key)
        primary['_duplicate_record_count'] = len(records)
        primary['_sample_bucket'] = item_bucket
        candidates.append(primary)
    rng.shuffle(candidates)
    selected = candidates if all_in_bucket else candidates[:sample_size]
    return selected, {
        'requested_sample_size': len(candidates) if all_in_bucket else sample_size,
        'actual_sample_size': len(selected),
        'selection_bucket': bucket,
        'all_in_bucket': all_in_bucket,
        'available_unique_runs_in_bucket': len(candidates),
        'actual_by_bucket': dict(Counter(item['_sample_bucket'] for item in selected)),
        'random_seed': seed,
    }


def jsonable_step(step: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'name': step.get('name'),
        'status': step.get('status'),
        'conclusion': step.get('conclusion'),
        'number': step.get('number'),
        'started_at': step.get('started_at'),
        'completed_at': step.get('completed_at'),
    }


def summarize_jobs(
    selected: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    matched_attempt: str,
    request_count: int,
    response_bytes: int,
) -> Dict[str, Any]:
    job_conclusions: Counter[str] = Counter()
    step_conclusions: Counter[str] = Counter()
    failed_jobs: List[Dict[str, Any]] = []
    failed_steps: List[Dict[str, Any]] = []
    job_summaries: List[Dict[str, Any]] = []

    for job in jobs:
        job_conclusion = str(job.get('conclusion') or '').lower() or 'none'
        job_conclusions[job_conclusion] += 1
        steps = job.get('steps') or []
        step_items: List[Dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_conclusion = str(step.get('conclusion') or '').lower() or 'none'
            step_conclusions[step_conclusion] += 1
            compact = jsonable_step(step)
            step_items.append(compact)
            if step_conclusion in TECHNICAL_FAILURES:
                failed_steps.append({
                    'job_name': job.get('name'),
                    'job_id': job.get('id'),
                    **compact,
                })
        if job_conclusion in TECHNICAL_FAILURES:
            failed_jobs.append({
                'name': job.get('name'),
                'id': job.get('id'),
                'conclusion': job_conclusion,
                'status': job.get('status'),
            })
        job_summaries.append({
            'name': job.get('name'),
            'id': job.get('id'),
            'status': job.get('status'),
            'conclusion': job_conclusion,
            'run_attempt': job.get('run_attempt'),
            'started_at': job.get('started_at'),
            'completed_at': job.get('completed_at'),
            'labels': job.get('labels'),
            'steps': step_items,
        })

    return {
        'repository_name': selected.get('repository_name'),
        'run_id': selected.get('run_id'),
        'run_attempt': selected.get('run_attempt') or 1,
        'version_id': selected.get('version_id'),
        'logical_version_id': selected.get('logical_version_id'),
        'exact_snapshot_version_id': selected.get('exact_snapshot_version_id'),
        'workflow_path': selected.get('workflow_path'),
        'workflow_id': selected.get('workflow_id'),
        'head_sha': selected.get('head_sha'),
        'created_at': selected.get('created_at'),
        'conclusion': selected.get('conclusion'),
        'event': selected.get('event'),
        'cache_smell_types': selected.get('_cache_smell_types', []),
        'has_cache_smell': selected.get('_has_cache_smell', False),
        'sample_bucket': selected.get('_sample_bucket'),
        'duplicate_record_count': selected.get('_duplicate_record_count', 1),
        'matched_attempt': matched_attempt,
        'jobs_count': len(jobs),
        'job_conclusion_counts': dict(sorted(job_conclusions.items())),
        'step_conclusion_counts': dict(sorted(step_conclusions.items())),
        'failed_jobs': failed_jobs,
        'failed_steps': failed_steps,
        'jobs': job_summaries,
        'api_request_count': request_count,
        'response_bytes': response_bytes,
        'fetched_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'error': None,
    }


def fetch_one(
    client: GitHubClient,
    selected: Mapping[str, Any],
) -> Dict[str, Any]:
    repository = str(selected.get('repository_name') or '')
    run_id = str(selected.get('run_id') or '')
    target_attempt = str(selected.get('run_attempt') or 1)
    endpoint = (
        f'https://api.github.com/repos/{quote(repository, safe="/")}'
        f'/actions/runs/{quote(run_id, safe="")}/jobs'
    )
    all_jobs: List[Dict[str, Any]] = []
    request_count = 0
    response_bytes = 0
    page = 1
    try:
        while True:
            response = client.get(
                endpoint,
                params={'per_page': 100, 'page': page, 'filter': 'all'},
            )
            request_count += 1
            if response is None:
                raise RuntimeError('no response')
            response_bytes += len(response.content or b'')
            if response.status_code != 200:
                result = dict(selected)
                result.update({
                    'sample_bucket': selected.get('_sample_bucket'),
                    'matched_attempt': 'http_error',
                    'jobs_count': 0,
                    'api_request_count': request_count,
                    'response_bytes': response_bytes,
                    'fetched_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'error': f'HTTP {response.status_code}',
                })
                return result
            payload = response.json()
            page_jobs = payload.get('jobs') or []
            all_jobs.extend(job for job in page_jobs if isinstance(job, Mapping))
            if len(page_jobs) < 100:
                break
            page += 1
        matching = [
            job for job in all_jobs
            if str(job.get('run_attempt') or 1) == target_attempt
        ]
        if matching:
            return summarize_jobs(
                selected, matching, 'exact', request_count, response_bytes
            )
        # A small number of old API responses may omit run_attempt.  Do not
        # silently claim an exact match; retain the metadata as unresolved.
        no_attempt_jobs = [job for job in all_jobs if 'run_attempt' not in job]
        if no_attempt_jobs and len(all_jobs) == len(no_attempt_jobs):
            return summarize_jobs(
                selected, no_attempt_jobs, 'field_missing', request_count, response_bytes
            )
        return summarize_jobs(
            selected, [], 'not_found', request_count, response_bytes
        ) | {'available_attempts': sorted({str(job.get('run_attempt')) for job in all_jobs})}
    except Exception as exc:  # Keep one API failure from aborting the pilot.
        result = dict(selected)
        result.update({
            'sample_bucket': selected.get('_sample_bucket'),
            'matched_attempt': 'request_error',
            'jobs_count': 0,
            'api_request_count': request_count,
            'response_bytes': response_bytes,
            'fetched_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'error': str(exc),
        })
        return result


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def parse_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def job_os(labels: Any) -> str:
    normalized = [str(label).lower() for label in labels] if isinstance(labels, list) else []
    joined = ' '.join(normalized)
    if 'self-hosted' in normalized:
        return 'self-hosted'
    if any(value in joined for value in ('linux', 'ubuntu')):
        return 'Linux'
    if 'windows' in joined:
        return 'Windows'
    if any(value in joined for value in ('macos', 'mac-os', 'osx')):
        return 'macOS'
    return 'unknown'


def build_audit_and_summary(
    selected: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    elapsed_seconds: float,
    total_unique_runs: int,
) -> Tuple[Dict[str, Any], str]:
    result_by_key = {run_key(item): item for item in results if run_key(item) is not None}
    jobs = [job for item in results for job in (item.get('jobs') or []) if isinstance(job, Mapping)]
    successful = [item for item in results if not item.get('error')]
    exact = [item for item in results if item.get('matched_attempt') == 'exact']
    with_jobs = [item for item in results if int(item.get('jobs_count') or 0) > 0]
    complete_runs = [item for item in with_jobs if all(job.get('started_at') and job.get('completed_at') for job in (item.get('jobs') or []))]
    missing_job_timestamps = sum(1 for job in jobs if not job.get('started_at') or not job.get('completed_at'))
    labels_missing = sum(1 for job in jobs if job.get('labels') is None)
    os_counts = Counter(job_os(job.get('labels')) for job in jobs)
    vm_differences: List[float] = []
    vm_rows = 0
    for selected_item in selected:
        result = result_by_key.get(run_key(selected_item))
        if not result or result not in complete_runs:
            continue
        durations = []
        for job in result.get('jobs') or []:
            started_at = parse_utc(job.get('started_at'))
            completed_at = parse_utc(job.get('completed_at'))
            if started_at and completed_at:
                durations.append((completed_at - started_at).total_seconds())
        wall = selected_item.get('duration_seconds')
        if durations and isinstance(wall, (int, float)):
            vm_rows += 1
            vm_differences.append(sum(durations) - float(wall))
    request_count = sum(int(item.get('api_request_count') or 0) for item in results)
    requested = len(selected)
    smell_missing: Dict[str, Dict[str, Any]] = {}
    for label, predicate in (
        ('with_cache_smell', lambda item: bool(item.get('_has_cache_smell'))),
        ('without_cache_smell', lambda item: not bool(item.get('_has_cache_smell'))),
    ):
        layer = [item for item in selected if predicate(item)]
        missing = sum(1 for item in layer if not result_by_key.get(run_key(item)) or int(result_by_key[run_key(item)].get('jobs_count') or 0) == 0)
        smell_missing[label] = {'sample_size': len(layer), 'missing_jobs_runs': missing, 'missing_rate': missing / len(layer) if layer else None}
    audit = {
        'requested_runs': requested,
        'successful_responses': len(successful),
        'api_request_count': request_count,
        'pagination': {
            'runs_with_multiple_pages': sum(1 for item in results if int(item.get('api_request_count') or 0) > 1),
            'maximum_pages_for_one_run': max((int(item.get('api_request_count') or 0) for item in results), default=0),
            'page_request_count': request_count,
        },
        'http_404_count': sum(1 for item in results if item.get('error') == 'HTTP 404'),
        'request_error_count': sum(1 for item in results if item.get('matched_attempt') == 'request_error'),
        'other_error_count': sum(1 for item in results if item.get('error') and item.get('error') != 'HTTP 404' and item.get('matched_attempt') != 'request_error'),
        'exact_run_attempt_matches': len(exact),
        'exact_run_attempt_match_rate': len(exact) / requested if requested else 0.0,
        'runs_with_at_least_one_job': len(with_jobs),
        'runs_with_at_least_one_job_rate': len(with_jobs) / requested if requested else 0.0,
        'runs_with_all_job_timestamps': len(complete_runs),
        'runs_with_all_job_timestamps_rate': len(complete_runs) / requested if requested else 0.0,
        'jobs_count': len(jobs),
        'jobs_missing_started_or_completed_at': missing_job_timestamps,
        'jobs_missing_labels': labels_missing,
        'job_os_counts': {name: os_counts.get(name, 0) for name in ('Linux', 'Windows', 'macOS', 'self-hosted', 'unknown')},
        'bytes_received': sum(int(item.get('response_bytes') or 0) for item in results),
        'elapsed_seconds': elapsed_seconds,
        'vm_time_validation': {
            'runs_compared': vm_rows,
            'vmseconds_minus_wall_clock_seconds': {
                'mean': mean(vm_differences) if vm_differences else None,
                'median': median(vm_differences) if vm_differences else None,
                'p95': percentile(vm_differences, 0.95),
            },
        },
        'missing_jobs_by_smell_group': smell_missing,
        'full_collection_estimate': {
            'unique_runs': total_unique_runs,
            'estimated_api_requests': round(total_unique_runs * request_count / requested) if requested else None,
            'estimated_elapsed_seconds_at_pilot_throughput': round(total_unique_runs * elapsed_seconds / requested, 1) if requested else None,
        },
    }
    recognized = len(jobs) - os_counts.get('unknown', 0)
    with_smell = smell_missing['with_cache_smell']
    without_smell = smell_missing['without_cache_smell']
    missing_gap = None
    if with_smell['missing_rate'] is not None and without_smell['missing_rate'] is not None:
        missing_gap = abs(with_smell['missing_rate'] - without_smell['missing_rate'])
    summary = f"""# RQ2 job resource pilot summary

- Requested runs: {requested}; successful Jobs API responses: {len(successful)} ({len(successful) / requested:.2%}).
- Exact `run_attempt` matches: {len(exact)} ({len(exact) / requested:.2%}).
- Runs with at least one job: {len(with_jobs)} ({len(with_jobs) / requested:.2%}).
- Runs whose jobs all have API-level `started_at` and `completed_at`: {len(complete_runs)} ({len(complete_runs) / requested:.2%}); jobs missing either timestamp: {missing_job_timestamps}.
- Runner OS/self-hosted recognized from `job.labels`: {recognized}/{len(jobs)} ({recognized / len(jobs) if jobs else 0:.2%}); unknown: {os_counts.get('unknown', 0)}.
- Missing-job rate: smell group {with_smell['missing_rate'] if with_smell['missing_rate'] is not None else 0:.2%}; no-smell group {without_smell['missing_rate'] if without_smell['missing_rate'] is not None else 0:.2%}; absolute gap {missing_gap if missing_gap is not None else 0:.2%}.
- `VMSeconds_run` is the sum of API job durations within the same `run_id` and `run_attempt`; `duration_seconds` is workflow wall-clock duration. Parallel jobs can make `VMSeconds_run` larger, so equality is neither expected nor required.
- VM/wall-clock comparison is available for {vm_rows} runs; median `VMSeconds_run - duration_seconds` is {median(vm_differences) if vm_differences else 'N/A'} seconds.
- Technical feasibility: {'supported by this pilot' if len(successful) / requested >= 0.9 and len(complete_runs) / requested >= 0.8 else 'not yet established'}.
- Estimated full collection: approximately {audit['full_collection_estimate']['estimated_api_requests']} API requests and {audit['full_collection_estimate']['estimated_elapsed_seconds_at_pilot_throughput']} seconds at observed pilot throughput. No full collection was started.
"""
    return audit, summary


def stable_identity(record: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(record.get('repository_name') or '').strip(),
        str(record.get('workflow_path') or '').strip(),
        str(record.get('workflow_id') or '').strip(),
    )


def stable_rank(seed: int, identity: Tuple[str, str, str], endpoint: str, record: Mapping[str, Any]) -> str:
    payload = '|'.join([str(seed), endpoint, *identity, str(record.get('run_id')), str(record.get('run_attempt') or 1)])
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def formal_scan_labels(path: Path) -> Dict[str, List[str]]:
    return scan_smells_by_version(path)


def generate_formal_manifest(args: argparse.Namespace) -> int:
    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in load_jsonl(args.run_records):
        identity = stable_identity(record)
        if all(identity):
            grouped[identity].append(record)
    labels = formal_scan_labels(args.version_scan_results)
    manifest_path = args.sample_manifest
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint_totals = {'all_completed': 0, 'successful_duration': 0}
    endpoint_selected = {'all_completed': 0, 'successful_duration': 0}
    selected: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for identity in sorted(grouped):
        records = grouped[identity]
        completed = [r for r in records if str(r.get('status') or '').lower() == 'completed']
        successful = [r for r in completed if str(r.get('conclusion') or '').lower() == 'success']
        choices = {
            'all_completed': completed,
            'successful_duration': successful,
        }
        for endpoint, candidates in choices.items():
            endpoint_totals[endpoint] += len(candidates)
            ranked = sorted(candidates, key=lambda r: stable_rank(args.seed, identity, endpoint, r))[:2]
            endpoint_selected[endpoint] += len(ranked)
            for record in ranked:
                key = run_key(record)
                if key is None:
                    continue
                item = selected.setdefault(key, {
                    'repository_name': record.get('repository_name'),
                    'repository_id': record.get('repository_id'),
                    'workflow_path': record.get('workflow_path'),
                    'workflow_id': record.get('workflow_id'),
                    'workflow_name': record.get('workflow_name'),
                    'version_id': record.get('version_id'),
                    'logical_version_id': record.get('logical_version_id'),
                    'exact_snapshot_version_id': record.get('exact_snapshot_version_id'),
                    'run_id': record.get('run_id'),
                    'run_attempt': record.get('run_attempt') or 1,
                    'conclusion': record.get('conclusion'),
                    'event': record.get('event'),
                    'run_order_in_version': record.get('run_order_in_version'),
                    'cache_smell_types': labels.get(str(record.get('version_id') or record.get('logical_version_id') or ''), []),
                    'all_completed_total_runs': len(completed),
                    'all_completed_sampled_runs': min(2, len(completed)),
                    'all_completed_endpoint_inclusion_probability': min(2, len(completed)) / len(completed) if completed else 0.0,
                    'successful_duration_total_runs': len(successful),
                    'successful_duration_sampled_runs': min(2, len(successful)),
                    'successful_duration_endpoint_inclusion_probability': min(2, len(successful)) / len(successful) if successful else 0.0,
                    'selected_for_all_completed': False,
                    'selected_for_successful_duration': False,
                })
                item[f'selected_for_{endpoint}'] = True
    with manifest_path.open('w', encoding='utf-8', newline='\n') as handle:
        for key in sorted(selected):
            handle.write(json.dumps(selected[key], ensure_ascii=False) + '\n')
    metadata = {
        'run_records': str(args.run_records),
        'version_records': str(args.version_records),
        'version_scan_results': str(args.version_scan_results),
        'sample_manifest': str(manifest_path),
        'random_seed': args.seed,
        'sampling_rule': 'independent deterministic top-2 by sha256 rank per workflow identity and endpoint',
        'workflow_identity': ['repository_name', 'workflow_path', 'workflow_id'],
        'run_key': ['repository_name', 'run_id', 'run_attempt'],
        'workflow_identity_count': len(grouped),
        'unique_run_count': len(selected),
        'endpoint_total_runs': endpoint_totals,
        'endpoint_selected_before_run_key_deduplication': endpoint_selected,
        'selected_for_both_endpoints': sum(1 for item in selected.values() if item['selected_for_all_completed'] and item['selected_for_successful_duration']),
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'rq3_used': False,
    }
    metadata_path = manifest_path.parent / 'sampling_metadata.json'
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'manifest': str(manifest_path), 'unique_run_count': len(selected), 'endpoint_selected': endpoint_selected}, ensure_ascii=False))
    return 0


class ResourceClient:
    """Small per-token client for the formal resource-only acquisition."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github+json'}
        self.rate_remaining = None
        self.rate_reset = None
        self.lock = threading.Lock()
        self.local = threading.local()
        self.api_requests = 0
        self.http_404 = 0
        self.fallback_requests = 0
        self.request_errors = 0
        self.rate_limit_errors = 0

    def session(self) -> requests.Session:
        session = getattr(self.local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            self.local.session = session
        return session

    def get(self, url: str, params: Mapping[str, Any]) -> Optional[requests.Response]:
        for attempt in range(4):
            with self.lock:
                remaining = self.rate_remaining
                reset = self.rate_reset
            if remaining is not None and remaining < 2 and reset:
                time.sleep(max(0, reset - time.time()) + 2)
            try:
                response = self.session().get(url, params=params, timeout=45)
                with self.lock:
                    self.api_requests += 1
                    if response.headers.get('X-RateLimit-Remaining'):
                        self.rate_remaining = int(response.headers['X-RateLimit-Remaining'])
                    if response.headers.get('X-RateLimit-Reset'):
                        self.rate_reset = int(response.headers['X-RateLimit-Reset'])
                if response.status_code == 200 or response.status_code == 404:
                    if response.status_code == 404:
                        with self.lock:
                            self.http_404 += 1
                    return response
                if response.status_code in (403, 429):
                    with self.lock:
                        self.rate_limit_errors += 1
                    retry_after = response.headers.get('Retry-After')
                    delay = float(retry_after) if retry_after else min(120, 2 ** attempt)
                    if response.status_code == 403 and self.rate_remaining and self.rate_remaining <= 0 and self.rate_reset:
                        delay = max(delay, self.rate_reset - time.time() + 2)
                    time.sleep(max(1, delay))
                    continue
                if response.status_code >= 500:
                    time.sleep(min(60, 2 ** attempt))
                    continue
                return response
            except requests.RequestException:
                with self.lock:
                    self.request_errors += 1
                if attempt == 3:
                    return None
                time.sleep(min(30, 2 ** attempt))
        return None


def token_from_env(name: str) -> str:
    value = os.environ.get(name)
    if value and value != 'your_github_token_here':
        return value
    from src.utils.env_loader import load_env_file
    values = load_env_file()
    value = values.get(name)
    if not value or value == 'your_github_token_here':
        raise ValueError(f'missing credential env {name}')
    return value


def verify_credentials(args: argparse.Namespace) -> int:
    token1 = token_from_env('GITHUB_TOKEN')
    token2 = token_from_env('GITHUB_TOKEN_2')
    clients = [ResourceClient(token1), ResourceClient(token2)]
    rate_before = []
    users = []
    for client in clients:
        response = client.get('https://api.github.com/rate_limit', {})
        if response is None or response.status_code != 200:
            raise RuntimeError('credential rate_limit verification failed')
        payload = response.json()
        core = payload.get('resources', {}).get('core', {})
        if not isinstance(core.get('remaining'), int) or core.get('remaining', 0) <= 0:
            raise RuntimeError('credential rate_limit quota is unavailable')
        rate_before.append(core['remaining'])
        response = client.get('https://api.github.com/user', {})
        if response is None or response.status_code != 200:
            raise RuntimeError('credential user verification failed')
        users.append(response.json().get('id'))
    pilot_items = list(load_jsonl(args.pilot_sample))
    if not pilot_items:
        raise RuntimeError('pilot sample is empty')
    sample = pilot_items[0]
    url = f"https://api.github.com/repos/{quote(str(sample['repository_name']), safe='/')}/actions/runs/{sample['run_id']}/attempts/{sample.get('run_attempt') or 1}/jobs"
    for client in clients:
        response = client.get(url, {'per_page': 100, 'page': 1})
        if response is None or response.status_code != 200:
            raise RuntimeError('credential attempt-specific Jobs API verification failed')
    before_token2 = clients[1].rate_remaining
    response = clients[0].get('https://api.github.com/rate_limit', {})
    if response is None or response.status_code != 200:
        raise RuntimeError('token 1 billing request verification failed')
    after_token2 = clients[1].rate_remaining
    result = {
        'rate_limit_authenticated': True,
        'attempt_specific_jobs_200_both': True,
        'distinct_accounts': bool(users[0] is not None and users[1] is not None and users[0] != users[1]),
        'token_2_remaining_unchanged_after_token_1_request': before_token2 == after_token2,
    }
    if not result['distinct_accounts'] or not result['token_2_remaining_unchanged_after_token_1_request']:
        raise RuntimeError('credential independence verification failed')
    args.credential_output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


def resource_job_summary(job: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: job.get(key) for key in ('id', 'name', 'status', 'conclusion', 'run_attempt', 'started_at', 'completed_at', 'labels', 'runner_id', 'runner_name', 'runner_group_id', 'runner_group_name')}


def fetch_resource_item(client: ResourceClient, item: Mapping[str, Any]) -> Dict[str, Any]:
    repo = str(item.get('repository_name') or '')
    run_id = str(item.get('run_id') or '')
    attempt = str(item.get('run_attempt') or 1)
    base = f'https://api.github.com/repos/{quote(repo, safe="/")}/actions/runs/{quote(run_id, safe="")}'
    primary = f'{base}/attempts/{quote(attempt, safe="")}/jobs'
    all_jobs: List[Mapping[str, Any]] = []
    page = 1
    requests_used = 0
    fallback = False
    fallback_reason = None
    response = client.get(primary, {'per_page': 100, 'page': page})
    requests_used += 1
    if response is not None and response.status_code == 404:
        fallback = True
        fallback_reason = 'attempt_specific_404'
        client.fallback_requests += 1
        response = client.get(f'{base}/jobs', {'per_page': 100, 'page': page, 'filter': 'all'})
        requests_used += 1
    if response is None:
        return {**dict(item), 'matched_attempt': 'request_error', 'jobs': [], 'jobs_count': 0, 'api_request_count': requests_used, 'error': 'request_error', 'fallback_used': fallback, 'fallback_reason': fallback_reason}
    if response.status_code != 200:
        return {**dict(item), 'matched_attempt': 'http_error', 'jobs': [], 'jobs_count': 0, 'api_request_count': requests_used, 'error': f'HTTP {response.status_code}', 'fallback_used': fallback, 'fallback_reason': fallback_reason}
    while True:
        payload = response.json()
        page_jobs = payload.get('jobs') or []
        all_jobs.extend(job for job in page_jobs if isinstance(job, Mapping))
        if len(page_jobs) < 100:
            break
        page += 1
        response = client.get(primary if not fallback else f'{base}/jobs', {'per_page': 100, 'page': page, **({'filter': 'all'} if fallback else {})})
        requests_used += 1
        if response is None or response.status_code != 200:
            return {**dict(item), 'matched_attempt': 'request_error', 'jobs': [], 'jobs_count': 0, 'api_request_count': requests_used, 'error': 'pagination_error', 'fallback_used': fallback, 'fallback_reason': fallback_reason}
    matching = [job for job in all_jobs if str(job.get('run_attempt') or 1) == attempt]
    if fallback and not matching:
        return {**dict(item), 'matched_attempt': 'attempt_not_found', 'jobs': [], 'jobs_count': 0, 'api_request_count': requests_used, 'error': 'attempt_not_found', 'fallback_used': fallback, 'fallback_reason': fallback_reason}
    return {**dict(item), 'matched_attempt': 'exact' if matching else ('fallback_no_attempt' if fallback else 'not_found'), 'jobs': [resource_job_summary(job) for job in matching], 'jobs_count': len(matching), 'api_request_count': requests_used, 'error': None, 'fallback_used': fallback, 'fallback_reason': fallback_reason}


def acquire_resource_shard(args: argparse.Namespace) -> int:
    if args.shard_count != 2 or args.shard_index not in (0, 1):
        raise SystemExit('formal resource acquisition requires --shard-count 2 and --shard-index 0 or 1')
    token_name = args.token_env
    token = token_from_env(token_name)
    records = list(load_jsonl(args.sample_manifest))
    selected = [item for item in records if int(hashlib.sha256(f"{item.get('repository_name')}{item.get('run_id')}{item.get('run_attempt') or 1}".encode()).hexdigest(), 16) % args.shard_count == args.shard_index]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / 'run_job_metadata.jsonl'
    done_keys = set()
    if args.resume and output_path.exists():
        done_keys = {run_key(item) for item in load_jsonl(output_path) if run_key(item)}
    pending = [item for item in selected if run_key(item) not in done_keys]
    client = ResourceClient(token)
    started = time.monotonic()
    mode = 'a' if args.resume and output_path.exists() else 'w'
    completed = len(done_keys)
    with output_path.open(mode, encoding='utf-8', newline='\n') as handle:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            iterator = iter(pending)
            futures = {executor.submit(fetch_resource_item, client, next(iterator)): True for _ in range(min(args.workers * 4, len(pending)))}
            while futures:
                for future in list(as_completed(futures)):
                    futures.pop(future, None)
                    result = future.result()
                    handle.write(json.dumps(result, ensure_ascii=False) + '\n')
                    completed += 1
                    if completed % 50 == 0:
                        handle.flush()
                        (args.output_dir / 'acquisition_checkpoint.json').write_text(json.dumps({'shard_index': args.shard_index, 'completed': completed, 'target': len(selected), 'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}, indent=2) + '\n', encoding='utf-8')
                    try:
                        futures[executor.submit(fetch_resource_item, client, next(iterator))] = True
                    except StopIteration:
                        pass
    handle = None
    results = list(load_jsonl(output_path))
    jobs = [job for item in results for job in item.get('jobs', [])]
    audit = {
        'shard_index': args.shard_index, 'shard_count': args.shard_count, 'token_env': token_name,
        'requested_runs': len(selected), 'completed_records': len(results), 'successful_responses': sum(1 for x in results if not x.get('error')),
        'api_request_count': sum(int(x.get('api_request_count') or 0) for x in results), 'fallback_count': sum(1 for x in results if x.get('fallback_used')),
        'fallback_reasons': dict(Counter(str(x.get('fallback_reason')) for x in results if x.get('fallback_reason'))),
        'http_404_count': sum(1 for x in results if x.get('error') == 'HTTP 404'), 'attempt_not_found_count': sum(1 for x in results if x.get('error') == 'attempt_not_found'),
        'request_error_count': sum(1 for x in results if x.get('error') and x.get('error') not in ('HTTP 404', 'attempt_not_found')),
        'exact_attempt_matches': sum(1 for x in results if x.get('matched_attempt') == 'exact'), 'jobs_count': len(jobs),
        'jobs_missing_timestamps': sum(1 for j in jobs if not j.get('started_at') or not j.get('completed_at')), 'jobs_missing_labels': sum(1 for j in jobs if j.get('labels') is None),
        'elapsed_seconds': round(time.monotonic() - started, 3), 'rate_limit_errors': client.rate_limit_errors, 'request_errors': client.request_errors,
        'job_os_counts': dict(Counter(job_os(j.get('labels')) for j in jobs)), 'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    (args.output_dir / 'acquisition_checkpoint.json').write_text(json.dumps({'shard_index': args.shard_index, 'completed': len(results), 'target': len(selected), 'finished': True}, indent=2) + '\n', encoding='utf-8')
    (args.output_dir / 'acquisition_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'shard_index': args.shard_index, 'completed': len(results), 'api_requests': audit['api_request_count'], 'elapsed_seconds': audit['elapsed_seconds']}, ensure_ascii=False))
    return 0


def merge_resource_shards(args: argparse.Namespace) -> int:
    root = args.output_dir
    manifest = list(load_jsonl(args.sample_manifest))
    manifest_keys = {run_key(item) for item in manifest if run_key(item)}
    shard_records: List[List[Dict[str, Any]]] = []
    shard_audits = []
    for index in (0, 1):
        shard_dir = root / f'shard_{index}'
        shard_records.append(list(load_jsonl(shard_dir / 'run_job_metadata.jsonl')))
        shard_audits.append(json.loads((shard_dir / 'acquisition_audit.json').read_text(encoding='utf-8')))
    shard_keys = [{run_key(item) for item in records if run_key(item)} for records in shard_records]
    overlap = shard_keys[0] & shard_keys[1]
    merged_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    duplicate_count = 0
    for records in shard_records:
        for item in records:
            key = run_key(item)
            if key is None:
                continue
            if key in merged_by_key:
                duplicate_count += 1
                continue
            merged_by_key[key] = item
    missing = manifest_keys - set(merged_by_key)
    extra = set(merged_by_key) - manifest_keys
    if overlap or missing or extra:
        raise RuntimeError(f'merge validation failed: overlap={len(overlap)}, missing={len(missing)}, extra={len(extra)}')
    merged_dir = root / 'merged'
    merged_dir.mkdir(parents=True, exist_ok=True)
    merged_path = merged_dir / 'run_job_metadata.jsonl'
    with merged_path.open('w', encoding='utf-8', newline='\n') as handle:
        for key in sorted(merged_by_key):
            handle.write(json.dumps(merged_by_key[key], ensure_ascii=False) + '\n')
    records = list(merged_by_key.values())
    jobs = [job for item in records for job in item.get('jobs', [])]
    exact = sum(1 for item in records if item.get('matched_attempt') == 'exact')
    runs_with_jobs = sum(1 for item in records if int(item.get('jobs_count') or 0) > 0)
    complete_jobs = sum(1 for job in jobs if job.get('started_at') and job.get('completed_at'))
    os_counts = Counter(job_os(job.get('labels')) for job in jobs)
    recognized = len(jobs) - os_counts.get('unknown', 0)
    audit = {
        'manifest_runs': len(manifest_keys),
        'merged_records': len(records),
        'shard_record_counts': [len(items) for items in shard_records],
        'shard_key_overlap': len(overlap),
        'missing_manifest_keys': len(missing),
        'extra_run_keys': len(extra),
        'duplicates_removed': duplicate_count,
        'api_request_count': sum(int(a.get('api_request_count') or 0) for a in shard_audits),
        'elapsed_seconds_by_shard': [a.get('elapsed_seconds') for a in shard_audits],
        'successful_responses': sum(int(a.get('successful_responses') or 0) for a in shard_audits),
        'exact_attempt_matches': exact,
        'exact_attempt_match_rate': exact / len(records) if records else 0.0,
        'runs_with_jobs': runs_with_jobs,
        'runs_with_jobs_rate': runs_with_jobs / len(records) if records else 0.0,
        'fallback_count': sum(int(a.get('fallback_count') or 0) for a in shard_audits),
        'http_404_count': sum(int(a.get('http_404_count') or 0) for a in shard_audits),
        'attempt_not_found_count': sum(int(a.get('attempt_not_found_count') or 0) for a in shard_audits),
        'request_error_count': sum(int(a.get('request_error_count') or 0) for a in shard_audits),
        'rate_limit_events': sum(int(a.get('rate_limit_errors') or 0) for a in shard_audits),
        'transport_request_errors_with_retries': sum(int(a.get('request_errors') or 0) for a in shard_audits),
        'jobs_count': len(jobs),
        'jobs_with_complete_timestamps': complete_jobs,
        'job_timestamp_completeness_rate': complete_jobs / len(jobs) if jobs else 0.0,
        'jobs_missing_labels': sum(1 for job in jobs if job.get('labels') is None),
        'job_os_counts': {name: os_counts.get(name, 0) for name in ('Linux', 'Windows', 'macOS', 'self-hosted', 'unknown')},
        'os_self_hosted_recognition_rate': recognized / len(jobs) if jobs else 0.0,
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    (merged_dir / 'acquisition_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = f"""# RQ2 job resource sample summary

- Manifest and merged runs: {len(records):,}; shard overlap: {len(overlap)}; missing manifest keys: {len(missing)}.
- API requests: {audit['api_request_count']:,}; shard elapsed times: {audit['elapsed_seconds_by_shard'][0]:,.3f}s and {audit['elapsed_seconds_by_shard'][1]:,.3f}s.
- Exact run-attempt matches: {exact:,}/{len(records):,} ({audit['exact_attempt_match_rate']:.2%}); runs with jobs: {runs_with_jobs:,} ({audit['runs_with_jobs_rate']:.2%}).
- Jobs: {len(jobs):,}; timestamp completeness: {audit['job_timestamp_completeness_rate']:.4%}; OS/self-hosted recognition from labels: {audit['os_self_hosted_recognition_rate']:.2%}.
- Attempt-specific 404/fallback: {audit['http_404_count']}/{audit['fallback_count']}; attempt not found after fallback: {audit['attempt_not_found_count']}; terminal request errors: {audit['request_error_count']}.
- Rate-limit responses handled: {audit['rate_limit_events']}; transport errors retried: {audit['transport_request_errors_with_retries']}.
- The merged resource sample is suitable for VM-time calculation for jobs with complete API-level `started_at` and `completed_at`. Economic cost analysis is not performed here.
"""
    (merged_dir / 'resource_sample_summary.md').write_text(summary, encoding='utf-8')
    print(json.dumps({'merged_records': len(records), 'exact_attempt_match_rate': audit['exact_attempt_match_rate'], 'jobs': len(jobs)}, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Fetch current GitHub Actions job metadata for matched RQ2 runs.'
    )
    parser.add_argument('--run-records', type=Path, default=DEFAULT_RUN_RECORDS)
    parser.add_argument('--version-records', type=Path, default=DEFAULT_VERSION_RECORDS)
    parser.add_argument('--version-scan-results', type=Path, default=DEFAULT_SCAN_RESULTS)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--sample-size', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument(
        '--bucket',
        choices=('stratified', 'rq2_pilot', 'technical_failure', 'success', 'other_non_success', 'all'),
        default='rq2_pilot',
        help='Use the pilot stratification or select one bucket for staged expansion.',
    )
    parser.add_argument(
        '--all-in-bucket',
        action='store_true',
        help='Fetch every unique run in --bucket instead of capping at --sample-size.',
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Append only runs not already present in the output JSONL.',
    )
    parser.add_argument('--sample-manifest', type=Path)
    parser.add_argument('--generate-manifest', action='store_true')
    parser.add_argument('--token-env', default='GITHUB_TOKEN')
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--resource-only', action='store_true')
    parser.add_argument('--verify-credentials', action='store_true')
    parser.add_argument('--pilot-sample', type=Path)
    parser.add_argument('--credential-output', type=Path)
    parser.add_argument('--smoke-limit', type=int)
    parser.add_argument('--merge-shards', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.merge_shards:
        if not args.sample_manifest:
            raise SystemExit('--merge-shards requires --sample-manifest')
        return merge_resource_shards(args)
    if args.generate_manifest:
        if not args.sample_manifest:
            raise SystemExit('--generate-manifest requires --sample-manifest')
        return generate_formal_manifest(args)
    if args.verify_credentials:
        if not args.pilot_sample or not args.credential_output:
            raise SystemExit('--verify-credentials requires --pilot-sample and --credential-output')
        return verify_credentials(args)
    if args.resource_only:
        if not args.sample_manifest:
            raise SystemExit('--resource-only requires --sample-manifest')
        if args.smoke_limit is not None:
            original = list(load_jsonl(args.sample_manifest))
            smoke_path = args.output_dir / '_smoke_manifest.jsonl'
            smoke_path.parent.mkdir(parents=True, exist_ok=True)
            smoke_items = [x for x in original if int(hashlib.sha256(f"{x.get('repository_name')}{x.get('run_id')}{x.get('run_attempt') or 1}".encode()).hexdigest(), 16) % args.shard_count == args.shard_index][:args.smoke_limit]
            smoke_path.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in smoke_items), encoding='utf-8')
            args.sample_manifest = smoke_path
        return acquire_resource_shard(args)
    if args.sample_size <= 0:
        raise SystemExit('--sample-size must be positive')
    if args.workers <= 0:
        raise SystemExit('--workers must be positive')
    if not args.run_records.exists():
        raise SystemExit(f'run records not found: {args.run_records}')

    started = time.monotonic()
    grouped, raw_count = grouped_run_records(args.run_records)
    scan_labels = scan_smells_by_version(args.version_scan_results)
    if args.bucket == 'rq2_pilot':
        selected, sampling = choose_rq2_pilot_sample(grouped, scan_labels, args.sample_size, args.seed)
    elif args.bucket == 'stratified':
        if args.all_in_bucket:
            raise SystemExit('--all-in-bucket requires a non-stratified --bucket')
        selected, sampling = choose_sample(grouped, args.sample_size, args.seed)
    else:
        selected, sampling = choose_bucket_sample(
            grouped,
            args.bucket,
            args.sample_size,
            args.seed,
            args.all_in_bucket,
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / 'run_job_metadata.jsonl'
    metadata_path = args.output_dir / 'sampling_metadata.json'

    existing_results: List[Dict[str, Any]] = []
    existing_keys: set[Tuple[str, str, str]] = set()
    if args.resume and output_path.exists():
        for item in load_jsonl(output_path):
            key = run_key(item)
            if key is None or key in existing_keys:
                continue
            existing_results.append(item)
            existing_keys.add(key)
    pending = [
        item for item in selected
        if run_key(item) not in existing_keys
    ]

    client = GitHubClient()
    new_results: List[Dict[str, Any]] = []
    completed = 0
    output_mode = 'a' if existing_results else 'w'
    with output_path.open('a' if output_mode == 'a' else 'w', encoding='utf-8', newline='\n') as handle:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_one, client, item): index
                for index, item in enumerate(pending)
            }
            for future in as_completed(futures):
                result = future.result()
                new_results.append(result)
                handle.write(json.dumps(result, ensure_ascii=False) + '\n')
                handle.flush()
                completed += 1
                if completed % 50 == 0 or completed == len(pending):
                    print(f'progress={completed}/{len(pending)}', flush=True)

    final_results = existing_results + new_results

    response_sizes = [
        float(item.get('response_bytes') or 0)
        for item in final_results if item.get('response_bytes') is not None
    ]
    status_counts = Counter(
        'ok' if not item.get('error') else str(item.get('error'))
        for item in final_results
    )
    attempt_counts = Counter(str(item.get('matched_attempt')) for item in final_results)
    metadata = {
        'run_records': str(args.run_records),
        'output_file': str(output_path),
        'raw_run_record_count': raw_count,
        'version_records': str(args.version_records),
        'version_scan_results': str(args.version_scan_results),
        'unique_run_count': len(grouped),
        'duplicate_run_record_groups': sum(
            1 for records in grouped.values() if len(records) > 1
        ),
        'duplicate_run_record_count': sum(
            len(records) - 1 for records in grouped.values() if len(records) > 1
        ),
        **sampling,
        'workers': args.workers,
        'resume': args.resume,
        'resumed_existing_results': len(existing_results),
        'new_results': len(new_results),
        'pending_before_fetch': len(pending),
        'completed_results': len(final_results),
        'status_counts': dict(status_counts),
        'attempt_match_counts': dict(attempt_counts),
        'exact_attempt_match_rate': (
            attempt_counts.get('exact', 0) / len(final_results)
            if final_results else 0.0
        ),
        'jobs_available_rate': (
            sum(1 for item in final_results if item.get('jobs_count', 0) > 0)
            / len(final_results)
            if final_results else 0.0
        ),
        'api_request_count': sum(
            int(item.get('api_request_count') or 0) for item in final_results
        ),
        'bytes_received': sum(response_sizes),
        'response_size_bytes': {
            'mean': mean(response_sizes) if response_sizes else None,
            'median': median(response_sizes) if response_sizes else None,
            'p95': percentile(response_sizes, 0.95),
        },
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'rate_limit_remaining_after_pilot': client.rate_limit_remaining,
        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'data_policy': {
            'source': 'current GitHub Actions Jobs API',
            'stale_local_log_archive_used': False,
            'version_alignment_fields_retained': [
                'version_id',
                'logical_version_id',
                'exact_snapshot_version_id',
                'head_sha',
                'workflow_path',
                'run_id',
                'run_attempt',
            ],
        },
    }
    pilot_path = args.output_dir / 'pilot_sample.jsonl'
    with pilot_path.open('w', encoding='utf-8', newline='\n') as handle:
        for item in selected:
            output_item = dict(item)
            output_item.pop('_run_key', None)
            output_item.pop('_duplicate_record_count', None)
            output_item.pop('_sample_bucket', None)
            output_item.pop('_cache_smell_types', None)
            output_item.pop('_has_cache_smell', None)
            output_item['cache_smell_types'] = item.get('_cache_smell_types', [])
            output_item['has_cache_smell'] = item.get('_has_cache_smell', False)
            handle.write(json.dumps(output_item, ensure_ascii=False) + '\n')
    metadata['pilot_sample_file'] = str(pilot_path)
    metadata['sample_counts'] = {
        'repositories': len({item.get('repository_name') for item in selected}),
        'workflows': len({(item.get('repository_name'), item.get('workflow_path'), item.get('workflow_id')) for item in selected}),
        'events': dict(Counter(str(item.get('event') or 'unknown') for item in selected)),
        'conclusions': dict(Counter(conclusion_bucket(item.get('conclusion')) for item in selected)),
        'cache_smell_runs': sum(1 for item in selected if item.get('_has_cache_smell')),
        'no_cache_smell_runs': sum(1 for item in selected if not item.get('_has_cache_smell')),
        'cache_smell_types': dict(Counter(s for item in selected for s in item.get('_cache_smell_types', []))),
    }
    audit, summary = build_audit_and_summary(selected, final_results, metadata['elapsed_seconds'], len(grouped))
    (args.output_dir / 'acquisition_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output_dir / 'pilot_summary.md').write_text(summary, encoding='utf-8')
    metadata['acquisition_audit_file'] = str(args.output_dir / 'acquisition_audit.json')
    metadata['pilot_summary_file'] = str(args.output_dir / 'pilot_summary.md')
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    print(json.dumps({
        'output': str(output_path),
        'metadata': str(metadata_path),
        'completed': len(final_results),
        'exact_attempt_match_rate': metadata['exact_attempt_match_rate'],
        'jobs_available_rate': metadata['jobs_available_rate'],
        'elapsed_seconds': metadata['elapsed_seconds'],
        'rate_limit_remaining': client.rate_limit_remaining,
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
