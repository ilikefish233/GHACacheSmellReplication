"""Rebuild the RQ1 exclusion ledger from archived snapshot and scan records."""

import argparse
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, new_output, write


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Pass --data-dir with the extracted Zenodo directory")
    snapshots_path = paths.path("data/canonical/workflow_snapshots.jsonl")
    cohort_path = paths.path("data/canonical/rq1_cohort.jsonl")
    scan_path = paths.path("RQ1/data/rq1_scan_direct.jsonl")
    for path in (snapshots_path, cohort_path, scan_path):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if args.output.exists():
        parser.error(f"Use a new output directory: {args.output}")

    snapshots = {row["workflow_key"]: row for row in rows(snapshots_path)}
    cohort = {row["workflow_key"]: row for row in rows(cohort_path)}
    if len(snapshots) != 118_883 or len(cohort) != 118_523:
        raise ValueError("Canonical snapshot or cohort count differs from the paper")
    if not set(cohort) <= set(snapshots):
        raise ValueError("RQ1 cohort contains an unknown workflow")

    out = new_output(args.output)
    excluded_path = out / "excluded_workflows.jsonl"
    detector_errors = 0
    scanned = set()
    with excluded_path.open("w", encoding="utf-8", newline="\n") as stream:
        for key in sorted(set(snapshots) - set(cohort)):
            stream.write(json.dumps({"workflow_key": key, "reason": "not_in_rq1_cohort"}, separators=(",", ":")) + "\n")
        for row in rows(scan_path):
            key = row["workflow_key"]
            if key not in cohort or key in scanned:
                raise ValueError(f"Unexpected or duplicate scan workflow: {key}")
            scanned.add(key)
            state = row["detectors"]["cache_smell"]
            if state.get("error"):
                detector_errors += 1
                stream.write(json.dumps({"workflow_key": key, "reason": "detector_error"}, separators=(",", ":")) + "\n")
    if scanned != set(cohort):
        raise ValueError("RQ1 scan does not cover the whole cohort")
    report = {
        "canonical_snapshots": len(snapshots),
        "rq1_cohort": len(cohort),
        "not_in_rq1_cohort": len(snapshots) - len(cohort),
        "detector_error_workflows": detector_errors,
        "excluded_total": len(snapshots) - len(cohort) + detector_errors,
        "paper_workflows": len(cohort) - detector_errors,
    }
    report["matches_paper"] = (
        report["not_in_rq1_cohort"] == 360
        and report["detector_error_workflows"] == 121
        and report["excluded_total"] == 481
        and report["paper_workflows"] == 118_402
    )
    write(out / "exclusion_audit.json", report)
    print(json.dumps(report, indent=2))
    if not report["matches_paper"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
