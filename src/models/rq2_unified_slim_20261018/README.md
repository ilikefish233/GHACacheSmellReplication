
# Unified RQ2 models and exact grouping

Run the adopted models with
`python RQ2/analysis.py --data-dir PATH_TO_ZENODO_DATA --output results_rq2`.
Add `--rebuild-groups` to reaggregate the individual run records, or
`--bootstrap 1000` for the optional duration sensitivity analysis. Use a
fresh output directory for each run.

The models use the saved detector labels and version-aligned run cohort.
Both duration endpoints model log duration for completed and successful runs,
respectively. The code also contains the earlier logistic and Firth
sensitivity analyses. The adopted success analysis uses the linear probability
model documented in the sibling model directory.

Controls include log-transformed declared job and non-cache step counts,
the matrix indicator, workflow role, dependency ecosystem, runner OS and
hosting, trigger, and calendar week. Static workflow-role and environment
classifications are heuristic. Unknown classifications are retained.

Runs with the same version, trigger and week share a design vector.
The grouped data preserve counts, success counts, and the sum and sum of
squares of log duration. Grouped and expanded fits and cluster scores agree
within numerical precision. Each contributing workflow version has total
weight one per endpoint. Primary duration inference uses repository-clustered
standard errors and Benjamini–Hochberg correction across estimable CCS
coefficients. The default bootstrap count is zero.

The data manifest records code and input hashes. The prepared workflow
features, group keys and grouped outcomes support fast refitting. The
run-level records can be used to rebuild the groups.
