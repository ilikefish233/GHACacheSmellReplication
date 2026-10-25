"""Run-level, latest-workflow-version outcome analysis for RQ2.

The primary exposure is the workflow YAML that is current on the default branch
at the fixed 2026-06-09 cutoff. Each completed run is assigned to that
exposure when it has the same repository, workflow path, and workflow id, runs
on the default branch, and falls within the effective interval that starts at
the latest workflow-file content change and ends at the study cutoff. The
primary cohort may additionally use a common final 30-day window.

This interval-based alignment does not require fetching the YAML at every run's
``head_sha``. Ordinary repository commits therefore do not split an unchanged
workflow version, while runs before a later workflow-file change do not inherit
the current smell label. Exact head-SHA snapshot matching is retained as a
separate, stricter sensitivity analysis.

The primary outcome is the duration of all completed runs with a valid
positive duration; technical success is a separate secondary outcome.  The
reliability model is a grouped-binomial logistic regression with one trial per
run. Formal regression inference uses repository-clustered sandwich standard
errors, while repository-clustered bootstrap percentile intervals provide a
distribution-free sensitivity analysis. The primary analysis gives every
observed workflow version total weight one within each endpoint's usable cohort,
preventing highly triggered versions from dominating the result.  Run-weighted
and repository-balanced analyses are reported as sensitivities.  Duration
is modelled on the log scale for (1) all completed runs (primary) and (2)
technically successful runs (selection-sensitive sensitivity analysis).  A
within-workflow paired sensitivity analysis is also reported for identities
that contain both exposed and eligible control versions.  Version-balanced
and repository-balanced analyses are available as prespecified sensitivity
analyses.  These are adjusted observational associations, not causal effects.
"""

from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import loads_smell_json


import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import rankdata

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.analyze_rq2_adjusted import (  # noqa: E402
    OPERATIONALIZED_SMELL_TYPES,
    ScanProfile,
    _cliffs_delta,
    _merge_profile,
    _wilcoxon_rank_sum,
    normalize_smell_type,
)
from scripts.analyze_rq2_regression import (  # noqa: E402
    _penalty_matrix,
    _prediction_difference,
    _safe_exp,
    _sigmoid,
    fit_firth_logistic,
    fit_grouped_binomial_logistic,
    fit_ridge_log_duration,
)


DEFAULT_INPUT_DIR = (
    REPOSITORY_ROOT / 'output_full_logical_20260915_complete'
    / 'rq2_latest_version_interval_20260609_strict_20260915'
)
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / 'output_full_logical_20260915_complete'
    / 'rq2_latest_version_interval_20260609_strict_20260915'
    / 'rq2_version_balanced_enhanced_b2000'
)
TECHNICAL_SUCCESS = {'success'}
TECHNICAL_FAILURES = {'failure', 'startup_failure', 'timed_out'}
RELIABILITY_OUTCOMES = {'reliability', 'all_completed_reliability'}
KNOWN_EVENTS = (
    'push',
    'pull_request',
    'merge_group',
    'schedule',
    'workflow_dispatch',
    'workflow_run',
    'release',
)
MAX_WORKFLOW_DURATION_SECONDS = 35 * 24 * 60 * 60
DEFAULT_BOOTSTRAP_ITERATIONS = 2000
WEIGHTING_MODES = ('version', 'run', 'repository')
DEFAULT_WEIGHTING_MODE = 'version'
EPSILON = 1e-12
RIDGE_PENALTY = 1e-6
LOGISTIC_MAX_ITERATIONS = 100
LOGISTIC_TOLERANCE = 1e-8

# These thresholds are fixed before interpreting the results.  A smell below
# either threshold remains in the dataset and receives descriptive/model-based
# estimates, but it is not promoted to a confirmatory significance claim.
MIN_EXPOSED_VERSIONS = 30
MIN_EXPOSED_REPOSITORIES = 10
RARE_SMELL_POLICY = (
    'A smell is confirmatory only when it is exposed in at least '
    f'{MIN_EXPOSED_VERSIONS} workflow versions and {MIN_EXPOSED_REPOSITORIES} '
    'repositories; otherwise its regression result is exploratory/descriptive.'
)

COHORT_ELIGIBILITY_RULES = {
    'no_dependency_cache': (
        'A high-confidence supported dependency-installation pattern is present; '
        'opaque nested setup actions make the version ambiguous rather than an '
        'eligible control.'
    ),
    'no_fallback_cache': (
        'At least one manual actions/cache or actions/cache/restore step has a key.'
    ),
    'redundant_cache_update': (
        'At least two jobs are statically identified as writable cache writers.'
    ),
    'cache_position_error': (
        'At least one cache step is present and its position can be evaluated.'
    ),
    'conflicting_cache_sources': (
        'A setup-action cache and a manual cache source are both present.'
    ),
    'static_cache_key': (
        'At least one writable manual cache step with a statically analyzable key '
        'is present.'
    ),
    'overly_specific_cache_key': (
        'At least one writable manual cache step with a statically analyzable key '
        'is present.'
    ),
    'no_docker_layer_cache': (
        'A Docker image build is statically identified and no effective layer-cache '
        'configuration is visible.'
    ),
    'deprecated_cache_action': (
        'At least one cache action uses a version explicitly classified as deprecated.'
    ),
    'unreachable_cache_write': (
        'At least one cache write is statically identified, and its execution path or '
        'referenced restore output is evaluable.'
    ),
    'cache_as_artifact': (
        'A cache path or key is strongly artifact-like and the producer/consumer '
        'relationship is statically visible.'
    ),
}


@dataclass(slots=True)
class RunObservation:
    repository_name: str
    workflow_path: str
    version_id: str
    version_commit_sha: str
    version_commit_date: datetime
    labels: frozenset[str]
    eligible_smells: frozenset[str]
    ambiguous_smells: frozenset[str]
    dependency_languages: frozenset[str]
    dependency_managers: frozenset[str]
    job_count: int
    cache_job_count: int
    event: str
    calendar_year: str
    version_age_days: float
    run_order_in_version: int
    is_first_run_in_version: bool
    cache_key_dynamic: bool
    conclusion: str
    duration_seconds: Optional[float]
    calendar_month: str = ''
    run_id: Any = None
    run_attempt: Any = 1
    version_run_count: int = 1
    version_weight: float = 1.0
    version_age_is_proxy: bool = False

    @property
    def technical_success(self) -> bool:
        return self.conclusion in TECHNICAL_SUCCESS

    @property
    def technical_failure(self) -> bool:
        return self.conclusion in TECHNICAL_FAILURES

    @property
    def technical_eligible(self) -> bool:
        return self.technical_success or self.technical_failure

    @property
    def valid_duration(self) -> bool:
        return (
            self.duration_seconds is not None
            and 0 < self.duration_seconds <= MAX_WORKFLOW_DURATION_SECONDS
        )

    @property
    def workflow_identity(self) -> Tuple[str, str]:
        return self.repository_name, self.workflow_path


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


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


