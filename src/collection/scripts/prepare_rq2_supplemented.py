"""Build run-level records for API-supplemented workflow versions.

This is the bridge between ``supplement_workflow_snapshots.py`` and the
run-level RQ2 analysis.  It keeps only runs in the same study window and
default-branch scope as the supplement candidate list, first assigns each run
to the YAML fetched at its exact ``head_sha``, and then groups identical YAML
content into one logical workflow version.  Thus unrelated repository commits
do not split an unchanged workflow file into multiple analysis versions.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


DEFAULT_DATA_DIR = REPOSITORY_ROOT / 'data_26_6_9'
DEFAULT_INPUT_DIR = (
    REPOSITORY_ROOT
    / 'output_full_logical_20260831'
    / 'rq2_snapshot_supplement_20260609'
)
WORKFLOW_PREFIX = '.github/workflows/'


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


def logical_version_id(
    repository_name: str,
    workflow_path: str,
    content_hash: Any,
    exact_version_id: str,
) -> str:
    """Build a logical workflow version from the workflow file content.

    ``head_sha`` identifies the repository revision that triggered a run and
    remains the exact snapshot join key.  It should not, however, split one
    unchanged workflow file into a new analysis version every time unrelated
    source code is committed.  A content hash therefore defines the logical
    version; an exact snapshot id is retained as a conservative fallback when
    a hash is unavailable.
    """

    content_token = str(content_hash or '').strip()
    if content_token:
        return (
            f'{normalize_repo(repository_name)}|{workflow_path}|'
            f'content:{content_token}'
        )
    return exact_version_id


def load_repo_aliases(path: Path) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    by_alias: Dict[str, Dict[str, str]] = {}
    by_id: Dict[str, Dict[str, str]] = {}
    for entry in load_jsonl(path):
        repo = entry.get('repo') or {}
        repository_id = str(repo.get('id') or '')
        canonical = str(repo.get('name') or entry.get('_id') or '')
        default_branch = str(repo.get('defaultBranch') or '')
        if not repository_id or not canonical or not default_branch:
            continue
        item = by_id.setdefault(repository_id, {
            'repository_id': repository_id,
            'repository_name': canonical,
            'default_branch': default_branch,
        })
        for alias in (entry.get('_id'), repo.get('name')):
            alias_key = normalize_repo(alias)
            if alias_key:
                by_alias[alias_key] = item
    return by_alias, by_id


def load_scans(path: Path) -> Tuple[Dict[Tuple[str, str, str], Dict[str, Any]], set[str]]:
    scans: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    detector_errors: set[str] = set()
    for entry in load_jsonl(path):
        repository = normalize_repo(entry.get('repository_name'))
        workflow_path = str(entry.get('workflow_path') or '')
        sha = str(entry.get('commit_sha') or '')
        version = str(entry.get('version_id') or '')
        if not repository or not workflow_path or not sha or not version:
            continue
        scans[(repository, workflow_path, sha)] = entry
        detector = entry.get('detector') or {}
        if detector.get('error'):
            detector_errors.add(version)
    return scans, detector_errors


def build_logical_scan_index(
    scans: Mapping[Tuple[str, str, str], Dict[str, Any]],
) -> Tuple[
    Dict[Tuple[str, str, str], str],
    Dict[str, Dict[str, Any]],
    Dict[str, set[str]],
]:
    """Index exact snapshots by logical workflow-file content.

    The first exact scan entry is used as the representative detector profile
    because entries with the same content hash represent the same YAML bytes.
    All exact ``(repository, path, head_sha)`` mappings remain available for
    provenance and run matching.
    """

    exact_to_logical: Dict[Tuple[str, str, str], str] = {}
    logical_entries: Dict[str, Dict[str, Any]] = {}
    logical_head_shas: Dict[str, set[str]] = defaultdict(set)
    for exact_key, entry in scans.items():
        repository, workflow_path, head_sha = exact_key
        exact_version = str(entry.get('version_id') or '')
        logical_id = logical_version_id(
            repository,
            workflow_path,
            entry.get('content_hash'),
            exact_version,
        )
        exact_to_logical[exact_key] = logical_id
        logical_head_shas[logical_id].add(head_sha)
        if logical_id not in logical_entries:
            representative = dict(entry)
            representative['exact_snapshot_version_id'] = exact_version
            representative['logical_version_id'] = logical_id
            representative['version_id'] = logical_id
            logical_entries[logical_id] = representative
    return exact_to_logical, logical_entries, dict(logical_head_shas)


def prepare(
    data_dir: Path,
    input_dir: Path,
    output_dir: Path,
    as_of: datetime,
    window_start: Optional[datetime],
    additional_runs: Sequence[Path],
) -> Dict[str, Any]:
    aliases, _ = load_repo_aliases(data_dir / 'repo_info.jsonl')
    scans, detector_errors = load_scans(input_dir / 'supplemented_version_scan_results.jsonl')
    exact_to_logical, logical_scans, logical_head_shas = build_logical_scan_index(scans)
    sources = [data_dir / 'workflow_runs.jsonl', *additional_runs]
    for source in sources:
        if not source.exists():
            raise FileNotFoundError(source)

    output_dir.mkdir(parents=True, exist_ok=True)
    run_output = output_dir / 'run_records.jsonl'
    matched: list[Dict[str, Any]] = []
    seen_run_attempts: set[Tuple[str, str]] = set()
    stats: Dict[str, Any] = {
        'raw_run_records': 0,
        'completed_run_records': 0,
        'excluded_after_as_of': 0,
        'excluded_before_window_start': 0,
        'excluded_non_default_branch': 0,
        'excluded_missing_repository_mapping': 0,
        'excluded_non_workflow_path': 0,
        'excluded_missing_head_sha': 0,
        'runs_without_supplemented_snapshot': 0,
        'runs_with_detector_error': 0,
        'deduplicated_run_records': 0,
        'matched_run_records': 0,
        'version_ids_observed': 0,
        'exact_snapshot_ids_observed': len(scans),
        'logical_version_ids_observed': len(logical_scans),
        'repositories_observed': 0,
        'as_of': timestamp_text(as_of),
        'window_start': timestamp_text(window_start),
        'branch_scope': 'default branch only',
        'runs_sources': [str(source) for source in sources],
        'exact_snapshot_identity': '(repository_name, workflow_path, head_sha, content_hash)',
        'logical_version_identity': '(repository_name, workflow_path, content_hash)',
        'version_grouping_rule': (
            'runs first match an exact snapshot by repository, workflow_path, and '
            'head_sha; exact snapshots with the same workflow content_hash share '
            'one logical workflow version even when repository commits differ'
        ),
    }

    for source in sources:
        for run in load_jsonl(source):
            stats['raw_run_records'] += 1
            if str(run.get('status') or '').lower() != 'completed':
                continue
            stats['completed_run_records'] += 1
            created_at = parse_timestamp(run.get('created_at'))
            if created_at is None or created_at > as_of:
                stats['excluded_after_as_of'] += 1
                continue
            if window_start is not None and created_at < window_start:
                stats['excluded_before_window_start'] += 1
                continue
            repository = aliases.get(normalize_repo(run.get('repository_name')))
            if repository is None:
                stats['excluded_missing_repository_mapping'] += 1
                continue
            if run.get('head_branch') != repository['default_branch']:
                stats['excluded_non_default_branch'] += 1
                continue
            workflow_path = str(run.get('workflow_path') or '')
            if not workflow_path.startswith(WORKFLOW_PREFIX):
                stats['excluded_non_workflow_path'] += 1
                continue
            sha = str(run.get('head_sha') or '')
            if not sha:
                stats['excluded_missing_head_sha'] += 1
                continue
            scan = scans.get((
                normalize_repo(repository['repository_name']), workflow_path, sha,
            ))
            if scan is None:
                stats['runs_without_supplemented_snapshot'] += 1
                continue
            version = str(scan.get('version_id') or '')
            if version in detector_errors:
                stats['runs_with_detector_error'] += 1
                continue
            exact_key = (
                normalize_repo(repository['repository_name']), workflow_path, sha,
            )
            logical_version = exact_to_logical.get(exact_key)
            if not logical_version:
                stats['runs_without_supplemented_snapshot'] += 1
                continue
            run_id = run.get('run_id')
            if run_id not in (None, ''):
                dedup_key = (str(run_id), str(run.get('run_attempt') or 1))
                if dedup_key in seen_run_attempts:
                    stats['deduplicated_run_records'] += 1
                    continue
                seen_run_attempts.add(dedup_key)
            matched.append({
                'version_id': logical_version,
                'logical_version_id': logical_version,
                'exact_snapshot_version_id': version,
                'repository_name': repository['repository_name'],
                'workflow_path': workflow_path,
                'commit_sha': sha,
                'commit_date': scan.get('commit_date'),
                'next_commit_date': None,
                'commit_date_is_run_time_proxy': bool(
                    scan.get('commit_date_is_run_time_proxy')
                ),
                'content_hash': scan.get('content_hash'),
                'content_file': scan.get('content_file'),
                'version_sequence': 0,
                'run_id': run.get('run_id'),
                'run_attempt': run.get('run_attempt') or 1,
                'run_number': run.get('run_number'),
                'workflow_id': run.get('workflow_id'),
                'created_at': run.get('created_at'),
                'updated_at': run.get('updated_at'),
                'run_started_at': run.get('run_started_at'),
                'event': str(run.get('event') or '').lower(),
                'conclusion': str(run.get('conclusion') or '').lower(),
                'head_branch': run.get('head_branch'),
                'head_sha': sha,
                'duration_seconds': run.get('duration_seconds'),
            })

    matched.sort(key=lambda row: (
        str(row.get('version_id') or ''),
        str(row.get('created_at') or ''),
        str(row.get('run_id') or ''),
        str(row.get('run_attempt') or '1'),
    ))
    orders: Dict[str, int] = defaultdict(int)
    for row in matched:
        version = str(row.get('version_id') or '')
        orders[version] += 1
        row['run_order_in_version'] = orders[version]
        row['is_first_run_in_version'] = orders[version] == 1

    with run_output.open('w', encoding='utf-8') as handle:
        for row in matched:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    scan_copy = output_dir / 'version_scan_results.jsonl'
    with scan_copy.open('w', encoding='utf-8') as output:
        for entry in logical_scans.values():
            output.write(json.dumps(entry, ensure_ascii=False) + '\n')

    stats['matched_run_records'] = len(matched)
    stats['version_ids_observed'] = len({row['version_id'] for row in matched})
    stats['logical_version_ids_observed_in_runs'] = stats['version_ids_observed']
    stats['repositories_observed'] = len({row['repository_name'] for row in matched})
    multi_sha_logical_versions = {
        version for version, shas in logical_head_shas.items() if len(shas) > 1
    }
    stats['logical_versions_with_multiple_head_shas'] = len(multi_sha_logical_versions)
    stats['matched_runs_grouped_across_multiple_head_shas'] = sum(
        1 for row in matched if row['version_id'] in multi_sha_logical_versions
    )
    stats['detector_error_versions'] = len(detector_errors)
    stats['run_records_path'] = str(run_output)
    stats['version_scan_results_path'] = str(scan_copy)
    stats['matching_rule'] = (
        'completed default-branch run created_at within the fixed study window '
        'is assigned to the YAML fetched at the same run head_sha; runs whose '
        'matched YAML content_hash is identical share one logical workflow version'
    )
    metadata_path = output_dir / 'matching_metadata.json'
    with metadata_path.open('w', encoding='utf-8') as handle:
        json.dump(stats, handle, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description='Prepare run records for supplemented workflow versions')
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--as-of', default='2026-06-09T23:59:59Z')
    parser.add_argument('--window-start', default=None)
    parser.add_argument('--additional-runs', type=Path, action='append', default=[])
    args = parser.parse_args()
    as_of = parse_timestamp(args.as_of)
    window_start = parse_timestamp(args.window_start) if args.window_start else None
    if as_of is None:
        raise SystemExit('--as-of must be an ISO-8601 timestamp')
    if args.window_start and window_start is None:
        raise SystemExit('--window-start must be an ISO-8601 timestamp')
    if window_start is not None and window_start > as_of:
        raise SystemExit('--window-start must not be later than --as-of')
    output_dir = args.output_dir or args.input_dir / 'rq2_run_level'
    prepare(args.data_dir, args.input_dir, output_dir, as_of, window_start, args.additional_runs)


if __name__ == '__main__':
    main()
