# Shared implementation

Use the RQ/Validation/Pipeline entry points from the repository root. The source
modules below are shared by those entry points.

| Location | Role |
| --- | --- |
| `frozen/` | Byte-preserved October detector snapshot and its supporting source |
| `models/` | Byte-preserved adopted duration/LPM model implementations and their original tests |
| `analysis/` | Preserved paper analysis and plotting helpers |
| `resources/` | Preserved exploratory resource-comparison implementations |
| `collection/` | Preserved acquisition and curation source used by Pipeline entry points |
| `artifact.py` | Portable path, loading, naming and output helpers |
| `scan.py` | User-facing frozen-detector scan entry point |
| `render_figures.py` | User-facing quantitative figure entry point |
| `vendor/` | Included figure-alignment helper |
| `tool/` | Optional local UI bound to the frozen detector |
| `tests/` | Portable test entry points |

Do not run preserved historical orchestration files directly: their original
paths are retained as evidence. Portable adapters select the adopted study,
resolve current paths and avoid invoking retired RQ3 branches.

```powershell
python src/scan.py --help
python src/render_figures.py --help
python -I src/tests/run_models.py
```

See `docs/layout_map.json` at the repository root for the complete old/new path map.
