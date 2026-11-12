# RQ1

See the root README and docs/data_dictionary.md for frozen inputs, counting rules and commands. All paths are package-relative; choose a new --output directory.


## RQ1 exclusion audit

`audit_exclusions.py` compares the 118,883 archived workflow snapshots with
the 118,523 RQ1 scan cohort, then checks detector errors in the scan records.
It documents the paper's 481 exclusions as 360 workflows not in the scan cohort
and 121 with detector errors. `expected/excluded_workflows.jsonl` lists all
481 workflow keys and their stage-specific reasons; `expected/exclusion_audit.json`
records the counts. Rebuild both from Zenodo:

```shell
python RQ1/audit_exclusions.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq1_exclusions
```


## Rebuild RQ1 scan inputs from curated workflows

`rebuild_scan.py` starts from the archived RQ1 cohort, canonical workflow
snapshots, original detector-state references, shared YAML, and repository
metadata. It scans the YAML with the packaged run3 detector, then writes
`rq1_scan_direct.jsonl` and `repo_info.jsonl` for `RQ1/analysis.py`.

```shell
python RQ1/rebuild_scan.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq1_scan
python RQ1/analysis.py --data-dir "PATH_TO_ZENODO_DATA" --scan results_rq1_scan/rq1_scan_direct.jsonl --output results_rq1_from_scan
```

This reconstructs the detector view from the curated workflow cohort. The
original GitHub API acquisition and the cohort's earlier curation still rely
on historical source records outside this data package. A small `--limit N`
option is available to check the interface before scanning all workflows.
