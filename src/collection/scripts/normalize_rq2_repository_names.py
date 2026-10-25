"""Normalize repository aliases in derived RQ2 inputs.

The workflow index and the GitHub API do not always use the same repository
name after a rename or redirect.  This utility maps both names in
``repo_info.jsonl`` to the repository-name spelling used by the current
workflow index, so run and history joins are based on repository identity
rather than a stale display name.  It only writes a derived output and never
contacts GitHub or changes the input file.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Set


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                yield value


def normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def load_workflow_repositories(path: Path) -> Set[str]:
    repositories: Set[str] = set()
    for record in load_jsonl(path):
        repository = normalize(record.get("repository_name"))
        if repository:
            repositories.add(repository)
    return repositories


def load_aliases(path: Path, workflow_repositories: Set[str]) -> Dict[str, str]:
    candidates: Dict[str, Set[str]] = defaultdict(set)
    fallbacks: Dict[str, str] = {}
    for record in load_jsonl(path):
        repo = record.get("repo") or {}
        source = normalize(record.get("_id"))
        api_name = normalize(repo.get("name"))
        aliases = {value for value in (source, api_name) if value}
        current_candidates = aliases & workflow_repositories
        fallback = source or api_name
        for alias in aliases:
            candidates[alias].update(current_candidates)
            if fallback:
                fallbacks.setdefault(alias, fallback)

    aliases: Dict[str, str] = {}
    for alias, values in candidates.items():
        if len(values) == 1:
            aliases[alias] = next(iter(values))
    for alias, fallback in fallbacks.items():
        aliases.setdefault(alias, fallback)
    return aliases


def normalize_file(
    input_path: Path,
    output_path: Path,
    aliases: Mapping[str, str],
    workflow_repositories: Set[str],
) -> Dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(f"Output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records_read = 0
    records_written = 0
    changed = 0
    unresolved = Counter()
    with input_path.open("r", encoding="utf-8") as source, output_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as target:
        for line_number, line in enumerate(source, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {input_path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                continue
            records_read += 1
            original = normalize(record.get("repository_name"))
            resolved = original
            if original and original not in workflow_repositories:
                candidate = normalize(aliases.get(original))
                if candidate:
                    resolved = candidate
            if original and resolved != original:
                record = {
                    **record,
                    "repository_name": resolved,
                    "_repository_name_original": record.get("repository_name"),
                }
                changed += 1
            if resolved and resolved not in workflow_repositories:
                unresolved[original or "<missing>"] += 1
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            records_written += 1

    metadata = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "records_read": records_read,
        "records_written": records_written,
        "repository_names_changed": changed,
        "records_unresolved_against_workflow_index": sum(unresolved.values()),
        "top_unresolved_repository_names": unresolved.most_common(20),
        "workflow_repository_count": len(workflow_repositories),
        "alias_count": len(aliases),
        "external_fetch_performed": False,
    }
    output_path.with_name(output_path.stem + "_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize repository aliases in an RQ2 JSONL input")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workflows", type=Path, required=True)
    parser.add_argument("--repos", type=Path, required=True)
    args = parser.parse_args()

    workflow_repositories = load_workflow_repositories(args.workflows)
    aliases = load_aliases(args.repos, workflow_repositories)
    normalize_file(args.input, args.output, aliases, workflow_repositories)


if __name__ == "__main__":
    main()
