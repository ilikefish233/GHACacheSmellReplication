"""Keep a reproducible, per-workflow cap for API-supplemented runs.

The original collection retains at most 95 runs per workflow.  The API
supplementer intentionally keeps all returned records for auditability; this
utility creates the analysis input with the same per-workflow cap without
modifying that raw supplement.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON at {path}:{line_number}: {exc}') from exc
            if isinstance(value, dict):
                yield value


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def run_key(run: Mapping[str, Any]) -> Tuple[str, str]:
    return (
        str(run.get('repository_id') or run.get('repository_name') or ''),
        str(run.get('workflow_path') or ''),
    )


def cap_runs(input_path: Path, output_path: Path, max_per_workflow: int) -> Dict[str, Any]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    raw_records = 0
    for run in iter_jsonl(input_path):
        raw_records += 1
        groups[run_key(run)].append(run)

    selected: List[Dict[str, Any]] = []
    for key, runs in groups.items():
        ordered = sorted(
            runs,
            key=lambda item: (
                timestamp(item.get('created_at')),
                str(item.get('run_id') or ''),
                str(item.get('run_attempt') or '1'),
            ),
            reverse=True,
        )
        selected.extend(ordered[:max(1, max_per_workflow)])
    selected.sort(key=lambda item: (
        str(item.get('repository_name') or '').lower(),
        str(item.get('workflow_path') or ''),
        timestamp(item.get('created_at')),
        str(item.get('run_id') or ''),
    ))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8') as handle:
        for run in selected:
            handle.write(json.dumps(run, ensure_ascii=False, allow_nan=False) + '\n')
    metadata = {
        'input': str(input_path),
        'output': str(output_path),
        'raw_records': raw_records,
        'workflow_groups': len(groups),
        'selected_records': len(selected),
        'max_per_workflow': max(1, max_per_workflow),
        'selection': 'latest records by created_at, then run_id and run_attempt',
    }
    output_path.with_name('supplemental_workflow_runs_latest95_metadata.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-per-workflow', type=int, default=95)
    args = parser.parse_args()
    print(json.dumps(cap_runs(args.input, args.output, args.max_per_workflow), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
