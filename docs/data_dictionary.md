# Data dictionary and counting rules

## RQ1

`RQ1/data/rq1_scan_direct.jsonl`: one original cohort workflow per line. `workflow_key` is the canonical repository identity plus normalized workflow path; `repository_id` is the stable repository identifier; `content_hash` identifies frozen YAML; `workflow_path` is its original repository path. `detectors.cache_smell` includes findings, error status and job-level eligibility metadata. Stored original machine paths are provenance only; adapters resolve YAML by content hash within the package. Detector-error records are excluded, not negative examples.

`data/canonical/repositories.jsonl`: repository identity, aliases, default branch, ecosystem and collected repository measurements. Original repository measurements are retained; dates must not be inferred from directory names. `rq1_cohort.jsonl` and `workflow_snapshots.jsonl` preserve cohort membership and content lineage. `content/` holds frozen YAML; its filename is the content hash. Byte/line-ending variants are checked explicitly when rerunning detection.

Overall prevalence divides affected workflows/repositories by the corresponding valid corpus, not the smell's eligibility denominator. Conditional supplementary rates use the original static opportunity metadata and do not imply every opportunity passed all final auxiliary-command exclusions. Trigger groups overlap; ecosystems and primary roles use the manuscript's original classification. Multiplicity counts the eleven formal types, not finding instances or nested diagnostics. Repository comparisons use the global no-detectable-smell reference and each target-smell group, two-sided asymptotic Mann-Whitney tests, continuity/tie handling, target-oriented Cliff's delta, and Holm correction over 55 tests.

## RQ2.1 / RQ2.2

`RQ2/data/run_records.jsonl`: matched workflow run attempts, run/version identity, event, timestamps, conclusion and duration. `version_records.jsonl`: workflow version, latest-version boundary and detector provenance. `version_scan_results.jsonl`: the frozen detector labels used for each version. `matching_metadata.json`: original linking and exclusion metadata. Run identity distinguishes attempts; default-branch and version-time linkage must be retained.

`RQ2/prepared/workflow_features.jsonl`: static YAML controls and labels per workflow version. `group_keys.jsonl`: version/event/calendar-week design-vector groups. `grouped_outcomes.npz`: `values` and `success_n`, sufficient statistics produced by the frozen implementation. Inspect that implementation for exact array column definitions. Identical design vectors are grouped; no run-level outcome information needed by the fitted model is lost. Endpoint-specific version weights total one per contributing version.

Success: success=1; failure/startup_failure/timed_out=0. Other conclusions are excluded. Duration endpoints use valid positive durations, with the original 35-day upper bound. Successful-duration sensitivity conditions on success. Unreachable cache write has no exposed technical-outcome version and is reported as non-estimable rather than assigned a zero effect. LPM predictions are not clipped; pointwise intervals differ from FDR-adjusted significance.

## RQ2.3

`RQ2/RQ2_3/expected/frozen_events.jsonl` and `frozen_run_manifest.jsonl` fix historical comparisons before resource computation. `data/frozen_job_metadata.jsonl` contains the exact referenced jobs, keyed by repository/run/attempt, plus original archive evidence. `data/frequency/` preserves the audited April-May run archive, acquisition statuses and audit counts. Missing frequency is not zero.

VM seconds sum allocated job time. Reference cost uses the original fixed historical OS multipliers, rounding and base rate in `src/resources/recompute.py`; it does not estimate current charges. Comparisons retain successful runs, average events within repositories and then weight repositories equally. Monthly equivalents apply workflow-specific differences once to observed workflow frequencies, then aggregate within repository; they are not causal savings. Historical events may include simultaneous workflow changes. Pointwise repository-bootstrap intervals are not multiplicity-adjusted.

## Validation

The workbook is unchanged. Only its first 200 physical data rows are scored, with 24 adoption-zero cases excluded. Matching is primarily smell type plus job. Parenthesized NDC annotations follow the original per-job exclusions. This is a detector-development benchmark, not a population-representative accuracy estimate or an independent 383-case benchmark. Frozen scoring also emits diagnostic candidate types; only eleven formal types belong in paper quantitative summaries.
