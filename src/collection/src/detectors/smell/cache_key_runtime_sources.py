"""Conservative S6 provenance for run identifiers and simple shell output writes.

Never execute shell. Unsupported expressions/control flow have no positive
evidence. Workflow-file hashing is a separate, existing detector policy.
"""
import re


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
RUN_CONTEXT = re.compile(r"github\.(run_id|run_number|run_attempt)", re.I)
SHELL_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
SHELL_BUILTINS = {
    'GITHUB_RUN_ID': ('github.run_id',),
    'GITHUB_RUN_NUMBER': ('github.run_number',),
    'GITHUB_RUN_ATTEMPT': ('github.run_attempt',),
    'RANDOM': ('shell.RANDOM',),
}


def _merge(*facts):
    return tuple(dict.fromkeys(item for fact in facts for item in fact))


def _without_comment(line):
    quote, escape = None, False
    for index, char in enumerate(line):
        if escape:
            escape = False
        elif char == '\\' and quote != "'":
            escape = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == '#' and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _generator(command):
    """Only recognize direct generation; historical dates and transformations fail."""
    command = command.strip()
    if command in {'uuidgen', 'uuidgen -r', 'uuidgen --random',
                   'cat /proc/sys/kernel/random/uuid', 'New-Guid',
                   '[guid]::NewGuid()', '[guid]::NewGuid().ToString()'}:
        return ('runtime.uuid',)
    if command in {'date -Iseconds', 'date --iso-8601=seconds'}:
        return ('runtime.timestamp',)
    match = re.fullmatch(
        r'''date\s+(?:(?:-u|--utc)\s+)?(?:'([^']*)'|"([^"]*)"|([^\s'";|&<>]+))''',
        command,
    )
    if match:
        # Seconds/epoch precision only; a calendar day is not a run identifier.
        fmt = next(part for part in match.groups() if part is not None)
        tokens = set(re.findall(r'%([A-Za-z])', fmt.replace('%%', '')))
        absolute_date = 'F' in tokens or {'Y', 'm', 'd'} <= tokens
        clock = 'T' in tokens or {'H', 'M', 'S'} <= tokens
        if fmt.startswith('+') and ('s' in tokens or (absolute_date and clock)):
            return ('runtime.timestamp',)
    if re.fullmatch(r'Get-Date\s+-UFormat\s+[\'\"]?%s[\'\"]?', command, re.I):
        return ('runtime.timestamp',)
    return ()


def _substitutions(text):
    """Find balanced $(...) regions, respecting quotes in their command."""
    regions = []
    start = 0
    while (start := text.find('$(', start)) >= 0:
        if start and text[start - 1] in {'\\', '`'}:
            start += 2
            continue
        depth, quote, escape, end = 1, None, False, start + 2
        while end < len(text) and depth:
            char = text[end]
            if escape:
                escape = False
            elif char == '\\' and quote != "'":
                escape = True
            elif quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            end += 1
        if depth:
            break
        regions.append((start, end, text[start + 2:end - 1]))
        start = end
    return regions


