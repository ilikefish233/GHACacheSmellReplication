# Workflow duration

`data.csv` is a frozen expected-output table. Source: `RQ2/expected/duration/all_completed_duration.json and successful_duration.json, coefficients arrays`.
It is not the workflow/run-level input dataset.

From the repository root, after installing external data:

```powershell
python RQ2/RQ2_1/analysis.py --output results_new/rq2
```

RQ1 subentries share the RQ1 analysis; duration and success subentries share
the unified RQ2 analysis. Each shared analysis runs once for the relevant RQ.
Do not invoke a second subentry with an existing output directory.
