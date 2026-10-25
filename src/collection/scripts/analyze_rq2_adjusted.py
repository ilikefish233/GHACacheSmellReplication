"""Adjusted RQ2 analysis for cache-smell outcome associations.

This entry point is intentionally separate from the historical pooled-run
analysis.  It implements the current experimental protocol:

* only workflow identities represented in the scan are eligible;
* completed run records are deduplicated by ``(run_id, run_attempt)``;
* the snapshot cohort joins current detector labels to all available runs of
  the same workflow identity without requiring workflow-change history;
* an optional default-branch filter can be applied without changing the
  workflow-identity join;
* technically interpretable conclusions are separated from skipped, cancelled
  and approval-gated conclusions;
* durations outside the GitHub workflow-run limit are excluded as invalid;
* every smell is compared with its own detector-eligible control cohort; and
* workflow identities are the descriptive unit in the snapshot-compatible
  path, workflow-version intervals are the unit in the version-aligned path,
  and repositories are the bootstrap cluster; and
* two-sided Wilcoxon rank-sum tests are reported only as unadjusted
  descriptive comparisons of version-level duration medians.

The analysis remains observational.  The snapshot path is intentionally not
revision-aligned: it estimates the association between the currently scanned
workflow identity and its available run outcomes.  A separate version-aligned
pipeline may still be used as a temporal sensitivity analysis.
"""

from __future__ import annotations
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[1]


import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import canonical_smell_name, loads_smell_json


import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import mannwhitneyu


WorkflowKey = Tuple[str, str]

OPERATIONALIZED_SMELL_TYPES = (
    'no_dependency_cache',
    'no_fallback_cache',
    'redundant_cache_update',
    'cache_position_error',
    'conflicting_cache_sources',
    'static_cache_key',
    'overly_specific_cache_key',
    'no_docker_layer_cache',
    'deprecated_cache_action',
    'unreachable_cache_write',
    'cache_as_artifact',
)

SMELL_TYPE_ALIASES = {
    'disposable_cache_key': 'overly_specific_cache_key',
}

TECHNICAL_SUCCESS = {'success'}
TECHNICAL_FAILURES = {'failure', 'startup_failure', 'timed_out'}
CORE_CI_EVENTS = {'push', 'pull_request', 'merge_group'}
MAX_WORKFLOW_DURATION_SECONDS = 35 * 24 * 60 * 60


def normalize_smell_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = SMELL_TYPE_ALIASES.get(canonical_smell_name(value), canonical_smell_name(value))
    return value if value in OPERATIONALIZED_SMELL_TYPES else None


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


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


@dataclass
class ScanProfile:
    """Static detector facts used to define smell-specific eligible cohorts."""

    repository_name: str
    workflow_path: str
    labels: set[str] = field(default_factory=set)
    eligible_smells: set[str] = field(default_factory=set)
    ambiguous_smells: set[str] = field(default_factory=set)
    dependency_languages: set[str] = field(default_factory=set)
    dependency_managers: set[str] = field(default_factory=set)
    job_count: int = 0
    cache_job_count: int = 0
    cache_step_count: int = 0
    manual_cache_step_count: int = 0
    writable_cache_step_count: int = 0
    writer_job_count: int = 0
    setup_cache_job_count: int = 0
    nested_action_count: int = 0
    revision_count: int = 0

    @property
    def key(self) -> WorkflowKey:
        return self.repository_name, self.workflow_path


@dataclass
class WorkflowRecord:
    """One workflow identity or one observed workflow-version interval."""

    repository_name: str
    workflow_path: str
    labels: frozenset[str]
    eligible_smells: frozenset[str]
    ambiguous_smells: frozenset[str]
    dependency_languages: frozenset[str]
    dependency_managers: frozenset[str]
    job_count: int
    cache_job_count: int
    completed_count: int = 0
    success_count: int = 0
    technical_success_count: int = 0
    technical_failure_count: int = 0
    successful_durations: List[float] = field(default_factory=list)
    completed_durations: List[float] = field(default_factory=list)
    event_counts: Dict[str, int] = field(default_factory=dict)
    conclusion_counts: Dict[str, int] = field(default_factory=dict)
    # These fields are populated only by the revision-aligned RQ2 pipeline.
    # Keeping them optional preserves compatibility with the snapshot analysis.
    version_commit_sha: Optional[str] = None
    version_commit_date: Optional[str] = None
    version_next_commit_date: Optional[str] = None
    version_content_hash: Optional[str] = None
    version_content_file: Optional[str] = None

    @property
    def key(self) -> WorkflowKey:
        return self.repository_name, self.workflow_path

    @property
    def all_completed_success_rate(self) -> Optional[float]:
        if self.completed_count <= 0:
            return None
        return self.success_count / self.completed_count

    @property
    def technical_total_count(self) -> int:
        return self.technical_success_count + self.technical_failure_count

    @property
    def technical_success_rate(self) -> Optional[float]:
        if self.technical_total_count <= 0:
            return None
        return self.technical_success_count / self.technical_total_count

    @property
    def successful_duration_median(self) -> Optional[float]:
        if not self.successful_durations:
            return None
        return float(np.median(self.successful_durations))

    @property
    def completed_duration_median(self) -> Optional[float]:
        if not self.completed_durations:
            return None
        return float(np.median(self.completed_durations))


