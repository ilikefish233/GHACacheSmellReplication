"""Execute or preview the Phase 2.0 RQ2 repository acquisition plan.

The default mode is a deterministic, local-only dry run.  The explicit
``--execute`` mode is deliberately lazy: only that mode imports the existing
GitHub client and reads a token.  This module never writes canonical inputs;
all acquisition output is written to a separate Phase 2.1 directory.

The production client is the one in ``supplement_default_branch_runs.py``.
This module adds orchestration, provenance, checkpointing, and an offline
scripted client for tests without implementing a second production pagination
algorithm.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple, Union


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
DEFAULT_PLAN_DIR = REPOSITORY_ROOT / 'output_rq2_unified_acquisition_plan_phase2_0_20260919'
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / 'output_rq2_unified_acquisition_phase2_1_dry_run_20260919'

FIXED_CUTOFF = '2026-06-09T23:59:59Z'
FIXED_QUERY_START = '2026-05-10T23:59:59Z'
FIXED_WINDOW_DAYS = 30
PLAN_RULES_VERSION = 'rq2-phase2.0-repository-plan-rules-2026-09-19'
EXECUTION_RULES_VERSION = 'rq2-phase2.1-recoverable-executor-rules-2026-09-19'
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100
DEFAULT_RETRIES = 3
DEFAULT_BATCH_SIZE = 100

# These hashes are the accepted Phase 2.0 inputs.  The CLI allows the same
# values to be passed explicitly for isolated tests, but production defaults
# remain locked to this accepted plan.
EXPECTED_PLAN_MANIFEST_SHA256 = '5aa8c9fb5ff75e1189080fe27126082bde1c3a9db651a2fdc2669cd8b79d88cc'
EXPECTED_REPOSITORY_PLAN_SHA256 = '7260263f244434da998e2fce2d59cecbee76168b6dbf2503e69f7a0d2f60c4f5'
EXPECTED_WORKFLOW_PLAN_SHA256 = '8efbd1488608ddc149268b4193c910cd30630b49d9c81dc5ebe877776117f5c4'

RAW_OUTPUT = 'raw_workflow_runs.jsonl'
NORMALIZED_OUTPUT = 'normalized_workflow_runs.jsonl'
REQUEST_OUTPUT = 'request_events.jsonl'
REPOSITORY_STATUS_OUTPUT = 'repository_execution_status.jsonl'
WORKFLOW_STATUS_OUTPUT = 'workflow_execution_status.jsonl'
CHECKPOINT_OUTPUT = 'execution_checkpoint.json'
SUMMARY_OUTPUT = 'execution_summary.json'
MANIFEST_OUTPUT = 'execution_manifest.json'

OUTPUT_NAMES = (
    RAW_OUTPUT,
    NORMALIZED_OUTPUT,
    REQUEST_OUTPUT,
    REPOSITORY_STATUS_OUTPUT,
    WORKFLOW_STATUS_OUTPUT,
    CHECKPOINT_OUTPUT,
    SUMMARY_OUTPUT,
    MANIFEST_OUTPUT,
)

TERMINAL_EXECUTION_STATUSES = {
    'complete',
    'no_runs',
    'terminal_outcome',
    'access_denied',
    'dry_run_planned',
}


class PlanValidationError(RuntimeError):
    """Raised when a plan or checkpoint is not the locked Phase 2.0 input."""


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


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


def load_json(path: Path) -> Dict[str, Any]:
    with path.open('r', encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'Expected an object in {path}')
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as handle:
        for row in rows:
            handle.write(stable_json(row) + '\n')


def append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        for row in rows:
            handle.write(stable_json(row) + '\n')


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


def timestamp_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def workflow_key(repository_id: Any, workflow_path: Any) -> str:
    return f"{str(repository_id or '').strip()}|{str(workflow_path or '').strip()}"


MemoryRunKey = Union[int, str]


def run_key(entry: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    run_id = entry.get('run_id')
    if run_id in (None, ''):
        run_id = entry.get('id')
    if run_id in (None, ''):
        return None
    return str(run_id), str(entry.get('run_attempt') or 1)


def compact_run_key(entry: Mapping[str, Any]) -> Optional[int]:
    """Return a collision-free integer key for in-memory run deduplication."""
    key = run_key(entry)
    if key is None:
        return None
    try:
        run_id = int(key[0])
        run_attempt = int(key[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f'run key is not numeric: {key!r}') from exc
    if run_id < 0:
        raise ValueError(f'run_id must be non-negative: {run_id}')
    if run_attempt >= 65536:
        raise ValueError(f'Unexpected run_attempt: {run_attempt}')
    if run_attempt < 1:
        raise ValueError(f'run_attempt must be positive: {run_attempt}')
    return (run_id << 16) | run_attempt


def _memory_run_key(entry: Mapping[str, Any]) -> MemoryRunKey:
    compact = compact_run_key(entry)
    if compact is not None:
        return compact
    return 'raw:' + hashlib.sha256(stable_json(entry).encode('utf-8')).hexdigest()


def _raw_fallback_key(entry: Mapping[str, Any]) -> str:
    key = run_key(entry)
    if key is not None:
        return f'{key[0]}|{key[1]}'
    return 'raw:' + hashlib.sha256(stable_json(entry).encode('utf-8')).hexdigest()


SENSITIVE_KEY_WORDS = ('authorization', 'token', 'password', 'secret', 'api_key', 'apikey')
BEARER_RE = re.compile(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+')


def redact_sensitive(value: Any, key: Optional[str] = None) -> Any:
    if key and any(word in key.lower() for word in SENSITIVE_KEY_WORDS):
        return '[REDACTED]'
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            text_key = str(raw_key)
            if any(word in text_key.lower() for word in SENSITIVE_KEY_WORDS):
                # The header/key itself is sensitive metadata and must not
                # appear in logs, even with a redacted value.
                continue
            sanitized[text_key] = redact_sensitive(raw_value, text_key)
        return sanitized
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return BEARER_RE.sub('Bearer [REDACTED]', value)
    return value


def plan_strategy_mode(strategy: Any) -> Optional[str]:
    value = str(strategy or '').strip().lower()
    if value.startswith('access-probe-once-then-'):
        value = value[len('access-probe-once-then-'):]
    if value.startswith('repository-wide'):
        return 'repository-wide'
    if value.startswith('workflow-specific'):
        return 'workflow-specific'
    return None


def plan_lock(
    bundle: 'PlanBundle',
    repository_ids_file_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    lock = {
        'phase2_manifest_sha256': bundle.manifest_sha256,
        'repository_plan_sha256': bundle.repository_plan_sha256,
        'workflow_plan_sha256': bundle.workflow_plan_sha256,
        'fixed_cutoff': FIXED_CUTOFF,
        'fixed_query_start': FIXED_QUERY_START,
        'window_days': FIXED_WINDOW_DAYS,
        'plan_rules_version': PLAN_RULES_VERSION,
        'execution_rules_version': EXECUTION_RULES_VERSION,
    }
    if repository_ids_file_sha256:
        lock['repository_ids_file_sha256'] = repository_ids_file_sha256
    return lock


@dataclass(frozen=True)
class PlanBundle:
    plan_dir: Path
    manifest: Dict[str, Any]
    repositories: List[Dict[str, Any]]
    workflows: List[Dict[str, Any]]
    manifest_sha256: str
    repository_plan_sha256: str
    workflow_plan_sha256: str


def _manifest_output_hashes(manifest: Mapping[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in manifest.get('output_files') or []:
        if isinstance(item, Mapping) and item.get('path'):
            result[str(item['path'])] = str(item.get('sha256') or '')
    return result


def load_and_validate_plan(
    plan_dir: Path,
    *,
    expected_manifest_sha256: Optional[str] = EXPECTED_PLAN_MANIFEST_SHA256,
    expected_repository_plan_sha256: Optional[str] = EXPECTED_REPOSITORY_PLAN_SHA256,
    expected_workflow_plan_sha256: Optional[str] = EXPECTED_WORKFLOW_PLAN_SHA256,
) -> PlanBundle:
    """Load and validate the immutable Phase 2.0 plan bundle."""
    manifest_path = plan_dir / 'rq2_acquisition_plan_manifest.json'
    repository_path = plan_dir / 'rq2_acquisition_plan_repositories.jsonl'
    workflow_path = plan_dir / 'rq2_acquisition_plan_workflows.jsonl'
    for path in (manifest_path, repository_path, workflow_path):
        if not path.exists():
            raise PlanValidationError(f'Missing locked plan input: {path}')

    manifest_sha256 = sha256_file(manifest_path)
    repository_sha256 = sha256_file(repository_path)
    workflow_sha256 = sha256_file(workflow_path)
    comparisons = (
        ('Phase 2.0 manifest', manifest_sha256, expected_manifest_sha256),
        ('repository plan', repository_sha256, expected_repository_plan_sha256),
        ('workflow plan', workflow_sha256, expected_workflow_plan_sha256),
    )
    for label, actual, expected in comparisons:
        if expected and actual != expected:
            raise PlanValidationError(
                f'{label} SHA-256 mismatch: expected {expected}, got {actual}; '
                'refusing to use an old checkpoint'
            )

    manifest = load_json(manifest_path)
    if manifest.get('plan_rules_version') != PLAN_RULES_VERSION:
        raise PlanValidationError('Unexpected Phase 2.0 plan rules version')
    if manifest.get('fixed_cutoff') != FIXED_CUTOFF:
        raise PlanValidationError('Fixed cutoff does not match Phase 2.0')
    if manifest.get('fixed_query_start') != FIXED_QUERY_START:
        raise PlanValidationError('Fixed query start does not match Phase 2.0')
    if int(manifest.get('window_days') or 0) != FIXED_WINDOW_DAYS:
        raise PlanValidationError('Fixed window does not match Phase 2.0')
    randomness = manifest.get('randomness') or {}
    if randomness.get('random_sampling') is not False:
        raise PlanValidationError('Phase 2.0 plan unexpectedly uses random sampling')

    output_hashes = _manifest_output_hashes(manifest)
    for relative, expected in (
        ('rq2_acquisition_plan_repositories.jsonl', repository_sha256),
        ('rq2_acquisition_plan_workflows.jsonl', workflow_sha256),
    ):
        if output_hashes.get(relative) != expected:
            raise PlanValidationError(f'Manifest output hash mismatch for {relative}')

    repositories = sorted(
        list(load_jsonl(repository_path)),
        key=lambda row: (str(row.get('repository_id') or ''), str(row.get('repository_name') or '')),
    )
    workflows = sorted(
        list(load_jsonl(workflow_path)),
        key=lambda row: workflow_key(row.get('repository_id'), row.get('workflow_path')),
    )
    repository_ids = [str(row.get('repository_id') or '') for row in repositories]
    if not all(repository_ids) or len(repository_ids) != len(set(repository_ids)):
        raise PlanValidationError('Repository plan IDs are not unique')
    flat_workflow_keys = [
        workflow_key(row.get('repository_id'), row.get('workflow_path'))
        for row in workflows
    ]
    if not all('|' in key and key.split('|', 1)[1] for key in flat_workflow_keys):
        raise PlanValidationError('Workflow plan contains an invalid repository/path key')
    if len(flat_workflow_keys) != len(set(flat_workflow_keys)):
        raise PlanValidationError('Workflow plan keys are not unique')

    nested: Dict[str, List[str]] = {}
    for repository in repositories:
        repository_id = str(repository['repository_id'])
        targets = repository.get('workflows') or []
        nested[repository_id] = [
            workflow_key(repository_id, target.get('workflow_path'))
            for target in targets
            if isinstance(target, Mapping)
        ]
        if len(nested[repository_id]) != len(set(nested[repository_id])):
            raise PlanValidationError(f'Duplicate nested workflow in repository {repository_id}')
        if int(repository.get('target_workflow_count') or 0) != len(nested[repository_id]):
            raise PlanValidationError(f'Nested workflow count mismatch in repository {repository_id}')
        primary_mode = plan_strategy_mode(repository.get('primary_endpoint_strategy'))
        if primary_mode is None:
            raise PlanValidationError(f'Unknown primary endpoint strategy in {repository_id}')
        if primary_mode == 'workflow-specific' and int(repository.get('missing_workflow_id_count') or 0):
            raise PlanValidationError(
                f'Workflow-specific plan illegally contains missing IDs in {repository_id}'
            )

    nested_keys = sorted(key for keys in nested.values() for key in keys)
    if nested_keys != sorted(flat_workflow_keys):
        raise PlanValidationError('Nested and flat workflow plans do not conserve the same targets')

    expected_counts = manifest.get('counts') or {}
    if int(expected_counts.get('retry_repositories') or 0) != len(repositories):
        raise PlanValidationError('Repository count differs from the locked manifest')
    if int(expected_counts.get('retry_workflows') or 0) != len(workflows):
        raise PlanValidationError('Workflow count differs from the locked manifest')
    return PlanBundle(
        plan_dir=plan_dir,
        manifest=manifest,
        repositories=repositories,
        workflows=workflows,
        manifest_sha256=manifest_sha256,
        repository_plan_sha256=repository_sha256,
        workflow_plan_sha256=workflow_sha256,
    )


def load_repository_ids_file(
    path: Path,
    bundle: PlanBundle,
) -> Set[str]:
    """Load a deterministic repository-ID allow-list and reject unknown IDs."""
    if not path.exists():
        raise PlanValidationError(f'Missing repository ID allow-list: {path}')
    requested: List[str] = []
    with path.open('r', encoding='utf-8') as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value: Any = json.loads(line)
            except json.JSONDecodeError:
                value = line
            if isinstance(value, Mapping):
                repository_id = str(
                    value.get('repository_id')
                    or value.get('id')
                    or ''
                ).strip()
            else:
                repository_id = str(value or '').strip()
            if not repository_id:
                raise PlanValidationError(
                    f'Empty repository ID at {path}:{line_number}'
                )
            requested.append(repository_id)
    if len(requested) != len(set(requested)):
        duplicates = sorted(
            repository_id for repository_id, count in Counter(requested).items()
            if count > 1
        )
        raise PlanValidationError(
            f'Duplicate repository IDs in {path}: {duplicates[:5]}'
        )
    known = {
        str(row.get('repository_id') or '') for row in bundle.repositories
    }
    unknown = sorted(set(requested) - known)
    if unknown:
        raise PlanValidationError(
            f'Unknown repository ID(s) in {path}: {unknown[:10]}'
        )
    return set(requested)


def _selected_repositories(
    bundle: PlanBundle,
    repository_ids: Optional[Set[str]],
) -> List[Dict[str, Any]]:
    if repository_ids is None:
        return sorted(
            bundle.repositories,
            key=lambda row: str(row.get('repository_id') or ''),
        )
    return [
        row for row in sorted(
            bundle.repositories,
            key=lambda item: str(item.get('repository_id') or ''),
        )
        if str(row.get('repository_id') or '') in repository_ids
    ]


def _display_input_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPOSITORY_ROOT.resolve()))
    except ValueError:
        return str(path)


def validate_checkpoint(
    path: Path,
    bundle: PlanBundle,
    repository_ids_file_sha256: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    checkpoint = load_json(path)
    expected = plan_lock(bundle, repository_ids_file_sha256)
    actual = checkpoint.get('plan_lock') or {
        key: checkpoint.get(key) for key in expected
    }
    for key, value in expected.items():
        if actual.get(key) != value:
            raise PlanValidationError(
                f'Checkpoint lock mismatch for {key}; refusing resume with old state'
            )
    return checkpoint


def _repo_workflows(repository: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        dict(item) for item in (repository.get('workflows') or [])
        if isinstance(item, Mapping)
    ]


def _repository_status_label(repository: Mapping[str, Any]) -> str:
    return str(repository.get('plan_status') or '')


def _base_repository_info(repository: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'repository_id': str(repository.get('repository_id') or ''),
        'repository_name': str(repository.get('repository_name') or ''),
        'default_branch': str(repository.get('default_branch') or ''),
        'aliases': (),
    }


@dataclass
class FetchResult:
    status: str
    records: List[Dict[str, Any]] = field(default_factory=list)
    raw_records: List[Dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    page_sizes: List[int] = field(default_factory=list)
    pagination_complete: bool = False
    termination_reason: str = 'not_started'
    last_page_size: int = 0
    http_status: Optional[int] = None
    error: Optional[str] = None
    workflow_statuses: List[Dict[str, Any]] = field(default_factory=list)
    rate_limit_headers: Dict[str, str] = field(default_factory=dict)
    request_events: List[Dict[str, Any]] = field(default_factory=list)
    waits: List[int] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


def _fake_normalize(repository: Mapping[str, Any], run: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    run_id = run.get('id', run.get('run_id'))
    path = str(run.get('path') or run.get('workflow_path') or '')
    created_at = str(run.get('created_at') or '')
    if run_id in (None, '') or not path or not created_at:
        return None
    return {
        '_id': f"{repository.get('repository_id')}:{run_id}:{run.get('run_attempt') or 1}",
        'repository_id': str(repository.get('repository_id') or ''),
        'repository_name': repository.get('repository_name'),
        'workflow_id': run.get('workflow_id'),
        'workflow_path': path,
        'workflow_name': run.get('name'),
        'run_id': run_id,
        'run_number': run.get('run_number'),
        'run_attempt': run.get('run_attempt') or 1,
        'status': str(run.get('status') or 'completed').lower(),
        'conclusion': str(run.get('conclusion') or '').lower(),
        'event': str(run.get('event') or '').lower(),
        'head_branch': run.get('head_branch', repository.get('default_branch')),
        'head_sha': run.get('head_sha', 'fake-sha'),
        'created_at': created_at,
        'updated_at': run.get('updated_at', created_at),
        'run_started_at': run.get('run_started_at', created_at),
        'duration_seconds': run.get('duration_seconds'),
        'url': run.get('url'),
        'html_url': run.get('html_url'),
        'source': 'fake_github_actions_api',
    }


@dataclass(frozen=True)
class FakeReply:
    """One deterministic fake HTTP response/page."""

    status: int = 200
    runs: Tuple[Mapping[str, Any], ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    @classmethod
    def page(cls, runs: Sequence[Mapping[str, Any]]) -> 'FakeReply':
        return cls(status=200, runs=tuple(copy.deepcopy(list(runs))))


class FakeGitHubActionsClient:
    """Offline client implementing the production client's observable contract.

    It deliberately records retries and waits rather than sleeping.  The
    production executor never uses this class; it is only for deterministic
    unit tests and local simulations.
    """

    def __init__(
        self,
        scripts: Optional[Mapping[Tuple[str, str, Optional[str]], Sequence[FakeReply]]] = None,
        *,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.scripts = {
            key: list(value) for key, value in (scripts or {}).items()
        }
        self.retries = retries
        self.live_timestamps = False
        self.calls: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []

    def _script_for(
        self,
        repository: Mapping[str, Any],
        mode: str,
        workflow_id: Optional[str],
    ) -> List[FakeReply]:
        key = (str(repository.get('repository_id') or ''), mode, workflow_id)
        if key in self.scripts:
            return list(self.scripts[key])
        fallback = (str(repository.get('repository_id') or ''), mode, None)
        if fallback in self.scripts:
            return list(self.scripts[fallback])
        return [FakeReply.page(())]

    def drain_events(self) -> List[Dict[str, Any]]:
        events = self._events
        self._events = []
        return events

    def fetch_repository(
        self,
        repository: Mapping[str, Any],
        as_of: Any,
        window_start: Any,
        max_pages: int,
        workflow_filters: Optional[Sequence[Mapping[str, Any]]] = None,
        workflow_paths: Optional[Set[str]] = None,
    ) -> FetchResult:
        filters = [
            dict(item) for item in (workflow_filters or [])
            if str(item.get('workflow_id') or '').strip()
        ]
        mode = 'workflow-specific' if filters else 'repository-wide'
        all_records: List[Dict[str, Any]] = []
        all_raw: List[Dict[str, Any]] = []
        statuses: List[Dict[str, Any]] = []
        total_pages = 0
        page_sizes: List[int] = []
        events: List[Dict[str, Any]] = []
        waits: List[int] = []
        final_http: Optional[int] = None
        final_error: Optional[str] = None
        final_headers: Dict[str, str] = {}
        final_complete = True
        final_reason = 'not_started'

        query_filters = filters or [{'workflow_id': None, 'workflow_path': ''}]
        for filter_item in query_filters:
            workflow_id = str(filter_item.get('workflow_id') or '') or None
            workflow_path = str(filter_item.get('workflow_path') or '')
            scripted_pages = self._script_for(repository, mode, workflow_id)
            page_index = 0
            pages = 0
            endpoint_records: List[Dict[str, Any]] = []
            endpoint_raw: List[Dict[str, Any]] = []
            endpoint_sizes: List[int] = []
            endpoint_events: List[Dict[str, Any]] = []
            endpoint_waits: List[int] = []
            endpoint_status = 'no_runs'
            endpoint_complete = False
            endpoint_reason = 'not_started'
            endpoint_http: Optional[int] = None
            endpoint_error: Optional[str] = None
            while page_index < len(scripted_pages) or not scripted_pages:
                reply = scripted_pages[page_index] if page_index < len(scripted_pages) else FakeReply.page(())
                retry_number = 0
                while True:
                    event = {
                        'event_type': 'http_response' if reply.status else 'request_error',
                        'url': (
                            f"https://api.github.com/repos/{repository.get('repository_name')}/"
                            + ('actions/runs' if mode == 'repository-wide'
                               else f"actions/workflows/{workflow_id}/runs")
                            + f"?page={pages + 1}"
                        ),
                        'attempt': retry_number + 1,
                        'http_status': reply.status,
                        'rate_limit_headers': dict(reply.headers),
                    }
                    if reply.error:
                        event['error'] = reply.error
                    endpoint_events.append(event)
                    self._events.append(event)
                    if reply.status == 200:
                        final_headers = dict(reply.headers)
                        break
                    final_headers = dict(reply.headers)
                    rate_limited = (
                        reply.status == 429
                        or (reply.status == 403 and (
                            str(reply.headers.get('X-RateLimit-Remaining', '')) == '0'
                            or reply.headers.get('Retry-After') is not None
                        ))
                    )
                    retryable = rate_limited or reply.status in {500, 502, 503, 504}
                    if reply.status == 403 and not rate_limited:
                        endpoint_status = 'forbidden'
                        endpoint_reason = 'ordinary_forbidden'
                        endpoint_http = 403
                        endpoint_complete = False
                        break
                    if reply.status in {404, 410, 451}:
                        endpoint_status = 'not_found'
                        endpoint_reason = f'terminal_http_{reply.status}'
                        endpoint_http = reply.status
                        endpoint_complete = False
                        break
                    if not retryable or retry_number >= self.retries:
                        endpoint_status = 'retryable_failure' if retryable else 'error'
                        endpoint_reason = 'retry_exhausted' if retryable else 'api_error'
                        endpoint_http = reply.status or None
                        endpoint_error = reply.error or f'HTTP {reply.status}'
                        endpoint_complete = False
                        break
                    delay = int(reply.headers.get('Retry-After') or (2 ** retry_number))
                    endpoint_waits.append(delay)
                    waits.append(delay)
                    endpoint_events.append({
                        'event_type': 'retry_scheduled',
                        'retry_after_seconds': delay,
                        'http_status': reply.status,
                        'url': event['url'],
                    })
                    retry_number += 1
                    # A scripted next response represents the next retry of
                    # the same page.  If no next response is provided, repeat
                    # the final error until the finite retry budget expires.
                    if page_index + 1 < len(scripted_pages):
                        page_index += 1
                        reply = scripted_pages[page_index]
                if endpoint_status in {'forbidden', 'not_found', 'retryable_failure', 'error'}:
                    break

                pages += 1
                total_pages += 1
                runs = [dict(run) for run in reply.runs]
                endpoint_raw.extend(copy.deepcopy(runs))
                all_raw.extend(copy.deepcopy(runs))
                size = len(runs)
                endpoint_sizes.append(size)
                page_sizes.append(size)
                if not runs:
                    endpoint_status = 'ok' if endpoint_records else 'no_runs'
                    endpoint_complete = True
                    endpoint_reason = 'empty_page'
                    break
                for raw in runs:
                    created = parse_timestamp(raw.get('created_at'))
                    if created is None:
                        continue
                    if parse_timestamp(str(as_of)) and created > parse_timestamp(str(as_of)):
                        continue
                    if parse_timestamp(str(window_start)) and created < parse_timestamp(str(window_start)):
                        continue
                    if str(raw.get('status') or 'completed').lower() != 'completed':
                        continue
                    if str(raw.get('head_branch') or repository.get('default_branch')) != str(repository.get('default_branch') or ''):
                        continue
                    normalized = _fake_normalize(repository, raw)
                    if normalized is None:
                        continue
                    if workflow_paths and normalized.get('workflow_path') not in workflow_paths:
                        continue
                    if workflow_id and str(normalized.get('workflow_id') or '') != workflow_id:
                        continue
                    endpoint_records.append(normalized)
                if size < PAGE_SIZE:
                    endpoint_status = 'ok' if endpoint_records else 'no_runs'
                    endpoint_complete = True
                    endpoint_reason = 'short_page'
                    break
                if pages >= max_pages:
                    endpoint_status = 'ok' if endpoint_records else 'no_runs'
                    endpoint_complete = False
                    endpoint_reason = 'max_pages_reached'
                    break
                page_index += 1
                continue

            statuses.append({
                'workflow_id': workflow_id,
                'workflow_path': workflow_path,
                'status': endpoint_status,
                'records': len(endpoint_records),
                'pages': pages,
                'complete': endpoint_complete,
                'pagination_complete': endpoint_complete,
                'termination_reason': endpoint_reason,
                'last_page_size': endpoint_sizes[-1] if endpoint_sizes else 0,
                'http_status': endpoint_http,
                'error': endpoint_error,
            })
            all_records.extend(endpoint_records)
            events.extend(endpoint_events)
            final_http = endpoint_http or final_http
            final_error = endpoint_error or final_error
            final_complete = final_complete and endpoint_complete
            final_reason = endpoint_reason
            if endpoint_status in {'forbidden', 'not_found', 'retryable_failure', 'error'}:
                break
            if mode == 'repository-wide':
                break

        if any(item.get('status') == 'forbidden' for item in statuses):
            status = 'forbidden'
        elif any(item.get('status') == 'retryable_failure' for item in statuses):
            status = 'retryable_failure'
        elif any(item.get('status') == 'error' for item in statuses):
            status = 'error'
        elif any(item.get('status') == 'not_found' for item in statuses):
            status = 'not_found'
        elif all_records:
            status = 'ok'
        else:
            status = 'no_runs'
        self.calls.append({
            'repository_id': str(repository.get('repository_id') or ''),
            'mode': mode,
            'workflow_ids': [item.get('workflow_id') for item in filters],
            'max_pages': max_pages,
        })
        return FetchResult(
            status=status,
            records=all_records,
            raw_records=all_raw,
            pages=total_pages,
            page_sizes=page_sizes,
            pagination_complete=final_complete,
            termination_reason=final_reason,
            last_page_size=page_sizes[-1] if page_sizes else 0,
            http_status=final_http,
            error=final_error,
            workflow_statuses=statuses,
            request_events=events,
            waits=waits,
            rate_limit_headers=final_headers,
        )


class RealClientAdapter:
    """Adapter around the existing supplement client, imported only at execute time."""

    def __init__(self, token: str, retries: int = DEFAULT_RETRIES) -> None:
        # This import is intentionally inside the explicit execute path.
        from scripts.supplement_default_branch_runs import GitHubActionsClient

        self._events: List[Dict[str, Any]] = []
        self.live_timestamps = True
        self._context: Dict[str, Any] = {}
        self._event_sink: Optional[Callable[[Mapping[str, Any]], None]] = None

        def observe(event: Mapping[str, Any]) -> None:
            item = dict(event)
            item.update(self._context)
            item['observed_at'] = timestamp_text(datetime.now(timezone.utc))
            item = redact_sensitive(item)
            self._events.append(item)
            if self._event_sink is not None:
                self._event_sink(item)

        self._client = GitHubActionsClient(
            token,
            retries=retries,
            request_observer=observe,
        )

    def drain_events(self) -> List[Dict[str, Any]]:
        if self._event_sink is not None:
            self._events = []
            return []
        events = self._events
        self._events = []
        return events

    def set_context(self, **context: Any) -> None:
        self._context = redact_sensitive(context)

    def set_event_sink(self, sink: Optional[Callable[[Mapping[str, Any]], None]]) -> None:
        self._event_sink = sink

    def fetch_repository(
        self,
        repository: Mapping[str, Any],
        as_of: datetime,
        window_start: datetime,
        max_pages: int,
        workflow_filters: Optional[Sequence[Mapping[str, Any]]] = None,
        workflow_paths: Optional[Set[str]] = None,
    ) -> Any:
        # Lazy import of the established RepositoryInfo/parse helpers keeps
        # dry-run imports free of a network client and token loading.
        try:
            from scripts.supplement_default_branch_runs import RepositoryInfo
        except ModuleNotFoundError:
            from supplement_default_branch_runs import RepositoryInfo

        info = RepositoryInfo(
            repository_id=str(repository.get('repository_id') or ''),
            repository_name=str(repository.get('repository_name') or ''),
            default_branch=str(repository.get('default_branch') or ''),
            aliases=(),
        )
        return self._client.fetch_repository(
            info,
            as_of,
            window_start,
            max_pages,
            workflow_filters=workflow_filters,
            workflow_paths=workflow_paths,
        )


def build_real_client(retries: int = DEFAULT_RETRIES) -> RealClientAdapter:
    """Create a network client only after the caller selected ``--execute``."""
    try:
        from scripts.supplement_workflow_snapshots import load_token
    except ModuleNotFoundError:
        from supplement_workflow_snapshots import load_token

    token = load_token(REPOSITORY_ROOT)
    if not token:
        raise RuntimeError('No GITHUB_TOKEN/GH_TOKEN was found for explicit execute mode')
    return RealClientAdapter(token, retries=retries)


def _coerce_fetch_result(result: Any) -> FetchResult:
    if isinstance(result, FetchResult):
        return result
    return FetchResult(
        status=str(getattr(result, 'status', 'error') or 'error'),
        records=list(getattr(result, 'records', None) or []),
        raw_records=list(getattr(result, 'raw_records', None) or []),
        pages=int(getattr(result, 'pages', 0) or 0),
        pagination_complete=bool(getattr(result, 'pagination_complete', False)),
        termination_reason=str(getattr(result, 'termination_reason', 'unknown') or 'unknown'),
        last_page_size=int(getattr(result, 'last_page_size', 0) or 0),
        http_status=getattr(result, 'http_status', None),
        error=getattr(result, 'error', None),
        workflow_statuses=list(getattr(result, 'workflow_statuses', None) or []),
        rate_limit_headers=dict(getattr(result, 'rate_limit_headers', None) or {}),
    )


def _drain_client_events(client: Any) -> List[Dict[str, Any]]:
    drain = getattr(client, 'drain_events', None)
    if callable(drain):
        return [redact_sensitive(item) for item in (drain() or [])]
    return []


def _fetch_once(
    client: Any,
    repository: Mapping[str, Any],
    strategy: str,
    as_of: datetime,
    window_start: datetime,
    max_pages: int,
    targets: Sequence[Mapping[str, Any]],
    phase: str = 'query',
) -> Tuple[List[FetchResult], List[Dict[str, Any]]]:
    mode = plan_strategy_mode(strategy)
    if mode is None:
        raise PlanValidationError(f'Cannot execute unknown endpoint strategy: {strategy}')
    set_context = getattr(client, 'set_context', None)
    if callable(set_context):
        set_context(
            repository_id=str(repository.get('repository_id') or ''),
            repository_name=str(repository.get('repository_name') or ''),
            endpoint_strategy=strategy,
            phase=phase,
        )
    results: List[FetchResult] = []
    def call_client(**kwargs: Any) -> FetchResult:
        started = timestamp_text(datetime.now(timezone.utc)) if getattr(client, 'live_timestamps', False) else None
        result = _coerce_fetch_result(client.fetch_repository(**kwargs))
        result.started_at = started
        result.finished_at = (
            timestamp_text(datetime.now(timezone.utc))
            if getattr(client, 'live_timestamps', False) else None
        )
        return result
    if mode == 'repository-wide':
        result = call_client(
            repository=repository,
            as_of=as_of,
            window_start=window_start,
            max_pages=max_pages,
            workflow_filters=None,
            workflow_paths=None,
        )
        results.append(result)
    else:
        missing = [
            str(target.get('workflow_path') or '')
            for target in targets
            if not str(target.get('workflow_id') or '').strip()
        ]
        if missing:
            raise PlanValidationError(
                f'Workflow-specific endpoint requested without IDs: {missing[:3]}'
            )
        for target in targets:
            result = call_client(
                repository=repository,
                as_of=as_of,
                window_start=window_start,
                max_pages=max_pages,
                workflow_filters=[{
                    'workflow_id': str(target.get('workflow_id')),
                    'workflow_path': str(target.get('workflow_path') or ''),
                }],
                workflow_paths=None,
            )
            results.append(_coerce_fetch_result(result))
    events = _drain_client_events(client)
    return results, events


def _is_rate_limit_result(result: FetchResult) -> bool:
    if result.http_status == 429:
        return True
    if result.http_status == 403:
        headers = result.rate_limit_headers
        return str(headers.get('X-RateLimit-Remaining', '')) == '0' or 'Retry-After' in headers
    return False


def _result_is_abnormal(result: FetchResult) -> bool:
    return (
        result.termination_reason in {'api_error', 'max_pages_reached', 'retry_exhausted'}
        or result.status in {'error', 'retryable_failure'}
    )


def _result_has_terminal_http(result: FetchResult) -> bool:
    return result.http_status in {404, 410, 451} or result.termination_reason.startswith('terminal_http_')


def _target_record_matches(target: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    if str(record.get('workflow_path') or '') != str(target.get('workflow_path') or ''):
        return False
    workflow_id = str(target.get('workflow_id') or '')
    if workflow_id and str(record.get('workflow_id') or '') != workflow_id:
        return False
    return True


def _decorate_request_events(
    events: Sequence[Mapping[str, Any]],
    repository: Mapping[str, Any],
    strategy: str,
    phase: str,
) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        item = redact_sensitive(dict(event))
        item.update({
            'repository_id': str(repository.get('repository_id') or ''),
            'repository_name': str(repository.get('repository_name') or ''),
            'endpoint_strategy': strategy,
            'phase': phase,
            'sequence_in_phase': index,
        })
        decorated.append(item)
    return decorated


def _combine_results(results: Sequence[FetchResult]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    raw: List[Dict[str, Any]] = []
    seen_normalized: Set[MemoryRunKey] = set()
    seen_raw: Set[str] = set()
    for result in results:
        for item in result.raw_records:
            key = _raw_fallback_key(item)
            if key not in seen_raw:
                seen_raw.add(key)
                raw.append(copy.deepcopy(item))
        for item in result.records:
            key = _memory_run_key(item)
            if key not in seen_normalized:
                seen_normalized.add(key)
                records.append(copy.deepcopy(item))
    return records, raw


def execute_repository(
    repository: Mapping[str, Any],
    client: Any,
    *,
    as_of: datetime,
    window_start: datetime,
    max_pages: int = DEFAULT_MAX_PAGES,
    canonical_run_keys: Optional[Set[int]] = None,
    existing_normalized_keys: Optional[Set[int]] = None,
    existing_raw_keys: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Execute one repository plan with explicit fallback and target mapping."""
    repository_id = str(repository.get('repository_id') or '')
    targets = _repo_workflows(repository)
    plan_status = _repository_status_label(repository)
    common = {
        'repository_id': repository_id,
        'repository_name': str(repository.get('repository_name') or ''),
        'plan_status': plan_status,
        'target_workflow_count': len(targets),
    }
    if plan_status in {'terminal_review', 'already_resolved'}:
        status = {
            **common,
            'execution_status': 'terminal_outcome' if plan_status == 'terminal_review' else 'already_resolved',
            'skip_reason': 'plan_terminal' if plan_status == 'terminal_review' else 'plan_already_resolved',
            'calls': 0,
            'fallback_triggered': False,
        }
        return {
            'repository_status': status,
            'workflow_statuses': [],
            'raw_rows': [],
            'normalized_rows': [],
            'request_events': [],
            'terminal': True,
        }

    primary_strategy = str(repository.get('primary_endpoint_strategy') or '')
    fallback_strategy = str(repository.get('fallback_endpoint_strategy') or '').strip()
    if plan_strategy_mode(primary_strategy) == 'workflow-specific' and any(
        not str(target.get('workflow_id') or '').strip() for target in targets
    ):
        raise PlanValidationError(f'Illegal workflow-specific primary plan for {repository_id}')

    results: List[FetchResult] = []
    request_events: List[Dict[str, Any]] = []
    access_probe = plan_status == 'access_probe_required'
    fallback_triggered = False
    phases: List[str] = []

    if access_probe:
        probe_results, probe_events = _fetch_once(
            client,
            repository,
            'repository-wide-access-probe',
            as_of,
            window_start,
            1,
            targets,
            phase='access_probe',
        )
        request_events.extend(_decorate_request_events(
            probe_events, repository, 'repository-wide-access-probe', 'access_probe'
        ))
        probe = probe_results[-1] if probe_results else FetchResult(status='error', error='empty probe result')
        if probe.http_status == 403 and not _is_rate_limit_result(probe):
            return {
                'repository_status': {
                    **common,
                    'execution_status': 'access_denied',
                    'http_status': 403,
                    'termination_reason': probe.termination_reason,
                    'access_probe': True,
                    'calls': 1,
                    'fallback_triggered': False,
                },
                'workflow_statuses': [],
                'raw_rows': [],
                'normalized_rows': [],
                'request_events': request_events,
                'terminal': True,
            }
        if _result_has_terminal_http(probe):
            return {
                'repository_status': {
                    **common,
                    'execution_status': 'terminal_outcome',
                    'http_status': probe.http_status,
                    'termination_reason': probe.termination_reason,
                    'access_probe': True,
                    'calls': 1,
                    'fallback_triggered': False,
                },
                'workflow_statuses': [],
                'raw_rows': [],
                'normalized_rows': [],
                'request_events': request_events,
                'terminal': True,
            }
        if probe.status == 'retryable_failure' or (probe.status == 'forbidden' and _is_rate_limit_result(probe)):
            return {
                'repository_status': {
                    **common,
                    'execution_status': 'retryable_failure',
                    'http_status': probe.http_status,
                    'termination_reason': probe.termination_reason,
                    'access_probe': True,
                    'calls': 1,
                    'fallback_triggered': False,
                },
                'workflow_statuses': [],
                'raw_rows': [],
                'normalized_rows': [],
                'request_events': request_events,
                'terminal': False,
            }
        phases.append('access_probe')

    primary_results, primary_events = _fetch_once(
        client,
        repository,
        primary_strategy,
        as_of,
        window_start,
        max_pages,
        targets,
        phase='primary',
    )
    results.extend(primary_results)
    request_events.extend(_decorate_request_events(
        primary_events, repository, primary_strategy, 'primary'
    ))
    phases.append('primary')

    primary_abnormal = any(_result_is_abnormal(result) for result in primary_results)
    fallback_allowed = bool(fallback_strategy) and primary_abnormal
    missing_ids = any(not str(target.get('workflow_id') or '').strip() for target in targets)
    if fallback_allowed:
        fallback_mode = plan_strategy_mode(fallback_strategy)
        if fallback_mode == 'workflow-specific' and missing_ids:
            fallback_allowed = False
        if fallback_allowed:
            fallback_triggered = True
            fallback_results, fallback_events = _fetch_once(
                client,
                repository,
                fallback_strategy,
                as_of,
                window_start,
                max_pages,
                targets,
                phase='fallback',
            )
            results.extend(fallback_results)
            request_events.extend(_decorate_request_events(
                fallback_events, repository, fallback_strategy, 'fallback'
            ))
            phases.append('fallback')

    all_records, all_raw = _combine_results(results)
    raw_keys: Set[str] = set(existing_raw_keys or set())
    canonical_keys = canonical_run_keys or set()
    normalized_keys = set(existing_normalized_keys or set())
    raw_rows: List[Dict[str, Any]] = []
    normalized_rows: List[Dict[str, Any]] = []
    for raw in all_raw:
        key = _raw_fallback_key(raw)
        if key in (existing_raw_keys or set()):
            # A resumed/repeated query keeps the first raw provenance row and
            # never appends the same run again.
            continue
        raw_rows.append({
            'repository_id': repository_id,
            'repository_name': repository.get('repository_name'),
            'run_key': key,
            'raw_run': redact_sensitive(raw),
            'provenance': {
                'phase': 'phase2.1',
                'primary_endpoint_strategy': primary_strategy,
                'fallback_triggered': fallback_triggered,
            },
        })
        raw_keys.add(key)

    for record in all_records:
        if not any(_target_record_matches(target, record) for target in targets):
            # Repository-wide responses for non-target paths remain in raw
            # provenance only and never silently enter the target increment.
            continue
        key = compact_run_key(record)
        if key is None or key in canonical_keys or key in normalized_keys:
            continue
        enriched = redact_sensitive(record)
        enriched['acquisition_provenance'] = {
            'phase': 'phase2.1',
            'primary_endpoint_strategy': primary_strategy,
            'fallback_triggered': fallback_triggered,
        }
        normalized_rows.append(enriched)
        if key is not None:
            normalized_keys.add(key)

    target_statuses: List[Dict[str, Any]] = []
    mode = plan_strategy_mode(fallback_strategy if fallback_triggered else primary_strategy)
    for target in targets:
        matches = [record for record in all_records if _target_record_matches(target, record)]
        target_id = str(target.get('workflow_id') or '')
        target_path = str(target.get('workflow_path') or '')
        specific_statuses = [
            item for result in results for item in result.workflow_statuses
            if (
                target_id and str(item.get('workflow_id') or '') == target_id
            )
        ]
        # In workflow-specific mode, the repository-level aggregate is not a
        # valid proxy for this target.  For example, one workflow can be a
        # terminal 404 while a second workflow has completed normally with an
        # empty page.  Resolve each target from its own endpoint status first.
        if mode == 'workflow-specific':
            specific_complete = bool(specific_statuses) and all(
                bool(item.get('pagination_complete', item.get('complete', False)))
                for item in specific_statuses
            )
            specific_not_found = any(
                item.get('status') == 'not_found'
                or item.get('http_status') in {404, 410, 451}
                for item in specific_statuses
            )
            specific_abnormal = any(
                item.get('status') in {
                    'forbidden', 'error', 'retryable_failure'
                }
                or item.get('termination_reason') in {
                    'api_error', 'retry_exhausted', 'max_pages_reached'
                }
                for item in specific_statuses
            )
            if specific_not_found:
                target_execution = 'terminal_exclusion'
            elif specific_abnormal or not specific_complete:
                target_execution = 'incomplete'
            elif matches:
                target_execution = 'complete'
            else:
                target_execution = 'no_qualifying_runs'
            target_query_complete = specific_complete
        else:
            repository_query_complete = bool(results) and all(
                result.pagination_complete for result in results
            )
            repository_not_found = any(
                _result_has_terminal_http(result) for result in results
            )
            repository_abnormal = any(
                _result_is_abnormal(result) for result in results
            )
            if repository_not_found:
                target_execution = 'terminal_exclusion'
            elif repository_abnormal or not repository_query_complete:
                target_execution = 'incomplete'
            elif matches:
                target_execution = 'complete'
            else:
                target_execution = 'no_qualifying_runs'
            target_query_complete = repository_query_complete
        target_statuses.append({
            'repository_id': repository_id,
            'workflow_path': target_path,
            'workflow_id': target_id or None,
            'execution_status': target_execution,
            'records': len(matches),
            'query_mode': mode,
            'query_complete': target_query_complete,
            'fallback_triggered': fallback_triggered,
            'source_phases': phases,
        })

    all_complete = bool(results) and all(result.pagination_complete for result in results)
    has_retryable = any(result.status in {'error', 'retryable_failure', 'forbidden'} for result in results)
    has_terminal = any(_result_has_terminal_http(result) for result in results)
    terminal_workflow_statuses = {
        'complete', 'no_qualifying_runs', 'terminal_exclusion'
    }
    terminal_exclusion_count = sum(
        status.get('execution_status') == 'terminal_exclusion'
        for status in target_statuses
    )
    unresolved_workflow_count = sum(
        status.get('execution_status') not in terminal_workflow_statuses
        for status in target_statuses
    )
    all_target_workflows_terminal = bool(target_statuses) and (
        unresolved_workflow_count == 0
    )
    if all_target_workflows_terminal:
        # A repository is terminal when every planned workflow has a terminal
        # target-level outcome.  A 404 target must not poison another target
        # that completed with an empty page, although the aggregate pagination
        # flag may remain false because the 404 endpoint did not paginate.
        if terminal_exclusion_count:
            execution_status = 'terminal_outcome'
        elif any(
            status.get('execution_status') == 'complete'
            for status in target_statuses
        ):
            execution_status = 'complete'
        else:
            execution_status = 'no_runs'
    elif has_terminal and not all_complete:
        execution_status = 'terminal_outcome'
    elif has_retryable:
        execution_status = 'retryable_failure'
    elif all_complete:
        execution_status = 'complete' if any(all_records) else 'no_runs'
    else:
        execution_status = 'incomplete'
    canonical_duplicate_count = 0
    for record in all_records:
        key = compact_run_key(record)
        if key is not None and key in canonical_keys:
            canonical_duplicate_count += 1
    repository_status = {
        **common,
        'execution_status': execution_status,
        'query_mode': mode,
        'primary_endpoint_strategy': primary_strategy,
        'fallback_endpoint_strategy': fallback_strategy or None,
        'fallback_available': bool(fallback_strategy) and not (
            plan_strategy_mode(fallback_strategy) == 'workflow-specific' and missing_ids
        ),
        'fallback_unavailable_reason': (
            'missing_workflow_id'
            if fallback_strategy
            and plan_strategy_mode(fallback_strategy) == 'workflow-specific'
            and missing_ids
            else None
        ),
        'fallback_triggered': fallback_triggered,
        'pagination_complete': all_complete,
        'pages': sum(result.pages for result in results),
        'page_sizes': [size for result in results for size in result.page_sizes],
        'last_page_size': results[-1].last_page_size if results else 0,
        'endpoint_timings': [
            {
                'started_at': result.started_at,
                'finished_at': result.finished_at,
            }
            for result in results
        ],
        'termination_reasons': [result.termination_reason for result in results],
        'http_statuses': [result.http_status for result in results if result.http_status is not None],
        'raw_records_seen': len(all_raw),
        'normalized_records_seen': len(all_records),
        'normalized_records_new': len(normalized_rows),
        'canonical_duplicates': canonical_duplicate_count,
        'terminal_exclusion_count': terminal_exclusion_count,
        'unresolved_workflow_count': unresolved_workflow_count,
        'workflow_status_counts': {
            status_name: sum(
                status.get('execution_status') == status_name
                for status in target_statuses
            )
            for status_name in sorted({
                status.get('execution_status') for status in target_statuses
            })
        },
        'calls': len(results),
        'source_phases': phases,
    }
    return {
        'repository_status': repository_status,
        'workflow_statuses': target_statuses,
        'raw_rows': raw_rows,
        'normalized_rows': normalized_rows,
        'request_events': request_events,
        'terminal': execution_status in TERMINAL_EXECUTION_STATUSES,
    }


