"""EXPERIMENTAL unified success LPM; preserves the previous logistic experiment."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np
from scipy.stats import t

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LEGACY = HERE.parent / 'rq2_unified_slim_20261018'
sys.path.insert(0, str(LEGACY))
spec = importlib.util.spec_from_file_location('rq2_previous_unified', LEGACY/'run.py')
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
from engine import fit, covariance, bh

TYPES = previous.TYPES


def inference(names, beta, cov, clusters, unidentified=()):
    """CR1-G sandwich with two-sided t(G-1) inference; no bootstrap."""
    if clusters < 2:
        raise ValueError('At least two independent repository clusters are required')
    critical = float(t.ppf(.975, clusters - 1))
    result = []
    for i, name in enumerate(names):
        variance = float(cov[i, i])
        if variance < -1e-12 or not np.isfinite(variance):
            raise ValueError(f'Invalid variance for {name}: {variance}')
        se = float(np.sqrt(max(0., variance)))
        identifiable = name not in unidentified
        p = float(2*t.sf(abs(beta[i]/se), clusters-1)) if se > 0 and identifiable else None
        underflow = p == 0. if p is not None else False
        if underflow:
            # A positive upper bound is conservative for BH and avoids reporting p=0.
            p = float(np.finfo(float).tiny)
        ci = [float(beta[i]-critical*se), float(beta[i]+critical*se)]
        result.append(dict(feature=name, coefficient=float(beta[i]), standard_error=se,
            coefficient_ci_95=ci, probability_difference_pp=float(100*beta[i]),
            probability_difference_pp_ci_95=[100*v for v in ci], cluster_robust_p=p,
            p_is_numerical_upper_bound=underflow,
            inference_status='repository_cluster_robust_t' if identifiable else 'not_identifiable'))
    q = bh({r['feature']:r['cluster_robust_p'] for r in result if r['feature'] in TYPES})
    for row in result:
        row['fdr_q'] = q.get(row['feature'])
        row['significant_fdr_05'] = row['fdr_q'] is not None and row['fdr_q'] < .05
    return result


def analyze(versions, keys, values):
    count, total = values[:, 0], values[:, 1]
    active = count > 0
    keys = [key for key, yes in zip(keys, active) if yes]
    count, total = count[active], total[active]
    if np.any(total < 0) or np.any(total > count):
        raise ValueError('Invalid binary outcome sufficient statistics')
    version_n = Counter()
    for key, n in zip(keys, count):
        version_n[key[0]] += n
    weight = np.array([1./version_n[key[0]] for key in keys])
    repositories = np.array([versions[key[0]]['repository_id'] for key in keys])
    repo_names, repo_codes = np.unique(repositories, return_inverse=True)
    x, names = previous.design(versions, keys)
    x, names, rank = previous.independent_columns(x, names)
    # For a binary outcome y^2=y. Grouping retains exact run-level WLS and cluster scores.
    beta, bread, converged, iterations = fit(x, count, total, total, weight, logistic=False)
    cov = covariance(x, count, total, weight, repo_codes, beta, bread, logistic=False)
    coefficients = inference(names, beta, cov, len(repo_names), rank['unidentified_smells'])
    for name in TYPES:
        if name not in names:
            coefficients.append(dict(feature=name, coefficient=None, standard_error=None,
                coefficient_ci_95=None, probability_difference_pp=None,
                probability_difference_pp_ci_95=None, cluster_robust_p=None,
                p_is_numerical_upper_bound=False, inference_status='not_identifiable',
                fdr_q=None, significant_fdr_05=False))
    fitted = x @ beta
    mean = float(np.sum(weight*total)/np.sum(weight*count))
    sse = float(np.sum(weight*(total-2*fitted*total+count*fitted**2)))
    sst = float(np.sum(weight*(total-2*mean*total+count*mean**2)))
    outside = (fitted < 0) | (fitted > 1)
    exposure = {}
    for name in TYPES:
        selected = np.array([name in versions[key[0]]['labels'] for key in keys])
        exposed_versions = {key[0] for key, yes in zip(keys, selected) if yes}
        exposure[name] = dict(runs=int(np.sum(count[selected])), successes=int(np.sum(total[selected])),
            workflow_versions=len(exposed_versions), repositories=len(set(repositories[selected])),
            all_failed=bool(np.any(selected) and np.sum(total[selected]) == 0))
        for row in coefficients:
            if row['feature'] == name:
                row.update(exposed_runs=exposure[name]['runs'],
                    exposed_workflow_versions=exposure[name]['workflow_versions'],
                    exposed_repositories=exposure[name]['repositories'])
    result = dict(experimental=True, endpoint='reliability', model='weighted_linear_probability_model',
        status='ok_with_nonestimable_smells' if rank['unidentified_smells'] else 'ok',
        outcome='success=1; failure/startup_failure/timed_out=0; other conclusions excluded',
        runs=int(np.sum(count)), successes=int(np.sum(total)), workflow_versions=len(version_n),
        repositories=len(repo_names), design_groups=len(keys), coefficients=coefficients,
        weighting='Each workflow version has total weight 1 within the success endpoint',
        inference=dict(covariance='repository-cluster sandwich, G/(G-1) correction',
            test='two-sided t test', degrees_of_freedom=len(repo_names)-1,
            confidence_interval='pointwise 95%, t(G-1); not simultaneous FDR intervals',
            multiple_testing='BH across identifiable smell coefficients only; q<0.05',
            tested_smells=sum(r['feature'] in TYPES and r['cluster_robust_p'] is not None for r in coefficients),
            numerical_underflow='Underflowed p values are conservatively bounded above by numpy.finfo(float).tiny', bootstrap=0),
        rank_audit=rank, fit_iterations=iterations, weighted_r_squared=1-sse/sst if sst>0 else None,
        exposure_audit=exposure,
        predictions=dict(minimum=float(fitted.min()), maximum=float(fitted.max()),
            outside_0_1_runs=int(np.sum(count[outside])),
            outside_0_1_run_fraction=float(np.sum(count[outside])/np.sum(count)),
            outside_0_1_version_balanced_fraction=float(np.sum(count[outside]*weight[outside])/np.sum(count*weight))),
        interpretation='Adjusted linear associations, not causal effects. Coefficients are probability differences, not odds ratios.',
        limitations=['Linear additive probability specification; predictions are not clipped.',
            'Conventional cluster inference may be fragile for smells present in few repositories; report exposure cluster counts.'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-dir', type=Path, default=ROOT/'output_experimental_rq2_unified_slim_20261018_results')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    source, out = args.prepared_dir.resolve(), args.output_dir.resolve()
    if out.exists():
        raise FileExistsError(f'Refusing to overwrite {out}')
    out.mkdir(parents=True)
    start = time.perf_counter()
    source_manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    manifest = dict(experimental=True, status='running', started_at=datetime.now(timezone.utc).isoformat(),
        prepared_source=str(source), bootstrap=0, model='unified LPM for success only',
        input_sha256={}, prepared_sha256={}, code_sha256={}, original_code_verified=False)
    previous.dump(out/'manifest.json', manifest)
    try:
        if source_manifest['status'] != 'complete':
            raise ValueError('Prepared source is not complete')
        # Verify exact preparation/design/engine code before reusing sufficient statistics.
        for name, expected in source_manifest['code_sha256'].items():
            actual = previous.digest(LEGACY/name)
            if actual != expected:
                raise ValueError(f'Previous experiment code changed: {name}')
            manifest['code_sha256'][str((LEGACY/name).relative_to(ROOT))] = actual
        manifest['original_code_verified'] = True
        for name, expected in source_manifest['input_sha256'].items():
            actual = previous.digest(Path(source_manifest['input_dir'])/name)
            if actual != expected:
                raise ValueError(f'Original input changed: {name}')
            manifest['input_sha256'][name] = actual
        # Keep provenance of the cached data and previous endpoint output; never modify them.
        for name in ['manifest.json', 'workflow_features.jsonl', 'group_keys.jsonl',
                     'grouped_outcomes.npz', 'reliability.json', 'all_completed_duration.json',
                     'successful_duration.json']:
            manifest['prepared_sha256'][name] = previous.digest(source/name)
        for path in HERE.glob('*.py'):
            manifest['code_sha256'][str(path.relative_to(ROOT))] = previous.digest(path)
        versions = {r['version_id']:r for r in previous.rows(source/'workflow_features.jsonl')}
        keys = [tuple(r) for r in previous.rows(source/'group_keys.jsonl')]
        with np.load(source/'grouped_outcomes.npz') as data:
            result = analyze(versions, keys, data['values'])
        old = json.loads((source/'reliability.json').read_text(encoding='utf-8'))
        for name in ['runs', 'workflow_versions', 'repositories', 'design_groups']:
            if old[name] != result[name]:
                raise ValueError(f'Sample changed for {name}: {old[name]} vs {result[name]}')
        previous.dump(out/'reliability_lpm.json', result)
        rows = [r for r in result['coefficients'] if r['feature'] in TYPES]
        with (out/'smell_coefficients.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        summary = ['# Experimental unified success LPM (2026-10-19)', '',
            f"Runs: {result['runs']:,}; workflow versions: {result['workflow_versions']:,}; repositories: {result['repositories']:,}.", '',
            'All 11 smell indicators are considered jointly; non-identifiable indicators are explicitly marked.',
            'Same endpoint sample, slim controls and version-balanced weights as the previous experiment.',
            'Repository-cluster sandwich with G/(G-1) correction; two-sided t(G-1) tests; pointwise 95% intervals.',
            f"BH-FDR over {result['inference']['tested_smells']} estimable smell hypotheses; significance q<0.05; bootstrap=0.", '',
            '| Smell | Difference (pp) | 95% CI (pp) | p | q | Exposed repositories |',
            '|---|---:|---|---:|---:|---:|']
        for row in rows:
            if row['coefficient'] is None:
                summary.append(f"| {row['feature']} | Not estimable | N/A | N/A | N/A | {row['exposed_repositories']} |")
                continue
            lo, hi = row['probability_difference_pp_ci_95']
            ptext = '<2.23e-308' if row['p_is_numerical_upper_bound'] else f"{row['cluster_robust_p']:.6g}"
            summary.append(f"| {row['feature']} | {row['probability_difference_pp']:.3f} | [{lo:.3f}, {hi:.3f}] | {ptext} | {row['fdr_q']:.6g} | {row['exposed_repositories']} |")
        summary.extend(['', '## Diagnostics and interpretation', '',
            f"Unclipped predictions outside [0,1]: {result['predictions']['outside_0_1_run_fraction']:.2%} of runs; "
            f"{result['predictions']['outside_0_1_version_balanced_fraction']:.2%} under the analysis weights.",
            f"Weighted R-squared: {result['weighted_r_squared']:.4f}.",
            'The model estimates adjusted linear associations, not causal effects or odds ratios.',
            'unreachable_cache_write has no exposed technical-outcome runs and cannot be tested.',
            'deprecated_cache_action: 652 runs, zero successes, 33 workflow versions in 22 repositories. '
            'Its conventional cluster inference needs caution because the exposure occurs in few repositories.',
            'Pointwise confidence intervals are not multiplicity-adjusted; significance is based on q.',
            'Duration models were not recomputed; their unchanged results remain in the prepared source directory.',
            '', '## Provenance', '', str(source), '',
            'See manifest.json for code/input hashes and original_result_integrity.json for preservation checks.'])
        (out/'README.md').write_text('\n'.join(summary)+'\n', encoding='utf-8')
        checks = {name:previous.digest(source/name) == expected for name, expected in manifest['prepared_sha256'].items()}
        previous.dump(out/'original_result_integrity.json', checks)
        if not all(checks.values()):
            raise ValueError('Previous results changed during calculation')
        manifest.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat(),
            seconds=time.perf_counter()-start, sample_matches_previous_success_model=True,
            duration_results_source=str(source), duration_results_recomputed=False)
        previous.dump(out/'manifest.json', manifest)
        print(json.dumps(dict(status=manifest['status'], runs=result['runs'], repositories=result['repositories'],
            seconds=manifest['seconds'], significant=[r['feature'] for r in rows if r['significant_fdr_05']],
            predictions=result['predictions']), ensure_ascii=False), flush=True)
    except BaseException:
        import traceback
        manifest.update(status='failed', error=traceback.format_exc())
        previous.dump(out/'manifest.json', manifest)
        raise


if __name__ == '__main__':
    main()
