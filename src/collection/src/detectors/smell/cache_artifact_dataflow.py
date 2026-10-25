"""S7-local static cache identities and explicit output evidence; never execute code."""
import copy
import hashlib
import json
import re

from src.detectors.smell.cache_consumption import CacheConsumptionAnalyzer
from src.detectors.smell.cache_position_dataflow import explicit_file_outputs


EXPRESSION = re.compile(r'\$\{\{\s*(.*?)\s*\}\}', re.S)
DEPENDENCY_RESOURCE = re.compile(
    r'(?:^|/)(?:node_modules|\.npm|\.yarn|\.pnpm-store|\.cargo|ccache|sccache|\.gradle|\.m2)(?:/|$)'
    r'|/\.cache/|/target/(?:debug|release)/(?:deps|incremental)(?:/|$)')


class ArtifactTransferResolver:
    def __init__(self, workflow, job, context, cache_index, is_false):
        self.workflow, self.job, self.context = workflow, job, context
        self.cache_index, self.is_false = cache_index, is_false
        self.analyzer = CacheConsumptionAnalyzer(workflow, job)
        self.raw_env = {**(workflow.env or {}), **(job.env or {})}
        # A shell-written value cannot inherit an earlier static YAML value.
        for step in job.steps[:cache_index]:
            if not is_false(step.if_) and isinstance(step.run, str):
                for name in re.findall(r'\b([A-Za-z_][\w-]*)=[^\n]*>>[^\n]*GITHUB_ENV', step.run):
                    self.raw_env[name] = None
                    # HEAD is comparable only with the same visible checkout ref.
                    short = re.search(re.escape(name) + r'=\$\(git rev-parse (--short(?:=\d+)?) HEAD\)', step.run)
                    if short and not step.if_:
                        source = self.checkout_source(job.steps.index(step))
                        if source is not None:
                            self.raw_env[name] = self.symbol('git-head', source, short[1])

    @staticmethod
    def symbol(kind, *source):
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()
        return '__s7_' + kind + '_' + digest + '__'

    def checkout_source(self, before):
        source = None
        for step in self.job.steps[:before]:
            if isinstance(step.run, str) and re.search(r'\bgit\s+(?:checkout|switch|reset|pull|merge|commit|cherry-pick)\b', step.run):
                source = None
            if str(step.uses or '').lower().startswith('actions/checkout@'):
                if step.if_:
                    source = None
                else:
                    source = (str(step.with_.get('repository', '${{github.repository}}')),
                              str(step.with_.get('ref', '${{github.sha}}')))
        return source

    def expand(self, value, step=None, seen=()):
        text = str(value or '')
        scope = {**self.raw_env, **((step.env or {}) if step else {})}

        def expression(match):
            ref = match[1].strip()
            if ref.startswith('env.'):
                name = ref[4:]
                if name in seen or scope.get(name) is None:
                    return match[0]
                return self.expand(scope[name], step, (*seen, name))
            if ref.startswith('matrix.'):
                result = self.context
                for name in ref[7:].split('.'):
                    result = result.get(name) if isinstance(result, dict) else None
                return str(result) if isinstance(result, (str, int, float, bool)) else match[0]
            if ref == 'github.workspace':
                return '/__workspace__'
            if ref == 'github.job':
                return self.job.name
            if ref == 'runner.os':
                runner = self.expand(self.job.runs_on) if isinstance(self.job.runs_on, str) else ''
                for prefix, platform in [('ubuntu-', 'Linux'), ('macos-', 'macOS'), ('windows-', 'Windows')]:
                    if runner.startswith(prefix):
                        return platform
            if ref.startswith('steps.'):
                found = re.fullmatch(r'steps\.([\w-]+)\.outputs\.([\w-]+)', ref)
                if found:
                    return self.output(self.job, *found.groups(), self.cache_index, seen) or match[0]
            if ref.startswith('needs.'):
                found = re.fullmatch(r'needs\.([\w-]+)\.outputs\.([\w-]+)', ref)
                if found and found[1] in self.workflow.jobs:
                    upstream = self.workflow.jobs[found[1]]
                    body = (self.workflow.yaml_data.get('jobs') or {}).get(found[1], {})
                    output = (body.get('outputs') or {}).get(found[2])
                    direct = re.fullmatch(r'\s*\$\{\{\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}\s*', str(output or ''))
                    if direct:
                        return self.output(upstream, *direct.groups(), len(upstream.steps), seen) or match[0]
            return match[0]

        for _ in range(12):
            updated = EXPRESSION.sub(expression, text)
            if updated == text:
                break
            text = updated
        return text

    def output(self, job, step_id, name, before, seen):
        token = (job.name, step_id, name)
        if token in seen:
            return None
        if job.name != self.job.name and job.matrix:
            return None  # Do not resolve an upstream matrix with a consumer instance.
        matches = [(i, s) for i, s in enumerate(job.steps) if s.id == step_id]
        if len(matches) != 1:
            return None
        i, step = matches[0]
        if i >= before or step.if_ or not isinstance(step.run, str):
            return None
        # A source token represents one output, not an arbitrary matching value.
        writes = re.findall(r'''(?m)^\s*echo\s+["']?''' + re.escape(name)
                            + r'''=([^\n]*?)\s*>>\s*["']?\$GITHUB_OUTPUT["']?\s*$''', step.run)
        resolver = ArtifactTransferResolver(self.workflow, job, self.context, i, self.is_false)
        if not writes:
            return None
        unknown = self.symbol('output', job.name, step_id, name,
                              self.context if job.name == self.job.name else {})
        if len(writes) != 1 or re.search(r'\$\(|`|[;|]', writes[0]):
            return unknown
        value = resolver.expand(writes[0].rstrip().rstrip('"\''), step, (*seen, token))
        variable = re.fullmatch(r'\$([A-Za-z_]\w*)|\$\{([A-Za-z_]\w*)\}', value)
        if variable:
            name = next(x for x in variable.groups() if x)
            # Identical make recipes in the same checkout/scope are static
            # provenance evidence; never equate merely equal shell variable names.
            recipes = re.findall(r'(?m)^\s*' + re.escape(name)
                                 + r'=(?:`(make\s+[\w./-]+)`|\$\((make\s+[\w./-]+)\))\s*$', step.run)
            source = resolver.checkout_source(i)
            if len(recipes) == 1 and source is not None:
                command = next(x for x in recipes[0] if x)
                return self.symbol('recipe', source, command, resolver.raw_env,
                                   step.env or {}, resolver.analyzer.cwd(i, resolver.analyzer.env))
            return unknown
        if re.search(r'\$\{\{\s*(?:env|matrix|steps|needs)\.', value):
            return unknown
        if re.search(r'\$|`', value) and '${{' not in value:
            return unknown
        return value

    def paths(self, value, step):
        expanded = self.expand(value, step)
        # Workflow-scoped values may remain symbolic, with exact identity; they
        # are never replaced by a glob and never equated to a literal filename.
        expanded = EXPRESSION.sub(
            lambda m: self.symbol('workflow', m[1].strip())
            if m[1].strip() in {'github.sha', 'github.run_id', 'github.run_number',
                                'github.ref', 'github.ref_name', 'github.run_attempt'}
            else m[0], expanded)
        env = {k: v for k, v in self.analyzer.env.items() if k not in self.raw_env}
        env.update({k: self.expand(v, step) for k, v in self.raw_env.items() if v is not None})
        env.update(step.env or {})
        return [path for line in expanded.splitlines() if line.strip() and not line.strip().startswith('!')
                if (path := self.analyzer.path(line, env)) is not None]

    def snapshot_evidence(self, paths, step, operation):
        """Recognize nonstandard snapshots without calling ordinary deps artifacts."""
        if not paths or any(DEPENDENCY_RESOURCE.search(p) for p in paths):
            return []
        evidence = []
        raw_key = self.expand(step.with_.get('key'), step)
        run_bound = bool(re.search(r'\$\{\{\s*github\.run_(?:id|number)\s*\}\}', raw_key))
        # A whole prepared environment/state saved explicitly for this run and
        # restored by a dependent job is a snapshot, irrespective of directory name.
        historical_restore = any(
            'cache' in str(previous.uses or '')
            and '/save@' not in str(previous.uses or '')
            and previous.with_.get('restore-keys')
            and any(self.analyzer.overlaps(left, right) for left in paths
                    for right in self.paths(previous.with_.get('path'), previous))
            for previous in self.job.steps[:self.cache_index]
            if not self.is_false(previous.if_))
        if run_bound and operation == 'save' and not historical_restore:
            evidence.append('run_bound_explicit_snapshot_save')
        # Combined cache restores first but saves at post-job, after visible writes.
        limit = len(self.job.steps) if operation == 'restore_and_save' else self.cache_index
        archive_extraction = False
        cached_read = False
        for i, original in enumerate(self.job.steps[:limit]):
            if self.is_false(original.if_):
                continue
            current = copy.copy(original)
            current.run = self.expand(original.run, original) if isinstance(original.run, str) else None
            current.env = {k: self.expand(v, original) for k, v in (original.env or {}).items()}
            shell = self.analyzer.expand(current.run, {**self.analyzer.env, **current.env})
            archive_extraction |= bool(re.search(r'\btar\s+(?:-[\w-]+\s+)*[\w-]*x[\w-]*\s|\bunzip\s', shell))
            for argument in re.findall(r'\bcat\s+([^\s;`|]+)', shell):
                path = self.analyzer.path(argument, self.analyzer.env,
                                          self.analyzer.cwd(i, self.analyzer.env))
                cached_read |= bool(path and any(self.analyzer.overlaps(p, path) for p in paths))
            commands = shell + '\n' + str(current.with_.get('command') or '')
            if any(p.endswith('/.venv') for p in paths) and re.search(r'\b(?:uv\s+sync|python\S*\s+-m\s+venv|poetry\s+install)\b', commands):
                evidence.append('prepared_virtual_environment')
            # Gradle's documented build directory is a concrete output relation,
            # unlike treating every directory named "build" as an artifact.
            if re.search(r'(?m)^\s*(?:\./)?gradle[w]?(?:\s+[^\n]*)?\s(?:compileJava|assemble|build|jar|war)\b', shell):
                default_output = self.analyzer.path('build', self.analyzer.env,
                                                    self.analyzer.cwd(i, self.analyzer.env))
                if (default_output and any(self.analyzer.overlaps(p, default_output) for p in paths)
                        and not re.search(r'\b(?:buildDir|buildDirectory|--project-dir|-p)\b', shell)):
                    evidence.append('standard_build_output')
            for output in explicit_file_outputs(self.workflow, self.job, current, i):
                if any(self.analyzer.overlaps(p, output['path']) for p in paths):
                    evidence.append('explicit_snapshot_write')
            # Literal redirection destination, including JSON transformations.
            for line in (current.run or '').splitlines():
                if line.lstrip().startswith('#'):
                    continue
                for dest in re.findall(r'''(?<![<>])>(?![>&])\s*(["']?[^\s;|&<>]+["']?)\s*$''', line):
                    path = self.analyzer.path(dest, {**self.analyzer.env, **current.env},
                                              self.analyzer.cwd(i, self.analyzer.env))
                    if path and any(self.analyzer.overlaps(p, path) for p in paths):
                        evidence.append('explicit_snapshot_write')
        if run_bound and archive_extraction and cached_read:
            evidence.append('extracted_snapshot_with_visible_files')
        return sorted(set(evidence))
