"""Run the active cache-smell detector on API-supplemented snapshots.

The generated records use one ``version_id`` per ``(repository, workflow,
head_sha, content_hash)`` and retain the run provenance created by
``supplement_workflow_snapshots.py``.  They can therefore be consumed by the
run-level RQ2 tooling without treating a historical YAML as the current
workflow identity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.detectors.smell.cache_smell_detector import CacheSmellDetector  # noqa: E402


DEFAULT_INPUT_DIR = (
    REPOSITORY_ROOT
    / 'output_full_logical_20260831'
    / 'rq2_snapshot_supplement_20260609'
)
DEFAULT_DATA_DIR = REPOSITORY_ROOT / 'data_26_6_9'


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
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


def timestamp(value: Any) -> Optional[str]:
    return str(value) if isinstance(value, str) and value else None


def version_id(entry: Mapping[str, Any]) -> str:
    return (
        f"{entry.get('repository_name', '')}|{entry.get('workflow_path', '')}|"
        f"{entry.get('head_sha', '')}|{entry.get('content_hash', '')}"
    )


_WORKER_INPUT_DIR: Optional[Path] = None
_WORKER_DETECTOR: Optional[CacheSmellDetector] = None


def _init_worker(input_dir: str) -> None:
    global _WORKER_INPUT_DIR, _WORKER_DETECTOR
    _WORKER_INPUT_DIR = Path(input_dir)
    _WORKER_DETECTOR = CacheSmellDetector(workflows_dir=_WORKER_INPUT_DIR / 'workflows')


def _scan_snapshot_entry(entry: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, bool]:
    """Scan one snapshot in a worker process and return its output record."""

    if _WORKER_INPUT_DIR is None or _WORKER_DETECTOR is None:
        raise RuntimeError('snapshot scanner worker was not initialized')

    content_file = str(entry.get('content_file') or '')
    yaml_path = _WORKER_INPUT_DIR / Path(content_file)
    result: Dict[str, Any]
    missing_content_file = False
    detector_error = False
    if not yaml_path.exists():
        missing_content_file = True
        result = {
            'detector_name': 'cache_smell',
            'has_smell': False,
            'smells': [],
            'metadata': {},
            'error': f'missing supplemented content file: {yaml_path}',
        }
    else:
        try:
            result = _WORKER_DETECTOR.detect_from_file(
                yaml_path,
                workflow_path=str(entry.get('workflow_path') or ''),
            ).to_dict()
            detector_error = bool(result.get('error'))
        except Exception as exc:  # preserve one bad YAML in the batch
            detector_error = True
            result = {
                'detector_name': 'cache_smell',
                'has_smell': False,
                'smells': [],
                'metadata': {},
                'error': str(exc),
            }

    output_entry = {
        'version_id': version_id(entry),
        'repository_name': entry.get('repository_name'),
        'source_repository_name': entry.get('source_repository_name'),
        'workflow_path': entry.get('workflow_path'),
        'commit_sha': entry.get('head_sha'),
        'commit_date': entry.get('run_created_at_min'),
        'next_commit_date': None,
        'content_hash': entry.get('content_hash'),
        'content_file': str(yaml_path.resolve()),
        'snapshot_as_of': entry.get('snapshot_as_of'),
        'matched_run_ids': entry.get('matched_run_ids') or [],
        'matched_run_count': entry.get('matched_run_count', 0),
        'commit_date_is_run_time_proxy': True,
        'detector': result,
    }
    return output_entry, missing_content_file, detector_error


def _count_jsonl_records(path: Path) -> int:
    with path.open('r', encoding='utf-8') as handle:
        return sum(1 for line in handle if line.strip())


def scan_snapshots(
    input_dir: Path,
    progress_every: int = 250,
    workers: int = 1,
) -> Dict[str, Any]:
    source_path = input_dir / 'supplemental_workflow_snapshots.jsonl'
    output_path = input_dir / 'supplemented_version_scan_results.jsonl'
    if not source_path.exists():
        raise FileNotFoundError(source_path)

    total_records = _count_jsonl_records(source_path)
    stats: Dict[str, Any] = {
        'snapshot_records_read': 0,
        'scanned': 0,
        'missing_content_files': 0,
        'detector_errors': 0,
        'workers': max(1, int(workers)),
    }
    with output_path.open('w', encoding='utf-8') as output:
        entries = load_jsonl(source_path)
        if workers > 1:
            scanner = ProcessPoolExecutor(
                max_workers=max(1, int(workers)),
                initializer=_init_worker,
                initargs=(str(input_dir.resolve()),),
            )
            scanned_entries = scanner.map(_scan_snapshot_entry, entries, chunksize=32)
        else:
            _init_worker(str(input_dir.resolve()))
            scanner = None
            scanned_entries = map(_scan_snapshot_entry, entries)
        try:
            for entry_result, missing_content_file, detector_error in scanned_entries:
                stats['snapshot_records_read'] += 1
                if missing_content_file:
                    stats['missing_content_files'] += 1
                if detector_error:
                    stats['detector_errors'] += 1
                output.write(json.dumps(entry_result, ensure_ascii=False) + '\n')
                output.flush()
                stats['scanned'] += 1
                if progress_every > 0 and stats['scanned'] % progress_every == 0:
                    print(
                        f'扫描补充版本 {stats["scanned"]}/{total_records}',
                        flush=True,
                    )
        finally:
            if scanner is not None:
                scanner.shutdown()

    metadata_path = input_dir / 'supplemented_scan_metadata.json'
    metadata = {
        **stats,
        'source_snapshot_records': str(source_path),
        'version_scan_results': str(output_path),
        'detector': 'src.detectors.smell.cache_smell_detector.CacheSmellDetector',
        'version_identity': '(repository_name, workflow_path, head_sha, content_hash)',
        'source_snapshot_record_count': total_records,
        'commit_date_note': (
            'commit_date is the earliest matched run created_at for the head_sha; '
            'it is a conservative run-time proxy because the supplement fetches '
            'contents at an exact commit but does not fetch commit metadata'
        ),
    }
    with metadata_path.open('w', encoding='utf-8') as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description='Scan supplemented workflow snapshots')
    parser.add_argument('--input-dir', type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument('--progress-every', type=int, default=250)
    parser.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()
    scan_snapshots(args.input_dir, args.progress_every, max(1, args.workers))


if __name__ == '__main__':
    main()
