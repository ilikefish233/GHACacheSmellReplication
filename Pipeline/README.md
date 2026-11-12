# Data collection and preparation stages

[stages.json](stages.json) maps each historical stage to source files in the
current GitHub layout. The source is under [`src/collection/`](../src/collection/).
The stage counts record the original execution and are not promises of the
counts returned by a new GitHub collection. The historical canonical and
handoff wrappers are absent; the stage list is a provenance map, not a single
command that runs the complete acquisition pipeline.

The upstream candidate list is [`repositories.json.gz`](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1)
from GHALogs. Extract `repositories.json` before repository filtering. The
workflow and run data were collected from the selected repositories through
GitHub. Collection steps that query GitHub require network access and may
observe different repository states on a later run.

[records/](records/) includes the filter result, filter statistics, and the
subsequent repository metadata file.

[data_manifest.json](data_manifest.json) records 94 archived outputs from the
original acquisition and curation stages, including their original paths and
SHA-256 hashes. Those archived files are in the older complete-package ZIP
under `Pipeline/data/`. They are not included in the current Zenodo data
package, which holds curated cohorts, analysis inputs, and the three RQ2 rebuild checkpoints.
The supported offline reconstruction entry point is `RQ2/rebuild_input.py`.
It begins with the curated repository, workflow, and consolidated run records. To replay
cleaning from the original collected records, the 94 files must be restored
from that archive and mapped to the script inputs. The current
[`reproduce.py`](../reproduce.py) starts from the prepared Zenodo data.
