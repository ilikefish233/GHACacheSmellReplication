# Repository layout

The top level contains eight directories: `RQ1`, `RQ2`, `Validation`, `Pipeline`,
`src`, `data`, `docs` and `LICENSES`. Portable RQ/validation/pipeline entry points
remain user-facing. Shared implementations are under `src`. Documentation,
manuscript assets, original-source metadata and verification reports are under
`docs`.

`layout_map.json` maps every path in the earlier GitHub edition to its new path.
Frozen detector, model, analysis-helper and collection Python/JSON sources retain
their original bytes. Portable adapters supply new locations and import roots.
The legacy folder names in those files and historical JSON reports describe the
original experiments. They are not additional required top-level directories.

The companion full-data ZIP does not need rebuilding: its external data paths
are unchanged, and `data_setup.py` only restores selected data. Repository code
and documentation are not overwritten. Source settings now live in
`data/sources.json`. Archive publication URLs/DOIs remain pending.

Completed relocation checks are recorded in `verification/github_layout.json`.
