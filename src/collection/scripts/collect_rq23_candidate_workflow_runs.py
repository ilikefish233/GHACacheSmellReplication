"""Collect runs only for batch-2 workflows with structurally comparable smell changes."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.collect_rq23_sample_data import ApiError, CUTOFF, START, client, runs_in_interval
from src.utils.env_loader import load_env_file


DATA = ROOT / "output_rq23_sample_acquisition_batch2_20260930"
EVENTS = {"push", "schedule", "workflow_dispatch"}


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    transitions = list(rows(DATA / "candidate_smell_transitions.jsonl"))
    audit = list(rows(DATA / "transition_change_audit.jsonl"))
    targets = {}
    for item in audit:
        if any(key in item["key_configuration_changes"] for key in ("triggers", "job_ids", "runners")):
            continue
        transition = transitions[item["transition_index"]]
        key = (transition["repository_name"], transition["workflow_path"])
        targets[key] = transition.get("workflow_id")
    branches = {}
    for item in rows(ROOT / "data_26_6_9_complete_20260609" / "repo_info.jsonl"):
        name = str(item.get("_id") or "").lower()
        if name:
            branches[name] = (item.get("repo") or {}).get("defaultBranch")
    status_path = DATA / "candidate_workflow_runs_status.jsonl"
    output_path = DATA / "candidate_workflow_runs.jsonl"
    done = {(x["repository_name"], x["workflow_path"]) for x in rows(status_path) if x["status"] == "ok"} if status_path.exists() else set()
    pending = [(key, workflow_id) for key, workflow_id in targets.items() if key not in done]
    token = load_env_file().get("GITHUB_TOKEN_2") or load_env_file().get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Missing GitHub credential")
    thread_local = local()

    def task(key, workflow_id):
        repo, path = key
        if not hasattr(thread_local, "session"):
            thread_local.session = client(token)
        reference = workflow_id or Path(path).name
        records = {}
        try:
            for run in runs_in_interval(thread_local.session, repo, START, CUTOFF, reference):
                run_path = str(run.get("path") or "").split("@", 1)[0]
                if run_path.lower() != path.lower():
                    continue
                if run.get("head_branch") != branches.get(repo) or run.get("event") not in EVENTS:
                    continue
                created = str(run.get("created_at") or "")
                if not (START < created <= CUTOFF):
                    continue
                if run.get("status") != "completed":
                    continue
                identity = (run["id"], run.get("run_attempt") or 1)
                records[identity] = {
                    "repository_name": repo, "workflow_path": path,
                    "workflow_id": run.get("workflow_id"),
                    "run_id": run["id"], "run_attempt": identity[1],
                    "created_at": created, "run_started_at": run.get("run_started_at"),
                    "event": run.get("event"), "head_branch": run.get("head_branch"),
                    "head_sha": run.get("head_sha"), "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                }
            return key, list(records.values()), "ok", ""
        except ApiError as exc:
            return key, [], "error", str(exc)
        except Exception as exc:
            return key, [], "error", f"{type(exc).__name__}:{str(exc)[:140]}"

    print(f"target_workflows={len(targets)} done={len(done)} pending={len(pending)}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(task, key, ref): key for key, ref in pending}
        completed = 0
        for future in as_completed(futures):
            key, run_rows, status, reason = future.result()
            with output_path.open("a", encoding="utf-8") as out:
                for row in run_rows:
                    out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            with status_path.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"repository_name": key[0], "workflow_path": key[1], "status": status, "run_count": len(run_rows), "reason": reason}, ensure_ascii=False, separators=(",", ":")) + "\n")
            completed += 1
            if completed % 15 == 0 or status != "ok":
                print(f"completed={completed}/{len(pending)} repo={key[0]} workflow={key[1]} status={status} runs={len(run_rows)} reason={reason[:80]}", flush=True)
    statuses = list(rows(status_path))
    summary = {"target_workflows": len(targets), "status": dict(Counter(x["status"] for x in statuses)), "selected_run_records": sum(x["run_count"] for x in statuses)}
    (DATA / "candidate_workflow_runs_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
