"""Conservative, job-local cache resource use analysis (no external source required)."""

import fnmatch
import posixpath
import re
import shlex
from typing import Any, Dict, List


CONSUMED = 'consumed'
UNKNOWN = 'unknown'
NO_CONSUMER = 'no_consumer'
WORKSPACE = '/__workspace__'
HOME = '/__home__'


def verdict(status: str, reason: str, **details) -> Dict[str, Any]:
    return {'status': status, 'reasons': [reason], **details}


class CacheConsumptionAnalyzer:
    """Resolve YAML paths and recognize reads; unmodeled execution stays unknown."""

    def __init__(self, workflow=None, job=None):
        self.workflow = workflow
        self.job = job
        self.env = {'GITHUB_WORKSPACE': WORKSPACE, 'HOME': HOME,
                    'USERPROFILE': HOME, 'GITHUB_ENV': '/__github_env__'}
        self.env.update(getattr(workflow, 'env', None) or {})
        self.env.update(getattr(job, 'env', None) or {})

    def expand(self, value: Any, env: Dict) -> str:
        text = str(value or '')
        for _ in range(12):
            previous = text
            text = re.sub(r'\$\{\{\s*github\.workspace\s*\}\}', WORKSPACE, text)
            text = re.sub(r'\$\{\{\s*env\.([\w]+)\s*\}\}',
                          lambda m: str(env.get(m[1], m[0])), text)
            text = re.sub(r'\$(?:env:([\w]+)|\{([\w]+)\}|([\w]+))|%([\w]+)%',
                          lambda m: str(env.get(next(g for g in m.groups() if g), m[0])), text)
            if text == previous:
                break
        return text

    @staticmethod
    def unresolved(text: str) -> bool:
        return bool(re.search(r'\$|%[\w]+%|`', text))

    def path(self, value: Any, env: Dict, cwd=WORKSPACE):
        text = self.expand(value, env).strip().strip('"\'').replace('\\', '/')
        if not text or self.unresolved(text) or text.startswith('!'):
            return None
        if text == '~' or text.startswith('~/'):
            text = HOME + text[1:]
        windows = bool(re.match(r'^[a-zA-Z]:/', text)) or text.startswith('//')
        if windows:
            text = text.casefold()
        elif not text.startswith('/'):
            if cwd is None:
                return None
            text = posixpath.join(cwd, text)
        if re.match(r'^[a-zA-Z]:/', text) or text.startswith('//'):
            text = text.casefold()
        return posixpath.normpath(text)

    def cache_paths(self, value: Any, step=None):
        env = {**self.env, **(getattr(step, 'env', None) or {})}
        entries = str(value or '').splitlines()
        resolved = [self.path(line, env) for line in entries if line.strip()]
        return [p for p in resolved if p is not None], any(p is None for p in resolved) or not resolved

    @staticmethod
    def overlaps(cache: str, candidate: str) -> bool:
        if not candidate:
            return False
        # Match components, never arbitrary substrings (dist != dist-other).
        if not any(c in cache + candidate for c in '*?['):
            return (candidate == cache or candidate.startswith(cache.rstrip('/') + '/')
                    or cache.startswith(candidate.rstrip('/') + '/'))
        def matches(value, pattern):
            patterns = {pattern}
            while any('/**/' in p for p in patterns):
                additions = {p.replace('/**/', '/', 1) for p in patterns if '/**/' in p}
                if additions.issubset(patterns):
                    break
                patterns.update(additions)
            return any(fnmatch.fnmatchcase(value, p) for p in patterns)

        if matches(candidate, cache) or matches(cache, candidate):
            return True
        # Check file ancestors when the glob describes a directory rather than files.
        for pattern, value in [(cache, candidate), (candidate, cache)]:
            parent = posixpath.dirname(value)
            while parent and parent != posixpath.dirname(parent):
                if matches(parent, pattern):
                    return True
                parent = posixpath.dirname(parent)
        # A directory input can contain a globbed resource. A common workspace
        # prefix alone does not connect unrelated globs such as *.chkpt and dist.
        for pattern, directory in [(cache, candidate), (candidate, cache)]:
            prefix = re.split(r'[*?\[]', pattern, maxsplit=1)[0].rstrip('/')
            if prefix and (prefix == directory or prefix.startswith(directory.rstrip('/') + '/')):
                return True
        return False

    def cwd(self, index, env):
        raw_jobs = (getattr(self.workflow, 'yaml_data', None) or {}).get('jobs', {})
        raw_job = raw_jobs.get(getattr(self.job, 'name', ''), {})
        defaults = (getattr(self.workflow, 'defaults', None) or {}).get('run', {})
        defaults = {**defaults, **((raw_job.get('defaults') or {}).get('run') or {})}
        steps = raw_job.get('steps') or []
        raw_step = steps[index] if index < len(steps) else {}
        return self.path(raw_step.get('working-directory', defaults.get('working-directory', WORKSPACE)), env)

    def step(self, step, paths: List[str], index=0) -> Dict[str, Any]:
        env = {**self.env, **(getattr(step, 'env', None) or {})}
        cwd = self.cwd(index, env)
        action = str(step.uses or '').split('@')[0]

        def linked(value, directory=cwd):
            return any(self.overlaps(p, self.path(value, env, directory)) for p in paths)

        if action:
            config = step.with_ or {}
            if action == 'actions/upload-artifact':
                values = self.expand(config.get('path', ''), env).splitlines()
                if any(v.startswith('!') for v in values):
                    return verdict(UNKNOWN, 'artifact_upload_exclusion_not_analyzed')
                if any(linked(v, WORKSPACE) for v in values if not v.startswith('!')):
                    return verdict(CONSUMED, 'artifact_upload_reads_cache_path')
                if not values or any(self.unresolved(v) for v in values):
                    return verdict(UNKNOWN, 'artifact_upload_path_unresolved')
                return verdict(NO_CONSUMER, 'artifact_upload_reads_other_paths')
            if action == 'docker/build-push-action':
                value = self.expand(config.get('cache-from', ''), env)
                sources = re.findall(r'(?:^|[,\s])src=([^,\s]+)', value)
                if any(linked(v, WORKSPACE) for v in sources):
                    return verdict(CONSUMED, 'docker_local_cache_input')
                return verdict(UNKNOWN, 'dockerfile_or_build_context_not_analyzed')
            if action in {'actions/cache', 'actions/cache/save', 'actions/cache/restore',
                          'actions/checkout', 'actions/download-artifact',
                          'actions/setup-node', 'actions/setup-python', 'actions/setup-java'}:
                return verdict(NO_CONSUMER, 'action_has_no_build_resource_read')
            # with/env may expose paths, but unknown action code does not prove a read.
            exposed = any(linked(v) for v in list(config.values()) + list((step.env or {}).values()))
            return verdict(UNKNOWN, 'action_source_unavailable', path_reference=exposed)

        run = step.run
        if not isinstance(run, str):
            return verdict(UNKNOWN, 'run_not_analyzable') if run else verdict(NO_CONSUMER, 'empty_step')
        unknown = []
        for line in run.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Compound shell grammar and command substitution require source-aware analysis.
            if re.search(r'\$\(|`|&&|\|\||[;|]|[<>]\(', line):
                unknown.append('compound_or_dynamic_shell')
                continue
            line = self.expand(line, env)
            try:
                # Preserve backslashes in Windows paths; normalize them during path resolution.
                tokens = [t.strip('"\'') for t in shlex.split(line, posix=False)]
            except ValueError:
                unknown.append('shell_parse_failed')
                continue
            if not tokens:
                continue
            command = tokens[0].lower()
            args = tokens[1:]
            if command == 'cd':
                cwd = self.path(args[0], env, cwd) if args else None
                if cwd is None:
                    unknown.append('working_directory_unresolved')
                continue
            if re.match(r'^(?:export\s+)?\w+=', line):
                unknown.append('shell_environment_assignment')
                continue
            if command in {'echo', 'printf', 'write-host', 'pwd', 'true', 'false', 'sleep'}:
                if self.unresolved(line) or '/__github_env__' in line:
                    unknown.append('dynamic_shell_or_environment')
                continue
            reads = []
            if command in {'cat', 'head', 'tail', 'wc', 'sha256sum', 'md5sum', 'cksum',
                           'get-content', 'get-filehash', 'source', '.'}:
                reads = [a for a in args if not a.startswith('-')]
            elif command in {'cp', 'mv', 'copy', 'copy-item', 'move-item'}:
                if any(a == '-t' or a.startswith('--target-directory') for a in args):
                    unknown.append('copy_target_option_not_analyzed')
                    continue
                operands = [a for a in args if not a.startswith('-')]
                reads = operands[:-1]
            elif command == 'java':
                reads = [args[i+1] for i,a in enumerate(args[:-1])
                         if a in {'-jar', '-cp', '-classpath', '--class-path'}]
                reads += [a for a in args if a.startswith('-Djava.library.path=')]
                reads += [env[k] for k in ('LD_LIBRARY_PATH', 'CLASSPATH') if k in env]
                reads += re.findall(r'-Djava.library.path=([^\s]+)', str(env.get('JAVA_TOOL_OPTIONS', '')))
            elif command == 'dotnet':
                reads = [a for a in args if a.lower().endswith('.dll')]
            elif command in {'tar', 'zip', 'unzip'}:
                if command == 'zip':
                    reads = [a for a in args if not a.startswith('-')][1:]
                elif command == 'unzip':
                    reads = args[:1]
                else:
                    if '-C' in args or any(a.startswith('--directory') for a in args):
                        unknown.append('archive_working_directory_not_analyzed')
                        continue
                    creating = any('c' in a for a in args if a.startswith('-'))
                    reads = []
                    skip = False
                    for a in args:
                        if skip:
                            skip = False
                            continue
                        if a.startswith('-'):
                            skip = a == '-C' or (creating and 'f' in a)
                        else:
                            reads.append(a)
            elif command == 'docker':
                if args[:1] == ['load']:
                    reads = [args[i+1] for i,a in enumerate(args[:-1]) if a in {'-i', '--input'}]
                reads += re.findall(r'(?:-v\s+|--volume[=\s]+)([^:\s]+):', line)
                reads += re.findall(r'--mount[=\s]+[^\s]*?(?:src|source)=([^,\s]+)', line)
            elif command in {'ant', 'mvn', 'gradle', 'gradlew', './gradlew',
                             'msbuild', 'cmake', 'ninja', 'cargo', 'go'}:
                # Only explicit library/input paths establish a read; generic builds stay unknown.
                reads = [a for a in args if re.search(r'library.path=|^-I|^-L|classpath', a)]
            elif '/' in command or '\\' in command:
                reads = [tokens[0]]  # executing a cached binary is a resource read

            candidates = []
            for value in reads:
                value = str(value)
                if '=' in value:
                    value = value.split('=', 1)[1]
                if value.startswith(('-I', '-L')):
                    value = value[2:]
                candidates.append(value)
                # Unix library path lists (do not split a Windows drive letter).
                if ':' in value and not re.match(r'^[a-zA-Z]:[\\/]', value):
                    candidates.extend(value.split(':'))
            if any(linked(v, cwd) for v in candidates):
                return verdict(CONSUMED, 'command_reads_cache_path', command=command)
            # Finite set of modeled operations with statically unrelated operands.
            if command in {'cat', 'head', 'tail', 'wc', 'sha256sum', 'md5sum', 'cksum',
                           'get-content', 'get-filehash', 'cp', 'mv', 'copy', 'copy-item', 'move-item'}:
                if candidates and not self.unresolved(line) and cwd is not None:
                    continue
            if command in {'pip', 'pip3'} and args[:1] == ['install'] and not self.unresolved(line):
                if not any(linked(a, cwd) for a in args):
                    continue
            if command in {'ls', 'dir', 'mkdir', 'chmod'} and not self.unresolved(line):
                continue  # listing/permissions/creating directories is not payload consumption
            unknown.append('unmodeled_command_or_indirect_consumer')
        if unknown:
            return {'status': UNKNOWN, 'reasons': sorted(set(unknown))}
        return verdict(NO_CONSUMER, 'only_analyzable_unrelated_operations')

    def js_build_outputs(self, step, index=0) -> Dict[str, Any]:
        """Associate visible JS compiler/bundler output arguments with workspace paths.

        Package scripts and implicit/config-defined output locations remain unknown.
        """
        result = {'is_build': False, 'paths': [], 'uncertain': False,
                  'reason': 'no_js_build_command'}
        if not isinstance(step.run, str):
            return result
        env = {**self.env, **(step.env or {})}
        cwd = self.cwd(index, env)
        tools = {'vite', 'tsc', 'webpack', 'rollup', 'esbuild'}
        output_flags = {'vite': {'--outDir'}, 'tsc': {'--outDir', '--outFile'},
                        'webpack': {'--output-path'}, 'rollup': {'--dir', '--file', '-d', '-o'},
                        'esbuild': {'--outdir', '--outfile'}}
        for line in step.run.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            expanded = self.expand(line, env)
            try:
                tokens = [t.strip('"\'') for t in shlex.split(expanded, posix=False)]
            except ValueError:
                continue
            if not tokens:
                continue
            if tokens[0] == 'cd' and len(tokens) == 2:
                cwd = self.path(tokens[1], env, cwd)
                continue
            command = list(tokens)
            if command[0] in {'npx', 'yarn', 'pnpm', 'npm'}:
                command.pop(0)
                while command and command[0] in {'exec', 'dlx', '--'}:
                    command.pop(0)
            tool = command[0] if command else ''
            if tool not in tools or (tool == 'vite' and 'build' not in command[1:]):
                if re.search(r'^\s*(?:npm|yarn|pnpm)\b.*\bbuild(?:[:\w-]*)\b', line):
                    result.update(is_build=True, uncertain=True, reason='package_build_script_output_unknown')
                continue
            if '--version' in command or '--help' in command:
                continue
            result['is_build'] = True
            if re.search(r'&&|\|\||[;|]|\$\(|`', expanded):
                result.update(uncertain=True, reason='compound_build_command_not_analyzed')
                continue
            outputs = []
            for offset, token in enumerate(command[1:], 1):
                flag, separator, value = token.partition('=')
                if flag not in output_flags[tool]:
                    continue
                if not separator:
                    value = command[offset+1] if offset+1 < len(command) else ''
                outputs.append(self.path(value, env, cwd))
            if not outputs or any(path is None for path in outputs):
                result.update(uncertain=True, reason='build_output_not_resolved_from_yaml')
            result['paths'].extend(path for path in outputs if path is not None)
            if result['paths'] and not result['uncertain']:
                result['reason'] = 'explicit_js_output_argument'
        return result

    def summarize(self, cache_step_idx, cache_path, definitely_false) -> Dict[str, Any]:
        paths, partial = self.cache_paths(cache_path, self.job.steps[cache_step_idx])
        evidence = []
        for index in range(cache_step_idx + 1, len(self.job.steps)):
            step = self.job.steps[index]
            if definitely_false(step.if_):
                continue
            evidence.append({'step_index': index, **self.step(step, paths, index)})
        status = (CONSUMED if any(e['status'] == CONSUMED for e in evidence)
                  else UNKNOWN if partial or any(e['status'] == UNKNOWN for e in evidence)
                  else NO_CONSUMER)
        reasons = sorted({reason for e in evidence if e['status'] == status for reason in e['reasons']})
        if not evidence and not partial:
            reasons.append('no_reachable_followup_steps')
        if partial:
            reasons.append('cache_path_partially_unresolved')
        return {'status': status, 'reasons': reasons, 'resolved_paths': paths,
                'path_partially_unresolved': partial, 'steps': evidence}
