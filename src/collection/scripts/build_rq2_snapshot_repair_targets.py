"""Build targets whose stored workflow snapshot differs from the cutoff blob.

The history supplement identifies the latest commit touching each workflow path
before the study cutoff.  When that commit's blob differs from the stored
snapshot, the stored snapshot was collected after the cutoff and must be
replaced for a correct latest-at-cutoff RQ2 exposure.  This utility joins the
validated mismatch statuses with the corrected target metadata and emits only
one target per workflow identity.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


WorkflowKey = Tuple[str, str]


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                yield value


def key(record: Mapping[str, Any]) -> Optional[WorkflowKey]:
    repository = str(record.get("repository_name") or "").strip().lower()
    path = str(record.get("workflow_path") or record.get("path") or "").strip()
    if not repository or not path:
        return None
    return repository, path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build RQ2 cutoff-snapshot repair targets")
    parser.add_argument("--corrected-targets", type=Path, required=True)
    parser.add_argument("--old-targets", type=Path, required=True)
    parser.add_argument("--old-status", type=Path, action="append", default=[])
    parser.add_argument("--repair-status", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corrected = {target_key: dict(record) for record in load_jsonl(args.corrected_targets) if (target_key := key(record)) is not None}
    old_explicit = {
        target_key
        for record in load_jsonl(args.old_targets)
        if (target_key := key(record)) is not None
        and str(record.get("default_branch") or "").strip()
    }
    mismatch_by_key: Dict[WorkflowKey, Dict[str, Any]] = {}
    stats: Counter = Counter()

    def consume_status(path: Path, restrict_keys: Optional[set[WorkflowKey]]) -> None:
        for status in load_jsonl(path):
            stats["status_records_read"] += 1
            target_key = key(status)
            if target_key is None or status.get("status") != "current_snapshot_mismatch":
                continue
            if restrict_keys is not None and target_key not in restrict_keys:
                continue
            mismatch_by_key[target_key] = dict(status)
            stats["mismatch_statuses_used"] += 1

    for path in args.old_status:
        consume_status(path, old_explicit)
    for path in args.repair_status:
        consume_status(path, None)

    if args.output.exists():
        raise FileExistsError(f"Output already exists: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    selected = []
    for target_key, status in sorted(mismatch_by_key.items()):
        target = corrected.get(target_key)
        if target is None:
            stats["mismatch_without_corrected_target"] += 1
            continue
        target = {
            **target,
            "cutoff_commit_sha": status.get("commit_sha"),
            "cutoff_commit_date": status.get("commit_date"),
            "stored_snapshot_blob_sha": target.get("file_sha"),
            "repair_reason": "stored snapshot differs from latest pre-cutoff workflow blob",
        }
        selected.append(target)

    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for target in selected:
            handle.write(json.dumps(target, ensure_ascii=False) + "\n")
    metadata = {
        "corrected_targets": str(args.corrected_targets),
        "old_targets": str(args.old_targets),
        "old_status_sources": [str(path) for path in args.old_status],
        "repair_status_sources": [str(path) for path in args.repair_status],
        "output": str(args.output),
        "corrected_target_identities": len(corrected),
        "old_explicit_identities": len(old_explicit),
        "mismatch_identities": len(mismatch_by_key),
        "selected_targets": len(selected),
        **dict(stats),
        "external_fetch_performed": False,
    }
    args.output.with_name(args.output.stem + "_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
