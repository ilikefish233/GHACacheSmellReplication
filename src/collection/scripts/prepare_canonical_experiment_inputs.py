"""Create analysis views from the canonical RQ data layer.

The legacy RQ scripts predate the stable ``repository_id`` data layer and
expect a few narrow JSONL schemas.  This adapter derives those schemas only
from ``data_26_6_9_canonical_20260609``; it never reads the archived raw or
legacy experiment directories.  The derived files are disposable analysis
inputs, while the canonical directory remains the source of record.
"""

from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import load_smell_json, loads_smell_json


import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANONICAL = ROOT / "data_26_6_9_canonical_20260609"
DEFAULT_OUTPUT = ROOT / "output_canonical_rerun_20260918" / "analysis_inputs"


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            value = loads_smell_json(line)
            if isinstance(value, dict):
                yield value


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def load_manifest(canonical: Path) -> Dict[str, Any]:
    with (canonical / "dataset_manifest.json").open("r", encoding="utf-8") as handle:
        return load_smell_json(handle)


def detector_result(state: Mapping[str, Any]) -> Dict[str, Any]:
    smells = [item for item in state.get("smells", []) if isinstance(item, dict)]
    severity = Counter(str(item['severity']) for item in smells if item.get('severity') is not None)
    return {
        "detector_name": "cache_smell",
        "has_smell": bool(state.get("has_smell", bool(smells))),
        "smells": smells,
        "smell_count": len(smells),
        **({'smells_by_severity': dict(severity)} if severity else {}),
        "smells_by_type": dict(state.get("smells_by_type") or {}),
        "metadata": state.get("metadata") or {},
        "error": state.get("error"),
    }


