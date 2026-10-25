"""Merge the two batch-2 job shards against their fixed run manifest."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "output_rq23_sample_acquisition_batch2_20260930" / "job_comparison_sample"


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def key(row):
    return row["repository_name"], row["run_id"], row.get("run_attempt") or 1


def main():
    manifest = {key(row) for row in rows(DATA / "sample_manifest.jsonl")}
    collected = {}
    for shard in ("shard0", "shard1"):
        for row in rows(DATA / shard / "run_job_metadata.jsonl"):
            identity = key(row)
            if identity in collected:
                raise ValueError(f"duplicate collected run: {identity}")
            collected[identity] = row
    if set(collected) != manifest:
        raise ValueError(f"manifest mismatch: missing={len(manifest - set(collected))}, extra={len(set(collected) - manifest)}")
    with (DATA / "selected_run_job_metadata.jsonl").open("w", encoding="utf-8") as stream:
        for identity in sorted(collected):
            stream.write(json.dumps(collected[identity], ensure_ascii=False, separators=(",", ":")) + "\n")
    summary = {
        "manifest_runs": len(manifest), "collected_runs": len(collected),
        "exact_attempt_with_jobs": sum(row.get("matched_attempt") == "exact" and bool(row.get("jobs")) for row in collected.values()),
        "exact_attempt_empty_jobs": sum(row.get("matched_attempt") == "exact" and not row.get("jobs") for row in collected.values()),
        "other_attempt_statuses": [{"repository_name": row["repository_name"], "run_id": row["run_id"], "run_attempt": row.get("run_attempt") or 1, "matched_attempt": row.get("matched_attempt"), "error": row.get("error")} for row in collected.values() if row.get("matched_attempt") != "exact"],
        "jobs": sum(len(row.get("jobs") or []) for row in collected.values()),
    }
    (DATA / "job_collection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
