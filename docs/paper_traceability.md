# Paper methods and replication inputs

This map follows the current manuscript (current submitted manuscript). The GitHub
checkout holds code, small audit records, and expected results. The extracted
Zenodo directory holds the large analysis inputs. Run `python reproduce.py
--data-dir PATH_TO_ZENODO_DATA --output NEW_OUTPUT` from the checkout root to
recompute and compare the offline paper results.

| Manuscript step | Code | Input or retained evidence | Output and paper count |
| --- | --- | --- | --- |
| Select repositories from GHALogs | `src/collection/scripts/filter_repositories.py` | External GHALogs `repositories.json.gz`; `Pipeline/records/filter_stats.json.gz`, `filtered_repositories.json.gz`, `collected_repository_info.jsonl.gz` | Selection records: 22,911 filtered and 22,659 later metadata rows. These intermediate counts are not stated in the current manuscript. |
| Collect cutoff workflow YAML, histories, and runs | `src/collection/scripts/fetch_workflows.py`, `supplement_workflow_snapshots.py`, and the collection scripts listed in `Pipeline/stages.json` | Zenodo `workflows/`, `runs/`, and `repositories/`; `workflows/dataset_manifest.json`; `Pipeline/records/workflow_identity_audit.json` | 131,009 source workflow records. Collection from the live GitHub API may change over time. |
| Resolve repository aliases and workflow identities | `src/collection/scripts/build_canonical_dataset.py` and related curation scripts in `src/collection/scripts/` | Zenodo `workflows/workflow_snapshots.jsonl`, `workflows/dataset_manifest.json`; `Pipeline/records/workflow_identity_audit.json` | 11,807 duplicate workflow identities merged; 319 unidentified source records removed; 118,883 distinct workflows. |
| Validate the 11-type detector | `Validation/analysis.py` and `src/frozen/src/detectors/smell/cache_smell_detector.py` | `Validation/data/annotations.xlsx`, three full YAML replacements, and `Validation/expected/evaluation.json` | 383 manually annotated workflows; per-type precision, recall, and F1. |
| Compute RQ1 | `RQ1/rebuild_scan.py` builds a scan view from curated YAML; `src/scan.py` rescans records; `RQ1/analysis.py` computes paper tables | Zenodo `analysis_inputs/rq1/rq1_scan_direct.jsonl`, `repo_info.jsonl`, `workflows/rq1_cohort.jsonl`, shared YAML; GitHub `RQ1/audit_exclusions.py`, `RQ1/expected/excluded_workflows.jsonl`, and `RQ1/expected/paper_tables/` | 481 workflows without usable detection results excluded; 118,402 workflows from 19,324 repositories. |
| Match RQ2 runs and fit models | Collection and preparation scripts in `src/collection/scripts/`; `RQ2/analysis.py` (`--rebuild-groups` recomputes run groups) | Zenodo `runs/rq2_observation_runs.jsonl`, `analysis_inputs/rq2/model_input/` and `prepared/`; GitHub `RQ2/check_collected_runs.py`, `RQ2/rebuild_input.py`, `RQ2/check_rebuilt_input.py`, `RQ2/rescan_labels.py`, `RQ2/audit_detector_exclusions.py`, and `RQ2/expected/detector_excluded_runs.jsonl` | Common sample: 1,091,978 attempts from 14,695 repositories. Duration: 1,089,617 from 14,690. Success: 1,047,015 from 14,632. |
| Scan RQ2.3 historical CCS changes | `src/collection/scripts/sample_rq23_repositories.py`, history collection scripts in the same directory, `RQ2/RQ2_3/scan.py` | GitHub `RQ2/RQ2_3/data/repositories_batch1.txt` and `repositories_batch2.txt`; Zenodo `analysis_inputs/rq2_3/collection/workflow_versions.jsonl` and shared YAML | Two lists of 1,000 repositories; 1,942 formal candidate events across 663 repository names. `scan.py` emits `candidate_transitions.jsonl` and `scan_summary.json`. |
| Render the two quantitative manuscript figures | `src/render_figures.py` | Zenodo `analysis_inputs/rq1/paper_source_data/`; GitHub `RQ2/expected/` or regenerated RQ2 results | Repository-context violin panel and adjusted-association forest panel. Other conceptual and interface artwork remains in the manuscript source. |
| Match RQ2.3 runs and calculate resource contrasts | `RQ2/RQ2_3/from_scan.py`, `src/resources/recompute.py`, `RQ2/RQ2_3/analysis.py` | Zenodo `analysis_inputs/rq2_3/matching/run_jobs.jsonl` and `frequency/`; GitHub `RQ2/RQ2_3/expected/` | Generated `event_status.jsonl`: 167 events had qualifying runs on both sides; 2 lacked complete job resource data. Final result: 165 events, 87 repositories, 1,265 successful run attempts. |

The two RQ2.3 repository lists were reconstructed for replication to retain the
repositories represented in the candidate-event data; they do not document
the original random draw. The archived history and run/job inputs are fixed,
so the offline analysis checks the reported result from those records. The
current packages retain selected repository-selection records and curated
analysis inputs, but not the complete 94-file raw acquisition archive listed
in `Pipeline/data_manifest.json`. Thus the full historical GitHub collection
cannot be replayed solely from these packages. The executable offline path
starts from the prepared Zenodo inputs.
