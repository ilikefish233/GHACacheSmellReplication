> Companion guide retained from the complete-package edition. For the current GitHub layout and commands, use the root README. Old paths in immutable reports are mapped in `docs/layout_map.json`.

# Cache Configuration Smells in GitHub Actions: A Large-Scale Empirical Study

Replication package for the Empirical Software Engineering manuscript. This package includes the complete frozen analysis inputs, workflow YAML, paper-selected detector, annotated validation workbook, model implementations, and expected results. No GitHub account, API token, network collection, external drive, or Codex installation is needed to run the analyses.

The layout follows the RQ-oriented approach of [GHALincesepack](https://github.com/xchu7504-sys/GHALincesepack). Shared large data are stored once rather than duplicated for each subquestion. The adopted analyses were originally saved in directories named `experimental`; that historical flag is retained in frozen files and does not indicate a different model in this package.

## Install

Tested using Python 3.12.3. Create an environment and install dependencies:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

The requirements pin the versions used in the artifact validation environment. Install them on an internet-connected machine, or use an independently prepared wheel cache. Analysis execution itself is offline. Allow 20 GB free for the unpacked package, environment and new outputs; more space is needed if retaining multiple runs.

## Run

Run from any working directory; paths are resolved relative to the package:

```bash
python reproduce.py --output results_new
```

All output directories must be new. Original data and expected results are never overwritten.

Individual entry points:

```bash
python Validation/analysis.py --output results_new/validation
python RQ1/analysis.py --output results_new/rq1
python RQ2/analysis.py --output results_new/rq2
python RQ2/RQ2_3/analysis.py --output results_new/rq2_3
python verify.py --results results_new
python verify.py --checksums --integrity-only
python -I src/tests/run_models.py
python src/render_figures.py --output figures_new --results results_new/rq2
```

RQ2's default mode refits the regressions from bundled exact sufficient statistics. To also reconstruct the groups and outcome sums from every run, use `--rebuild-groups`. This still reuses verified YAML feature definitions; it does not silently substitute newer features.

```bash
python RQ2/analysis.py --output results_rebuilt --rebuild-groups
python src/scan.py --output rescanned --workers 4
python RQ1/analysis.py --scan rescanned/rq1_scan_direct.jsonl --output rq1_rescanned
```

The full scan is optional and slower. The scan retains the original excluded state only for content unavailable in the frozen corpus; it aborts if content is missing for an originally valid record. `--limit 100` performs a smoke scan and cannot reproduce population results. Dataset construction starts from the supplied frozen cohort, not a new live GitHub query. The original GHALogs candidate repository frame is obtained separately at `data/upstream/ghalogs/repositories.json` using the instructions below; initial filtering and offline curation are now included; live collection has not been repeated. See `docs/data_provenance.md`.

## Contents and paper mapping

| Directory | Data and executable analysis | Paper scope |
|---|---|---|
| `Validation/` | Unchanged annotated workbook; frozen detector evaluation; expected metrics | Detector evaluation: first 200 physical cases, 24 excluded, 176 analyzed |
| `RQ1/` | Full scan records, repository metadata, analysis, paper-facing source CSVs | RQ1.1 prevalence and contexts; RQ1.2 multiplicity and co-occurrence |
| `RQ2/` | Full run/version/label records; grouped sufficient statistics; unified regression code | RQ2.1 log duration; RQ2.2 success LPM |
| `RQ2/RQ2_3/` | Frozen historical comparisons, raw referenced job records, full observed-frequency archive | RQ2.3 exploratory VM time and historical reference cost |
| `data/canonical/` | Frozen workflow YAML, repository measurements, cohort and version-boundary evidence | Shared provenance and detector reruns |
| `src/frozen/` | Byte-preserved run3 detector and supporting source | Exact detector used in the manuscript |
| `src/models/` | Byte-preserved adopted unified duration and LPM implementations | Original model mathematics and inference |
| `paper/` | Manuscript source snapshot, cited figure assets and supplementary source | Identifies the manuscript version audited |
| `Tool/` | Optional local Workflow Cache Inspector UI bound to the frozen detector | Prototype demonstration; live scans are separate from replication |
| `docs/provenance/` | Original manifests and comparison-only legacy reference tables | Hash and version traceability |

RQ1 contains 118,402 valid workflows in 19,324 repositories, including 26,388 affected workflows and 10,528 affected repositories. RQ2 starts with 51,179 versions and 1,093,639 linked run attempts. The completed-duration endpoint has 1,089,617 runs; success has 1,047,015 runs. Endpoint-specific exclusions and populations are retained rather than mixed. RQ2.3 includes 166 historical events in 88 repositories and 1,268 distinct successful run attempts.

The study cutoff is 2026-06-09 23:59:59 UTC. Main RQ2 models use the fixed 30-day window, default-branch/version linkage, endpoint-specific version-balanced weights, repository-clustered uncertainty and endpoint-wise BH correction. Duration uses log-linear regression; success uses a linear probability model and reports percentage-point differences, not odds ratios. Primary unified models use no bootstrap; RQ2.3 uses its original repository bootstrap. Results describe observational associations, not causal repair benefits or current bills.

Only the eleven paper types are quantitative outcomes. New exports use `no_fallback_cache`; frozen sources may retain `no_restore_keys`. These are aliases of one type. `conflicting_cache_sources` is displayed as `overlapping_cache_mechanisms`. RQ3 is excluded; RQ2.3 is the adopted exploratory resource subquestion and does not resume RQ3 collection or analysis.

Subquestion launchers are also available at `RQ1/RQ1_1/analysis.py`, `RQ1/RQ1_2/analysis.py`, `RQ2/RQ2_1/analysis.py` and `RQ2/RQ2_2/analysis.py`. They reuse the shared parent analysis and compute its related tables/endpoints together; they do not duplicate the underlying data.

## Verification and boundaries

`verify.py` compares regenerated sample sizes, validation metrics, RQ1 paper tables, all regression coefficients and clustered inference, and RQ2.3 outputs against frozen references. Numerical tolerance is `rtol=1e-8`, `atol=1e-10`; elapsed times, machine paths and timestamps are not scientific equality targets. See `results/verification.json` for the actual local test outcome. File hashes are in `checksums.sha256`.

Original figures are supplied under `paper/figures`; their numerical source data are bundled. Manuscript compilation requires a separate TeX environment and publisher class files and is not a prerequisite for data analysis. Paper-facing CSVs preserve the original context grouping rather than substituting the newer activity detector or 383-case evaluation.

`src/render_figures.py` regenerates the main repository-context figure and the joint duration/success forest from the original plotting designs; its panel-alignment helper is bundled. The archive also retains the original non-quantitative case illustrations and screenshots; those are supplied assets, not regenerated API responses.

See `docs/data_dictionary.md`, `docs/data_provenance.md` and `docs/third_party_notices.md`. This is a local artifact prepared for review. No repository release, DOI or public hosting has been created. Author-owned code is MIT; author-owned derived data and documentation are CC BY 4.0. See `docs/license_scope.md` for exact scope and third-party exclusions. Public deposit and citation identifiers remain pending; see `docs/zenodo_deposit.md` for upload instructions.

Acquisition and curation are included under `Pipeline/`. See [the pipeline guide](Pipeline/README.md) for the offline filter/canonical/consolidation/coverage/queue/model-input replay, losslessly compressed raw API archives, original collection tables, exact source hashes, and separate explicit live-acquisition commands. The default paper analysis above remains the October frozen analysis.

## Obtain the upstream GHALogs candidate list

GHALogs v1.0.1 is the third-party source of the initial candidate repository list. It is separate from the workflow YAML and run records collected for this study.

- Dataset record and citation: [GHALogs v1.0.1, DOI 10.5281/zenodo.14796970](https://zenodo.org/records/14796970).
- Download only [repositories.json.gz](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1) (approximately 69.2 MB). The 142 GB log archive and `runs.json.gz` are not needed for this initial filtering stage.

From the extracted package root, download and decompress with Python:

```powershell
python -c "from pathlib import Path; import urllib.request; p=Path('data/upstream/ghalogs/repositories.json.gz'); p.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve('https://zenodo.org/records/14796970/files/repositories.json.gz?download=1', str(p))"
python -c "import gzip, shutil; from pathlib import Path; p=Path('data/upstream/ghalogs'); shutil.copyfileobj(gzip.open(p/'repositories.json.gz', 'rb'), (p/'repositories.json').open('wb'))"
```

Alternatively, download the named file from the dataset page and decompress it manually. The required final path is `data/upstream/ghalogs/repositories.json`. Despite its extension, this is JSON Lines; do not convert it into a JSON array. Skip these download/extraction commands if a valid copy already exists, since extraction overwrites that file.

To replay initial filtering after installing the pipeline dependencies:

```powershell
python -m pip install -r Pipeline/requirements.txt
python Pipeline/run.py filter --work-dir ../emse_pipeline_work
```

To continue through the six offline curation stages, use `python Pipeline/run.py offline-all --work-dir ../emse_pipeline_work`. Downloading requires internet access; the subsequent offline stages do not. The default paper analysis (`python reproduce.py --output results_new`) uses the frozen analysis inputs and does not require this upstream download.

**Third-party terms:** the GHALogs file is not licensed by this package. Consult the original record and its authors for applicable reuse and redistribution terms; public download access is not itself an explicit redistribution licence. Cite the original dataset and associated GHALogs paper when using it. The original candidate list and its compressed copy are excluded from this distributed package. Obtain them directly from the source when replaying initial filtering; the authors retain a separate research copy outside the package.
