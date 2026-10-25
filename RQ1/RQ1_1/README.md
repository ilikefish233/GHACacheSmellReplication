# Prevalence and contexts

`data.csv` is a frozen expected-output table. Source: `RQ1/expected/paper_tables/prevalence_comparison.csv (current new_* columns only; prefix removed)`.
It is not the workflow/run-level input dataset.

From the repository root, after installing external data:

```powershell
python RQ1/RQ1_1/analysis.py --output results_new/rq1
```

RQ1 subentries share the RQ1 analysis; duration and success subentries share
the unified RQ2 analysis. Each shared analysis runs once for the relevant RQ.
Do not invoke a second subentry with an existing output directory.
