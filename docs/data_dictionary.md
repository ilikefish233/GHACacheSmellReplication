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

The RQ2.3 history index lists sampled repositories, workflow paths, commits,
and references to verified YAML. `analysis_inputs/rq2_3/matching/run_jobs.jsonl`
contains successful default-branch run attempts, their archived job records,
and available exact content evidence. The entry point matches these runs to
newly detected events. `analysis_inputs/rq2_3/frequency/` contains the April
and May run archive and acquisition audits used for monthly estimates.

VM seconds sum allocated job time. Reference cost uses the original fixed historical OS multipliers, rounding and base rate in `src/resources/recompute.py`; it does not estimate current charges. Comparisons retain successful runs, average events within repositories and then weight repositories equally. Monthly equivalents apply workflow-specific differences once to observed workflow frequencies, then aggregate within repository; they are not causal savings. Historical events may include simultaneous workflow changes. Pointwise repository-bootstrap intervals are not multiplicity-adjusted.

## Validation

`Validation/data/annotations.xlsx` contains 383 annotated workflows and is copied
byte for byte from the author's completed workbook. All 383 are processed with
the frozen run3 detector/parser; no whole-workflow exclusions apply. Primary
TP/FP/FN and precision/recall/F1 use one binary workflow label per CCS type.
NDC excludes parenthesized out-of-scope labels (341 evaluated workflows); the
other ten formal types use 383 each. Job-level results are supplementary.

`annotations.yaml_sources.json` resolves three truncated Excel YAML cells to
packaged full snapshots under `Validation/data/yaml/`, with recorded SHA-256,
case identity and embedded-prefix checks. Paths are relative to the package root.
Missing or corrupt snapshots must fail rather than silently score truncated YAML.

Current provenance: `docs/replication_manifest.json` (section `validation`). Current expected results:
`Validation/expected/evaluation.json`. Earlier validation checks remain as historical entries in
`docs/verification/`; their old counting rules do not apply to this evaluation. See `Validation/README.md` for annotation
conventions and known limitations, including the sampling-provenance distinction.
