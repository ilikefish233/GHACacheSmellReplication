"""Rebuild the RQ2 version/run queue and pre-run3 model input from Zenodo data.

The final paper's run3 detector labels remain in RQ2/data/version_scan_results.jsonl;
this command verifies the selected run and version identities feeding that checkpoint.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir


def run(command, log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, *map(str, command)], cwd=PACKAGE,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    print(f"Completed {command[0].name}; details: {log_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("check", "queue", "model", "all"), default="all")
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Pass --data-dir with the extracted Zenodo directory")
    needed = {
        "manifest": paths.path("data/canonical/dataset_manifest.json"),
        "repositories": paths.path("data/canonical/repositories.jsonl"),
        "snapshots": paths.path("data/canonical/workflow_snapshots.jsonl"),
        "rq1": paths.path("data/canonical/rq1_cohort.jsonl"),
        "boundaries": paths.path("data/canonical/workflow_boundaries.jsonl"),
        "runs": paths.path("RQ2/collected_runs"),
        "rq2": paths.path("data/canonical/rq2_cohort.jsonl"),
        "status": paths.path("RQ2/coverage_status"),
        "evidence": paths.path("RQ2/coverage_evidence"),
        "states": paths.path("RQ2/canonical_detector_states"),
    }
    missing = [str(path) for path in needed.values() if not path.is_file()]
    if missing:
        parser.error("Missing input files:\n" + "\n".join(missing))
    inventory = json.loads(paths.path("RQ2/intermediate_manifest").read_text(encoding="utf-8"))["files"]
    for name, item in inventory.items():
        if (paths.root / "analysis_inputs" / "intermediate" / name).stat().st_size != item["size_bytes"]:
            parser.error(f"Intermediate input size mismatch: {name}")
    output = args.output.resolve()
    queue = output / "queue"
    model = output / "model_input"
    if args.stage == "check":
        print(json.dumps({"inputs_present": True, "intermediate_size_checks": len(inventory)}, indent=2))
        return
    if args.stage in ("queue", "all"):
        if output.exists() and any(output.iterdir()):
            parser.error(f"Use a new output directory for the queue: {output}")
        run([
            PACKAGE / "src/collection/scripts/audit_rq2_canonical_coverage.py",
            "--manifest", needed["manifest"],
            "--repositories", needed["repositories"],
            "--snapshots", needed["snapshots"],
            "--rq1", needed["rq1"],
            "--boundaries", needed["boundaries"],
            "--runs", needed["runs"],
            "--rq2", needed["rq2"],
            "--status-file", needed["status"],
            "--coverage-evidence", needed["evidence"],
            "--output-dir", queue,
        ], output / "queue.log")
        summary = json.loads((queue / "rq2_coverage_summary.json").read_text(encoding="utf-8"))
        if (summary["coverage_confirmed_final_workflows"], summary["coverage_confirmed_final_runs"]) != (51_179, 1_093_639):
            raise ValueError("Rebuilt queue counts differ from the original paper input")
    if args.stage in ("model", "all"):
        for name in ("rq2_final_analysis_workflows.jsonl", "rq2_final_analysis_runs.jsonl"):
            if not (queue / name).is_file():
                parser.error(f"Missing rebuilt queue file: {queue / name}")
        if model.exists() and any(model.iterdir()):
            parser.error(f"Use a new model output directory: {model}")
        run([
            PACKAGE / "src/collection/scripts/prepare_rq2_final_model_input.py",
            "--workflows", queue / "rq2_final_analysis_workflows.jsonl",
            "--runs", queue / "rq2_final_analysis_runs.jsonl",
            "--rq1", needed["rq1"],
            "--detector-states", needed["states"],
            "--output-dir", model,
        ], output / "model.log")
        metadata = json.loads((model / "matching_metadata.json").read_text(encoding="utf-8"))
        counts = metadata["counts"]
        if (counts["version_records"], counts["run_records"]) != (51_179, 1_093_639):
            raise ValueError("Rebuilt model input counts differ from the paper input")
    print(json.dumps({"stage": args.stage, "queue": str(queue), "model_input": str(model),
                      "paper_run3_labels": str(paths.path("RQ2/data/version_scan_results.jsonl"))}, indent=2))


if __name__ == "__main__":
    main()
