# Cache Configuration Smells in GitHub Actions: A Large-Scale Empirical Study

This repository provides the replication code, validation material and small
result tables for our empirical study, prepared for submission to *Empirical
Software Engineering* (EMSE).

## Overview

The package supports reproducing the paper's detector evaluation, cache-smell
prevalence and co-occurrence analyses (RQ1), adjusted duration and success
associations (RQ2.1–RQ2.2), and exploratory resource comparisons (RQ2.3). It also
includes the data acquisition, initial filtering and cleaning pipeline.

The organization follows the RQ/sub-RQ layout of
[GHALincesepack](https://github.com/xchu7504-sys/GHALincesepack). The analysis uses
the paper-selected October run3 detector, unified duration models and success
linear probability model (LPM). RQ3 has been retired and is outside this package.

**This is the GitHub edition.** Code, annotations and small expected-result
tables are included. Large workflow/run datasets must be restored from the
separate complete-package ZIP before the RQ analyses can run. The annotated
validation experiment can run using this checkout alone.

## Contents

```text
GHACacheSmellReplication/
  RQ1/                 # RQ/sub-RQ entry points and expected tables
  RQ2/
  Validation/
  Pipeline/            # data construction entry points
  src/                 # shared detector, models, plotting and test code
  data/                # external data configuration and input locations
  docs/                # methods, provenance, reports and manuscript material
  LICENSES/
  README.md
  README_zh.md
  requirements.txt
  reproduce.py
  data_setup.py
  verify.py
```


- `Validation/`: Detector evaluation.
  - `data/annotations.xlsx`: Original annotation workbook; evaluation uses the
    first 200 physical cases, excludes 24 cases and analyzes 176 eligible cases.
  - `analysis.py`: Runs the frozen detector evaluation.
  - `expected/evaluation.json`: Expected case counts and metrics.
- `RQ1/RQ1_1/`: Prevalence and contexts.
  - `data.csv`: Adopted prevalence counts and eligible-population percentages.
  - `analysis.py`: Entry point for the shared RQ1 analysis.
- `RQ1/RQ1_2/`: Multiplicity and co-occurrence.
  - `data.csv`: Frozen multiplicity table.
  - `cooccurrence.csv`: Frozen co-occurrence table.
  - `analysis.py`: Alternative entry point for the same shared RQ1 analysis.
- `RQ2/RQ2_1/`: Workflow duration associations.
  - `data.csv`: Frozen coefficients for all-completed and successful-run duration.
  - `analysis.py`: Entry point for the shared unified RQ2 models.
- `RQ2/RQ2_2/`: Workflow-run success associations.
  - `data.csv`: Frozen LPM coefficients, intervals and inference fields.
  - `analysis.py`: Alternative entry point for the same shared RQ2 models.
- `RQ2/RQ2_3/`: Exploratory resource comparisons.
  - `data.csv`: Frozen smell-level resource summary.
  - `analysis.py`: Recomputes the selected historical comparisons and frequency summaries.
- `RQ1/expected/`, `RQ2/expected/`: Small expected outputs used for comparison.
- `src/`: Shared implementations: `frozen/` (detector), `models/` (duration/LPM),
  `analysis/` and `resources/` (paper analysis helpers), `collection/` (captured
  acquisition source), plus `scan.py`, `render_figures.py`, `tool/` and `tests/`.
- `Pipeline/`: User-facing acquisition, supplementation and offline cleaning entry points.
- `docs/`: Data dictionary and paper mapping, with `paper/`, `provenance/` and
  `verification/` subdirectories for manuscript assets, source records and checks.
- `data/`: External input placeholders and the data source configuration.
- `LICENSES/`: Full data/documentation license text; the code license is `LICENSE`.
- `data_setup.py`, `data/sources.json`: External-data installation and source configuration.
- `reproduce.py`, `verify.py`: Complete paper-analysis orchestration and result comparison.
- `requirements.txt`: Pinned analysis dependencies.

The adjacent `data.csv` files are frozen **expected outputs** for inspection.
The scripts recompute from shared workflow/run-level inputs, rather than fitting
models to those summary CSVs. Each subdirectory README identifies its table source.

## Usage

You can use this package to inspect the published-result tables, evaluate the
detector, reproduce the adopted analyses, rescan workflow snapshots, or replay
the data construction pipeline. Use the following sequence for full replication:

1. Extract this GitHub package or clone the repository, then open a terminal in
   the root folder containing `reproduce.py` and `requirements.txt`.
2. Create a Python environment and install dependencies.
3. Obtain the matching complete-package ZIP and restore its external datasets.
4. Run the analyses in a new output directory.
5. Compare the generated results with the included expected results.

### Environment setup

The existing validation environment uses **Python 3.12.3 on Windows**. A fresh
dependency installation and Linux/macOS execution have not been tested. Allow
at least **20 GB** for restored data, the environment and default analysis outputs;
the full acquisition/cleaning replay needs substantially more working space.

```powershell
python -m venv .venv
```

Activate the environment using the command for your shell:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux/macOS shell
source .venv/bin/activate
```

Then install the analysis dependencies:

```powershell
python -m pip install -r requirements.txt
```

Dependency installation normally needs internet access. After data are restored,
the paper analyses do not require a GitHub account, API token or network access.

### Obtain and restore the analysis data

**Data archive status: Zenodo draft; DOI reserved; data not yet published.**
Reserved data DOI: [10.5281/zenodo.23640810](https://doi.org/10.5281/zenodo.23640810).
The DOI is not registered until the Zenodo record is published and may not resolve
before then. The draft is not a public data download. `data/sources.json` records
the reserved `doi`; `archive_url` remains `null`. After publication, the authors
will configure the actual data ZIP download URL. Readers do not need to edit
these settings. The Zenodo deposit contains the data omitted from this GitHub
release; executable code is distributed here.

If you already have the matching complete ZIP, place it next to this repository
folder and run:

```powershell
python data_setup.py --status
python data_setup.py --archive "../replication_package_emse_20261025.zip"
python data_setup.py --status
```

Replace the ZIP path with its actual location. Successful restoration prints
the number of restored files; the final status should show
`"missing_required_files": []`. Status checks file presence, not content integrity.
The installer restores only omitted datasets and preserves this repository's
code, README and expected tables. It checks the archived scientific identity,
selected file count and total sizes. Existing same-size data files are skipped,
not independently authenticated.

Large inputs populate `data/canonical/`, `RQ1/data/`, `RQ1/paper_source_data/`,
`RQ2/data/`, `RQ2/prepared/`, `RQ2/RQ2_3/data/`, `Pipeline/data/` and the additional
files listed in `data/sources.json`. These paths are ignored by Git.

After a real direct-download URL is entered in `data/sources.json`, the equivalent
download-and-install command will be:

```powershell
python data_setup.py
```

Use a ZIP download URL, not the DOI landing-page URL. Data restoration does not
require the separate GHALogs candidate list for the default paper analyses.

## Running the Analyses

### Reproduce all adopted analyses

After restoring the data, run:

```powershell
python reproduce.py --output results_reproduced
```

This runs validation, RQ1, the shared RQ2 duration/success models and RQ2.3, then
compares their results against the frozen expected outputs. Outputs are written
to `results_reproduced/validation/`, `rq1/`, `rq2/` and `rq2_3/`.
Successful comparison writes `results_reproduced/verification.json` with
`"passed": true`.

**Use a new output directory for each run.** The scripts refuse to overwrite
existing output directories. For a repeat run, choose another name, such as
`results_reproduced_2`.

### Run the components separately

The following commands together produce the same result-directory structure:

```powershell
python Validation/analysis.py --output results_manual/validation
python RQ1/RQ1_1/analysis.py --output results_manual/rq1
python RQ2/RQ2_1/analysis.py --output results_manual/rq2
python RQ2/RQ2_3/analysis.py --output results_manual/rq2_3
python verify.py --results results_manual
```

Validation needs only the included workbook and code. The other analyses need
the restored datasets. Verification expects all four result subdirectories.

`RQ1/RQ1_1/analysis.py` and `RQ1/RQ1_2/analysis.py` each run the shared RQ1
computation, producing both prevalence/context and multiplicity/co-occurrence
outputs. Likewise, `RQ2/RQ2_1/analysis.py` and `RQ2/RQ2_2/analysis.py` each fit both
duration endpoints and the success LPM. Run one entry point per shared analysis;
there is no need to run both aliases. Each script requires `--output`.

| Component | Main generated files |
| --- | --- |
| Validation | `evaluation.json` |
| RQ1 | `smell_prevalence.json`, `multiplicity.csv`, `cooccurrence.csv`, context and repository tables |
| RQ2 | `all_completed_duration.json`, `successful_duration.json`, `reliability_lpm.json` |
| RQ2.3 | `smell_resource_summary.json`, `result_audit.json`, `observed_monthly_frequency/` |
| Verification | `verification.json` |

### Rebuild RQ2 groups from individual runs

The default RQ2 entry point refits the models from archived exact sufficient
statistics. To also reconstruct grouping and outcome sums from all individual
run records, use a separate new output directory:

```powershell
python RQ2/analysis.py --output results_rq2_rebuilt --rebuild-groups
```

Or apply this option to the complete analysis:

```powershell
python reproduce.py --output results_rebuilt --rebuild-groups
```

### Optional detector rescan and figure generation

After restoring the workflow snapshots, a 100-workflow smoke scan is:

```powershell
python src/scan.py --output scan_smoke --workers 4 --limit 100
```

A full scan omits `--limit`; only a full scan can be used to reproduce population
results from newly generated detector labels:

```powershell
python src/scan.py --output scan_full --workers 4
python RQ1/analysis.py --scan scan_full/rq1_scan_direct.jsonl --output results_rq1_rescanned
```

To regenerate the two quantitative figure designs using restored figure source
data and the regenerated RQ2 results:

```powershell
python src/render_figures.py --results results_reproduced/rq2 --output figures_new
```

The figure output includes PDF/SVG/PNG exports and `figure_contract.json`.

## Data Acquisition, Filtering and Cleaning

This is optional for readers who only need to recompute the paper statistics.
For pipeline replay, restore the full data archive, install the pipeline
dependencies and obtain the original GHALogs candidate list.

GHALogs v1.0.1 supplies the initial repository candidates. Download only
[`repositories.json.gz`](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1)
from the [original record](https://zenodo.org/records/14796970). Its approximately
142 GB log archive and `runs.json.gz` are unnecessary for initial filtering.

Run from the repository root:

```powershell
python -c "from pathlib import Path; import urllib.request; p=Path('data/upstream/ghalogs/repositories.json.gz'); p.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve('https://zenodo.org/records/14796970/files/repositories.json.gz?download=1', str(p))"
python -c "import gzip, shutil; from pathlib import Path; p=Path('data/upstream/ghalogs'); shutil.copyfileobj(gzip.open(p/'repositories.json.gz', 'rb'), (p/'repositories.json').open('wb'))"
python -m pip install -r Pipeline/requirements.txt
python Pipeline/run.py offline-all --work-dir ../emse_pipeline_work
python Pipeline/verify_handoff.py --work-dir ../emse_pipeline_work
```

Skip download/extraction if a valid file already exists; extraction overwrites
the same filename. `repositories.json` is JSON Lines despite its extension.
The file is obtained separately, is not redistributed or relicensed here, and
should be credited to the original GHALogs creators.

`offline-all` runs `filter`, `canonical`, `consolidate`, `coverage`, `queue` and
`model-input` in sequence. Logs go to `../emse_pipeline_work/logs/`; the work
directory must be outside this repository. The replay uses frozen captured
inputs. Its model-input handoff reconstructs identities and run ordering before
the October detector refresh; use the root paper-analysis commands above for
the adopted October results.

Live collection is a separate optional operation requiring `GITHUB_TOKEN`, the
pipeline prerequisites and an explicit `--execute` flag. Historical availability
can change, so a fresh collection is not guaranteed to reproduce the frozen
corpus. See [`Pipeline/README.md`](Pipeline/README.md) for stage-specific and
online commands. No retired RQ3 operation is invoked.

## Data and Counting Rules

- `no_fallback_cache` is the current name. Historical `no_restore_keys` is an
  input alias, not an additional smell. Frozen historical fields are retained
  where required for provenance.
- Prevalence denominators depend on each smell's eligibility criteria. Adjacent
  summary tables include eligible-population fields; percentages should not be
  recomputed using one universal workflow denominator.
- Validation uses 176 eligible cases. Parenthesized unsupported-ecosystem
  annotations are excluded from `no_dependency_cache` evaluation as documented.
- Duration endpoints use log duration; success uses the adopted weighted LPM.
  Success coefficients describe probability differences, not odds ratios.
- Model inference uses repository clustering and endpoint-specific multiplicity
  correction. RQ2 estimates and RQ2.3 historical comparisons are observational.
- The `experimental` label in adopted frozen files is a historical directory/
  metadata flag; this package preserves the paper-selected implementations.

Field definitions and detailed mappings are in
[`docs/data_dictionary.md`](docs/data_dictionary.md) and
[`docs/paper_artifact_map.csv`](docs/paper_artifact_map.csv).

## Verification and Troubleshooting

The GitHub-edition checks are recorded in `docs/verification/github_layout.json`: the
176-case evaluation matched the frozen metrics, 11 original model tests passed,
and the data-restoration helper was checked on small extraction fixtures.
Reports in `docs/verification/original/` and `docs/verification/pipeline/` retain earlier complete-artifact checks;
they do not claim a new full-data execution in this code-only checkout.

| Message or situation | Action |
| --- | --- |
| Missing research data | Restore the matching complete ZIP with `data_setup.py --archive ...` |
| No configured download URL | The deposit is pending; supply a local ZIP or wait for the actual published URL |
| Output directory already exists | Choose a new `--output` directory |
| Result verification cannot find a subdirectory | Run validation, RQ1, RQ2 and RQ2.3 under the same parent, or use `reproduce.py` |
| Pipeline cannot find `repositories.json` | Obtain GHALogs separately and place it at the documented path |
| Full-package checksum option unavailable | The full checksum manifest belongs to the complete offline artifact, not this GitHub edition |

A new full-population detector scan and fresh-environment/cross-platform tests
were not performed during package assembly. The optional local prototype is
documented in [`src/tool/README.md`](src/tool/README.md); starting it is unnecessary for
paper replication.

## License and Citation

Author-owned code is licensed under **MIT** (`LICENSE`); author-owned derived
data and documentation use **CC BY 4.0** (`LICENSES/CC-BY-4.0.txt`). Both permit
research and commercial reuse under their terms. Exact scope and third-party/
manuscript exclusions are defined in [`docs/license_scope.md`](docs/license_scope.md).
Third-party workflow licensing review is deferred; no new license is asserted
over upstream materials.

Please cite the accompanying paper and the exact archived package version.
Creator metadata are in `CITATION.cff`. The paper DOI and package DOI will be
added only when assigned. See [`docs/zenodo_deposit.md`](docs/zenodo_deposit.md)
for the author's data-deposit procedure.
