"""Build a provenance-preserving complete study dataset.

The original collection and the API supplement are intentionally kept as
separate inputs in the repository.  This script creates a new, self-contained
study directory containing their union, without modifying either source.

The supplement contains current workflow snapshots at the fixed study cutoff,
whereas the base directory contains the original run and workflow-history
files.  The merged directory is therefore the staging area for the next
history-completion step; it is not considered RQ2-ready until
``workflow_changes.jsonl`` has also been completed for the supplemented
repositories.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Tuple


DEFAULT_BASE_DIR = Path("data_26_6_9")
DEFAULT_SUPPLEMENT_DIR = (
    DEFAULT_BASE_DIR / "supplement_missing_workflows_20260609"
)
DEFAULT_OUTPUT_DIR = Path("data_26_6_9_complete_20260609")
STUDY_CUTOFF = "2026-06-09T23:59:59Z"


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


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def normalize_repo(value: Any) -> str:
    return str(value or "").strip().lower()


def copy_tree_without_index(source: Path, target: Path) -> int:
    """Copy workflow content while excluding each collection's local hash index."""

    copied = 0
    if not source.exists():
        return copied
    for source_path in source.rglob("*"):
        if not source_path.is_file() or source_path.name == "_hash_index.json":
            continue
        relative = source_path.relative_to(source)
        target_path = target / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            # The two collections are expected to be disjoint.  Refuse a
            # silent collision unless the bytes are exactly identical.
            if source_path.read_bytes() != target_path.read_bytes():
                raise RuntimeError(f"Conflicting workflow content path: {relative}")
            continue
        shutil.copy2(source_path, target_path)
        copied += 1
    return copied


def load_base_repositories(path: Path) -> OrderedDict[str, Dict[str, Any]]:
    records: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    for entry in load_jsonl(path):
        repository = normalize_repo(
            entry.get("_id") or (entry.get("repo") or {}).get("name")
        )
        if repository and repository not in records:
            item = dict(entry)
            item["source_dataset"] = "base"
            item["study_as_of"] = STUDY_CUTOFF
            records[repository] = item
    return records


