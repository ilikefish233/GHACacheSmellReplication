# Shared implementation

Use the RQ and Validation entry points from the repository root. The source
modules below support these analyses and the documented collection stages.

| Location | Role |
| --- | --- |
| `frozen/` | Selected October run3 detector iteration and required supporting source |
| `models/` | Byte-preserved adopted duration/LPM model implementations and their original tests |
| `analysis/` | Current paper analysis and plotting helpers |
| `resources/` | Preserved exploratory resource-comparison implementations |
| `collection/` | Acquisition and curation source mapped by `Pipeline/stages.json` |
| `artifact.py` | Portable path, loading, naming and output helpers |
| `scan.py` | User-facing frozen-detector scan entry point |
| `render_figures.py` | User-facing quantitative figure entry point |
| `vendor/` | Included figure-alignment helper |
| `tool/` | Optional local UI bound to the frozen detector |
| `tests/` | Portable test entry points |

`frozen/` is a version label, not a rule against maintenance. Retain code used
by current entry points; obsolete scripts can be removed after dependency and
result checks. Record and verify any detector/parser changes.

Do not run historical orchestration files directly: their original
paths are retained as evidence. Portable adapters select the adopted study,
resolve current paths and avoid invoking retired RQ3 branches.

```powershell
python src/scan.py --help
python src/render_figures.py --help
python -I src/tests/run_models.py
```

