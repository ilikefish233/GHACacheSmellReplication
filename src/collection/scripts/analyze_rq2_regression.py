"""Workflow-level regression analysis for the adjusted RQ2 experiment.

This module is deliberately separate from the descriptive cohort analysis in
``analyze_rq2_adjusted.py``.  It consumes the same cleaned workflow-level
records and smell-specific eligible cohorts, then estimates adjusted
associations rather than treating pooled runs as independent observations.

Primary models
--------------
* Reliability: grouped-binomial logistic regression using technical successes
  and technical failures for each workflow identity.
* Duration: ordinary least-squares regression of the log median duration of
  successful runs for each workflow identity.

Both models include the target smell, a small set of pre-specified workflow
covariates, and indicators for the other operationalized smells.  Repositories
are resampled as clusters for uncertainty intervals.  The analysis is
observational and does not establish causality.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

# ``python scripts/analyze_rq2_regression.py`` puts ``scripts`` rather than
# the repository root on sys.path on some Python installations.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.analyze_rq2_adjusted import (  # noqa: E402
    OPERATIONALIZED_SMELL_TYPES,
    WorkflowRecord,
    _cohort,
    analysis_unit_name,
    load_records,
)


DEFAULT_SCAN = REPOSITORY_ROOT / 'output_full_logical_20260831' / 'smell_scan_results.jsonl'
DEFAULT_DATA = REPOSITORY_ROOT / 'data_26_6_9'
DEFAULT_OUTPUT = REPOSITORY_ROOT / 'output_full_logical_20260831' / 'rq2_regression_30d'

KNOWN_EVENTS = (
    'push',
    'pull_request',
    'merge_group',
    'schedule',
    'workflow_dispatch',
    'workflow_run',
    'release',
)

EPSILON = 1e-12
RIDGE_PENALTY = 1e-6
LOGISTIC_MAX_ITERATIONS = 100
LOGISTIC_TOLERANCE = 1e-8


def _finite_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -40.0, 40.0)
    result = np.empty_like(clipped, dtype=float)
    positive = clipped >= 0
    result[positive] = 1.0 / (1.0 + np.exp(-clipped[positive]))
    negative_values = np.exp(clipped[~positive])
    result[~positive] = negative_values / (1.0 + negative_values)
    return result


def _percentile_ci(values: Sequence[float]) -> Optional[List[float]]:
    finite = np.asarray([value for value in values if math.isfinite(float(value))], dtype=float)
    if finite.size < 20:
        return None
    return [float(np.percentile(finite, 2.5)), float(np.percentile(finite, 97.5))]


def _safe_exp(value: float) -> Optional[float]:
    if not math.isfinite(value):
        return None
    return float(math.exp(float(np.clip(value, -40.0, 40.0))))


def _primary_ecosystem(record: WorkflowRecord) -> str:
    """Return a low-cardinality ecosystem category for adjustment."""

    languages = sorted(value for value in record.dependency_languages if value)
    if not languages:
        return 'none'
    if len(languages) == 1:
        return languages[0]
    return 'multi'


def _event_category(record: WorkflowRecord) -> str:
    if not record.event_counts:
        return 'unknown'
    event, _ = max(record.event_counts.items(), key=lambda item: (item[1], item[0]))
    return event if event in KNOWN_EVENTS else 'other'


def _top_levels(values: Iterable[str], max_levels: int = 6) -> List[str]:
    counts = Counter(values)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    levels = [value for value, _ in ordered[:max_levels]]
    return levels or ['unknown']


def _standardize(values: np.ndarray) -> Tuple[np.ndarray, Dict[str, float]]:
    values = np.asarray(values, dtype=float)
    mean = float(np.mean(values)) if values.size else 0.0
    std = float(np.std(values)) if values.size else 0.0
    if std <= EPSILON:
        return np.zeros_like(values), {'mean': mean, 'std': 0.0}
    return (values - mean) / std, {'mean': mean, 'std': std}


def build_design_matrix(
    records: Sequence[WorkflowRecord],
    target_smell: str,
) -> Tuple[np.ndarray, List[str], int, Dict[str, Any]]:
    """Build the fixed design matrix used by both model fits and bootstrap.

    The matrix is intentionally small and interpretable.  Categorical levels
    are determined within the target smell's eligible cohort; the most common
    level is the reference category.  Constant columns are removed, while the
    intercept and target exposure are retained.
    """

    if not records:
        raise ValueError('cannot build a design matrix for an empty cohort')

    ecosystems = [_primary_ecosystem(record) for record in records]
    events = [_event_category(record) for record in records]
    ecosystem_levels = _top_levels(ecosystems)
    event_levels = _top_levels(events)
    ecosystem_reference = ecosystem_levels[0]
    event_reference = event_levels[0]

    numeric_job, job_stats = _standardize(
        np.log1p(np.asarray([max(record.job_count, 0) for record in records], dtype=float))
    )
    numeric_cache, cache_stats = _standardize(
        np.log1p(np.asarray([max(record.cache_job_count, 0) for record in records], dtype=float))
    )

    columns: List[np.ndarray] = [np.ones(len(records), dtype=float)]
    feature_names = ['intercept']
    exposure = np.asarray([target_smell in record.labels for record in records], dtype=bool)
    columns.append(exposure.astype(float))
    feature_names.append('target_smell')

    columns.append(numeric_job)
    feature_names.append('log_job_count_z')
    columns.append(numeric_cache)
    feature_names.append('log_cache_job_count_z')

    for level in ecosystem_levels[1:]:
        columns.append(np.asarray([value == level for value in ecosystems], dtype=float))
        feature_names.append(f'ecosystem={level}')
    for level in event_levels[1:]:
        columns.append(np.asarray([value == level for value in events], dtype=float))
        feature_names.append(f'event={level}')

    for smell_type in OPERATIONALIZED_SMELL_TYPES:
        if smell_type == target_smell:
            continue
        columns.append(
            np.asarray([smell_type in record.labels for record in records], dtype=float)
        )
        feature_names.append(f'co_smell={smell_type}')

    raw_matrix = np.column_stack(columns).astype(float)
    keep = [0, 1]
    for index in range(2, raw_matrix.shape[1]):
        if float(np.ptp(raw_matrix[:, index])) > EPSILON:
            keep.append(index)
    matrix = raw_matrix[:, keep]
    feature_names = [feature_names[index] for index in keep]
    exposure_index = feature_names.index('target_smell')

    metadata = {
        'categorical_reference': {
            'ecosystem': ecosystem_reference,
            'event': event_reference,
        },
        'ecosystem_levels': ecosystem_levels,
        'event_levels': event_levels,
        'standardization': {
            'log_job_count_z': job_stats,
            'log_cache_job_count_z': cache_stats,
        },
        'constant_columns_removed': [
            name for index, name in enumerate(
                ['intercept', 'target_smell', 'log_job_count_z', 'log_cache_job_count_z']
                + [f'ecosystem={level}' for level in ecosystem_levels[1:]]
                + [f'event={level}' for level in event_levels[1:]]
                + [
                    f'co_smell={smell_type}'
                    for smell_type in OPERATIONALIZED_SMELL_TYPES
                    if smell_type != target_smell
                ]
            )
            if index not in keep
        ],
    }
    return matrix, feature_names, exposure_index, metadata


def _penalty_matrix(column_count: int) -> np.ndarray:
    penalty = np.eye(column_count, dtype=float)
    if column_count:
        penalty[0, 0] = 0.0  # do not penalize the intercept
    return penalty


def _binomial_log_likelihood(
    beta: np.ndarray,
    matrix: np.ndarray,
    successes: np.ndarray,
    trials: np.ndarray,
    penalty: np.ndarray,
) -> float:
    probabilities = np.clip(_sigmoid(matrix @ beta), 1e-15, 1.0 - 1e-15)
    failures = trials - successes
    value = np.sum(successes * np.log(probabilities) + failures * np.log1p(-probabilities))
    value -= 0.5 * RIDGE_PENALTY * float(beta @ penalty @ beta)
    return float(value)


def fit_grouped_binomial_logistic(
    matrix: np.ndarray,
    successes: np.ndarray,
    trials: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Fit grouped-binomial logistic regression with a tiny ridge stabilizer."""

    successes = np.asarray(successes, dtype=float)
    trials = np.asarray(trials, dtype=float)
    if weights is not None:
        weights = np.asarray(weights, dtype=float)
        successes = successes * weights
        trials = trials * weights
    if matrix.shape[0] == 0 or np.any(trials < 0) or np.any(successes < 0) or np.any(successes > trials):
        raise ValueError('invalid grouped-binomial observations')
    if float(np.sum(trials)) <= 0:
        raise ValueError('no technical outcomes available')

    beta = np.zeros(matrix.shape[1], dtype=float)
    penalty = _penalty_matrix(matrix.shape[1])
    converged = False
    iteration_count = 0
    for iteration in range(1, LOGISTIC_MAX_ITERATIONS + 1):
        iteration_count = iteration
        probabilities = _sigmoid(matrix @ beta)
        residual = successes - trials * probabilities
        gradient = matrix.T @ residual - RIDGE_PENALTY * (penalty @ beta)
        variance = np.maximum(trials * probabilities * (1.0 - probabilities), 1e-12)
        hessian = matrix.T @ (matrix * variance[:, None]) + RIDGE_PENALTY * penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]

        if float(np.linalg.norm(step, ord=np.inf)) < LOGISTIC_TOLERANCE:
            converged = True
            break

        current_ll = _binomial_log_likelihood(beta, matrix, successes, trials, penalty)
        step_scale = 1.0
        while step_scale >= 1e-7:
            candidate = beta + step_scale * step
            candidate_ll = _binomial_log_likelihood(
                candidate, matrix, successes, trials, penalty
            )
            if candidate_ll >= current_ll:
                beta = candidate
                break
            step_scale *= 0.5
        else:
            break

        if float(np.linalg.norm(step_scale * step, ord=np.inf)) < LOGISTIC_TOLERANCE:
            converged = True
            break

    return {
        'coefficients': beta,
        'converged': converged,
        'iterations': iteration_count,
        'total_trials': float(np.sum(trials)),
    }


