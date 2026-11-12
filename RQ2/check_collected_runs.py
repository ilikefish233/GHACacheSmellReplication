"""Check that the archived RQ2 collection contains every paper model run."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, new_output, write


def run_key(row):
    return (str(row["repository_id"]), str(row["run_id"]), str(row.get("run_attempt") or 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Pass --data-dir with the extracted Zenodo directory")
    collected = paths.path("RQ2/collected_runs")
    manifest_path = paths.path("RQ2/collected_runs_manifest")
    model_input = paths.path("RQ2/data/run_records.jsonl")
    for path in (collected, manifest_path, model_input):
        if not path.is_file():
            parser.error(f"Missing input: {path}")
    if args.output.exists():
        parser.error(f"Use a new output directory: {args.output}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = set()
    selected_rows = 0
    with model_input.open("rb") as stream:
        for line in stream:
            if line.strip():
                selected.add(run_key(json.loads(line)))
                selected_rows += 1

    missing = set(selected)
    digest = hashlib.sha256()
    collected_rows = 0
    with collected.open("rb") as stream:
        for line in stream:
            digest.update(line)
            if line.strip():
                collected_rows += 1
                missing.discard(run_key(json.loads(line)))

    report = {
        "collected_file": collected.relative_to(paths.root).as_posix(),
        "collected_rows": collected_rows,
        "collected_sha256": digest.hexdigest(),
        "model_input_rows": selected_rows,
        "model_input_unique_run_keys": len(selected),
        "model_input_keys_found": len(selected) - len(missing),
        "model_input_keys_missing": len(missing),
        "missing_key_examples": [list(key) for key in sorted(missing)[:5]],
    }
    report["passed"] = (
        collected_rows == manifest["record_count"]
        and digest.hexdigest() == manifest["sha256"]
        and collected.stat().st_size == manifest["size_bytes"]
        and selected_rows == manifest["paper_model_input_run_count"]
        and len(selected) == selected_rows
        and not missing
    )
    out = new_output(args.output)
    write(out / "coverage.json", report)
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