def as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _cache_key_is_dynamic(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.lower()
    dynamic_tokens = (
        '${{',
        'hashfiles',
        'github.sha',
        'github.run_id',
        'github.run_number',
        'github.ref',
        'runner.os',
        'runner.arch',
    )
    return any(token in text for token in dynamic_tokens)


def _scan_entry_profile(scan: Mapping[str, Any]) -> Tuple[ScanProfile, bool, bool]:
    detector = scan.get('detector') or {}
    profile = ScanProfile(
        str(scan.get('repository_name') or ''),
        str(scan.get('workflow_path') or ''),
    )
    entry = {
        'repository_name': profile.repository_name,
        'workflow_path': profile.workflow_path,
        'detectors': {
            'cache_smell': {
                'has_smell': detector.get('has_smell', False),
                'smells': detector.get('smells') or [],
                'metadata': detector.get('metadata') or {},
                'error': detector.get('error'),
            }
        },
    }
    _merge_profile(profile, entry)

    dynamic_key = False
    metadata = detector.get('metadata') or {}
    for job_data in (metadata.get('jobs') or {}).values():
        if not isinstance(job_data, dict):
            continue
        analysis = job_data.get('analysis') or {}
        for cache_step in analysis.get('cache_steps') or []:
            if isinstance(cache_step, dict) and _cache_key_is_dynamic(cache_step.get('key')):
                dynamic_key = True
    return profile, bool(detector.get('error')), dynamic_key


def load_profiles(path: Path) -> Tuple[Dict[str, ScanProfile], set[str], Dict[str, bool]]:
    profiles: Dict[str, ScanProfile] = {}
    detector_errors: set[str] = set()
    dynamic_keys: Dict[str, bool] = {}
    for scan in load_jsonl(path):
        version_id = str(scan.get('version_id') or '')
        if not version_id:
            continue
        profile, has_error, dynamic_key = _scan_entry_profile(scan)
        profiles[version_id] = profile
        dynamic_keys[version_id] = dynamic_key
        if has_error:
            detector_errors.add(version_id)
    return profiles, detector_errors, dynamic_keys


def load_runs(
    input_dir: Path,
    profiles: Mapping[str, ScanProfile],
    detector_errors: set[str],
    dynamic_keys: Mapping[str, bool],
) -> Tuple[List[RunObservation], Dict[str, Any]]:
    path = input_dir / 'run_records.jsonl'
    if not path.exists():
        raise FileNotFoundError(f'Missing run-level records: {path}; rerun prepare_rq2_versioned.py')

    rows: List[RunObservation] = []
    # The canonical 30-day queue contains hundreds of thousands of run rows.
    # Keep immutable profile values shared by all runs of one logical version;
    # otherwise each row allocates duplicate frozensets and strings and the
    # bootstrap stage can exhaust memory before fitting the model.
    profile_views: Dict[str, Dict[str, Any]] = {}
    version_commit_cache: Dict[str, Tuple[Optional[datetime], bool]] = {}
    metadata: Dict[str, Any] = {
        'run_records_read': 0,
        'run_records_missing_profile': 0,
        'run_records_detector_error_version': 0,
        'run_records_missing_created_at': 0,
        'run_records_in_analysis': 0,
        'technical_success_runs': 0,
        'technical_failure_runs': 0,
        'valid_all_completed_duration_runs': 0,
        'valid_successful_duration_runs': 0,
    }
    for row in load_jsonl(path):
        metadata['run_records_read'] += 1
        version_id = sys.intern(str(row.get('version_id') or ''))
        if version_id in detector_errors:
            metadata['run_records_detector_error_version'] += 1
            continue
        profile = profiles.get(version_id)
        if profile is None:
            metadata['run_records_missing_profile'] += 1
            continue
        created_at = parse_timestamp(row.get('created_at'))
        if version_id not in version_commit_cache:
            version_commit_cache[version_id] = (
                parse_timestamp(row.get('commit_date')),
                bool(row.get('commit_date_is_run_time_proxy')),
            )
        commit_date, version_age_is_proxy = version_commit_cache[version_id]
        if created_at is None or commit_date is None:
            metadata['run_records_missing_created_at'] += 1
            continue
        if version_id not in profile_views:
            profile_views[version_id] = {
                'repository_name': sys.intern(profile.repository_name),
                'workflow_path': sys.intern(profile.workflow_path),
                'labels': frozenset(profile.labels),
                'eligible_smells': frozenset(profile.eligible_smells),
                'ambiguous_smells': frozenset(profile.ambiguous_smells),
                'dependency_languages': frozenset(profile.dependency_languages),
                'dependency_managers': frozenset(profile.dependency_managers),
                'job_count': profile.job_count,
                'cache_job_count': profile.cache_job_count,
            }
        profile_view = profile_views[version_id]
        conclusion = sys.intern(str(row.get('conclusion') or '').lower())
        duration = as_float(row.get('duration_seconds'))
        observation = RunObservation(
            repository_name=profile_view['repository_name'],
            workflow_path=profile_view['workflow_path'],
            version_id=version_id,
            version_commit_sha=sys.intern(str(row.get('commit_sha') or '')),
            version_commit_date=commit_date,
            labels=profile_view['labels'],
            eligible_smells=profile_view['eligible_smells'],
            ambiguous_smells=profile_view['ambiguous_smells'],
            dependency_languages=profile_view['dependency_languages'],
            dependency_managers=profile_view['dependency_managers'],
            job_count=profile_view['job_count'],
            cache_job_count=profile_view['cache_job_count'],
            event=sys.intern(str(row.get('event') or '').lower()),
            calendar_year=str(created_at.year),
            calendar_month=created_at.strftime('%Y-%m'),
            version_age_days=(
                0.0
                if version_age_is_proxy
                else max(0.0, (created_at - commit_date).total_seconds() / 86400.0)
            ),
            run_order_in_version=int(row.get('run_order_in_version') or 1),
            is_first_run_in_version=bool(row.get('is_first_run_in_version')),
            cache_key_dynamic=bool(dynamic_keys.get(version_id, False)),
            conclusion=conclusion,
            duration_seconds=duration,
            run_id=row.get('run_id'),
            run_attempt=row.get('run_attempt') or 1,
            version_age_is_proxy=version_age_is_proxy,
        )
        rows.append(observation)
        metadata['run_records_in_analysis'] += 1
        metadata['technical_success_runs'] += int(observation.technical_success)
        metadata['technical_failure_runs'] += int(observation.technical_failure)
        metadata['valid_all_completed_duration_runs'] += int(observation.valid_duration)
        metadata['valid_successful_duration_runs'] += int(
            observation.valid_duration and observation.technical_success
        )
    # A busy workflow version can contribute many more runs than a rarely
    # triggered version.  Give every observed version equal total weight so
    # the primary estimand is the average workflow-version association rather
    # than the behavior of a small number of highly active repositories.
    version_counts = Counter(record.version_id for record in rows)
    for record in rows:
        count = max(1, int(version_counts.get(record.version_id, 1)))
        record.version_run_count = count
        record.version_weight = 1.0 / count
    metadata['observed_versions_in_analysis'] = len(version_counts)
    metadata['version_balanced_effective_observations'] = float(
        sum(1.0 for _ in version_counts)
    )
    metadata['weighting'] = (
        'The primary analysis computes inverse run-count weights separately '
        'within each endpoint-specific usable cohort, so each observed '
        'workflow version contributes total weight one to that endpoint; '
        'repository-clustered bootstrap remains the primary uncertainty '
        'procedure.'
    )
    metadata['version_age_proxy_observations'] = sum(
        int(record.version_age_is_proxy) for record in rows
    )
    metadata['version_age_covariate_note'] = (
        'For exact head_sha supplements, commit_date is a run-time proxy and '
        'version age is set to zero rather than treating the proxy as a real '
        'commit timestamp; interval-matched workflow history retains the real '
        'commit date.'
    )
    return rows, metadata


def _observation_weights(
    records: Sequence[RunObservation],
    weighting_mode: str = DEFAULT_WEIGHTING_MODE,
) -> np.ndarray:
    """Return endpoint-specific analysis weights.

    ``records`` is already filtered to one endpoint's usable observations.
    Computing counts here, rather than reusing weights assigned before
    endpoint filtering, guarantees that the selected estimand has the
    advertised unit balance.  The repository mode balances repositories while
    retaining run-level observations within each repository; the run mode is
    the ordinary unweighted sensitivity analysis.
    """

    if weighting_mode not in WEIGHTING_MODES:
        raise ValueError(
            f'unknown weighting mode {weighting_mode!r}; '
            f'choose one of {", ".join(WEIGHTING_MODES)}'
        )
    if not records:
        return np.asarray([], dtype=float)
    if weighting_mode == 'run':
        return np.ones(len(records), dtype=float)
    if weighting_mode == 'version':
        units = [record.version_id for record in records]
    else:
        units = [record.repository_name for record in records]
    counts = Counter(units)
    return np.asarray(
        [1.0 / max(1, int(counts[unit])) for unit in units],
        dtype=float,
    )


def _weighting_unit_count(
    records: Sequence[RunObservation],
    weighting_mode: str,
) -> int:
    if weighting_mode == 'version':
        return len({record.version_id for record in records})
    if weighting_mode == 'repository':
        return len({record.repository_name for record in records})
    return len(records)


def _distribution_summary(values: Sequence[float]) -> Dict[str, Any]:
    """Return a reproducible compact summary of a count distribution."""

    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if array.size == 0:
        return {
            'count': 0,
            'min': None,
            'p25': None,
            'median': None,
            'p75': None,
            'p95': None,
            'p99': None,
            'max': None,
            'mean': None,
        }
    quantiles = np.percentile(array, [25, 50, 75, 95, 99])
    return {
        'count': int(array.size),
        'min': float(np.min(array)),
        'p25': float(quantiles[0]),
        'median': float(quantiles[1]),
        'p75': float(quantiles[2]),
        'p95': float(quantiles[3]),
        'p99': float(quantiles[4]),
        'max': float(np.max(array)),
        'mean': float(np.mean(array)),
    }


def _effective_sample_size(weights: Sequence[float]) -> Optional[float]:
    """Return Kish effective sample size for a set of analysis weights."""

    array = np.asarray(weights, dtype=float)
    array = array[np.isfinite(array) & (array > 0)]
    if array.size == 0:
        return None
    denominator = float(np.sum(array ** 2))
    if denominator <= EPSILON:
        return None
    return float(np.sum(array) ** 2 / denominator)


def _records_by_version(records: Sequence[RunObservation]) -> Dict[str, List[RunObservation]]:
    grouped: Dict[str, List[RunObservation]] = defaultdict(list)
    for record in records:
        grouped[record.version_id].append(record)
    return grouped


def _sample_diagnostics(
    records: Sequence[RunObservation],
    weighting_mode: str,
) -> Dict[str, Any]:
    """Describe run/version/repository multiplicity without changing the model."""

    by_version = _records_by_version(records)
    by_repository: Dict[str, int] = Counter(
        record.repository_name for record in records
    )
    diagnostics: Dict[str, Any] = {
        'runs': len(records),
        'workflow_versions': len(by_version),
        'repositories': len(by_repository),
        'runs_per_workflow_version': _distribution_summary(
            [len(rows) for rows in by_version.values()]
        ),
        'runs_per_repository': _distribution_summary(by_repository.values()),
        'weighting_mode': weighting_mode,
    }
    endpoint_rules = {
        'reliability': lambda record: record.technical_eligible,
        'all_completed_reliability': lambda record: bool(record.conclusion),
        'successful_duration': lambda record: record.technical_success and record.valid_duration,
        'all_completed_duration': lambda record: record.valid_duration,
    }
    endpoints: Dict[str, Any] = {}
    for endpoint, predicate in endpoint_rules.items():
        usable = [record for record in records if predicate(record)]
        weights = _observation_weights(usable, weighting_mode)
        endpoints[endpoint] = {
            'runs': len(usable),
            'workflow_versions': len({record.version_id for record in usable}),
            'repositories': len({record.repository_name for record in usable}),
            'total_weight': float(np.sum(weights)) if weights.size else 0.0,
            'effective_sample_size': _effective_sample_size(weights),
            'effective_weighting_units': _weighting_unit_count(usable, weighting_mode),
        }
    diagnostics['endpoints'] = endpoints
    return diagnostics


def _pairwise_smell_overlap(records: Sequence[RunObservation]) -> Dict[str, Any]:
    """Measure co-exposure at the workflow-version level."""

    versions = _records_by_version(records)
    labels_by_version = {
        version_id: set(rows[0].labels)
        for version_id, rows in versions.items()
        if rows
    }
    counts = {
        smell: sum(smell in labels for labels in labels_by_version.values())
        for smell in OPERATIONALIZED_SMELL_TYPES
    }
    pairs: List[Dict[str, Any]] = []
    for index, left in enumerate(OPERATIONALIZED_SMELL_TYPES):
        for right in OPERATIONALIZED_SMELL_TYPES[index + 1:]:
            both = sum(
                left in labels and right in labels
                for labels in labels_by_version.values()
            )
            union = counts[left] + counts[right] - both
            pairs.append({
                'left': left,
                'right': right,
                'left_versions': counts[left],
                'right_versions': counts[right],
                'both_versions': both,
                'jaccard': float(both / union) if union else None,
            })
    pairs.sort(key=lambda value: (-value['both_versions'], value['left'], value['right']))
    return {
        'unit': 'unique observed workflow version',
        'version_count': len(labels_by_version),
        'version_counts_by_smell': counts,
        'pairs': pairs,
    }


def _design_collinearity_diagnostics(
    matrix: np.ndarray,
    feature_names: Sequence[str],
    high_correlation_threshold: float = 0.70,
) -> Dict[str, Any]:
    """Report predictor correlation and redundancy diagnostics.

    These are diagnostics, not additional hypothesis tests.  The target and
    co-smell indicators are retained in the fitted model; the report makes
    their overlap visible instead of silently removing a smell after seeing
    the result.
    """

    if matrix.ndim != 2 or matrix.shape[1] <= 2:
        return {
            'predictor_count': max(0, int(matrix.shape[1] - 1)),
            'high_correlation_threshold': high_correlation_threshold,
            'high_correlation_pairs': [],
            'vif': {},
            'max_vif': None,
            'condition_number': None,
        }
    predictors = np.asarray(matrix[:, 1:], dtype=float)
    names = list(feature_names[1:])
    valid_columns = [
        index for index in range(predictors.shape[1])
        if np.ptp(predictors[:, index]) > EPSILON
    ]
    if not valid_columns:
        return {
            'predictor_count': 0,
            'high_correlation_threshold': high_correlation_threshold,
            'high_correlation_pairs': [],
            'vif': {},
            'max_vif': None,
            'condition_number': None,
        }
    predictors = predictors[:, valid_columns]
    names = [names[index] for index in valid_columns]
    ranked = np.column_stack([
        rankdata(predictors[:, index], method='average')
        for index in range(predictors.shape[1])
    ])
    correlation = np.corrcoef(ranked, rowvar=False)
    if correlation.ndim == 0:
        correlation = np.asarray([[1.0]])
    high_pairs: List[Dict[str, Any]] = []
    for left_index in range(len(names)):
        for right_index in range(left_index + 1, len(names)):
            value = float(correlation[left_index, right_index])
            if abs(value) >= high_correlation_threshold:
                high_pairs.append({
                    'left': names[left_index],
                    'right': names[right_index],
                    'spearman_rho': value,
                    'absolute_rho': abs(value),
                })
    high_pairs.sort(key=lambda value: (-value['absolute_rho'], value['left'], value['right']))

    vif: Dict[str, Optional[float]] = {}
    for index, name in enumerate(names):
        if len(names) == 1:
            vif[name] = 1.0
            continue
        response = predictors[:, index]
        others = np.delete(predictors, index, axis=1)
        fitted = others @ np.linalg.lstsq(others, response, rcond=None)[0]
        centered = response - float(np.mean(response))
        total = float(np.sum(centered ** 2))
        residual = response - fitted
        r_squared = 1.0 - float(np.sum(residual ** 2)) / total if total > EPSILON else 1.0
        vif[name] = (
            float(1.0 / max(EPSILON, 1.0 - min(1.0, r_squared)))
            if math.isfinite(r_squared) else None
        )
    scaled = predictors / np.maximum(
        np.linalg.norm(predictors, axis=0, keepdims=True), EPSILON
    )
    try:
        condition_number = float(np.linalg.cond(scaled))
    except np.linalg.LinAlgError:
        condition_number = None
    finite_vif = [value for value in vif.values() if value is not None and math.isfinite(value)]
    return {
        'predictor_count': len(names),
        'predictor_names': names,
        'high_correlation_threshold': high_correlation_threshold,
        'high_correlation_pairs': high_pairs,
        'vif': vif,
        'max_vif': max(finite_vif) if finite_vif else None,
        'condition_number': condition_number,
        'interpretation': (
            'high correlations describe co-exposure and possible redundancy; '
            'they do not by themselves justify dropping a smell. The primary '
            'ridge stabilization is retained and these diagnostics are reported.'
        ),
    }


def _top_levels(values: Iterable[str], max_levels: int = 6) -> List[str]:
    counts = Counter(values)
    levels = sorted(counts, key=lambda value: (-counts[value], value))[:max_levels]
    return levels or ['unknown']


def _ecosystem(record: RunObservation) -> str:
    values = sorted(value for value in record.dependency_languages if value)
    if not values:
        return 'none'
    return values[0] if len(values) == 1 else 'multi'


def _standardize(values: Sequence[float]) -> Tuple[np.ndarray, Dict[str, float]]:
    array = np.asarray(values, dtype=float)
    mean = float(np.mean(array)) if array.size else 0.0
    std = float(np.std(array)) if array.size else 0.0
    if std <= EPSILON:
        return np.zeros_like(array), {'mean': mean, 'std': 0.0}
    return (array - mean) / std, {'mean': mean, 'std': std}


def _weighted_prediction_difference(
    matrix: np.ndarray,
    exposure_index: int,
    beta: np.ndarray,
    weights: Optional[np.ndarray],
    logistic: bool,
) -> float:
    """Estimate a standardized exposure contrast using analysis weights."""

    baseline = matrix.copy()
    exposed = matrix.copy()
    baseline[:, exposure_index] = 0.0
    exposed[:, exposure_index] = 1.0
    if logistic:
        baseline_values = _sigmoid(baseline @ beta)
        exposed_values = _sigmoid(exposed @ beta)
    else:
        baseline_values = baseline @ beta
        exposed_values = exposed @ beta
    differences = exposed_values - baseline_values
    if weights is None:
        return float(np.mean(differences))
    weights = np.asarray(weights, dtype=float)
    total = float(np.sum(weights))
    if total <= EPSILON:
        return float(np.mean(differences))
    return float(np.sum(differences * weights) / total)


def _weighted_quantiles(
    values: np.ndarray,
    weights: np.ndarray,
    quantiles: Sequence[float],
) -> List[float]:
    """Return weighted empirical quantiles for compact model diagnostics."""

    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    values = values[valid]
    weights = weights[valid]
    if values.size == 0 or float(np.sum(weights)) <= EPSILON:
        return []
    order = np.argsort(values)
    values = values[order]
    weights = weights[order]
    cumulative = np.cumsum(weights)
    cumulative = (cumulative - 0.5 * weights) / float(np.sum(weights))
    return [
        float(np.interp(float(q), cumulative, values))
        for q in quantiles
    ]


def _weighted_correlation(
    left: np.ndarray,
    right: np.ndarray,
    weights: np.ndarray,
) -> Optional[float]:
    """Return a weighted Pearson correlation when both variables vary."""

    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = (
        np.isfinite(left)
        & np.isfinite(right)
        & np.isfinite(weights)
        & (weights > 0)
    )
    left = left[valid]
    right = right[valid]
    weights = weights[valid]
    total = float(np.sum(weights))
    if left.size < 2 or total <= EPSILON:
        return None
    left_mean = float(np.average(left, weights=weights))
    right_mean = float(np.average(right, weights=weights))
    left_centered = left - left_mean
    right_centered = right - right_mean
    covariance = float(np.sum(weights * left_centered * right_centered) / total)
    left_variance = float(np.sum(weights * left_centered ** 2) / total)
    right_variance = float(np.sum(weights * right_centered ** 2) / total)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator <= EPSILON:
        return None
    return covariance / denominator


def _model_diagnostics(
    matrix: np.ndarray,
    beta: np.ndarray,
    outcome: str,
    values: np.ndarray,
    weights: np.ndarray,
) -> Dict[str, Any]:
    """Summarize diagnostics without turning them into extra hypothesis tests.

    The outcome models are intentionally simple ridge-stabilized models.  These
    summaries make their limitations visible: duration is fitted on the log
    scale, so the checks focus on log-scale residuals and the distribution of
    the original durations; reliability is summarized by fitted probabilities
    and weighted prediction error rather than by a normal-residual test.
    """

    weights = np.asarray(weights, dtype=float)
    values = np.asarray(values, dtype=float)
    if outcome in RELIABILITY_OUTCOMES:
        fitted = _sigmoid(matrix @ beta)
        residuals = values - fitted
        weighted_mean = float(np.average(values, weights=weights))
        null_brier = float(np.average((values - weighted_mean) ** 2, weights=weights))
        brier = float(np.average(residuals ** 2, weights=weights))
        diagnostics: Dict[str, Any] = {
            'outcome_scale': 'probability',
            'observations': int(values.size),
            'weighted_observations': float(np.sum(weights)),
            'fitted_probability_quantiles': _weighted_quantiles(
                fitted, weights, (0.01, 0.25, 0.50, 0.75, 0.99)
            ),
            'weighted_brier_score': brier,
            'weighted_null_brier_score': null_brier,
            'weighted_brier_skill_score': (
                float(1.0 - brier / null_brier) if null_brier > EPSILON else None
            ),
            'weighted_mean_absolute_residual': float(
                np.average(np.abs(residuals), weights=weights)
            ),
            'weighted_residual_mean': float(
                np.average(residuals, weights=weights)
            ),
            'weighted_residual_fitted_correlation': _weighted_correlation(
                residuals, fitted, weights
            ),
        }
        return diagnostics

    fitted = matrix @ beta
    residuals = values - fitted
    raw_durations = np.exp(values)
    weighted_mean = float(np.average(values, weights=weights))
    total_sum_squares = float(
        np.sum(weights * (values - weighted_mean) ** 2)
    )
    residual_sum_squares = float(np.sum(weights * residuals ** 2))
    diagnostics = {
        'outcome_scale': 'log_seconds',
        'observations': int(values.size),
        'weighted_observations': float(np.sum(weights)),
        'weighted_rmse_log_seconds': float(
            math.sqrt(np.average(residuals ** 2, weights=weights))
        ),
        'weighted_mean_absolute_residual_log_seconds': float(
            np.average(np.abs(residuals), weights=weights)
        ),
        'weighted_r_squared_log_seconds': (
            float(1.0 - residual_sum_squares / total_sum_squares)
            if total_sum_squares > EPSILON else None
        ),
        'weighted_residual_mean_log_seconds': float(
            np.average(residuals, weights=weights)
        ),
        'weighted_residual_fitted_correlation': _weighted_correlation(
            residuals, fitted, weights
        ),
        'fitted_log_seconds_quantiles': _weighted_quantiles(
            fitted, weights, (0.01, 0.25, 0.50, 0.75, 0.99)
        ),
        'duration_seconds_quantiles': _weighted_quantiles(
            raw_durations, weights, (0.50, 0.90, 0.95, 0.99, 1.00)
        ),
        'share_duration_over_7_days': float(
            np.average(raw_durations > 7 * 24 * 60 * 60, weights=weights)
        ),
        'share_duration_at_35_day_cap': float(
            np.average(
                raw_durations >= MAX_WORKFLOW_DURATION_SECONDS,
                weights=weights,
            )
        ),
    }
    fitted_order = np.argsort(fitted)
    residual_variances: List[Optional[float]] = []
    for group in np.array_split(fitted_order, 4):
        if group.size == 0:
            residual_variances.append(None)
            continue
        group_weights = weights[group]
        if float(np.sum(group_weights)) <= EPSILON:
            residual_variances.append(None)
            continue
        group_residuals = residuals[group]
        mean = float(np.average(group_residuals, weights=group_weights))
        residual_variances.append(float(
            np.average((group_residuals - mean) ** 2, weights=group_weights)
        ))
    diagnostics['weighted_residual_variance_by_fitted_quartile'] = residual_variances
    finite_variances = [value for value in residual_variances if value is not None]
    diagnostics['max_to_min_quartile_residual_variance_ratio'] = (
        float(max(finite_variances) / min(finite_variances))
        if finite_variances and min(finite_variances) > EPSILON
        else None
    )
    return diagnostics


def build_design_matrix(
    records: Sequence[RunObservation],
    target_smell: str,
) -> Tuple[np.ndarray, List[str], int, Dict[str, Any]]:
    ecosystems = [_ecosystem(record) for record in records]
    events = [record.event if record.event in KNOWN_EVENTS else 'other' for record in records]
    years = [record.calendar_year for record in records]
    months = [record.calendar_month or record.calendar_year for record in records]
    ecosystem_levels = _top_levels(ecosystems)
    event_levels = _top_levels(events)
    year_levels = _top_levels(years)
    month_levels = _top_levels(months, max_levels=12)

    job_values, job_stats = _standardize([
        math.log1p(max(record.job_count, 0)) for record in records
    ])
    cache_values, cache_stats = _standardize([
        math.log1p(max(record.cache_job_count, 0)) for record in records
    ])
    age_values, age_stats = _standardize([
        math.log1p(max(record.version_age_days, 0.0)) for record in records
    ])
    order_values, order_stats = _standardize([
        math.log1p(max(record.run_order_in_version, 1)) for record in records
    ])

    # The supplemented exact snapshots do not carry the workflow-file commit
    # timestamp.  Their ``commit_date`` is the matched run time and is marked
    # with ``version_age_is_proxy`` during loading.  A zero-filled proxy is
    # not a valid version-age adjustment: it is constant in the current
    # cohort and would make the paper's model description disagree with the
    # fitted design matrix.  Include this covariate only when every row has a
    # genuine commit timestamp and the resulting feature varies.
    proxy_age_count = sum(
        int(record.version_age_is_proxy) for record in records
    )
    version_age_included = (
        proxy_age_count == 0 and age_stats['std'] > EPSILON
    )
    version_age_exclusion_reason = None
    if proxy_age_count:
        version_age_exclusion_reason = (
            'workflow-file commit dates are unavailable for some or all '
            'matched snapshots; run-time proxies are not used as version age'
        )
    elif age_stats['std'] <= EPSILON:
        version_age_exclusion_reason = 'version age has no variation in this endpoint cohort'

    columns: List[np.ndarray] = [np.ones(len(records), dtype=float)]
    feature_names = ['intercept']
    exposure = np.asarray([target_smell in record.labels for record in records], dtype=float)
    columns.append(exposure)
    feature_names.append('target_smell')
    numeric_features = [
        (job_values, 'log_job_count_z'),
        (cache_values, 'log_cache_job_count_z'),
        (order_values, 'log_run_order_z'),
    ]
    if version_age_included:
        numeric_features.insert(2, (age_values, 'log_version_age_days_z'))
    for values, name in numeric_features:
        columns.append(values)
        feature_names.append(name)
    columns.append(np.asarray([
        record.is_first_run_in_version for record in records
    ], dtype=float))
    feature_names.append('first_run_in_version')

    for level in ecosystem_levels[1:]:
        columns.append(np.asarray([value == level for value in ecosystems], dtype=float))
        feature_names.append(f'ecosystem={level}')
    for level in event_levels[1:]:
        columns.append(np.asarray([value == level for value in events], dtype=float))
        feature_names.append(f'event={level}')
    for level in year_levels[1:]:
        columns.append(np.asarray([value == level for value in years], dtype=float))
        feature_names.append(f'calendar_year={level}')
    for level in month_levels[1:]:
        columns.append(np.asarray([value == level for value in months], dtype=float))
        feature_names.append(f'calendar_month={level}')
    for smell_type in OPERATIONALIZED_SMELL_TYPES:
        if smell_type == target_smell:
            continue
        columns.append(np.asarray([
            smell_type in record.labels for record in records
        ], dtype=float))
        feature_names.append(f'co_smell={smell_type}')

    raw = np.column_stack(columns).astype(float)
    keep = [0, 1]
    for index in range(2, raw.shape[1]):
        if float(np.ptp(raw[:, index])) > EPSILON:
            keep.append(index)
    matrix = raw[:, keep]
    names = [feature_names[index] for index in keep]
    metadata = {
        'categorical_reference': {
            'ecosystem': ecosystem_levels[0],
            'event': event_levels[0],
            'calendar_year': year_levels[0],
            'calendar_month': month_levels[0],
        },
        'ecosystem_levels': ecosystem_levels,
        'event_levels': event_levels,
        'calendar_year_levels': year_levels,
        'calendar_month_levels': month_levels,
        'standardization': {
            'log_job_count_z': job_stats,
            'log_cache_job_count_z': cache_stats,
            'log_version_age_days_z': age_stats,
            'log_run_order_z': order_stats,
        },
        'version_age': {
            'included': version_age_included,
            'proxy_observations': proxy_age_count,
            'exclusion_reason': version_age_exclusion_reason,
        },
        'requested_covariates': [
            'log_job_count_z',
            'log_cache_job_count_z',
            'log_version_age_days_z',
            'log_run_order_z',
        ],
        'included_covariates': [
            name for name in feature_names
            if name not in {'intercept', 'target_smell'}
        ],
        'excluded_covariates': (
            ['log_version_age_days_z']
            if not version_age_included else []
        ),
    }
    return matrix, names, names.index('target_smell'), metadata


def _cluster_weights(repositories: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    unique, inverse = np.unique(repositories, return_inverse=True)
    sampled = rng.integers(0, len(unique), size=len(unique))
    multiplicities = np.bincount(sampled, minlength=len(unique)).astype(float)
    return multiplicities[inverse]


def _normal_two_sided_p(z_value: Optional[float]) -> Optional[float]:
    """Return a two-sided normal-reference probability for a Wald statistic."""

    if z_value is None or not math.isfinite(float(z_value)):
        return None
    return float(math.erfc(abs(float(z_value)) / math.sqrt(2.0)))


def _cluster_robust_covariance(
    matrix: np.ndarray,
    repositories: np.ndarray,
    beta: np.ndarray,
    outcome: str,
    observation_weights: np.ndarray,
    successes: Optional[np.ndarray] = None,
    trials: Optional[np.ndarray] = None,
    log_durations: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Estimate a repository-clustered sandwich covariance matrix.

    The fitted models use endpoint-specific observation weights and a tiny
    ridge penalty.  The score contributions below use those same weights and
    the fitted mean, then aggregate scores by repository before forming the
    sandwich meat.  This accounts for dependence among repeated runs from the
    same repository without treating repositories as independent runs.
    """

    matrix = np.asarray(matrix, dtype=float)
    beta = np.asarray(beta, dtype=float)
    weights = np.asarray(observation_weights, dtype=float)
    repositories = np.asarray(repositories, dtype=object)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError('cluster-robust covariance requires a non-empty matrix')
    if beta.size != matrix.shape[1] or weights.size != matrix.shape[0]:
        raise ValueError('cluster-robust covariance inputs are misaligned')

    fitted = _sigmoid(matrix @ beta) if outcome in RELIABILITY_OUTCOMES else matrix @ beta
    if outcome in RELIABILITY_OUTCOMES:
        if successes is None or trials is None:
            raise ValueError('reliability covariance requires successes and trials')
        successes = np.asarray(successes, dtype=float)
        trials = np.asarray(trials, dtype=float)
        if successes.size != matrix.shape[0] or trials.size != matrix.shape[0]:
            raise ValueError('reliability outcomes are misaligned')
        residual = successes - trials * fitted
        bread_weights = weights * trials * fitted * (1.0 - fitted)
        score_values = weights * residual
    else:
        if log_durations is None:
            raise ValueError('duration covariance requires log durations')
        log_durations = np.asarray(log_durations, dtype=float)
        if log_durations.size != matrix.shape[0]:
            raise ValueError('duration outcomes are misaligned')
        residual = log_durations - fitted
        bread_weights = weights
        score_values = weights * residual

    penalty = _penalty_matrix(matrix.shape[1])
    bread = matrix.T @ (matrix * bread_weights[:, None]) + RIDGE_PENALTY * penalty
    identity = np.eye(matrix.shape[1], dtype=float)
    try:
        bread_inverse = np.linalg.solve(bread, identity)
    except np.linalg.LinAlgError:
        bread_inverse = np.linalg.pinv(bread, rcond=1e-12)

    _, inverse = np.unique(repositories.astype(str), return_inverse=True)
    cluster_count = int(np.max(inverse) + 1) if inverse.size else 0
    cluster_scores = np.zeros((cluster_count, matrix.shape[1]), dtype=float)
    np.add.at(cluster_scores, inverse, matrix * score_values[:, None])
    meat = cluster_scores.T @ cluster_scores
    if cluster_count > 1:
        meat *= cluster_count / (cluster_count - 1.0)
    covariance = bread_inverse @ meat @ bread_inverse
    covariance = 0.5 * (covariance + covariance.T)
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    return {
        'covariance': covariance,
        'standard_errors': standard_errors,
        'cluster_count': cluster_count,
        'bread_rank': int(np.linalg.matrix_rank(bread)),
    }


def _effect_gradient(
    matrix: np.ndarray,
    exposure_index: int,
    beta: np.ndarray,
    weights: np.ndarray,
    logistic: bool,
) -> np.ndarray:
    """Numerically differentiate the adjusted exposure contrast."""

    beta = np.asarray(beta, dtype=float)
    gradient = np.zeros(beta.size, dtype=float)
    for index in range(beta.size):
        step = 1e-6 * max(1.0, abs(float(beta[index])))
        plus = beta.copy()
        minus = beta.copy()
        plus[index] += step
        minus[index] -= step
        gradient[index] = (
            _weighted_prediction_difference(
                matrix, exposure_index, plus, weights, logistic
            )
            - _weighted_prediction_difference(
                matrix, exposure_index, minus, weights, logistic
            )
        ) / (2.0 * step)
    return gradient


def _cluster_robust_inference(
    matrix: np.ndarray,
    repositories: np.ndarray,
    exposure_index: int,
    beta: np.ndarray,
    outcome: str,
    observation_weights: np.ndarray,
    effect: float,
    feature_names: Optional[Sequence[str]] = None,
    successes: Optional[np.ndarray] = None,
    trials: Optional[np.ndarray] = None,
    log_durations: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Return robust uncertainty for the target-smell coefficient and contrast."""

    logistic = outcome in RELIABILITY_OUTCOMES
    robust = _cluster_robust_covariance(
        matrix,
        repositories,
        beta,
        outcome,
        observation_weights,
        successes=successes,
        trials=trials,
        log_durations=log_durations,
    )
    covariance = robust['covariance']
    standard_error = float(robust['standard_errors'][exposure_index])
    coefficient = float(beta[exposure_index])
    wald_z = coefficient / standard_error if standard_error > EPSILON else None
    p_value = _normal_two_sided_p(wald_z)
    coefficient_ci = (
        [coefficient - 1.96 * standard_error, coefficient + 1.96 * standard_error]
        if math.isfinite(standard_error)
        else None
    )
    ratio_ci = (
        [_safe_exp(value) for value in coefficient_ci]
        if coefficient_ci is not None
        else None
    )
    effect_ci = None
    try:
        gradient = _effect_gradient(
            matrix, exposure_index, beta, observation_weights, logistic
        )
        effect_variance = float(gradient @ covariance @ gradient)
        if effect_variance >= 0 and math.isfinite(effect_variance):
            effect_se = math.sqrt(effect_variance)
            effect_ci = [effect - 1.96 * effect_se, effect + 1.96 * effect_se]
    except (FloatingPointError, ValueError, np.linalg.LinAlgError):
        effect_ci = None
    coefficient_inference = []
    if feature_names is not None:
        if len(feature_names) != beta.size:
            raise ValueError('feature names and coefficient vector are misaligned')
        for name, estimate, robust_se in zip(
            feature_names, beta, robust['standard_errors']
        ):
            estimate = float(estimate)
            robust_se = float(robust_se)
            z_value = estimate / robust_se if robust_se > EPSILON else None
            interval = (
                [estimate - 1.96 * robust_se, estimate + 1.96 * robust_se]
                if math.isfinite(robust_se) else None
            )
            coefficient_inference.append({
                'feature': str(name),
                'coefficient': estimate,
                'cluster_robust_standard_error': robust_se,
                'cluster_robust_wald_z': z_value,
                'cluster_robust_p_value_unadjusted': _normal_two_sided_p(z_value),
                'cluster_robust_coefficient_ci_95': interval,
                'exponentiated_coefficient': _safe_exp(estimate),
                'cluster_robust_exponentiated_ci_95': (
                    [_safe_exp(value) for value in interval]
                    if interval is not None else None
                ),
            })
    return {
        'cluster_count': robust['cluster_count'],
        'bread_rank': robust['bread_rank'],
        'coefficient': coefficient,
        'standard_error': standard_error,
        'wald_z': wald_z,
        'p_value': p_value,
        'coefficient_ci_95': coefficient_ci,
        'ratio_ci_95': ratio_ci,
        'effect_ci_95': effect_ci,
        'coefficient_inference': coefficient_inference,
    }


def _percentile_ci(values: Sequence[float]) -> Optional[List[float]]:
    finite = np.asarray([value for value in values if math.isfinite(float(value))], dtype=float)
    if finite.size < 20:
        return None
    return [float(np.percentile(finite, 2.5)), float(np.percentile(finite, 97.5))]


def _bootstrap_two_sided_p(
    values: Sequence[float],
    null_value: float,
) -> Optional[float]:
    """Return a conservative two-sided tail probability for a bootstrap effect.

    This is reported as a bootstrap tail probability, not as a model-based
    p-value.  The +1 correction prevents a finite bootstrap sample from
    producing an exact zero.  It is used only to apply a transparent
    multiple-comparison rule to the same clustered resamples that generate
    the primary confidence intervals.
    """

    finite = np.asarray(
        [value for value in values if math.isfinite(float(value))],
        dtype=float,
    )
    if finite.size < 20:
        return None
    lower = (float(np.count_nonzero(finite <= null_value)) + 1.0) / (finite.size + 1.0)
    upper = (float(np.count_nonzero(finite >= null_value)) + 1.0) / (finite.size + 1.0)
    return float(min(1.0, 2.0 * min(lower, upper)))


def _benjamini_hochberg(
    p_values: Mapping[str, Optional[float]],
) -> Dict[str, Optional[float]]:
    """Apply Benjamini--Hochberg FDR correction within one endpoint family."""

    valid = sorted(
        (
            name,
            float(value),
        )
        for name, value in p_values.items()
        if value is not None and math.isfinite(float(value))
    )
    valid.sort(key=lambda item: item[1])
    result: Dict[str, Optional[float]] = {name: None for name in p_values}
    if not valid:
        return result
    adjusted: List[float] = [0.0] * len(valid)
    running = 1.0
    total = len(valid)
    for reverse_index in range(total - 1, -1, -1):
        rank = reverse_index + 1
        candidate = min(1.0, valid[reverse_index][1] * total / rank)
        running = min(running, candidate)
        adjusted[reverse_index] = running
    for (name, _), value in zip(valid, adjusted):
        result[name] = float(value)
    return result


def _ci_excludes_null(
    interval: Optional[Sequence[float]],
    null_value: float,
) -> bool:
    if not interval or len(interval) < 2:
        return False
    return float(interval[0]) > null_value or float(interval[1]) < null_value


def _firth_reliability_sensitivity(
    records: Sequence[RunObservation],
    target_smell: str,
    outcome: str,
    weighting_mode: str,
) -> Dict[str, Any]:
    """Fit a bias-reduced logistic sensitivity for a rare smell.

    The estimate is intentionally separated from the primary clustered
    bootstrap result.  Firth correction addresses small-sample separation;
    the reported Wald interval is model-based and is not a replacement for
    repository-clustered uncertainty.
    """

    if outcome == 'reliability':
        usable = [record for record in records if record.technical_eligible]
        successes = np.asarray([record.technical_success for record in usable], dtype=float)
    elif outcome == 'all_completed_reliability':
        usable = [record for record in records if record.conclusion]
        successes = np.asarray([record.conclusion == 'success' for record in usable], dtype=float)
    else:
        return {'status': 'not_estimable', 'reason': f'unsupported Firth outcome: {outcome}'}
    if not usable:
        return {'status': 'not_estimable', 'reason': 'no usable observations'}
    matrix, feature_names, exposure_index, design = build_design_matrix(usable, target_smell)
    if np.unique(matrix[:, exposure_index]).size < 2:
        return {'status': 'not_estimable', 'reason': 'target exposure has no variation'}
    weights = _observation_weights(usable, weighting_mode)
    fit = fit_firth_logistic(
        matrix,
        successes,
        np.ones(len(usable), dtype=float),
        weights,
    )
    coefficient = float(fit['coefficients'][exposure_index])
    standard_error = float(fit['standard_errors'][exposure_index])
    z_value = coefficient / standard_error if standard_error > EPSILON else None
    p_value = (
        float(math.erfc(abs(z_value) / math.sqrt(2.0)))
        if z_value is not None and math.isfinite(z_value)
        else None
    )
    interval = (
        [
            _safe_exp(coefficient - 1.96 * standard_error),
            _safe_exp(coefficient + 1.96 * standard_error),
        ]
        if standard_error >= 0 and math.isfinite(standard_error)
        else None
    )
    effect = _weighted_prediction_difference(
        matrix,
        exposure_index,
        fit['coefficients'],
        weights,
        logistic=True,
    )
    return {
        'status': 'ok',
        'model': 'Firth bias-reduced logistic regression sensitivity',
        'outcome': outcome,
        'analysis_unit': 'run',
        'weighting_mode': weighting_mode,
        'observations': len(usable),
        'effective_observations': _effective_sample_size(weights),
        'technical_trials': float(np.sum(weights)),
        'technical_successes': float(np.sum(successes * weights)),
        'technical_failures': float(np.sum((1.0 - successes) * weights)),
        'adjusted_odds_ratio': _safe_exp(coefficient),
        'adjusted_odds_ratio_ci_95_model_based': interval,
        'adjusted_success_difference_pp': effect * 100.0,
        'coefficient': coefficient,
        'standard_error': standard_error,
        'wald_z': z_value,
        'wald_p_value_unadjusted': p_value,
        'fit_converged': bool(fit.get('converged')),
        'fit_iterations': int(fit.get('iterations') or 0),
        'design': design,
        'feature_names': feature_names,
        'uncertainty_note': (
            'Exploratory rare-smell sensitivity; Firth correction addresses '
            'separation, while the interval is model-based and does not include '
            'repository-clustered bootstrap or FDR correction.'
        ),
    }


def _bootstrap_model(
    matrix: np.ndarray,
    repositories: np.ndarray,
    exposure_index: int,
    outcome: str,
    successes: Optional[np.ndarray],
    trials: Optional[np.ndarray],
    log_durations: Optional[np.ndarray],
    iterations: int,
    seed: int,
    observation_weights: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    coefficients: List[float] = []
    effects: List[float] = []
    ratios: List[float] = []
    if observation_weights is None:
        observation_weights = np.ones(matrix.shape[0], dtype=float)
    else:
        observation_weights = np.asarray(observation_weights, dtype=float)
    if observation_weights.size != matrix.shape[0]:
        raise ValueError('observation weights must align with model rows')
    for _ in range(max(0, int(iterations))):
        weights = _cluster_weights(repositories, rng) * observation_weights
        active = weights > 0
        if np.unique(matrix[active, exposure_index]).size < 2:
            continue
        try:
            if outcome in RELIABILITY_OUTCOMES:
                assert successes is not None and trials is not None
                fit = fit_grouped_binomial_logistic(matrix, successes, trials, weights)
                beta = fit['coefficients']
                effect = _weighted_prediction_difference(
                    matrix, exposure_index, beta, weights, logistic=True
                )
            else:
                assert log_durations is not None
                fit = fit_ridge_log_duration(matrix, log_durations, weights)
                beta = fit['coefficients']
                effect = _weighted_prediction_difference(
                    matrix, exposure_index, beta, weights, logistic=False
                )
            ratio = _safe_exp(float(beta[exposure_index]))
            if ratio is None or not math.isfinite(effect):
                continue
            coefficients.append(float(beta[exposure_index]))
            effects.append(float(effect))
            ratios.append(ratio)
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            continue
    return {
        'valid_replicates': len(coefficients),
        'iterations_requested': max(0, int(iterations)),
        'coefficient_ci_95': _percentile_ci(coefficients),
        'effect_ci_95': _percentile_ci(effects),
        'ratio_ci_95': _percentile_ci(ratios),
        'effect_bootstrap_tail_p_value': _bootstrap_two_sided_p(
            effects, 0.0
        ),
        'ratio_bootstrap_tail_p_value': _bootstrap_two_sided_p(
            ratios, 1.0
        ),
    }


def fit_outcome(
    records: Sequence[RunObservation],
    target_smell: str,
    outcome: str,
    bootstrap_iterations: int,
    seed: int,
    weighting_mode: str = DEFAULT_WEIGHTING_MODE,
) -> Dict[str, Any]:
    if outcome == 'reliability':
        usable = [record for record in records if record.technical_eligible]
    elif outcome == 'all_completed_reliability':
        usable = [record for record in records if record.conclusion]
    elif outcome == 'successful_duration':
        usable = [
            record for record in records
            if record.technical_success and record.valid_duration
        ]
    elif outcome == 'all_completed_duration':
        usable = [record for record in records if record.valid_duration]
    else:
        raise ValueError(f'unknown outcome: {outcome}')
    if not usable:
        return {'status': 'not_estimable', 'reason': 'no usable run observations', 'observations': 0}

    matrix, feature_names, exposure_index, design = build_design_matrix(usable, target_smell)
    design_diagnostics = _design_collinearity_diagnostics(matrix, feature_names)
    if np.unique(matrix[:, exposure_index]).size < 2:
        return {'status': 'not_estimable', 'reason': 'target exposure has no variation', 'observations': len(usable)}
    repositories = np.asarray([record.repository_name for record in usable], dtype=object)
    observation_weights = _observation_weights(usable, weighting_mode)
    effective_observations = float(np.sum(observation_weights))
    effective_units = _weighting_unit_count(usable, weighting_mode)
    if outcome in RELIABILITY_OUTCOMES:
        successes = np.asarray([
            record.technical_success
            if outcome == 'reliability'
            else record.conclusion == 'success'
            for record in usable
        ], dtype=float)
        trials = np.ones(len(usable), dtype=float)
        fit = fit_grouped_binomial_logistic(
            matrix, successes, trials, observation_weights
        )
        beta = fit['coefficients']
        diagnostics = _model_diagnostics(
            matrix, beta, outcome, successes, observation_weights
        )
        effect = _weighted_prediction_difference(
            matrix, exposure_index, beta, observation_weights, logistic=True
        )
        cluster_robust = _cluster_robust_inference(
            matrix,
            repositories,
            exposure_index,
            beta,
            outcome,
            observation_weights,
            effect,
            feature_names=feature_names,
            successes=successes,
            trials=trials,
        )
        bootstrap = _bootstrap_model(
            matrix, repositories, exposure_index, outcome,
            successes, trials, None, bootstrap_iterations, seed,
            observation_weights,
        )
        return {
            'model': f'{weighting_mode}-weighted run-level grouped-binomial logistic regression',
            'formula': (
                'technical_success / technical_failure ~ target_smell + covariates'
                if outcome == 'reliability'
                else 'success / all_other_completed_conclusions ~ target_smell + covariates'
            ),
            'outcome': outcome,
            'outcome_definition': (
                'success versus failure, startup_failure, or timed_out'
                if outcome == 'reliability'
                else 'success versus every other non-empty completed conclusion '
                     '(including cancelled, skipped, action_required, and neutral)'
            ),
            'analysis_unit': 'run',
            'weighting_mode': weighting_mode,
            'observations': len(usable),
            'effective_observations': effective_observations,
            'effective_weighting_units': effective_units,
            'effective_version_observations': (
                effective_observations if weighting_mode == 'version' else None
            ),
            'technical_trials': float(np.sum(trials)),
            'technical_successes': float(np.sum(successes)),
            'technical_failures': float(np.sum(trials - successes)),
            'successes': float(np.sum(successes)),
            'failures': float(np.sum(trials - successes)),
            'weighted_technical_trials': float(np.sum(trials * observation_weights)),
            'weighted_technical_successes': float(np.sum(successes * observation_weights)),
            'weighted_technical_failures': float(
                np.sum((trials - successes) * observation_weights)
            ),
            'adjusted_odds_ratio': _safe_exp(float(beta[exposure_index])),
            'adjusted_technical_success_difference_pp': effect * 100.0,
            'adjusted_success_difference_pp': effect * 100.0,
            'adjusted_odds_ratio_ci_95': bootstrap['ratio_ci_95'],
            'adjusted_success_difference_ci_95': (
                [value * 100.0 for value in bootstrap['effect_ci_95']]
                if bootstrap['effect_ci_95'] else None
            ),
            'success_difference_ci_95': (
                [value * 100.0 for value in bootstrap['effect_ci_95']]
                if bootstrap['effect_ci_95'] else None
            ),
            'bootstrap_tail_p_value': bootstrap['effect_bootstrap_tail_p_value'],
            'odds_ratio_bootstrap_tail_p_value': bootstrap['ratio_bootstrap_tail_p_value'],
            'cluster_robust_cluster_count': cluster_robust['cluster_count'],
            'cluster_robust_bread_rank': cluster_robust['bread_rank'],
            'cluster_robust_standard_error': cluster_robust['standard_error'],
            'cluster_robust_wald_z': cluster_robust['wald_z'],
            'cluster_robust_p_value_unadjusted': cluster_robust['p_value'],
            'cluster_robust_coefficient_ci_95': cluster_robust['coefficient_ci_95'],
            'cluster_robust_odds_ratio_ci_95': cluster_robust['ratio_ci_95'],
            'cluster_robust_success_difference_ci_95': (
                [value * 100.0 for value in cluster_robust['effect_ci_95']]
                if cluster_robust['effect_ci_95'] else None
            ),
            'fit_converged': bool(fit.get('converged')),
            'fit_iterations': int(fit.get('iterations') or 0),
            'model_diagnostics': diagnostics,
            'design_collinearity': design_diagnostics,
            'feature_names': feature_names,
            'coefficient_inference': cluster_robust['coefficient_inference'],
            'design': design,
            'bootstrap': bootstrap,
        }

    durations = np.asarray([record.duration_seconds for record in usable], dtype=float)
    log_durations = np.log(durations)
    fit = fit_ridge_log_duration(matrix, log_durations, observation_weights)
    beta = fit['coefficients']
    diagnostics = _model_diagnostics(
        matrix, beta, outcome, log_durations, observation_weights
    )
    effect = _weighted_prediction_difference(
        matrix, exposure_index, beta, observation_weights, logistic=False
    )
    cluster_robust = _cluster_robust_inference(
        matrix,
        repositories,
        exposure_index,
        beta,
        outcome,
        observation_weights,
        effect,
        feature_names=feature_names,
        log_durations=log_durations,
    )
    bootstrap = _bootstrap_model(
        matrix, repositories, exposure_index, outcome,
        None, None, log_durations, bootstrap_iterations, seed,
        observation_weights,
    )
    return {
        'model': f'{weighting_mode}-weighted run-level linear regression on log duration',
        'formula': f'log(duration_seconds) ~ target_smell + covariates ({outcome})',
        'analysis_unit': 'run',
        'weighting_mode': weighting_mode,
        'observations': len(usable),
        'effective_observations': effective_observations,
        'effective_weighting_units': effective_units,
        'effective_version_observations': (
            effective_observations if weighting_mode == 'version' else None
        ),
        'adjusted_duration_ratio': _safe_exp(float(beta[exposure_index])),
        'adjusted_log_duration_difference': effect,
        'adjusted_duration_ratio_ci_95': bootstrap['ratio_ci_95'],
        'bootstrap_tail_p_value': bootstrap['effect_bootstrap_tail_p_value'],
        'duration_ratio_bootstrap_tail_p_value': bootstrap['ratio_bootstrap_tail_p_value'],
        'cluster_robust_cluster_count': cluster_robust['cluster_count'],
        'cluster_robust_bread_rank': cluster_robust['bread_rank'],
        'cluster_robust_standard_error': cluster_robust['standard_error'],
        'cluster_robust_wald_z': cluster_robust['wald_z'],
        'cluster_robust_p_value_unadjusted': cluster_robust['p_value'],
        'cluster_robust_coefficient_ci_95': cluster_robust['coefficient_ci_95'],
        'cluster_robust_duration_ratio_ci_95': cluster_robust['ratio_ci_95'],
        'cluster_robust_log_duration_difference_ci_95': cluster_robust['effect_ci_95'],
        'fit_converged': True,
        'fit_iterations': None,
        'model_diagnostics': diagnostics,
        'design_collinearity': design_diagnostics,
        'feature_names': feature_names,
        'coefficient_inference': cluster_robust['coefficient_inference'],
        'design': design,
        'bootstrap': bootstrap,
        'rmse_log_seconds': fit.get('rmse_log_seconds'),
    }


def _cohort(records: Sequence[RunObservation], smell_type: str) -> Tuple[List[RunObservation], List[RunObservation], int]:
    exposed: List[RunObservation] = []
    controls: List[RunObservation] = []
    ambiguous = 0
    for record in records:
        if smell_type in record.labels:
            exposed.append(record)
        elif smell_type in record.ambiguous_smells:
            ambiguous += 1
        elif smell_type in record.eligible_smells:
            controls.append(record)
    return exposed, controls, ambiguous


def duration_descriptive(
    exposed: Sequence[RunObservation],
    controls: Sequence[RunObservation],
) -> Dict[str, Any]:
    exposed_values = [record.duration_seconds for record in exposed if record.valid_duration]
    control_values = [record.duration_seconds for record in controls if record.valid_duration]
    rank_sum = _wilcoxon_rank_sum(exposed_values, control_values)
    exposed_median = float(np.median(exposed_values)) if exposed_values else None
    control_median = float(np.median(control_values)) if control_values else None
    return {
        'exposed_n': len(exposed_values),
        'control_n': len(control_values),
        'exposed_median_seconds': exposed_median,
        'control_median_seconds': control_median,
        'unadjusted_duration_ratio': (
            exposed_median / control_median
            if exposed_median is not None and control_median and control_median > 0
            else None
        ),
        'cliffs_delta': _cliffs_delta(exposed_values, control_values),
        'wilcoxon_rank_sum_u_statistic': rank_sum['u_statistic'],
        'wilcoxon_rank_sum_p_value': rank_sum['p_value'],
    }


def _bootstrap_paired(
    pairs: Sequence[Mapping[str, Any]],
    iterations: int,
    seed: int,
) -> Dict[str, Any]:
    repositories = sorted({str(pair['repository_name']) for pair in pairs})
    if not repositories:
        return {'valid_replicates': 0, 'duration_ratio_ci_95': None, 'success_difference_ci_95': None}
    by_repo: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for pair in pairs:
        by_repo[str(pair['repository_name'])].append(pair)
    rng = np.random.default_rng(seed)
    duration_ratios: List[float] = []
    success_differences: List[float] = []
    for _ in range(max(0, int(iterations))):
        sampled = rng.integers(0, len(repositories), size=len(repositories))
        selected: List[Mapping[str, Any]] = []
        for index in sampled:
            selected.extend(by_repo[repositories[index]])
        duration_logs = [
            math.log(float(pair['exposed_duration']) / float(pair['control_duration']))
            for pair in selected
            if pair.get('exposed_duration') and pair.get('control_duration')
            and pair['exposed_duration'] > 0 and pair['control_duration'] > 0
        ]
        success_diffs = [
            float(pair['exposed_success_rate']) - float(pair['control_success_rate'])
            for pair in selected
            if pair.get('exposed_success_rate') is not None
            and pair.get('control_success_rate') is not None
        ]
        if duration_logs:
            duration_ratios.append(math.exp(float(np.mean(duration_logs))))
        if success_diffs:
            success_differences.append(float(np.mean(success_diffs)))
    return {
        'valid_replicates': max(len(duration_ratios), len(success_differences)),
        'duration_ratio_ci_95': _percentile_ci(duration_ratios),
        'success_difference_ci_95': _percentile_ci(success_differences),
    }


def within_workflow_sensitivity(
    records: Sequence[RunObservation],
    target_smell: str,
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    """Compare exposed and control logical versions within each workflow.

    A version contributes one duration median and one technical-success rate
    before the paired comparison is formed.  This prevents a version with
    unusually many runs from dominating the within-workflow sensitivity
    analysis.  The repository bootstrap still resamples the paired workflow
    identities as clusters.
    """

    by_identity: Dict[Tuple[str, str], List[RunObservation]] = defaultdict(list)
    for record in records:
        by_identity[record.workflow_identity].append(record)
    pairs: List[Dict[str, Any]] = []
    for identity, items in by_identity.items():
        by_version: Dict[str, List[RunObservation]] = defaultdict(list)
        for record in items:
            by_version[record.version_id].append(record)
        exposed_duration_medians: List[float] = []
        control_duration_medians: List[float] = []
        exposed_success_rates: List[float] = []
        control_success_rates: List[float] = []
        exposed_versions = 0
        control_versions = 0
        for version_records in by_version.values():
            representative = version_records[0]
            if target_smell in representative.labels:
                state = 'exposed'
            elif target_smell in representative.ambiguous_smells:
                continue
            elif target_smell in representative.eligible_smells:
                state = 'control'
            else:
                continue
            successful_durations = [
                record.duration_seconds
                for record in version_records
                if record.technical_success and record.valid_duration
            ]
            technical_records = [
                record for record in version_records if record.technical_eligible
            ]
            if not successful_durations or not technical_records:
                continue
            duration_median = float(np.median(successful_durations))
            success_rate = sum(
                record.technical_success for record in technical_records
            ) / len(technical_records)
            if state == 'exposed':
                exposed_versions += 1
                exposed_duration_medians.append(duration_median)
                exposed_success_rates.append(float(success_rate))
            else:
                control_versions += 1
                control_duration_medians.append(duration_median)
                control_success_rates.append(float(success_rate))
        if (
            not exposed_duration_medians
            or not control_duration_medians
            or not exposed_success_rates
            or not control_success_rates
        ):
            continue
        pairs.append({
            'repository_name': identity[0],
            'workflow_path': identity[1],
            'exposed_duration': float(np.median(exposed_duration_medians)),
            'control_duration': float(np.median(control_duration_medians)),
            'exposed_success_rate': float(np.mean(exposed_success_rates)),
            'control_success_rate': float(np.mean(control_success_rates)),
            'exposed_versions': exposed_versions,
            'control_versions': control_versions,
        })
    if not pairs:
        return {'workflow_identities_with_both_exposure_states': 0}
    duration_log_ratios = [
        math.log(pair['exposed_duration'] / pair['control_duration'])
        for pair in pairs
    ]
    success_differences = [
        pair['exposed_success_rate'] - pair['control_success_rate']
        for pair in pairs
    ]
    bootstrap = _bootstrap_paired(pairs, bootstrap_iterations, seed)
    return {
        'workflow_identities_with_both_exposure_states': len(pairs),
        'repositories': len({pair['repository_name'] for pair in pairs}),
        'within_workflow_duration_ratio': math.exp(float(np.mean(duration_log_ratios))),
        'within_workflow_success_difference_pp': float(np.mean(success_differences)) * 100.0,
        'duration_ratio_ci_95': bootstrap['duration_ratio_ci_95'],
        'success_difference_ci_95': (
            [value * 100.0 for value in bootstrap['success_difference_ci_95']]
            if bootstrap['success_difference_ci_95'] else None
        ),
        'exposure_unit': 'logical workflow version',
        'pair_unit': 'workflow identity',
        'interpretation': (
            'version-level paired descriptive sensitivity with repository '
            'bootstrap; not a causal estimate'
        ),
    }


def no_fallback_strata(
    records: Sequence[RunObservation],
    target_smell: str = 'no_fallback_cache',
) -> Dict[str, Any]:
    exposed, controls, ambiguous = _cohort(records, target_smell)
    strata = {
        'all': lambda record: True,
        'first_run_in_version': lambda record: record.is_first_run_in_version,
        'later_runs_in_version': lambda record: not record.is_first_run_in_version,
        'dynamic_cache_key': lambda record: record.cache_key_dynamic,
        'non_dynamic_cache_key': lambda record: not record.cache_key_dynamic,
    }
    result: Dict[str, Any] = {'excluded_ambiguous_runs': ambiguous}
    for name, predicate in strata.items():
        exp = [record for record in exposed if predicate(record)]
        ctl = [record for record in controls if predicate(record)]
        result[name] = duration_descriptive(
            [record for record in exp if record.technical_success],
            [record for record in ctl if record.technical_success],
        )
    return result


# Compatibility for callers of the historical helper.
no_restore_strata = no_fallback_strata


def analyze(
    input_dir: Path,
    output_dir: Path,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = 42,
    weighting_mode: str = DEFAULT_WEIGHTING_MODE,
    core_only: bool = False,
) -> Dict[str, Any]:
    if weighting_mode not in WEIGHTING_MODES:
        raise ValueError(
            f'unknown weighting mode {weighting_mode!r}; '
            f'choose one of {", ".join(WEIGHTING_MODES)}'
        )
    profiles, detector_errors, dynamic_keys = load_profiles(
        input_dir / 'version_scan_results.jsonl'
    )
    records, run_metadata = load_runs(input_dir, profiles, detector_errors, dynamic_keys)
    matching_metadata_path = input_dir / 'matching_metadata.json'
    matching_metadata = {}
    if matching_metadata_path.exists():
        matching_metadata = loads_smell_json(matching_metadata_path.read_text(encoding='utf-8'))

    comparisons: Dict[str, Any] = {}
    for index, smell_type in enumerate(OPERATIONALIZED_SMELL_TYPES):
        exposed, controls, ambiguous = _cohort(records, smell_type)
        ambiguous_records = [
            record for record in records
            if smell_type not in record.labels
            and smell_type in record.ambiguous_smells
        ]
        cohort = exposed + controls
        exposed_versions = len({record.version_id for record in exposed})
        exposed_repositories = len({record.repository_name for record in exposed})
        confirmatory_eligible = (
            exposed_versions >= MIN_EXPOSED_VERSIONS
            and exposed_repositories >= MIN_EXPOSED_REPOSITORIES
        )
        not_run = {
            'status': 'not_run',
            'reason': (
                'omitted from the core exact-matching analysis; only the '
                'primary duration endpoint and secondary technical-reliability '
                'endpoint were bootstrapped'
            ),
            'observations': 0,
        }
        comparisons[smell_type] = {
            'analysis_unit': 'run',
            'cluster_unit': 'repository',
            'exposure_unit': 'logical workflow version, inherited by matched runs',
            'outcome_unit': 'workflow run',
            'eligibility_rule': COHORT_ELIGIBILITY_RULES[smell_type],
            'exposed_runs': len(exposed),
            'eligible_control_runs': len(controls),
            'excluded_ambiguous_runs': ambiguous,
            'exposed_logical_versions': exposed_versions,
            'eligible_control_logical_versions': len({record.version_id for record in controls}),
            'excluded_ambiguous_logical_versions': len({
                record.version_id for record in ambiguous_records
            }),
            'exposed_repositories': exposed_repositories,
            'control_repositories': len({record.repository_name for record in controls}),
            'sample_diagnostics': _sample_diagnostics(cohort, weighting_mode),
            'rare_smell': not confirmatory_eligible,
            'confirmatory_eligible': confirmatory_eligible,
            'interpretation_scope': (
                'confirmatory' if confirmatory_eligible else 'exploratory/descriptive'
            ),
            'rare_smell_rule': RARE_SMELL_POLICY,
            'reliability': fit_outcome(
                cohort, smell_type, 'reliability', bootstrap_iterations, seed + index,
                weighting_mode,
            ),
            'all_completed_reliability': (
                not_run if core_only else fit_outcome(
                    cohort, smell_type, 'all_completed_reliability',
                    bootstrap_iterations, seed + 5_000 + index, weighting_mode,
                )
            ),
            'successful_duration': (
                not_run if core_only else fit_outcome(
                    cohort, smell_type, 'successful_duration',
                    bootstrap_iterations, seed + 10_000 + index, weighting_mode,
                )
            ),
            'all_completed_duration': fit_outcome(
                cohort, smell_type, 'all_completed_duration', bootstrap_iterations,
                seed + 20_000 + index, weighting_mode,
            ),
            'successful_duration_descriptive': duration_descriptive(
                [record for record in exposed if record.technical_success],
                [record for record in controls if record.technical_success],
            ),
            'all_completed_duration_descriptive': duration_descriptive(
                exposed,
                controls,
            ),
            'within_workflow_sensitivity': within_workflow_sensitivity(
                cohort, smell_type,
                0 if core_only else bootstrap_iterations,
                seed + 30_000 + index,
            ) if not core_only else {
                'status': 'not_run',
                'reason': (
                    'omitted from the core exact-matching analysis; within-workflow '
                    'sensitivity is reserved for the subsequent sensitivity stage'
                ),
            },
        }
        if not confirmatory_eligible:
            comparisons[smell_type]['firth_reliability'] = _firth_reliability_sensitivity(
                cohort, smell_type, 'reliability', weighting_mode
            )
            comparisons[smell_type]['firth_all_completed_reliability'] = (
                _firth_reliability_sensitivity(
                    cohort, smell_type, 'all_completed_reliability', weighting_mode
                )
            )
        if smell_type == 'no_fallback_cache':
            comparisons[smell_type]['stratified_duration'] = no_fallback_strata(records)

    # Apply the same multiplicity rule to each prespecified endpoint family.
    # The bootstrap tail probabilities are deliberately kept separate from
    # the unadjusted Wilcoxon p-values, which remain descriptive only.
    endpoint_p_values = {
        'reliability': {
            smell_type: (
                comparison['reliability'].get('bootstrap_tail_p_value')
            )
            for smell_type, comparison in comparisons.items()
        },
        'all_completed_reliability': {
            smell_type: (
                comparison['all_completed_reliability'].get('bootstrap_tail_p_value')
            )
            for smell_type, comparison in comparisons.items()
        },
        'successful_duration': {
            smell_type: (
                comparison['successful_duration'].get(
                    'duration_ratio_bootstrap_tail_p_value'
                )
            )
            for smell_type, comparison in comparisons.items()
        },
        'all_completed_duration': {
            smell_type: (
                comparison['all_completed_duration'].get(
                    'duration_ratio_bootstrap_tail_p_value'
                )
            )
            for smell_type, comparison in comparisons.items()
        },
        'successful_duration_wilcoxon_descriptive': {
            smell_type: (
                comparison['successful_duration_descriptive'].get(
                    'wilcoxon_rank_sum_p_value'
                )
            )
            for smell_type, comparison in comparisons.items()
        },
        'all_completed_duration_wilcoxon_descriptive': {
            smell_type: (
                comparison['all_completed_duration_descriptive'].get(
                    'wilcoxon_rank_sum_p_value'
                )
            )
            for smell_type, comparison in comparisons.items()
        },
    }
    cluster_robust_endpoint_p_values = {
        endpoint: {
            smell_type: comparison[endpoint].get(
                'cluster_robust_p_value_unadjusted'
            )
            for smell_type, comparison in comparisons.items()
        }
        for endpoint in (
            'reliability',
            'all_completed_reliability',
            'successful_duration',
            'all_completed_duration',
        )
    }
    endpoint_q_values = {
        endpoint: _benjamini_hochberg(p_values)
        for endpoint, p_values in endpoint_p_values.items()
    }
    cluster_robust_endpoint_q_values = {
        endpoint: _benjamini_hochberg(p_values)
        for endpoint, p_values in cluster_robust_endpoint_p_values.items()
    }
    for smell_type, comparison in comparisons.items():
        reliability = comparison['reliability']
        all_completed_reliability = comparison['all_completed_reliability']
        successful_duration = comparison['successful_duration']
        all_completed_duration = comparison['all_completed_duration']
        descriptive_duration = comparison['successful_duration_descriptive']
        all_completed_descriptive = comparison['all_completed_duration_descriptive']

        reliability['fdr_q_value'] = endpoint_q_values['reliability'][smell_type]
        reliability['significant_after_fdr'] = bool(
            reliability['fdr_q_value'] is not None
            and reliability['fdr_q_value'] < 0.05
            and _ci_excludes_null(
                reliability.get('adjusted_success_difference_ci_95'), 0.0
            )
        )
        all_completed_reliability['fdr_q_value'] = endpoint_q_values[
            'all_completed_reliability'
        ][smell_type]
        all_completed_reliability['significant_after_fdr'] = bool(
            all_completed_reliability['fdr_q_value'] is not None
            and all_completed_reliability['fdr_q_value'] < 0.05
            and _ci_excludes_null(
                all_completed_reliability.get('adjusted_success_difference_ci_95'), 0.0
            )
        )
        successful_duration['fdr_q_value'] = endpoint_q_values[
            'successful_duration'
        ][smell_type]
        successful_duration['significant_after_fdr'] = bool(
            successful_duration['fdr_q_value'] is not None
            and successful_duration['fdr_q_value'] < 0.05
            and _ci_excludes_null(
                successful_duration.get('adjusted_duration_ratio_ci_95'), 1.0
            )
        )
        all_completed_duration['fdr_q_value'] = endpoint_q_values[
            'all_completed_duration'
        ][smell_type]
        all_completed_duration['significant_after_fdr'] = bool(
            all_completed_duration['fdr_q_value'] is not None
            and all_completed_duration['fdr_q_value'] < 0.05
            and _ci_excludes_null(
                all_completed_duration.get('adjusted_duration_ratio_ci_95'), 1.0
            )
        )
        robust_ci_specs = {
            'reliability': ('cluster_robust_success_difference_ci_95', 0.0),
            'all_completed_reliability': (
                'cluster_robust_success_difference_ci_95', 0.0
            ),
            'successful_duration': ('cluster_robust_duration_ratio_ci_95', 1.0),
            'all_completed_duration': (
                'cluster_robust_duration_ratio_ci_95', 1.0
            ),
        }
        for endpoint, (interval_key, null_value) in robust_ci_specs.items():
            endpoint_result = comparison[endpoint]
            endpoint_result['cluster_robust_fdr_q_value'] = (
                cluster_robust_endpoint_q_values[endpoint][smell_type]
            )
            endpoint_result['cluster_robust_significant_after_fdr'] = bool(
                endpoint_result['cluster_robust_fdr_q_value'] is not None
                and endpoint_result['cluster_robust_fdr_q_value'] < 0.05
                and _ci_excludes_null(
                    endpoint_result.get(interval_key), null_value
                )
            )
        descriptive_duration['wilcoxon_rank_sum_fdr_q_value'] = (
            endpoint_q_values['successful_duration_wilcoxon_descriptive'][smell_type]
        )
        all_completed_descriptive['wilcoxon_rank_sum_fdr_q_value'] = (
            endpoint_q_values['all_completed_duration_wilcoxon_descriptive'][smell_type]
        )

        # Preserve the statistical signal for transparency, but do not label
        # a rare smell as confirmatory evidence.  This prevents a sparse
        # exposure (especially one affected by separation) from becoming a
        # headline finding solely because its adjusted q-value is small.
        for endpoint in (
            'reliability',
            'all_completed_reliability',
            'successful_duration',
            'all_completed_duration',
        ):
            endpoint_result = comparison[endpoint]
            endpoint_result['exploratory_signal_after_fdr'] = endpoint_result.get(
                'significant_after_fdr', False
            )
            endpoint_result['cluster_robust_exploratory_signal_after_fdr'] = (
                endpoint_result.get('cluster_robust_significant_after_fdr', False)
            )
            if not comparison.get('confirmatory_eligible', False):
                endpoint_result['significant_after_fdr'] = False
                endpoint_result['cluster_robust_significant_after_fdr'] = False

    model_endpoints = (
        'reliability',
        'all_completed_reliability',
        'successful_duration',
        'all_completed_duration',
    )
    version_age_included_endpoints = sorted({
        endpoint
        for comparison in comparisons.values()
        for endpoint in model_endpoints
        if 'log_version_age_days_z' in comparison.get(endpoint, {}).get(
            'feature_names', []
        )
    })
    version_age_excluded_endpoints = sorted(
        set(model_endpoints) - set(version_age_included_endpoints)
    )
    base_covariates = [
        'target smell exposure',
        'primary dependency ecosystem',
        'run event',
        'calendar year',
        'calendar month',
        'log job count',
        'log cache-job count',
        'log run order within version',
        'first run within version',
        'indicators for other operationalized smells',
    ]
    if version_age_included_endpoints:
        base_covariates.insert(7, 'log version age in days')

    metadata = {
        **matching_metadata,
        **run_metadata,
        'run_records_path': str(input_dir / 'run_records.jsonl'),
        'version_records_path': str(input_dir / 'version_records.jsonl'),
        'version_scan_results_path': str(input_dir / 'version_scan_results.jsonl'),
        'operationalized_smell_types': list(OPERATIONALIZED_SMELL_TYPES),
        'rare_smell_policy': RARE_SMELL_POLICY,
        'rare_smell_thresholds': {
            'minimum_exposed_workflow_versions': MIN_EXPOSED_VERSIONS,
            'minimum_exposed_repositories': MIN_EXPOSED_REPOSITORIES,
        },
        'sample_composition': _sample_diagnostics(records, weighting_mode),
        'co_exposure_overlap': _pairwise_smell_overlap(records),
        'design_collinearity_diagnostics': {
            smell_type: {
                endpoint: comparisons[smell_type][endpoint].get('design_collinearity')
                for endpoint in (
                    'reliability',
                    'all_completed_reliability',
                    'successful_duration',
                    'all_completed_duration',
                )
                if isinstance(comparisons[smell_type].get(endpoint), dict)
                and comparisons[smell_type][endpoint].get('design_collinearity') is not None
            }
            for smell_type in comparisons
        },
        'analysis': 'run-level version-aligned adjusted RQ2 analysis',
        'analysis_unit': 'run',
        'cluster_unit': 'repository',
        'exposure_unit': 'logical workflow version, inherited by matched runs',
        'outcome_unit': 'workflow run',
        'cohort_eligibility_rules': COHORT_ELIGIBILITY_RULES,
        'control_definition': (
            'For each smell separately, controls are detector-eligible logical '
            'workflow versions without the target label. Versions marked ambiguous '
            'for that smell are excluded from both the control cohort and the '
            'primary comparison.'
        ),
        'weighting_mode': weighting_mode,
        'estimand': (
            {
                'version': (
                    'endpoint-specific version-balanced association: every '
                    'observed workflow version has total weight one within the '
                    'endpoint, with repeated runs retained within that version'
                ),
                'run': (
                    'run-weighted association: every usable run contributes '
                    'equal analysis weight'
                ),
                'repository': (
                    'repository-balanced association: every repository has '
                    'total weight one within the endpoint, with repeated runs '
                    'retained within that repository'
                ),
            }[weighting_mode]
        ),
        'historical_detector_error_versions': len(detector_errors),
        'historical_detector_error_runs_excluded': run_metadata['run_records_detector_error_version'],
        # ``covariates`` describes the fitted models, not merely the fields
        # that were requested by the analysis plan.  This prevents a
        # constant/proxy-only field from being reported as an adjustment.
        'covariates': base_covariates,
        'requested_covariates': [
            'target smell exposure',
            'primary dependency ecosystem',
            'run event',
            'calendar year',
            'calendar month',
            'log job count',
            'log cache-job count',
            'log version age in days',
            'log run order within version',
            'first run within version',
            'indicators for other operationalized smells',
        ],
        'version_age_covariate': {
            'included_in_endpoints': version_age_included_endpoints,
            'excluded_from_endpoints': version_age_excluded_endpoints,
            'proxy_observations': run_metadata.get('version_age_proxy_observations', 0),
            'reason': (
                'The current supplemented snapshots use run-time proxies for '
                'workflow-file commit dates; proxy ages are not entered into '
                'the fitted models.'
                if run_metadata.get('version_age_proxy_observations', 0)
                else None
            ),
        },
        'duration_endpoints': {
            'primary': 'all_completed_duration',
            'all_completed_duration': 'valid duration among all completed runs',
            'successful_duration': (
                'valid duration among technically successful runs; sensitivity '
                'analysis because conditioning on success can induce selection'
            ),
        },
        'reliability_endpoints': {
            'primary': 'reliability',
            'reliability': (
                'success versus failure, startup_failure, or timed_out; '
                'neutral/non-technical conclusions are excluded'
            ),
            'all_completed_reliability': (
                'success versus every other non-empty completed conclusion; '
                'sensitivity endpoint'
            ),
        },
        'analysis_plan': {
            'primary_endpoint': 'all_completed_duration',
            'secondary_endpoint': 'technical reliability',
            'sensitivity_endpoints': [
                'all_completed_reliability',
                'successful_duration',
                'within_workflow',
            ],
            'control_cohort': 'smell-specific detector-eligible controls',
            'primary_weighting': (
                'equal weight per usable workflow run'
                if weighting_mode == 'run'
                else 'endpoint-specific inverse run-count by logical workflow version'
            ),
            'weighting_sensitivities': [
                'endpoint-specific inverse run-count by logical workflow version',
                'endpoint-specific inverse run-count by repository',
            ],
            'effect_interpretation': 'adjusted observational association',
        },
        'analysis_scope': (
            'core endpoints only: all_completed_duration as the primary endpoint '
            'and reliability as the secondary endpoint'
            if core_only else 'full endpoint set'
        ),
        'multiple_comparison_correction': {
            'method': 'Benjamini-Hochberg false discovery rate',
            'alpha': 0.05,
            'primary_p_value_source': (
                'two-sided Wald p-values from repository-clustered sandwich '
                'standard errors for the target-smell coefficient'
            ),
            'primary_fdr_q_value_fields': {
                'reliability': 'cluster_robust_fdr_q_value',
                'all_completed_reliability': 'cluster_robust_fdr_q_value',
                'successful_duration': 'cluster_robust_fdr_q_value',
                'all_completed_duration': 'cluster_robust_fdr_q_value',
            },
            'families': {
                'reliability': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} smell-level '
                    'cluster-robust Wald p-values'
                ),
                'all_completed_reliability': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} smell-level '
                    'cluster-robust Wald p-values; sensitivity endpoint '
                    'using every non-empty completed conclusion'
                ),
                'successful_duration': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} smell-level '
                    'cluster-robust Wald p-values'
                ),
                'all_completed_duration': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} smell-level '
                    'cluster-robust Wald p-values'
                ),
                'successful_duration_wilcoxon_descriptive': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} unadjusted Wilcoxon '
                    'rank-sum p-values; descriptive only'
                ),
                'all_completed_duration_wilcoxon_descriptive': (
                    f'{len(OPERATIONALIZED_SMELL_TYPES)} unadjusted Wilcoxon '
                    'rank-sum p-values; descriptive only'
                ),
            },
            'significance_rule': (
                'primary: cluster-robust q < 0.05 and the corresponding '
                'cluster-robust 95% CI excludes the null value; bootstrap '
                'percentile intervals are reported as a sensitivity analysis'
            ),
        },
        'cluster_robust_inference': {
            'covariance': 'repository-clustered sandwich covariance',
            'small_sample_correction': 'G/(G-1) applied to the cluster meat',
            'p_value_reference': 'standard normal two-sided Wald test',
            'coefficient_tested': 'target_smell coefficient equals zero',
            'fdr_q_values': cluster_robust_endpoint_q_values,
        },
        'bootstrap_iterations': bootstrap_iterations,
        'bootstrap_seed': seed,
        'p_values': (
            'Primary regression p-values are two-sided Wald p-values based on '
            'repository-clustered sandwich standard errors and are corrected '
            'within endpoint families using Benjamini-Hochberg FDR. Bootstrap '
            'percentile intervals and bootstrap tail probabilities are retained '
            'as secondary uncertainty diagnostics. Wilcoxon p-values remain raw, '
            'unadjusted descriptive comparisons.'
        ),
        'causal_interpretation': 'not supported; estimates are adjusted observational associations',
    }
    metadata['sample_diagnostics_path'] = str(output_dir / 'rq2_sample_diagnostics.json')
    metadata['collinearity_diagnostics_path'] = str(
        output_dir / 'rq2_collinearity_diagnostics.json'
    )
    report = {'metadata': metadata, 'comparisons': comparisons}
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / 'rq2_run_level_results.json').open('w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)

    sample_report = {
        'metadata': {
            'analysis_unit': 'run with workflow-version and repository multiplicity diagnostics',
            'weighting_mode': weighting_mode,
            'rare_smell_policy': RARE_SMELL_POLICY,
        },
        'overall': metadata['sample_composition'],
        'by_smell': {
            smell_type: comparison['sample_diagnostics']
            for smell_type, comparison in comparisons.items()
        },
    }
    with (output_dir / 'rq2_sample_diagnostics.json').open(
        'w', encoding='utf-8'
    ) as handle:
        json.dump(sample_report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    with (output_dir / 'rq2_collinearity_diagnostics.json').open(
        'w', encoding='utf-8'
    ) as handle:
        json.dump(
            {
                'metadata': {
                    'analysis_unit': 'run-level regression cohort',
                    'high_correlation_threshold': 0.70,
                    'note': (
                        'Correlation, VIF and condition-number diagnostics are '
                        'reported for transparency and do not trigger post-hoc '
                        'predictor removal.'
                    ),
                },
                'by_smell': metadata['design_collinearity_diagnostics'],
                'co_exposure_overlap': metadata['co_exposure_overlap'],
            },
            handle,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )

    rows: List[Dict[str, Any]] = []
    for smell_type, comparison in comparisons.items():
        all_completed_reliability = comparison['all_completed_reliability']
        successful = comparison['successful_duration']
        all_completed = comparison['all_completed_duration']
        desc = comparison['successful_duration_descriptive']
        all_completed_desc = comparison['all_completed_duration_descriptive']
        rows.append({
            'smell_type': smell_type,
            'exposed_runs': comparison['exposed_runs'],
            'eligible_control_runs': comparison['eligible_control_runs'],
            'exposed_logical_versions': comparison['exposed_logical_versions'],
            'eligible_control_logical_versions': comparison[
                'eligible_control_logical_versions'
            ],
            'exposed_repositories': comparison['exposed_repositories'],
            'control_repositories': comparison['control_repositories'],
            'confirmatory_eligible': comparison['confirmatory_eligible'],
            'interpretation_scope': comparison['interpretation_scope'],
            'reliability_observations': comparison['reliability'].get('observations'),
            'all_completed_reliability_observations': (
                all_completed_reliability.get('observations')
            ),
            'successful_duration_observations': successful.get('observations'),
            'all_completed_duration_observations': all_completed.get('observations'),
            'reliability_difference_pp': comparison['reliability'].get('adjusted_technical_success_difference_pp'),
            'reliability_ci_low_pp': (comparison['reliability'].get('adjusted_success_difference_ci_95') or [None, None])[0],
            'reliability_ci_high_pp': (comparison['reliability'].get('adjusted_success_difference_ci_95') or [None, None])[1],
            'reliability_bootstrap_tail_p': comparison['reliability'].get('bootstrap_tail_p_value'),
            'reliability_fdr_q': comparison['reliability'].get('fdr_q_value'),
            'reliability_significant_after_fdr': comparison['reliability'].get('significant_after_fdr'),
            'reliability_cluster_robust_p': comparison['reliability'].get(
                'cluster_robust_p_value_unadjusted'
            ),
            'reliability_cluster_robust_fdr_q': comparison['reliability'].get(
                'cluster_robust_fdr_q_value'
            ),
            'reliability_cluster_robust_ci_low_pp': (
                comparison['reliability'].get(
                    'cluster_robust_success_difference_ci_95'
                ) or [None, None]
            )[0],
            'reliability_cluster_robust_ci_high_pp': (
                comparison['reliability'].get(
                    'cluster_robust_success_difference_ci_95'
                ) or [None, None]
            )[1],
            'reliability_cluster_robust_significant_after_fdr': comparison[
                'reliability'
            ].get('cluster_robust_significant_after_fdr'),
            'all_completed_reliability_difference_pp': (
                all_completed_reliability.get('adjusted_success_difference_pp')
            ),
            'all_completed_reliability_ci_low_pp': (
                all_completed_reliability.get('adjusted_success_difference_ci_95') or [None, None]
            )[0],
            'all_completed_reliability_ci_high_pp': (
                all_completed_reliability.get('adjusted_success_difference_ci_95') or [None, None]
            )[1],
            'all_completed_reliability_bootstrap_tail_p': (
                all_completed_reliability.get('bootstrap_tail_p_value')
            ),
            'all_completed_reliability_fdr_q': all_completed_reliability.get('fdr_q_value'),
            'all_completed_reliability_significant_after_fdr': (
                all_completed_reliability.get('significant_after_fdr')
            ),
            'all_completed_reliability_cluster_robust_p': (
                all_completed_reliability.get('cluster_robust_p_value_unadjusted')
            ),
            'all_completed_reliability_cluster_robust_fdr_q': (
                all_completed_reliability.get('cluster_robust_fdr_q_value')
            ),
            'all_completed_reliability_cluster_robust_significant_after_fdr': (
                all_completed_reliability.get('cluster_robust_significant_after_fdr')
            ),
            'successful_duration_ratio': successful.get('adjusted_duration_ratio'),
            'successful_duration_ci_low': (successful.get('adjusted_duration_ratio_ci_95') or [None, None])[0],
            'successful_duration_ci_high': (successful.get('adjusted_duration_ratio_ci_95') or [None, None])[1],
            'successful_duration_bootstrap_tail_p': successful.get('duration_ratio_bootstrap_tail_p_value'),
            'successful_duration_fdr_q': successful.get('fdr_q_value'),
            'successful_duration_significant_after_fdr': successful.get('significant_after_fdr'),
            'successful_duration_cluster_robust_p': successful.get(
                'cluster_robust_p_value_unadjusted'
            ),
            'successful_duration_cluster_robust_fdr_q': successful.get(
                'cluster_robust_fdr_q_value'
            ),
            'successful_duration_cluster_robust_ci_low': (
                successful.get('cluster_robust_duration_ratio_ci_95') or [None, None]
            )[0],
            'successful_duration_cluster_robust_ci_high': (
                successful.get('cluster_robust_duration_ratio_ci_95') or [None, None]
            )[1],
            'successful_duration_cluster_robust_significant_after_fdr': successful.get(
                'cluster_robust_significant_after_fdr'
            ),
            'all_completed_duration_ratio': all_completed.get('adjusted_duration_ratio'),
            'all_completed_duration_ci_low': (all_completed.get('adjusted_duration_ratio_ci_95') or [None, None])[0],
            'all_completed_duration_ci_high': (all_completed.get('adjusted_duration_ratio_ci_95') or [None, None])[1],
            'all_completed_duration_bootstrap_tail_p': all_completed.get('duration_ratio_bootstrap_tail_p_value'),
            'all_completed_duration_fdr_q': all_completed.get('fdr_q_value'),
            'all_completed_duration_significant_after_fdr': all_completed.get('significant_after_fdr'),
            'all_completed_duration_cluster_robust_p': all_completed.get(
                'cluster_robust_p_value_unadjusted'
            ),
            'all_completed_duration_cluster_robust_fdr_q': all_completed.get(
                'cluster_robust_fdr_q_value'
            ),
            'all_completed_duration_cluster_robust_ci_low': (
                all_completed.get('cluster_robust_duration_ratio_ci_95') or [None, None]
            )[0],
            'all_completed_duration_cluster_robust_ci_high': (
                all_completed.get('cluster_robust_duration_ratio_ci_95') or [None, None]
            )[1],
            'all_completed_duration_cluster_robust_significant_after_fdr': all_completed.get(
                'cluster_robust_significant_after_fdr'
            ),
            'successful_duration_unadjusted_ratio': desc.get('unadjusted_duration_ratio'),
            'successful_duration_wilcoxon_u': desc.get('wilcoxon_rank_sum_u_statistic'),
            'successful_duration_wilcoxon_p_value': desc.get('wilcoxon_rank_sum_p_value'),
            'successful_duration_wilcoxon_fdr_q': desc.get('wilcoxon_rank_sum_fdr_q_value'),
            'successful_duration_cliffs_delta': desc.get('cliffs_delta'),
            'all_completed_duration_unadjusted_ratio': all_completed_desc.get('unadjusted_duration_ratio'),
            'all_completed_duration_wilcoxon_u': all_completed_desc.get('wilcoxon_rank_sum_u_statistic'),
            'all_completed_duration_wilcoxon_p_value': all_completed_desc.get('wilcoxon_rank_sum_p_value'),
            'all_completed_duration_wilcoxon_fdr_q': all_completed_desc.get('wilcoxon_rank_sum_fdr_q_value'),
            'all_completed_duration_cliffs_delta': all_completed_desc.get('cliffs_delta'),
        })
    with (output_dir / 'rq2_run_level_results.csv').open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ['smell_type'])
        writer.writeheader()
        writer.writerows(rows)

    coefficient_rows: List[Dict[str, Any]] = []
    for smell_type, comparison in comparisons.items():
        for endpoint in ('all_completed_duration', 'reliability'):
            model = comparison.get(endpoint, {})
            for coefficient in model.get('coefficient_inference', []):
                interval = coefficient.get('cluster_robust_coefficient_ci_95') or [None, None]
                exponentiated_interval = (
                    coefficient.get('cluster_robust_exponentiated_ci_95')
                    or [None, None]
                )
                coefficient_rows.append({
                    'smell_type': smell_type,
                    'endpoint': endpoint,
                    'feature': coefficient.get('feature'),
                    'coefficient': coefficient.get('coefficient'),
                    'cluster_robust_standard_error': coefficient.get(
                        'cluster_robust_standard_error'
                    ),
                    'cluster_robust_wald_z': coefficient.get(
                        'cluster_robust_wald_z'
                    ),
                    'cluster_robust_p_value_unadjusted': coefficient.get(
                        'cluster_robust_p_value_unadjusted'
                    ),
                    'coefficient_ci_95_low': interval[0],
                    'coefficient_ci_95_high': interval[1],
                    'exponentiated_coefficient': coefficient.get(
                        'exponentiated_coefficient'
                    ),
                    'exponentiated_ci_95_low': exponentiated_interval[0],
                    'exponentiated_ci_95_high': exponentiated_interval[1],
                    'observations': model.get('observations'),
                    'repository_clusters': model.get(
                        'cluster_robust_cluster_count'
                    ),
                })
    coefficient_path = output_dir / 'rq2_full_coefficient_inference.csv'
    with coefficient_path.open('w', encoding='utf-8', newline='') as handle:
        fieldnames = list(coefficient_rows[0].keys()) if coefficient_rows else [
            'smell_type', 'endpoint', 'feature'
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(coefficient_rows)
    metadata['full_coefficient_inference_path'] = str(coefficient_path)
    metadata['control_coefficient_reporting'] = (
        'All fitted coefficients use repository-clustered sandwich standard '
        'errors. P-values for nuisance/control coefficients are descriptive and '
        'are not included in the smell-level FDR families.'
    )
    with (output_dir / 'analysis_metadata.json').open('w', encoding='utf-8') as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2, allow_nan=False)

    print(f'Run-level RQ2 results: {output_dir / "rq2_run_level_results.json"}')
    print(json.dumps({
        'analysis_unit': 'run',
        'runs': len(records),
        'repositories': len({record.repository_name for record in records}),
        'output_dir': str(output_dir),
    }, ensure_ascii=False, indent=2))
    for smell_type, comparison in comparisons.items():
        successful = comparison['successful_duration']
        print(
            f"{smell_type}: runs={comparison['exposed_runs']}/{comparison['eligible_control_runs']}, "
            f"success_pp={comparison['reliability'].get('adjusted_technical_success_difference_pp')}, "
            f"success_duration_ratio={successful.get('adjusted_duration_ratio')}, "
            f"all_duration_ratio={comparison['all_completed_duration'].get('adjusted_duration_ratio')}"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='Run run-level version-aligned RQ2 analysis')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--bootstrap', type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument(
        '--weighting',
        choices=WEIGHTING_MODES,
        default=DEFAULT_WEIGHTING_MODE,
        help=(
            'analysis weighting: version (primary), run (run-weighted sensitivity), '
            'or repository (repository-balanced sensitivity)'
        ),
    )
    parser.add_argument(
        '--core-only',
        action='store_true',
        help=(
            'bootstrap only the prespecified primary all-completed duration '
            'endpoint and secondary technical-reliability endpoint; omit '
            'secondary endpoint families and within-workflow sensitivity'
        ),
    )
    args = parser.parse_args()
    output_dir = args.output_dir or args.input_dir / 'rq2_run_level'
    analyze(
        args.input_dir,
        output_dir,
        args.bootstrap,
        args.seed,
        args.weighting,
        args.core_only,
    )


if __name__ == '__main__':
    main()
