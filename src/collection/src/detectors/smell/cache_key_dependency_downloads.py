"""S6: workflow commit partitioning of explicit dependency download resources.

Keep this local to S6. Parent/mixed build caches cannot inherit this verdict.
"""
import re

from .cache_consumption import CacheConsumptionAnalyzer, HOME
from .cache_key_runtime_sources import RuntimeCacheKeySources


# Full component matches, under a known home/cache location, never arbitrary
# directory-name substrings. Maven/Gradle roots and Cargo target/src are mixed.
DOWNLOAD_PATHS = {
    'npm': rf'{HOME}/\.npm(?:/_cacache(?:/[^*?\[\]]+)?)?',
    'pip': rf'{HOME}/(?:\.cache/pip|\.pip/cache)(?:/[^*?\[\]]+)?',
    'yarn': rf'{HOME}/\.cache/yarn(?:/[^*?\[\]]+)?',
    'pnpm': rf'{HOME}/(?:\.pnpm-store|\.local/share/pnpm/store)(?:/[^*?\[\]]+)?',
    'cargo_downloads': rf'{HOME}/\.cargo/registry/(?:cache|index)(?:/[^*?\[\]]+)?',
    'go_downloads': rf'{HOME}/go/pkg/mod/cache/download(?:/[^*?\[\]]+)?',
    'pip_windows': r'[a-z]:/users/[^/]+/appdata/local/pip/cache(?:/[^*?\[\]]+)?',
    'npm_windows': r'[a-z]:/users/[^/]+/appdata/local/npm-cache(?:/[^*?\[\]]+)?',
    'yarn_windows': r'[a-z]:/users/[^/]+/appdata/local/yarn/cache(?:/[^*?\[\]]+)?',
}


def workflow_sha_download_evidence(workflow, job, cache_info):
    """Return proof only if every cached path is a known download cache."""
    index = cache_info.get('index')
    if job is None or not isinstance(index, int) or not 0 <= index < len(job.steps):
        return None
    action = str(cache_info.get('action') or '').split('@', 1)[0].lower()
    if action not in {'actions/cache', 'actions/cache/restore', 'actions/cache/save'}:
        return None
    lookup = cache_info.get('lookup_only')
    if (cache_info.get('setup_cache') or lookup is True
            or (lookup is not None and lookup is not False
                and (not isinstance(lookup, str) or lookup.strip().lower() != 'false'))):
        return None
    step = job.steps[index]
    raw = cache_info.get('path')
    if not isinstance(raw, str):
        return None
    # This resolver covers static YAML env only; a prior shell may override it.
    if '$' in raw or '%' in raw:
        if any('GITHUB_ENV' in str(prior.run or '') for prior in job.steps[:index]):
            return None
        # Opaque actions alone do not establish an overwrite of YAML env paths.
        # Visible GITHUB_ENV writes remain unknown to this static path resolver.
    paths, partial = CacheConsumptionAnalyzer(workflow, job).cache_paths(raw, step)
    if partial or not paths or any(re.search(r'[*?\[\]]', path) for path in paths):
        return None
    resources = set()
    for path in paths:
        kind = next((name for name, pattern in DOWNLOAD_PATHS.items()
                     if re.fullmatch(pattern, path)), None)
        if kind is None:
            return None
        resources.add(kind)
    sources = RuntimeCacheKeySources(workflow, job, additional_contexts={'github.workflow_sha'}).evidence(
        cache_info.get('key'), index,
    )
    if 'github.workflow_sha' not in sources:
        return None
    return {'reason': 'workflow_commit_partitions_dependency_downloads',
            'key_sources': sources, 'download_resources': sorted(resources),
            'resolved_paths': paths}
