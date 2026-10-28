# Repository layout

The top level contains eight directories: `RQ1`, `RQ2`, `Validation`, `Pipeline`,
`src`, `data`, `docs` and `LICENSES`. Portable RQ/validation/pipeline entry points
remain user-facing. Shared implementations are under `src`. Documentation,
manuscript assets, original-source metadata and verification reports are under
`docs`.

`layout_map.json` maps every path in the earlier GitHub edition to its new path.
The `src/frozen` name identifies the selected run3 iteration, not an immutable
archive. Source retention follows current entry-point dependencies and paper-result
verification; superseded scripts may be removed. Portable adapters supply current
locations and import roots. Historical manifests describe earlier versions rather
than requiring every historical file to remain in this distribution.
The legacy folder names in those files and historical JSON reports describe the
original experiments. They are not additional required top-level directories.

The external data paths selected by `data_setup.py` are unchanged. That helper
restores selected data only. A separately distributed complete package should
incorporate current code, validation inputs and documentation when repackaged. Repository code
and documentation are not overwritten. Source settings now live in
`data/sources.json`. Archive publication URLs/DOIs remain pending.

Completed relocation checks are recorded in `verification/github_layout.json`.
