"""S8-only evidence helpers. Never execute scripts or assume package-script outputs."""
import re
import shlex

from src.detectors.smell.cache_consumption import CacheConsumptionAnalyzer, WORKSPACE


def _known_hosted_runner(job):
    """Persistent/unknown runners may already contain dependency files."""
    runner = job.runs_on
    values = [runner]
    if isinstance(runner, str):
        match = re.fullmatch(r'\$\{\{\s*matrix\.([\w-]+)\s*\}\}', runner)
        if match:
            matrix = job.matrix or (job.strategy or {}).get('matrix') or {}
            if not isinstance(matrix, dict):
                return False
            base = matrix.get(match[1], [])
            values = list(base) if isinstance(base, list) else [base]
            include = matrix.get('include', [])
            if not isinstance(include, list):
                return False
            values += [item[match[1]] for item in include
                       if isinstance(item, dict) and match[1] in item]
    pattern = r'(?:ubuntu-(?:latest|\d+\.\d+)(?:-arm)?|windows-(?:latest|\d+)(?:-arm)?|macos-(?:latest|\d+)(?:-large|-xlarge|-intel)?)'
    return bool(values) and all(isinstance(v, str) and re.fullmatch(pattern, v) for v in values)


def future_cache_inputs(job, index, fields, is_false):
    """Only unconditional, direct output references establish a forward dependency.

    Fallback/conditional expressions and duplicate/missing step IDs remain unknown.
    """
    ids = {}
    for position, step in enumerate(job.steps):
        if step.id:
            ids.setdefault(step.id, []).append(position)
    evidence = []
    for field, value in fields.items():
        for expression in re.findall(r'\$\{\{(.*?)\}\}', str(value or ''), re.S):
            match = re.fullmatch(r'\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*', expression)
            if not match:
                continue
            positions = ids.get(match[1], [])
            if len(positions) != 1 or positions[0] <= index:
                continue
            producer = job.steps[positions[0]]
            if not is_false(producer.if_):
                evidence.append({'input': field, 'step_id': match[1], 'output': match[2],
                                 'producer_step_index': positions[0]})
    return evidence


def setup_checkout_inputs(workflow, job, index, cache_info, is_false):
    """Enabled official setup caches needing files supplied by a later checkout."""
    action = str(cache_info.get('action') or '').split('@')[0]
    defaults = {'actions/setup-go': 'go.mod/go.sum',
                'actions/setup-node': 'package-lock.json/npm-shrinkwrap.json/yarn.lock/pnpm-lock.yaml',
                'actions/setup-python': 'requirements.txt/Pipfile.lock/poetry.lock'}
    if action not in defaults:
        return None
    if not _known_hosted_runner(job):
        return None
    setup = job.steps[index]
    if action == 'actions/setup-python':
        # The shared detector accepts older/dynamic Python cache inputs; S8 must
        # not infer file dependencies from an unverified cache capability.
        manager = setup.with_.get('cache')
        if (setup.uses_version not in {3, 4, 5, 6} or not isinstance(manager, str)
                or manager not in {'pip', 'pipenv', 'poetry'}):
            return None
    analyzer = CacheConsumptionAnalyzer(workflow, job)
    # Earlier checkout/download/script could already have supplied the files.
    for step in job.steps[:index]:
        if is_false(step.if_):
            continue
        if step.run or (step.uses and step.uses.split('@')[0] not in defaults):
            return None
    config = job.steps[index].with_ or {}
    dependency_path = config.get('cache-dependency-path')
    paths = []
    if dependency_path:
        paths, partial = analyzer.cache_paths(dependency_path, job.steps[index])
        if partial or not all(p.startswith(WORKSPACE + '/') for p in paths):
            return None
    for checkout_index, step in enumerate(job.steps[index + 1:], index + 1):
        if str(step.uses or '').split('@')[0] != 'actions/checkout' or is_false(step.if_):
            continue
        checkout_path = analyzer.path((step.with_ or {}).get('path', '.'), analyzer.env)
        if not checkout_path:
            continue
        if paths:
            if not all(p.startswith(checkout_path.rstrip('/') + '/') for p in paths):
                continue
        elif checkout_path != WORKSPACE:
            continue
        return {'checkout_step_index': checkout_index,
                'dependency_files': dependency_path or defaults[action],
                'association': 'enabled_setup_cache_requires_repository_files'}
    return None


def explicit_file_outputs(workflow, job, step, index):
    """Recognize write destinations, not vague build names or default directories."""
    if not isinstance(step.run, str):
        return []
    analyzer = CacheConsumptionAnalyzer(workflow, job)
    env = {**analyzer.env, **(step.env or {})}
    cwd = analyzer.cwd(index, env)
    outputs = []
    for line in step.run.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if re.search(r'&&|\|\||[;|<>]|\$\(|`', line):
            if re.search(r'\bcd\b', line):
                cwd = None
            continue
        assignment = re.match(r'(?:export\s+)?(\w+)=', line)
        if assignment:
            env.pop(assignment[1], None)
            continue
        try:
            tokens = shlex.split(analyzer.expand(line, env), comments=True)
        except ValueError:
            continue
        if not tokens:
            continue
        command, args = tokens[0], tokens[1:]
        if command == 'cd':
            cwd = analyzer.path(args[0], env, cwd) if len(args) == 1 else None
            continue
        destination = None
        if command in {'cp', 'mv'}:
            if any(a in {'-t', '-S', '--suffix'} or a.startswith('--target-directory') for a in args):
                continue
            operands = [a for a in args if not a.startswith('-')]
            if len(operands) == 2:
                destination = operands[-1]
        elif command == 'go' and args[:1] == ['build']:
            destination = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == '-o'), None)
            destination = next((a[3:] for a in args if a.startswith('-o=')), destination)
        elif command == 'tar':
            # Narrow, unambiguous creation syntax: tar -czf archive.tar.gz inputs...
            if args and re.fullmatch(r'-[czjJvf]+', args[0]) and 'c' in args[0] and args[0].endswith('f') and len(args) > 1:
                destination = args[1]
        path = analyzer.path(destination, env, cwd) if destination else None
        if path and path != WORKSPACE and not analyzer.unresolved(path):
            outputs.append({'path': path, 'command': command, 'step_index': index,
                            'association': 'explicit_write_destination'})
    return outputs
