# Cache Configuration Smells in GitHub Actions: A Large-Scale Empirical Study

This repository provides the code, validation inputs, and expected results.
The bulk analysis data are distributed separately through Zenodo.

## Reproduce the results

The data DOI is [10.5281/zenodo.23640810](https://doi.org/10.5281/zenodo.23640810).
The DOI is reserved and the data deposit is pending publication. If you have
a local copy, use the extracted directory containing `manifest.json` as
`PATH_TO_ZENODO_DATA`.

From the code repository root, run these commands with Python 3.12:

```shell
python -m pip install -r requirements.txt
python data_setup.py --status --data-dir "PATH_TO_ZENODO_DATA"
python reproduce.py --data-dir "PATH_TO_ZENODO_DATA" --output results_reproduced
```

Choose a new output directory for each run. The preflight checks for required
files. The reproduction command runs the analyses offline and compares them
with the expected results. A successful comparison reports `"passed": true`
in `results_reproduced/verification.json`.

| Component | Entry point |
| --- | --- |
| [Validation](Validation/README.md) | `Validation/analysis.py` evaluates the detector on 383 annotated workflows using this checkout alone. |
| [RQ1](RQ1/README.md) | `RQ1/analysis.py` computes prevalence, contexts, multiplicity, and co-occurrence. |
| [RQ2.1 and RQ2.2](RQ2/README.md) | `RQ2/analysis.py` fits the duration and success models. |
| [RQ2.3](RQ2/RQ2_3/README.md) | `RQ2/RQ2_3/analysis.py` scans workflow histories, matches runs, and computes resource contrasts. |

Use `--rebuild-groups` with `reproduce.py` to reconstruct RQ2 model groups from
individual run records. The default fit uses archived grouped inputs.
The optional `--verify-collected-runs` flag checks every RQ2 model run key
against the consolidated collected runs in Zenodo before refitting the models.

## Source data and collection

The upstream GHALogs dataset supplies the candidate repository list in
[`repositories.json.gz`](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1). Extract `repositories.json`
from this file to start repository selection. The other study data were
collected from the selected repositories and then cleaned and prepared
for analysis. They are provided in the separate Zenodo data package.
The acquisition and preparation source is under [`src/collection/`](src/collection/).
The [collection stage map](Pipeline/README.md) records the inputs and current script paths.
RQ1 scans can be rebuilt from curated YAML with `RQ1/rebuild_scan.py`;
RQ1 exclusions can be rebuilt with `RQ1/audit_exclusions.py`; the RQ2 versioned
run input can be rebuilt from curated records with `RQ2/rebuild_input.py`.
Selected [repository selection records](Pipeline/records/README.md) are included for provenance.
The [paper methods and inputs map](docs/paper_traceability.md) links each reported stage to its code and retained evidence.

## RQ2.3 repository lists

The two lists were reconstructed for replication. Each contains 1,000 repositories.

- [repositories_batch1.txt](RQ2/RQ2_3/data/repositories_batch1.txt)
- [repositories_batch2.txt](RQ2/RQ2_3/data/repositories_batch2.txt)

See [data provenance](docs/data_provenance.md) for the origin of these lists.

## Zenodo data layout

`manifest.json` maps the analysis inputs to paths in the extracted data package.
`repositories/` contains repository metadata, `workflows/` contains workflow
indexes and YAML content, and `runs/` contains run records. Analysis-specific
inputs are in `analysis_inputs/`.

To rescan the archived workflow snapshots, run
`python src/scan.py --data-dir "PATH_TO_ZENODO_DATA" --output scan_results`.

## License and citation

Author-owned code uses the [MIT license](LICENSE). Author-owned derived data
and documentation use [CC BY 4.0](LICENSES/CC-BY-4.0.txt).
See the [license scope](docs/license_scope.md) for third-party materials.
Cite the accompanying paper and the Zenodo data version used.
Creator metadata are in [CITATION.cff](CITATION.cff).
