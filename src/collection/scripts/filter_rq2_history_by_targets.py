"""Filter a derived RQ2 history supplement to an explicit target identity set.

This is used when an earlier supplement was generated with incomplete
repository metadata.  It keeps only history records whose
``(repository_name, workflow_path)`` appears in the supplied target list, so a
superseded/invalid supplement cannot influence the final latest-boundary
selection.
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
    parser = argparse.ArgumentParser(description="Filter RQ2 history records by target identities")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--require-nonempty-branch",
        action="store_true",
        help="Keep only targets carrying an explicit default_branch",
    )
    args = parser.parse_args()

    allowed = set()
    target_count = 0
    for target in load_jsonl(args.targets):
        target_count += 1
        if args.require_nonempty_branch and not str(target.get("default_branch") or "").strip():
            continue
        target_key = key(target)
        if target_key is not None:
            allowed.add(target_key)

    if args.output.exists():
        raise FileExistsError(f"Output already exists: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter({"target_records": target_count, "allowed_identities": len(allowed)})
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in load_jsonl(args.source):
            stats["source_records"] += 1
            if key(record) not in allowed:
                stats["filtered_records"] += 1
                continue
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            stats["kept_records"] += 1

    metadata = {
        "source": str(args.source),
        "targets": str(args.targets),
        "output": str(args.output),
        "require_nonempty_branch": args.require_nonempty_branch,
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
