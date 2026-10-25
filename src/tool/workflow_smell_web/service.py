"""Fetch one GitHub Actions workflow and apply the existing research detector."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from src.detectors.smell.cache_smell_detector import CacheSmellDetector
from src.parsers.workflow_parser import WorkflowParser
from workflow_smell_web.presentation import RULES, finding_text, step_label


ALIASES = {
    "no_restore_keys": "no_fallback_cache",
    "conflicting_cache_sources": "overlapping_cache_mechanisms",
    "disposable_cache_key": "overly_specific_cache_key",
}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_YAML_BYTES = 1_000_000


class ScanError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate_target(repository: str, workflow_path: str) -> tuple[str, str]:
    repository = repository.strip()
    workflow_path = workflow_path.strip().replace("\\", "/")
    if not REPO_RE.fullmatch(repository) or any(part in {".", ".."} for part in repository.split("/")):
        raise ScanError("Enter a repository as owner/repo, for example actions/checkout.")
    if (not workflow_path.startswith(".github/workflows/")
            or not workflow_path.endswith((".yml", ".yaml"))
            or any(part in {"", ".", ".."} for part in workflow_path.split("/"))
            or "?" in workflow_path or "#" in workflow_path):
        raise ScanError("Enter a .yml or .yaml file path under .github/workflows/.")
    return repository, workflow_path


def fetch_workflow(repository: str, workflow_path: str) -> dict:
    target = "/".join(quote(part, safe="") for part in workflow_path.split("/"))
    url = f"https://api.github.com/repos/{repository}/contents/{target}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "research-cache-smell-viewer"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            payload = response.read(2_000_000)
    except HTTPError as exc:
        if exc.code == 404:
            raise ScanError("Repository or workflow not found, or access to this private repository was denied.", 404) from exc
        if exc.code in (401, 403, 429):
            # Public raw files remain readable when the shared unauthenticated API quota is exhausted.
            return _fetch_public_raw(repository, workflow_path)
        raise ScanError(f"GitHub API returned HTTP {exc.code}.", 502) from exc
    except (URLError, TimeoutError) as exc:
        raise ScanError("Could not connect to GitHub. Please try again later.", 502) from exc
    try:
        data = json.loads(payload)
        if data.get("type") != "file" or data.get("encoding") != "base64":
            raise ScanError("GitHub did not return a readable workflow file.", 422)
        content = base64.b64decode(data["content"], validate=False)
        if len(content) > MAX_YAML_BYTES:
            raise ScanError("The workflow exceeds the 1 MB scan limit.", 413)
        return {
            "content": content.decode("utf-8-sig"),
            "blob_sha": data.get("sha"),
            "html_url": data.get("html_url"),
        }
    except (KeyError, ValueError, UnicodeDecodeError, TypeError) as exc:
        raise ScanError("The file returned by GitHub could not be decoded as UTF-8 YAML.", 422) from exc


def _fetch_public_raw(repository: str, workflow_path: str) -> dict:
    target = "/".join(quote(part, safe="") for part in workflow_path.split("/"))
    url = f"https://raw.githubusercontent.com/{repository}/HEAD/{target}"
    try:
        with urlopen(Request(url, headers={"User-Agent": "research-cache-smell-viewer"}), timeout=15) as response:
            content = response.read(MAX_YAML_BYTES + 1)
    except HTTPError as exc:
        if exc.code == 404:
            raise ScanError("Public workflow not found. For private repositories, set a valid GITHUB_TOKEN.", 404) from exc
        raise ScanError(f"GitHub file service returned HTTP {exc.code}.", 502) from exc
    except (URLError, TimeoutError) as exc:
        raise ScanError("Could not connect to the GitHub file service. Please try again later.", 502) from exc
    if len(content) > MAX_YAML_BYTES:
        raise ScanError("The workflow exceeds the 1 MB scan limit.", 413)
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScanError("The file returned by GitHub is not UTF-8 YAML.", 422) from exc
    return {
        "content": decoded,
        "blob_sha": None,
        "html_url": f"https://github.com/{repository}/blob/HEAD/{target}",
    }


def scan_content(repository: str, workflow_path: str, content: str, *,
                 blob_sha: str | None = None, html_url: str | None = None) -> dict:
    workflow = WorkflowParser().parse_content(
        content, repository_name=repository, workflow_path=workflow_path
    )
    if workflow is None:
        raise ScanError("Could not parse the workflow YAML. Check the file syntax.", 422)
    if not workflow.has_jobs:
        raise ScanError("The workflow has no jobs to scan.", 422)

    result = CacheSmellDetector().detect(workflow)
    if result.error:
        raise ScanError("The detector could not complete the scan. Check the server logs for details.", 422)
    findings = []
    for smell in result.smells:
        name = ALIASES.get(smell.type, smell.type)
        if name not in RULES:
            continue
        label, description = RULES[name]
        message, suggestion = finding_text(name, smell)
        findings.append({
            "type": name,
            "label": label,
            "description": description,
            "message": message,
            "suggestion": suggestion,
            "job": smell.job_name,
            "step": step_label(smell.step_name),
            "line": smell.line_number,
            "details": smell.to_dict()["details"],
        })
    findings.sort(key=lambda item: (item["type"], item["job"] or ""))
    counts = Counter(item["type"] for item in findings)
    return {
        "repository": repository,
        "workflow_path": workflow_path,
        "workflow_name": workflow.name or workflow_path.rsplit("/", 1)[-1],
        "job_count": len(workflow.jobs),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "github_blob_sha": blob_sha,
        "source_url": html_url,
        "rules": [{"type": name, "label": label, "description": description, "count": counts[name]}
                  for name, (label, description) in RULES.items()],
        "findings": findings,
        "summary": {"total": len(findings), "types": sum(count > 0 for count in counts.values())},
    }


def scan_github(repository: str, workflow_path: str) -> dict:
    repository, workflow_path = validate_target(repository, workflow_path)
    source = fetch_workflow(repository, workflow_path)
    return scan_content(repository, workflow_path, source["content"],
                        blob_sha=source["blob_sha"], html_url=source["html_url"])
