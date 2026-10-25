# Acquisition and curation pipeline

This directory supplements the paper-level replication with the upstream repository frame, acquisition code, frozen API responses, normalized increments, coverage evidence, and executable offline curation stages. Only RQ1 and RQ2 are active. The paper's RQ2.3 exploratory comparison is distinct from retired RQ3.

## External upstream input

Before `filter` or `offline-all`, obtain the GHALogs candidate list using the root README. It is not distributed. The archived verification reports describe earlier checks against the original research copy. Default paper analyses do not require this input.

## Quick start

Run from the package root with Python 3.12:

```powershell
python -m pip install -r Pipeline/requirements.txt
python Pipeline/run.py offline-all --work-dir ../emse_pipeline_work
```

The work directory is separate from the package. Inputs are losslessly compressed with gzip; `data_manifest.json` maps original project paths to archive files and records the SHA256 of the uncompressed bytes. Each input is decompressed only when its stage needs it, and its hash is checked before use. Original large API responses remain compressed during normal offline replay. Temporary SQLite indexes and replay outputs require additional disk space. `offline-all` performs no network requests; the invocation wrapper blocks socket connections. Outputs and logs go under the work directory, never into frozen expected results.

Stages can be run individually in the following order with the same `--work-dir`:

| Stage | Inputs and processing | Output under the work directory |
|---|---|---|
| `filter` | Original GHALogs `repositories.json`; original selected/processed, nonfork, nonarchived, stars >100, commits >100, runs >50 predicates | `rebuilt/filter` |
| `canonical` | Author-collected repository metadata, cutoff snapshot index and scan, merged workflow changes and pre-canonical run table; stable repository identities, alias/path normalization, cutoff/version boundaries and run-attempt deduplication | `rebuilt/canonical` |
| `consolidate` | Frozen canonical runs plus all eleven formal acquisition increments and their status records; original SQLite-backed conflict resolution and deduplication | `rebuilt/consolidated` |
| `coverage` | Original query coverage evidence plus consolidated per-workflow acquisition status | `rebuilt/coverage` |
| `queue` | Consolidated runs, frozen canonical cohorts/boundaries and rebuilt coverage; identity recovery, completed/default-branch/event/version-window predicates | `rebuilt/queue` |
| `model-input` | Rebuilt final workflow/run queues and historical cutoff detector states; original conversion-only adapter | `rebuilt/model_input` |

The `canonical` adapter calls the original RQ1/RQ2 functions, relocates YAML by frozen content hash, and preserves original missing-content exclusions. It does **not** call the historical builder's `main()`, which also contains retired RQ3 sampling/history branches. The original source remains available for inspection. Generated content paths point back to the shared packaged YAML store; the package-wide checksum entry point verifies those bytes. The adapter also avoids repeated filesystem resolution of unchanged provenance paths. The consolidation wrapper caches the original pure path-label function and uses larger sequential I/O buffers; serialized rows, deduplication precedence and statistical rules are unchanged. The model-input wrapper maintains the same set of prior version IDs incrementally, preserving duplicate-ID validation while removing a quadratic set reconstruction. The copied historical source files remain byte-for-byte unchanged; these transparent adapters are in `invoke.py`. Retired historical detector states are not recomputed. Consolidation and downstream queue replay deliberately start from the frozen canonical tables so that they can also be run without repeating canonical construction. The original September detector-state file is preserved byte-for-byte for queue-construction provenance, including unused archival cache rows. Only states referenced by cutoff RQ1/RQ2 records are selected; no retired RQ3 analysis is run. These historical states are not the October detector used for paper findings.

## Handoff to October paper results

`rebuilt/model_input` reconstructs workflow identities, version boundaries, the 51,179-version/1,093,639-run queue, ordering and controls before the October detector refresh. The manuscript's run3 refresh retained these versions and runs and replaced detector labels. Its exact source is preserved in `src/frozen/scripts/rerun_rq1_rq2_experimental.py`; its original orchestration also invokes an older regression and is not the adopted unified-model entry point. For adopted analyses use the package root `reproduce.py`, `RQ2/analysis.py`, and the exact implementations under `src/models/`. `src/scan.py` provides the frozen run3 RQ1 rescan. The pipeline replay does not silently substitute September smell labels or the legacy regression for October paper models. `verify_handoff.py` checks regenerated identities and run order against the adopted frozen inputs.