class RuntimeCacheKeySources:
    """Resolve only earlier outputs and env values visible at this cache step."""

    def __init__(self, workflow=None, job=None, *, additional_contexts=()):
        self.workflow = workflow
        self.job = job
        self.additional_contexts = frozenset(value.lower() for value in additional_contexts)
        self.outputs = {}
        self.runtime_env = {}
        self.env_unknown = False

    def _value(self, value, scope, seen=()):
        if not isinstance(value, str):
            return ()
        evidence = ()
        for match in EXPRESSION.finditer(value):
            expression = match[1].strip()
            if RUN_CONTEXT.fullmatch(expression) or expression.lower() in self.additional_contexts:
                fact = (expression.lower(),)
            elif (env := re.fullmatch(r'env\.([A-Za-z_]\w*)', expression, re.I)):
                name = env[1]
                if name in seen:
                    continue
                raw = scope.get(name)
                fact = raw if isinstance(raw, tuple) else self._value(raw, scope, (*seen, name))
                if fact:
                    fact = (*fact, f'env.{name}')
            elif (output := re.fullmatch(r'steps\.([\w-]+)\.outputs\.([\w-]+)', expression)):
                fact = self.outputs.get((output[1], output[2]), ())
                if fact:
                    fact = (*fact, expression)
            else:
                # Includes string literals, comparisons, fallbacks and branches.
                fact = ()
            evidence = _merge(evidence, fact)
        return evidence

    def _scope(self, step):
        scope = {}
        if not self.env_unknown:
            scope.update(getattr(self.workflow, 'env', None) or {})
            scope.update(getattr(self.job, 'env', None) or {})
        scope.update(self.runtime_env)
        scope.update(getattr(step, 'env', None) or {})
        return scope

    def _shell_value(self, value, scope, local, *, literal=False):
        if literal:
            return self._value(value, scope)  # GitHub still interpolates single quotes.
        evidence = ()
        text = value
        regions = _substitutions(value)
        for start, end, command in reversed(regions):
            evidence = _merge(evidence, _generator(command))
            # An unknown command's arguments are not its output provenance.
            text = text[:start] + text[end:]
        evidence = _merge(evidence, self._value(text, scope))
        text = EXPRESSION.sub('', text)
        for match in SHELL_REFERENCE.finditer(text):
            if match.start() and text[match.start() - 1] in {'\\', '`'}:
                continue
            name = match[1] or match[2]
            if name in local:
                fact = local[name]
            elif name in SHELL_BUILTINS:
                fact = SHELL_BUILTINS[name]
            elif name == 'GITHUB_WORKFLOW_SHA' and 'github.workflow_sha' in self.additional_contexts:
                fact = ('github.workflow_sha',)
            else:
                raw = scope.get(name)
                fact = raw if isinstance(raw, tuple) else self._value(raw, scope, (name,))
            evidence = _merge(evidence, fact)
        return evidence

    def _step(self, step, index, duplicate_ids):
        raw_condition = step.if_
        condition = str(raw_condition if raw_condition is not None else '').strip().removeprefix('${{').removesuffix('}}').strip().lower()
        if condition in {'false', '0'}:
            return  # A skipped step cannot overwrite env or produce outputs.
        run = step.run if isinstance(step.run, str) else ''
        if not run:
            # An opaque action is not visible evidence of an env overwrite.
            # Keep declared/prior values without inventing its internal outputs.
            return
        lines = [clean for line in run.splitlines()
                 if (clean := _without_comment(line).strip())]
        scope, local, output, env_writes = self._scope(step), {}, {}, {}
        unparsed_env_write = False
        ambiguous = condition not in {'', 'true', 'success()', 'always()'}
        # We do not pretend to execute branches, loops, functions or sourced code.
        ambiguous |= any(re.match(r'(?:if|elif|else|fi|for|while|until|case|function|source|\.)\s|(?:if|else|fi|done)\s*$|\w+\s*\(\)\s*\{', line)
                         for line in lines)
        for line in lines:
            ps_write = re.fullmatch(r'''(["'])(.*)\1\s*>>\s*\$env:(GITHUB_OUTPUT|GITHUB_ENV)''', line, re.I)
            if ps_write:
                line = f'echo {ps_write[1]}{ps_write[2]}{ps_write[1]} >> "${ps_write[3].upper()}"'
            control_text = EXPRESSION.sub('', line)
            for start, end, _ in reversed(_substitutions(control_text)):
                control_text = control_text[:start] + control_text[end:]
            control_text = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", '', control_text)
            if re.search(r'[;|&]', control_text):
                ambiguous = True
            assignment = re.fullmatch(r'(?:export\s+)?([A-Za-z_]\w*)=(.*)', line)
            if assignment:
                name, value = assignment.groups()
                # A bare assignment followed by a command only sets its child env.
                if re.search(r'\s', control_text.split('=', 1)[-1]):
                    ambiguous = True
                literal = len(value) >= 2 and value[0] == value[-1] == "'"
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                local[name] = self._shell_value(value, scope, local, literal=literal)
                continue
            write = re.fullmatch(
                r'echo\s+([\'\"])(.*)\1\s*>>\s*[\'\"]?\$(?:\{(GITHUB_OUTPUT|GITHUB_ENV)\}|(GITHUB_OUTPUT|GITHUB_ENV))[\'\"]?',
                line)
            if write:
                payload = re.fullmatch(r'([A-Za-z_][\w-]*)=(.*)', write[2])
                if payload:
                    fact = self._shell_value(payload[2], scope, local, literal=write[1] == "'")
                    target = output if (write[3] or write[4]) == 'GITHUB_OUTPUT' else env_writes
                    target[payload[1]] = () if ambiguous else (*fact, f'step[{index}]') if fact else ()
                    continue
            if 'GITHUB_OUTPUT' in line or 'GITHUB_ENV' in line:
                ambiguous = True  # Unparsed writes may override known values.
                unparsed_env_write |= 'GITHUB_ENV' in line
            # An arbitrary command may modify shell variables via eval/source.
            if re.search(r'\beval\b|\bexec\b|^\.\s|^(?:unset|read|declare|typeset|local)\b|^printf\s+-v\b', line):
                ambiguous = True
        if ambiguous:
            output = {name: () for name in output}
            if unparsed_env_write:
                self.env_unknown = True
                self.runtime_env.clear()
            # Known conditional writes only invalidate the named variables.
            env_writes = {name: () for name in env_writes}
        self.runtime_env.update(env_writes)
        if step.id and step.id not in duplicate_ids:
            self.outputs.update({(step.id, name): fact for name, fact in output.items()})

    def evidence(self, key, cache_index):
        steps = getattr(self.job, 'steps', [])
        ids = [step.id for step in steps if step.id]
        duplicates = {name for name in ids if ids.count(name) > 1}
        for index, step in enumerate(steps[:cache_index]):
            self._step(step, index, duplicates)
        step = steps[cache_index] if 0 <= cache_index < len(steps) else None
        return list(self._value(key, self._scope(step)))
