"""Offline reference-aware policy for the legacy GitHub cache service migration."""
from functools import lru_cache
import json
from pathlib import Path
import re

POLICY_SOURCE = 'https://github.com/actions/cache#whats-new'
DEPRECATION_LIST_SOURCE = 'https://github.com/orgs/community/discussions/148746'
REGISTRY_PATH = Path(__file__).with_name('cache_action_release_registry.json')
DEPRECATED_LISTED_REFS = frozenset({
    'v3.2.0-beta.1', 'v3.1.0-beta.3', 'v3.1.0-beta.2', 'v3.1.0-beta.1',
    'v0.0.2', 'v0.0.1',
})
VERSION_PART = r'(0|[1-9]\d*)'
STABLE = re.compile(r'v?' + r'\.'.join([VERSION_PART] * 3))


def classify_version_ref(ref):
    """The verdict is limited to service migration, not runtime/runner support."""
    if ref in DEPRECATED_LISTED_REFS:
        return {'status': 'deprecated', 'reason': 'official_deprecation_list',
                'ref_type': 'prerelease' if '-' in ref else 'stable_version',
                'source': DEPRECATION_LIST_SOURCE}
    stable = STABLE.fullmatch(ref)
    major = re.fullmatch(r'v?' + VERSION_PART, ref)
    if stable:
        version = tuple(map(int, stable.groups()))
        deprecated = version[0] in (1, 2) or (version[0] == 3 and version < (3, 4, 0)) or (version[0] == 4 and version < (4, 2, 0))
        return {'status': 'deprecated' if deprecated else 'not_deprecated' if version[0] >= 3 else 'unknown',
                'reason': 'legacy_cache_service_version' if deprecated else 'stable_version_not_deprecated_by_migration' if version[0] >= 3 else 'version_outside_known_migration_policy',
                'ref_type': 'stable_version', 'version': list(version), 'action_major_version': version[0],
                'source': POLICY_SOURCE}
    if major:
        number = int(major[1])
        return {'status': 'deprecated' if number in (1, 2) else 'not_deprecated' if number >= 3 else 'unknown',
                'reason': 'legacy_major_alias' if number in (1, 2) else 'major_alias_not_deprecated_by_migration' if number >= 3 else 'version_outside_known_migration_policy',
                'ref_type': 'major_alias', 'action_major_version': number, 'source': POLICY_SOURCE}
    prerelease = re.fullmatch(r'v?\d+\.\d+\.\d+-[0-9A-Za-z.-]+', ref)
    return {'status': 'unknown', 'reason': 'reference_not_in_official_deprecation_policy',
            'ref_type': 'prerelease' if prerelease else 'other'}


@lru_cache(maxsize=4)
def _load_registry(path, modified_ns):
    registry = json.loads(Path(path).read_text(encoding='utf-8'))
    if registry.get('schema_version') != 1 or registry.get('repository') != 'actions/cache':
        return {}
    evidence = registry.get('evidence', {})
    refs = evidence.get('tag_refs', [])
    annotated = evidence.get('annotated_tags', {})
    mapped = {}
    for record in refs:
        tag = str(record.get('ref', '')).removeprefix('refs/tags/')
        if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[\w.-]+)?', tag):
            continue
        obj = record.get('object', {})
        seen = set()
        while obj.get('type') == 'tag':
            sha = obj.get('sha')
            if sha in seen or len(seen) >= 8:
                obj = {}
                break
            seen.add(sha)
            obj = annotated.get('https://api.github.com/repos/actions/cache/git/tags/' + str(sha), {}).get('object', {})
        if obj.get('type') == 'commit' and re.fullmatch(r'[a-f0-9]{40}', str(obj.get('sha', ''))):
            mapped.setdefault(obj['sha'], set()).add(tag)
    return {sha: entry for sha, entry in registry.get('entries', {}).items()
            if isinstance(entry, dict) and isinstance(entry.get('versions'), list)
            and set(entry['versions']) == mapped.get(sha) and mapped.get(sha)}


def assess_cache_action_reference(uses, registry_path=None):
    if not isinstance(uses, str) or '@' not in uses:
        return {'status': 'unknown', 'reason': 'action_reference_missing', 'ref_type': 'other'}
    action, ref = uses.rsplit('@', 1)
    if action not in {'actions/cache', 'actions/cache/restore', 'actions/cache/save'}:
        return {'status': 'unknown', 'reason': 'not_official_cache_action', 'ref_type': 'other'}
    if not re.fullmatch(r'[a-fA-F0-9]{40}', ref):
        return classify_version_ref(ref)
    path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    try:
        entry = _load_registry(str(path.resolve()), path.stat().st_mtime_ns).get(ref.lower())
    except (OSError, ValueError, TypeError, AttributeError):
        entry = None
    if not isinstance(entry, dict) or not isinstance(entry.get('versions'), list) or not entry['versions']:
        return {'status': 'unknown', 'reason': 'sha_has_no_verified_release_mapping', 'ref_type': 'sha'}
    versions = entry['versions']
    states = [classify_version_ref(version) for version in versions if isinstance(version, str)]
    statuses = {state['status'] for state in states}
    if len(statuses) != 1 or not states:
        return {'status': 'unknown', 'reason': 'sha_release_mapping_ambiguous', 'ref_type': 'sha', 'mapped_versions': versions}
    return {**states[0], 'ref_type': 'sha', 'mapped_versions': versions, 'source_commit': ref.lower(),
            'resolution': 'offline_official_release_tags', 'mapping_source': entry.get('source')}
