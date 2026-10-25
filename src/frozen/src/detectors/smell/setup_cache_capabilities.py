"""Offline cache capability evidence for verified immutable official action refs.

Checked 2026-10-13 against each pinned action.yml. No version is guessed from
comments or a SHA prefix. Unknown hashes are deliberately not resolved online
during detection. For Node's v5 pin, main.ts also verifies supported managers.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re

import yaml

DEFAULT_REGISTRY_PATH = Path(__file__).with_name('setup_cache_registry.json')


def derive_cache_capability(action, action_yaml, main_source=None):
    """Parse official source as data. Unrecognized source semantics stay unknown."""
    document = yaml.safe_load(action_yaml)
    if not isinstance(document, dict) or not isinstance(document.get('inputs'), dict):
        raise ValueError('Invalid action input schema')
    inputs = document['inputs']
    capability = {'cache_supported': 'cache' in inputs,
                  'action_yaml_sha256': hashlib.sha256(action_yaml.encode()).hexdigest()}
    if action == 'actions/setup-go':
        value = (inputs.get('cache') or {}).get('default', False)
        if value not in (True, False, 'true', 'false'):
            raise ValueError('Unknown Go default-cache semantics')
        capability['default_enabled'] = value is True or value == 'true'
    elif action == 'actions/setup-node':
        cache_description = str((inputs.get('cache') or {}).get('description', ''))
        capability['explicit_managers'] = sorted(set(re.findall(r'\b(?:npm|yarn|pnpm)\b', cache_description))) if 'cache' in inputs else []
        capability['auto_selector'] = None
        if 'package-manager-cache' not in inputs:
            capability['auto_managers'] = []
        else:
            default = (inputs.get('package-manager-cache') or {}).get('default', False)
            capability['auto_default_enabled'] = (default is True or default == 'true') if default in (True, False, 'true', 'false') else None
            # The input schema proves the feature exists, not which managers its
            # implementation selects. Recognize the two observed source forms.
            capability['auto_managers'] = None
            if isinstance(main_source, str):
                capability['main_source_sha256'] = hashlib.sha256(main_source.encode()).hexdigest()
                match = re.search(r'const\s+SUPPORTED_PACKAGE_MANAGERS\s*=\s*\[([^]]+)\]', main_source)
                if match and 'getNameFromPackageManagerField' in main_source:
                    managers = re.findall(r"['\"](npm|yarn|pnpm)['\"]", match.group(1))
                    if managers:
                        capability['auto_managers'] = sorted(set(managers))
                        capability['auto_selector'] = 'top_level_package_manager'
                elif (re.search(r"return\s+['\"]npm['\"]", main_source)
                      and 'getNameFromPackageManagerField' in main_source
                      and 'npmRegex' in main_source):
                    capability['auto_managers'] = ['npm']
                    capability['auto_selector'] = 'npm_context_fields'
    else:
        raise ValueError('Unsupported official action')
    return capability


@lru_cache(maxsize=4)
def _read_registry(path, modified_ns):
    registry = json.loads(Path(path).read_text(encoding='utf-8'))
    if registry.get('schema_version') != 1 or not isinstance(registry.get('entries'), dict):
        raise ValueError('Invalid setup-cache registry schema')
    verified = {}
    for uses, entry in registry['entries'].items():
        if not isinstance(entry, dict) or not isinstance(entry.get('evidence'), dict):
            continue
        evidence = entry['evidence']
        try:
            derived = derive_cache_capability(uses.rsplit('@', 1)[0], evidence['action_yaml'], evidence.get('main_source'))
        except (KeyError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            continue
        if all(entry.get(key) == value for key, value in derived.items()):
            verified[uses] = entry
    return verified


def registry_setup_cache_capability(uses, registry_path=None):
    if not isinstance(uses, str) or '@' not in uses:
        return None
    action, ref = uses.rsplit('@', 1)
    if action not in {'actions/setup-go', 'actions/setup-node'} or not re.fullmatch(r'[0-9a-fA-F]{40}', ref):
        return None
    path = Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
    if not path.exists():
        return None
    try:
        entry = _read_registry(str(path.resolve()), path.stat().st_mtime_ns).get(f'{action}@{ref.lower()}')
    except (ValueError, OSError):
        return None  # A damaged registry does not establish any cache capability.
    if not isinstance(entry, dict) or entry.get('source_commit') != ref.lower():
        return None
    if not isinstance(entry.get('cache_supported'), bool):
        return None
    if action == 'actions/setup-go' and not isinstance(entry.get('default_enabled'), bool):
        return None
    if action == 'actions/setup-node' and 'auto_managers' not in entry:
        return None
    return {**entry, 'resolution': 'official_source_registry'}


_GO_DEFAULT_ON = (
    '4b73464bb391d4059bd26b0524d20df3927bd417',
    '4a3601121dd01d1626a1e23e37211e3254c1c06c',
    '44694675825211faa026b3c33043df3e48a5fa00',
    '4dc6199c7b1a012772edbd06daecab0f50c9053c',
    '40f1582b2485089dde7abd97c1529aa768e1baff',
    '7a3fe6cf4cb3a834922a1244abfce67bcef6a0c5',
    '0aaccfd150d50ccaeb58ebd88d36e91967a5f35b',
)
_NODE_NPM_AUTO = (
    '53b83947a5a98c8d113130e565377fae1a50d02f',
    '48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e',
    '6044e13b5dc448c55e2357c09f80417699197238',
)
_NODE_NO_AUTO = (
    '1a4442cacd436585916779262731d5b162bc6ec7',
    'cdca7365b2dadb8aad0a33bc7601856ffabcc48e',
)

PINNED_SETUP_CACHE_CAPABILITIES = {
    'actions/setup-go': {
        **{sha: {'cache_supported': True, 'default_enabled': True} for sha in _GO_DEFAULT_ON},
        'bfdd3570ce990073878bf10f6b2d79082de49492': {
            'cache_supported': False, 'default_enabled': False,
        },
    },
    'actions/setup-node': {
        **{sha: {'cache_supported': True, 'auto_managers': ('npm',)} for sha in _NODE_NPM_AUTO},
        **{sha: {'cache_supported': True, 'auto_managers': ()} for sha in _NODE_NO_AUTO},
        'a0853c24544627f65ddf259abe73b1d18a591444': {
            'cache_supported': True, 'auto_managers': ('npm', 'yarn', 'pnpm'),
        },
    },
}


def pinned_setup_cache_capability(uses):
    if not isinstance(uses, str) or '@' not in uses:
        return None
    action, ref = uses.rsplit('@', 1)
    registry = registry_setup_cache_capability(uses)
    if registry is not None:
        return registry
    capability = PINNED_SETUP_CACHE_CAPABILITIES.get(action.strip(), {}).get(ref.strip().lower())
    if capability is None:
        return registry_setup_cache_capability(uses)
    return {**capability, 'resolution': 'verified_sha_mapping',
            'explicit_managers': ['npm', 'yarn', 'pnpm'] if action.strip() == 'actions/setup-node' else [],
            'auto_selector': 'npm_context_fields' if capability.get('auto_managers') == ('npm',) else 'top_level_package_manager',
            'source': f'https://raw.githubusercontent.com/{action.strip()}/{ref.strip().lower()}/action.yml'}
