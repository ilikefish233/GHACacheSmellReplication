"""Prepare a default-branch, workflow-revision-aligned RQ2 cohort.

The workflow-history collector stores a revision whenever the content of a
workflow file changes on the repository's default branch.  This script joins
completed workflow runs to those revisions using the interval

    [revision_commit_time, next_revision_commit_time)

and writes one aggregated observation per observed revision interval.  Runs
from non-default branches, runs before the first retained history entry, and
runs for which the repository default branch cannot be resolved are excluded.

The script intentionally does not use ``workflow_files.jsonl``'s current
snapshot time.  That snapshot is useful for a current-state scan but cannot
establish which historical YAML revision executed a past run.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPOSITORY_ROOT / 'data_26_6_9'
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / 'output_full_logical_20260831' / 'rq2_versioned_default'
)
CORE_CI_EVENTS = {'push', 'pull_request', 'merge_group'}
MAX_WORKFLOW_DURATION_SECONDS = 35 * 24 * 60 * 60
TECHNICAL_SUCCESS = {'success'}
TECHNICAL_FAILURES = {'failure', 'startup_failure', 'timed_out'}

WorkflowKey = Tuple[str, str]
VersionKey = Tuple[str, str, str, str]


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


def as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def workflow_key(entry: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = str(entry.get('repository_name') or '')
    workflow_path = str(entry.get('workflow_path') or '')
    if not repository or not workflow_path:
        return None
    return repository, workflow_path


@dataclass(frozen=True)
class WorkflowVersion:
    repository_name: str
    workflow_path: str
    commit_sha: str
    commit_date: datetime
    content_hash: str
    content_file: str
    prev_content_hash: Optional[str] = None
    sequence: int = 0
    end_date: Optional[datetime] = None

    @property
    def key(self) -> VersionKey:
        return (
            self.repository_name,
            self.workflow_path,
            self.commit_sha,
            self.content_hash,
        )

    @property
    def version_id(self) -> str:
        return (
            f'{self.repository_name}|{self.workflow_path}|'
            f'{self.commit_sha}|{self.commit_date.isoformat()}'
        )


@dataclass
class VersionAggregate:
    version: WorkflowVersion
    completed_count: int = 0
    success_count: int = 0
    technical_success_count: int = 0
    technical_failure_count: int = 0
    valid_completed_duration_count: int = 0
    valid_success_duration_count: int = 0
    completed_durations: List[float] = field(default_factory=list)
    successful_durations: List[float] = field(default_factory=list)
    event_counts: Counter = field(default_factory=Counter)
    conclusion_counts: Counter = field(default_factory=Counter)

    def to_record(self) -> Dict[str, Any]:
        def median(values: Sequence[float]) -> Optional[float]:
            if not values:
                return None
            ordered = sorted(values)
            middle = len(ordered) // 2
            if len(ordered) % 2:
                return float(ordered[middle])
            return float((ordered[middle - 1] + ordered[middle]) / 2.0)

        return {
            'version_id': self.version.version_id,
            'repository_name': self.version.repository_name,
            'workflow_path': self.version.workflow_path,
            'commit_sha': self.version.commit_sha,
            'commit_date': timestamp_text(self.version.commit_date),
            'next_commit_date': timestamp_text(self.version.end_date),
            'content_hash': self.version.content_hash,
            'content_file': self.version.content_file,
            'prev_content_hash': self.version.prev_content_hash,
            'sequence': self.version.sequence,
            'completed_count': self.completed_count,
            'success_count': self.success_count,
            'technical_success_count': self.technical_success_count,
            'technical_failure_count': self.technical_failure_count,
            'technical_total_count': (
                self.technical_success_count + self.technical_failure_count
            ),
            'valid_completed_duration_count': self.valid_completed_duration_count,
            'valid_success_duration_count': self.valid_success_duration_count,
            'completed_duration_median': median(self.completed_durations),
            'successful_duration_median': median(self.successful_durations),
            'event_counts': dict(self.event_counts),
            'conclusion_counts': dict(self.conclusion_counts),
        }


def load_default_branches(path: Path) -> Tuple[Dict[str, str], int]:
    """Return aliases to default branch names and count conflicting aliases."""

    branches: Dict[str, str] = {}
    conflicts = 0
    for entry in load_jsonl(path):
        repository = entry.get('repo') or {}
        default_branch = repository.get('defaultBranch')
        if not isinstance(default_branch, str) or not default_branch:
            continue
        aliases = (entry.get('_id'), repository.get('name'))
        for alias in aliases:
            alias = str(alias or '')
            if not alias:
                continue
            previous = branches.get(alias)
            if previous is not None and previous != default_branch:
                conflicts += 1
                continue
            branches[alias] = default_branch
    return branches, conflicts


def load_versions(path: Path) -> Dict[WorkflowKey, List[WorkflowVersion]]:
    """Load and order the retained workflow-file history by commit time."""

    raw: Dict[WorkflowKey, Dict[Tuple[str, str], Dict[str, Any]]] = defaultdict(dict)
    for entry in load_jsonl(path):
        key = workflow_key(entry)
        commit_date = parse_timestamp(entry.get('commit_date'))
        commit_sha = str(entry.get('commit_sha') or '')
        content_hash = str(entry.get('content_hash') or '')
        content_file = str(entry.get('content_file') or '')
        if (
            key is None
            or commit_date is None
            or not commit_sha
            or not content_hash
            or not content_file
        ):
            continue
        # The same commit/content pair may occur more than once in the raw
        # history.  It is one version event, whereas a later revert to the
        # same content hash is intentionally retained as a new event.
        raw[key][(commit_sha, content_hash)] = {
            'repository_name': key[0],
            'workflow_path': key[1],
            'commit_sha': commit_sha,
            'commit_date': commit_date,
            'content_hash': content_hash,
            'content_file': content_file,
            'prev_content_hash': entry.get('prev_content_hash'),
        }

    timelines: Dict[WorkflowKey, List[WorkflowVersion]] = {}
    for key, entries in raw.items():
        ordered = sorted(
            entries.values(),
            key=lambda value: (
                value['commit_date'],
                value['commit_sha'],
                value['content_file'],
            ),
        )
        versions: List[WorkflowVersion] = []
        for index, value in enumerate(ordered):
            next_date = (
                ordered[index + 1]['commit_date']
                if index + 1 < len(ordered)
                else None
            )
            versions.append(
                WorkflowVersion(
                    repository_name=value['repository_name'],
                    workflow_path=value['workflow_path'],
                    commit_sha=value['commit_sha'],
                    commit_date=value['commit_date'],
                    content_hash=value['content_hash'],
                    content_file=value['content_file'],
                    prev_content_hash=value.get('prev_content_hash'),
                    sequence=index,
                    end_date=next_date,
                )
            )
        timelines[key] = versions
    return timelines


def select_version(
    versions: Sequence[WorkflowVersion],
    created_at: datetime,
) -> Tuple[Optional[WorkflowVersion], str]:
    """Select the version interval containing ``created_at``."""

    if not versions:
        return None, 'missing_timeline'
    dates = [version.commit_date for version in versions]
    index = bisect.bisect_right(dates, created_at) - 1
    if index < 0:
        return None, 'before_first_retained_version'
    return versions[index], 'after_last_or_within_version_interval'


def _update_aggregate(aggregate: VersionAggregate, run: Mapping[str, Any]) -> Dict[str, int]:
    conclusion = str(run.get('conclusion') or '').lower()
    event = str(run.get('event') or '').lower()
    aggregate.completed_count += 1
    aggregate.conclusion_counts[conclusion] += 1
    aggregate.event_counts[event] += 1

    if conclusion in TECHNICAL_SUCCESS:
        aggregate.success_count += 1
        aggregate.technical_success_count += 1
    elif conclusion in TECHNICAL_FAILURES:
        aggregate.technical_failure_count += 1

    duration = as_number(run.get('duration_seconds'))
    counters = {
        'invalid_duration_missing_or_nonpositive': 0,
        'invalid_duration_over_max_seconds': 0,
        'valid_duration_completed_runs': 0,
        'valid_duration_success_runs': 0,
    }
    if duration is None or duration <= 0:
        counters['invalid_duration_missing_or_nonpositive'] = 1
    elif duration > MAX_WORKFLOW_DURATION_SECONDS:
        counters['invalid_duration_over_max_seconds'] = 1
    else:
        aggregate.valid_completed_duration_count += 1
        aggregate.completed_durations.append(duration)
        counters['valid_duration_completed_runs'] = 1
        if conclusion in TECHNICAL_SUCCESS:
            aggregate.valid_success_duration_count += 1
            aggregate.successful_durations.append(duration)
            counters['valid_duration_success_runs'] = 1
    return counters


def prepare_cohort(
    data_dir: Path,
    output_dir: Path,
    event_scope: str = 'all',
    runs_path: Optional[Path] = None,
    additional_runs_paths: Optional[Sequence[Path]] = None,
    max_run_created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    default_branches, branch_alias_conflicts = load_default_branches(
        data_dir / 'repo_info.jsonl'
    )
    timelines = load_versions(data_dir / 'workflow_changes.jsonl')
    aggregates: Dict[VersionKey, VersionAggregate] = {}
    matched_runs: List[Dict[str, Any]] = []
    seen_run_attempts: Dict[Tuple[str, str], WorkflowKey] = {}

    metadata: Dict[str, Any] = {
        'raw_run_records': 0,
        'completed_run_records_total': 0,
        'completed_runs_missing_default_branch_mapping': 0,
        'completed_runs_missing_head_branch': 0,
        'completed_runs_non_default_branch': 0,
        'completed_runs_excluded_non_core_ci_events': 0,
        'completed_runs_missing_workflow_history': 0,
        'completed_runs_missing_created_at': 0,
        'completed_runs_before_first_retained_version': 0,
        'completed_runs_assigned_to_version': 0,
        'deduplicated_run_records': 0,
        'duplicate_run_ids_across_repository_names': 0,
        'version_timelines': len(timelines),
        'version_events_after_deduplication': sum(len(v) for v in timelines.values()),
        'observed_version_intervals': 0,
        'observed_workflow_identities': 0,
        'observed_repositories': 0,
        'invalid_duration_missing_or_nonpositive': 0,
        'invalid_duration_over_max_seconds': 0,
        'valid_duration_completed_runs': 0,
        'valid_duration_success_runs': 0,
        'runs_excluded_after_created_cutoff': 0,
        'event_scope': event_scope,
        'default_branch_aliases': len(default_branches),
        'default_branch_alias_conflicts': branch_alias_conflicts,
        'conclusion_counts': Counter(),
    }

    runs_sources = [runs_path or (data_dir / 'workflow_runs.jsonl')]
    runs_sources.extend(additional_runs_paths or [])
    for source in runs_sources:
        if not source.exists():
            raise FileNotFoundError(f'Workflow runs file not found: {source}')
    metadata['runs_source'] = str(runs_sources[0])
    metadata['runs_sources'] = [str(source) for source in runs_sources]
    if max_run_created_at is not None:
        metadata['max_run_created_at'] = timestamp_text(max_run_created_at)
    for entry in (
        entry
        for source in runs_sources
        for entry in load_jsonl(source)
    ):
        metadata['raw_run_records'] += 1
        entry_created_at = parse_timestamp(entry.get('created_at'))
        if (
            max_run_created_at is not None
            and entry_created_at is not None
            and entry_created_at > max_run_created_at
        ):
            metadata['runs_excluded_after_created_cutoff'] += 1
            continue
        if entry.get('status') != 'completed':
            continue
        metadata['completed_run_records_total'] += 1

        repository_name = str(entry.get('repository_name') or '')
        default_branch = default_branches.get(repository_name)
        if default_branch is None:
            metadata['completed_runs_missing_default_branch_mapping'] += 1
            continue
        head_branch = entry.get('head_branch')
        if not isinstance(head_branch, str) or not head_branch:
            metadata['completed_runs_missing_head_branch'] += 1
            continue
        if head_branch != default_branch:
            metadata['completed_runs_non_default_branch'] += 1
            continue

        event = str(entry.get('event') or '').lower()
        if event_scope == 'core_ci' and event not in CORE_CI_EVENTS:
            metadata['completed_runs_excluded_non_core_ci_events'] += 1
            continue

        key = workflow_key(entry)
        if key is None or key not in timelines:
            metadata['completed_runs_missing_workflow_history'] += 1
            continue
        created_at = parse_timestamp(entry.get('created_at'))
        if created_at is None:
            metadata['completed_runs_missing_created_at'] += 1
            continue

        version, reason = select_version(timelines[key], created_at)
        if version is None:
            if reason == 'before_first_retained_version':
                metadata['completed_runs_before_first_retained_version'] += 1
            else:
                metadata['completed_runs_missing_workflow_history'] += 1
            continue

        run_id = entry.get('run_id')
        if run_id not in (None, ''):
            dedup_key = (str(run_id), str(entry.get('run_attempt') or 1))
            previous_key = seen_run_attempts.get(dedup_key)
            if previous_key is not None:
                metadata['deduplicated_run_records'] += 1
                if previous_key[0] != key[0]:
                    metadata['duplicate_run_ids_across_repository_names'] += 1
                continue
            seen_run_attempts[dedup_key] = key

        aggregate = aggregates.get(version.key)
        if aggregate is None:
            aggregate = VersionAggregate(version=version)
            aggregates[version.key] = aggregate
        metadata['completed_runs_assigned_to_version'] += 1
        metadata['conclusion_counts'][str(entry.get('conclusion') or '').lower()] += 1
        duration_counts = _update_aggregate(aggregate, entry)
        for name, count in duration_counts.items():
            metadata[name] += count

        # Preserve the run-level assignment as well as the interval aggregate.
        # This enables sensitivity analyses to distinguish first/cold-start
        # executions from later executions without changing the matching rule.
        matched_runs.append({
            'version_id': version.version_id,
            'repository_name': version.repository_name,
            'workflow_path': version.workflow_path,
            'commit_sha': version.commit_sha,
            'commit_date': timestamp_text(version.commit_date),
            'next_commit_date': timestamp_text(version.end_date),
            'content_hash': version.content_hash,
            'content_file': version.content_file,
            'version_sequence': version.sequence,
            'run_id': entry.get('run_id'),
            'run_attempt': entry.get('run_attempt') or 1,
            'run_number': entry.get('run_number'),
            'workflow_id': entry.get('workflow_id'),
            'created_at': entry.get('created_at'),
            'updated_at': entry.get('updated_at'),
            'run_started_at': entry.get('run_started_at'),
            'event': event,
            'conclusion': str(entry.get('conclusion') or '').lower(),
            'head_branch': head_branch,
            'head_sha': entry.get('head_sha'),
            'duration_seconds': as_number(entry.get('duration_seconds')),
        })

    # Make the within-version order explicit.  A run with order 1 is only a
    # temporal proxy for a cold start; cache hit/miss logs are not available.
    matched_runs.sort(key=lambda value: (
        str(value.get('version_id') or ''),
        str(value.get('created_at') or ''),
        str(value.get('run_id') or ''),
        str(value.get('run_attempt') or '1'),
    ))
    version_orders: Dict[str, int] = defaultdict(int)
    for run in matched_runs:
        version_id = str(run.get('version_id') or '')
        version_orders[version_id] += 1
        run['run_order_in_version'] = version_orders[version_id]
        run['is_first_run_in_version'] = version_orders[version_id] == 1

    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / 'version_records.jsonl'
    observed = sorted(
        aggregates.values(),
        key=lambda aggregate: (
            aggregate.version.repository_name,
            aggregate.version.workflow_path,
            aggregate.version.sequence,
        ),
    )
    with records_path.open('w', encoding='utf-8') as handle:
        for aggregate in observed:
            handle.write(json.dumps(aggregate.to_record(), ensure_ascii=False) + '\n')

    run_records_path = output_dir / 'run_records.jsonl'
    with run_records_path.open('w', encoding='utf-8') as handle:
        for run in matched_runs:
            handle.write(json.dumps(run, ensure_ascii=False, allow_nan=False) + '\n')

    metadata['observed_version_intervals'] = len(observed)
    metadata['observed_workflow_identities'] = len({
        (aggregate.version.repository_name, aggregate.version.workflow_path)
        for aggregate in observed
    })
    metadata['observed_repositories'] = len({
        aggregate.version.repository_name for aggregate in observed
    })
    metadata['conclusion_counts'] = dict(metadata['conclusion_counts'])
    metadata.update({
        'revision_alignment': (
            'default-branch workflow revision interval matching: each completed run '
            'is assigned by created_at to [workflow-change commit_date, next '
            'workflow-change commit_date); exact head_sha equality is not required '
            'because workflow_changes.jsonl stores workflow-file commit history '
            'but not a complete commit-parent graph'
        ),
        'branch_scope': (
            'completed runs with head_branch exactly equal to the repository default '
            'branch; non-default branches and missing branch mappings are excluded'
        ),
        'history_censoring': (
            'workflow_changes.jsonl contains the retained history returned by the '
            'collector (up to 50 changes per file); runs before the earliest '
            'retained change are excluded as left-censored'
        ),
        'duration_definition': (
            'duration_seconds > 0 and <= 35 days; each revision interval stores '
            'the median of its valid completed durations and the median among '
            'technically successful durations'
        ),
        'technical_reliability_definition': (
            'success / (success + failure + startup_failure + timed_out); '
            'skipped, cancelled, action_required and missing conclusions are '
            'excluded from the technical reliability denominator'
        ),
        'run_records_path': str(run_records_path),
        'matched_run_records_written': len(matched_runs),
        'within_version_order_definition': (
            'runs are ordered by created_at, run_id, and run_attempt within each '
            'matched version; order 1 is a temporal first-run proxy, not an '
            'observed cache miss'
        ),
    })
    metadata_path = output_dir / 'matching_metadata.json'
    with metadata_path.open('w', encoding='utf-8') as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(f'Version records: {records_path}')
    print(f'Matching metadata: {metadata_path}')
    print(json.dumps({
        key: metadata[key]
        for key in (
            'raw_run_records',
            'completed_run_records_total',
            'completed_runs_missing_default_branch_mapping',
            'completed_runs_missing_head_branch',
            'completed_runs_non_default_branch',
            'completed_runs_missing_workflow_history',
            'completed_runs_before_first_retained_version',
            'completed_runs_assigned_to_version',
            'deduplicated_run_records',
            'observed_version_intervals',
            'observed_workflow_identities',
            'observed_repositories',
            'valid_duration_completed_runs',
            'valid_duration_success_runs',
        )
    }, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Prepare a default-branch workflow-version RQ2 cohort'
    )
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--event-scope', choices=['all', 'core_ci'], default='all')
    parser.add_argument(
        '--runs-file',
        type=Path,
        action='append',
        dest='runs_files',
        default=None,
        help=(
            'Workflow-runs JSONL file; repeat to add supplemental files. '
            'The first file defaults to <data-dir>/workflow_runs.jsonl.'
        ),
    )
    parser.add_argument(
        '--max-run-created-at',
        type=str,
        default=None,
        help='Optional inclusive UTC timestamp for the run study-window upper bound',
    )
    args = parser.parse_args()
    runs_files = args.runs_files or []
    primary_runs_file = runs_files[0] if runs_files else None
    additional_runs_files = runs_files[1:]
    max_run_created_at = None
    if args.max_run_created_at:
        max_run_created_at = parse_timestamp(args.max_run_created_at)
        if max_run_created_at is None:
            raise SystemExit('--max-run-created-at must be an ISO-8601 timestamp')
    prepare_cohort(
        args.data_dir,
        args.output_dir,
        args.event_scope,
        primary_runs_file,
        additional_runs_files,
        max_run_created_at,
    )


if __name__ == '__main__':
    main()