def load_supplement_workflows(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    records: List[Dict[str, Any]] = []
    default_branches: Dict[str, str] = {}
    for entry in load_jsonl(path):
        item = dict(entry)
        repository = normalize_repo(item.get("repository_name"))
        if not repository:
            continue
        item["source_dataset"] = "supplement_20260609"
        item["study_as_of"] = STUDY_CUTOFF
        branch = str(item.get("default_branch") or "").strip()
        if branch:
            default_branches.setdefault(repository, branch)
        records.append(item)
    return records, default_branches


def supplement_repo_record(
    repository: str,
    candidate: Mapping[str, Any] | None,
    default_branch: str,
) -> Dict[str, Any]:
    candidate = candidate or {}
    selection = candidate.get("selection") or {}
    repo_name = str(candidate.get("repository_name") or repository)
    repo = {
        "name": repo_name,
        "defaultBranch": str(candidate.get("default_branch") or default_branch or "main"),
        "isFork": bool(selection.get("isFork", False)),
        "isArchived": bool(selection.get("isArchived", False)),
        "stargazers": candidate.get("stargazers"),
        "commits": candidate.get("commits"),
        "branches": None,
        "contributors": None,
        "totalRuns90d": candidate.get("total_runs_90d"),
        "nbWorkflows": candidate.get("nb_workflows"),
    }
    return {
        "_id": repo_name,
        "total_runs_90d": candidate.get("total_runs_90d"),
        "nb_runs": candidate.get("total_runs_90d"),
        "nb_workflows": candidate.get("nb_workflows"),
        "repo": repo,
        "source_dataset": "supplement_20260609",
        "study_as_of": STUDY_CUTOFF,
    }


def load_supplement_candidates(path: Path) -> Dict[str, Dict[str, Any]]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for entry in load_jsonl(path):
        repository = normalize_repo(entry.get("repository_name"))
        if repository:
            candidates[repository] = entry
    return candidates


def build_complete_dataset(
    base_dir: Path,
    supplement_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(
            f"Output directory is non-empty; refusing to overwrite: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    base_workflow_path = base_dir / "workflow_files.jsonl"
    supplement_workflow_path = supplement_dir / "workflow_files_deduplicated.jsonl"
    base_repo_path = base_dir / "repo_info.jsonl"
    supplement_candidate_path = supplement_dir / "candidate_repositories.jsonl"

    base_workflows = [dict(entry) for entry in load_jsonl(base_workflow_path)]
    supplement_workflows, supplement_branches = load_supplement_workflows(
        supplement_workflow_path
    )
    workflow_keys: set[Tuple[str, str]] = set()
    base_workflow_repositories: set[str] = set()
    for entry in base_workflows:
        base_repository = normalize_repo(entry.get("repository_name"))
        if base_repository:
            base_workflow_repositories.add(base_repository)
        workflow_keys.add(
            (base_repository, str(entry.get("workflow_path") or ""))
        )
    overlap_keys = 0
    for entry in supplement_workflows:
        key = (
            normalize_repo(entry.get("repository_name")),
            str(entry.get("workflow_path") or ""),
        )
        if key in workflow_keys:
            overlap_keys += 1
        workflow_keys.add(key)

    merged_workflows = [
        {
            **entry,
            "source_dataset": entry.get("source_dataset", "base"),
            "study_as_of": entry.get("study_as_of", STUDY_CUTOFF),
        }
        for entry in base_workflows
    ] + supplement_workflows
    merged_workflow_path = output_dir / "workflow_files.jsonl"
    merged_workflow_count = write_jsonl(merged_workflow_path, merged_workflows)

    repositories = load_base_repositories(base_repo_path)
    candidates = load_supplement_candidates(supplement_candidate_path)
    supplement_repositories = {
        normalize_repo(entry.get("repository_name"))
        for entry in supplement_workflows
        if normalize_repo(entry.get("repository_name"))
    }
    repository_overlap_with_base_workflow_index = 0
    for repository in sorted(supplement_repositories):
        if repository in base_workflow_repositories:
            repository_overlap_with_base_workflow_index += 1
        if repository in repositories:
            continue
        repositories[repository] = supplement_repo_record(
            repository,
            candidates.get(repository),
            supplement_branches.get(repository, "main"),
        )
    merged_repo_path = output_dir / "repo_info.jsonl"
    merged_repo_count = write_jsonl(merged_repo_path, repositories.values())

    for name in ("workflow_runs.jsonl", "workflow_changes.jsonl"):
        source = base_dir / name
        target = output_dir / name
        if source.exists():
            shutil.copy2(source, target)
        else:
            target.touch()

    copied_base_files = copy_tree_without_index(
        base_dir / "workflows", output_dir / "workflows"
    )
    copied_supplement_files = copy_tree_without_index(
        supplement_dir / "workflows", output_dir / "workflows"
    )

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "study_as_of": STUDY_CUTOFF,
        "base_dir": str(base_dir),
        "supplement_dir": str(supplement_dir),
        "output_dir": str(output_dir),
        "base_workflow_records": len(base_workflows),
        "supplement_workflow_records": len(supplement_workflows),
        "merged_workflow_records": merged_workflow_count,
        "workflow_key_overlap": overlap_keys,
        "base_repository_records": len(load_base_repositories(base_repo_path)),
        "base_repositories_with_workflows": len(base_workflow_repositories),
        "supplement_repositories_with_workflows": len(supplement_repositories),
        "repository_overlap_with_base_workflow_index": (
            repository_overlap_with_base_workflow_index
        ),
        "merged_repositories_with_workflows": (
            len(base_workflow_repositories | supplement_repositories)
        ),
        "merged_repository_records": merged_repo_count,
        "copied_base_workflow_files": copied_base_files,
        "copied_supplement_workflow_files": copied_supplement_files,
        "run_records_source": str(base_dir / "workflow_runs.jsonl"),
        "workflow_history_source": str(base_dir / "workflow_changes.jsonl"),
        "workflow_history_status": (
            "base history copied; supplemented repositories require history completion"
        ),
        "provenance_fields": ["source_dataset", "study_as_of"],
        "rq2_readiness": {
            "rq1": "ready for full rescan after merged index validation",
            "rq2": "not ready until supplemented workflow histories are fetched",
            "rq3": "not changed by current-snapshot merge alone",
        },
    }
    with (output_dir / "dataset_manifest.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the complete merged study dataset")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--supplement-dir", type=Path, default=DEFAULT_SUPPLEMENT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_complete_dataset(args.base_dir, args.supplement_dir, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