def analysis_unit_name(records: Sequence[WorkflowRecord]) -> str:
    """Describe whether observations are identities or revision intervals."""

    if any(record.version_commit_sha for record in records):
        return 'workflow_version_interval'
    return 'workflow_identity'


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = loads_smell_json(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON at {path}:{line_number}: {exc}') from exc
            if isinstance(value, dict):
                yield value


def _entry_values(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield scan entries from the one-entry-per-line scan representation."""

    if 'repository_name' in record and 'workflow_path' in record:
        yield record
        return
    for value in record.values():
        if isinstance(value, dict):
            yield value


def _workflow_key(entry: Mapping[str, Any]) -> Optional[WorkflowKey]:
    # Repository names are case-insensitive on GitHub.  Normalizing only the
    # repository component prevents losing matches when the API returns a
    # different capitalization; workflow paths remain case-sensitive.
    repository = str(entry.get('repository_name') or '').strip().lower()
    workflow_path = str(entry.get('workflow_path') or '').strip()
    if not repository or not workflow_path:
        return None
    return repository, workflow_path


def _is_writable_cache_step(step: Mapping[str, Any]) -> bool:
    return step.get('can_write', True) is not False and not bool(step.get('read_only', False))


def _merge_profile(profile: ScanProfile, entry: Mapping[str, Any]) -> None:
    profile.revision_count += 1
    detectors = entry.get('detectors') or {}
    cache_detector = detectors.get('cache_smell') or {}
    if not isinstance(cache_detector, dict):
        return

    for smell in cache_detector.get('smells') or []:
        if isinstance(smell, dict):
            normalized = normalize_smell_type(smell.get('type'))
            if normalized:
                profile.labels.add(normalized)

    metadata = cache_detector.get('metadata') or {}
    jobs = metadata.get('jobs') or {}
    if not isinstance(jobs, dict):
        return

    writer_jobs: set[str] = set()
    manual_cache = False
    writable_manual_cache = False
    setup_cache = False
    has_cache = False
    has_dependency = False
    has_nested_action = False
    has_docker_build = False
    has_artifact_transfer_candidate = False

    for job_name, job_data in jobs.items():
        if not isinstance(job_data, dict):
            continue
        profile.job_count = max(profile.job_count, len(jobs))
        analysis = job_data.get('analysis') or {}
        if not isinstance(analysis, dict):
            continue

        has_docker_build = has_docker_build or bool(analysis.get('docker_build_detected'))

        dep_langs = analysis.get('dep_langs') or []
        dep_managers = analysis.get('dep_managers') or []
        profile.dependency_languages.update(str(value) for value in dep_langs if value)
        profile.dependency_managers.update(str(value) for value in dep_managers if value)
        has_dependency = has_dependency or bool(dep_langs)

        nested_actions = analysis.get('nested_actions') or []
        if nested_actions:
            has_nested_action = True
            profile.nested_action_count += len(nested_actions)

        cache_steps = analysis.get('cache_steps') or []
        if not isinstance(cache_steps, list):
            continue
        job_has_cache = False
        job_has_writer = False
        for step in cache_steps:
            if not isinstance(step, dict):
                continue
            has_cache = True
            job_has_cache = True
            profile.cache_step_count += 1
            action = str(step.get('action') or '').lower()
            is_manual = action.startswith('actions/cache')
            is_setup = bool(step.get('setup_cache'))
            has_key = step.get('key') not in (None, '')
            if is_manual and has_key:
                manual_cache = True
                if _is_writable_cache_step(step):
                    writable_manual_cache = True
            if is_setup:
                setup_cache = True
            if _is_writable_cache_step(step):
                job_has_writer = True
                profile.writable_cache_step_count += 1
            path_text = '\n'.join(
                str(value).lower().replace('\\', '/')
                for value in (
                    step.get('path') if isinstance(step.get('path'), list)
                    else [step.get('path')]
                )
                if value not in (None, '')
            )
            if analysis.get('needs') and (
                re.search(
                    r'(?:^|/)(?:dist|release|out|output|coverage|lcov|artifacts?)(?:/|$)'
                    r'|\.(?:tar|tar\.gz|tgz|zip|qcow2|iso|img)$'
                    r'|(?:^|/)(?:target|bin)/',
                    path_text,
                )
            ):
                has_artifact_transfer_candidate = True
        if job_has_cache:
            profile.cache_job_count += 1
        if job_has_writer:
            writer_jobs.add(str(job_name))

    profile.writer_job_count = max(profile.writer_job_count, len(writer_jobs))
    if setup_cache:
        profile.setup_cache_job_count = max(profile.setup_cache_job_count, 1)
    if manual_cache:
        profile.manual_cache_step_count = max(profile.manual_cache_step_count, 1)

    # A nested custom setup action makes NDC status ambiguous.  It is not
    # considered cache evidence and must not become a clean control.
    if has_dependency and not has_nested_action:
        profile.eligible_smells.add('no_dependency_cache')
    elif has_nested_action:
        profile.ambiguous_smells.add('no_dependency_cache')

    if manual_cache:
        profile.eligible_smells.update({'no_fallback_cache', 'cache_position_error'})
    if has_cache:
        profile.eligible_smells.add('cache_position_error')
    if setup_cache and manual_cache:
        profile.eligible_smells.add('conflicting_cache_sources')
    if writable_manual_cache:
        profile.eligible_smells.update({'static_cache_key', 'overly_specific_cache_key'})
    if len(writer_jobs) >= 2:
        profile.eligible_smells.add('redundant_cache_update')

    if has_docker_build:
        profile.eligible_smells.add('no_docker_layer_cache')
    if has_cache:
        profile.eligible_smells.update({
            'deprecated_cache_action',
            'unreachable_cache_write',
        })
    if has_artifact_transfer_candidate:
        profile.eligible_smells.add('cache_as_artifact')

    # A positive detector label always remains observable, even if an older
    # scan entry lacks enough metadata to reconstruct its eligibility rule.
    profile.eligible_smells.update(profile.labels)


def load_scan_profiles(path: Path) -> Dict[WorkflowKey, ScanProfile]:
    profiles: Dict[WorkflowKey, ScanProfile] = {}
    for record in load_jsonl(path):
        for entry in _entry_values(record):
            key = _workflow_key(entry)
            if key is None:
                continue
            profile = profiles.get(key)
            if profile is None:
                profile = ScanProfile(*key)
                profiles[key] = profile
            _merge_profile(profile, entry)
    return profiles


def load_snapshot_times(path: Optional[Path]) -> Dict[WorkflowKey, datetime]:
    """Load the fetch time of each workflow-file snapshot when available."""

    if path is None or not path.exists():
        return {}
    result: Dict[WorkflowKey, datetime] = {}
    for entry in load_jsonl(path):
        key = _workflow_key(entry)
        timestamp = parse_timestamp(entry.get('fetched_at'))
        if key is None or timestamp is None:
            continue
        if key not in result or timestamp > result[key]:
            result[key] = timestamp
    return result


def load_default_branches(path: Optional[Path]) -> Tuple[Dict[str, str], int]:
    """Load default branches for repository aliases used by the run data."""

    if path is None or not path.exists():
        return {}, 0
    branches: Dict[str, str] = {}
    conflicts = 0
    for entry in load_jsonl(path):
        repository = entry.get('repo') or {}
        default_branch = repository.get('defaultBranch')
        if not isinstance(default_branch, str) or not default_branch:
            continue
        for alias in (entry.get('_id'), repository.get('name')):
            normalized = str(alias or '').strip().lower()
            if not normalized:
                continue
            previous = branches.get(normalized)
            if previous is not None and previous != default_branch:
                conflicts += 1
                continue
            branches[normalized] = default_branch
    return branches, conflicts


def _new_workflow_record(profile: ScanProfile) -> WorkflowRecord:
    return WorkflowRecord(
        repository_name=profile.repository_name,
        workflow_path=profile.workflow_path,
        labels=frozenset(profile.labels),
        eligible_smells=frozenset(profile.eligible_smells),
        ambiguous_smells=frozenset(profile.ambiguous_smells),
        dependency_languages=frozenset(profile.dependency_languages),
        dependency_managers=frozenset(profile.dependency_managers),
        job_count=profile.job_count,
        cache_job_count=profile.cache_job_count,
    )


def load_records(
    scan_path: Path,
    runs_path: Path,
    workflow_files_path: Optional[Path],
    snapshot_window_days: Optional[float],
    event_scope: str = 'all',
    default_branch_only: bool = False,
    default_branches_path: Optional[Path] = None,
    additional_runs_paths: Optional[Sequence[Path]] = None,
    min_run_created_at: Optional[datetime] = None,
    max_run_created_at: Optional[datetime] = None,
) -> Tuple[List[WorkflowRecord], Dict[str, Any]]:
    profiles = load_scan_profiles(scan_path)
    snapshot_times = load_snapshot_times(workflow_files_path)
    default_branches, default_branch_conflicts = load_default_branches(
        default_branches_path if default_branch_only else None
    )
    runs_sources = [runs_path, *(additional_runs_paths or [])]
    for source in runs_sources:
        if not source.exists():
            raise FileNotFoundError(f'Workflow runs file not found: {source}')
    if default_branch_only and not default_branches:
        raise ValueError(
            'Default-branch filtering was requested, but no repository default-branch mapping was loaded'
        )
    records: Dict[WorkflowKey, WorkflowRecord] = {}
    seen_run_attempts: Dict[Tuple[str, str], WorkflowKey] = {}
    metadata: Dict[str, Any] = {
        'scan_workflow_identities': len(profiles),
        'scan_repositories': len({key[0] for key in profiles}),
        'snapshot_timestamps': len(snapshot_times),
        'snapshot_window_days': snapshot_window_days,
        'event_scope': event_scope,
        'runs_sources': [str(source) for source in runs_sources],
        'default_branch_only': default_branch_only,
        'default_branch_aliases': len(default_branches),
        'default_branch_conflicts': default_branch_conflicts,
        'raw_run_records': 0,
        'completed_run_records_total': 0,
        'completed_run_records_outside_scan': 0,
        'completed_run_records_outside_snapshot_window': 0,
        'completed_run_records_missing_snapshot_time': 0,
        'completed_run_records_missing_created_at': 0,
        'deduplicated_run_records': 0,
        'duplicate_run_ids_across_repository_names': 0,
        'completed_run_records_missing_default_branch_mapping': 0,
        'completed_run_records_missing_head_branch': 0,
        'completed_run_records_non_default_branch': 0,
        'completed_run_records_before_created_cutoff': 0,
        'completed_run_records_after_created_cutoff': 0,
        'run_records_in_analysis': 0,
        'workflow_identities_with_runs': 0,
        'repositories_with_runs': 0,
        'invalid_duration_missing_or_nonpositive': 0,
        'invalid_duration_over_max_seconds': 0,
        'valid_duration_completed_runs': 0,
        'valid_duration_success_runs': 0,
        'excluded_non_core_ci_events': 0,
        'conclusion_counts': Counter(),
    }
    metadata['min_run_created_at'] = (
        min_run_created_at.isoformat().replace('+00:00', 'Z')
        if min_run_created_at is not None else None
    )
    metadata['max_run_created_at'] = (
        max_run_created_at.isoformat().replace('+00:00', 'Z')
        if max_run_created_at is not None else None
    )

    for source in runs_sources:
        with source.open('r', encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():
                    continue
                metadata['raw_run_records'] += 1
                try:
                    entry = loads_smell_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get('status') != 'completed':
                    continue
                metadata['completed_run_records_total'] += 1
                created_at = parse_timestamp(entry.get('created_at'))
                if min_run_created_at is not None:
                    if created_at is None:
                        metadata['completed_run_records_missing_created_at'] += 1
                        continue
                    if created_at < min_run_created_at:
                        metadata['completed_run_records_before_created_cutoff'] += 1
                        continue
                if max_run_created_at is not None:
                    if created_at is None:
                        metadata['completed_run_records_missing_created_at'] += 1
                        continue
                    if created_at > max_run_created_at:
                        metadata['completed_run_records_after_created_cutoff'] += 1
                        continue
                key = _workflow_key(entry)
                if key is None or key not in profiles:
                    metadata['completed_run_records_outside_scan'] += 1
                    continue

                if default_branch_only:
                    default_branch = default_branches.get(key[0])
                    head_branch = entry.get('head_branch')
                    if default_branch is None:
                        metadata['completed_run_records_missing_default_branch_mapping'] += 1
                        continue
                    if not isinstance(head_branch, str) or not head_branch:
                        metadata['completed_run_records_missing_head_branch'] += 1
                        continue
                    if head_branch != default_branch:
                        metadata['completed_run_records_non_default_branch'] += 1
                        continue

                event = str(entry.get('event') or '').lower()
                if event_scope == 'core_ci' and event not in CORE_CI_EVENTS:
                    metadata['excluded_non_core_ci_events'] += 1
                    continue

                if snapshot_window_days is not None:
                    snapshot_time = snapshot_times.get(key)
                    if snapshot_time is None:
                        metadata['completed_run_records_missing_snapshot_time'] += 1
                        continue
                    if created_at is None:
                        metadata['completed_run_records_missing_created_at'] += 1
                        continue
                    if abs((created_at - snapshot_time).total_seconds()) > snapshot_window_days * 86400:
                        metadata['completed_run_records_outside_snapshot_window'] += 1
                        continue

                run_id = entry.get('run_id')
                attempt = entry.get('run_attempt')
                if run_id not in (None, ''):
                    dedup_key = (str(run_id), str(attempt or 1))
                    previous_key = seen_run_attempts.get(dedup_key)
                    if previous_key is not None:
                        metadata['deduplicated_run_records'] += 1
                        if previous_key[0] != key[0]:
                            metadata['duplicate_run_ids_across_repository_names'] += 1
                        continue
                    seen_run_attempts[dedup_key] = key

                record = records.get(key)
                if record is None:
                    record = _new_workflow_record(profiles[key])
                    records[key] = record
                metadata['run_records_in_analysis'] += 1

                conclusion = str(entry.get('conclusion') or '').lower()
                metadata['conclusion_counts'][conclusion] += 1
                record.completed_count += 1
                record.conclusion_counts[conclusion] = record.conclusion_counts.get(conclusion, 0) + 1
                record.event_counts[event] = record.event_counts.get(event, 0) + 1

                if conclusion in TECHNICAL_SUCCESS:
                    record.success_count += 1
                    record.technical_success_count += 1
                elif conclusion in TECHNICAL_FAILURES:
                    record.technical_failure_count += 1

                duration = as_number(entry.get('duration_seconds'))
                if duration is None or duration <= 0:
                    metadata['invalid_duration_missing_or_nonpositive'] += 1
                elif duration > MAX_WORKFLOW_DURATION_SECONDS:
                    metadata['invalid_duration_over_max_seconds'] += 1
                else:
                    metadata['valid_duration_completed_runs'] += 1
                    record.completed_durations.append(duration)
                    if conclusion in TECHNICAL_SUCCESS:
                        metadata['valid_duration_success_runs'] += 1
                        record.successful_durations.append(duration)

    metadata['conclusion_counts'] = dict(metadata['conclusion_counts'])
    metadata['workflow_identities_with_runs'] = len(records)
    metadata['repositories_with_runs'] = len({record.repository_name for record in records.values()})
    if snapshot_window_days is None:
        metadata['revision_alignment'] = (
            'current workflow identity snapshot joined to all available completed runs by '
            '(repository_name, workflow_path); workflow_changes.jsonl is not required, and '
            'historical runs are not assigned to historical YAML revisions'
        )
    else:
        metadata['revision_alignment'] = (
            'workflow identity snapshot with bounded created_at-to-fetched_at window; '
            'exact head_sha-to-workflow-revision alignment is unavailable because workflow_files.jsonl '
            'does not contain the workflow commit SHA'
        )
    metadata['duration_definition'] = (
        'duration_seconds > 0 and <= 35 days; primary duration is the median of '
        'per-workflow successful-run medians'
    )
    metadata['technical_reliability_definition'] = (
        'success / (success + failure + startup_failure + timed_out); '
        'skipped, cancelled, action_required and missing conclusions are excluded'
    )
    return list(records.values()), metadata


def _summary(records: Sequence[WorkflowRecord]) -> Dict[str, Any]:
    success_rates = [r.all_completed_success_rate for r in records if r.all_completed_success_rate is not None]
    technical_rates = [r.technical_success_rate for r in records if r.technical_success_rate is not None]
    success_medians = [r.successful_duration_median for r in records if r.successful_duration_median is not None]
    completed_medians = [r.completed_duration_median for r in records if r.completed_duration_median is not None]
    conclusion_counts: Counter[str] = Counter()
    for record in records:
        conclusion_counts.update(record.conclusion_counts)
    return {
        'workflow_count': len(records),
        'repository_count': len({r.repository_name for r in records}),
        'completed_run_count': sum(r.completed_count for r in records),
        'success_run_count': sum(r.success_count for r in records),
        'technical_success_run_count': sum(r.technical_success_count for r in records),
        'technical_failure_run_count': sum(r.technical_failure_count for r in records),
        'technical_run_count': sum(r.technical_total_count for r in records),
        'pooled_all_completed_success_rate': (
            sum(r.success_count for r in records) / sum(r.completed_count for r in records)
            if sum(r.completed_count for r in records) else None
        ),
        'pooled_technical_success_rate': (
            sum(r.technical_success_count for r in records) / sum(r.technical_total_count for r in records)
            if sum(r.technical_total_count for r in records) else None
        ),
        'mean_workflow_all_completed_success_rate': float(np.mean(success_rates)) if success_rates else None,
        'mean_workflow_technical_success_rate': float(np.mean(technical_rates)) if technical_rates else None,
        'median_workflow_success_duration_sec': float(np.median(success_medians)) if success_medians else None,
        'median_workflow_completed_duration_sec': float(np.median(completed_medians)) if completed_medians else None,
        'successful_duration_workflow_count': len(success_medians),
        'completed_duration_workflow_count': len(completed_medians),
        'conclusion_counts': dict(conclusion_counts),
    }


def _cohort(records: Sequence[WorkflowRecord], smell_type: str) -> Tuple[List[WorkflowRecord], List[WorkflowRecord], int]:
    exposed: List[WorkflowRecord] = []
    controls: List[WorkflowRecord] = []
    ambiguous = 0
    for record in records:
        if smell_type in record.labels:
            exposed.append(record)
        elif smell_type in record.ambiguous_smells:
            ambiguous += 1
        elif smell_type in record.eligible_smells:
            controls.append(record)
    return exposed, controls, ambiguous


def _repo_payload(records: Sequence[WorkflowRecord], value_name: str) -> Dict[str, List[float]]:
    result: Dict[str, List[float]] = defaultdict(list)
    for record in records:
        value = getattr(record, value_name)
        if value is not None:
            result[record.repository_name].append(float(value))
    return dict(result)


def _bootstrap_pair(
    exposed: Sequence[WorkflowRecord],
    controls: Sequence[WorkflowRecord],
    iterations: int,
    seed: int,
) -> Dict[str, List[float]]:
    repositories = sorted({r.repository_name for r in exposed} | {r.repository_name for r in controls})
    rng = np.random.default_rng(seed)
    payload = {
        'exposed_rate': _repo_payload(exposed, 'technical_success_rate'),
        'control_rate': _repo_payload(controls, 'technical_success_rate'),
        'exposed_all_rate': _repo_payload(exposed, 'all_completed_success_rate'),
        'control_all_rate': _repo_payload(controls, 'all_completed_success_rate'),
        'exposed_duration': _repo_payload(exposed, 'successful_duration_median'),
        'control_duration': _repo_payload(controls, 'successful_duration_median'),
        'exposed_completed_duration': _repo_payload(exposed, 'completed_duration_median'),
        'control_completed_duration': _repo_payload(controls, 'completed_duration_median'),
    }
    output = {
        'technical_rate_difference': [],
        'all_completed_rate_difference': [],
        'successful_duration_ratio': [],
        'completed_duration_ratio': [],
    }
    if not repositories or iterations <= 0:
        return output

    for _ in range(iterations):
        sampled_indices = rng.integers(0, len(repositories), size=len(repositories))
        multiplicities = Counter(repositories[index] for index in sampled_indices)

        def aggregate_rate(name: str) -> Optional[float]:
            values: List[float] = []
            for repository, multiplicity in multiplicities.items():
                values.extend(payload[name].get(repository, []) * multiplicity)
            return float(np.mean(values)) if values else None

        def aggregate_median(name: str) -> Optional[float]:
            values: List[float] = []
            for repository, multiplicity in multiplicities.items():
                values.extend(payload[name].get(repository, []) * multiplicity)
            return float(np.median(values)) if values else None

        exposed_rate = aggregate_rate('exposed_rate')
        control_rate = aggregate_rate('control_rate')
        if exposed_rate is not None and control_rate is not None:
            output['technical_rate_difference'].append(exposed_rate - control_rate)

        exposed_all = aggregate_rate('exposed_all_rate')
        control_all = aggregate_rate('control_all_rate')
        if exposed_all is not None and control_all is not None:
            output['all_completed_rate_difference'].append(exposed_all - control_all)

        exposed_duration = aggregate_median('exposed_duration')
        control_duration = aggregate_median('control_duration')
        if exposed_duration is not None and control_duration and control_duration > 0:
            output['successful_duration_ratio'].append(exposed_duration / control_duration)

        exposed_completed = aggregate_median('exposed_completed_duration')
        control_completed = aggregate_median('control_completed_duration')
        if exposed_completed is not None and control_completed and control_completed > 0:
            output['completed_duration_ratio'].append(exposed_completed / control_completed)
    return output


def _percentile_ci(values: Sequence[float]) -> Optional[List[float]]:
    if not values:
        return None
    return [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]


def _cliffs_delta(exposed: Sequence[float], controls: Sequence[float]) -> Optional[float]:
    if not exposed or not controls:
        return None
    x = np.asarray(exposed, dtype=float)
    y = np.sort(np.asarray(controls, dtype=float))
    less = np.searchsorted(y, x, side='left').sum()
    greater = (len(y) - np.searchsorted(y, x, side='right')).sum()
    return float((less - greater) / (len(x) * len(y)))


def _wilcoxon_rank_sum(
    exposed: Sequence[float],
    controls: Sequence[float],
) -> Dict[str, Optional[float]]:
    """Run a two-sided rank-sum test on independent version-level values.

    SciPy's Mann--Whitney U statistic is the standard computational form of
    the Wilcoxon rank-sum test for two independent samples.  The test is kept
    descriptive: it does not replace the adjusted regression or account for
    repository clustering.  Repository-clustered bootstrap intervals remain
    the uncertainty measure for the adjusted analysis.
    """

    if len(exposed) < 2 or len(controls) < 2:
        return {'u_statistic': None, 'p_value': None}
    try:
        statistic, p_value = mannwhitneyu(
            np.asarray(exposed, dtype=float),
            np.asarray(controls, dtype=float),
            alternative='two-sided',
        )
    except (TypeError, ValueError):
        return {'u_statistic': None, 'p_value': None}
    return {'u_statistic': float(statistic), 'p_value': float(p_value)}


def _pair_result(
    smell_type: str,
    exposed: Sequence[WorkflowRecord],
    controls: Sequence[WorkflowRecord],
    ambiguous_control_count: int,
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    exposed_summary = _summary(exposed)
    control_summary = _summary(controls)
    boot = _bootstrap_pair(exposed, controls, bootstrap_iterations, seed)

    exposed_tech = exposed_summary['mean_workflow_technical_success_rate']
    control_tech = control_summary['mean_workflow_technical_success_rate']
    exposed_all = exposed_summary['mean_workflow_all_completed_success_rate']
    control_all = control_summary['mean_workflow_all_completed_success_rate']
    exposed_duration = exposed_summary['median_workflow_success_duration_sec']
    control_duration = control_summary['median_workflow_success_duration_sec']
    exposed_completed_duration = exposed_summary['median_workflow_completed_duration_sec']
    control_completed_duration = control_summary['median_workflow_completed_duration_sec']

    tech_difference = exposed_tech - control_tech if exposed_tech is not None and control_tech is not None else None
    all_difference = exposed_all - control_all if exposed_all is not None and control_all is not None else None
    duration_ratio = exposed_duration / control_duration if exposed_duration and control_duration else None
    completed_duration_ratio = (
        exposed_completed_duration / control_completed_duration
        if exposed_completed_duration and control_completed_duration else None
    )
    reliability_ratio = control_tech / exposed_tech if exposed_tech and control_tech else None

    duration_values = [r.successful_duration_median for r in exposed if r.successful_duration_median is not None]
    control_duration_values = [r.successful_duration_median for r in controls if r.successful_duration_median is not None]
    completed_values = [r.completed_duration_median for r in exposed if r.completed_duration_median is not None]
    control_completed_values = [r.completed_duration_median for r in controls if r.completed_duration_median is not None]
    successful_rank_sum = _wilcoxon_rank_sum(duration_values, control_duration_values)
    completed_rank_sum = _wilcoxon_rank_sum(completed_values, control_completed_values)
    dcbi = duration_ratio * reliability_ratio if duration_ratio is not None and reliability_ratio is not None else None

    dcbi_bootstrap = []
    for rate_diff, duration_boot in zip(
        boot['technical_rate_difference'], boot['successful_duration_ratio']
    ):
        # The repository bootstrap output does not retain the two rates for
        # each replicate, so DCBI is intentionally not inferred from a rate
        # difference.  The point estimate remains descriptive only.
        _ = rate_diff, duration_boot

    return {
        'smell_type': smell_type,
        'analysis_unit': analysis_unit_name([*exposed, *controls]),
        'cluster_unit': 'repository',
        'control_definition': 'detector-eligible workflows without this smell; ambiguous controls excluded',
        'exposed': exposed_summary,
        'eligible_control': control_summary,
        'excluded_ambiguous_controls': ambiguous_control_count,
        'technical_reliability': {
            'exposed_mean_workflow_rate': exposed_tech,
            'control_mean_workflow_rate': control_tech,
            'difference': tech_difference,
            'difference_percentage_points': tech_difference * 100 if tech_difference is not None else None,
            'cluster_bootstrap_ci_95_difference': _percentile_ci(boot['technical_rate_difference']),
            'pooled_exposed_rate': exposed_summary['pooled_technical_success_rate'],
            'pooled_control_rate': control_summary['pooled_technical_success_rate'],
        },
        'all_completed_reliability_sensitivity': {
            'exposed_mean_workflow_rate': exposed_all,
            'control_mean_workflow_rate': control_all,
            'difference': all_difference,
            'difference_percentage_points': all_difference * 100 if all_difference is not None else None,
            'cluster_bootstrap_ci_95_difference': _percentile_ci(boot['all_completed_rate_difference']),
        },
        'successful_duration': {
            'scope': 'median of per-workflow medians among technically successful runs',
            'exposed_median_sec': exposed_duration,
            'control_median_sec': control_duration,
            'ratio': duration_ratio,
            'difference_sec': exposed_duration - control_duration if exposed_duration is not None and control_duration is not None else None,
            'cluster_bootstrap_ci_95_ratio': _percentile_ci(boot['successful_duration_ratio']),
            'cliffs_delta': _cliffs_delta(duration_values, control_duration_values),
            'wilcoxon_rank_sum_u_statistic': successful_rank_sum['u_statistic'],
            'wilcoxon_rank_sum_p_value': successful_rank_sum['p_value'],
        },
        'all_completed_duration_sensitivity': {
            'scope': 'median of per-workflow medians among all completed runs with valid duration',
            'exposed_median_sec': exposed_completed_duration,
            'control_median_sec': control_completed_duration,
            'ratio': completed_duration_ratio,
            'difference_sec': exposed_completed_duration - control_completed_duration if exposed_completed_duration is not None and control_completed_duration is not None else None,
            'cluster_bootstrap_ci_95_ratio': _percentile_ci(boot['completed_duration_ratio']),
            'cliffs_delta': _cliffs_delta(completed_values, control_completed_values),
            'wilcoxon_rank_sum_u_statistic': completed_rank_sum['u_statistic'],
            'wilcoxon_rank_sum_p_value': completed_rank_sum['p_value'],
        },
        'descriptive_cache_burden_index': {
            'definition': 'successful-duration ratio × inverse technical reliability ratio',
            'value': dcbi,
            'interpretation': 'descriptive; not an adjusted causal effect',
            'bootstrap_ci': None,
        },
        'bootstrap_valid_replicates': {name: len(values) for name, values in boot.items()},
    }


def build_report(
    records: Sequence[WorkflowRecord],
    load_metadata: Mapping[str, Any],
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    global_no_smell = [r for r in records if not r.labels]
    cohorts: Dict[str, Any] = {}
    cohort_counts: Dict[str, Any] = {}
    for index, smell_type in enumerate(OPERATIONALIZED_SMELL_TYPES):
        exposed, controls, ambiguous = _cohort(records, smell_type)
        cohorts[smell_type] = _pair_result(
            smell_type,
            exposed,
            controls,
            ambiguous,
            bootstrap_iterations,
            seed + index,
        )
        cohort_counts[smell_type] = {
            'exposed_workflows': len(exposed),
            'eligible_control_workflows': len(controls),
            'excluded_ambiguous_controls': ambiguous,
            'exposed_repositories': len({r.repository_name for r in exposed}),
            'control_repositories': len({r.repository_name for r in controls}),
        }

    return {
        'metadata': {
            **dict(load_metadata),
            'analysis_unit': analysis_unit_name(records),
            'operationalized_smell_types': list(OPERATIONALIZED_SMELL_TYPES),
            'baseline_group': 'smell-specific detector-eligible control cohort',
            'global_no_smell_is_not_used_as_primary_control': True,
            'bootstrap_iterations': bootstrap_iterations,
            'bootstrap_seed': seed,
            'p_values': (
                'Wilcoxon rank-sum p-values are reported only for unadjusted '
                'descriptive duration comparisons; regression p-values are not '
                'computed, and repository-clustered bootstrap intervals remain '
                'the primary uncertainty measure'
            ),
        },
        'global_no_smell_descriptive_only': _summary(global_no_smell),
        'cohort_counts': cohort_counts,
        'comparisons': cohorts,
    }


def report_rows(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for smell_type, comparison in (report.get('comparisons') or {}).items():
        reliability = comparison.get('technical_reliability') or {}
        duration = comparison.get('successful_duration') or {}
        sensitivity = comparison.get('all_completed_reliability_sensitivity') or {}
        row = {
            'smell_type': smell_type,
            'exposed_workflows': comparison.get('exposed', {}).get('workflow_count'),
            'control_workflows': comparison.get('eligible_control', {}).get('workflow_count'),
            'exposed_repositories': comparison.get('exposed', {}).get('repository_count'),
            'control_repositories': comparison.get('eligible_control', {}).get('repository_count'),
            'technical_success_rate_exposed': reliability.get('exposed_mean_workflow_rate'),
            'technical_success_rate_control': reliability.get('control_mean_workflow_rate'),
            'technical_success_difference_pp': reliability.get('difference_percentage_points'),
            'technical_success_ci_low_pp': (
                reliability.get('cluster_bootstrap_ci_95_difference') or [None, None]
            )[0] * 100 if reliability.get('cluster_bootstrap_ci_95_difference') else None,
            'technical_success_ci_high_pp': (
                reliability.get('cluster_bootstrap_ci_95_difference') or [None, None]
            )[1] * 100 if reliability.get('cluster_bootstrap_ci_95_difference') else None,
            'successful_duration_exposed_sec': duration.get('exposed_median_sec'),
            'successful_duration_control_sec': duration.get('control_median_sec'),
            'successful_duration_ratio': duration.get('ratio'),
            'successful_duration_ci_low': (duration.get('cluster_bootstrap_ci_95_ratio') or [None, None])[0],
            'successful_duration_ci_high': (duration.get('cluster_bootstrap_ci_95_ratio') or [None, None])[1],
            'successful_duration_cliffs_delta': duration.get('cliffs_delta'),
            'successful_duration_wilcoxon_u': duration.get('wilcoxon_rank_sum_u_statistic'),
            'successful_duration_wilcoxon_p_value': duration.get('wilcoxon_rank_sum_p_value'),
            'all_completed_success_difference_pp': sensitivity.get('difference_percentage_points'),
            'dcbi_descriptive': (comparison.get('descriptive_cache_burden_index') or {}).get('value'),
        }
        rows.append(row)
    return rows


def save_report(report: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / 'rq2_adjusted_results.json'
    csv_path = output_dir / 'rq2_adjusted_results.csv'
    flow_path = output_dir / 'rq2_cohort_flow.json'
    with json_path.open('w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    rows = report_rows(report)
    with csv_path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ['smell_type'])
        writer.writeheader()
        writer.writerows(rows)
    flow = {
        'metadata': report.get('metadata', {}),
        'cohort_counts': report.get('cohort_counts', {}),
        'global_no_smell_descriptive_only': report.get('global_no_smell_descriptive_only', {}),
    }
    with flow_path.open('w', encoding='utf-8') as handle:
        json.dump(flow, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(f'JSON: {json_path}')
    print(f'CSV:  {csv_path}')
    print(f'Flow: {flow_path}')


def main() -> None:
    parser = argparse.ArgumentParser(description='Run adjusted RQ2 cache-smell outcome analysis')
    parser.add_argument('--data-dir', type=Path, default=Path(str(_PORTABLE_PROJECT_ROOT / 'data_26_6_9')))
    parser.add_argument('--smell-scan', type=Path,
                        default=Path(str(_PORTABLE_PROJECT_ROOT / 'output_full_logical_20260831/smell_scan_results.jsonl')))
    parser.add_argument('--runs-path', type=Path, default=None,
                        help='primary workflow-runs JSONL; defaults to <data-dir>/workflow_runs.jsonl')
    parser.add_argument('--additional-runs-path', type=Path, action='append', default=[],
                        help='additional workflow-runs JSONL files to merge before deduplication')
    parser.add_argument('--workflow-files', type=Path, default=None,
                        help='workflow_files.jsonl; used for bounded snapshot alignment')
    parser.add_argument('--output-dir', type=Path,
                        default=Path(str(_PORTABLE_PROJECT_ROOT / 'output_full_logical_20260831/rq2_adjusted')))
    parser.add_argument('--snapshot-window-days', type=float, default=30.0,
                        help='symmetric window around workflow-file fetched_at; use -1 for all time')
    parser.add_argument('--min-run-created-at', type=str, default=None,
                        help='optional inclusive UTC lower bound for the run study window')
    parser.add_argument('--max-run-created-at', type=str, default=None,
                        help='optional inclusive UTC upper bound for the run study window')
    parser.add_argument('--event-scope', choices=['all', 'core_ci'], default='all')
    parser.add_argument('--default-branch-only', action='store_true',
                        help='retain only completed runs whose head_branch equals the repository default branch')
    parser.add_argument('--bootstrap', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    workflow_files = args.workflow_files
    if workflow_files is None:
        candidate = args.data_dir / 'workflow_files.jsonl'
        workflow_files = candidate if candidate.exists() else None
    window = None if args.snapshot_window_days < 0 else args.snapshot_window_days
    runs_path = args.runs_path or (args.data_dir / 'workflow_runs.jsonl')
    default_branches_path = args.data_dir / 'repo_info.jsonl' if args.default_branch_only else None
    min_run_created_at = parse_timestamp(args.min_run_created_at) if args.min_run_created_at else None
    max_run_created_at = parse_timestamp(args.max_run_created_at) if args.max_run_created_at else None
    if args.min_run_created_at and min_run_created_at is None:
        raise SystemExit('--min-run-created-at must be an ISO-8601 timestamp')
    if args.max_run_created_at and max_run_created_at is None:
        raise SystemExit('--max-run-created-at must be an ISO-8601 timestamp')
    if min_run_created_at is not None and max_run_created_at is not None:
        if min_run_created_at > max_run_created_at:
            raise SystemExit('--min-run-created-at must not be later than --max-run-created-at')

    print('Adjusted RQ2 analysis')
    print(f'Scan: {args.smell_scan}')
    print(f'Runs: {runs_path}')
    if args.additional_runs_path:
        print(f'Additional runs: {args.additional_runs_path}')
    print(
        f'Snapshot window: {window} days; event scope: {args.event_scope}; '
        f'default branch only: {args.default_branch_only}; '
        f'run window: {min_run_created_at} to {max_run_created_at}'
    )
    records, metadata = load_records(
        args.smell_scan,
        runs_path,
        workflow_files,
        window,
        args.event_scope,
        default_branch_only=args.default_branch_only,
        default_branches_path=default_branches_path,
        additional_runs_paths=args.additional_runs_path,
        min_run_created_at=min_run_created_at,
        max_run_created_at=max_run_created_at,
    )
    report = build_report(records, metadata, args.bootstrap, args.seed)
    save_report(report, args.output_dir)
    print(f"Workflow identities in analysis: {metadata['workflow_identities_with_runs']}")
    print(f"Run records in analysis: {metadata['run_records_in_analysis']}")
    print(f"Deduplicated records: {metadata['deduplicated_run_records']}")
    print(f"Invalid durations > 35 days: {metadata['invalid_duration_over_max_seconds']}")
    print('Cohorts:')
    for smell_type, counts in report['cohort_counts'].items():
        print(f"  {smell_type}: exposed={counts['exposed_workflows']}, control={counts['eligible_control_workflows']}")


if __name__ == '__main__':
    main()
