# Distribution layout

The complete ZIP is the authoritative offline artifact for the default paper analyses and contains both data and code. Replaying initial candidate filtering additionally requires the external GHALogs file described in the root README. Preserve the directory structure when extracting it. Do not flatten the RQ directories or rename frozen files independently of their manifests.

For a code repository, keep the README, requirements, executable entry points, frozen source, expected small tables and documentation in version control. The full frozen datasets can be deposited separately while retaining the same package-relative paths. Document the actual permanent deposit URL, exact archive version and checksum once created. Until a real deposit exists, the package intentionally contains no fabricated download URL or DOI.

Large-data paths are `data/canonical/`, `RQ1/data/`, `RQ2/data/`, `RQ2/prepared/` and `RQ2/RQ2_3/data/` and `Pipeline/data/`. `RQ1/paper_source_data/` provides the numerical figure data; it can remain with code or be part of the data deposit. A code-only copy without those datasets is not a self-contained replication package.

Keep `provenance` manifests, `package_inventory.json`, `checksums.sha256`, the selected license texts and final citation metadata with the released version. The local package's release checklist states which author decisions remain. No upload or external publication was performed during assembly.

The original GHALogs candidate list is excluded from distribution and from package checksums. Author-owned software is MIT; author-owned derived data and documentation are CC BY 4.0, with third-party exceptions in `docs/license_scope.md`. Detailed Zenodo upload, mixed-license metadata and versioning instructions are in `docs/zenodo_deposit.md`.
