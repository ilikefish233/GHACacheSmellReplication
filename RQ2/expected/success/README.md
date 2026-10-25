# Experimental unified success LPM (2026-10-19)

Runs: 1,047,015; workflow versions: 50,460; repositories: 14,632.

All 11 smell indicators are considered jointly; non-identifiable indicators are explicitly marked.
Same endpoint sample, slim controls and version-balanced weights as the previous experiment.
Repository-cluster sandwich with G/(G-1) correction; two-sided t(G-1) tests; pointwise 95% intervals.
BH-FDR over 10 estimable smell hypotheses; significance q<0.05; bootstrap=0.

| Smell | Difference (pp) | 95% CI (pp) | p | q | Exposed repositories |
|---|---:|---|---:|---:|---:|
| no_dependency_cache | -2.417 | [-3.681, -1.153] | 0.000177949 | 0.000889744 | 3838 |
| no_fallback_cache | -2.226 | [-3.904, -0.548] | 0.00933045 | 0.0311015 | 1835 |
| redundant_cache_update | -0.472 | [-2.466, 1.522] | 0.642523 | 0.713915 | 889 |
| cache_position_error | 1.382 | [-2.342, 5.106] | 0.466938 | 0.583672 | 292 |
| conflicting_cache_sources | -1.760 | [-5.549, 2.030] | 0.362787 | 0.537738 | 228 |
| static_cache_key | 1.322 | [-1.382, 4.025] | 0.337937 | 0.537738 | 695 |
| overly_specific_cache_key | -1.695 | [-5.451, 2.061] | 0.376416 | 0.537738 | 249 |
| no_docker_layer_cache | -0.922 | [-2.744, 0.901] | 0.321651 | 0.537738 | 1073 |
| deprecated_cache_action | -82.521 | [-85.854, -79.188] | <2.23e-308 | 2.22507e-307 | 22 |
| cache_as_artifact | -1.437 | [-9.616, 6.742] | 0.730579 | 0.730579 | 67 |
| unreachable_cache_write | Not estimable | N/A | N/A | N/A | 0 |

## Diagnostics and interpretation

Unclipped predictions outside [0,1]: 1.43% of runs; 1.30% under the analysis weights.
Weighted R-squared: 0.0599.
The model estimates adjusted linear associations, not causal effects or odds ratios.
unreachable_cache_write has no exposed technical-outcome runs and cannot be tested.
deprecated_cache_action: 652 runs, zero successes, 33 workflow versions in 22 repositories. Its conventional cluster inference needs caution because the exposure occurs in few repositories.
Pointwise confidence intervals are not multiplicity-adjusted; significance is based on q.
Duration models were not recomputed; their unchanged results remain in the prepared source directory.

## Provenance

E:\gha_log_analysis\output_experimental_rq2_unified_slim_20261018_results

See manifest.json for code/input hashes and original_result_integrity.json for preservation checks.
