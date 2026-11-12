# RQ2

See the root README and docs/data_dictionary.md for frozen inputs, counting rules and commands. All paths are package-relative; choose a new --output directory.

## Collected run coverage

Zenodo `runs/rq2_observation_runs.jsonl` contains the consolidated RQ2 30-day
run collection (2,132,983 unique attempts). Its companion manifest records
the original collection hash and selection window. Run this independent check
from the code package root:

```shell
python RQ2/check_collected_runs.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq2_collection_check
```

The check verifies the collection hash and confirms that all 1,093,639 run
keys in the archived version-aligned RQ2 model input occur in the collection.
It does not reconstruct the workflow/version matching or coverage decisions;
those remain embodied in the archived model input and collection-stage code.
Use `python reproduce.py --verify-collected-runs --data-dir ... --output ...`
to run this check together with the paper analyses.

## Detector exclusions and the paper sample

The archived RQ2 model input links 1,093,639 run attempts to workflow versions.
The paper model code in `src/models/rq2_unified_slim_20261018/run.py`
excludes versions whose detector returned an error, then excludes any version
whose workflow-feature extraction failed. In the adopted data, 82 detector-error
versions account for 1,661 run attempts; feature extraction had no errors.
This leaves the paper's common sample of 1,091,978 attempts from 14,695
repositories. The later outcome filters yield 1,089,617 duration attempts and
1,047,015 success attempts.

`expected/detector_excluded_runs.jsonl` lists the repository ID, run ID,
attempt, version ID, and exclusion reason for each of the 1,661 attempts.
`expected/detector_exclusion_audit.json` records the checked counts. Recreate
both files from the Zenodo model input and its two small prepared-data audits:

```shell
python RQ2/audit_detector_exclusions.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq2_exclusions
```

The script checks the archived sample audit and paper counts. Its output is a
new directory containing `detector_excluded_runs.jsonl` and
`exclusion_audit.json`.


## Rebuild the versioned run input

The optional offline reconstruction uses `runs/rq2_observation_runs.jsonl`,
canonical workflow/repository indexes, and the three original checkpoints
in Zenodo `analysis_inputs/intermediate/`. The checkpoints are required to
recreate the historical coverage decision and versioned input; they are not
additional outcomes. First check their presence, then create a new queue and
pre-run3 model input directory:

```shell
python RQ2/rebuild_input.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq2_rebuild --stage check
python RQ2/rebuild_input.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq2_rebuild --stage all
```

The original paper queue contains 51,179 workflow versions and 1,093,639
run attempts. The model conversion uses the archived canonical detector states.
The final paper instead uses run3 detector labels, retained in Zenodo
`analysis_inputs/rq2/model_input/version_scan_results.jsonl` and evaluated by
`RQ2/analysis.py`. The rebuild verifies the selected run/version population;
it does not claim that old canonical detector labels equal run3 labels.


### Check the rebuilt input and run3 labels

The queue reconstruction writes its large stage diagnostics to `queue.log` and
`model.log`. Compare its scientifically relevant run and version fields with
the paper's archived model input:

```shell
python RQ2/check_rebuilt_input.py --data-dir "PATH_TO_ZENODO_DATA" --rebuilt results_rq2_rebuild/model_input --output results_rq2_rebuild/identity_audit.json
```

The original canonical detector states used during model conversion precede
the paper's run3 detector. To independently rescan the archived version YAML
with the packaged run3 detector and compare formal CCS labels with the adopted
paper labels, run:

```shell
python RQ2/rescan_labels.py --data-dir "PATH_TO_ZENODO_DATA" --output results_rq2_run3_rescan
```

The scanner retains already documented failures when archived YAML content is
unavailable or does not match its recorded hash. A `--limit N` option supports
a smaller interface check. These checks trace the reconstructed population
to the current paper model; `RQ2/analysis.py` fits the paper model from its
archived run3 input.