def _empty_output_files(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        RAW_OUTPUT,
        NORMALIZED_OUTPUT,
        REQUEST_OUTPUT,
        REPOSITORY_STATUS_OUTPUT,
        WORKFLOW_STATUS_OUTPUT,
    ):
        (output_dir / name).write_text('', encoding='utf-8', newline='')


def _dry_run_repository_status(repository: Mapping[str, Any]) -> Dict[str, Any]:
    primary = str(repository.get('primary_endpoint_strategy') or '')
    fallback = str(repository.get('fallback_endpoint_strategy') or '')
    fallback_mode = plan_strategy_mode(fallback)
    missing_ids = int(repository.get('missing_workflow_id_count') or 0)
    fallback_available = bool(fallback) and not (
        fallback_mode == 'workflow-specific' and missing_ids > 0
    )
    return {
        'repository_id': str(repository.get('repository_id') or ''),
        'repository_name': str(repository.get('repository_name') or ''),
        'plan_status': str(repository.get('plan_status') or ''),
        'execution_status': 'dry_run_planned',
        'primary_endpoint_strategy': primary,
        'fallback_endpoint_strategy': fallback or None,
        'fallback_available': fallback_available,
        'target_workflow_count': int(repository.get('target_workflow_count') or 0),
        'estimated_min_requests': int(repository.get('estimated_min_requests') or 0),
        'estimated_max_requests': int(repository.get('estimated_max_requests') or 0),
        'missing_workflow_id_count': missing_ids,
        'true_conflict_workflow_count': int(repository.get('true_conflict_workflow_count') or 0),
        'access_probe_required': str(repository.get('plan_status') or '') == 'access_probe_required',
        'network_requested': False,
    }


