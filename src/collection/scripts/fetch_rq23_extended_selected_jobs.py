"""Fetch exact-attempt jobs for selected older random-sample comparisons."""

import json
import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.fetch_current_run_jobs import ResourceClient, fetch_resource_item, token_from_env


DATA = ROOT / "output_rq23_full_history_sample_20261005" / "job_comparison_sample"


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def key(row):
    return row["repository_name"], row["run_id"], row.get("run_attempt") or 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DATA.parent)
    args = parser.parse_args()
    data = args.data_dir / "job_comparison_sample"
    manifest = {key(row): row for row in rows(data / "sample_manifest.jsonl")}
    output = data / "selected_run_job_metadata.jsonl"
    existing = {key(row): row for row in rows(output)} if output.exists() else {}
    pending = [row for identity, row in manifest.items() if identity not in existing or existing[identity].get("matched_attempt") not in {"exact", "not_found", "attempt_not_found"}]
    client = ResourceClient(token_from_env("GITHUB_TOKEN_2"))
    check = client.get("https://api.github.com/user", {})
    if check is None or check.status_code != 200:
        raise RuntimeError(f"GitHub credential check failed: {None if check is None else check.status_code}")
    print(f"manifest_runs={len(manifest)} existing={len(existing)} pending={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_resource_item, client, row): row for row in pending}
        completed = 0
        for future in as_completed(futures):
            row = future.result()
            existing[key(row)] = row
            with output.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed += 1
            if completed % 20 == 0 or row.get("matched_attempt") != "exact":
                print(f"completed={completed}/{len(pending)} status={row.get('matched_attempt')} repo={row['repository_name']}", flush=True)
    if set(existing) != set(manifest):
        raise ValueError(f"job manifest mismatch: missing={len(set(manifest) - set(existing))}")
    summary = {"selected_runs": len(manifest), "exact_attempt_with_jobs": sum(row.get("matched_attempt") == "exact" and bool(row.get("jobs")) for row in existing.values()), "statuses": dict(Counter(row.get("matched_attempt") for row in existing.values())), "jobs": sum(len(row.get("jobs") or []) for row in existing.values())}
    (data / "job_collection_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
