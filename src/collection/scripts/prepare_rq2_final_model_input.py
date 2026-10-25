"""Prepare the Phase 3.1 final RQ2 queue in the run-level model format.

This is deliberately a conversion-only step.  It does not reselect workflows
or runs, rerun a detector, contact GitHub, or modify the canonical directory.
The Phase 3.1 final workflow and run queues are treated as authoritative.
"""

from __future__ import annotations

import sys as _smell_sys
from pathlib import Path as _SmellPath
_SMELL_PROJECT_ROOT = str(_SmellPath(__file__).resolve().parents[1])
if _SMELL_PROJECT_ROOT not in _smell_sys.path:
    _smell_sys.path.insert(0, _SMELL_PROJECT_ROOT)
from src.smell_names import loads_smell_json


import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOWS = (
    ROOT
    / "output_canonical_rq2_phase3_1_final_queue_20260920"
    / "rq2_final_analysis_workflows.jsonl"
)
DEFAULT_RUNS = (
    ROOT
    / "output_canonical_rq2_phase3_1_final_queue_20260920"
    / "rq2_final_analysis_runs.jsonl"
)
DEFAULT_RQ1 = ROOT / "data_26_6_9_canonical_20260609" / "rq1_cohort.jsonl"
DEFAULT_DETECTOR_STATES = (
    ROOT / "data_26_6_9_canonical_20260609" / "detector_states.jsonl"
)
DEFAULT_OUTPUT = ROOT / "output_canonical_rq2_phase4_final_model_input_20260920"

CUTOFF = "2026-06-09T23:59:59Z"
WINDOW_START = "2026-05-10T23:59:59Z"
EXPECTED_WORKFLOWS = 51_179
EXPECTED_RUNS = 1_093_639