def _dry_run_workflow_status(repository: Mapping[str, Any], workflow: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'repository_id': str(repository.get('repository_id') or workflow.get('repository_id') or ''),
        'repository_name': str(repository.get('repository_name') or workflow.get('repository_name') or ''),
        'workflow_path': str(workflow.get('workflow_path') or ''),
        'workflow_id': str(workflow.get('workflow_id') or '') or None,
        'workflow_id_source': workflow.get('workflow_id_source'),
        'plan_status': str(repository.get('plan_status') or workflow.get('workflow_plan_status') or ''),
        'execution_status': 'dry_run_planned',
        'query_coverage_status': workflow.get('query_coverage_status'),
        'analysis_ready': bool(workflow.get('analysis_ready')),
        'coverage_confirmed': bool(workflow.get('coverage_confirmed')),
        'retry_reason': workflow.get('retry_reason'),
        'network_requested': False,
    }


def _batch_count(value: int, batch_size: int) -> int:
    return math.ceil(value / batch_size) if value else 0


def dry_run(
    bundle: PlanBundle,
    output_dir: Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    repository_ids: Optional[Set[str]] = None,
    repository_ids_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a complete, deterministic local preview without API data."""
    _empty_output_files(output_dir)
    repositories = _selected_repositories(bundle, repository_ids)
    repository_ids_file_sha256 = (
        sha256_file(repository_ids_file)
        if repository_ids_file is not None else None
    )
    workflows_by_repo: Dict[str, List[Dict[str, Any]]] = {}
    for workflow in bundle.workflows:
        workflows_by_repo.setdefault(str(workflow.get('repository_id') or ''), []).append(workflow)
    for rows in workflows_by_repo.values():
        rows.sort(key=lambda row: str(row.get('workflow_path') or ''))

    repo_statuses: List[Dict[str, Any]] = []
    workflow_statuses: List[Dict[str, Any]] = []
    request_events: List[Dict[str, Any]] = []
    for repository in repositories:
        repo_statuses.append(_dry_run_repository_status(repository))
        repository_id = str(repository.get('repository_id') or '')
        request_events.append({
            'event_type': 'dry_run_plan_preview',
            'repository_id': repository_id,
            'repository_name': repository.get('repository_name'),
            'plan_status': repository.get('plan_status'),
            'primary_endpoint_strategy': repository.get('primary_endpoint_strategy'),
            'fallback_endpoint_strategy': repository.get('fallback_endpoint_strategy'),
            'estimated_min_requests': repository.get('estimated_min_requests'),
            'estimated_max_requests': repository.get('estimated_max_requests'),
            'target_workflow_count': repository.get('target_workflow_count'),
            'missing_workflow_id_count': repository.get('missing_workflow_id_count'),
            'network_requested': False,
        })
        for workflow in workflows_by_repo.get(repository_id, []):
            workflow_statuses.append(_dry_run_workflow_status(repository, workflow))

    write_jsonl(output_dir / RAW_OUTPUT, [])
    write_jsonl(output_dir / NORMALIZED_OUTPUT, [])
    write_jsonl(output_dir / REQUEST_OUTPUT, request_events)
    write_jsonl(output_dir / REPOSITORY_STATUS_OUTPUT, repo_statuses)
    write_jsonl(output_dir / WORKFLOW_STATUS_OUTPUT, workflow_statuses)

    plan_status_counts = Counter(str(row.get('plan_status') or '') for row in repositories)
    action_repositories = [
        row for row in repositories
        if str(row.get('plan_status') or '') in {'fetch_required', 'access_probe_required'}
    ]
    endpoint_counts = Counter(
        plan_strategy_mode(row.get('primary_endpoint_strategy')) for row in action_repositories
    )
    endpoint_workflow_counts = Counter()
    for row in action_repositories:
        mode = plan_strategy_mode(row.get('primary_endpoint_strategy'))
        endpoint_workflow_counts[mode] += int(row.get('target_workflow_count') or 0)
    fallback_available = sum(
        bool(_dry_run_repository_status(row).get('fallback_available'))
        for row in action_repositories
    )
    missing_ids = sum(int(row.get('missing_workflow_id_count') or 0) for row in repositories)
    true_conflicts = sum(int(row.get('true_conflict_workflow_count') or 0) for row in repositories)
    estimated_min = sum(int(row.get('estimated_min_requests') or 0) for row in repositories)
    estimated_max = sum(int(row.get('estimated_max_requests') or 0) for row in repositories)
    repository_id_set = {
        str(repo.get('repository_id') or '') for repo in repositories
    }
    summary = {
        'phase': 'RQ2 Phase 2.1',
        'mode': 'dry-run',
        'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
        'network_client_created': False,
        'external_fetch_performed': False,
        'canonical_inputs_modified': False,
        'statistical_models_rerun': False,
        'randomness': {'random_sampling': False, 'seed': None},
        'plan_coverage': {
            'repositories': len(repositories),
            'workflows': len(workflow_statuses),
            'action_repositories': len(action_repositories),
            'action_workflows': sum(int(row.get('target_workflow_count') or 0) for row in action_repositories),
            'fetch_required_repositories': plan_status_counts.get('fetch_required', 0),
            'access_probe_required_repositories': plan_status_counts.get('access_probe_required', 0),
            'terminal_review_repositories': plan_status_counts.get('terminal_review', 0),
            'already_resolved_repositories': plan_status_counts.get('already_resolved', 0),
            'missing_workflow_ids': missing_ids,
            'true_conflict_requery_workflows': true_conflicts,
            'repository_ids_file': (
                _display_input_path(repository_ids_file)
                if repository_ids_file is not None else None
            ),
            'repository_ids_file_sha256': repository_ids_file_sha256,
        },
        'endpoint_strategies': {
            'repository_wide_repositories': endpoint_counts.get('repository-wide', 0),
            'workflow_specific_repositories': endpoint_counts.get('workflow-specific', 0),
            'repository_wide_workflows': endpoint_workflow_counts.get('repository-wide', 0),
            'workflow_specific_workflows': endpoint_workflow_counts.get('workflow-specific', 0),
            'fallback_available_repositories': fallback_available,
            'fallback_unavailable_repositories': len(action_repositories) - fallback_available,
        },
        'estimated_requests': {
            'minimum': estimated_min,
            'maximum': estimated_max,
        },
        'batches': {
            'batch_size': batch_size,
            'repository_batches': _batch_count(len(repositories), batch_size),
            'action_batches': _batch_count(len(action_repositories), batch_size),
            'access_probe_batches': _batch_count(plan_status_counts.get('access_probe_required', 0), batch_size),
        },
        'conservation_checks': {
            'repository_plan_count_matches_status_rows': len(repositories) == len(repo_statuses),
            'workflow_plan_count_matches_status_rows': (
                sum(len(workflows_by_repo.get(str(row.get('repository_id') or ''), [])) for row in repositories)
                == len(workflow_statuses)
            ),
            'workflow_repository_reference_matches': all(
                str(row.get('repository_id') or '') in repository_id_set
                for row in workflow_statuses
            ),
            'raw_api_runs_generated': 0,
            'normalized_api_runs_generated': 0,
        },
        'output_files': {},
    }
    write_json(output_dir / SUMMARY_OUTPUT, summary)
    checkpoint = {
        'schema_version': 'rq2-phase2.1-checkpoint-v1',
        'mode': 'dry-run',
        'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
        'completed_repositories': [],
        'in_flight_repository': None,
        'dry_run_preview_complete': True,
        'raw_records_written': 0,
        'normalized_records_written': 0,
    }
    write_json(output_dir / CHECKPOINT_OUTPUT, checkpoint)

    output_hashes = {
        name: sha256_file(output_dir / name)
        for name in OUTPUT_NAMES
        if name != MANIFEST_OUTPUT
    }
    summary['output_files'] = {
        name: digest for name, digest in output_hashes.items()
        if name != SUMMARY_OUTPUT
    }
    write_json(output_dir / SUMMARY_OUTPUT, summary)
    # Summary changed after its first hash; update the stable hash map and
    # write it once more so the manifest refers to the final summary bytes.
    output_hashes[SUMMARY_OUTPUT] = sha256_file(output_dir / SUMMARY_OUTPUT)
    manifest = {
        'schema_version': 'rq2-phase2.1-execution-manifest-v1',
        'phase': 'phase2.1_unified_acquisition',
        'mode': 'dry-run',
        'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
        'input_files': [
            {'path': 'rq2_acquisition_plan_manifest.json', 'sha256': bundle.manifest_sha256},
            {'path': 'rq2_acquisition_plan_repositories.jsonl', 'sha256': bundle.repository_plan_sha256},
            {'path': 'rq2_acquisition_plan_workflows.jsonl', 'sha256': bundle.workflow_plan_sha256},
        ],
        'output_files': [
            {'path': name, 'sha256': output_hashes[name]}
            for name in sorted(output_hashes)
        ],
        'network_client_created': False,
        'external_fetch_performed': False,
        'canonical_inputs_modified': False,
        'statistical_models_rerun': False,
        'randomness': {'random_sampling': False, 'seed': None},
        'manifest_self_hash': 'excluded_from_manifest_self_reference',
    }
    if repository_ids_file is not None:
        manifest['input_files'].append({
            'path': _display_input_path(repository_ids_file),
            'sha256': repository_ids_file_sha256,
            'roles': ['phase2.2_pilot_repository_allow_list'],
        })
    write_json(output_dir / MANIFEST_OUTPUT, manifest)
    return summary


def _load_run_keys(path: Path) -> Set[int]:
    keys: Set[int] = set()
    if not path.exists():
        return keys
    for row in load_jsonl(path):
        key = compact_run_key(row)
        if key is not None:
            keys.add(key)
    return keys


def _load_canonical_run_keys(bundle: PlanBundle) -> Set[int]:
    for item in bundle.manifest.get('input_files') or []:
        if not isinstance(item, Mapping):
            continue
        roles = {str(role) for role in (item.get('roles') or [])}
        if 'phase2_input:canonical_workflow_runs' in roles:
            path = REPOSITORY_ROOT / str(item.get('path') or '')
            return _load_run_keys(path)
    return set()


def _checkpoint_update(
    path: Path,
    bundle: PlanBundle,
    checkpoint: MutableMapping[str, Any],
    repository_ids_file_sha256: Optional[str] = None,
) -> None:
    checkpoint['plan_lock'] = plan_lock(bundle, repository_ids_file_sha256)
    write_json(path, checkpoint)


def execute_plan(
    bundle: PlanBundle,
    output_dir: Path,
    *,
    client: Any,
    max_pages: int = DEFAULT_MAX_PAGES,
    resume: bool = True,
    stop_after: Optional[int] = None,
    repository_ids: Optional[Set[str]] = None,
    repository_ids_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run an injected client; used by explicit execute mode and offline tests."""
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / CHECKPOINT_OUTPUT
    repository_ids_file_sha256 = (
        sha256_file(repository_ids_file)
        if repository_ids_file is not None else None
    )
    repositories = _selected_repositories(bundle, repository_ids)
    checkpoint = (
        validate_checkpoint(checkpoint_path, bundle, repository_ids_file_sha256)
        if resume else None
    )
    if checkpoint is None:
        checkpoint = {
            'schema_version': 'rq2-phase2.1-checkpoint-v1',
            'mode': 'execute',
            'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
            'repository_ids_file': (
                _display_input_path(repository_ids_file)
                if repository_ids_file is not None else None
            ),
            'repository_ids_file_sha256': repository_ids_file_sha256,
            'completed_repositories': [],
            'in_flight_repository': None,
            'raw_records_written': 0,
            'normalized_records_written': 0,
        }
    for name in (
        RAW_OUTPUT,
        NORMALIZED_OUTPUT,
        REQUEST_OUTPUT,
        REPOSITORY_STATUS_OUTPUT,
        WORKFLOW_STATUS_OUTPUT,
    ):
        (output_dir / name).touch(exist_ok=True)

    # The real adapter streams each token-free response event immediately so
    # an interruption cannot lose completed page events.  The fake client has
    # no sink and is drained deterministically after each scripted request.
    set_event_sink = getattr(client, 'set_event_sink', None)
    if callable(set_event_sink):
        set_event_sink(
            lambda event: append_jsonl(output_dir / REQUEST_OUTPUT, [redact_sensitive(event)])
        )

    canonical_keys = _load_canonical_run_keys(bundle)
    existing_normalized_keys = _load_run_keys(output_dir / NORMALIZED_OUTPUT)
    existing_raw_keys = {
        str(row.get('run_key') or '')
        for row in load_jsonl(output_dir / RAW_OUTPUT)
        if row.get('run_key')
    }
    raw_records_written = int(checkpoint.get('raw_records_written') or 0)
    normalized_records_written = int(
        checkpoint.get('normalized_records_written') or 0
    )
    completed = set(str(value) for value in checkpoint.get('completed_repositories') or [])
    repository_rows = list(load_jsonl(output_dir / REPOSITORY_STATUS_OUTPUT))
    workflow_rows = list(load_jsonl(output_dir / WORKFLOW_STATUS_OUTPUT))
    request_rows = list(load_jsonl(output_dir / REQUEST_OUTPUT))
    processed_this_run = 0
    selected_repository_ids = {
        str(row.get('repository_id') or '') for row in repositories
    }
    for repository in repositories:
        repository_id = str(repository.get('repository_id') or '')
        if repository_id not in selected_repository_ids:
            continue
        if repository_id in completed:
            continue
        checkpoint['in_flight_repository'] = repository_id
        _checkpoint_update(
            checkpoint_path,
            bundle,
            checkpoint,
            repository_ids_file_sha256,
        )
        result = execute_repository(
            repository,
            client,
            as_of=parse_timestamp(FIXED_CUTOFF) or datetime(2026, 6, 9, 23, 59, 59, tzinfo=timezone.utc),
            window_start=parse_timestamp(FIXED_QUERY_START) or datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc),
            max_pages=max_pages,
            canonical_run_keys=canonical_keys,
            existing_normalized_keys=existing_normalized_keys,
            existing_raw_keys=existing_raw_keys,
        )
        append_jsonl(output_dir / RAW_OUTPUT, result['raw_rows'])
        append_jsonl(output_dir / NORMALIZED_OUTPUT, result['normalized_rows'])
        append_jsonl(output_dir / REQUEST_OUTPUT, result['request_events'])
        append_jsonl(output_dir / REPOSITORY_STATUS_OUTPUT, [result['repository_status']])
        append_jsonl(output_dir / WORKFLOW_STATUS_OUTPUT, result['workflow_statuses'])
        raw_records_written += len(result['raw_rows'])
        normalized_records_written += len(result['normalized_rows'])
        repository_rows.append(result['repository_status'])
        workflow_rows.extend(result['workflow_statuses'])
        request_rows.extend(result['request_events'])
        for row in result['normalized_rows']:
            key = compact_run_key(row)
            if key is not None:
                existing_normalized_keys.add(key)
        for row in result['raw_rows']:
            existing_raw_keys.add(str(row.get('run_key') or ''))
        if result.get('terminal'):
            completed.add(repository_id)
            checkpoint['completed_repositories'] = sorted(completed)
        checkpoint['in_flight_repository'] = None
        checkpoint['raw_records_written'] = raw_records_written
        checkpoint['normalized_records_written'] = normalized_records_written
        _checkpoint_update(
            checkpoint_path,
            bundle,
            checkpoint,
            repository_ids_file_sha256,
        )
        processed_this_run += 1
        if stop_after is not None and processed_this_run >= stop_after:
            raise RuntimeError('simulated interruption after requested repository count')

    status_counter = Counter(str(row.get('execution_status') or '') for row in repository_rows)
    summary = {
        'phase': 'RQ2 Phase 2.1',
        'mode': 'execute',
        'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
        'network_client_created': True,
        'external_fetch_performed': True,
        'canonical_inputs_modified': False,
        'statistical_models_rerun': False,
        'repositories_processed': len(repository_rows),
        'workflows_processed': len(workflow_rows),
        'repository_ids_file': (
            _display_input_path(repository_ids_file)
            if repository_ids_file is not None else None
        ),
        'repository_ids_file_sha256': repository_ids_file_sha256,
        'repository_status_counts': dict(sorted(status_counter.items())),
        'completed_repositories': len(completed),
        'raw_records_written': raw_records_written,
        'normalized_records_written': normalized_records_written,
        'output_files': {},
    }
    write_json(output_dir / SUMMARY_OUTPUT, summary)
    output_hashes = {
        name: sha256_file(output_dir / name)
        for name in OUTPUT_NAMES
        if name not in {MANIFEST_OUTPUT, SUMMARY_OUTPUT}
    }
    summary['output_files'] = {
        name: digest for name, digest in output_hashes.items()
        if name != SUMMARY_OUTPUT
    }
    write_json(output_dir / SUMMARY_OUTPUT, summary)
    output_hashes[SUMMARY_OUTPUT] = sha256_file(output_dir / SUMMARY_OUTPUT)
    manifest = {
        'schema_version': 'rq2-phase2.1-execution-manifest-v1',
        'phase': 'phase2.1_unified_acquisition',
        'mode': 'execute',
        'plan_lock': plan_lock(bundle, repository_ids_file_sha256),
        'input_files': [
            {'path': 'rq2_acquisition_plan_manifest.json', 'sha256': bundle.manifest_sha256},
            {'path': 'rq2_acquisition_plan_repositories.jsonl', 'sha256': bundle.repository_plan_sha256},
            {'path': 'rq2_acquisition_plan_workflows.jsonl', 'sha256': bundle.workflow_plan_sha256},
        ],
        'output_files': [
            {'path': name, 'sha256': output_hashes[name]}
            for name in sorted(output_hashes)
        ],
        'network_client_created': True,
        'external_fetch_performed': True,
        'canonical_inputs_modified': False,
        'statistical_models_rerun': False,
        'randomness': {'random_sampling': False, 'seed': None},
        'manifest_self_hash': 'excluded_from_manifest_self_reference',
    }
    if repository_ids_file is not None:
        manifest['input_files'].append({
            'path': _display_input_path(repository_ids_file),
            'sha256': repository_ids_file_sha256,
            'roles': ['phase2.2_pilot_repository_allow_list'],
        })
    write_json(output_dir / MANIFEST_OUTPUT, manifest)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('--dry-run', action='store_true', help='Preview locally; this is the default')
    mode_group.add_argument('--execute', action='store_true', help='Explicitly enable the network client')
    parser.add_argument('--plan-dir', type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument('--max-pages', type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument('--retries', type=int, default=DEFAULT_RETRIES)
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        '--repository-ids-file',
        type=Path,
        help='JSONL allow-list of repository IDs to execute; unknown IDs stop execution',
    )
    parser.add_argument('--resume', action='store_true', help='Resume an execute-mode checkpoint')
    parser.add_argument('--expected-manifest-sha256', default=EXPECTED_PLAN_MANIFEST_SHA256)
    parser.add_argument('--expected-repository-plan-sha256', default=EXPECTED_REPOSITORY_PLAN_SHA256)
    parser.add_argument('--expected-workflow-plan-sha256', default=EXPECTED_WORKFLOW_PLAN_SHA256)
    args = parser.parse_args(argv)
    if args.max_pages <= 0 or args.retries < 0 or args.batch_size <= 0:
        parser.error('--max-pages, --batch-size must be positive and --retries non-negative')
    if args.resume and not args.execute:
        parser.error('--resume is only valid with explicit --execute')

    try:
        bundle = load_and_validate_plan(
            args.plan_dir,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_repository_plan_sha256=args.expected_repository_plan_sha256,
            expected_workflow_plan_sha256=args.expected_workflow_plan_sha256,
        )
        repository_ids = None
        repository_ids_file_sha256 = None
        if args.repository_ids_file is not None:
            repository_ids = load_repository_ids_file(args.repository_ids_file, bundle)
            repository_ids_file_sha256 = sha256_file(args.repository_ids_file)
        checkpoint_path = args.output_dir / CHECKPOINT_OUTPUT
        if checkpoint_path.exists():
            validate_checkpoint(checkpoint_path, bundle, repository_ids_file_sha256)
        if not args.execute:
            summary = dry_run(
                bundle,
                args.output_dir,
                batch_size=args.batch_size,
                repository_ids=repository_ids,
                repository_ids_file=args.repository_ids_file,
            )
        else:
            # This is the only branch that can read a token or construct the
            # real network client.  It is intentionally never used by the
            # Phase 2.1 validation command in this task.
            client = build_real_client(retries=args.retries)
            summary = execute_plan(
                bundle,
                args.output_dir,
                client=client,
                max_pages=args.max_pages,
                resume=args.resume,
                repository_ids=repository_ids,
                repository_ids_file=args.repository_ids_file,
            )
        print(json.dumps(redact_sensitive(summary), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (PlanValidationError, ValueError, RuntimeError) as exc:
        print(f'RQ2 acquisition stopped: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
