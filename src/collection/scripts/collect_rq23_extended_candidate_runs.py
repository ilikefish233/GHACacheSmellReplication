"""Query runs for older structurally comparable changes in the random sample."""

import json
import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import local


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.collect_rq23_sample_data import ApiError, CUTOFF, client, request_json, runs_in_interval
from src.utils.env_loader import load_env_file


DATA = ROOT / "output_rq23_full_history_sample_20261005"
START = "2025-04-01T00:00:00Z"
EVENTS = {"push", "schedule", "workflow_dispatch"}


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--include-configuration-changes", action="store_true",
                        help="Query all older smell changes, including changes to triggers, job IDs, or runners")
    args = parser.parse_args()
    data = args.data_dir
    transitions = list(rows(data / "candidate_smell_transitions.jsonl"))
    audits = list(rows(data / "transition_change_audit.jsonl"))
    versions = {}
    for row in rows(data / "version_smell_states.jsonl"):
        versions.setdefault((row["repository_name"], row["workflow_path"]), []).append(row)
    for sequence in versions.values():
        sequence.sort(key=lambda row: (row["commit_date"], row["commit_sha"]))
    targets = {}
    windows = {}
    for row in audits:
        change = transitions[row["transition_index"]]
        if change["after_commit_date"] >= "2026-03-12T00:00:00Z" or (
            not args.include_configuration_changes
            and set(row["key_configuration_changes"]) & {"triggers", "job_ids", "runners"}
        ):
            continue
        key = (change["repository_name"], change["workflow_path"])
        targets[key] = change.get("workflow_id")
        sequence = versions[key]
        pos = next(index for index, version in enumerate(sequence) if version["commit_sha"] == change["after_commit_sha"] and version["commit_date"] == change["after_commit_date"])
        point = datetime.fromisoformat(change["after_commit_date"].replace("Z", "+00:00"))
        lower = (point - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        upper = (point + timedelta(days=90)).isoformat().replace("+00:00", "Z")
        intervals = [
            (max(START, change["before_commit_date"], lower), change["after_commit_date"]),
            (change["after_commit_date"], min(CUTOFF, upper, sequence[pos + 1]["commit_date"] if pos + 1 < len(sequence) else CUTOFF)),
        ]
        windows.setdefault(key, []).extend((start, end) for start, end in intervals if start < end)
    for key, intervals in windows.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        windows[key] = merged
    branches = {}
    for row in rows(ROOT / "data_26_6_9_complete_20260609" / "repo_info.jsonl"):
        repo = str(row.get("_id") or row.get("repository_name") or "").lower()
        branch = (row.get("repo") or {}).get("defaultBranch")
        if repo and branch:
            branches[repo] = branch
    status_path = data / "live_candidate_runs_status.jsonl"
    output_path = data / "live_candidate_runs.jsonl"
    done = {(row["repository_name"], row["workflow_path"]) for row in rows(status_path) if row["status"] in {"ok", "not_found"}} if status_path.exists() else set()
    pending = [(key, ref) for key, ref in sorted(targets.items()) if key not in done]
    token = load_env_file().get("GITHUB_TOKEN_2") or load_env_file().get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Missing GitHub credential")
    code, _ = request_json(client(token), "GET", "https://api.github.com/user")
    if code != 200:
        raise RuntimeError(f"GitHub REST credential check failed: HTTP {code}")
    worker_local = local()

    def task(key, workflow_id):
        repo, path = key
        if not hasattr(worker_local, "session"):
            worker_local.session = client(token)
        reference = workflow_id or Path(path).name
        records = {}
        try:
            for start, end in windows[key]:
                for run in runs_in_interval(worker_local.session, repo, start, end, reference):
                    run_path = str(run.get("path") or "").split("@", 1)[0]
                    if run_path.lower() != path.lower() or run.get("head_branch") != branches.get(repo):
                        continue
                    if run.get("event") not in EVENTS or run.get("status") != "completed":
                        continue
                    created = str(run.get("created_at") or "")
                    if not (START <= created <= CUTOFF):
                        continue
                    identity = (run["id"], run.get("run_attempt") or 1)
                    records[identity] = {"repository_name": repo, "workflow_path": path, "workflow_id": run.get("workflow_id"), "run_id": run["id"], "run_attempt": identity[1], "created_at": created, "run_started_at": run.get("run_started_at"), "event": run.get("event"), "head_branch": run.get("head_branch"), "head_sha": run.get("head_sha"), "status": run.get("status"), "conclusion": run.get("conclusion")}
            return key, list(records.values()), "ok", ""
        except ApiError as exc:
            reason = str(exc)
            return key, [], "not_found" if reason == "not_found" else "error", reason
        except Exception as exc:
            return key, [], "error", f"{type(exc).__name__}:{str(exc)[:150]}"

    print(f"target_workflows={len(targets)} intervals={sum(len(value) for value in windows.values())} done={len(done)} pending={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(task, key, ref): key for key, ref in pending}
        completed = 0
        for future in as_completed(futures):
            key, run_rows, status, reason = future.result()
            if run_rows:
                with output_path.open("a", encoding="utf-8") as stream:
                    for row in run_rows:
                        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            with status_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"repository_name": key[0], "workflow_path": key[1], "status": status, "run_count": len(run_rows), "reason": reason}, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed += 1
            if completed % 10 == 0 or status != "ok":
                print(f"completed={completed}/{len(pending)} repo={key[0]} status={status} runs={len(run_rows)} reason={reason[:80]}", flush=True)
    statuses = {(row["repository_name"], row["workflow_path"]): row for row in rows(status_path)}
    summary = {"target_workflows": len(targets), "status": dict(Counter(row["status"] for row in statuses.values())), "run_records": sum(row["run_count"] for row in statuses.values())}
    (data / "live_candidate_runs_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
