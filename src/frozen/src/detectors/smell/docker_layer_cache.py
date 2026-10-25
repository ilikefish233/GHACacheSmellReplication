"""Per-build cache imports; a saved directory or export alone is not an import."""
import csv
import re
import shlex

from src.detectors.smell.cache_consumption import CacheConsumptionAnalyzer, WORKSPACE


class DockerLayerCacheAnalyzer:
    def __init__(self, job, analysis, split_commands, definitely_false, mutex, workflow=None):
        self.job = job
        self.analysis = analysis
        self.split_commands = split_commands
        self.definitely_false = definitely_false
        self.mutex = mutex
        self.paths = CacheConsumptionAnalyzer(workflow=workflow, job=job)

    def assess(self):
        results = []
        for index, step in enumerate(self.job.steps):
            if self.definitely_false(step.if_) or self.definitely_false(self.job.if_):
                continue
            action = str(step.uses or '').split('@')[0].lower()
            if action == 'docker/build-push-action':
                config = step.with_ or {}
                results.append(self.build(index, step, config.get('cache-from'), config.get('cache-to'),
                                          config.get('no-cache'), action=step.uses))
            elif action == 'docker/bake-action':
                # Bake target configuration lives in external HCL/Compose files.
                results.append({'index': index, 'action': step.uses, 'status': 'unknown',
                                'reason': 'external_bake_targets_not_available'})
            if not isinstance(step.run, str):
                continue
            for ordinal, command in enumerate(self.split_commands(step.run)):
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    if re.search(r'\b(?:docker|buildx)\s+(?:buildx\s+)?build\b', command):
                        results.append({'index': index, 'command_index': ordinal, 'run': command,
                                        'status': 'unknown', 'reason': 'shell_command_not_parseable'})
                    continue
                start = None
                if tokens and tokens[0].lower() in {'echo', 'printf', 'write-output', 'write-host'}:
                    continue
                for pos in range(len(tokens)):
                    if tokens[pos:pos+3] == ['docker', 'buildx', 'build']:
                        start = pos + 3
                        break
                    if tokens[pos:pos+2] in (['docker', 'build'], ['buildx', 'build']):
                        start = pos + 2
                        break
                if start is None:
                    if (tokens[:3] == ['docker', 'compose', 'build']
                            or tokens[:2] == ['docker-compose', 'build']):
                        results.append({'index': index, 'command_index': ordinal, 'run': command,
                                        'status': 'unknown', 'reason': 'external_compose_build_config_not_available'})
                    continue
                imports, exports, no_cache = [], [], None
                args = tokens[start:]
                for pos, token in enumerate(args):
                    for flag, values in [('--cache-from', imports), ('--cache-to', exports)]:
                        if token.startswith(flag + '='):
                            values.append(token[len(flag)+1:])
                        elif token == flag:
                            values.append(args[pos+1] if pos+1 < len(args) else '')
                    if token == '--no-cache':
                        no_cache = True
                    elif token.startswith('--no-cache='):
                        no_cache = token.split('=', 1)[1]
                if not imports and any(token.startswith('$') and self.paths.unresolved(token) for token in args):
                    results.append({'index': index, 'command_index': ordinal, 'run': command,
                                    'status': 'unknown', 'reason': 'shell_build_arguments_unresolved'})
                else:
                    results.append(self.build(index, step, imports, exports, no_cache,
                                              run=command, command_index=ordinal))
        return results

    def build(self, index, step, imports, exports, no_cache, **identity):
        base = {'index': index, **identity, 'cache_from': imports, 'cache_to': exports}
        if no_cache is True or str(no_cache).strip().lower() == 'true':
            return {**base, 'status': 'missing', 'reason': 'build_cache_explicitly_disabled'}
        if no_cache not in (None, '', False, 'false', 'FALSE'):
            return {**base, 'status': 'unknown', 'reason': 'no_cache_control_not_static'}
        values = imports if isinstance(imports, (list, tuple)) else str(imports or '').splitlines()
        values = [str(value).strip() for value in values if str(value).strip().lower() not in {'', 'false', 'none', 'null'}]
        if not values:
            return {**base, 'status': 'missing', 'reason': 'cache_import_not_configured'}
        states = [self.cache_input(value, index, step, cli='run' in identity) for value in values]
        status = 'configured' if any(s['status'] == 'configured' for s in states) else 'unknown' if any(s['status'] == 'unknown' for s in states) else 'missing'
        return {**base, 'status': status, 'reason': 'cache_import_assessed', 'inputs': states}

    def cache_input(self, value, index, step, cli=False):
        env = {**self.paths.env, **(step.env or {})}
        expanded = self.paths.expand(value, env)
        if self.paths.unresolved(expanded):
            return {'status': 'unknown', 'reason': 'cache_import_expression_unresolved', 'value': value}
        try:
            fields = next(csv.reader([expanded], skipinitialspace=True))
        except (csv.Error, StopIteration):
            return {'status': 'unknown', 'reason': 'cache_import_not_parseable', 'value': value}
        options = dict(field.strip().split('=', 1) for field in fields if '=' in field)
        backend = options.get('type', '').strip()
        if backend != 'local':
            if backend in {'gha', 'registry', 's3', 'azblob', 'inline'} or (not backend and re.fullmatch(r'[^\s,=]+', expanded)):
                return {'status': 'configured', 'reason': 'external_cache_import', 'value': value}
            return {'status': 'unknown', 'reason': 'cache_backend_unrecognized', 'value': value}
        source = options.get('src', '').strip()
        if not source:
            return {'status': 'missing', 'reason': 'local_cache_src_missing', 'value': value}
        cwd = self.paths.cwd(index, env) if cli else WORKSPACE
        resolved = self.paths.path(source, env, cwd)
        if resolved is None:
            return {'status': 'unknown', 'reason': 'local_cache_src_unresolved', 'value': value}
        unresolved_restore = False
        for restore in self.analysis.get('cache_steps', []):
            restore_index = restore.get('index', index)
            if (restore_index >= index or restore.get('setup_cache') or restore.get('operation') == 'save'
                    or restore.get('lookup_only') is True or str(restore.get('lookup_only', '')).lower() == 'true'
                    or self.definitely_false(restore.get('if')) or self.mutex(restore.get('if'), step.if_)):
                continue
            restore_step = self.job.steps[restore_index]
            raw = restore.get('path')
            entries = raw if isinstance(raw, (list, tuple)) else str(raw or '').splitlines()
            paths, unknown = self.paths.cache_paths('\n'.join(str(v) for v in entries), restore_step)
            unresolved_restore |= unknown
            for path in paths:
                if any(c in path for c in '*?['):
                    if self.paths.overlaps(path, resolved):
                        unresolved_restore = True
                elif resolved == path or resolved.startswith(path.rstrip('/') + '/'):
                    return {'status': 'configured', 'reason': 'local_cache_src_restored', 'src': resolved,
                            'restore_step_index': restore_index}
        return {'status': 'unknown' if unresolved_restore else 'missing',
                'reason': 'restore_path_unresolved' if unresolved_restore else 'local_cache_src_not_restored', 'src': resolved}
