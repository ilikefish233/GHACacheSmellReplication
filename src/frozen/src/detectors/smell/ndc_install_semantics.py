"""Static install facts for NDC; never execute shell or inspect external projects."""
import posixpath
import re
import shlex

from .cache_consumption import CacheConsumptionAnalyzer


EXPRESSION = re.compile(r"\$\{\{(?:[^'}]|'(?:[^']|'')*'|}(?!}))*\}\}")


def protect_expressions(text):
    values = {}
    def replace(match):
        key = f'__NDC_EXPRESSION_{len(values)}__'
        while key in text or key in values:
            key += '_'
        values[key] = match[0]
        return key
    return EXPRESSION.sub(replace, text), values


def tokens(command):
    protected, expressions = protect_expressions(command)
    try:
        # Retain Windows separators for subsequent path normalization.
        lexer = shlex.shlex(protected, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        lexer.escape = ''
        result = list(lexer)
    except ValueError:
        return []
    for i, token in enumerate(result):
        for key, value in expressions.items():
            token = token.replace(key, value)
        result[i] = token
    return result


def install_fact(command):
    """Normalize known launchers/options; unknown syntax cannot justify skipping."""
    parts = tokens(command)
    while parts and (parts[0] in {'sudo', 'env', '&'} or re.fullmatch(r'\w+=.*', parts[0])):
        parts.pop(0)
    if parts and parts[0] in {'-E', '-H', '-n'}:
        return None  # Unsupported sudo flags: retain the existing conservative path.
    if not parts:
        return None
    executable = parts.pop(0).replace('\\', '/').rsplit('/', 1)[-1].lower()
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', executable):
        if parts[:2] != ['-m', 'pip']:
            return None
        parts = ['pip', *parts[2:]]
        executable = parts.pop(0)
    manager = re.sub(r'\.exe$', '', executable)
    manager = 'pip' if re.fullmatch(r'pip(?:3(?:\.\d+)?)?', manager) else manager
    if manager not in {'pip', 'npm', 'pnpm', 'yarn'}:
        return None
    # Accept only documented install flags; an unknown option may take a value.
    value_options = {'--cache-dir', '--index-url', '--extra-index-url', '-i', '--trusted-host',
                     '--timeout', '--retries', '--proxy', '--root-user-action', '--prefix',
                     '--target', '-t', '--root', '--python', '--config-settings'}
    flags = {'-U', '--upgrade', '--user', '--no-cache-dir', '--break-system-packages',
             '--no-deps', '--disable-pip-version-check', '--quiet', '-q', '--verbose', '-v',
             '--global', '-g', '--ignore-installed', '--pre', '--prefer-binary', '--no-input'}
    objects, requirements = [], []
    operation = None
    unknown = False
    i = 0
    while i < len(parts):
        token = parts[i]
        if operation is None and token in {'install', 'i', 'ci'}:
            operation = token
        elif token in {'-r', '--requirement'} or token.startswith('--requirement='):
            if '=' in token:
                requirements.append(token.split('=', 1)[1])
            elif i + 1 < len(parts):
                i += 1
                requirements.append(parts[i])
            else:
                unknown = True
        elif token.split('=', 1)[0] in value_options:
            if '=' not in token:
                i += 1
                unknown |= i >= len(parts)
        elif token in flags:
            pass
        elif token.startswith('-'):
            unknown = True
        elif operation:
            objects.append(token)
        else:
            unknown = True
        i += 1
    if operation is None:
        return None
    names = [re.split(r'[<>=!~\[]', obj, maxsplit=1)[0].lower() for obj in objects]
    return dict(manager=manager, objects=objects, names=names, requirements=requirements,
                unknown=unknown, global_install=bool({'-g', '--global'} & set(parts)),
                no_deps='--no-deps' in parts,
                redirected=any(p.split('=', 1)[0] in {'--target', '-t', '--prefix', '--root', '--user'} for p in parts))


def non_project_reason(fact):
    if not fact or fact['unknown'] or fact['requirements'] or not fact['objects']:
        return None
    if fact['manager'] == 'pip' and all(name in {'pip', 'poetry', 'pipenv', 'uv'} for name in fact['names']):
        return 'installer_maintenance_or_bootstrap'
    if fact['manager'] in {'npm', 'pnpm', 'yarn'} and fact['global_install'] and all(
            re.fullmatch(r'(?:pnpm|yarn|npm|corepack)(?:@[^\s]+)?', obj) for obj in fact['objects']):
        return 'package_manager_bootstrap'
    if fact['manager'] == 'pip' and fact['no_deps'] and all(
            not re.search(r'[:$`%]', obj) and obj.lower().endswith('.whl') for obj in fact['objects']):
        return 'local_wheel_without_dependency_resolution'
    return None


def install_context(detector, workflow, job, index, command, occurrence=0):
    analyzer = CacheConsumptionAnalyzer(workflow, job)
    step = job.steps[index]
    env = {**analyzer.env, **step.env}
    cwd = analyzer.cwd(index, env)
    target = None
    seen = 0
    protected, _ = protect_expressions(step.run)
    uncertain_control = bool(re.search(r'\|', protected))
    # State is step-local; do not infer it through scripts, subshells or branches.
    for previous in detector._shell_commands(step.run):
        if previous == command:
            if seen == occurrence:
                break
            seen += 1
        if re.search(r'\b(?:if|else|fi|for|while|case|function)\b|[()]', previous):
            cwd, target = None, None
            continue
        parts = tokens(previous)
        if parts == ['deactivate']:
            target = None
        if len(parts) == 2 and parts[0] in {'cd', 'Set-Location'}:
            cwd = analyzer.path(parts[1], env, cwd) if cwd else None
        activation = re.fullmatch(r'(?:source|\.)\s+[\"\']?(.+?)[/\\](?:bin[/\\]activate|Scripts[/\\]Activate\.ps1)[\"\']?', previous, re.I)
        if activation:
            target = analyzer.path(activation[1], env, cwd) if cwd else None
    command_parts = tokens(command)
    privileged = bool(command_parts and command_parts[0] == 'sudo')
    while command_parts and command_parts[0] in {'sudo', '&'}:
        command_parts.pop(0)
    if command_parts:
        executable = command_parts[0].replace('\\', '/')
        explicit = re.fullmatch(r'(.+)/(?:bin|Scripts)/(?:python(?:\d+(?:\.\d+)*)?|pip(?:3(?:\.\d+)*)?)(?:\.exe)?', executable, re.I)
        if explicit:
            target = analyzer.path(explicit[1], env, cwd) if cwd else None
        elif privileged or '/' in executable or executable in {'deactivate'}:
            target = None
    if uncertain_control:
        cwd, target = None, None
    return {'working_directory': cwd, 'environment_target': target,
            'original_step_run': step.run, 'execution_condition': step.if_,
            'fact': install_fact(command)}


def environment_cache_covers(detector, record, analysis, workflow, job):
    manager = record.get('manager')
    if manager not in {'pip', 'poetry'}:
        return False
    fact = record['context']['fact']
    if fact and fact['redirected']:
        return False
    analyzer = CacheConsumptionAnalyzer(workflow, job)
    index = record['index']
    env = {**analyzer.env, **job.steps[index].env}
    cwd = record['context']['working_directory']
    target = record['context']['environment_target'] if manager == 'pip' else None
    if manager == 'poetry':
        enabled = str(env.get('POETRY_VIRTUALENVS_IN_PROJECT', '')).lower() == 'true'
        for step in job.steps[:index]:
            if step.if_:
                continue
            if step.uses and step.uses.split('@')[0] == 'snok/install-poetry':
                enabled = str(step.with_.get('virtualenvs-in-project', '')).lower() == 'true'
                enabled &= str(step.with_.get('virtualenvs-create', True)).lower() != 'false'
            if step.run and not re.search(r'\b(?:if|else|fi|for|while|case|function)\b|\|', str(step.run)):
                for command in detector._shell_commands(step.run):
                    parts = tokens(command)
                    if parts[:3] == ['poetry', 'config', 'virtualenvs.in-project'] and parts[3:] == ['true']:
                        enabled = True
                    if (parts[:2] == ['poetry', 'config'] and len(parts) == 4
                            and parts[2] in {'virtualenvs.in-project', 'virtualenvs.create'} and parts[3] == 'false'):
                        enabled = False
        if str(env.get('POETRY_VIRTUALENVS_CREATE', '')).lower() == 'false':
            enabled = False
        target = analyzer.path('.venv', env, cwd) if enabled and cwd else None
    if not target:
        return False
    for cache in analysis.get('cache_steps', []):
        if (cache.get('setup_cache') or cache.get('operation') == 'save'
                or str(cache.get('lookup_only')).lower() == 'true'
                or detector._condition_is_definitely_false(cache.get('if'))):
            continue
        paths, _ = analyzer.cache_paths(cache.get('path'), job.steps[cache['index']])
        if any(line.strip().startswith('!') for line in str(cache.get('path', '')).splitlines()):
            continue  # Excluded environment files require more precise coverage evidence.
        if target in paths:
            return True
    return False