def inferred_cache_detector(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Reconstruct the small cache-detector view needed by RQ1 summaries.

    The canonical detector state is intentionally centered on the operational
    cache-smell detector.  Cache presence/categories are therefore derived
    from its audited job-level cache steps instead of importing an archived
    detector output.
    """

    metadata = state.get("metadata") or {}
    jobs = metadata.get("jobs") or {}
    categories = set()
    cache_count = 0
    for job in jobs.values() if isinstance(jobs, dict) else []:
        analysis = job.get("analysis") or {} if isinstance(job, dict) else {}
        for step in analysis.get("cache_steps") or []:
            if not isinstance(step, dict):
                continue
            cache_count += 1
            action = str(step.get("action") or "").lower()
            if step.get("setup_cache"):
                categories.add("setup_action_cache")
            elif action.startswith("actions/cache"):
                categories.add("manual_actions_cache")
            elif action:
                categories.add("other_cache_action")
            else:
                categories.add("manual_cache")
    return {
        "use_cache": cache_count > 0,
        "cache_step_count": cache_count,
        "cache_categories": sorted(categories),
    }


def make_scan_record(
    canonical: Path,
    snapshot: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    content_file = str(snapshot.get("content_file") or "")
    content_path = canonical / content_file if content_file else None
    cache_smell = detector_result(state)
    return {
        "workflow_key": snapshot["workflow_key"],
        "repository_id": snapshot.get("repository_id"),
        "repository_name": snapshot.get("canonical_name") or snapshot.get("repository_name"),
        "workflow_path": snapshot.get("workflow_path"),
        "workflow_id": snapshot.get("workflow_id"),
        "file_sha": snapshot.get("file_blob_sha") or snapshot.get("file_sha"),
        "content_hash": snapshot.get("content_hash"),
        "yml_file": str(content_path) if content_path else "",
        "detectors": {
            "cache_smell": cache_smell,
            "cache": inferred_cache_detector(state),
        },
        "summary": {"smells_by_type": dict(state.get("smells_by_type") or {})},
    }


def build_repo_info(canonical: Path, repositories: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    rows = []
    for item in repositories.values():
        name = str(item.get("canonical_name") or "").strip()
        if not name:
            continue
        repo = {
            "name": name,
            "defaultBranch": item.get("default_branch") or "",
            "mainLanguage": item.get("main_language") or "",
            "createdAt": item.get("created_at"),
            "stargazers": item.get("stargazers") or 0,
            "forks": item.get("forks") or 0,
            "commits": item.get("commits") or 0,
            "contributors": item.get("contributors") or 0,
            "totalIssues": item.get("total_issues") or 0,
            "totalPullRequests": item.get("total_pull_requests") or 0,
            "releases": item.get("releases") or 0,
            "codeLines": item.get("code_lines") or 0,
            "branches": item.get("branches") or 0,
        }
        rows.append(
            {
                "_id": name,
                "repo": repo,
                "total_runs_90d": item.get("total_runs_90d") or 0,
                "nb_workflows": item.get("workflow_count") or 0,
                "repository_id": item.get("repository_id"),
            }
        )
    return sorted(rows, key=lambda row: str(row.get("repository_id") or ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build canonical-only RQ analysis input views")
    parser.add_argument("--canonical-dir", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    canonical = args.canonical_dir.resolve()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"Output directory is non-empty: {output}; use --force")
    output.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(canonical)
    detector_version = manifest["detector"]["detector_version"]
    taxonomy_version = manifest["detector"]["taxonomy_version"]
    cutoff = manifest["study_cutoff"]

    repositories = {
        str(row["repository_id"]): row
        for row in load_jsonl(canonical / "repositories.jsonl")
        if row.get("repository_id")
    }
    snapshots = {
        str(row["workflow_key"]): row
        for row in load_jsonl(canonical / "workflow_snapshots.jsonl")
        if row.get("workflow_key")
    }
    states = {
        str(row["state_key"]): row
        for row in load_jsonl(canonical / "detector_states.jsonl")
        if row.get("state_key")
        and row.get("detector_version") == detector_version
        and row.get("taxonomy_version") == taxonomy_version
    }

    repo_info_rows = build_repo_info(canonical, repositories)
    view = output / "canonical_views"
    write_jsonl(view / "repo_info.jsonl", repo_info_rows)

    rq1_rows = []
    for cohort in load_jsonl(canonical / "rq1_cohort.jsonl"):
        snapshot = snapshots.get(str(cohort.get("workflow_key") or ""))
        state = states.get(str(cohort.get("detector_state_key") or ""))
        if snapshot is None or state is None:
            continue
        rq1_rows.append(make_scan_record(canonical, snapshot, state))
    rq1_rows.sort(key=lambda row: str(row["workflow_key"]))
    write_jsonl(view / "rq1_scan_direct.jsonl", rq1_rows)
    write_jsonl(
        view / "smell_scan_results.jsonl",
        ({row["workflow_key"]: row} for row in rq1_rows),
    )

    rq2_rows = []
    for snapshot in snapshots.values():
        state_key = (
            f"{snapshot['content_hash']}|{detector_version}|{taxonomy_version}"
        )
        state = states.get(state_key)
        if state is None:
            continue
        rq2_rows.append(make_scan_record(canonical, snapshot, state))
    rq2_rows.sort(key=lambda row: str(row["workflow_key"]))
    write_jsonl(view / "rq2_scan_direct.jsonl", rq2_rows)

    workflow_files = []
    for snapshot in snapshots.values():
        workflow_files.append(
            {
                "repository_name": snapshot.get("canonical_name"),
                "workflow_path": snapshot.get("workflow_path"),
                "workflow_id": snapshot.get("workflow_id"),
                "content_hash": snapshot.get("content_hash"),
                "file_sha": snapshot.get("file_blob_sha") or snapshot.get("file_sha"),
                "content_file": snapshot.get("content_file"),
                "study_as_of": cutoff,
            }
        )
    workflow_files.sort(key=lambda row: (str(row.get("repository_name")), str(row.get("workflow_path"))))
    write_jsonl(view / "workflow_files.jsonl", workflow_files)

    boundaries = {
        str(row["workflow_key"]): row
        for row in load_jsonl(canonical / "workflow_boundaries.jsonl")
        if row.get("workflow_key")
    }
    workflow_changes = []
    for key, snapshot in snapshots.items():
        boundary = boundaries.get(key) or {}
        if not boundary.get("last_change_at") or not boundary.get("boundary_commit_sha"):
            continue
        workflow_changes.append(
            {
                "repository_name": snapshot.get("canonical_name"),
                "workflow_path": snapshot.get("workflow_path"),
                "workflow_id": snapshot.get("workflow_id"),
                "commit_sha": boundary.get("boundary_commit_sha"),
                "commit_date": boundary.get("last_change_at"),
                "content_hash": snapshot.get("content_hash"),
                "content_file": snapshot.get("content_file"),
                "boundary_status": boundary.get("boundary_status"),
            }
        )
    workflow_changes.sort(key=lambda row: (str(row.get("repository_name")), str(row.get("workflow_path"))))
    write_jsonl(view / "workflow_changes.jsonl", workflow_changes)

    metadata = {
        "canonical_dir": str(canonical),
        "study_cutoff": cutoff,
        "detector_version": detector_version,
        "taxonomy_version": taxonomy_version,
        "source_policy": "all derived inputs are generated from canonical JSONL/content only",
        "counts": {
            "repositories": len(repo_info_rows),
            "rq1_scan_rows": len(rq1_rows),
            "rq2_scan_rows": len(rq2_rows),
            "workflow_files": len(workflow_files),
            "workflow_changes": len(workflow_changes),
        },
        "paths": {
            "repo_info": str(view / "repo_info.jsonl"),
            "rq1_scan_direct": str(view / "rq1_scan_direct.jsonl"),
            "rq1_scan_wrapped": str(view / "smell_scan_results.jsonl"),
            "rq2_scan_direct": str(view / "rq2_scan_direct.jsonl"),
            "workflow_files": str(view / "workflow_files.jsonl"),
            "workflow_changes": str(view / "workflow_changes.jsonl"),
            "workflow_runs": str(canonical / "workflow_runs.jsonl"),
        },
    }
    write_json(output / "analysis_input_manifest.json", metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