def fit_firth_logistic(
    matrix: np.ndarray,
    successes: np.ndarray,
    trials: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Fit a bias-reduced binomial logistic model using Firth's correction.

    This fit is used as a rare-exposure sensitivity analysis.  It is not the
    primary clustered model: the primary analysis continues to use the
    version-balanced ridge logistic fit and repository bootstrap.  Firth's
    Jeffreys-prior correction is useful here because a rare smell can produce
    quasi-complete separation, which otherwise yields unstable or extreme
    odds ratios.
    """

    successes = np.asarray(successes, dtype=float)
    trials = np.asarray(trials, dtype=float)
    if weights is None:
        weights = np.ones(matrix.shape[0], dtype=float)
    else:
        weights = np.asarray(weights, dtype=float)
    effective_successes = successes * weights
    effective_trials = trials * weights
    if (
        matrix.shape[0] == 0
        or matrix.ndim != 2
        or matrix.shape[0] != successes.size
        or matrix.shape[0] != trials.size
        or matrix.shape[0] != weights.size
        or np.any(effective_trials <= 0)
        or np.any(effective_successes < 0)
        or np.any(effective_successes > effective_trials)
    ):
        raise ValueError('invalid binomial observations for Firth logistic regression')

    beta = np.zeros(matrix.shape[1], dtype=float)
    converged = False
    iteration_count = 0

    def objective(values: np.ndarray) -> float:
        probabilities = np.clip(_sigmoid(matrix @ values), 1e-15, 1.0 - 1e-15)
        log_likelihood = float(np.sum(
            effective_successes * np.log(probabilities)
            + (effective_trials - effective_successes) * np.log1p(-probabilities)
        ))
        variance = effective_trials * probabilities * (1.0 - probabilities)
        information = matrix.T @ (matrix * variance[:, None])
        sign, logdet = np.linalg.slogdet(information)
        if sign <= 0 or not math.isfinite(float(logdet)):
            return -float('inf')
        return log_likelihood + 0.5 * float(logdet)

    for iteration in range(1, LOGISTIC_MAX_ITERATIONS + 1):
        iteration_count = iteration
        probabilities = _sigmoid(matrix @ beta)
        variance = np.maximum(
            effective_trials * probabilities * (1.0 - probabilities),
            1e-12,
        )
        information = matrix.T @ (matrix * variance[:, None])
        try:
            inverse_information = np.linalg.inv(information)
        except np.linalg.LinAlgError:
            inverse_information = np.linalg.pinv(information, rcond=1e-12)
        leverage = variance * np.einsum(
            'ij,jk,ik->i', matrix, inverse_information, matrix
        )
        score = matrix.T @ (
            effective_successes - effective_trials * probabilities
            + leverage * (0.5 - probabilities)
        )
        try:
            step = inverse_information @ score
        except (FloatingPointError, ValueError):
            break
        if float(np.linalg.norm(step, ord=np.inf)) < LOGISTIC_TOLERANCE:
            converged = True
            break

        current_objective = objective(beta)
        step_scale = 1.0
        while step_scale >= 1e-7:
            candidate = beta + step_scale * step
            candidate_objective = objective(candidate)
            if candidate_objective >= current_objective:
                beta = candidate
                break
            step_scale *= 0.5
        else:
            break
        if float(np.linalg.norm(step_scale * step, ord=np.inf)) < LOGISTIC_TOLERANCE:
            converged = True
            break

    probabilities = np.clip(_sigmoid(matrix @ beta), 1e-15, 1.0 - 1e-15)
    variance = np.maximum(
        effective_trials * probabilities * (1.0 - probabilities),
        1e-12,
    )
    information = matrix.T @ (matrix * variance[:, None])
    try:
        covariance = np.linalg.inv(information)
    except np.linalg.LinAlgError:
        covariance = np.linalg.pinv(information, rcond=1e-12)
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    return {
        'coefficients': beta,
        'standard_errors': standard_errors,
        'covariance': covariance,
        'converged': converged,
        'iterations': iteration_count,
        'total_trials': float(np.sum(effective_trials)),
        'firth_objective': objective(beta),
    }


def fit_ridge_log_duration(
    matrix: np.ndarray,
    log_durations: np.ndarray,
    weights: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Fit a workflow-level log-duration model with no intercept penalty."""

    values = np.asarray(log_durations, dtype=float)
    if matrix.shape[0] == 0 or values.size != matrix.shape[0] or not np.all(np.isfinite(values)):
        raise ValueError('invalid log-duration observations')
    if weights is None:
        weights = np.ones(matrix.shape[0], dtype=float)
    else:
        weights = np.asarray(weights, dtype=float)
    weighted_matrix = matrix * np.sqrt(weights)[:, None]
    weighted_values = values * np.sqrt(weights)
    penalty = _penalty_matrix(matrix.shape[1])
    normal = weighted_matrix.T @ weighted_matrix + RIDGE_PENALTY * penalty
    target = weighted_matrix.T @ weighted_values
    try:
        beta = np.linalg.solve(normal, target)
    except np.linalg.LinAlgError:
        beta = np.linalg.lstsq(normal, target, rcond=None)[0]
    residuals = values - matrix @ beta
    return {
        'coefficients': beta,
        'rmse_log_seconds': float(np.sqrt(np.average(residuals ** 2, weights=weights))),
    }


def _prediction_difference(
    matrix: np.ndarray,
    exposure_index: int,
    beta: np.ndarray,
    logistic: bool,
) -> float:
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
    return float(np.mean(exposed_values - baseline_values))


def _cohort_arrays(
    records: Sequence[WorkflowRecord],
    target_smell: str,
    outcome: str,
) -> Dict[str, Any]:
    if outcome == 'reliability':
        usable = [record for record in records if record.technical_total_count > 0]
    elif outcome == 'duration':
        usable = [
            record
            for record in records
            if record.successful_duration_median is not None
            and record.successful_duration_median > 0
        ]
    else:
        raise ValueError(f'unknown outcome: {outcome}')
    if not usable:
        raise ValueError(f'no usable workflow identities for {outcome}')

    matrix, feature_names, exposure_index, design_metadata = build_design_matrix(
        usable, target_smell
    )
    if np.unique(matrix[:, exposure_index]).size < 2:
        raise ValueError('target smell exposure has no variation')
    result: Dict[str, Any] = {
        'records': usable,
        'matrix': matrix,
        'feature_names': feature_names,
        'exposure_index': exposure_index,
        'design_metadata': design_metadata,
        'repositories': np.asarray([record.repository_name for record in usable], dtype=object),
    }
    if outcome == 'reliability':
        result['successes'] = np.asarray(
            [record.technical_success_count for record in usable], dtype=float
        )
        result['trials'] = np.asarray(
            [record.technical_total_count for record in usable], dtype=float
        )
    else:
        result['log_durations'] = np.log(
            np.asarray([record.successful_duration_median for record in usable], dtype=float)
        )
    return result


def _cluster_weights(repositories: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    unique_repositories, repository_index = np.unique(repositories, return_inverse=True)
    sampled = rng.integers(0, len(unique_repositories), size=len(unique_repositories))
    multiplicities = np.bincount(sampled, minlength=len(unique_repositories)).astype(float)
    return multiplicities[repository_index]


def _bootstrap_model(
    arrays: Mapping[str, Any],
    outcome: str,
    iterations: int,
    seed: int,
) -> Dict[str, Any]:
    matrix = arrays['matrix']
    exposure_index = arrays['exposure_index']
    repositories = arrays['repositories']
    rng = np.random.default_rng(seed)
    outputs: Dict[str, List[float]] = {
        'target_coefficient': [],
        'target_effect': [],
    }
    if outcome == 'reliability':
        outputs['target_odds_ratio'] = []
        successes = arrays['successes']
        trials = arrays['trials']
    else:
        outputs['target_duration_ratio'] = []
        log_durations = arrays['log_durations']

    for _ in range(max(0, int(iterations))):
        weights = _cluster_weights(repositories, rng)
        if np.count_nonzero(weights) == 0:
            continue
        active = weights > 0
        if np.unique(matrix[active, exposure_index]).size < 2:
            continue
        try:
            if outcome == 'reliability':
                fit = fit_grouped_binomial_logistic(matrix, successes, trials, weights)
                beta = fit['coefficients']
                effect = _prediction_difference(matrix, exposure_index, beta, logistic=True)
                ratio = _safe_exp(float(beta[exposure_index]))
                if ratio is None or not math.isfinite(effect):
                    continue
                outputs['target_odds_ratio'].append(ratio)
            else:
                fit = fit_ridge_log_duration(matrix, log_durations, weights)
                beta = fit['coefficients']
                effect = _prediction_difference(matrix, exposure_index, beta, logistic=False)
                ratio = _safe_exp(float(beta[exposure_index]))
                if ratio is None or not math.isfinite(effect):
                    continue
                outputs['target_duration_ratio'].append(ratio)
            outputs['target_coefficient'].append(float(beta[exposure_index]))
            outputs['target_effect'].append(float(effect))
        except (FloatingPointError, ValueError, np.linalg.LinAlgError):
            continue
    return {
        'valid_replicates': len(outputs['target_coefficient']),
        'iterations_requested': max(0, int(iterations)),
        'target_coefficient_ci_95': _percentile_ci(outputs['target_coefficient']),
        'target_effect_ci_95': _percentile_ci(outputs['target_effect']),
        **(
            {'target_odds_ratio_ci_95': _percentile_ci(outputs['target_odds_ratio'])}
            if outcome == 'reliability'
            else {'target_duration_ratio_ci_95': _percentile_ci(outputs['target_duration_ratio'])}
        ),
    }


def _model_details(
    arrays: Mapping[str, Any],
    fit: Mapping[str, Any],
    outcome: str,
    bootstrap: Mapping[str, Any],
) -> Dict[str, Any]:
    coefficients = fit['coefficients']
    exposure_index = arrays['exposure_index']
    beta = float(coefficients[exposure_index])
    matrix = arrays['matrix']
    if outcome == 'reliability':
        effect = _prediction_difference(matrix, exposure_index, coefficients, logistic=True)
        return {
            'model': 'grouped-binomial logistic regression',
            'formula': 'technical_successes / technical_failures ~ target_smell + covariates',
            'analysis_unit': analysis_unit_name(arrays['records']),
            'observations': int(matrix.shape[0]),
            # Retain the historical field name for downstream readers.
            'workflow_identities': int(matrix.shape[0]),
            'technical_trials': float(np.sum(arrays['trials'])),
            'technical_successes': float(np.sum(arrays['successes'])),
            'technical_failures': float(np.sum(arrays['trials'] - arrays['successes'])),
            'adjusted_odds_ratio': _safe_exp(beta),
            'adjusted_technical_success_difference': effect,
            'adjusted_technical_success_difference_pp': effect * 100.0,
            'adjusted_odds_ratio_ci_95': bootstrap.get('target_odds_ratio_ci_95'),
            'adjusted_success_difference_ci_95': bootstrap.get('target_effect_ci_95'),
            'coefficient': beta,
            'coefficient_ci_95': bootstrap.get('target_coefficient_ci_95'),
            'converged': bool(fit.get('converged')),
            'iterations': fit.get('iterations'),
        }

    effect = _prediction_difference(matrix, exposure_index, coefficients, logistic=False)
    return {
        'model': 'linear regression on log successful duration',
        'formula': 'log(successful_duration_median_seconds) ~ target_smell + covariates',
        'analysis_unit': analysis_unit_name(arrays['records']),
        'observations': int(matrix.shape[0]),
        # Retain the historical field name for downstream readers.
        'workflow_identities': int(matrix.shape[0]),
        'adjusted_duration_ratio': _safe_exp(beta),
        'adjusted_log_duration_difference': effect,
        'adjusted_duration_ratio_ci_95': bootstrap.get('target_duration_ratio_ci_95'),
        'coefficient': beta,
        'coefficient_ci_95': bootstrap.get('target_coefficient_ci_95'),
        'rmse_log_seconds': fit.get('rmse_log_seconds'),
    }


def _fit_outcome(
    records: Sequence[WorkflowRecord],
    target_smell: str,
    outcome: str,
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    try:
        arrays = _cohort_arrays(records, target_smell, outcome)
        if outcome == 'reliability':
            fit = fit_grouped_binomial_logistic(
                arrays['matrix'], arrays['successes'], arrays['trials']
            )
        else:
            fit = fit_ridge_log_duration(arrays['matrix'], arrays['log_durations'])
        bootstrap = _bootstrap_model(arrays, outcome, bootstrap_iterations, seed)
        details = _model_details(arrays, fit, outcome, bootstrap)
        details['feature_names'] = arrays['feature_names']
        details['design'] = arrays['design_metadata']
        details['bootstrap'] = bootstrap
        return details
    except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
        return {
            'status': 'not_estimable',
            'reason': str(exc),
            'analysis_unit': analysis_unit_name(records),
            'observations': 0,
            'workflow_identities': 0,
        }


def analyze_smell(
    records: Sequence[WorkflowRecord],
    smell_type: str,
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    exposed, controls, ambiguous = _cohort(records, smell_type)
    cohort = exposed + controls
    return {
        'smell_type': smell_type,
        'analysis_unit': analysis_unit_name(records),
        'cluster_unit': 'repository',
        'exposed_workflows': len(exposed),
        'eligible_control_workflows': len(controls),
        'excluded_ambiguous_controls': ambiguous,
        'reliability': _fit_outcome(
            cohort, smell_type, 'reliability', bootstrap_iterations, seed
        ),
        'duration': _fit_outcome(
            cohort, smell_type, 'duration', bootstrap_iterations, seed + 100_000
        ),
        'control_definition': (
            'detector-eligible workflows without the target smell; ambiguous '
            'workflows are excluded'
        ),
    }


def build_report(
    records: Sequence[WorkflowRecord],
    load_metadata: Mapping[str, Any],
    bootstrap_iterations: int,
    seed: int,
) -> Dict[str, Any]:
    comparisons = {
        smell_type: analyze_smell(records, smell_type, bootstrap_iterations, seed + index)
        for index, smell_type in enumerate(OPERATIONALIZED_SMELL_TYPES)
    }
    return {
        'metadata': {
            **dict(load_metadata),
            'analysis': 'adjusted workflow-level regression',
            'operationalized_smell_types': list(OPERATIONALIZED_SMELL_TYPES),
            'reliability_endpoint': (
                'grouped-binomial technical success probability; success versus '
                'failure/startup_failure/timed_out'
            ),
            'duration_endpoint': (
                'log of the per-workflow median duration among technically successful runs'
            ),
            'covariates': [
                'target smell exposure',
                'primary dependency ecosystem',
                'dominant triggering event',
                'log job count',
                'log cache-job count',
                'indicators for other operationalized smells',
            ],
            'repository_cluster_bootstrap': True,
            'bootstrap_iterations': int(bootstrap_iterations),
            'bootstrap_seed': int(seed),
            'ridge_penalty': RIDGE_PENALTY,
            'p_values': 'not computed; effect sizes and repository-clustered bootstrap intervals are primary',
            'causal_interpretation': 'not supported; estimates are adjusted observational associations',
        },
        'comparisons': comparisons,
    }


def _ci_values(value: Any) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(value, list) or len(value) != 2:
        return None, None
    return _finite_float(value[0]), _finite_float(value[1])


def report_rows(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for smell_type, comparison in (report.get('comparisons') or {}).items():
        reliability = comparison.get('reliability') or {}
        duration = comparison.get('duration') or {}
        reliability_ci_low, reliability_ci_high = _ci_values(
            reliability.get('adjusted_success_difference_ci_95')
        )
        or_ci_low, or_ci_high = _ci_values(reliability.get('adjusted_odds_ratio_ci_95'))
        duration_ci_low, duration_ci_high = _ci_values(
            duration.get('adjusted_duration_ratio_ci_95')
        )
        rows.append(
            {
                'smell_type': smell_type,
                'analysis_unit': comparison.get('analysis_unit'),
                'exposed_workflows': comparison.get('exposed_workflows'),
                'eligible_control_workflows': comparison.get('eligible_control_workflows'),
                'excluded_ambiguous_controls': comparison.get('excluded_ambiguous_controls'),
                'reliability_workflows': reliability.get('workflow_identities'),
                'technical_trials': reliability.get('technical_trials'),
                'adjusted_success_difference_pp': (
                    reliability.get('adjusted_technical_success_difference_pp')
                ),
                'adjusted_success_difference_ci_low_pp': (
                    reliability_ci_low * 100.0 if reliability_ci_low is not None else None
                ),
                'adjusted_success_difference_ci_high_pp': (
                    reliability_ci_high * 100.0 if reliability_ci_high is not None else None
                ),
                'adjusted_odds_ratio': reliability.get('adjusted_odds_ratio'),
                'adjusted_odds_ratio_ci_low': or_ci_low,
                'adjusted_odds_ratio_ci_high': or_ci_high,
                'duration_workflows': duration.get('workflow_identities'),
                'adjusted_duration_ratio': duration.get('adjusted_duration_ratio'),
                'adjusted_duration_ratio_ci_low': duration_ci_low,
                'adjusted_duration_ratio_ci_high': duration_ci_high,
            }
        )
    return rows


def save_report(report: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / 'rq2_regression_results.json'
    csv_path = output_dir / 'rq2_regression_results.csv'
    with json_path.open('w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    rows = report_rows(report)
    with csv_path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()) if rows else ['smell_type'],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f'JSON: {json_path}')
    print(f'CSV:  {csv_path}')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Run workflow-level adjusted regression models for RQ2'
    )
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--smell-scan', type=Path, default=DEFAULT_SCAN)
    parser.add_argument('--runs-path', type=Path, default=None,
                        help='primary workflow-runs JSONL; defaults to <data-dir>/workflow_runs.jsonl')
    parser.add_argument('--additional-runs-path', type=Path, action='append', default=[],
                        help='additional workflow-runs JSONL files to merge before deduplication')
    parser.add_argument('--workflow-files', type=Path, default=None)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        '--snapshot-window-days',
        type=float,
        default=30.0,
        help='symmetric created_at-to-fetched_at window; use -1 for all time',
    )
    parser.add_argument('--min-run-created-at', type=str, default=None,
                        help='optional inclusive UTC lower bound for the run study window')
    parser.add_argument('--max-run-created-at', type=str, default=None,
                        help='optional inclusive UTC upper bound for the run study window')
    parser.add_argument('--event-scope', choices=['all', 'core_ci'], default='all')
    parser.add_argument('--default-branch-only', action='store_true',
                        help='retain only completed runs whose head_branch equals the repository default branch')
    parser.add_argument(
        '--bootstrap',
        type=int,
        default=500,
        help='repository-cluster bootstrap replicates for regression intervals',
    )
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    workflow_files = args.workflow_files
    if workflow_files is None:
        candidate = args.data_dir / 'workflow_files.jsonl'
        workflow_files = candidate if candidate.exists() else None
    snapshot_window = None if args.snapshot_window_days < 0 else args.snapshot_window_days
    runs_path = args.runs_path or (args.data_dir / 'workflow_runs.jsonl')
    default_branches_path = args.data_dir / 'repo_info.jsonl' if args.default_branch_only else None
    from scripts.analyze_rq2_adjusted import parse_timestamp
    min_run_created_at = parse_timestamp(args.min_run_created_at) if args.min_run_created_at else None
    max_run_created_at = parse_timestamp(args.max_run_created_at) if args.max_run_created_at else None
    if args.min_run_created_at and min_run_created_at is None:
        raise SystemExit('--min-run-created-at must be an ISO-8601 timestamp')
    if args.max_run_created_at and max_run_created_at is None:
        raise SystemExit('--max-run-created-at must be an ISO-8601 timestamp')
    if min_run_created_at is not None and max_run_created_at is not None:
        if min_run_created_at > max_run_created_at:
            raise SystemExit('--min-run-created-at must not be later than --max-run-created-at')

    print('Adjusted RQ2 regression analysis')
    print(f'Scan: {args.smell_scan}')
    print(f'Runs: {runs_path}')
    if args.additional_runs_path:
        print(f'Additional runs: {args.additional_runs_path}')
    print(
        f'Snapshot window: {snapshot_window} days; event scope: {args.event_scope}; '
        f'default branch only: {args.default_branch_only}; '
        f'run window: {min_run_created_at} to {max_run_created_at}'
    )
    records, metadata = load_records(
        args.smell_scan,
        runs_path,
        workflow_files,
        snapshot_window,
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
    for smell_type, comparison in report['comparisons'].items():
        reliability = comparison['reliability']
        duration = comparison['duration']
        print(
            f"  {smell_type}: exposed={comparison['exposed_workflows']}, "
            f"control={comparison['eligible_control_workflows']}, "
            f"reliability={reliability.get('adjusted_technical_success_difference_pp')}, "
            f"duration_ratio={duration.get('adjusted_duration_ratio')}"
        )


if __name__ == '__main__':
    main()