WorkflowKey = Tuple[str, str]
RunKey = Tuple[str, str, str]


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = loads_smell_json(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            yield value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def normalize_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def workflow_key(row: Mapping[str, Any]) -> WorkflowKey:
    repository_id = str(row.get("repository_id") or "").strip()
    path = normalize_path(row.get("workflow_path"))
    if not repository_id or not path:
        raise ValueError("Missing repository_id or workflow_path")
    return repository_id, path


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def version_id(repository_id: str, workflow_path: str, content_hash: str) -> str:
    return f"{repository_id}|{workflow_path}|content:{content_hash}"


def run_key(row: Mapping[str, Any]) -> RunKey:
    repository_id = str(row.get("repository_id") or "").strip()
    run_id = str(row.get("run_id") or "").strip()
    run_attempt = str(row.get("run_attempt") or "1").strip()
    if not repository_id or not run_id:
        raise ValueError("Run is missing repository_id or run_id")
    return repository_id, run_id, run_attempt


def numeric_id(value: str, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric {field}, found {value!r}") from exc


def load_final_workflows(path: Path) -> Dict[WorkflowKey, Dict[str, Any]]:
    workflows: Dict[WorkflowKey, Dict[str, Any]] = {}
    for row in load_jsonl(path):
        key = workflow_key(row)
        if key in workflows:
            raise ValueError(f"Duplicate final workflow key: {key}")
        if not (
            row.get("detector_valid")
            and row.get("boundary_valid")
            and row.get("workflow_id")
            and row.get("query_coverage_status") == "complete"
            and row.get("coverage_confirmed")
            and int(row.get("qualifying_run_count") or 0) > 0
            and row.get("final_analysis_included")
        ):
            raise ValueError(f"Final workflow failed Phase 3.1 gates: {key}")
        workflows[key] = dict(row)
    if len(workflows) != EXPECTED_WORKFLOWS:
        raise ValueError(
            f"Expected {EXPECTED_WORKFLOWS} final workflows, found {len(workflows)}"
        )
    return workflows


def load_rq1_content(
    path: Path,
    needed_keys: set[WorkflowKey],
) -> Dict[WorkflowKey, Dict[str, str]]:
    result: Dict[WorkflowKey, Dict[str, str]] = {}
    for row in load_jsonl(path):
        try:
            key = workflow_key(row)
        except ValueError:
            continue
        if key not in needed_keys:
            continue
        content_hash = str(row.get("content_hash") or "").strip().lower()
        state_key = str(row.get("detector_state_key") or "").strip()
        if not content_hash or not state_key:
            raise ValueError(f"RQ1 record missing content_hash/detector_state_key: {key}")
        candidate = {
            "content_hash": content_hash,
            "detector_state_key": state_key,
            "repository_name": str(
                row.get("canonical_name") or row.get("repository_name") or ""
            ).strip(),
        }
        previous = result.get(key)
        if previous is not None and previous != candidate:
            raise ValueError(f"Conflicting RQ1 content records: {key}")
        result[key] = candidate
    missing = sorted(needed_keys - set(result))
    if missing:
        raise ValueError(f"Missing RQ1 content records: {missing[:5]}")
    return result


def detector_record(state: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "has_smell": bool(state.get("has_smell")),
        "smells": state.get("smells") or [],
        "smells_by_type": state.get("smells_by_type") or {},
        "metadata": state.get("metadata") or {},
        "error": state.get("error"),
    }


def write_jsonl_row(handle: Any, row: Mapping[str, Any]) -> None:
    handle.write(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def prepare(
    workflows_path: Path,
    runs_path: Path,
    rq1_path: Path,
    detector_states_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    for path in (workflows_path, runs_path, rq1_path, detector_states_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Output directory is non-empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    input_paths = {
        "final_workflows": workflows_path,
        "final_runs": runs_path,
        "rq1_cohort": rq1_path,
        "detector_states": detector_states_path,
    }
    input_hashes = {
        name: sha256_file(path) for name, path in input_paths.items()
    }

    workflows = load_final_workflows(workflows_path)
    rq1 = load_rq1_content(rq1_path, set(workflows))

    versions: Dict[WorkflowKey, Dict[str, Any]] = {}
    state_to_versions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    detector_versions: set[str] = set()
    taxonomy_versions: set[str] = set()
    for key in sorted(workflows):
        workflow = workflows[key]
        rq1_row = rq1[key]
        workflow_state_key = str(workflow.get("detector_state_key") or "").strip()
        if workflow_state_key != rq1_row["detector_state_key"]:
            raise ValueError(f"Workflow/RQ1 detector state mismatch: {key}")
        content_hash = rq1_row["content_hash"]
        value = {
            "workflow_key": f"{key[0]}|{key[1]}",
            "repository_id": key[0],
            "repository_name": str(
                workflow.get("repository_name") or rq1_row["repository_name"]
            ).strip(),
            "workflow_path": key[1],
            "workflow_id": str(workflow.get("workflow_id") or "").strip(),
            "content_hash": content_hash,
            "detector_state_key": workflow_state_key,
            "boundary_last_change_at": str(
                workflow.get("boundary_last_change_at") or ""
            ).strip(),
            "boundary_commit_sha": str(
                workflow.get("boundary_commit_sha") or ""
            ).strip(),
            "version_id": version_id(key[0], key[1], content_hash),
            "detector_version": str(workflow.get("detector_version") or "").strip(),
            "taxonomy_version": str(workflow.get("taxonomy_version") or "").strip(),
        }
        if not value["boundary_last_change_at"] or not value["boundary_commit_sha"]:
            raise ValueError(f"Missing verified boundary fields: {key}")
        if not value["detector_version"] or not value["taxonomy_version"]:
            raise ValueError(f"Missing detector/taxonomy version: {key}")
        parse_timestamp(value["boundary_last_change_at"])
        if value["version_id"] in {v["version_id"] for v in versions.values()}:
            raise ValueError(f"Duplicate version_id: {value['version_id']}")
        versions[key] = value
        state_to_versions[workflow_state_key].append(value)
        detector_versions.add(value["detector_version"])
        taxonomy_versions.add(value["taxonomy_version"])

    if len(detector_versions) != 1 or len(taxonomy_versions) != 1:
        raise ValueError(
            f"Detector/taxonomy versions are not stable: "
            f"{sorted(detector_versions)} / {sorted(taxonomy_versions)}"
        )

    run_output = output_dir / "run_records.jsonl"
    version_counts: Counter[str] = Counter()
    version_success_counts: Counter[str] = Counter()
    version_technical_counts: Counter[str] = Counter()
    seen_run_keys: set[RunKey] = set()
    run_order_keys: Dict[str, List[Tuple[float, int, int, str]]] = defaultdict(list)
    run_count = 0
    repositories: set[str] = set()
    events: Counter[str] = Counter()

    # First pass validates the immutable run set and creates chronological
    # within-version ordering without retaining the large run payloads.
    for row in load_jsonl(runs_path):
        run_count += 1
        if row.get("final_analysis_included") is not True:
            raise ValueError(f"Run is not marked final_analysis_included: {run_count}")
        key = workflow_key(row)
        workflow = workflows.get(key)
        if workflow is None:
            raise ValueError(f"Run references unknown final workflow: {key}")
        run_identity = run_key(row)
        if run_identity in seen_run_keys:
            raise ValueError(f"Duplicate final run key: {run_identity}")
        seen_run_keys.add(run_identity)
        workflow_id = str(row.get("workflow_id") or "").strip()
        if workflow_id != str(workflow.get("workflow_id") or "").strip():
            raise ValueError(f"Run/workflow ID mismatch: {run_identity} / {key}")
        created_at = parse_timestamp(row.get("created_at"))
        run_id = numeric_id(run_identity[1], "run_id")
        attempt = numeric_id(run_identity[2], "run_attempt")
        version = versions[key]
        version_id_value = version["version_id"]
        run_order_keys[version_id_value].append(
            (created_at.timestamp(), run_id, attempt, run_identity[0])
        )
        version_counts[version_id_value] += 1
        conclusion = str(row.get("conclusion") or "").strip().lower()
        if conclusion == "success":
            version_success_counts[version_id_value] += 1
        if conclusion in {"success", "failure", "startup_failure", "timed_out"}:
            version_technical_counts[version_id_value] += 1
        repositories.add(key[0])
        events[str(row.get("event") or "").strip().lower()] += 1

    if run_count != EXPECTED_RUNS:
        raise ValueError(f"Expected {EXPECTED_RUNS} final runs, found {run_count}")
    if set(run_order_keys) != {value["version_id"] for value in versions.values()}:
        missing = sorted(
            {value["version_id"] for value in versions.values()} - set(run_order_keys)
        )
        raise ValueError(f"Final versions without runs: {missing[:5]}")

    order_by_run_key: Dict[RunKey, int] = {}
    for version_id_value, items in run_order_keys.items():
        items.sort()
        for index, (_, _, _, repository_id) in enumerate(items, start=1):
            # run_id and attempt are numeric in the sort tuple; reconstruct the
            # string form from the source-independent integer representation.
            run_id = items[index - 1][1]
            attempt = items[index - 1][2]
            order_by_run_key[(repository_id, str(run_id), str(attempt))] = index
    del run_order_keys

    # Second pass writes the source run rows with only the model fields added.
    written_runs = 0
    with run_output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in load_jsonl(runs_path):
            key = workflow_key(row)
            version = versions[key]
            identity = run_key(row)
            output = dict(row)
            output.update(
                {
                    "version_id": version["version_id"],
                    "logical_version_id": version["version_id"],
                    "commit_sha": version["boundary_commit_sha"],
                    "commit_date": version["boundary_last_change_at"],
                    "last_change_at": version["boundary_last_change_at"],
                    "effective_end_at": CUTOFF,
                    "content_hash": version["content_hash"],
                    "commit_date_is_run_time_proxy": False,
                    "run_order_in_version": order_by_run_key[identity],
                    "is_first_run_in_version": order_by_run_key[identity] == 1,
                    "workflow_key": version["workflow_key"],
                }
            )
            write_jsonl_row(handle, output)
            written_runs += 1
    if written_runs != run_count:
        raise ValueError(f"Run output count changed: {written_runs} != {run_count}")

    version_output = output_dir / "version_records.jsonl"
    scan_output = output_dir / "version_scan_results.jsonl"
    with version_output.open("w", encoding="utf-8", newline="\n") as handle:
        for key in sorted(versions):
            value = versions[key]
            write_jsonl_row(
                handle,
                {
                    "version_id": value["version_id"],
                    "logical_version_id": value["version_id"],
                    "workflow_key": value["workflow_key"],
                    "repository_id": value["repository_id"],
                    "repository_name": value["repository_name"],
                    "workflow_path": value["workflow_path"],
                    "workflow_id": value["workflow_id"],
                    "content_hash": value["content_hash"],
                    "detector_state_key": value["detector_state_key"],
                    "commit_sha": value["boundary_commit_sha"],
                    "commit_date": value["boundary_last_change_at"],
                    "last_change_at": value["boundary_last_change_at"],
                    "effective_end_at": CUTOFF,
                    "next_commit_date": CUTOFF,
                    "commit_date_is_run_time_proxy": False,
                    "sequence": 0,
                    "completed_count": version_counts[value["version_id"]],
                    "success_count": version_success_counts[value["version_id"]],
                    "technical_total_count": version_technical_counts[value["version_id"]],
                    "history_boundary_inferred": False,
                },
            )

    state_to_versions_sorted = {
        state_key: sorted(values, key=lambda value: value["version_id"])
        for state_key, values in state_to_versions.items()
    }
    seen_state_keys: set[str] = set()
    matched_versions: set[str] = set()
    detector_state_records = 0
    with scan_output.open("w", encoding="utf-8", newline="\n") as handle:
        for state in load_jsonl(detector_states_path):
            state_key = str(state.get("state_key") or "").strip()
            if not state_key or state_key not in state_to_versions_sorted:
                continue
            if state_key in seen_state_keys:
                raise ValueError(f"Duplicate detector state key: {state_key}")
            seen_state_keys.add(state_key)
            detector_state_records += 1
            content_hash = str(state.get("content_hash") or "").strip().lower()
            detector_version = str(state.get("detector_version") or "").strip()
            taxonomy_version = str(state.get("taxonomy_version") or "").strip()
            for value in state_to_versions_sorted[state_key]:
                if content_hash != value["content_hash"]:
                    raise ValueError(f"Detector/content hash mismatch: {value['workflow_key']}")
                if detector_version != value["detector_version"]:
                    raise ValueError(f"Detector version mismatch: {value['workflow_key']}")
                if taxonomy_version != value["taxonomy_version"]:
                    raise ValueError(f"Taxonomy version mismatch: {value['workflow_key']}")
                write_jsonl_row(
                    handle,
                    {
                        "version_id": value["version_id"],
                        "logical_version_id": value["version_id"],
                        "workflow_key": value["workflow_key"],
                        "repository_id": value["repository_id"],
                        "repository_name": value["repository_name"],
                        "workflow_path": value["workflow_path"],
                        "workflow_id": value["workflow_id"],
                        "content_hash": value["content_hash"],
                        "detector_state_key": state_key,
                        "commit_sha": value["boundary_commit_sha"],
                        "commit_date": value["boundary_last_change_at"],
                        "effective_end_at": CUTOFF,
                        "next_commit_date": CUTOFF,
                        "file_sha": content_hash,
                        "detector_version": detector_version,
                        "taxonomy_version": taxonomy_version,
                        "detector": detector_record(state),
                    },
                )
                matched_versions.add(value["version_id"])

    if detector_state_records != len(state_to_versions_sorted):
        missing = sorted(set(state_to_versions_sorted) - seen_state_keys)
        raise ValueError(f"Missing detector states: {missing[:5]}")
    if matched_versions != {value["version_id"] for value in versions.values()}:
        raise ValueError("Not every final version received a detector state")

    metadata: Dict[str, Any] = {
        "schema_version": "rq2-final-model-input-v1",
        "study_cutoff": CUTOFF,
        "window_start": WINDOW_START,
        "window_days": 30,
        "external_fetch_performed": False,
        "canonical_inputs_modified": False,
        "selection_policy": (
            "No reselection: workflows and runs are copied from the Phase 3.1 "
            "final analysis queue after conversion-only validation."
        ),
        "workflow_identity": "(repository_id, normalized_workflow_path)",
        "version_id_rule": "<repository_id>|<workflow_path>|content:<content_hash>",
        "run_primary_key": "(repository_id, run_id, run_attempt)",
        "run_order_rule": "created_at ascending, then numeric run_id and run_attempt",
        "commit_date_is_run_time_proxy": False,
        "effective_end_at": CUTOFF,
        "detector_state_join": "workflow.detector_state_key -> detector_states.state_key",
        "detector_versions": sorted(detector_versions),
        "taxonomy_versions": sorted(taxonomy_versions),
        "events": dict(sorted(events.items())),
        "counts": {
            "version_records": len(versions),
            "version_scan_results": len(matched_versions),
            "run_records": written_runs,
            "repositories": len(repositories),
            "duplicate_run_keys": 0,
            "missing_detector_states": 0,
        },
        "source_files": {
            name: {
                "path": source_label(path),
                "sha256": input_hashes[name],
            }
            for name, path in input_paths.items()
        },
        "output_files": {
            "run_records": source_label(run_output),
            "version_records": source_label(version_output),
            "version_scan_results": source_label(scan_output),
        },
    }
    metadata_path = output_dir / "matching_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    output_hashes = {
        name: sha256_file(path)
        for name, path in {
            "run_records": run_output,
            "version_records": version_output,
            "version_scan_results": scan_output,
            "matching_metadata": metadata_path,
        }.items()
    }
    return {
        "output_dir": source_label(output_dir),
        "counts": metadata["counts"],
        "detector_versions": sorted(detector_versions),
        "taxonomy_versions": sorted(taxonomy_versions),
        "input_sha256": input_hashes,
        "output_sha256": output_hashes,
        "external_fetch_performed": False,
        "canonical_inputs_modified": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflows", type=Path, default=DEFAULT_WORKFLOWS)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--rq1", type=Path, default=DEFAULT_RQ1)
    parser.add_argument("--detector-states", type=Path, default=DEFAULT_DETECTOR_STATES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = prepare(
        args.workflows,
        args.runs,
        args.rq1,
        args.detector_states,
        args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
