"""Recreate the RQ1 scan view from curated snapshots, YAML, and the run3 detector."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, new_output
from src.collection.scripts.prepare_canonical_experiment_inputs import (
    build_repo_info, inferred_cache_detector, make_scan_record,
)


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Pass --data-dir with the extracted Zenodo directory")
    canonical = paths.path("data/canonical")
    cohort_path = paths.path("data/canonical/rq1_cohort.jsonl")
    snapshots_path = paths.path("data/canonical/workflow_snapshots.jsonl")
    states_path = paths.path("RQ2/canonical_detector_states")
    repositories_path = paths.path("data/canonical/repositories.jsonl")
    for path in (cohort_path, snapshots_path, states_path, repositories_path):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.output.exists():
        parser.error(f"Use a new output directory: {args.output}")

    cohort = list(rows(cohort_path))
    if len(cohort) != 118_523:
        raise ValueError("RQ1 cohort count differs from the paper input")
    if args.limit:
        cohort = cohort[:args.limit]
    keys = {row["workflow_key"] for row in cohort}
    state_keys = {row["detector_state_key"] for row in cohort}
    snapshots = {row["workflow_key"]: row for row in rows(snapshots_path) if row["workflow_key"] in keys}
    states = {row["state_key"]: row for row in rows(states_path) if row["state_key"] in state_keys}
    if len(snapshots) != len(cohort) or len(states) != len(state_keys):
        raise ValueError("Missing canonical snapshot or detector state for the RQ1 cohort")
    prepared = []
    for row in cohort:
        snapshot = snapshots[row["workflow_key"]]
        if snapshot["content_hash"] != row["content_hash"]:
            raise ValueError(f"Content hash mismatch: {row['workflow_key']}")
        prepared.append(make_scan_record(canonical, snapshot, states[row["detector_state_key"]]))

    out = new_output(args.output)
    repositories = {str(row["repository_id"]): row for row in rows(repositories_path)}
    with (out / "repo_info.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in build_repo_info(canonical, repositories):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    baseline = out / "baseline_scan.jsonl"
    with baseline.open("w", encoding="utf-8", newline="\n") as stream:
        for row in prepared:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    rescan_dir = out / "rescan"
    subprocess.run([
        sys.executable, str(PACKAGE / "src" / "scan.py"),
        "--data-dir", str(paths.root), "--input", str(baseline),
        "--output", str(rescan_dir), "--workers", str(args.workers),
    ], cwd=PACKAGE, check=True)
    errors = 0
    with (out / "rq1_scan_direct.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows(rescan_dir / "rq1_scan_direct.jsonl"):
            state = row["detectors"]["cache_smell"]
            row["detectors"]["cache"] = inferred_cache_detector(state)
            row["summary"] = {"smells_by_type": state.get("smells_by_type") or {}}
            if state.get("error"):
                errors += 1
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / "scan_summary.json").write_text(json.dumps({
        "cohort_records": len(cohort), "detector_errors": errors,
        "paper_workflows": len(cohort) - errors, "limited": args.limit is not None,
    }, indent=2) + "\n", encoding="utf-8")
    if args.limit is None and (errors, len(cohort) - errors) != (121, 118_402):
        raise ValueError("Full run3 scan counts differ from the paper")


if __name__ == "__main__":
    main()
