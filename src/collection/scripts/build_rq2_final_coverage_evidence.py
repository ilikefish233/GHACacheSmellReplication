"""Build the full offline RQ2 coverage-evidence file for Phase 3.1.

The base evidence is retained row-for-row and the consolidated Phase 3.0
workflow status is overlaid by ``(repository_id, normalized workflow_path)``.
This script is local-only and never creates a GitHub client or edits canonical
inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = (
    ROOT
    / 'output_canonical_rq2_query_coverage_phase1_2_1_20260919'
    / 'rq2_query_coverage_evidence.jsonl'
)
DEFAULT_LATEST = (
    ROOT
    / 'output_rq2_unified_acquisition_consolidated_20260920'
    / 'workflow_acquisition_status.jsonl'
)
DEFAULT_OUTPUT_DIR = ROOT / 'output_canonical_rq2_phase3_1_final_queue_20260920'
DEFAULT_OUTPUT_FILE = 'rq2_final_coverage_evidence.jsonl'

TERMINAL_QUERY_STATUSES = {'terminal_exclusion', 'access_denied'}
COMPLETED_QUERY_STATUSES = {'complete', 'no_qualifying_runs'}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve()).replace('\\', '/')


def normalize_path(value: Any) -> str:
    text = str(value or '').strip().replace('\\', '/')
    if not text:
        return ''
    normalized = posixpath.normpath(text)
    return '' if normalized == '.' else normalized


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON at {path}:{line_number}: {exc}') from exc
            if not isinstance(row, dict):
                raise ValueError(f'Expected JSON object at {path}:{line_number}')
            yield row


def workflow_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    repository_id = str(row.get('repository_id') or '').strip()
    workflow_path = normalize_path(row.get('workflow_path'))
    if not repository_id or not workflow_path:
        raise ValueError('Coverage evidence/status row is missing repository_id or workflow_path')
    return repository_id, workflow_path


def status_overlay(
    row: Mapping[str, Any],
    latest_source: str,
    latest_sha256: str,
) -> Dict[str, Any]:
    execution_status = str(row.get('execution_status') or '').strip()
    if execution_status in COMPLETED_QUERY_STATUSES:
        query_status = 'complete'
        coverage_confirmed = True
        observed_status = 'no_runs' if execution_status == 'no_qualifying_runs' else 'ok'
        confirmation_reason = 'phase3_0_consolidated_terminal_query_complete'
        unconfirmed_reason = None
    elif execution_status in TERMINAL_QUERY_STATUSES:
        query_status = execution_status
        coverage_confirmed = False
        observed_status = 'not_found' if execution_status == 'terminal_exclusion' else 'forbidden'
        confirmation_reason = None
        unconfirmed_reason = (
            'workflow_endpoint_terminally_unavailable'
            if execution_status == 'terminal_exclusion'
            else 'repository_or_workflow_access_denied'
        )
    else:
        raise ValueError(
            f'Latest consolidated status is not terminal: {execution_status!r} '
            f'for {row.get("repository_id")}|{row.get("workflow_path")}'
        )

    return {
        'query_status': query_status,
        'query_coverage_status': query_status,
        'coverage_confirmed': coverage_confirmed,
        'coverage_confirmation_reason': confirmation_reason,
        'coverage_unconfirmed_reason': unconfirmed_reason,
        'query_status_source': latest_source,
        'query_status_source_file_sha256': latest_sha256,
        'query_window_days': 30,
        'query_pages': row.get('pages'),
        'query_records': row.get('records'),
        'query_fetched_at': row.get('finished_at') or row.get('fetched_at'),
        'query_error': row.get('error'),
        'query_observed_status': observed_status,
        'query_termination_reason': row.get('termination_reason'),
        'query_last_page_size': row.get('last_page_size'),
        'query_endpoint_type': row.get('query_mode'),
        'query_pagination_complete': bool(row.get('pagination_complete')),
        'current_qualifying_run_count': int(row.get('records') or 0),
        'latest_acquisition_status': execution_status,
        'latest_acquisition_source_directories': row.get('status_source_directories') or [],
        'latest_acquisition_source_file': latest_source,
        'latest_acquisition_source_file_sha256': latest_sha256,
    }


def build(
    base_path: Path,
    latest_path: Path,
    output_dir: Path,
    output_name: str = DEFAULT_OUTPUT_FILE,
) -> Dict[str, Any]:
    if not base_path.is_file():
        raise FileNotFoundError(base_path)
    if not latest_path.is_file():
        raise FileNotFoundError(latest_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name

    latest_source = source_label(latest_path)
    latest_sha256 = sha256_file(latest_path)
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    latest_rows = 0
    for row in load_jsonl(latest_path):
        latest_rows += 1
        key = workflow_key(row)
        if key in latest:
            raise ValueError(f'Duplicate latest status key: {key}')
        latest[key] = row

    base_rows = 0
    matched_rows = 0
    terminal_counts: Dict[str, int] = {}
    coverage_confirmed_count = 0
    analysis_coverage_confirmed_keys = set()
    base_keys = set()
    output_rows: List[Dict[str, Any]] = []
    for base in load_jsonl(base_path):
        base_rows += 1
        key = workflow_key(base)
        if key in base_keys:
            raise ValueError(f'Duplicate base evidence key: {key}')
        base_keys.add(key)
        output = dict(base)
        current = latest.get(key)
        if current is not None:
            matched_rows += 1
            overlay = status_overlay(current, latest_source, latest_sha256)
            previous_status = output.get('query_coverage_status')
            previous_confirmed = bool(output.get('coverage_confirmed'))
            output.update(overlay)
            output['previous_query_coverage_status'] = previous_status
            output['previous_coverage_confirmed'] = previous_confirmed
            output['coverage_evidence_revision'] = 'phase3.1_consolidated_acquisition_overlay'
            terminal_counts[overlay['query_coverage_status']] = (
                terminal_counts.get(overlay['query_coverage_status'], 0) + 1
            )
        if (
            str(output.get('query_coverage_status') or '') == 'complete'
            and bool(output.get('analysis_ready'))
        ):
            analysis_coverage_confirmed_keys.add(key)
        if bool(output.get('coverage_confirmed')):
            coverage_confirmed_count += 1
        output_rows.append(output)

    for key, current in latest.items():
        if str(current.get('execution_status') or '') in COMPLETED_QUERY_STATUSES:
            analysis_coverage_confirmed_keys.add(key)

    if base_rows != 118883:
        raise ValueError(f'Expected 118883 base evidence rows, found {base_rows}')
    if latest_rows != 72859:
        raise ValueError(f'Expected 72859 consolidated status rows, found {latest_rows}')
    if matched_rows != latest_rows:
        raise ValueError(
            f'Latest status coverage mismatch: matched {matched_rows} of {latest_rows}'
        )
    if terminal_counts.get('terminal_exclusion', 0) != 4:
        raise ValueError(
            f'Expected 4 terminal exclusions, found {terminal_counts.get("terminal_exclusion", 0)}'
        )
    analysis_coverage_confirmed_count = len(analysis_coverage_confirmed_keys)

    with output_path.open('w', encoding='utf-8', newline='\n') as handle:
        for row in output_rows:
            handle.write(stable_json(row) + '\n')

    return {
        'base_source': source_label(base_path),
        'base_source_sha256': sha256_file(base_path),
        'latest_status_source': latest_source,
        'latest_status_source_sha256': latest_sha256,
        'base_workflow_count': base_rows,
        'latest_status_count': latest_rows,
        'latest_status_matched_count': matched_rows,
        'coverage_confirmed_count': coverage_confirmed_count,
        'analysis_coverage_confirmed_count': analysis_coverage_confirmed_count,
        'latest_status_counts': dict(sorted(terminal_counts.items())),
        'output_path': source_label(output_path),
        'output_sha256': sha256_file(output_path),
        'external_fetch_performed': False,
        'canonical_inputs_modified': False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-evidence', type=Path, default=DEFAULT_BASE)
    parser.add_argument('--latest-status', type=Path, default=DEFAULT_LATEST)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--output-name', default=DEFAULT_OUTPUT_FILE)
    args = parser.parse_args(argv)
    result = build(args.base_evidence, args.latest_status, args.output_dir, args.output_name)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
