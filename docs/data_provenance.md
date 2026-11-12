# Data provenance

The study used [`repositories.json.gz`](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1)
from GHALogs as the upstream candidate repository list. The list was
filtered to select repositories. Workflow configurations, histories, runs,
and job records were then collected from GitHub for the selected repositories
and prepared for analysis. The reproduction workflow uses the resulting
cohorts and archived records in the Zenodo data package.

Detector validation uses `Validation/data/annotations.xlsx` and the bundled YAML
snapshots for truncated workbook cells. RQ1 uses saved detector outputs or a
rescan of archived workflow snapshots. RQ2 uses prepared workflow features
and grouped outcome statistics. The `--rebuild-groups` option recomputes the
groups from archived runs.

RQ2.3 uses the two [reconstructed repository lists](../RQ2/RQ2_3/data/).
The lists were assembled for replication to retain repositories represented in
the candidate-event data.
The analysis scans archived workflow versions, matches
CCS changes to archived runs and job records, and computes per-run and monthly
resource contrasts.

Historical acquisition and curation records are under `Pipeline/` and
`docs/provenance/`. The RQ1 historical comparison and trigger checks use the
files under `docs/provenance/legacy/`.
