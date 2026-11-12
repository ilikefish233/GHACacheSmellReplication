"""Rebuild the RQ2 detector/feature exclusion ledger from Zenodo model inputs."""

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
    scans = paths.path("RQ2/data/version_scan_results.jsonl")
    runs = paths.path("RQ2/data/run_records.jsonl")
    feature_audit = paths.path("RQ2/prepared/feature_audit.json")
    sample_audit = paths.path("RQ2/prepared/sample_audit.json")
    for path in (scans, runs, feature_audit, sample_audit):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if args.output.exists():
        parser.error(f"Use a new output directory: {args.output}")

    scan_versions = {}
    detector_errors = set()
    for row in rows(scans):
        version_id = row["version_id"]
        if version_id in scan_versions:
            raise ValueError(f"Duplicate version ID: {version_id}")
        scan_versions[version_id] = row
        if row["detector"].get("error"):
            detector_errors.add(version_id)
    features = json.loads(feature_audit.read_text(encoding="utf-8"))
    feature_errors = {row["version_id"] for row in features["feature_errors"]}
    invalid = detector_errors | feature_errors
    if not invalid <= set(scan_versions):
        raise ValueError("Feature audit refers to an unknown version")

    out = new_output(args.output)
    excluded_path = out / "detector_excluded_runs.jsonl"
    input_runs = excluded_runs = 0
    valid_repositories = set()
    excluded_versions = set()
    seen_runs = set()
    with excluded_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows(runs):
            input_runs += 1
            version_id = row["version_id"]
            if version_id not in scan_versions:
                raise ValueError(f"Run refers to an unknown version: {version_id}")
            key = (str(row["repository_id"]), str(row["run_id"]), str(row.get("run_attempt") or 1))
            if key in seen_runs:
                raise ValueError(f"Duplicate run key: {key}")
            seen_runs.add(key)
            if version_id in invalid:
                excluded_runs += 1
                excluded_versions.add(version_id)
                record = {
                    "repository_id": key[0],
                    "run_id": key[1],
                    "run_attempt": key[2],
                    "version_id": version_id,
                    "reason": "detector_error" if version_id in detector_errors else "feature_error",
                }
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                valid_repositories.add(str(row["repository_id"]))

    paper = json.loads(sample_audit.read_text(encoding="utf-8"))
    report = {
        "input_runs": input_runs,
        "versions": len(scan_versions),
        "detector_error_versions": len(detector_errors),
        "feature_error_versions": len(feature_errors),
        "excluded_versions_with_runs": len(excluded_versions),
        "excluded_detector_or_feature_error_runs": excluded_runs,
        "common_dataset_runs": input_runs - excluded_runs,
        "common_dataset_repositories": len(valid_repositories),
        "matches_archived_sample_audit": (
            input_runs == paper["input_runs"]
            and excluded_runs == paper["excluded_detector_or_feature_error_runs"]
            and input_runs - excluded_runs == paper["included_completed_runs"]
            and len(scan_versions) - len(invalid) == paper["version_count"]
            and len(detector_errors) == features["detector_error_versions"]
        ),
    }
    report["matches_paper"] = (
        report["matches_archived_sample_audit"]
        and input_runs == 1_093_639
        and excluded_runs == 1_661
        and input_runs - excluded_runs == 1_091_978
        and len(valid_repositories) == 14_695
    )
    write(out / "exclusion_audit.json", report)
    print(json.dumps(report, indent=2))
    if not report["matches_paper"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
