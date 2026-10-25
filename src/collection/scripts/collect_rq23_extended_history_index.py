"""Collect older commit indexes for run-observed paths in the frozen RQ2.3 sample."""

import json
import argparse
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.collect_rq23_sample_data import ApiError, CUTOFF, client, query_histories, request_json
from src.utils.env_loader import load_env_file


DATA = ROOT / "output_rq23_full_history_sample_20261005"
START = "2025-04-01T00:00:00Z"


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--target-paths", type=Path, default=None)
    args = parser.parse_args()
    data = args.data_dir
    data.mkdir(exist_ok=True)
    targets = defaultdict(list)
    for row in rows(args.target_paths or data / "target_workflow_paths.jsonl"):
        targets[row["repository_name"]].append(row)
    status_path = data / "history_status.jsonl"
    index_path = data / "history_commit_index.jsonl"
    done = {row["repository_name"] for row in rows(status_path) if row["status"] in {"ok", "not_found", "no_default_branch"}} if status_path.exists() else set()
    pending = [repo for repo in sorted(targets) if repo not in done]
    token = load_env_file().get("GITHUB_TOKEN_2") or load_env_file().get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Missing GitHub credential")
    code, _ = request_json(client(token), "POST", "https://api.github.com/graphql", json={"query": "query{rateLimit{remaining}}"})
    if code != 200:
        raise RuntimeError(f"GitHub GraphQL credential check failed: HTTP {code}")
    worker_local = local()

    def task(repo):
        if not hasattr(worker_local, "session"):
            worker_local.session = client(token)
        try:
            records = query_histories(worker_local.session, repo, targets[repo], start=START, end=CUTOFF)
            return repo, records, "ok", ""
        except ApiError as exc:
            reason = str(exc)
            status = reason if reason in {"not_found", "no_default_branch"} else "error"
            return repo, [], status, reason
        except Exception as exc:
            return repo, [], "error", f"{type(exc).__name__}:{str(exc)[:150]}"

    print(f"target_repositories={len(targets)} done={len(done)} pending={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(task, repo): repo for repo in pending}
        completed = 0
        for future in as_completed(futures):
            repo, records, status, reason = future.result()
            if records:
                with index_path.open("a", encoding="utf-8") as stream:
                    for row in records:
                        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            with status_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"repository_name": repo, "status": status, "record_count": len(records), "reason": reason}, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed += 1
            if completed % 25 == 0 or status != "ok":
                print(f"completed={completed}/{len(pending)} repo={repo} status={status} versions={len(records)} reason={reason[:80]}", flush=True)
    statuses = {row["repository_name"]: row for row in rows(status_path)}
    summary = {"target_repositories": len(targets), "status": dict(Counter(row["status"] for row in statuses.values())), "version_index_rows": sum(row["record_count"] for row in statuses.values()), "start": START, "end": CUTOFF}
    (data / "history_index_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
