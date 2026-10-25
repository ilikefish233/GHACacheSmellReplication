"""Offline consolidation of the canonical RQ2 runs and formal Phase 2 data.

The script is intentionally local-only.  It does not import a GitHub client,
read a token, call an API, modify canonical files, or run an analysis model.
The large run table is indexed in a temporary SQLite database so that the
canonical JSONL does not have to be held in memory at once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANONICAL = ROOT / 'data_26_6_9_canonical_20260609' / 'workflow_runs.jsonl'
DEFAULT_EVIDENCE = (
    ROOT
    / 'output_canonical_rq2_query_coverage_phase1_2_1_20260919'
    / 'rq2_query_coverage_evidence.jsonl'
)
DEFAULT_PLAN_DIR = ROOT / 'output_rq2_unified_acquisition_plan_phase2_0_20260919'
DEFAULT_OUTPUT_DIR = ROOT / 'output_rq2_unified_acquisition_consolidated_20260920'

FIXED_START = datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc)
FIXED_CUTOFF = datetime(2026, 6, 9, 23, 59, 59, tzinfo=timezone.utc)

PLAN_REPOSITORY_FILE = 'rq2_acquisition_plan_repositories.jsonl'
PLAN_WORKFLOW_FILE = 'rq2_acquisition_plan_workflows.jsonl'

FORMAL_BATCHES = (
    'output_rq2_unified_acquisition_phase2_full_20260919',
    'output_rq2_unified_acquisition_phase2_full_shard01',
    'output_rq2_unified_acquisition_phase2_full_shard02',
    'output_rq2_unified_acquisition_phase2_full_shard03',
    'output_rq2_unified_acquisition_phase2_full_round2_shard01',
    'output_rq2_unified_acquisition_phase2_full_round2_shard02',
    'output_rq2_unified_acquisition_phase2_full_round2_shard03',
    'output_rq2_unified_acquisition_phase2_full_round2_shard04',
    'output_rq2_unified_acquisition_phase2_full_round2_shard05',
    'output_rq2_unified_acquisition_phase2_full_round3_retry_20260920',
    'output_rq2_unified_acquisition_phase2_full_round4_finalized_20260920',
)

TERMINAL_STATUSES = {
    'complete',
    'no_qualifying_runs',
    'terminal_exclusion',
    'access_denied',
}
NONTERMINAL_STATUSES = {'incomplete', 'retryable_failure', 'unknown'}

RUNS_OUTPUT = 'workflow_runs.jsonl'
STATUS_OUTPUT = 'workflow_acquisition_status.jsonl'
SUMMARY_OUTPUT = 'consolidation_summary.json'
MANIFEST_OUTPUT = 'consolidation_manifest.json'


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace('\\', '/')
    except ValueError:
        return str(path.resolve()).replace('\\', '/')


def parse_created_at(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_workflow_path(value: Any) -> str:
    text = str(value or '').strip().replace('\\', '/')
    if not text:
        return ''
    normalized = posixpath.normpath(text)
    return '' if normalized == '.' else normalized


def process_jsonl(
    path: Path,
    callback: Optional[Callable[[Dict[str, Any], int], None]] = None,
) -> Tuple[int, str]:
    """Stream one JSONL file and return its row count and byte hash."""
    digest = hashlib.sha256()
    count = 0
    with path.open('rb') as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            digest.update(raw_line)
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid JSON at {path}:{line_number}: {exc}') from exc
            if not isinstance(value, dict):
                raise ValueError(f'Expected object at {path}:{line_number}')
            count += 1
            if callback is not None:
                callback(value, line_number)
    return count, digest.hexdigest()


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open('w', encoding='utf-8', newline='\n') as handle:
        for row in rows:
            handle.write(stable_json(row) + '\n')


def run_key(record: Mapping[str, Any]) -> Tuple[str, int, int]:
    repository_id = str(record.get('repository_id') or '').strip()
    if not repository_id:
        raise ValueError('Run record is missing repository_id')
    try:
        run_id = int(record.get('run_id'))
        run_attempt = int(record.get('run_attempt') or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError('Run record has an invalid run_id/run_attempt') from exc
    if run_id < 0 or run_attempt < 1:
        raise ValueError('Run record has an invalid negative run key')
    return repository_id, run_id, run_attempt


def run_is_in_window(record: Mapping[str, Any]) -> bool:
    created = parse_created_at(record.get('created_at'))
    return created is not None and FIXED_START <= created <= FIXED_CUTOFF


def completeness_score(record: Mapping[str, Any]) -> int:
    """Stable, conservative completeness score for duplicate winner selection."""
    score = 0
    for value in record.values():
        if value is None or value == '' or value == [] or value == {}:
            continue
        score += 1
    return score


def plan_workflow_key(
    repository_id: str,
    workflow_id: Any,
    workflow_path: Any,
) -> Tuple[str, str, str]:
    workflow_id_text = str(workflow_id or '').strip()
    path = normalize_workflow_path(workflow_path)
    if workflow_id_text:
        return repository_id, 'workflow_id', workflow_id_text
    return repository_id, 'workflow_path', path


def status_label(row: Mapping[str, Any]) -> str:
    value = str(row.get('execution_status') or row.get('status') or '').strip()
    aliases = {
        'ok': 'complete',
        'no_runs': 'no_qualifying_runs',
        'forbidden': 'access_denied',
        'terminal_outcome': 'terminal_exclusion',
        'error': 'incomplete',
    }
    return aliases.get(value, value or 'unknown')


def evidence_projection(row: Mapping[str, Any]) -> Dict[str, Any]:
    fields = (
        'query_coverage_status',
        'coverage_confirmed',
        'analysis_ready',
        'coverage_confirmation_reason',
        'coverage_unconfirmed_reason',
        'evidence_source',
        'evidence_source_file',
        'evidence_source_file_sha256',
        'query_endpoint_type',
        'query_start',
        'query_cutoff',
        'query_window_days',
        'pagination_complete',
        'termination_reason',
        'current_qualifying_run_count',
    )
    return {field: row.get(field) for field in fields if field in row}


def evidence_status(row: Mapping[str, Any]) -> str:
    coverage_status = str(row.get('query_coverage_status') or '').strip()
    if coverage_status == 'complete' or bool(row.get('coverage_confirmed')):
        qualifying = row.get('current_qualifying_run_count')
        try:
            qualifying_count = int(qualifying or 0)
        except (TypeError, ValueError):
            qualifying_count = 0
        return 'complete' if qualifying_count > 0 else 'no_qualifying_runs'
    return 'incomplete'


def evidence_rank(row: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(row.get('fetched_at') or ''),
        str(row.get('evidence_source_file_sha256') or ''),
        stable_json(row),
    )


def build_plan(
    plan_dir: Path,
    input_files: List[Dict[str, Any]],
) -> Tuple[
    Dict[Tuple[str, str, str], Dict[str, Any]],
    Dict[Tuple[str, str], Tuple[str, str, str]],
    Dict[Tuple[str, str], List[Tuple[str, str, str]]],
    Set[str],
    int,
]:
    plan_repositories = plan_dir / PLAN_REPOSITORY_FILE
    plan_workflows = plan_dir / PLAN_WORKFLOW_FILE
    if not plan_repositories.is_file() or not plan_workflows.is_file():
        raise FileNotFoundError('Phase 2.0 plan repository/workflow files are required')

    repository_ids: Set[str] = set()

    def on_repository(row: Dict[str, Any], _: int) -> None:
        repository_id = str(row.get('repository_id') or '').strip()
        if not repository_id:
            raise ValueError('Phase 2.0 repository plan has a missing repository_id')
        if repository_id in repository_ids:
            raise ValueError(f'Duplicate repository_id in Phase 2.0 plan: {repository_id}')
        repository_ids.add(repository_id)

    repository_count, repository_hash = process_jsonl(plan_repositories, on_repository)
    input_files.append({
        'path': relpath(plan_repositories),
        'sha256': repository_hash,
        'record_count': repository_count,
        'role': 'phase2_plan_repositories',
    })

    workflows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    by_id: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
    by_path: MutableMapping[Tuple[str, str], List[Tuple[str, str, str]]] = defaultdict(list)

    def on_workflow(row: Dict[str, Any], _: int) -> None:
        repository_id = str(row.get('repository_id') or '').strip()
        if not repository_id:
            raise ValueError('Phase 2.0 workflow plan has a missing repository_id')
        workflow_id = str(row.get('workflow_id') or '').strip()
        workflow_path = normalize_workflow_path(row.get('workflow_path'))
        key = plan_workflow_key(repository_id, workflow_id, workflow_path)
        if key in workflows:
            raise ValueError(f'Duplicate workflow in Phase 2.0 plan: {key}')
        workflows[key] = {
            'repository_id': repository_id,
            'workflow_id': workflow_id or None,
            'workflow_path': workflow_path,
            'workflow_key': f'{repository_id}|{workflow_id}' if workflow_id else f'{repository_id}|{workflow_path}',
        }
        if workflow_id:
            by_id[(repository_id, workflow_id)] = key
        by_path[(repository_id, workflow_path)].append(key)

    workflow_count, workflow_hash = process_jsonl(plan_workflows, on_workflow)
    input_files.append({
        'path': relpath(plan_workflows),
        'sha256': workflow_hash,
        'record_count': workflow_count,
        'role': 'phase2_plan_workflows',
    })
    if repository_count != 14279:
        raise ValueError(f'Expected 14279 Phase 2.0 repositories, found {repository_count}')
    if len(repository_ids) != repository_count:
        raise ValueError('Phase 2.0 repository IDs are not unique')
    return workflows, by_id, dict(by_path), repository_ids, workflow_count


def build_evidence_index(
    evidence_path: Path,
    input_files: List[Dict[str, Any]],
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]], int]:
    by_id: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_path: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def select(
        target: MutableMapping[Tuple[str, str], Dict[str, Any]],
        key: Tuple[str, str],
        row: Dict[str, Any],
    ) -> None:
        previous = target.get(key)
        if previous is None or evidence_rank(row) > evidence_rank(previous):
            target[key] = row

    def on_evidence(row: Dict[str, Any], _: int) -> None:
        repository_id = str(row.get('repository_id') or '').strip()
        workflow_id = str(row.get('workflow_id') or '').strip()
        workflow_path = normalize_workflow_path(row.get('workflow_path'))
        workflow_key = str(row.get('workflow_key') or '')
        if '|' in workflow_key:
            key_repository, key_path = workflow_key.split('|', 1)
            repository_id = repository_id or key_repository
            workflow_path = workflow_path or normalize_workflow_path(key_path)
        if not repository_id:
            raise ValueError('Coverage evidence has a missing repository_id')
        if workflow_id:
            select(by_id, (repository_id, workflow_id), row)
        if workflow_path:
            select(by_path, (repository_id, workflow_path), row)

    count, digest = process_jsonl(evidence_path, on_evidence)
    input_files.append({
        'path': relpath(evidence_path),
        'sha256': digest,
        'record_count': count,
        'role': 'phase1_2_1_query_coverage_evidence',
    })
    return by_id, by_path, count


def resolve_plan_key(
    row: Mapping[str, Any],
    workflows: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    by_id: Mapping[Tuple[str, str], Tuple[str, str, str]],
    by_path: Mapping[Tuple[str, str], Sequence[Tuple[str, str, str]]],
) -> Optional[Tuple[str, str, str]]:
    repository_id = str(row.get('repository_id') or '').strip()
    workflow_id = str(row.get('workflow_id') or '').strip()
    workflow_path = normalize_workflow_path(row.get('workflow_path'))
    if workflow_id:
        exact = by_id.get((repository_id, workflow_id))
        if exact is not None:
            return exact
        path_candidates = by_path.get((repository_id, workflow_path), ())
        if len(path_candidates) == 1:
            candidate = workflows[path_candidates[0]]
            if not candidate.get('workflow_id'):
                return path_candidates[0]
        return None
    path_candidates = by_path.get((repository_id, workflow_path), ())
    if len(path_candidates) == 1:
        return path_candidates[0]
    return None


def add_input_file(
    input_files: List[Dict[str, Any]],
    path: Path,
    role: str,
    count: int,
    digest: str,
    in_window_count: Optional[int] = None,
) -> None:
    item: Dict[str, Any] = {
        'path': relpath(path),
        'sha256': digest,
        'record_count': count,
        'role': role,
    }
    if in_window_count is not None:
        item['in_window_record_count'] = in_window_count
    input_files.append(item)


class RunStore:
    """Disk-backed deterministic unique-run store."""

    def __init__(self, db_path: Path) -> None:
        self.connection = sqlite3.connect(str(db_path))
        self.connection.execute('PRAGMA journal_mode=DELETE')
        self.connection.execute('PRAGMA synchronous=NORMAL')
        self.connection.execute(
            '''CREATE TABLE runs (
                run_key TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                completeness_score INTEGER NOT NULL,
                source_preference INTEGER NOT NULL,
                source_order INTEGER NOT NULL,
                selected_source_type TEXT NOT NULL,
                selected_source_directory TEXT NOT NULL,
                source_types_json TEXT NOT NULL,
                source_directories_json TEXT NOT NULL,
                duplicate_count INTEGER NOT NULL
            )'''
        )
        self.total_candidates = 0
        self.window_candidates = 0
        self.duplicates_removed = 0

    def add(self, record: Mapping[str, Any], source_type: str, source_directory: str, source_order: int) -> None:
        self.total_candidates += 1
        if not run_is_in_window(record):
            return
        self.window_candidates += 1
        repository_id, run_id, run_attempt = run_key(record)
        key = f'{repository_id}\x1f{run_id}\x1f{run_attempt}'
        record_json = stable_json(record)
        score = completeness_score(record)
        preference = 1 if source_type == 'canonical' else 0
        source_types = [source_type]
        source_directories = [source_directory]
        cursor = self.connection.execute(
            '''INSERT OR IGNORE INTO runs (
                run_key, record_json, completeness_score, source_preference,
                source_order, selected_source_type, selected_source_directory,
                source_types_json, source_directories_json, duplicate_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
            (
                key,
                record_json,
                score,
                preference,
                source_order,
                source_type,
                source_directory,
                stable_json(source_types),
                stable_json(source_directories),
            ),
        )
        if cursor.rowcount:
            return

        self.duplicates_removed += 1
        existing = self.connection.execute(
            '''SELECT record_json, completeness_score, source_preference,
                      source_order, selected_source_type,
                      selected_source_directory, source_types_json,
                      source_directories_json, duplicate_count
               FROM runs WHERE run_key = ?''',
            (key,),
        ).fetchone()
        if existing is None:
            raise RuntimeError(f'Run store lost key {key}')
        (
            existing_json,
            existing_score,
            existing_preference,
            existing_order,
            existing_source_type,
            existing_source_directory,
            existing_types_json,
            existing_directories_json,
            existing_duplicate_count,
        ) = existing
        existing_types = json.loads(existing_types_json)
        existing_directories = json.loads(existing_directories_json)
        if source_type not in existing_types:
            existing_types.append(source_type)
        if source_directory not in existing_directories:
            existing_directories.append(source_directory)

        existing_rank = (
            int(existing_score),
            int(existing_preference),
            -int(existing_order),
            str(existing_json),
        )
        candidate_rank = (score, preference, -source_order, record_json)
        if candidate_rank > existing_rank:
            selected_values = (
                record_json,
                score,
                preference,
                source_order,
                source_type,
                source_directory,
            )
        else:
            selected_values = (
                existing_json,
                existing_score,
                existing_preference,
                existing_order,
                existing_source_type,
                existing_source_directory,
            )
        self.connection.execute(
            '''UPDATE runs SET
                 record_json = ?, completeness_score = ?, source_preference = ?,
                 source_order = ?, selected_source_type = ?,
                 selected_source_directory = ?, source_types_json = ?,
                 source_directories_json = ?, duplicate_count = ?
               WHERE run_key = ?''',
            (
                *selected_values,
                stable_json(sorted(existing_types)),
                stable_json(sorted(existing_directories)),
                int(existing_duplicate_count) + 1,
                key,
            ),
        )

    def iter_rows(self) -> Iterable[Tuple[Any, ...]]:
        return self.connection.execute(
            '''SELECT run_key, record_json, selected_source_type,
                      selected_source_directory, source_types_json,
                      source_directories_json, duplicate_count
               FROM runs ORDER BY run_key'''
        )

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()


def consolidate(
    canonical_path: Path,
    evidence_path: Path,
    plan_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f'Refusing to overwrite existing output: {output_dir}')
    for required in (canonical_path, evidence_path, plan_dir / PLAN_REPOSITORY_FILE, plan_dir / PLAN_WORKFLOW_FILE):
        if not required.is_file():
            raise FileNotFoundError(required)

    input_files: List[Dict[str, Any]] = []
    workflows, by_id, by_path, plan_repository_ids, plan_workflow_count = build_plan(
        plan_dir, input_files
    )
    evidence_by_id, evidence_by_path, evidence_count = build_evidence_index(
        evidence_path, input_files
    )

    output_dir.mkdir(parents=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix='rq2_consolidate_', dir=str(ROOT)))
    store: Optional[RunStore] = None
    try:
        store = RunStore(temporary_dir / 'runs.sqlite3')
        canonical_total = 0
        canonical_window = 0

        def on_canonical(row: Dict[str, Any], _: int) -> None:
            nonlocal canonical_window
            if run_is_in_window(row):
                canonical_window += 1
            store.add(row, 'canonical', relpath(canonical_path.parent), 0)

        canonical_total, canonical_hash = process_jsonl(canonical_path, on_canonical)
        add_input_file(
            input_files,
            canonical_path,
            'canonical_runs',
            canonical_total,
            canonical_hash,
            canonical_window,
        )

        normalized_batch_counts: Dict[str, Dict[str, int]] = {}
        for batch_index, batch_name in enumerate(FORMAL_BATCHES, start=1):
            batch_dir = ROOT / batch_name
            normalized_path = batch_dir / 'normalized_workflow_runs.jsonl'
            status_path = batch_dir / 'workflow_execution_status.jsonl'
            if not normalized_path.is_file() or not status_path.is_file():
                raise FileNotFoundError(
                    f'Formal batch missing normalized/status file: {batch_name}'
                )
            batch_total = 0
            batch_window = 0

            def on_normalized(row: Dict[str, Any], _: int, *, _batch_index=batch_index, _batch_name=batch_name) -> None:
                nonlocal batch_window
                if run_is_in_window(row):
                    batch_window += 1
                store.add(row, 'acquisition_normalized', _batch_name, _batch_index)

            batch_total, normalized_hash = process_jsonl(normalized_path, on_normalized)
            add_input_file(
                input_files,
                normalized_path,
                'formal_normalized_runs',
                batch_total,
                normalized_hash,
                batch_window,
            )
            normalized_batch_counts[batch_name] = {
                'records': batch_total,
                'in_window_records': batch_window,
            }

        store.connection.commit()
        canonical_hash_after_merge = sha256_file(canonical_path)

        # Initialize one status state per planned workflow.  Evidence is a
        # baseline only; formal acquisition status rows are applied afterward.
        states: Dict[Tuple[str, str, str], Dict[str, Any]] = {
            key: {
                'plan': dict(info),
                'selected': None,
                'selected_order': None,
                'history': [],
                'source_directories': set(),
                'source_files': set(),
            }
            for key, info in workflows.items()
        }

        def apply_candidate(
            key: Tuple[str, str, str],
            row: Mapping[str, Any],
            source_kind: str,
            source_directory: str,
            source_file: str,
            source_sha256: str,
            source_order: int,
        ) -> None:
            state = states[key]
            label = status_label(row)
            candidate = dict(row)
            candidate['execution_status'] = label
            candidate['_source_kind'] = source_kind
            candidate['_source_directory'] = source_directory
            candidate['_source_file'] = source_file
            candidate['_source_sha256'] = source_sha256
            candidate['_source_order'] = source_order
            state['history'].append({
                'execution_status': label,
                'source_kind': source_kind,
                'source_directory': source_directory,
                'source_file': source_file,
                'source_sha256': source_sha256,
                'query_complete': row.get('query_complete'),
                'pagination_complete': row.get('pagination_complete'),
                'records': row.get('records'),
                'termination_reason': row.get('termination_reason'),
            })
            state['source_directories'].add(source_directory)
            state['source_files'].add((source_file, source_sha256))
            current = state['selected']
            current_order = state['selected_order']
            if current is None:
                state['selected'] = candidate
                state['selected_order'] = source_order
                return
            current_label = str(current.get('execution_status') or 'unknown')
            current_terminal = current_label in TERMINAL_STATUSES
            candidate_terminal = label in TERMINAL_STATUSES
            if current_terminal and not candidate_terminal:
                return
            if candidate_terminal and not current_terminal:
                state['selected'] = candidate
                state['selected_order'] = source_order
                return
            if current_order is None or source_order >= current_order:
                state['selected'] = candidate
                state['selected_order'] = source_order

        # Phase 1.2.1 evidence is attached as a baseline where the planned
        # workflow can be resolved by ID or, when necessary, by path.
        evidence_matches = 0
        for key, info in workflows.items():
            repository_id = str(info['repository_id'])
            workflow_id = str(info.get('workflow_id') or '')
            workflow_path = str(info.get('workflow_path') or '')
            evidence = None
            if workflow_id:
                evidence = evidence_by_id.get((repository_id, workflow_id))
            if evidence is None:
                evidence = evidence_by_path.get((repository_id, workflow_path))
            if evidence is None:
                continue
            evidence_matches += 1
            evidence_row = evidence_projection(evidence)
            evidence_row.update({
                'repository_id': repository_id,
                'workflow_id': workflow_id or None,
                'workflow_path': workflow_path,
                'execution_status': evidence_status(evidence),
                'records': evidence.get('current_qualifying_run_count', 0),
                'query_complete': evidence.get('coverage_confirmed', False),
                'pagination_complete': evidence.get('pagination_complete'),
            })
            apply_candidate(
                key,
                evidence_row,
                'coverage_evidence',
                'output_canonical_rq2_query_coverage_phase1_2_1_20260919',
                relpath(evidence_path),
                next(
                    item['sha256'] for item in input_files
                    if item['path'] == relpath(evidence_path)
                ),
                0,
            )

        unresolved_status_rows = 0
        status_rows_by_batch: Dict[str, int] = {}
        for batch_index, batch_name in enumerate(FORMAL_BATCHES, start=1):
            batch_dir = ROOT / batch_name
            status_path = batch_dir / 'workflow_execution_status.jsonl'
            status_rows_seen = 0
            source_hash = sha256_file(status_path)
            source_file = relpath(status_path)

            def on_status(row: Dict[str, Any], line_number: int, *, _batch_index=batch_index, _batch_name=batch_name, _source_hash=source_hash, _source_file=source_file) -> None:
                nonlocal unresolved_status_rows, status_rows_seen
                status_rows_seen += 1
                key = resolve_plan_key(row, workflows, by_id, by_path)
                if key is None:
                    unresolved_status_rows += 1
                    return
                # Line number makes repeated rows in the same batch deterministic
                # while preserving the explicit later-batch precedence.
                apply_candidate(
                    key,
                    row,
                    'acquisition_status',
                    _batch_name,
                    _source_file,
                    _source_hash,
                    _batch_index * 1_000_000 + line_number,
                )

            status_count, _ = process_jsonl(status_path, on_status)
            if status_count != status_rows_seen:
                raise RuntimeError('Status row count accounting mismatch')
            status_rows_by_batch[batch_name] = status_count
            add_input_file(
                input_files,
                status_path,
                'formal_workflow_acquisition_status',
                status_count,
                source_hash,
            )

        if unresolved_status_rows:
            raise ValueError(
                f'{unresolved_status_rows} acquisition status rows do not match the Phase 2.0 workflow plan'
            )

        final_status_rows: List[Dict[str, Any]] = []
        final_status_counts: Counter[str] = Counter()
        repository_workflows: MutableMapping[str, List[Dict[str, Any]]] = defaultdict(list)
        unknown_keys: List[Tuple[str, str, str]] = []
        for key in sorted(states):
            state = states[key]
            plan = state['plan']
            selected = state['selected']
            final_status = str(selected.get('execution_status') if selected else 'unknown')
            if final_status not in TERMINAL_STATUSES:
                unknown_keys.append(key)
            final_status_counts[final_status] += 1
            evidence = None
            repository_id = str(plan['repository_id'])
            workflow_id = str(plan.get('workflow_id') or '')
            workflow_path = str(plan.get('workflow_path') or '')
            if workflow_id:
                evidence = evidence_by_id.get((repository_id, workflow_id))
            if evidence is None:
                evidence = evidence_by_path.get((repository_id, workflow_path))

            output_row: Dict[str, Any] = {
                **(dict(selected) if selected else {}),
                'workflow_key': plan['workflow_key'],
                'repository_id': repository_id,
                'workflow_id': workflow_id or None,
                'workflow_path': workflow_path,
                'execution_status': final_status,
                'terminal': final_status in TERMINAL_STATUSES,
                'status_source_kind': selected.get('_source_kind') if selected else None,
                'status_source_directory': selected.get('_source_directory') if selected else None,
                'status_source_file': selected.get('_source_file') if selected else None,
                'status_source_sha256': selected.get('_source_sha256') if selected else None,
                'status_source_directories': sorted(state['source_directories']),
                'status_source_files': [
                    {'path': path, 'sha256': digest}
                    for path, digest in sorted(state['source_files'])
                ],
                'status_history': sorted(
                    state['history'],
                    key=lambda item: (
                        item['source_directory'],
                        item['source_file'],
                        item['execution_status'],
                        str(item.get('termination_reason') or ''),
                    ),
                ),
                'coverage_evidence': evidence_projection(evidence) if evidence else None,
            }
            # A terminal workflow-specific acquisition result is the final
            # query-completion authority for this consolidation.  Keep the raw
            # source values in status_history, but expose normalized final flags.
            if final_status in {'complete', 'no_qualifying_runs'}:
                output_row['query_coverage_status'] = 'complete'
                output_row['query_complete'] = True
                output_row['pagination_complete'] = True
                output_row['coverage_confirmed'] = True
            elif final_status == 'terminal_exclusion':
                output_row['query_coverage_status'] = 'terminal_exclusion'
                output_row['query_complete'] = False
                output_row['pagination_complete'] = False
                output_row['coverage_confirmed'] = False
            elif final_status == 'access_denied':
                output_row['query_coverage_status'] = 'access_denied'
                output_row['query_complete'] = False
                output_row['pagination_complete'] = False
                output_row['coverage_confirmed'] = False
            output_row.pop('_source_kind', None)
            output_row.pop('_source_directory', None)
            output_row.pop('_source_file', None)
            output_row.pop('_source_sha256', None)
            output_row.pop('_source_order', None)
            final_status_rows.append(output_row)
            repository_workflows[repository_id].append(output_row)

        if unknown_keys:
            raise ValueError(
                f'Unresolved final workflow statuses: {len(unknown_keys)}; examples={unknown_keys[:5]}'
            )

        terminal_repositories = 0
        unresolved_repositories = 0
        repository_status_counts: Counter[str] = Counter()
        terminal_exclusion_repositories = 0
        access_denied_repositories = 0
        for repository_id in sorted(plan_repository_ids):
            rows = repository_workflows.get(repository_id, [])
            expected = sum(
                1 for info in workflows.values()
                if str(info['repository_id']) == repository_id
            )
            unresolved = expected != len(rows) or any(
                row.get('execution_status') not in TERMINAL_STATUSES for row in rows
            )
            if unresolved:
                unresolved_repositories += 1
                repository_status_counts['unresolved'] += 1
                continue
            terminal_repositories += 1
            has_exclusion = any(row.get('execution_status') == 'terminal_exclusion' for row in rows)
            has_access_denied = any(row.get('execution_status') == 'access_denied' for row in rows)
            if has_exclusion:
                terminal_exclusion_repositories += 1
            if has_access_denied:
                access_denied_repositories += 1
            repository_status_counts[
                'terminal_outcome' if has_exclusion else (
                    'access_denied' if has_access_denied else 'complete'
                )
            ] += 1

        if terminal_repositories != len(plan_repository_ids) or unresolved_repositories:
            raise ValueError(
                f'Unresolved repositories remain: {unresolved_repositories}; '
                f'terminal={terminal_repositories}/{len(plan_repository_ids)}'
            )

        # Materialize the two core outputs in stable key order.
        final_status_rows.sort(
            key=lambda row: (
                str(row.get('repository_id') or ''),
                str(row.get('workflow_id') or ''),
                str(row.get('workflow_path') or ''),
            )
        )
        final_run_count = 0
        output_runs_path = output_dir / RUNS_OUTPUT
        with output_runs_path.open('w', encoding='utf-8', newline='\n') as handle:
            for row in store.iter_rows():
                (
                    key,
                    record_json,
                    selected_source_type,
                    selected_source_directory,
                    source_types_json,
                    source_directories_json,
                    duplicate_count,
                ) = row
                record = json.loads(record_json)
                record['_consolidation_provenance'] = {
                    'run_key': key.replace('\x1f', '|'),
                    'selected_source_type': selected_source_type,
                    'selected_source_directory': selected_source_directory,
                    'source_types': json.loads(source_types_json),
                    'source_directories': json.loads(source_directories_json),
                    'duplicate_count': int(duplicate_count),
                }
                handle.write(stable_json(record) + '\n')
                final_run_count += 1
        output_status_path = output_dir / STATUS_OUTPUT
        write_jsonl(output_status_path, final_status_rows)

        merged_before_dedup = canonical_window + sum(
            item['in_window_records'] for item in normalized_batch_counts.values()
        )
        if merged_before_dedup - store.duplicates_removed != final_run_count:
            raise RuntimeError(
                'Run conservation failed: before - duplicates != final unique records'
            )
        if store.duplicates_removed != merged_before_dedup - final_run_count:
            raise RuntimeError('Duplicate count is inconsistent')

        output_data_hashes = {
            RUNS_OUTPUT: sha256_file(output_runs_path),
            STATUS_OUTPUT: sha256_file(output_status_path),
        }
        summary: Dict[str, Any] = {
            'phase': 'RQ2 Phase 3.0',
            'mode': 'offline-consolidation',
            'network_client_created': False,
            'external_fetch_performed': False,
            'statistical_models_rerun': False,
            'canonical_inputs_modified': False,
            'fixed_window': {
                'start': FIXED_START.isoformat().replace('+00:00', 'Z'),
                'cutoff': FIXED_CUTOFF.isoformat().replace('+00:00', 'Z'),
                'inclusive': True,
            },
            'canonical_raw_records': canonical_total,
            'canonical_window_records': canonical_window,
            'normalized_records_by_batch': normalized_batch_counts,
            'normalized_records_total': sum(item['records'] for item in normalized_batch_counts.values()),
            'normalized_window_records_total': sum(item['in_window_records'] for item in normalized_batch_counts.values()),
            'merged_records_before_dedup': merged_before_dedup,
            'duplicates_removed': store.duplicates_removed,
            'final_unique_runs': final_run_count,
            'duplicate_run_key_count': merged_before_dedup - final_run_count,
            'workflow_final_status_counts': dict(sorted(final_status_counts.items())),
            'terminal_repositories': terminal_repositories,
            'terminal_repository_status_counts': dict(sorted(repository_status_counts.items())),
            'terminal_exclusion_workflows': final_status_counts.get('terminal_exclusion', 0),
            'terminal_exclusion_repositories': terminal_exclusion_repositories,
            'access_denied_workflows': final_status_counts.get('access_denied', 0),
            'access_denied_repositories': access_denied_repositories,
            'unresolved_repositories': unresolved_repositories,
            'unresolved_workflows': final_status_counts.get('unknown', 0) + sum(
                final_status_counts.get(status, 0) for status in NONTERMINAL_STATUSES
            ),
            'phase2_plan_repositories': len(plan_repository_ids),
            'phase2_plan_workflows': plan_workflow_count,
            'phase1_2_1_evidence_records': evidence_count,
            'phase1_2_1_evidence_matches_to_plan': evidence_matches,
            'unmatched_acquisition_status_rows': unresolved_status_rows,
            'status_records_by_batch': status_rows_by_batch,
            'excluded_inputs': [
                'dry-run outputs',
                'Phase 2.2 pilot outputs',
                'Phase 2.2.1 pilot-fix outputs',
                'output_rq2_unified_acquisition_phase2_full_round4_retry_20260920',
                'raw_workflow_runs.jsonl from every acquisition directory',
            ],
            'canonical_sha256_before': canonical_hash,
            'canonical_sha256_after': canonical_hash_after_merge,
            'canonical_hash_unchanged': canonical_hash == canonical_hash_after_merge,
            'output_file_sha256': output_data_hashes,
        }
        if not summary['canonical_hash_unchanged']:
            raise RuntimeError('Canonical input hash changed during consolidation')
        write_json(output_dir / SUMMARY_OUTPUT, summary)

        manifest: Dict[str, Any] = {
            'manifest_version': 'rq2-phase3.0-offline-consolidation-v1',
            'phase': 'RQ2 Phase 3.0',
            'mode': 'offline-consolidation',
            'canonical_input_modified': False,
            'network_client_created': False,
            'external_fetch_performed': False,
            'statistical_models_rerun': False,
            'fixed_window_start': '2026-05-10T23:59:59Z',
            'fixed_window_cutoff': '2026-06-09T23:59:59Z',
            'input_files': input_files,
            'output_files': {
                name: sha256_file(output_dir / name)
                for name in (RUNS_OUTPUT, STATUS_OUTPUT, SUMMARY_OUTPUT)
            },
            'final_manifest_excludes_self_hash': True,
            'records': {
                'canonical_raw_records': canonical_total,
                'canonical_window_records': canonical_window,
                'merged_records_before_dedup': merged_before_dedup,
                'duplicates_removed': store.duplicates_removed,
                'final_unique_runs': final_run_count,
                'workflow_status_rows': len(final_status_rows),
                'terminal_repositories': terminal_repositories,
            },
            'rules': {
                'run_primary_key': '(repository_id, run_id, run_attempt)',
                'workflow_primary_key': '(repository_id, workflow_id) with path fallback',
                'terminal_statuses': sorted(TERMINAL_STATUSES),
                'raw_files_used_as_analysis_input': False,
                'input_order_deterministic': True,
            },
        }
        write_json(output_dir / MANIFEST_OUTPUT, manifest)
        result = {
            'summary': summary,
            'output_file_sha256': {
                **output_data_hashes,
                SUMMARY_OUTPUT: sha256_file(output_dir / SUMMARY_OUTPUT),
                MANIFEST_OUTPUT: sha256_file(output_dir / MANIFEST_OUTPUT),
            },
        }
        return result
    finally:
        if store is not None:
            store.close()
        # Temporary SQLite indexes are never part of the deliverable.
        for child in temporary_dir.iterdir():
            child.unlink()
        temporary_dir.rmdir()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--canonical', type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument('--evidence', type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument('--plan-dir', type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    result = consolidate(args.canonical, args.evidence, args.plan_dir, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