```powershell
python Pipeline/verify_canonical.py --work-dir ../emse_pipeline_work
python Pipeline/verify_handoff.py --work-dir ../emse_pipeline_work
python reproduce.py --output ../emse_paper_results --rebuild-groups
python verify.py --results ../emse_paper_results
```

## Optional live acquisition

Use an environment variable `GITHUB_TOKEN`. No credential file is bundled. Live acquisition is separate from offline replay and requires `--execute`:

```powershell
python Pipeline/run.py collect-base --work-dir ../emse_pipeline_work --execute
python Pipeline/run.py collect-runs --work-dir ../emse_pipeline_work --execute
```

`collect-base` uses the reproduced initial filter output and the original unified collector. `collect-runs` uses the frozen audited supplementation plan and the original paginated/retrying API executor. Fresh output is written to `new_collection/`. Historical API availability, deleted runs, repository changes, and current metadata mean a live recollection cannot promise byte-identical historical results. The original collector's historical page/run limits and later supplementation must be read together; the early five-runs-per-workflow collection is not the final RQ2 dataset.

Additional original entry points under `src/collection/scripts/` document missing-workflow snapshots, default-branch runs, history completion, cutoff snapshot repairs, history merging, name normalization, and raw-to-normalized response processing. Their `--help` flags and the captured manifests retain the original stage contracts. They are supporting acquisition/reconciliation procedures, not automatic extra stages of `offline-all`. The historical RQ2.3 acquisition sources are included for provenance; the adopted RQ2.3 replay remains `RQ2/RQ2_3/analysis.py` from its frozen events and referenced raw job metadata.

To extract a specific archived source, including an original raw API response archive:

```powershell
python Pipeline/run.py materialize --work-dir ../emse_pipeline_work --input-path output_rq2_unified_acquisition_phase2_full_20260919/raw_workflow_runs.jsonl
```

## Coverage and limits

`source_manifest.json` records the exact copied source revisions. These are acquisition/curation sources recovered from the working project; `src/frozen` remains the authority for the paper detector. `capture_report.json` gives exact payload sizes. `data_manifest.json` inventories frozen stage inputs and the original raw response archives for the eleven formal RQ2 supplementation batches. It is not a copy of every abandoned pilot, temporary SQLite database, duplicate history blob or retired RQ3 output. The original baseline collection and its metadata are included as JSONL stage tables; the collection did not preserve a separate full HTTP response archive for every baseline request. Workflow YAML required for the frozen study is shared with `data/canonical/content`, rather than repeating historical duplicate files. GHALogs supplied the candidate frame, not the author-collected workflow/run corpus. Author-owned materials use MIT/CC BY 4.0 as scoped in `docs/license_scope.md`; permanent release identifiers remain pending. Third-party workflow licensing review is deferred.

The original base-collection configuration is preserved in `src/collection/scripts/data_update/filter_config.py`: it excludes archived repositories, checks a recent-run threshold of 30 in a 90-day window, and excludes workflow files without matching runs. These acquisition-time checks are distinct from the initial candidate-frame filter (>50 runs) and the final RQ2 coverage/version eligibility checks. `stages.json` records the stage-specific entry points and expected counts.

## Assembly verification

`docs/verification/pipeline/verification.json` (repository-root path) links the historical checks. All six offline stages were executed on Windows/Python 3.12.3. Initial filter membership matches the saved 22,911-repository list. Canonical normalization reproduces the original run file byte-for-byte. Consolidated runs and acquisition statuses also match their original SHA256. Coverage and queue summaries match; all 51,179 version records and 1,093,639 ordered run records match the adopted model-input scientific fields. All 94 gzip inputs were decompressed and checked against their original byte hashes. Acquisition was tested in dry-run mode only. Fresh installation, other operating systems and a new full detector rescan were not performed.
