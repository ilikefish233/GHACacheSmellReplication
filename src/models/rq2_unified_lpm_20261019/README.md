
# Unified success linear probability model

Run the paper analysis from the code package root with
`python RQ2/analysis.py --data-dir PATH_TO_ZENODO_DATA --output results_rq2`.
The input is the exact grouped design used by the duration models. The
entry point checks the original input and model source hashes.

The response is one for success and zero for failure, startup failure, or
timeout. Each workflow version has total weight one among contributing
runs. The model fits the 11 CCS indicators jointly and uses the same
workflow and run controls as the duration analyses.

Weighted least squares uses repository-clustered sandwich standard errors
with correction G/(G-1), two-sided t tests with G-1 degrees of freedom,
and pointwise 95% intervals. The Benjamini–Hochberg correction covers the
10 estimable CCS coefficients. The constant indicator for
`unreachable_cache_write` is listed as not estimable.

Coefficients describe adjusted probability differences, reported also in
percentage points. The linear model can predict values outside [0, 1].
The output reports the fraction of such predictions. Inference for types
with few exposed repositories warrants care. See `reliability_lpm.json`.
