"""Resume-safe GitHub metadata acquisition for the frozen RQ2.3 sample.

Run with --phase history or --phase runs. This script does not change the
canonical RQ1/RQ2 inputs. History content and job details are later phases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.utils.env_loader import load_env_file


PACKAGE = ROOT.parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
from artifact import DataPaths, add_data_dir, rq23_sample_names


START = "2026-03-11T23:59:59Z"
CUTOFF = "2026-06-09T23:59:59Z"
RUN_START = "2026-03-12"
RUN_END = "2026-06-09"


class ApiError(RuntimeError):
    pass


def client(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "rq23-sample-acquisition/1.0"})
    return session


def request_json(session: requests.Session, method: str, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    for attempt in range(5):
        try:
            response = session.request(method, url, timeout=50, **kwargs)
        except requests.RequestException as exc:
            if attempt == 4:
                raise ApiError(f"transport:{type(exc).__name__}") from exc
            time.sleep(min(2**attempt, 20))
            continue
        remaining = response.headers.get("X-RateLimit-Remaining")
        if response.status_code == 403 and remaining == "0":
            reset = int(response.headers.get("X-RateLimit-Reset") or 0)
            delay = max(1, reset - int(time.time()) + 2)
            time.sleep(delay)
            continue
        if response.status_code in (429, 500, 502, 503, 504) or (response.status_code == 403 and "secondary rate limit" in response.text.lower()):
            if attempt == 4:
                raise ApiError(f"http:{response.status_code}:retry_exhausted")
            time.sleep(min(2**attempt * 3 + random.random(), 45))
            continue
        try:
            body = response.json()
        except ValueError:
            raise ApiError(f"http:{response.status_code}:invalid_json")
        if not isinstance(body, dict):
            raise ApiError(f"http:{response.status_code}:non_object")
        return response.status_code, body
    raise AssertionError("unreachable")


def read_status(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                record = json.loads(line)
                if record.get("status") in {"ok", "not_found", "no_default_branch"}:
                    done.add(record["repository_name"])
    return done


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()


def query_histories(session: requests.Session, repo: str, workflows: list[dict[str, Any]],
                    start: str = START, end: str = CUTOFF) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(workflows), 6):
        batch = workflows[offset:offset + 6]
        fields = []
        for index, item in enumerate(batch):
            path = json.dumps(item["workflow_path"])
            fields.append(f'h{index}:history(path:{path},since:"{start}",until:"{end}",first:100){{totalCount nodes{{oid committedDate}} pageInfo{{hasNextPage endCursor}}}}')
            fields.append(f'b{index}:history(path:{path},until:"{start}",first:1){{nodes{{oid committedDate}}}}')
        query = 'query{repository(owner:' + json.dumps(owner) + ',name:' + json.dumps(name) + '){defaultBranchRef{target{... on Commit{' + ' '.join(fields) + '}}}} rateLimit{remaining cost}}'
        code, payload = request_json(session, "POST", "https://api.github.com/graphql", json={"query": query})
        if any(error.get("type") == "NOT_FOUND" and error.get("path") == ["repository"] for error in payload.get("errors") or []):
            raise ApiError("not_found")
        if code != 200 or payload.get("errors"):
            raise ApiError(f"graphql:{code}:{str(payload.get('errors') or payload.get('message'))[:180]}")
        repository = (payload.get("data") or {}).get("repository")
        if repository is None:
            raise ApiError("not_found")
        branch = repository.get("defaultBranchRef")
        if branch is None:
            raise ApiError("no_default_branch")
        target = branch.get("target") or {}
        for index, item in enumerate(batch):
            history = target.get(f"h{index}")
            baseline = target.get(f"b{index}")
            if history is None or baseline is None:
                raise ApiError(f"missing_history_field:{item['workflow_path']}")
            common = {"repository_name": repo, "workflow_path": item["workflow_path"], "workflow_id": item.get("workflow_id")}
            for node in baseline.get("nodes") or []:
                rows.append({**common, "commit_sha": node["oid"], "commit_date": node["committedDate"], "role": "baseline"})
            for node in history.get("nodes") or []:
                rows.append({**common, "commit_sha": node["oid"], "commit_date": node["committedDate"], "role": "window"})
            page = history.get("pageInfo") or {}
            cursor = page.get("endCursor")
            while page.get("hasNextPage"):
                if not cursor:
                    raise ApiError(f"missing_cursor:{item['workflow_path']}")
                more_query = 'query{repository(owner:' + json.dumps(owner) + ',name:' + json.dumps(name) + '){defaultBranchRef{target{... on Commit{history(path:' + json.dumps(item["workflow_path"]) + ',since:"' + start + '",until:"' + end + '",first:100,after:' + json.dumps(cursor) + '){nodes{oid committedDate} pageInfo{hasNextPage endCursor}}}}}} rateLimit{remaining cost}}'
                more_code, more = request_json(session, "POST", "https://api.github.com/graphql", json={"query": more_query})
                if more_code != 200 or more.get("errors"):
                    raise ApiError(f"graphql_pagination:{more_code}:{str(more.get('errors') or more.get('message'))[:180]}")
                more_history = more["data"]["repository"]["defaultBranchRef"]["target"]["history"]
                for node in more_history.get("nodes") or []:
                    rows.append({**common, "commit_sha": node["oid"], "commit_date": node["committedDate"], "role": "window"})
                page = more_history.get("pageInfo") or {}
                cursor = page.get("endCursor")
    return rows


def month_intervals() -> list[tuple[str, str]]:
    return [("2026-03-12", "2026-03-31"), ("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-31"), ("2026-06-01", "2026-06-09")]


def runs_in_interval(session: requests.Session, repo: str, start: str, end: str,
                     workflow_ref: str | int | None = None) -> Iterator[dict[str, Any]]:
    if workflow_ref is None:
        url = f"https://api.github.com/repos/{repo}/actions/runs"
    else:
        from urllib.parse import quote
        url = f"https://api.github.com/repos/{repo}/actions/workflows/{quote(str(workflow_ref), safe='')}/runs"
    code, payload = request_json(session, "GET", url, params={"created": f"{start}..{end}", "per_page": 100, "page": 1})
    if code == 404:
        raise ApiError("not_found")
    if code != 200:
        raise ApiError(f"runs_http:{code}:{str(payload.get('message'))[:120]}")
    total = int(str(payload.get("total_count") or 0).rstrip("+"))
    if total >= 1000:
        first = datetime.fromisoformat(start.replace("Z", "+00:00")) if "T" in start else datetime.combine(date.fromisoformat(start), datetime.min.time(), tzinfo=timezone.utc)
        last = datetime.fromisoformat(end.replace("Z", "+00:00")) if "T" in end else datetime.combine(date.fromisoformat(end), datetime.max.time().replace(microsecond=0), tzinfo=timezone.utc)
        if (last - first).total_seconds() < 2:
            raise ApiError(f"one_second_run_limit:{start}:{total}")
        middle = first + timedelta(seconds=int((last - first).total_seconds() // 2))
        left_end = middle.isoformat().replace("+00:00", "Z")
        right_start = (middle + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        yield from runs_in_interval(session, repo, first.isoformat().replace("+00:00", "Z"), left_end, workflow_ref)
        yield from runs_in_interval(session, repo, right_start, last.isoformat().replace("+00:00", "Z"), workflow_ref)
        return
    yield from payload.get("workflow_runs") or []
    for page in range(2, (total + 99) // 100 + 1):
        page_code, page_payload = request_json(session, "GET", url, params={"created": f"{start}..{end}", "per_page": 100, "page": page})
        if page_code != 200:
            raise ApiError(f"runs_page_http:{page_code}:{start}:{page}")
        yield from page_payload.get("workflow_runs") or []


def query_runs(session: requests.Session, repo: str) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for start, end in month_intervals():
        for run in runs_in_interval(session, repo, start, end):
            created = str(run.get("created_at") or "")
            if not (START < created <= CUTOFF):
                continue
            key = (run["id"], run.get("run_attempt") or 1)
            by_key[key] = {
                "repository_name": repo, "run_id": run["id"], "run_attempt": key[1],
                "workflow_id": run.get("workflow_id"), "workflow_path": run.get("path"),
                "created_at": created, "run_started_at": run.get("run_started_at"),
                "updated_at": run.get("updated_at"), "event": run.get("event"),
                "head_sha": run.get("head_sha"), "head_branch": run.get("head_branch"),
                "status": run.get("status"), "conclusion": run.get("conclusion"),
                "run_number": run.get("run_number"),
            }
    return list(by_key.values())


def query_blobs(session: requests.Session, repo: str, commits: list[dict[str, Any]], output_dir: Path,
                shared_content: Path | None = None) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    results: list[dict[str, Any]] = []
    blob_dir = output_dir / "workflow_blobs"
    for offset in range(0, len(commits), 12):
        batch = commits[offset:offset + 12]
        fields = [f'v{index}:object(expression:{json.dumps(item["commit_sha"] + ":" + item["workflow_path"])}){{... on Blob{{oid text byteSize isBinary}}}}' for index, item in enumerate(batch)]
        query = 'query{repository(owner:' + json.dumps(owner) + ',name:' + json.dumps(name) + '){' + ' '.join(fields) + '} rateLimit{remaining cost}}'
        code, payload = request_json(session, "POST", "https://api.github.com/graphql", json={"query": query})
        if code != 200 or payload.get("errors"):
            raise ApiError(f"graphql_blob:{code}:{str(payload.get('errors') or payload.get('message'))[:180]}")
        objects = (payload.get("data") or {}).get("repository")
        if objects is None:
            raise ApiError("not_found")
        for index, item in enumerate(batch):
            obj = objects.get(f"v{index}")
            row = dict(item)
            if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
                row.update({"content_status": "unavailable", "blob_oid": obj.get("oid") if isinstance(obj, dict) else None})
            else:
                content = obj["text"]
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                path = (shared_content / f"{digest}.yml" if shared_content is not None
                        else blob_dir / digest[:2] / f"{digest}.yml")
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    temporary = path.with_suffix(f".{threading.get_ident()}.partial")
                    temporary.write_bytes(content.encode("utf-8"))
                    temporary.replace(path)
                content_root = shared_content if shared_content is not None else output_dir
                row.update({"content_status": "ok", "blob_oid": obj.get("oid"),
                            "content_sha256": digest, "content_file": path.relative_to(content_root).as_posix()})
                if shared_content is not None:
                    row["content_store"] = "shared"
            results.append(row)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["history", "runs", "blobs"], required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit-repos", type=int, default=0)
    add_data_dir(parser)
    parser.add_argument("--changed-workflows-only", action="store_true",
                        help="For blob phase, fetch baseline and window YAML only for paths with a window commit")
    parser.add_argument("--token-env", default=None)
    parser.add_argument("--after-repo", default=None,
                        help="Only collect sample repositories lexicographically after this name")
    parser.add_argument("--through-repo", default=None,
                        help="Only collect sample repositories lexicographically through this name")
    parser.add_argument("--only-repos-file", type=Path, default=None,
                        help="Only collect repositories listed in this UTF-8 file")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        raise ValueError("workers must be 1..8")
    paths = DataPaths(args.data_dir)
    if paths.mapping is None:
        parser.error("RQ2.3 collection requires zenodo_data/manifest.json or --data-dir")
    sample = rq23_sample_names()
    output_dir = paths.path("RQ2/RQ2_3/data/collection")
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / f"{args.phase}_status.jsonl"
    data_path = output_dir / {"history": "history_commit_index.jsonl", "runs": "workflow_runs.jsonl", "blobs": "workflow_versions.jsonl"}[args.phase]
    done = read_status(status_path)
    pending = [repo for repo in sample if repo not in done]
    if args.after_repo:
        pending = [repo for repo in pending if repo > args.after_repo]
    if args.through_repo:
        pending = [repo for repo in pending if repo <= args.through_repo]
    if args.only_repos_file:
        selected_repos = set(args.only_repos_file.read_text(encoding="utf-8").splitlines())
        if not selected_repos <= set(sample):
            raise ValueError("only-repos-file contains names outside the frozen sample")
        pending = [repo for repo in pending if repo in selected_repos]
    if args.limit_repos:
        pending = pending[:args.limit_repos]
    workflows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if args.phase == "history":
        selected = set(pending)
        with paths.path("data/canonical/workflow_snapshots.jsonl").open(encoding="utf-8-sig") as stream:
            for line in stream:
                item = json.loads(line)
                name = (item.get("canonical_name") or item["repository_name"]).lower()
                if name in selected:
                    workflows[name].append(item)
    if args.phase == "blobs":
        history_index = output_dir / "history_commit_index.jsonl"
        if not history_index.is_file():
            parser.error(f"Collect history before downloading workflow versions: {history_index}")
        history_done = read_status(output_dir / "history_status.jsonl")
        pending = [repo for repo in pending if repo in history_done]
        selected = set(pending)
        history_rows = []
        with history_index.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                if item["repository_name"] in selected:
                    history_rows.append(item)
        changed = {(item["repository_name"], item["workflow_path"]) for item in history_rows if item["role"] == "window"}
        for item in history_rows:
            if not args.changed_workflows_only or (item["repository_name"], item["workflow_path"]) in changed:
                workflows[item["repository_name"]].append(item)
    env = load_env_file()
    token_name = args.token_env or ("GITHUB_TOKEN_2" if args.phase == "runs" else "GITHUB_TOKEN")
    token = os.environ.get(token_name) or env.get(token_name)
    if not token:
        raise RuntimeError("Missing GitHub credential in config/.env")
    local = threading.local()

    def task(repo: str) -> tuple[str, list[dict[str, Any]], str, str]:
        if not hasattr(local, "session"):
            local.session = client(token)
        try:
            if args.phase == "history":
                rows = query_histories(local.session, repo, workflows[repo])
            elif args.phase == "blobs":
                rows = query_blobs(local.session, repo, workflows[repo], output_dir,
                                   paths.path("data/canonical/content"))
            else:
                rows = query_runs(local.session, repo)
            return repo, rows, "ok", ""
        except ApiError as exc:
            reason = str(exc)
            status = reason if reason in {"not_found", "no_default_branch"} else "error"
            return repo, [], status, reason
        except Exception as exc:
            return repo, [], "error", f"unexpected:{type(exc).__name__}:{str(exc)[:150]}"

    print(f"phase={args.phase} total_sample={len(sample)} already_done={len(done)} pending={len(pending)}", flush=True)
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(task, repo): repo for repo in pending}
        for future in as_completed(tuple(futures)):
            repo, rows, status, reason = future.result()
            append_jsonl(data_path, rows)
            append_jsonl(status_path, [{"repository_name": repo, "status": status, "record_count": len(rows), "reason": reason}])
            futures.pop(future, None)
            completed += 1
            if completed % 25 == 0 or status != "ok":
                print(f"{args.phase} completed={completed}/{len(pending)} repo={repo} status={status} rows={len(rows)} reason={reason[:100]}", flush=True)


if __name__ == "__main__":
    main()
