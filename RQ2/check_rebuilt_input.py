"""Compare a rebuilt RQ2 model input with the adopted paper run/version set."""

import argparse
from itertools import zip_longest
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir

RUN_FIELDS = (
    "repository_id", "workflow_key", "run_id", "run_attempt", "version_id",
    "content_hash", "created_at", "status", "conclusion", "duration_seconds",
)
VERSION_FIELDS = (
    "repository_id", "workflow_path", "version_id", "content_hash",
)


def compare(left, right, fields):
    count = 0
    with left.open(encoding="utf-8") as a, right.open(encoding="utf-8") as b:
        for index, (x, y) in enumerate(zip_longest(a, b), start=1):
            if x is None or y is None:
                raise ValueError(f"Row count differs at {index}: {left.name}")
            source = json.loads(x)
            target = json.loads(y)
            for field in fields:
                if source.get(field) != target.get(field):
                    raise ValueError(f"{left.name} differs at row {index}, field {field}")
            count = index
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_data_dir(parser)
    parser.add_argument("--rebuilt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("Pass --data-dir with the extracted Zenodo directory")
    if args.output.exists():
        parser.error(f"Use a new report file: {args.output}")
    frozen = paths.path("RQ2/data")
    run_count = compare(args.rebuilt / "run_records.jsonl", frozen / "run_records.jsonl", RUN_FIELDS)
    version_count = compare(args.rebuilt / "version_records.jsonl", frozen / "version_records.jsonl", VERSION_FIELDS)
    report = {"run_records": run_count, "version_records": version_count,
              "scientific_fields_equal": run_count == 1_093_639 and version_count == 51_179,
              "comparison_scope": "run identity, timing, outcome, version identity, content hash; provenance paths and detector labels are compared separately"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["scientific_fields_equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
