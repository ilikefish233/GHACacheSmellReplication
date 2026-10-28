# Detector validation

This is the 383-workflow evaluation reported in the manuscript. The primary unit
is one workflow per formal CCS type; job-level metrics are supplementary.
The selected run3 detector and parser are the same study versions used by RQ1/RQ2.
The folder name `src/frozen` identifies that iteration; it is not an immutable archive.

From the repository root, choose a new output directory:

```console
python Validation/analysis.py --output results_new/validation
python verify.py --results results_new --validation-only
```

No RQ large-data archive, network access or API token is needed. The entry point
also works from another working directory when invoked by its absolute path.
It checks input and frozen-source hashes before evaluating the annotations.

## Inputs and interpretation

- `data/annotations.xlsx`: the supplied 383-case workbook, copied byte for byte.
- `data/annotations.yaml_sources.json` and `data/yaml/`: three full, hash-checked
  YAML snapshots for cases 28, 44 and 70 whose Excel cells truncate the YAML.
  Snapshot paths are relative to the repository root. Case identity and embedded
  YAML prefixes must match; a missing or altered snapshot is an error.
- No whole-workflow exclusions apply to this workbook. All 383 workflows parse
  and run successfully. Unsupported parenthesized dependency-cache labels exclude
  42 workflows for that type only: NDC uses 341, the other ten types use 383.
- Blank/N/A means negative. Uncertain/pending labels are excluded per type.
  Deprecated-action annotations use inventory interpretation; code 99 means unknown.
- Eleven formal types only. Overly specific keys combine columns V and AC;
  unreachable writes derive from explicit annotations in the notes fields.
  See the evaluator and generated report for the precise label conventions.

## Outputs and verification

`evaluation.json` contains per-workflow findings, reference labels, exclusions,
and primary/secondary metrics. `case_metrics.csv` is the paper's primary table;
`job_metrics.csv` is supplementary. `disagreements.csv` lists workflow-level FP/FN;
`report.md` describes metrics and annotation boundaries.

The verifier compares all eleven type metrics and detailed case results with
`expected/evaluation.json`, including counts, exclusions, failures and input hash.
Current provenance is in `../docs/replication_manifest.json` (section `validation`).
Superseded 200-row validation scripts are not distributed. Earlier verification
results remain historical records, not the current scoring specification.

Workflow-level agreement does not establish step/job localization accuracy.
Sparse positive cases limit the evidence for some rules. The workbook filename
records rare-type enrichment; matching the reported table alone does not verify
the manuscript's random-sampling description.
