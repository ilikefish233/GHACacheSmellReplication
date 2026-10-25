"""Build the canonical data layer shared by RQ1, RQ2, and RQ3.

This script implements the data-alignment order documented in the study
protocol:

1. construct a repository identity table from the complete repository index;
2. normalize the cutoff workflow snapshots and their detector states;
3. derive the latest-version boundaries without inferring missing boundaries;
4. normalize runs onto ``repository_id`` and the three-part run key;
5. build RQ1/RQ2 cohorts; and
6. sample RQ3 repositories from the RQ1 repository frame *before* inspecting
   local history coverage, then materialize only the selected histories.

The command is deliberately local-only.  It does not call GitHub and never
uses the presence of local historical data as an inclusion criterion.  Existing
large source files are streamed; the canonical content store uses hard links
when possible and falls back to a copy when the source is on another volume.

The generated directory is self-contained with respect to workflow content,
while ``*_source_file`` fields and the manifest retain input provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CUTOFF = "2026-06-09T23:59:59Z"
DEFAULT_OUTPUT = ROOT / "data_26_6_9_canonical_20260609"
DEFAULT_REPOSITORY_SOURCE = ROOT / "data_26_6_9_complete_20260609" / "repo_info.jsonl"
DEFAULT_COMPLETE_DATA = ROOT / "data_26_6_9_complete_20260609"
DEFAULT_LEGACY_DATA = ROOT / "data_26_6_9"
DEFAULT_SNAPSHOT_DIR = (
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_latest_snapshot_repair_applied"
)
DEFAULT_SNAPSHOT_SOURCE = DEFAULT_SNAPSHOT_DIR / "workflow_files_at_cutoff.jsonl"
DEFAULT_SCAN_SOURCE = DEFAULT_SNAPSHOT_DIR / "smell_scan_results_at_cutoff.jsonl"
DEFAULT_HISTORY_SOURCE = (
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_history_canonical_all_20260918"
    / "workflow_changes.jsonl"
)
DEFAULT_RUN_SOURCE = (
    ROOT
    / "output_full_logical_20260915_complete"
    / "rq2_input_targeted_runs_60d_90d_all_unmatched_20260918"
    / "workflow_runs_normalized_dedup.jsonl"
)
DEFAULT_HISTORY_STATE_SOURCE = (
    ROOT / "output_new" / "rq3_current_history" / "current_detector_history_states.jsonl"
)
DEFAULT_DETECTOR_SOURCE = ROOT / "src" / "detectors" / "smell" / "cache_smell_detector.py"

DEFAULT_RQ2_EVENTS = ("push", "schedule", "workflow_dispatch")
SAMPLE_BATCH = "batch01"
DEFAULT_SAMPLE_SIZE = 2000
DEFAULT_SEED = 42
DEFAULT_MIN_PER_ECOSYSTEM = 50
MAX_HISTORY_COMMITS = 50


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield JSON objects from a JSONL file with line-aware errors."""

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


def require_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def normalize_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return None


def timestamp_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def json_dump(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), ensure_ascii=False) + "\n")
            count += 1
    return count


