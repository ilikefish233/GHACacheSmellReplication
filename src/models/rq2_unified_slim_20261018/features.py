"""Pre-outcome YAML controls for the experimental unified RQ2 model."""
import re
from collections import Counter

import yaml

LOADER = getattr(yaml, 'CBaseLoader', yaml.BaseLoader)
ROLE_RULES = {
    'test': r'\b(test\w*|e2e|specs?|integration)\b',
    'build': r'\b(build\w*|compil\w*|packag\w*)\b',
    'lint': r'\b(lint\w*|format\w*|typecheck\w*|static.analysis|code.quality)\b',
    'release': r'\b(releas\w*|publish\w*)\b',
    'deploy': r'\b(deploy\w*|delivery)\b',
}
ECOSYSTEMS = {
    'javascript': r'(?:actions/setup-node|pnpm/action-setup|\b(?:npm|pnpm|yarn|bun)\s+(?:ci|install|i|run|test|build)\b)',
    'python': r'(?:actions/setup-python|astral-sh/setup-uv|\b(?:pip\d?|pipenv|poetry|uv)\s+(?:install|sync|run|test)\b|\bpython\d?\s+-m\s+(?:pip|pytest))',
    'rust': r'(?:dtolnay/rust-toolchain|actions-rs/toolchain|\bcargo\s+(?:build|test|check|fetch|clippy)\b)',
    'java': r'(?:actions/setup-java|\b(?:mvnw?|gradlew?)\b)',
    'go': r'(?:actions/setup-go|\bgo\s+(?:build|test|mod|get|install)\b)',
    'dotnet': r'(?:actions/setup-dotnet|\bdotnet\s+(?:build|test|restore)\b)',
    'ruby': r'(?:ruby/setup-ruby|\b(?:bundle|gem)\s+(?:install|exec)\b)',
    'native': r'\b(?:cmake|meson|ninja|make)\b',
}


def runner_profiles(raw):
    os_values, hosting = set(), set()
    values = raw if isinstance(raw, list) else [raw]
    if isinstance(raw, dict):
        values = raw.get('labels', [])
        values = values if isinstance(values, list) else [values]
    joined = ' '.join(str(v).lower() for v in values if v is not None)
    for name, pattern in [('linux', r'ubuntu|\blinux\b'), ('windows', r'windows'), ('macos', r'macos|macosx')]:
        if re.search(pattern, joined):
            os_values.add(name)
    if not os_values or '${{' in joined:
        os_values.add('unknown')
    if 'self-hosted' in joined:
        hosting.add('self_hosted')
    elif all(re.fullmatch(r'(ubuntu|windows|macos)-[\w.\-]+', str(v).lower()) for v in values) and values:
        hosting.add('github_hosted')
    else:
        hosting.add('unknown')
    return os_values, hosting


def extract(text, workflow_path=''):
    data = yaml.load(text, Loader=LOADER)
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), dict):
        raise ValueError('No mapping jobs in YAML')
    jobs = data['jobs']
    steps_count, has_matrix, reusable = 0, False, False
    os_values, hosting, evidence = set(), set(), []
    roles_text = [str(data.get('name') or ''), workflow_path]
    for name, job in jobs.items():
        if not isinstance(job, dict):
            raise ValueError('Non-mapping job')
        roles_text.extend([str(name), str(job.get('name') or '')])
        reusable |= bool(job.get('uses'))
        strategy = job.get('strategy') or {}
        has_matrix |= isinstance(strategy, dict) and 'matrix' in strategy
        runner = job.get('runs-on')
        matrix = strategy.get('matrix') if isinstance(strategy, dict) else None
        if isinstance(runner,str) and isinstance(matrix,dict):
            match = re.fullmatch(r'\s*\$\{\{\s*matrix\.([\w-]+)\s*\}\}\s*',runner)
            if match and isinstance(matrix.get(match[1]),list):
                runner = matrix[match[1]]
        os_set, host_set = runner_profiles(runner)
        os_values.update(os_set); hosting.update(host_set)
        steps = job.get('steps') or []
        if not isinstance(steps, list):
            raise ValueError('Non-list steps')
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('Non-mapping step')
            uses = str(step.get('uses') or '').lower().split('@')[0]
            if uses not in {'actions/cache', 'actions/cache/restore', 'actions/cache/save'}:
                steps_count += 1
            evidence.extend([uses, str(step.get('run') or '')])
    role_text = re.sub(r'[_/\-.]', ' ', ' '.join(roles_text).lower())
    roles = [role for role, rule in ROLE_RULES.items() if re.search(rule, role_text)]
    command_text = '\n'.join(evidence).lower()
    ecosystems = [name for name, rule in ECOSYSTEMS.items() if re.search(rule, command_text)]
    def category(values):
        if not values:
            return 'unknown'
        return next(iter(values)) if len(values) == 1 else 'mixed'
    return {
        'job_count': len(jobs), 'non_cache_step_count': steps_count,
        'has_matrix': int(has_matrix),
        'workflow_role': roles[0] if len(roles) == 1 else ('mixed' if roles else 'other'),
        'ecosystem': ecosystems[0] if len(ecosystems) == 1 else ('multi' if ecosystems else ('unknown' if reusable else 'none_identified')),
        'runner_os': category(os_values), 'runner_hosting': category(hosting),
        'reusable_job_present': reusable,
    }
