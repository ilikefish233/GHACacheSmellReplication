"""Find formal CCS transitions in the RQ2.3 sample histories."""
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import hashlib
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, new_output, rows, rq23_sample_names, write

FORMAL = {
    "no_dependency_cache", "no_fallback_cache", "redundant_cache_update",
    "cache_position_error", "conflicting_cache_sources", "static_cache_key",
    "overly_specific_cache_key", "no_docker_layer_cache",
    "deprecated_cache_action", "unreachable_cache_write", "cache_as_artifact",
}
CUTOFF = "2026-06-10T00:00:00Z"
ALIASES = {
    "no_restore_keys": "no_fallback_cache",
    "disposable_cache_key": "overly_specific_cache_key",
    "overlapping_cache_mechanisms": "conflicting_cache_sources",
}


def scan_version(record, collection, shared_content, detector):
    directory = shared_content if record.get("content_store") == "shared" else collection
    relative = Path(record.get("content_file") or ".")
    path = (directory / relative).resolve()
    if relative.anchor or not path.is_relative_to(directory):
        raise ValueError(f"Invalid workflow content path: {relative}")
    if not path.is_file():
        return {"error": "missing_content"}
    try:
        raw = path.read_bytes()
        text = path.read_text(encoding="utf-8")
        normalized = text.encode("utf-8")
        content_hash = record.get("content_sha256")
        if not any(hashlib.sha256(data).hexdigest() == content_hash
                   for data in (raw, raw.replace(b"\r\n", b"\n"), normalized)):
            return {"error": "content_hash_mismatch"}
        result = detector.detect_from_file(path, workflow_path=record["workflow_path"]).to_dict()
        if result.get("error"):
            return {"error": str(result["error"])}
        labels = {ALIASES.get(smell["type"], smell["type"]) for smell in result.get("smells", [])}
        return {
            "labels": sorted(labels & FORMAL),
            "all_detector_types": sorted(labels),
            "content_sha256": hashlib.sha256(normalized).hexdigest(),
        }
    except (ValueError, OSError) as exc:
        return {"error": str(exc)}


def initialize_detector():
    global detector
    sys.path.insert(0, str(PACKAGE / "src/frozen"))
    from src.detectors.smell.cache_smell_detector import CacheSmellDetector
    detector = CacheSmellDetector()


def scan_workflow(item, collection, shared_content):
    (repo, path), commits = item
    states, blocks = [], []
    for record in sorted(commits.values(), key=lambda row: (row["commit_date"], row["commit_sha"])):
        result = ({"error": "conflicting_content"} if record.get("content_conflict")
                  else scan_version(record, collection, shared_content, detector))
        state = {**record, **result}
        states.append(state)
        # Identical consecutive content uses the first commit as the interval boundary.
        if (blocks and not state.get("error") and not blocks[-1].get("error")
                and state["content_sha256"] == blocks[-1]["content_sha256"]):
            continue
        blocks.append(state)
    events = []
    for i, (before, after) in enumerate(zip(blocks, blocks[1:]), 1):
        if before.get("error") or after.get("error") or before["commit_date"] >= after["commit_date"]:
            continue
        changed = set(before["labels"]) ^ set(after["labels"])
        if not changed:
            continue
        rid = str(before.get("repository_id") or f"name:{repo}")
        before_sha, after_sha = before["commit_sha"], after["commit_sha"]
        identity = json.dumps([rid, path, before_sha, after_sha], ensure_ascii=False, separators=(",", ":"))
        events.append({
            "event_id": "exp_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
            "repository_id": rid,
            "repository_name": repo,
            "workflow_path": path,
            "before_commit_sha": before_sha,
            "after_commit_sha": after_sha,
            "before_start": before["commit_date"],
            "change_time": after["commit_date"],
            "after_end": blocks[i + 1]["commit_date"] if i + 1 < len(blocks) else CUTOFF,
            "before_content_sha256": before["content_sha256"],
            "after_content_sha256": after["content_sha256"],
            "before_labels": before["labels"],
            "after_labels": after["labels"],
            "changed_formal_types": sorted(changed),
        })
    return states, events


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    add_data_dir(cli)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--workers", type=int, default=4)
    args = cli.parse_args()
    if args.workers < 1:
        cli.error("--workers must be positive")
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        cli.error("RQ2.3 scan requires zenodo_data/manifest.json or --data-dir")
    collection = paths.path("RQ2/RQ2_3/data/collection")
    shared_content = paths.path("data/canonical/content")
    source = collection / "workflow_versions.jsonl"
    if not source.is_file():
        cli.error(f"Collect workflow versions first: {source}")
    names = set(rq23_sample_names())
    versions = defaultdict(dict)
    for record in rows(source):
        repo = record["repository_name"].lower()
        if repo not in names or not record.get("commit_date") or record["commit_date"] >= CUTOFF:
            continue
        key = repo, record["workflow_path"]
        sha = record["commit_sha"]
        prior = versions[key].get(sha)
        if prior is None:
            versions[key][sha] = record
        elif prior.get("content_sha256") != record.get("content_sha256"):
            if prior.get("content_sha256") and record.get("content_sha256"):
                versions[key][sha] = {**record, "content_conflict": True}
            elif not prior.get("content_sha256"):
                versions[key][sha] = record

    output = new_output(args.output)
    count = errors = candidates = 0
    worker = partial(scan_workflow, collection=collection, shared_content=shared_content)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=initialize_detector) as pool, (
        output / "version_scan_states.jsonl"
    ).open("w", encoding="utf-8") as states, (
        output / "candidate_transitions.jsonl"
    ).open("w", encoding="utf-8") as events:
        for workflow_states, workflow_events in pool.map(worker, sorted(versions.items()), chunksize=8):
            for state in workflow_states:
                states.write(json.dumps(state, ensure_ascii=False) + "\n")
                errors += bool(state.get("error"))
            for event in workflow_events:
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
            count += len(workflow_states)
            candidates += len(workflow_events)
    write(output / "scan_summary.json", {
        "sample_repositories": len(names),
        "repositories_with_versions": len({repo for repo, _ in versions}),
        "versions": count,
        "version_errors": errors,
        "candidate_events": candidates,
    })


if __name__ == "__main__":
    main()
