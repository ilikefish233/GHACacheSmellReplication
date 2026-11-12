# RQ2.3 replication

## Rebuild the results from the repository lists

The two lists were reconstructed for replication. Each contains 1,000 repositories.

- [repositories_batch1.txt](data/repositories_batch1.txt)
- [repositories_batch2.txt](data/repositories_batch2.txt)

Run the commands below from the code package root. Replace the data directory
with the path to your extracted Zenodo data.

```powershell
python RQ2/RQ2_3/scan.py --data-dir "PATH_TO_ZENODO_DATA" --output results_new/rq2_3_scan
python RQ2/RQ2_3/from_scan.py --data-dir "PATH_TO_ZENODO_DATA" --scan-dir results_new/rq2_3_scan --output results_new/rq2_3_from_scan
```

The first command detects formal CCS transitions in the saved
workflow histories. The second independently matches the detected events to
the archived default-branch runs and job records in
`analysis_inputs/rq2_3/matching/run_jobs.jsonl` and computes per-run and monthly
resource contrasts. Run and job coverage is fixed by the saved acquisition.
The matching input was prepared at workflow level before event selection.
The combined entry point `analysis.py` runs both steps. Use `--scan-dir` to reuse
a scan. The output covers the paper's per-run and monthly resource estimates.
The research archive retains the descriptive configuration and control-eligibility
diagnostics.
