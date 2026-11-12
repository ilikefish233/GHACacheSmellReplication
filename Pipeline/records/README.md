# Repository selection records

These three files are selected from the historical acquisition archive. Their
uncompressed SHA-256 hashes match [`Pipeline/data_manifest.json`](../data_manifest.json).

- `filter_stats.json.gz` records the repository filter criteria and counts.
- `filtered_repositories.json.gz` contains 22,911 repository records after the
  initial filter. It is JSON Lines inside gzip.
- `collected_repository_info.jsonl.gz` contains 22,659 subsequent repository
  metadata records collected for the study. It is JSON Lines inside gzip.
- `workflow_identity_audit.json` records the 131,009 cutoff source records,
  11,807 merged workflow identities, and 319 unidentified records.

The current manuscript specifies the GHALogs-based filter
criteria without giving either intermediate count. These files record successive
collection stages. Their count difference should not be interpreted as a
specific exclusion reason without a separate status audit.

GHALogs `repositories.json` is the upstream candidate list and must be obtained
from [its source](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1).
The other historical records listed in `Pipeline/data_manifest.json` remain in
the older research archive and are not included in this curated set.