def source_label(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def source_fingerprint(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": source_label(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


class Audit:
    """Compact audit collector for records that cannot be aligned safely."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.unresolved: DefaultDict[str, Counter[str]] = defaultdict(Counter)
        self.examples: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)

    def count(self, kind: str, amount: int = 1) -> None:
        self.counts[kind] += amount

    def record_unresolved(
        self,
        source: str,
        repository_name: Any,
        identity: Any,
        example: Optional[Mapping[str, Any]] = None,
    ) -> None:
        repo = str(repository_name or "")
        identity_text = str(identity or "")
        self.unresolved[source][repo] += 1
        self.count(f"unresolved:{source}")
        key = f"{source}|{repo}|{identity_text}"
        if len(self.examples[key]) < 1:
            self.examples[key].append(dict(example or {}))

    def to_dict(self) -> Dict[str, Any]:
        unresolved: Dict[str, Any] = {}
        for source, counts in sorted(self.unresolved.items()):
            unresolved[source] = {
                "record_count": int(sum(counts.values())),
                "distinct_repository_names": len(counts),
                "repository_name_counts": dict(sorted(counts.items())),
            }
        examples: Dict[str, Any] = {}
        for key, values in sorted(self.examples.items()):
            if values:
                examples[key] = values[0]
        return {
            "counts": dict(sorted(self.counts.items())),
            "unresolved": unresolved,
            "unresolved_identity_examples": examples,
        }


def repository_id_from_record(record: Mapping[str, Any]) -> str:
    repository = record.get("repo")
    if not isinstance(repository, Mapping):
        repository = {}
    return normalize_id(repository.get("id") or record.get("repository_id"))


def build_repository_table(
    source: Path,
    audit: Audit,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Set[str]]]:
    """Build stable repository IDs and a case-insensitive alias map."""

    repositories: Dict[str, Dict[str, Any]] = {}
    alias_to_ids: DefaultDict[str, Set[str]] = defaultdict(set)
    for record in load_jsonl(source):
        nested = record.get("repo")
        if not isinstance(nested, Mapping):
            nested = {}
        repository_id = repository_id_from_record(record)
        aliases = {
            str(value).strip()
            for value in (record.get("_id"), nested.get("name"), record.get("repository_name"))
            if value not in (None, "") and str(value).strip()
        }
        if not repository_id:
            audit.count("repository_metadata_missing_id")
            audit.record_unresolved(
                "repository_metadata",
                record.get("_id"),
                record.get("_id"),
                {"_id": record.get("_id"), "aliases": sorted(aliases)},
            )
            continue
        canonical_name = str(nested.get("name") or record.get("_id") or "").strip()
        current = repositories.get(repository_id)
        if current is None:
            current = {
                "repository_id": repository_id,
                "canonical_name": canonical_name,
                "repository_aliases": sorted(aliases, key=str.lower),
                "default_branch": str(nested.get("defaultBranch") or "").strip(),
                "selected": bool(record.get("selected")),
                "processed": bool(record.get("processed")),
                "is_fork": bool(nested.get("isFork")),
                "is_archived": bool(nested.get("isArchived")),
                "stargazers": nested.get("stargazers", nested.get("watchers")),
                "commits": nested.get("commits"),
                "contributors": nested.get("contributors"),
                "total_runs_90d": record.get("total_runs_90d"),
                "workflow_count": 0,
                "eligible": False,
                "source_record_count": 1,
            }
            repositories[repository_id] = current
        else:
            current["source_record_count"] = int(current.get("source_record_count", 0)) + 1
            current["repository_aliases"] = sorted(
                set(current.get("repository_aliases") or []).union(aliases),
                key=str.lower,
            )
            for field, value in (
                ("default_branch", nested.get("defaultBranch")),
                ("canonical_name", nested.get("name")),
                ("stargazers", nested.get("stargazers", nested.get("watchers"))),
                ("commits", nested.get("commits")),
                ("contributors", nested.get("contributors")),
                ("total_runs_90d", record.get("total_runs_90d")),
            ):
                if current.get(field) in (None, "", 0) and value not in (None, ""):
                    current[field] = value
            current["selected"] = bool(current.get("selected")) or bool(record.get("selected"))
            current["processed"] = bool(current.get("processed")) or bool(record.get("processed"))
        for alias in aliases:
            alias_to_ids[normalize_name(alias)].add(repository_id)

    for alias, repository_ids in alias_to_ids.items():
        if len(repository_ids) > 1:
            audit.count("repository_alias_collision")

    for repository in repositories.values():
        repository["repository_aliases"] = sorted(
            repository.get("repository_aliases") or [], key=str.lower
        )
    return repositories, alias_to_ids


def resolve_repository_id(
    repository_name: Any,
    alias_to_ids: Mapping[str, Set[str]],
    audit: Audit,
    source: str,
    identity: Any,
    example: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    normalized = normalize_name(repository_name)
    candidates = alias_to_ids.get(normalized, set())
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        audit.count("ambiguous_repository_alias")
        audit.record_unresolved(source, repository_name, identity, example)
        return None
    audit.record_unresolved(source, repository_name, identity, example)
    return None


def candidate_paths(
    content_file: Any,
    roots: Sequence[Path],
) -> List[Path]:
    normalized = normalize_path(content_file)
    if not normalized:
        return []
    relative = Path(normalized)
    candidates: List[Path] = []
    for root in roots:
        candidates.append(root / relative)
        parts = relative.parts
        if parts and parts[0].lower() == "workflows":
            candidates.append(root / Path(*parts[1:]))
    return candidates


def build_content_index(
    sources: Sequence[Tuple[Path, Path]],
    audit: Audit,
) -> Tuple[Dict[str, List[Path]], Dict[Path, Set[str]]]:
    """Index hash-to-file candidates from existing workflow indexes."""

    index: DefaultDict[str, List[Path]] = defaultdict(list)
    trusted_path_hashes: DefaultDict[Path, Set[str]] = defaultdict(set)
    for index_source, root in sources:
        if not index_source.exists():
            continue
        for record in load_jsonl(index_source):
            content_hash = str(record.get("content_hash") or "").strip().lower()
            if not content_hash:
                continue
            paths = candidate_paths(record.get("content_file"), [root])
            for path in paths:
                if path.exists():
                    resolved = path.resolve()
                    trusted_path_hashes[resolved].add(content_hash)
                    if resolved not in index[content_hash]:
                        index[content_hash].append(resolved)
        audit.count(f"content_index_records:{source_label(index_source)}")
    return dict(index), dict(trusted_path_hashes)


def sha256_file(path: Path, cache: MutableMapping[Path, str]) -> str:
    cached = cache.get(path)
    if cached:
        return cached
    # ``content_hash`` in the collection scripts is computed from the
    # UTF-8 text returned by the API.  Workflow files were written with
    # platform newline translation on Windows, so hashing raw bytes would
    # incorrectly reject otherwise identical CRLF files.
    try:
        value = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    except UnicodeDecodeError:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        value = digest.hexdigest()
    cache[path] = value
    return value


def resolve_content_file(
    record: Mapping[str, Any],
    roots: Sequence[Path],
    content_index: Mapping[str, Sequence[Path]],
    trusted_path_hashes: Mapping[Path, Set[str]],
    hash_cache: MutableMapping[Path, str],
    audit: Audit,
    source_kind: str,
) -> Optional[Path]:
    expected = str(record.get("content_hash") or "").strip().lower()
    direct: List[Path] = []
    for field in ("content_file", "yml_file", "file"):
        direct.extend(candidate_paths(record.get(field), roots))
    candidates = direct + list(content_index.get(expected, []))
    seen: Set[Path] = set()
    for path in candidates:
        path = path.resolve()
        if path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        if expected:
            if expected not in trusted_path_hashes.get(path, set()):
                actual = sha256_file(path, hash_cache)
                if actual != expected:
                    audit.count("content_hash_mismatch")
                    continue
        return path
    audit.count(f"content_file_missing:{source_kind}")
    return None


def materialize_content(
    source: Optional[Path],
    content_hash: str,
    output_dir: Path,
) -> Optional[str]:
    if not content_hash:
        return None
    relative = Path("content") / f"{content_hash}.yml"
    target = output_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        if source is None:
            return None
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    return relative.as_posix()


def scan_record_key(record: Mapping[str, Any]) -> Tuple[str, str]:
    return normalize_name(record.get("repository_name")), normalize_path(
        record.get("workflow_path")
    )


def iter_cutoff_scan_records(
    path: Path,
    content_hash_by_identity: Optional[Mapping[Tuple[str, str], str]] = None,
    content_hash_by_file_sha: Optional[Mapping[str, str]] = None,
) -> Iterable[Dict[str, Any]]:
    """Yield both flat and wrapped cutoff-scan records in one shape."""

    for record in load_jsonl(path):
        normalized_record = dict(record)
        key = scan_record_key(normalized_record)
        # The cutoff scan file contains two locally generated formats: the
        # first 2,522 repair records are flat objects, while the older scan
        # records are one-key maps whose value contains the detector result.
        # Normalize the latter here instead of treating 128k valid states as
        # malformed records.
        if (not key[0] or not key[1]) and len(record) == 1:
            nested = next(iter(record.values()))
            if isinstance(nested, Mapping):
                normalized_record = dict(nested)
                key = scan_record_key(normalized_record)
                if normalized_record.get("content_file") in (None, ""):
                    normalized_record["content_file"] = normalized_record.get("yml_file")
        if normalized_record.get("content_hash") in (None, ""):
            inferred_hash = None
            if content_hash_by_identity is not None:
                inferred_hash = content_hash_by_identity.get(key)
            if inferred_hash is None and content_hash_by_file_sha is not None:
                inferred_hash = content_hash_by_file_sha.get(
                    str(normalized_record.get("file_sha") or "")
                )
            if inferred_hash:
                normalized_record["content_hash"] = inferred_hash
        yield normalized_record


def load_cutoff_scan(
    path: Path,
    audit: Audit,
    content_hash_by_identity: Optional[Mapping[Tuple[str, str], str]] = None,
    content_hash_by_file_sha: Optional[Mapping[str, str]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Load only the metadata needed to enrich cutoff snapshots."""

    records: Dict[Tuple[str, str], Dict[str, Any]] = {}
    keep_fields = (
        "repository_name",
        "workflow_path",
        "workflow_id",
        "file_sha",
        "content_hash",
        "content_file",
        "snapshot_commit_sha",
        "snapshot_commit_date",
    )
    for normalized_record in iter_cutoff_scan_records(
        path, content_hash_by_identity, content_hash_by_file_sha
    ):
        key = scan_record_key(normalized_record)
        if not key[0] or not key[1]:
            audit.count("cutoff_scan_missing_identity")
            continue
        normalized_record = {
            field: normalized_record.get(field)
            for field in keep_fields
            if normalized_record.get(field) not in (None, "")
        }
        previous = records.get(key)
        if previous is not None:
            audit.count("duplicate_cutoff_scan_identity")
            previous_rank = (
                bool(previous.get("content_hash")),
                bool(previous.get("snapshot_commit_sha")),
                bool(previous.get("content_file")),
            )
            new_rank = (
                bool(normalized_record.get("content_hash")),
                bool(normalized_record.get("snapshot_commit_sha")),
                bool(normalized_record.get("content_file")),
            )
            if new_rank <= previous_rank:
                continue
        records[key] = normalized_record
    return records


def detector_result(record: Mapping[str, Any]) -> Mapping[str, Any]:
    detectors = record.get("detectors")
    if isinstance(detectors, Mapping):
        value = detectors.get("cache_smell")
        if isinstance(value, Mapping):
            return value
    value = record.get("detector")
    if isinstance(value, Mapping):
        return value
    if any(key in record for key in ("has_smell", "smells", "error")):
        return record
    return {}


def compact_detector_state(
    content_hash: str,
    result: Mapping[str, Any],
    detector_version: str,
    taxonomy_version: str,
    source_kind: str,
) -> Dict[str, Any]:
    smells = [
        dict(item)
        for item in (result.get("smells") or [])
        if isinstance(item, Mapping)
    ]
    by_type = result.get("smells_by_type") or result.get("summary", {}).get("smells_by_type")
    if not isinstance(by_type, Mapping):
        by_type = Counter(str(item.get("type") or "") for item in smells if item.get("type"))
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "state_key": f"{content_hash}|{detector_version}|{taxonomy_version}",
        "content_hash": content_hash,
        "detector_version": detector_version,
        "taxonomy_version": taxonomy_version,
        "has_smell": bool(result.get("has_smell", bool(smells))),
        "smells": smells,
        "smells_by_type": {str(key): int(value or 0) for key, value in by_type.items()},
        # RQ1 contextual distributions and the RQ2 adjusted covariates need
        # the detector's job-level analysis (dependency managers, cache steps,
        # job count, and workflow mode), not only the final smell list.
        "metadata": dict(metadata),
        "error": result.get("error"),
        "state_source": source_kind,
        "detector_version_compatible_with_current": source_kind == "rq1_cutoff_scan",
    }


def load_detector_states(
    cutoff_scan: Path,
    history_states: Path,
    detector_version: str,
    taxonomy_version: str,
    audit: Audit,
    content_hash_by_identity: Optional[Mapping[Tuple[str, str], str]] = None,
    content_hash_by_file_sha: Optional[Mapping[str, str]] = None,
    history_detector_version: str = "unversioned:rq3-history-state-cache-20260908",
    history_taxonomy_version: str = "unversioned:rq3-history-taxonomy-20260908",
) -> Dict[str, Dict[str, Any]]:
    """Combine cutoff scan states and the previous RQ3 content-hash cache."""

    states: Dict[str, Dict[str, Any]] = {}
    for record in iter_cutoff_scan_records(
        cutoff_scan,
        content_hash_by_identity,
        content_hash_by_file_sha,
    ):
        content_hash = str(record.get("content_hash") or "").strip().lower()
        if not content_hash:
            audit.count("cutoff_scan_missing_content_hash")
            continue
        compact = compact_detector_state(
            content_hash,
            detector_result(record),
            detector_version,
            taxonomy_version,
            "rq1_cutoff_scan",
        )
        state_key = compact["state_key"]
        previous = states.get(state_key)
        if previous is not None:
            audit.count("duplicate_detector_content_hash")
            if previous.get("smells") != compact.get("smells"):
                audit.count("detector_state_conflict")
            if not previous.get("error") and compact.get("error"):
                continue
        states[state_key] = compact

    for record in load_jsonl(history_states):
        content_hash = str(record.get("content_hash") or "").strip().lower()
        if not content_hash:
            audit.count("history_detector_state_missing_content_hash")
            continue
        compact = compact_detector_state(
            content_hash,
            record,
            history_detector_version,
            history_taxonomy_version,
            "rq3_history_state_cache",
        )
        state_key = compact["state_key"]
        previous = states.get(state_key)
        if previous is None:
            states[state_key] = compact
        elif previous.get("smells") != compact.get("smells"):
            audit.count("duplicate_detector_content_hash")
    return states


def build_snapshots(
    snapshot_source: Path,
    scan_source: Path,
    snapshot_dir: Path,
    complete_data_dir: Path,
    legacy_data_dir: Path,
    repositories: MutableMapping[str, Dict[str, Any]],
    alias_to_ids: Mapping[str, Set[str]],
    content_index: Mapping[str, Sequence[Path]],
    trusted_path_hashes: Mapping[Path, Set[str]],
    output_dir: Path,
    cutoff: datetime,
    audit: Audit,
    hash_cache: MutableMapping[Path, str],
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
    raw_snapshot_records = list(load_jsonl(snapshot_source))
    content_hash_by_identity = {
        scan_record_key(record): str(record.get("content_hash") or "").strip().lower()
        for record in raw_snapshot_records
        if scan_record_key(record)[0] and scan_record_key(record)[1]
        and record.get("content_hash")
    }
    content_hash_by_file_sha = {
        str(record.get("file_sha")): str(record.get("content_hash") or "").strip().lower()
        for record in raw_snapshot_records
        if record.get("file_sha") and record.get("content_hash")
    }
    scan_by_identity = load_cutoff_scan(
        scan_source,
        audit,
        content_hash_by_identity,
        content_hash_by_file_sha,
    )
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    roots = [
        snapshot_dir,
        snapshot_dir / "workflow_files",
        complete_data_dir,
        legacy_data_dir,
    ]
    for raw in raw_snapshot_records:
        audit.count("cutoff_snapshot_source_record")
        repository_name = raw.get("repository_name")
        workflow_path = normalize_path(raw.get("workflow_path"))
        if not repository_name or not workflow_path:
            audit.count("cutoff_snapshot_missing_identity")
            continue
        repository_id = resolve_repository_id(
            repository_name,
            alias_to_ids,
            audit,
            "cutoff_snapshots",
            workflow_path,
            raw,
        )
        if repository_id is None:
            continue
        enriched = dict(raw)
        scan = scan_by_identity.get((normalize_name(repository_name), workflow_path))
        if scan is not None:
            for field in (
                "snapshot_commit_sha",
                "snapshot_commit_date",
                "content_hash",
                "file_sha",
                "content_file",
            ):
                if scan.get(field) not in (None, ""):
                    enriched[field] = scan[field]
        content_hash = str(enriched.get("content_hash") or "").strip().lower()
        if not content_hash:
            audit.count("cutoff_snapshot_missing_content_hash")
            continue
        key = (repository_id, workflow_path)
        source_file = resolve_content_file(
            enriched,
            roots,
            content_index,
            trusted_path_hashes,
            hash_cache,
            audit,
            "cutoff_snapshot",
        )
        content_file = materialize_content(source_file, content_hash, output_dir)
        existing = by_key.get(key)
        candidate = {
            "workflow_key": f"{repository_id}|{workflow_path}",
            "repository_id": repository_id,
            "repository_name": str(repository_name).strip(),
            "canonical_name": repositories[repository_id].get("canonical_name"),
            "workflow_path": workflow_path,
            "workflow_id": enriched.get("workflow_id"),
            "default_branch": repositories[repository_id].get("default_branch"),
            "snapshot_commit_sha": enriched.get("snapshot_commit_sha"),
            "snapshot_commit_date": enriched.get("snapshot_commit_date"),
            "file_blob_sha": enriched.get("file_sha"),
            "content_hash": content_hash,
            "content_file": content_file,
            "content_source_file": source_label(source_file),
            "snapshot_fetched_at": enriched.get("fetched_at"),
            "study_cutoff": timestamp_text(cutoff),
            "snapshot_status": (
                "verified_content_snapshot" if source_file is not None else "content_file_missing"
            ),
        }
        if existing is not None:
            audit.count("duplicate_cutoff_workflow_identity")
            if normalize_name(existing.get("repository_name")) != normalize_name(
                candidate.get("repository_name")
            ):
                audit.count("alias_workflow_identity_collapsed")
            if existing.get("content_hash") != candidate.get("content_hash"):
                audit.count("workflow_identity_content_conflict")
            old_rank = (
                normalize_name(existing.get("repository_name"))
                == normalize_name(existing.get("canonical_name")),
                bool(existing.get("content_file")),
                bool(existing.get("snapshot_commit_sha")),
                str(existing.get("snapshot_fetched_at") or ""),
            )
            new_rank = (
                normalize_name(candidate.get("repository_name"))
                == normalize_name(candidate.get("canonical_name")),
                bool(candidate.get("content_file")),
                bool(candidate.get("snapshot_commit_sha")),
                str(candidate.get("snapshot_fetched_at") or ""),
            )
            if new_rank <= old_rank:
                continue
        by_key[key] = candidate

    workflow_counts: Counter[str] = Counter(item["repository_id"] for item in by_key.values())
    for repository_id, record in repositories.items():
        record["workflow_count"] = int(workflow_counts.get(repository_id, 0))
        record["eligible"] = bool(record["workflow_count"])
    snapshots = sorted(
        by_key.values(), key=lambda item: (str(item["repository_id"]), item["workflow_path"])
    )
    return snapshots, by_key


def build_history_index(
    history_source: Path,
    alias_to_ids: Mapping[str, Set[str]],
    cutoff: datetime,
    audit: Audit,
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    histories: DefaultDict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for raw in load_jsonl(history_source):
        repository_name = raw.get("repository_name")
        workflow_path = normalize_path(raw.get("workflow_path") or raw.get("path"))
        commit_date = parse_timestamp(raw.get("commit_date"))
        content_hash = str(raw.get("content_hash") or "").strip().lower()
        commit_sha = str(raw.get("commit_sha") or "").strip()
        if not repository_name or not workflow_path or not commit_sha:
            audit.count("history_missing_identity")
            continue
        repository_id = resolve_repository_id(
            repository_name,
            alias_to_ids,
            audit,
            "workflow_history",
            f"{workflow_path}|{commit_sha}",
            raw,
        )
        if repository_id is None:
            continue
        if commit_date is None:
            audit.count("history_missing_commit_date")
            continue
        if commit_date > cutoff:
            audit.count("post_cutoff_history_record")
            continue
        if not content_hash:
            audit.count("history_missing_content_hash")
            continue
        item = dict(raw)
        item["repository_id"] = repository_id
        item["repository_name"] = str(repository_name).strip()
        item["workflow_path"] = workflow_path
        item["commit_date"] = timestamp_text(commit_date)
        item["content_hash"] = content_hash
        histories[(repository_id, workflow_path)].append(item)
    for entries in histories.values():
        entries.sort(
            key=lambda item: (
                str(item.get("commit_date") or ""),
                str(item.get("commit_sha") or ""),
            )
        )
    return dict(histories)


def choose_boundary_record(
    entries: Sequence[Mapping[str, Any]],
    content_hash: str,
) -> Optional[Mapping[str, Any]]:
    matching = [entry for entry in entries if entry.get("content_hash") == content_hash]
    if not matching:
        return None
    return max(
        matching,
        key=lambda item: (
            str(item.get("commit_date") or ""),
            1 if "content_hash_equals_current" in str(item.get("verification") or "") else 0,
            str(item.get("commit_sha") or ""),
        ),
    )


def build_boundaries(
    snapshots: Sequence[Mapping[str, Any]],
    histories: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
    cutoff: datetime,
) -> List[Dict[str, Any]]:
    boundaries: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        key = (str(snapshot["repository_id"]), str(snapshot["workflow_path"]))
        entries = histories.get(key, [])
        exact = choose_boundary_record(entries, str(snapshot["content_hash"]))
        if exact is not None:
            verification = str(exact.get("verification") or "")
            status = (
                "verified_path_change_boundary"
                if "path_change" in verification or "path-change" in verification
                else "verified_content_boundary"
            )
            last_change_at = exact.get("commit_date")
            commit_sha = exact.get("commit_sha")
            boundary_source = exact.get("source") or verification or "workflow_history"
        else:
            explicit_status = next(
                (
                    str(entry.get("history_status") or entry.get("status"))
                    for entry in entries
                    if str(entry.get("history_status") or entry.get("status") or "")
                    in {"no_commit_before_cutoff", "workflow_not_found_at_cutoff"}
                ),
                None,
            )
            status = explicit_status or "api_unresolved"
            last_change_at = None
            commit_sha = None
            boundary_source = "no_exact_current_content_hash_in_local_history"
        boundaries.append(
            {
                "workflow_key": snapshot["workflow_key"],
                "repository_id": snapshot["repository_id"],
                "workflow_path": snapshot["workflow_path"],
                "workflow_id": snapshot.get("workflow_id"),
                "content_hash": snapshot["content_hash"],
                "last_change_at": last_change_at,
                "boundary_commit_sha": commit_sha,
                "boundary_status": status,
                "boundary_source": boundary_source,
                "study_cutoff": timestamp_text(cutoff),
            }
        )
    return boundaries


def primary_ecosystem(repository: Mapping[str, Any]) -> str:
    value = str(repository.get("main_language") or "").strip().lower()
    if value in {"javascript", "typescript", "javascript/typescript", "tsx", "jsx"}:
        return "JavaScript/TypeScript"
    if value == "python":
        return "Python"
    if value in {"java", "kotlin", "scala", "groovy"}:
        return "Java/Kotlin"
    if value == "go":
        return "Go"
    if value == "rust":
        return "Rust"
    if value in {"c", "c++", "objective-c", "objective-c++", "cuda"}:
        return "C/C++"
    if value == "php":
        return "PHP"
    return "Other"


def enrich_repository_languages(
    repositories: MutableMapping[str, Dict[str, Any]],
    repository_source: Path,
) -> None:
    """Retain language metadata needed to document RQ3 stratification."""

    by_id: Dict[str, Dict[str, Any]] = {}
    for record in load_jsonl(repository_source):
        repository_id = repository_id_from_record(record)
        nested = record.get("repo") if isinstance(record.get("repo"), Mapping) else {}
        if repository_id:
            by_id[repository_id] = {
                "main_language": nested.get("mainLanguage"),
                "languages": nested.get("languages") or {},
                "created_at": nested.get("createdAt"),
            }
    for repository_id, record in repositories.items():
        extra = by_id.get(repository_id, {})
        record.update(extra)
        record["primary_ecosystem"] = primary_ecosystem(record)


def allocate_strata(
    stratum_sizes: Mapping[str, int],
    sample_size: int,
    minimum_per_stratum: int,
) -> Dict[str, int]:
    names = sorted(name for name, size in stratum_sizes.items() if size > 0)
    total = sum(stratum_sizes[name] for name in names)
    if sample_size >= total:
        return {name: int(stratum_sizes[name]) for name in names}
    if not names:
        return {}
    minimum = min(minimum_per_stratum, sample_size // len(names))
    quotas = {name: min(int(stratum_sizes[name]), minimum) for name in names}
    assigned = sum(quotas.values())
    remaining = sample_size - assigned
    if remaining <= 0:
        # If the sample is smaller than the requested minimum allocation, use
        # a deterministic proportional allocation instead.
        quotas = {name: 0 for name in names}
        assigned = 0
        remaining = sample_size
    available = {name: int(stratum_sizes[name]) - quotas[name] for name in names}
    available_total = sum(available.values())
    if available_total <= 0:
        return quotas
    exact = {
        name: remaining * available[name] / available_total for name in names
    }
    for name in names:
        add = min(available[name], int(exact[name]))
        quotas[name] += add
    leftover = sample_size - sum(quotas.values())
    order = sorted(
        names,
        key=lambda name: (-(exact[name] - int(exact[name])), name),
    )
    while leftover > 0:
        progressed = False
        for name in order:
            if quotas[name] < stratum_sizes[name]:
                quotas[name] += 1
                leftover -= 1
                progressed = True
                if leftover == 0:
                    break
        if not progressed:
            break
    return quotas


def sample_repositories(
    repository_frame: Sequence[str],
    repositories: Mapping[str, Mapping[str, Any]],
    sample_size: int,
    seed: int,
    minimum_per_stratum: int,
    batch: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    by_stratum: DefaultDict[str, List[str]] = defaultdict(list)
    for repository_id in sorted(set(repository_frame)):
        by_stratum[str(repositories[repository_id].get("primary_ecosystem") or "Other")].append(
            repository_id
        )
    quotas = allocate_strata(
        {name: len(values) for name, values in by_stratum.items()},
        min(sample_size, len(repository_frame)),
        minimum_per_stratum,
    )
    rng = random.Random(seed)
    selected: List[Dict[str, Any]] = []
    for stratum in sorted(by_stratum):
        values = list(by_stratum[stratum])
        rng.shuffle(values)
        stratum_n = len(values)
        take = quotas.get(stratum, 0)
        probability = take / stratum_n if stratum_n else 0.0
        weight = 1.0 / probability if probability else None
        for repository_id in sorted(values[:take]):
            repository = repositories[repository_id]
            selected.append(
                {
                    "sample_batch": batch,
                    "sample_seed": seed,
                    "sampling_frame": "rq1_eligible_repositories",
                    "sampling_frame_n": len(repository_frame),
                    "repository_id": repository_id,
                    "canonical_name": repository.get("canonical_name"),
                    "primary_ecosystem": stratum,
                    "stratum_n": stratum_n,
                    "stratum_sample_n": take,
                    "sample_probability": probability,
                    "sample_weight": weight,
                    "selection_basis": "stratified_random_repository_sample",
                    "history_coverage_used_for_selection": False,
                }
            )
    selected.sort(key=lambda item: str(item["repository_id"]))
    metadata = {
        "sample_batch": batch,
        "seed": seed,
        "sampling_frame": "RQ1 repositories with at least one parseable cutoff workflow",
        "sampling_frame_n": len(repository_frame),
        "requested_sample_size": sample_size,
        "realized_sample_size": len(selected),
        "minimum_per_ecosystem": minimum_per_stratum,
        "stratum_population": {name: len(values) for name, values in sorted(by_stratum.items())},
        "stratum_sample": dict(sorted(quotas.items())),
        "history_coverage_used_for_selection": False,
    }
    return selected, metadata


def build_rq1_cohort(
    snapshots: Sequence[Mapping[str, Any]],
    states: Mapping[str, Mapping[str, Any]],
    detector_version: str,
    taxonomy_version: str,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        state_key = (
            f"{snapshot['content_hash']}|{detector_version}|{taxonomy_version}"
        )
        state = states.get(state_key)
        if state is None or state.get("error"):
            continue
        result.append(
            {
                "cohort": "rq1",
                "workflow_key": snapshot["workflow_key"],
                "repository_id": snapshot["repository_id"],
                "canonical_name": snapshot.get("canonical_name"),
                "workflow_path": snapshot["workflow_path"],
                "workflow_id": snapshot.get("workflow_id"),
                "content_hash": snapshot["content_hash"],
                "content_file": snapshot.get("content_file"),
                "detector_state_key": state_key,
                "study_cutoff": snapshot["study_cutoff"],
                "eligible": True,
            }
        )
    return result


def match_run_workflow(
    record: Mapping[str, Any],
    repository_id: str,
    by_id: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
    by_path: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
) -> Tuple[Optional[Mapping[str, Any]], str]:
    workflow_id = normalize_id(record.get("workflow_id"))
    workflow_path = normalize_path(record.get("workflow_path") or record.get("path"))
    if workflow_id:
        candidates = list(by_id.get((repository_id, workflow_id), []))
        if workflow_path:
            path_matches = [
                item for item in candidates if item.get("workflow_path") == workflow_path
            ]
            if len(path_matches) == 1:
                return path_matches[0], "repository_id_workflow_id_and_path"
        if len(candidates) == 1:
            return candidates[0], "repository_id_workflow_id"
    if workflow_path:
        candidates = list(by_path.get((repository_id, workflow_path), []))
        if len(candidates) == 1:
            return candidates[0], "repository_id_workflow_path"
    if workflow_id:
        return None, "workflow_id_unresolved"
    return None, "workflow_path_unresolved"


def normalize_runs(
    source: Path,
    output: Path,
    snapshots_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
    boundaries_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
    repositories: Mapping[str, Mapping[str, Any]],
    alias_to_ids: Mapping[str, Set[str]],
    cutoff: datetime,
    events: Set[str],
    audit: Audit,
) -> Dict[Tuple[str, str], int]:
    by_id: DefaultDict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    by_path: DefaultDict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for snapshot in snapshots_by_key.values():
        repository_id = str(snapshot["repository_id"])
        workflow_id = normalize_id(snapshot.get("workflow_id"))
        if workflow_id:
            by_id[(repository_id, workflow_id)].append(snapshot)
        by_path[(repository_id, str(snapshot["workflow_path"]))].append(snapshot)

    output.parent.mkdir(parents=True, exist_ok=True)
    qualifying: Counter[Tuple[str, str]] = Counter()
    seen_run_keys: Set[Tuple[str, str, str]] = set()
    with source.open("r", encoding="utf-8") as input_handle, output.open(
        "w", encoding="utf-8", newline="\n"
    ) as output_handle:
        for line_number, line in enumerate(input_handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid run JSON at {source}:{line_number}: {exc}") from exc
            if not isinstance(raw, Mapping):
                continue
            repository_name = raw.get("repository_name")
            repository_id = resolve_repository_id(
                repository_name,
                alias_to_ids,
                audit,
                "workflow_runs",
                raw.get("run_id") or raw.get("id"),
                {"repository_name": repository_name, "run_id": raw.get("run_id")},
            )
            if repository_id is None:
                continue
            run_id = normalize_id(raw.get("run_id") or raw.get("id"))
            attempt = normalize_id(raw.get("run_attempt") or 1)
            if not run_id:
                audit.count("run_missing_run_id")
                continue
            run_key = (repository_id, run_id, attempt)
            if run_key in seen_run_keys:
                audit.count("duplicate_run_key")
                continue
            seen_run_keys.add(run_key)
            created_at = parse_timestamp(raw.get("created_at"))
            if created_at is None:
                audit.count("run_missing_created_at")
                continue
            if created_at > cutoff:
                audit.count("post_cutoff_run")
                continue
            snapshot, match_method = match_run_workflow(
                raw, repository_id, by_id, by_path
            )
            normalized = dict(raw)
            normalized["repository_id"] = repository_id
            normalized["repository_name_observed"] = repository_name
            normalized["repository_name"] = repositories[repository_id].get("canonical_name")
            normalized["run_id"] = raw.get("run_id", raw.get("id"))
            normalized["run_attempt"] = raw.get("run_attempt", 1)
            normalized["workflow_match_method"] = match_method
            normalized["workflow_key"] = snapshot.get("workflow_key") if snapshot else None
            if snapshot is None:
                audit.count("run_unmatched_current_workflow")
            else:
                workflow_key = (repository_id, str(snapshot["workflow_path"]))
                boundary = boundaries_by_key.get(workflow_key)
                if boundary and boundary.get("boundary_status") in {
                    "verified_content_boundary",
                    "verified_path_change_boundary",
                }:
                    default_branch = str(repositories[repository_id].get("default_branch") or "")
                    event = str(raw.get("event") or "")
                    branch = str(raw.get("head_branch") or "")
                    last_change = parse_timestamp(boundary.get("last_change_at"))
                    window_start = max(
                        last_change or (cutoff - timedelta(days=30)),
                        cutoff - timedelta(days=30),
                    )
                    if (
                        str(raw.get("status") or "").lower() == "completed"
                        and event in events
                        and default_branch
                        and branch == default_branch
                        and window_start <= created_at <= cutoff
                    ):
                        qualifying[workflow_key] += 1
            output_handle.write(json.dumps(normalized, ensure_ascii=False) + "\n")
    return dict(qualifying)


def build_rq2_cohort(
    snapshots: Sequence[Mapping[str, Any]],
    boundaries: Sequence[Mapping[str, Any]],
    qualifying: Mapping[Tuple[str, str], int],
    cutoff: datetime,
) -> List[Dict[str, Any]]:
    boundary_by_key = {
        (str(item["repository_id"]), str(item["workflow_path"])): item
        for item in boundaries
    }
    result: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        key = (str(snapshot["repository_id"]), str(snapshot["workflow_path"]))
        boundary = boundary_by_key[key]
        count = int(qualifying.get(key, 0))
        status = "has_qualifying_runs" if count else "run_query_incomplete"
        if not snapshot.get("workflow_id"):
            status = "workflow_id_unresolved"
        result.append(
            {
                "cohort": "rq2",
                "workflow_key": snapshot["workflow_key"],
                "repository_id": snapshot["repository_id"],
                "workflow_path": snapshot["workflow_path"],
                "workflow_id": snapshot.get("workflow_id"),
                "content_hash": snapshot["content_hash"],
                "last_change_at": boundary.get("last_change_at"),
                "boundary_status": boundary.get("boundary_status"),
                "run_status": status,
                "qualifying_run_count": count,
                "window_start": timestamp_text(
                    max(
                        parse_timestamp(boundary.get("last_change_at"))
                        or (cutoff - timedelta(days=30)),
                        cutoff - timedelta(days=30),
                    )
                ),
                "study_cutoff": timestamp_text(cutoff),
                "eligible": bool(
                    boundary.get("boundary_status")
                    in {"verified_content_boundary", "verified_path_change_boundary"}
                    and count > 0
                ),
            }
        )
    return result


def collapse_history(
    entries: Sequence[Mapping[str, Any]],
    current_snapshot: Mapping[str, Any],
    content_roots: Sequence[Path],
    content_index: Mapping[str, Sequence[Path]],
    trusted_path_hashes: Mapping[Path, Set[str]],
    output_dir: Path,
    hash_cache: MutableMapping[Path, str],
    audit: Audit,
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Keep the latest 50 file commits and fold consecutive equal hashes."""

    ordered = sorted(
        entries,
        key=lambda item: (
            str(item.get("commit_date") or ""),
            str(item.get("commit_sha") or ""),
        ),
    )[-MAX_HISTORY_COMMITS:]
    collapsed: List[Dict[str, Any]] = []
    for entry in ordered:
        content_hash = str(entry.get("content_hash") or "").strip().lower()
        if not content_hash:
            continue
        if collapsed and collapsed[-1]["content_hash"] == content_hash:
            continue
        source = resolve_content_file(
            entry,
            content_roots,
            content_index,
            trusted_path_hashes,
            hash_cache,
            audit,
            "rq3_history",
        )
        content_file = materialize_content(source, content_hash, output_dir)
        collapsed.append(
            {
                "raw": entry,
                "content_hash": content_hash,
                "content_file": content_file,
                "content_source_file": source_label(source),
            }
        )
    terminal_hash = collapsed[-1]["content_hash"] if collapsed else ""
    current_hash = str(current_snapshot["content_hash"])
    terminal_matches = bool(terminal_hash and terminal_hash == current_hash)
    all_content_present = bool(collapsed) and all(item.get("content_file") for item in collapsed)
    output_records: List[Dict[str, Any]] = []
    previous_hash: Optional[str] = None
    for index, item in enumerate(collapsed):
        raw = item["raw"]
        output_records.append(
            {
                "commit_sha": raw.get("commit_sha"),
                "commit_date": raw.get("commit_date"),
                "content_hash": item["content_hash"],
                "content_file": item.get("content_file"),
                "content_source_file": item.get("content_source_file"),
                "previous_content_hash": previous_hash,
                "is_content_change": True,
                "version_index": index,
            }
        )
        previous_hash = item["content_hash"]
    return output_records, ("complete_to_cutoff" if terminal_matches and all_content_present else (
        "terminal_hash_mismatch" if collapsed and not terminal_matches else "no_history"
    )), terminal_matches and all_content_present


def build_rq3_history(
    sample: Sequence[Mapping[str, Any]],
    snapshots_by_key: Mapping[Tuple[str, str], Mapping[str, Any]],
    histories: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
    output_dir: Path,
    content_roots: Sequence[Path],
    content_index: Mapping[str, Sequence[Path]],
    trusted_path_hashes: Mapping[Path, Set[str]],
    hash_cache: MutableMapping[Path, str],
    audit: Audit,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter[str]]:
    history_records: List[Dict[str, Any]] = []
    cohort_records: List[Dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    selected_ids = {str(item["repository_id"]): item for item in sample}
    for (repository_id, workflow_path), snapshot in sorted(
        snapshots_by_key.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        if repository_id not in selected_ids:
            continue
        sample_record = selected_ids[repository_id]
        raw_history, history_status, formal = collapse_history(
            histories.get((repository_id, workflow_path), []),
            snapshot,
            content_roots,
            content_index,
            trusted_path_hashes,
            output_dir,
            hash_cache,
            audit,
        )
        status_counts[history_status] += 1
        cohort_records.append(
            {
                "cohort": "rq3",
                "sample_batch": sample_record["sample_batch"],
                "sample_probability": sample_record["sample_probability"],
                "sample_weight": sample_record["sample_weight"],
                "workflow_key": snapshot["workflow_key"],
                "repository_id": repository_id,
                "canonical_name": snapshot.get("canonical_name"),
                "workflow_path": workflow_path,
                "workflow_id": snapshot.get("workflow_id"),
                "cutoff_content_hash": snapshot["content_hash"],
                "history_status": history_status,
                "formal_version_comparison_eligible": formal,
                "observed_version_count": len(raw_history),
                "study_cutoff": snapshot["study_cutoff"],
            }
        )
        for item in raw_history:
            history_records.append(
                {
                    "sample_batch": sample_record["sample_batch"],
                    "sample_probability": sample_record["sample_probability"],
                    "sample_weight": sample_record["sample_weight"],
                    "workflow_key": snapshot["workflow_key"],
                    "repository_id": repository_id,
                    "repository_name": snapshot.get("canonical_name"),
                    "workflow_path": workflow_path,
                    "workflow_id": snapshot.get("workflow_id"),
                    "version_index": item["version_index"],
                    "commit_sha": item["commit_sha"],
                    "commit_date": item["commit_date"],
                    "content_hash": item["content_hash"],
                    "content_file": item["content_file"],
                    "content_source_file": item["content_source_file"],
                    "previous_content_hash": item["previous_content_hash"],
                    "is_content_change": item["is_content_change"],
                    "history_status": history_status,
                }
            )
    history_records.sort(
        key=lambda item: (
            str(item["repository_id"]),
            str(item["workflow_path"]),
            int(item["version_index"]),
        )
    )
    cohort_records.sort(key=lambda item: (str(item["repository_id"]), item["workflow_path"]))
    return history_records, cohort_records, status_counts


def write_repository_table(
    output_dir: Path,
    repositories: Mapping[str, Mapping[str, Any]],
) -> int:
    ordered = sorted(
        repositories.values(), key=lambda item: str(item.get("repository_id") or "")
    )
    return write_jsonl(output_dir / "repositories.jsonl", ordered)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the canonical RQ1/RQ2/RQ3 data layer from local inputs"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repository-source", type=Path, default=DEFAULT_REPOSITORY_SOURCE)
    parser.add_argument("--complete-data-dir", type=Path, default=DEFAULT_COMPLETE_DATA)
    parser.add_argument("--legacy-data-dir", type=Path, default=DEFAULT_LEGACY_DATA)
    parser.add_argument("--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--snapshot-source", type=Path, default=DEFAULT_SNAPSHOT_SOURCE)
    parser.add_argument("--scan-source", type=Path, default=DEFAULT_SCAN_SOURCE)
    parser.add_argument("--history-source", type=Path, default=DEFAULT_HISTORY_SOURCE)
    parser.add_argument("--run-source", type=Path, default=DEFAULT_RUN_SOURCE)
    parser.add_argument("--history-state-source", type=Path, default=DEFAULT_HISTORY_STATE_SOURCE)
    parser.add_argument("--detector-source", type=Path, default=DEFAULT_DETECTOR_SOURCE)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--min-per-ecosystem", type=int, default=DEFAULT_MIN_PER_ECOSYSTEM)
    parser.add_argument("--sample-batch", default=SAMPLE_BATCH)
    parser.add_argument("--rq2-events", default=",".join(DEFAULT_RQ2_EVENTS))
    parser.add_argument("--taxonomy-version", default="cache-smell-taxonomy-2026-09-01")
    parser.add_argument(
        "--history-detector-version",
        default="unversioned:rq3-history-state-cache-20260908",
        help="Version label for the pre-existing RQ3 detector cache when its original version is not embedded.",
    )
    parser.add_argument(
        "--history-taxonomy-version",
        default="unversioned:rq3-history-taxonomy-20260908",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow writing into an existing output directory (existing files are replaced).",
    )
    args = parser.parse_args()

    cutoff = parse_timestamp(args.cutoff)
    if cutoff is None:
        raise SystemExit(f"Invalid --cutoff: {args.cutoff}")
    if args.sample_size <= 0:
        raise SystemExit("--sample-size must be positive")
    if args.min_per_ecosystem < 0:
        raise SystemExit("--min-per-ecosystem cannot be negative")

    inputs = [
        args.repository_source,
        args.snapshot_source,
        args.scan_source,
        args.history_source,
        args.run_source,
        args.history_state_source,
        args.detector_source,
    ]
    for path in inputs:
        require_file(path)
    if args.output_dir.exists() and any(args.output_dir.iterdir()) and not args.force:
        raise SystemExit(
            f"Output directory is not empty: {args.output_dir}. Use a new directory or --force."
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    audit = Audit()
    detector_digest = hashlib.sha256(args.detector_source.read_bytes()).hexdigest()
    detector_version = f"cache_smell_detector_sha256:{detector_digest}"
    hash_cache: Dict[Path, str] = {}

    repositories, alias_to_ids = build_repository_table(args.repository_source, audit)
    enrich_repository_languages(repositories, args.repository_source)

    content_index, trusted_path_hashes = build_content_index(
        [
            (args.complete_data_dir / "workflow_files.jsonl", args.complete_data_dir),
            (args.legacy_data_dir / "workflow_files.jsonl", args.legacy_data_dir),
        ],
        audit,
    )
    snapshots, snapshots_by_key = build_snapshots(
        args.snapshot_source,
        args.scan_source,
        args.snapshot_dir,
        args.complete_data_dir,
        args.legacy_data_dir,
        repositories,
        alias_to_ids,
        content_index,
        trusted_path_hashes,
        args.output_dir,
        cutoff,
        audit,
        hash_cache,
    )
    histories = build_history_index(args.history_source, alias_to_ids, cutoff, audit)
    boundaries = build_boundaries(snapshots, histories, cutoff)
    boundaries_by_key = {
        (str(item["repository_id"]), str(item["workflow_path"])): item
        for item in boundaries
    }

    states = load_detector_states(
        args.scan_source,
        args.history_state_source,
        detector_version,
        args.taxonomy_version,
        audit,
        {
            (normalize_name(item.get("repository_name")), str(item["workflow_path"])): str(
                item["content_hash"]
            )
            for item in snapshots
        },
        {
            str(item.get("file_blob_sha")): str(item["content_hash"])
            for item in snapshots
            if item.get("file_blob_sha")
        },
        args.history_detector_version,
        args.history_taxonomy_version,
    )
    rq1_cohort = build_rq1_cohort(
        snapshots, states, detector_version, args.taxonomy_version
    )
    rq1_repository_frame = sorted({str(item["repository_id"]) for item in rq1_cohort})
    sample, sample_metadata = sample_repositories(
        rq1_repository_frame,
        repositories,
        args.sample_size,
        args.seed,
        args.min_per_ecosystem,
        args.sample_batch,
    )

    run_output = args.output_dir / "workflow_runs.jsonl"
    events = {item.strip() for item in str(args.rq2_events).split(",") if item.strip()}
    qualifying = normalize_runs(
        args.run_source,
        run_output,
        snapshots_by_key,
        boundaries_by_key,
        repositories,
        alias_to_ids,
        cutoff,
        events,
        audit,
    )
    rq2_cohort = build_rq2_cohort(snapshots, boundaries, qualifying, cutoff)

    content_roots = [
        args.snapshot_dir,
        args.snapshot_dir / "workflow_files",
        args.complete_data_dir,
        args.legacy_data_dir,
        args.history_source.parent,
    ]
    rq3_history, rq3_cohort, rq3_status_counts = build_rq3_history(
        sample,
        snapshots_by_key,
        histories,
        args.output_dir,
        content_roots,
        content_index,
        trusted_path_hashes,
        hash_cache,
        audit,
    )

    repository_count = write_repository_table(args.output_dir, repositories)
    write_jsonl(args.output_dir / "workflow_snapshots.jsonl", snapshots)
    write_jsonl(args.output_dir / "workflow_boundaries.jsonl", boundaries)
    write_jsonl(args.output_dir / "detector_states.jsonl", states.values())
    write_jsonl(args.output_dir / "rq1_cohort.jsonl", rq1_cohort)
    write_jsonl(args.output_dir / "rq2_cohort.jsonl", rq2_cohort)
    write_jsonl(args.output_dir / "rq3_sample_repositories.jsonl", sample)
    write_jsonl(args.output_dir / "rq3_workflow_history.jsonl", rq3_history)
    write_jsonl(args.output_dir / "rq3_cohort.jsonl", rq3_cohort)

    boundary_counts = Counter(str(item.get("boundary_status")) for item in boundaries)
    rq2_status_counts = Counter(str(item.get("run_status")) for item in rq2_cohort)
    rq2_eligible = sum(1 for item in rq2_cohort if item.get("eligible"))
    rq3_formal = sum(1 for item in rq3_cohort if item.get("formal_version_comparison_eligible"))
    manifest = {
        "schema_version": "canonical-rq-data-v1",
        "generated_at": timestamp_text(datetime.now(timezone.utc)),
        "study_cutoff": timestamp_text(cutoff),
        "external_fetch_performed": False,
        "repository_identity": {
            "stable_key": "repository_id",
            "canonical_name_field": "canonical_name",
            "aliases_field": "repository_aliases",
            "source_record_count": sum(
                int(item.get("source_record_count", 0)) for item in repositories.values()
            ),
            "unique_repository_ids": repository_count,
            "repositories_with_cutoff_workflows": sum(
                1 for item in repositories.values() if item.get("workflow_count", 0)
            ),
        },
        "sources": {
            "repository_source": source_fingerprint(args.repository_source),
            "snapshot_source": source_fingerprint(args.snapshot_source),
            "scan_source": source_fingerprint(args.scan_source),
            "history_source": source_fingerprint(args.history_source),
            "run_source": source_fingerprint(args.run_source),
            "history_state_source": source_fingerprint(args.history_state_source),
            "detector_source": source_fingerprint(args.detector_source),
        },
        "detector": {
            "detector_version": detector_version,
            "taxonomy_version": args.taxonomy_version,
            "historical_cache_detector_version": args.history_detector_version,
            "historical_cache_taxonomy_version": args.history_taxonomy_version,
            "state_key": "(content_hash, detector_version, taxonomy_version)",
            "cutoff_scan_states_reused": True,
            "rq3_history_states_reused": True,
            "source_scans_were_unversioned": True,
            "historical_cache_version_compatible_with_current": False,
        },
        "counts": {
            "source_cutoff_snapshot_records": int(
                audit.counts.get("cutoff_snapshot_source_record", 0)
            ),
            "workflow_snapshots": len(snapshots),
            "detector_states": len(states),
            "rq1_workflow_cohort": len(rq1_cohort),
            "rq1_repository_frame": len(rq1_repository_frame),
            "workflow_boundaries": len(boundaries),
            "rq2_workflow_cohort": len(rq2_cohort),
            "rq2_eligible_workflows": rq2_eligible,
            "runs_written": sum(1 for _ in load_jsonl(run_output)),
            "rq3_sample_repositories": len(sample),
            "rq3_workflow_cohort": len(rq3_cohort),
            "rq3_workflow_history_records": len(rq3_history),
            "rq3_formal_workflows": rq3_formal,
        },
        "boundary_status_counts": dict(sorted(boundary_counts.items())),
        "rq2_run_status_counts": dict(sorted(rq2_status_counts.items())),
        "rq3_history_status_counts": dict(sorted(rq3_status_counts.items())),
        "rq3_sampling": sample_metadata,
        "rq2": {
            "window_days": 30,
            "events": sorted(events),
            "run_primary_key": "(repository_id, run_id, run_attempt)",
            "workflow_match_order": [
                "repository_id + workflow_id",
                "repository_id + workflow_path",
            ],
            "no_repository_level_fallback": True,
        },
        "rq3": {
            "sampling_unit": "repository",
            "history_limit_file_commits": MAX_HISTORY_COMMITS,
            "consecutive_same_content_hash_collapsed": True,
            "terminal_hash_must_equal_cutoff_snapshot": True,
            "selection_before_history_audit": True,
        },
        "integrity": {
            "cutoff": timestamp_text(cutoff),
            "post_cutoff_history_records_in_output": 0,
            "excluded_post_cutoff_history_records": int(
                audit.counts.get("post_cutoff_history_record", 0)
            ),
            "post_cutoff_runs_in_output": 0,
            "excluded_post_cutoff_runs": int(audit.counts.get("post_cutoff_run", 0)),
            "canonical_duplicate_workflow_identities": 0,
            "collapsed_alias_workflow_identities": int(
                audit.counts.get("duplicate_cutoff_workflow_identity", 0)
            ),
            "canonical_duplicate_run_keys": 0,
            "duplicate_run_keys_removed_from_source": int(
                audit.counts.get("duplicate_run_key", 0)
            ),
            "unresolved_repository_records": int(
                sum(value for key, value in audit.counts.items() if key.startswith("unresolved:"))
            ),
        },
    }
    json_dump(args.output_dir / "repository_alias_audit.json", audit.to_dict())
    json_dump(args.output_dir / "dataset_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
