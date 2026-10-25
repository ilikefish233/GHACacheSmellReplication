import re
import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional


CACHE_LOG_PATTERNS = [
    re.compile(r'Cache (hit|miss|not found|restored|saved|size)', re.IGNORECASE),
    re.compile(r'Upload cache', re.IGNORECASE),
    re.compile(r'Download cache', re.IGNORECASE),
    re.compile(r'Received \d+ of \d+', re.IGNORECASE),
    re.compile(r'compression (took|in) \d+', re.IGNORECASE),
    re.compile(r'Cache restored from key', re.IGNORECASE),
    re.compile(r'Cache saved with key', re.IGNORECASE),
    re.compile(r'Substituting the closest key', re.IGNORECASE),
    re.compile(r'Garbage collection', re.IGNORECASE),
    re.compile(r'Cache entry already exists', re.IGNORECASE),
    re.compile(r'Failed to save cache', re.IGNORECASE),
    re.compile(r'Failed to restore cache', re.IGNORECASE),
]

DEP_INSTALL_PATTERNS = [
    re.compile(r'added \d+ packages? in \d+', re.IGNORECASE),
    re.compile(r'Successfully installed .+ in \d+', re.IGNORECASE),
    re.compile(r'yarn install v[\d.]+.*Done in \d+s', re.IGNORECASE),
    re.compile(r'Packages: \d+.*Time: \d+s', re.IGNORECASE),
    re.compile(r'BUILD SUCCESSFUL in \d+s', re.IGNORECASE),
    re.compile(r'BUILD SUCCESS in \d+s', re.IGNORECASE),
    re.compile(r'Compiling .+ Finished .+ in \d+s', re.IGNORECASE),
    re.compile(r'Downloaded .+ in \d+s', re.IGNORECASE),
    re.compile(r'Restore completed in \d+ ms', re.IGNORECASE),
    re.compile(r'Bundle complete!.*in \d+s', re.IGNORECASE),
]


def parse_run_id(run_id: str):
    m = re.match(r'^([^/]+)/([^_]+)_(.+)_(\d+)_(\d+)$', run_id)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(4), m.group(5)


def find_log_entries(zf, owner, repo, run_number, attempt):
    prefix = f"logs/{owner}/{repo}/"
    suffix = f"/{run_number}-{attempt}.tar.gz"
    return [n for n in zf.namelist() if n.startswith(prefix) and n.endswith(suffix)]


def find_log_entries_ci(zf, owner, repo, run_number, attempt):
    suffix = f"/{run_number}-{attempt}.tar.gz"
    matches = []
    for name in zf.namelist():
        if name.endswith(suffix):
            parts = name.split('/')
            if len(parts) >= 4:
                if parts[1].lower() == owner.lower() and parts[2].lower() == repo.lower():
                    matches.append(name)
    return matches


class LogExtractor:

    def __init__(self, zip_path: Path, max_runs: int = 3):
        self.zip_path = zip_path
        self.max_runs = max_runs

    def extract_for_workflow(self, run_ids: List[str]) -> Dict[str, list]:
        results = {}

        if not self.zip_path.exists():
            return results

        selected_runs = run_ids[:self.max_runs]

        with zipfile.ZipFile(self.zip_path, 'r') as zf:
            for run_id in selected_runs:
                parsed = parse_run_id(run_id)
                if not parsed:
                    continue
                owner, repo, run_number, attempt = parsed

                entries = find_log_entries(zf, owner, repo, run_number, attempt)
                if not entries:
                    entries = find_log_entries_ci(zf, owner, repo, run_number, attempt)

                if not entries:
                    continue

                run_snippets = []
                for entry in entries[:2]:
                    try:
                        data = zf.read(entry)
                        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
                            for member in tf.getmembers():
                                if not member.isfile():
                                    continue
                                try:
                                    content = tf.extractfile(member).read().decode('utf-8', errors='replace')
                                except Exception:
                                    continue
                                snippet = self._filter_cache_lines(content)
                                if snippet:
                                    run_snippets.append({
                                        'file': member.name,
                                        'snippet': snippet,
                                    })
                                    if len(run_snippets) >= 5:
                                        break
                    except Exception:
                        continue

                if run_snippets:
                    results[run_id] = run_snippets

        return results

    def _filter_cache_lines(self, content: str, max_lines: int = 50) -> str:
        matched = []
        for line in content.split('\n'):
            for pat in CACHE_LOG_PATTERNS:
                if pat.search(line):
                    matched.append(line.strip())
                    break
            else:
                for pat in DEP_INSTALL_PATTERNS:
                    if pat.search(line):
                        matched.append(line.strip())
                        break

            if len(matched) >= max_lines:
                break

        return '\n'.join(matched)

    def format_log_snippets(self, log_results: Dict[str, list]) -> str:
        if not log_results:
            return "(无可用日志)"

        parts = []
        for i, (run_id, entries) in enumerate(log_results.items(), 1):
            parts.append(f"--- Run #{i} ({run_id}) ---")
            for entry in entries[:3]:
                parts.append(entry['snippet'])
            parts.append('')

        return '\n'.join(parts)