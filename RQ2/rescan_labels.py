"""Rescan RQ2 workflow YAML with the packaged run3 detector and compare labels."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, new_output

ALIASES = {
    "no_restore_keys": "no_fallback_cache",
    "conflicting_cache_sources": "overlapping_cache_mechanisms",
    "disposable_cache_key": "overly_specific_cache_key",
}


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def labels(detector):
    return {ALIASES.get(name, name): count for name, count in
            (detector.get("smells_by_type") or {}).items()}


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
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.output.exists():
        parser.error(f"Use a new output directory: {args.output}")
    reference = paths.path("RQ2/data/version_scan_results.jsonl")
    out = new_output(args.output)
    baseline = out / "baseline_scan.jsonl"
    reference_rows = []
    with baseline.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows(reference):
            reference_rows.append(row)
            candidate = dict(row)
            candidate["detectors"] = {"cache_smell": candidate.pop("detector")}
            stream.write(json.dumps(candidate, ensure_ascii=False) + "\n")
            if args.limit and len(reference_rows) >= args.limit:
                break
    if not args.limit and len(reference_rows) != 51_179:
        raise ValueError("Paper RQ2 version count differs")
    scanned = out / "scan"
    subprocess.run([
        sys.executable, str(PACKAGE / "src" / "scan.py"),
        "--data-dir", str(paths.root), "--input", str(baseline),
        "--output", str(scanned), "--workers", str(args.workers),
    ], cwd=PACKAGE, check=True)
    mismatches = errors = 0
    output = out / "version_scan_results.jsonl"
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for original, new in zip(reference_rows, rows(scanned / "rq1_scan_direct.jsonl"), strict=True):
            detector = new["detectors"]["cache_smell"]
            expected = original["detector"]
            if labels(detector) != labels(expected) or bool(detector.get("error")) != bool(expected.get("error")):
                mismatches += 1
            if detector.get("error"):
                errors += 1
            original["detector"] = detector
            stream.write(json.dumps(original, ensure_ascii=False) + "\n")
    report = {"versions": len(reference_rows), "detector_error_versions": errors,
              "paper_label_mismatches": mismatches, "limited": args.limit is not None}
    (out / "rescan_audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if mismatches or (args.limit is None and errors != 82):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
