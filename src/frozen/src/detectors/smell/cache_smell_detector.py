"""
缓存异味检测器（重构版）

检测 GitHub Actions 工作流中的缓存配置问题。
"""

import itertools
import json
import re
import shlex
from typing import Callable, Dict, List, Optional, Set, Any, Union
from pathlib import Path

from src.models.workflow import WorkflowModel
from src.detectors.smell.cache_consumption import (
    CacheConsumptionAnalyzer, CONSUMED, UNKNOWN, NO_CONSUMER,
)
from src.detectors.smell.setup_cache_capabilities import pinned_setup_cache_capability
from src.detectors.smell.docker_layer_cache import DockerLayerCacheAnalyzer
from src.detectors.smell.cache_action_deprecation import assess_cache_action_reference
from src.detectors.smell.cache_key_runtime_sources import RuntimeCacheKeySources
from src.detectors.smell.cache_key_dependency_downloads import workflow_sha_download_evidence
from src.detectors.smell.ndc_install_semantics import (
    protect_expressions, install_fact, non_project_reason, install_context,
    environment_cache_covers,
)
from src.detectors.smell.cache_position_dataflow import (
    future_cache_inputs, setup_checkout_inputs, explicit_file_outputs,
)
from src.detectors.smell.cache_artifact_dataflow import (
    ArtifactTransferResolver, EXPRESSION, DEPENDENCY_RESOURCE,
)
from src.detectors.base import (
    ReusableWorkflowDetector,
    DetectorConfig,
    SmellInstance,
    DetectorResult,
    SeverityLevel,
    create_detector_result,
)


class CacheSmellDetector(ReusableWorkflowDetector):
    """缓存异味检测器

    检测工作流中的缓存配置问题：

    1. no_dependency_cache: 存在依赖操作但缺少对应缓存
    2. no_restore_keys: 缺少 restore-keys 回退
    3. redundant_cache_update: 并行 Job 使用相同缓存 key
    4. cache_position_error: 缓存步骤位置错误
    5. conflicting_cache_sources: 不同缓存机制操作相同目录
    6. static_cache_key: 缓存 key 完全静态
    7. disposable_cache_key: 缓存 key 使用每次运行都会变化的标识
    8. no_docker_layer_cache: Docker 构建缺少层缓存
    9. incorrect_cache_hit_condition: 命中条件跳过必要的冷启动步骤
    10. unreachable_cache_write: 缓存保存条件不可达
    11. cache_as_artifact: 使用缓存跨 Job 传递确定性产物
    12. overly_broad_cache_path: 缓存路径范围过大
    13. cache_path_pollution: 缓存路径包含临时或运行时状态
    14. non_portable_cache_path: 跨平台复用时使用不可移植路径
    15. malformed_cache_path: 缓存路径存在确定的语法问题
    16. deprecated_cache_action: 使用已废弃的缓存 action 版本
    """

    NAME = "cache_smell"
    DESCRIPTION = "检测工作流中的缓存配置问题"
    SMELL_TYPES = {
        "no_dependency_cache",
        "no_restore_keys",
        "redundant_cache_update",
        "cache_position_error",
        "disposable_cache_key",
        "static_cache_key",
        "no_docker_layer_cache",
        "conflicting_cache_sources",
        "incorrect_cache_hit_condition",
        "unreachable_cache_write",
        "cache_as_artifact",
        "overly_broad_cache_path",
        "cache_path_pollution",
        "non_portable_cache_path",
        "malformed_cache_path",
        "deprecated_cache_action",
    }

    # 依赖检测模式 (pattern, language, default_paths, lang_keywords)
    # 重构策略：只支持高置信度的语言/生态系统
    # 覆盖约 33% 的工作流，检测模式清晰准确
    # lang_keywords 用于识别动态缓存路径（如 ${{ env.pythonLocation }}、${{ steps.pip-cache.outputs.dir }}）
    HIGH_CONFIDENCE_DEPENDENCY_PATTERNS = [
        # JavaScript/TypeScript - 19.3% 流行度
        (r'\bnpm\s+(ci|install|i)\b', 'npm', ['~/.npm', 'node_modules'], ['npm']),
        (r'\byarn\s+(install|ci)\b|(?m:^[ \t]*yarn[ \t]*(?:#[^\n]*)?$)', 'npm', ['~/.cache/yarn', 'node_modules'], ['yarn']),
        (r'\bpnpm\s+install\b', 'npm', ['~/.local/share/pnpm/store', 'node_modules'], ['pnpm']),
        # Python - 13.8% 流行度
        (r'\bpip3?\s+(install|compile)\b', 'python', ['~/.cache/pip', '~/.pip/cache'], ['pip', 'pythonlocation', 'pypoetry']),
        (r'\buv\s+(?:sync\b|pip\s+install\b)', 'python', ['~/.cache/uv'], ['uv']),
        (r'\bpipenv\s+install\b', 'python', ['~/.cache/pip', '~/.local/share/virtualenvs'], ['pipenv', 'virtualenv']),
        (r'\bpoetry\s+install\b', 'python', ['~/.cache/pypoetry', '~/.local/share/virtualenvs'], ['poetry', 'virtualenv']),
    ]

    # 保留旧变量名用于兼容
    DEPENDENCY_PATTERNS = HIGH_CONFIDENCE_DEPENDENCY_PATTERNS

    # Setup actions 配置
    # 只支持高置信度的语言：npm 和 python
    SETUP_ACTION_CACHE = {
        'actions/setup-node': {
            'langs': ['npm'],
            'cache_param': 'cache',
            'default_paths': {'npm': '~/.npm', 'yarn': '~/.cache/yarn', 'pnpm': '~/.local/share/pnpm/store'},
            'auto_cache_version': 5,  # Requires package.json; v5 supports npm/yarn/pnpm, v6 npm only
        },
        'actions/setup-python': {
            'langs': ['python'],
            'cache_param': 'cache',
            'default_paths': {'pip': '~/.cache/pip', 'pipenv': '~/.cache/pip', 'poetry': '~/.cache/pypoetry'},
            'default_cache_version': None,
        },
        'astral-sh/setup-uv': {
            'langs': ['python'],
            'cache_param': 'enable-cache',
            'default_paths': {'uv': '~/.cache/uv'},
            'default_cache_version': None,
        },
    }

    # Setup actions 缓存行为配置（用于检测冲突）
    # 记录每个 setup-action 的默认缓存行为和缓存目录
    SETUP_CACHE_ACTIONS = {
        'actions/setup-go': {
            'default_enabled': True,  # v4 起默认启用；v3 需显式开启，v2 不支持
            'cache_dirs': ['~/go/pkg/mod', '~/.cache/go-build', '~/.cache/go'],
            'disable_flag': 'cache',
            'version_threshold': 4,
            'cache_support_version': 3,
        },
        'actions/setup-node': {
            'default_enabled': False,  # 需要显式启用
            'cache_dirs': ['~/.npm', '~/.cache/yarn', '~/.local/share/pnpm/store', 'node_modules'],
            'enable_flag': 'cache',
            'version_threshold': None,
        },
        'actions/setup-python': {
            'default_enabled': False,  # 需要显式启用
            'cache_dirs': ['~/.cache/pip', '~/.cache/pipenv', '~/.cache/pypoetry', '~/.local/share/virtualenvs'],
            'enable_flag': 'cache',
            'version_threshold': None,
        },
        'astral-sh/setup-uv': {
            'default_enabled': False,  # 需要显式启用 enable-cache
            'cache_dirs': ['~/.cache/uv'],
            'enable_flag': 'enable-cache',
            'version_threshold': None,
        },
        'actions/setup-java': {
            'default_enabled': False,  # 需要显式启用
            'cache_dirs': ['~/.cache/gradle', '~/.cache/maven', '~/.gradle/caches', '~/.m2/repository'],
            'enable_flag': 'cache',
            'version_threshold': None,
        },
        'ruby/setup-ruby': {
            'default_enabled': False,  # 需要显式启用
            'cache_dirs': ['~/.bundle', '~/.gem'],
            'enable_flag': 'bundler-cache',
            'version_threshold': None,
        },
    }

    CACHE_ACTIONS = {'actions/cache', 'actions/restore', 'actions/save'}

    # 非依赖缓存路径模式（工具/环境/构建产物缓存）
    NON_DEPENDENCY_CACHE_PATTERNS = [
        # 工具/环境缓存
        r'\.android',           # Android SDK/AVD
        r'android-sdk',         # Android SDK
        r'avd',                 # Android Virtual Device
        r'\.cache/puppeteer',   # Puppeteer
        r'\.cache/electron',    # Electron
        r'\.gradle/wrapper',    # Gradle wrapper（不是依赖缓存）

        # 源代码路径
        r'/src/',               # 源代码目录
        r'/packages/',          # packages 目录（monorepo 源代码）
        r'\.dart_tool',         # Dart 构建产物

        # 构建产物/输出目录
        r'/build$',             # build 目录（行尾）
        r'/dist$',              # dist 目录（行尾）
        r'/out$',               # out 目录（行尾）
        r'/target$',            # Maven target（部分是构建产物）
        r'/DerivedData',        # Xcode 构建产物

        # iOS/macOS 特定
        r'/Pods',               # CocoaPods（可能是依赖也可能是构建产物）
        r'\.xcodebuild',        # Xcode 构建
        r'/build\.log',         # 构建日志
    ]

    # 构建产物路径模式（更明确）
    BUILD_OUTPUT_PATTERNS = [
        r'/build\b',            # build 目录
        r'/dist\b',             # dist 目录
        r'/out\b',              # out 目录
        r'/output\b',           # output 目录
        r'\.dart_tool',         # Dart 构建产物
        r'/DerivedData',        # Xcode 构建产物
    ]

    # 预热缓存工作流名称特征
    WARM_CACHE_KEYWORDS = {
        'warm', 'build', 'cache', 'generate', 'prebuild', 'seed',
        'populate', 'prime', 'prepare', 'setup-cache', 'create-cache'
    }

    # 只读缓存工作流名称特征
    READONLY_KEYWORDS = {
        'test', 'check', 'lint', 'verify', 'report', 'e2e', 'integration'
    }

    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        workflows_dir: Optional[Path] = None,
        reusable_map: Optional[Dict[str, Dict[str, str]]] = None,
        setup_node_package_json_provider: Optional[Callable] = None,
    ):
        """初始化"""
        super().__init__(config, workflows_dir, reusable_map)
        # Optional step-scoped source evidence; never read the agent's local/latest package.json.
        # Provider(workflow, job_name, step_index) must return package_json, source_commit,
        # and step_context_verified=True, or None when same-version evidence is unavailable.
        self.setup_node_package_json_provider = setup_node_package_json_provider
        # (compiled_pattern, lang, default_paths, lang_keywords)
        self._compiled_patterns = [
            (re.compile(p, re.IGNORECASE), lang, paths, keywords)
            for p, lang, paths, keywords in self.DEPENDENCY_PATTERNS
        ]
        # 编译非依赖缓存路径模式
        self._non_dep_cache_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.NON_DEPENDENCY_CACHE_PATTERNS
        ]
        self._build_output_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.BUILD_OUTPUT_PATTERNS
        ]

    def detect(self, workflow: WorkflowModel) -> DetectorResult:
        """检测缓存异味"""
        if not workflow.has_jobs:
            return create_detector_result(
                detector_name=self.NAME,
                smells=[],
                error='工作流中没有 jobs 定义'
            )

        smells = []
        jobs_analysis = {}

        # 分析工作流级别的特殊模式
        workflow_mode = self._analyze_workflow_mode(workflow)

        for job_name, job in workflow.jobs.items():
            job_smells, analysis = self._analyze_job(job_name, job, workflow, workflow_mode)
            smells.extend(job_smells)
            jobs_analysis[job_name] = {
                'display_name': job.display_name,
                'smells': [s.to_dict() for s in job_smells],
                'analysis': analysis,
            }

        # 检测冗余缓存更新
        redundant_smells = self._detect_redundant_cache_update(workflow)
        smells.extend(redundant_smells)
        smells.extend(self._detect_cache_as_artifact(workflow, jobs_analysis))

        consumer_counts = {CONSUMED: 0, UNKNOWN: 0, NO_CONSUMER: 0}
        setup_cache_activation_counts = {'enabled': 0, 'disabled': 0, 'unknown': 0}
        docker_cache_counts = {'configured': 0, 'missing': 0, 'unknown': 0}
        deprecated_cache_reference_counts = {'deprecated': 0, 'not_deprecated': 0, 'unknown': 0}
        for job_result in jobs_analysis.values():
            for assessment in job_result['analysis'].get('cache_consumer_assessments', []):
                consumer_counts[assessment['status']] += 1
            for assessment in job_result['analysis'].get('setup_cache_assessments', []):
                setup_cache_activation_counts[assessment['status']] += 1
            for status, count in job_result['analysis'].get('docker_cache_counts', {}).items():
                docker_cache_counts[status] += count
            for assessment in job_result['analysis'].get('cache_action_reference_assessments', []):
                deprecated_cache_reference_counts[assessment['status']] += 1

        return create_detector_result(
            detector_name=self.NAME,
            smells=self.filter_smells(smells),
            metadata={
                'jobs': jobs_analysis,
                'workflow_mode': workflow_mode,
                'cache_consumer_counts': consumer_counts,
                'setup_cache_activation_counts': setup_cache_activation_counts,
                'docker_cache_counts': docker_cache_counts,
                'deprecated_cache_reference_counts': deprecated_cache_reference_counts,
            }
        )

    def _analyze_workflow_mode(self, workflow: WorkflowModel) -> Dict[str, Any]:
        """分析工作流级别的缓存模式

        Returns:
            Dict包含:
            - is_warm_cache: 是否是预热缓存工作流
            - is_readonly_cache: 是否是只读缓存工作流（只有restore没有save）
            - has_restore: 是否存在restore操作
            - has_save: 是否存在save操作
            - has_cache_only_read: 是否有lookup-only缓存操作
        """
        has_restore = False
        has_save = False
        has_cache_only_read = False

        # 统计Job的步骤类型
        cache_job_count = 0
        build_job_count = 0
        test_job_count = 0

        for job_name, job in workflow.jobs.items():
            has_cache_step = False
            has_build_step = False
            has_test_step = False

            for step in job.steps:
                if not step.uses:
                    continue

                if 'actions/restore' in step.uses:
                    has_restore = True
                    # 检查是否是lookup-only模式
                    if step.with_.get('lookup-only'):
                        has_cache_only_read = True

                if 'actions/save' in step.uses:
                    has_save = True
                    has_cache_step = True

                # actions/cache 可能同时做 restore 和 save
                if 'actions/cache' in step.uses:
                    has_restore = True
                    has_save = True
                    has_cache_step = True
                    # 检查 lookup-only 参数
                    if step.with_.get('lookup-only'):
                        has_cache_only_read = True

                # 检测构建/测试步骤（通过action名称）
                if any(keyword in step.uses.lower() for keyword in ['build', 'compile', 'test', 'check', 'lint']):
                    if 'test' in step.uses.lower() or 'check' in step.uses.lower():
                        has_test_step = True
                    else:
                        has_build_step = True

            # 统计Job类型
            if has_cache_step:
                cache_job_count += 1
            if has_build_step:
                build_job_count += 1
            if has_test_step:
                test_job_count += 1

        # 检查工作流名称特征
        name_lower = workflow.name.lower() if workflow.name else ''

        # 预热缓存工作流检测：
        # 1. 工作流名称包含 warm/rebuild/cache 等关键字
        # 2. 或者只有缓存相关的Job，没有构建/测试Job
        warm_cache_keywords = {'warm', 'rebuild', 'seed', 'populate', 'prime', 'preload', 'pre-build'}
        is_warm_cache = (
            # 名称特征检测
            any(kw in name_lower for kw in warm_cache_keywords) or
            # 或只有缓存Job，没有构建/测试Job
            (cache_job_count > 0 and build_job_count == 0 and test_job_count == 0)
        )

        # 只读缓存：只有restore没有save
        is_readonly_cache = has_restore and not has_save

        return {
            'is_warm_cache': is_warm_cache,
            'is_readonly_cache': is_readonly_cache,
            'has_restore': has_restore,
            'has_save': has_save,
            'has_cache_only_read': has_cache_only_read,
        }

    def _are_jobs_mutex(self, job1: Any, job2: Any) -> bool:
        """检查两个Job是否互斥（不会同时执行）

        通过分析 if 条件判断互斥性：
        1. 两个Job的if条件相反（如一个需要某个tag，一个排除）
        2. 一个Job的if条件排除了另一个运行的环境

        Returns:
            True 如果两个Job互斥，False 否则
        """
        if not job1.if_ or not job2.if_:
            return False
        return self._conditions_are_mutex(job1.if_, job2.if_)

    def _strip_outer_parentheses(self, condition: str) -> str:
        """去除不会改变语义的最外层括号。"""
        value = condition.strip()
        while value.startswith('(') and value.endswith(')'):
            depth = 0
            quote = None
            enclosed = True
            for index, char in enumerate(value):
                if quote:
                    if char == quote and (index == 0 or value[index - 1] != '\\'):
                        quote = None
                    continue
                if char in {'\'', '"'}:
                    quote = char
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0 and index != len(value) - 1:
                        enclosed = False
                        break
            if not enclosed or depth != 0:
                break
            value = value[1:-1].strip()
        return value

    def _split_condition(self, condition: str, operator: str) -> List[str]:
        """在括号和字符串之外拆分 GitHub Actions 条件。"""
        parts = []
        start = 0
        depth = 0
        quote = None
        index = 0
        while index < len(condition):
            char = condition[index]
            if quote:
                if char == quote and (index == 0 or condition[index - 1] != '\\'):
                    quote = None
                index += 1
                continue
            if char in {'\'', '"'}:
                quote = char
                index += 1
                continue
            if char == '(':
                depth += 1
                index += 1
                continue
            if char == ')':
                depth = max(0, depth - 1)
                index += 1
                continue
            if depth == 0 and condition.startswith(operator, index):
                parts.append(condition[start:index].strip())
                start = index + len(operator)
                index = start
                continue
            index += 1
        parts.append(condition[start:].strip())
        return [part for part in parts if part]

    def _normalize_condition(self, condition: Any) -> str:
        """标准化 if 条件，去除 YAML 常见的表达式包裹。"""
        if condition is None:
            return ''
        value = str(condition).strip()
        if value.startswith('${{') and value.endswith('}}'):
            value = value[3:-2].strip()
        return self._strip_outer_parentheses(value)

    def _evaluate_matrix_condition(
        self,
        condition: Any,
        matrix_context: Dict[str, Any],
    ) -> Optional[bool]:
        """在给定 matrix 上下文中保守求值步骤/Job 条件。"""
        normalized = self._normalize_condition(condition)
        if not normalized:
            return True

        or_parts = self._split_condition(normalized, '||')
        if len(or_parts) > 1:
            states = [self._evaluate_matrix_condition(part, matrix_context) for part in or_parts]
            if any(state is True for state in states):
                return True
            if all(state is False for state in states):
                return False
            return None

        and_parts = self._split_condition(normalized, '&&')
        if len(and_parts) > 1:
            states = [self._evaluate_matrix_condition(part, matrix_context) for part in and_parts]
            if any(state is False for state in states):
                return False
            if all(state is True for state in states):
                return True
            return None

        atomic = self._strip_outer_parentheses(normalized)
        if atomic.lower() in {'true', 'always()', 'success()'}:
            return True
        if atomic.lower() in {'false'}:
            return False
        if atomic.lower() in {'failure()', 'cancelled()'}:
            return None

        if atomic.startswith('!') and not atomic.startswith('!='):
            nested = self._evaluate_matrix_condition(atomic[1:].strip(), matrix_context)
            return None if nested is None else not nested

        comparison = re.fullmatch(
            r"([A-Za-z_][\w.-]*(?:\.[A-Za-z_][\w.-]*)*)\s*(==|!=)\s*(.+)",
            atomic,
        )
        if not comparison:
            return None

        reference, operator, expected = comparison.groups()
        if not reference.lower().startswith('matrix.'):
            return None
        variable = reference.split('.', 1)[1]
        actual_value = self._matrix_context_value(matrix_context, variable)
        if actual_value is None:
            return None
        expected = expected.strip()
        if len(expected) >= 2 and expected[0] == expected[-1] and expected[0] in {'\'', '"'}:
            expected = expected[1:-1]
        actual = str(actual_value)
        matches = actual.lower() == expected.lower()
        return matches if operator == '==' else not matches

    def _parse_condition_clause(self, condition: str) -> Optional[List[tuple]]:
        """将一个 AND 条件转换成可比较的字面量约束。"""
        literals = []
        for part in self._split_condition(condition, '&&'):
            atomic = self._strip_outer_parentheses(part)
            lowered = atomic.lower()
            if lowered in {'true', 'always()', 'success()'}:
                continue
            if lowered == 'false':
                return []
            if lowered in {'failure()', 'cancelled()'}:
                return None

            negated = atomic.startswith('!') and not atomic.startswith('!=')
            if negated:
                atomic = atomic[1:].strip()

            comparison = re.fullmatch(
                r"([A-Za-z_][\w.-]*(?:\.[A-Za-z_][\w.-]*)*)\s*(==|!=)\s*(.+)",
                atomic,
            )
            if comparison:
                reference, operator, expected = comparison.groups()
                expected = expected.strip()
                if len(expected) >= 2 and expected[0] == expected[-1] and expected[0] in {'\'', '"'}:
                    expected = expected[1:-1]
                if negated:
                    operator = '!=' if operator == '==' else '=='
                literals.append(('comparison', reference.lower(), operator, expected.lower()))
                continue

            # 支持 startsWith(...) 与 !startsWith(...) 这类完全相反的条件。
            literals.append(('atom', atomic.lower(), not negated))
        return literals

    def _condition_clauses(self, condition: Any) -> Optional[List[List[tuple]]]:
        """将 OR/AND 条件转换为析取范式的近似表示。"""
        normalized = self._normalize_condition(condition)
        if not normalized:
            return [[]]
        clauses = []
        for part in self._split_condition(normalized, '||'):
            clause = self._parse_condition_clause(part)
            if clause is None:
                return None
            if clause == [] and self._strip_outer_parentheses(part).lower() == 'false':
                continue
            clauses.append(clause)
        return clauses

    def _clauses_are_compatible(self, left: List[tuple], right: List[tuple]) -> bool:
        """判断两个条件子句是否存在共同满足情况。"""
        equalities = {}
        inequalities = {}
        atoms = {}
        for literal in left + right:
            if literal[0] == 'atom':
                _, name, positive = literal
                if name in atoms and atoms[name] != positive:
                    return False
                atoms[name] = positive
                continue
            _, reference, operator, expected = literal
            if operator == '==':
                if reference in equalities and equalities[reference] != expected:
                    return False
                if expected in inequalities.get(reference, set()):
                    return False
                equalities[reference] = expected
            else:
                if equalities.get(reference) == expected:
                    return False
                inequalities.setdefault(reference, set()).add(expected)
        return True

    def _conditions_are_mutex(self, condition1: Any, condition2: Any) -> bool:
        """只有在两个条件的所有 OR 分支都不兼容时才判定互斥。"""
        clauses1 = self._condition_clauses(condition1)
        clauses2 = self._condition_clauses(condition2)
        if clauses1 is None or clauses2 is None or not clauses1 or not clauses2:
            return False
        return not any(
            self._clauses_are_compatible(left, right)
            for left in clauses1
            for right in clauses2
        )

    def _get_action_path(self, uses) -> str:
        """提取 uses 的 action 路径（去掉 @版本 后缀）"""
        if not uses or not isinstance(uses, str):
            return ''
        return uses.split('@')[0].strip()

    def _is_exact_action(self, uses, action_path: str) -> bool:
        """检查 uses 是否精确引用指定 action（路径部分完全一致）"""
        return self._get_action_path(uses) == action_path

    def _classify_cache_action(self, uses: str) -> Optional[Dict[str, Any]]:
        """区分缓存 action 的读写语义。"""
        action_path = self._get_action_path(uses)
        operations = {
            'actions/cache': ('restore_and_save', True),
            'actions/cache/save': ('save', True),
            'actions/cache/restore': ('restore', False),
        }
        operation = operations.get(action_path)
        if operation is None:
            return None
        name, can_write = operation
        return {
            'action_path': action_path,
            'operation': name,
            'can_write': can_write,
        }

    def _is_setup_cache_enabled(self, action_path: str, cache_value: Any) -> bool:
        """判断 setup action 的缓存参数是否为静态启用状态。"""
        if action_path == 'astral-sh/setup-uv':
            return cache_value is True or (
                isinstance(cache_value, str) and cache_value.strip().lower() in {'true', 'auto'}
            )
        if action_path == 'actions/setup-node':
            return isinstance(cache_value, str) and cache_value.strip() in {'npm', 'yarn', 'pnpm'}
        return cache_value is not None and cache_value is not False and cache_value != 'false'

    def _setup_node_auto_cache_state(self, step, package_evidence=None) -> Dict[str, Any]:
        """Without same-version package.json, auto-caching is unknown rather than enabled."""
        cache_value = step.with_.get('cache')
        pinned = pinned_setup_cache_capability(step.uses)
        version = step.uses_version
        ref = str(step.uses).rsplit('@', 1)[-1]
        old_node2 = re.fullmatch(r'v?2\.([01])(?:\.\d+)?', ref)
        if self._condition_is_definitely_false(step.if_):
            return {'status': 'disabled', 'reason': 'setup_step_condition_is_false', 'managers': []}
        if (pinned and not pinned['cache_supported']) or (version is not None and version < 2) or old_node2:
            return {'status': 'disabled', 'reason': 'cache_unsupported_in_this_version', 'managers': []}
        if cache_value not in (None, ''):
            if isinstance(cache_value, str) and cache_value.strip() in {'npm', 'yarn', 'pnpm'}:
                manager = cache_value.strip()
                supported = (pinned.get('explicit_managers') or None) if pinned else ['npm', 'yarn', 'pnpm'] if version is not None and 3 <= version <= 6 else ['npm', 'yarn'] if version == 2 and manager != 'pnpm' else None
                status = 'enabled' if supported and manager in supported else 'disabled' if supported is not None else 'unknown'
                return {'status': status, 'reason': 'explicit_cache_config' if status == 'enabled' else 'explicit_manager_support_unconfirmed',
                        'manager': manager, 'managers': [manager]}
            return {'status': 'unknown' if '${{' in str(cache_value) else 'disabled',
                    'reason': 'cache_input_not_statically_enabled', 'managers': ['npm', 'yarn', 'pnpm']}
        evidence = {'resolution': pinned['resolution'], 'source': pinned['source']} if pinned else {}
        if pinned and pinned.get('auto_managers') is not None:
            managers = list(pinned['auto_managers'])
        elif pinned:
            return {'status': 'unknown', 'reason': 'node_auto_cache_semantics_unrecognized',
                    'managers': ['npm', 'yarn', 'pnpm'], **evidence}
        elif version is not None and version <= 6:
            managers = ['npm'] if version >= 6 else ['npm', 'yarn', 'pnpm'] if version >= 5 else []
        else:
            managers = ['npm', 'yarn', 'pnpm']  # Unknown refs may support any of the auto-cache variants.
        if not managers:
            return {'status': 'disabled', 'reason': 'no_auto_cache_in_this_version', 'managers': [], **evidence}
        automatic = step.with_.get('package-manager-cache')
        if automatic in (None, '') and pinned and 'auto_default_enabled' in pinned:
            if pinned['auto_default_enabled'] is None:
                return {'status': 'unknown', 'reason': 'automatic_cache_default_unrecognized', 'managers': managers, **evidence}
            automatic = pinned['auto_default_enabled']
        if automatic is False or (isinstance(automatic, str) and automatic.strip().lower() == 'false'):
            return {'status': 'disabled', 'reason': 'package_manager_cache_disabled', 'managers': managers, **evidence}
        if automatic not in (None, '', True, 'true', 'TRUE'):
            return {'status': 'unknown', 'reason': 'automatic_cache_control_not_static', 'managers': managers, **evidence}
        selector = pinned.get('auto_selector') if pinned else 'npm_context_fields' if version == 6 else 'top_level_package_manager' if version == 5 else None
        if (isinstance(package_evidence, dict) and package_evidence.get('step_context_verified') is True
                and re.fullmatch(r'[0-9a-fA-F]{40}', str(package_evidence.get('source_commit', '')))
                and isinstance(package_evidence.get('package_json'), dict) and selector):
            package = package_evidence['package_json']
            candidates = []
            if selector == 'npm_context_fields':
                dev = package.get('devEngines') or {}
                declaration = dev.get('packageManager') if isinstance(dev, dict) else None
                declarations = declaration if isinstance(declaration, list) else [declaration]
                candidates.extend(obj.get('name') for obj in declarations if isinstance(obj, dict))
            candidates.append(package.get('packageManager'))
            pattern = r'^\^?(npm)(?:@.*)?$' if selector == 'npm_context_fields' else r'^\^?(npm|yarn|pnpm)@'
            selected = next((match.group(1) for candidate in candidates if isinstance(candidate, str)
                             for match in [re.match(pattern, candidate)] if match and match.group(1) in managers), None)
            return {'status': 'enabled' if selected else 'disabled',
                    'reason': 'verified_package_json_auto_cache' if selected else 'verified_package_json_has_no_supported_manager',
                    'manager': selected, 'managers': managers, 'package_source_commit': package_evidence['source_commit'], **evidence}
        return {'status': 'unknown', 'reason': 'same_version_package_json_unavailable', 'managers': managers, **evidence}

    def _setup_go_cache_state(self, step) -> Dict[str, Any]:
        if self._condition_is_definitely_false(step.if_):
            return {'status': 'disabled', 'reason': 'setup_step_condition_is_false'}
        version = step.uses_version
        value = step.with_.get('cache')
        config = self.SETUP_CACHE_ACTIONS['actions/setup-go']
        pinned = pinned_setup_cache_capability(step.uses)
        evidence = {'resolution': pinned['resolution'], 'source': pinned['source']} if pinned else {}
        if (pinned and not pinned['cache_supported']) or (version is not None and version < config['cache_support_version']):
            return {'status': 'disabled', 'reason': 'cache_unsupported_in_this_version', **evidence}
        if value is False or (isinstance(value, str) and value.strip().lower() == 'false'):
            return {'status': 'disabled', 'reason': 'explicitly_disabled', **evidence}
        if pinned is None and (version is None or version > 6):
            return {'status': 'unknown', 'reason': 'setup_go_version_unresolved'}
        if value is True or (isinstance(value, str) and value.strip().lower() == 'true'):
            return {'status': 'enabled', 'reason': 'explicitly_enabled', **evidence}
        if value not in (None, ''):
            return {'status': 'unknown', 'reason': 'cache_input_not_static_boolean'}
        default_enabled = pinned['default_enabled'] if pinned else version >= config['version_threshold']
        return {'status': 'enabled' if default_enabled else 'disabled',
                'reason': 'default_enabled' if default_enabled else 'default_disabled', **evidence}

    def _shell_commands(self, run_content: str) -> List[str]:
        """Split shell command boundaries while preserving quoted arguments.

        This is a lexical split, not shell execution. Unknown command forms are
        retained so they cannot establish that every dependency operation is auxiliary.
        """
        if not isinstance(run_content, str):
            return []
        text = re.sub(r'(?:\\|`)\r?\n', ' ', run_content)
        text, expressions = protect_expressions(text)
        commands, current = [], []
        quote = None
        escaped = False
        comment = False
        for char in text:
            if comment:
                if char != '\n':
                    continue
                comment = False
            if escaped:
                current.append(char)
                escaped = False
                continue
            if char == '\\' and quote != "'":
                current.append(char)
                escaped = True
                continue
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
                current.append(char)
            elif char == '#' and (not current or current[-1].isspace()):
                comment = True
            elif char in '\n;&|':
                command = ''.join(current).strip()
                if command:
                    commands.append(command)
                current = []
            else:
                current.append(char)
        if current:
            commands.append(''.join(current).strip())
        return [self._restore_ndc_expressions(command, expressions) for command in commands]

    @staticmethod
    def _restore_ndc_expressions(command, expressions):
        for token, original in expressions.items():
            command = command.replace(token, original)
        return command

    def _dependency_commands(self, run_content: str) -> List[str]:
        return [command for command in self._shell_commands(run_content)
                if any(pattern.search(command) for pattern, _, _, _ in self._compiled_patterns)]

    def _is_uv_bootstrap_install(self, run_content: str) -> bool:
        commands = self._dependency_commands(run_content)
        return bool(commands) and all(self._is_uv_bootstrap_command(command) for command in commands)

    def _is_uv_bootstrap_command(self, run_content: str) -> bool:
        """判断命令是否仅通过 pip 安装 uv 工具本身。"""
        if not isinstance(run_content, str):
            return False
        for command in run_content.splitlines():
            if not re.search(r'\bpip3?\s+install\b', command, re.IGNORECASE):
                continue
            try:
                tokens = shlex.split(command)
            except ValueError:
                continue
            try:
                install_index = next(
                    index for index, token in enumerate(tokens)
                    if token.lower() in {'pip', 'pip3'} and index + 1 < len(tokens)
                    and tokens[index + 1].lower() == 'install'
                )
            except StopIteration:
                continue
            arguments = tokens[install_index + 2:]
            if '--no-cache-dir' not in arguments:
                continue
            packages = [
                token for token in arguments
                if not token.startswith('-') and not token.startswith('$')
            ]
            if packages and all(re.fullmatch(r'uv(?:[<>=!~].*)?', package, re.IGNORECASE)
                                for package in packages):
                return True
        return False

    def _analyze_job(self, job_name: str, job, workflow: WorkflowModel, workflow_mode: Dict[str, Any]) -> tuple:
        """分析单个 Job"""
        smells = []
        dep_langs = set()
        dep_managers = set()
        cache_steps = []
        setup_steps = []
        checkout_idx = None
        dep_install_indices = []  # 依赖安装步骤索引
        dep_install_steps = []   # 依赖安装步骤详情 (index, lang)
        setup_cache_assessments = []
        cache_step_indices = []
        all_run_content = []     # job 内所有 run 命令（用于项目类型判断）
        nested_actions = []      # 可能封装缓存行为的自定义 setup action，仅供审阅
        full_job_dep_langs = {
            lang
            for step in job.steps
            if step.run and isinstance(step.run, str)
            for command in self._dependency_commands(step.run)
            for pattern, lang, _, _ in self._compiled_patterns
            if pattern.search(command)
            and not (lang == 'python' and self._is_uv_bootstrap_install(command))
        }

        for idx, step in enumerate(job.steps):
            # 记录所有 run 命令
            if step.run and isinstance(step.run, str):
                all_run_content.append(step.run)

            # 检测依赖操作
            if step.run and isinstance(step.run, str):
                for command, pattern, lang, keywords in (
                    (command, pattern, lang, keywords)
                    for command in self._dependency_commands(step.run)
                    for pattern, lang, _, keywords in self._compiled_patterns
                ):
                    match = pattern.search(command)
                    if match:
                        # ``uv pip install`` uses uv's cache, not pip's. The
                        # generic pip expression also matches its inner words.
                        if (lang == 'python' and 'pip' in keywords
                                and re.search(r'\buv\s+$', command[:match.start()], re.I)):
                            continue
                        if lang == 'python' and self._is_uv_bootstrap_install(command):
                            continue
                        manager = lang
                        if lang == 'npm':
                            manager = next(
                                (candidate for candidate in ('npm', 'yarn', 'pnpm')
                                 if candidate in keywords),
                                'npm',
                            )
                        elif lang == 'python':
                            manager = next(
                                (candidate for candidate in ('uv', 'pipenv', 'poetry', 'pip')
                                 if candidate in keywords),
                                'pip',
                            )
                        dep_langs.add(lang)
                        dep_managers.add(manager)
                        dep_install_indices.append(idx)
                        dep_install_steps.append({
                            'index': idx,
                            'lang': lang,
                            'manager': manager,
                            'run': command,
                        })

            # 检测缓存操作
            if step.uses:
                for cache_action in self.CACHE_ACTIONS:
                    if cache_action in step.uses:
                        # 检测 lookup-only 模式
                        lookup_only = step.with_.get('lookup-only')
                        cache_semantics = self._classify_cache_action(step.uses)
                        cache_info = {
                            'index': idx,
                            'action': step.uses,
                            'step_id': step.id,
                            'if': step.if_,
                            'key': step.with_.get('key'),
                            'key_present': 'key' in (step.with_ or {}),
                            'path': step.with_.get('path'),
                            'path_present': 'path' in (step.with_ or {}),
                            'restore_keys': step.with_.get('restore-keys'),
                            'lookup_only': lookup_only,
                            'fail_on_cache_miss': step.with_.get('fail-on-cache-miss'),
                            'can_write': cache_semantics['can_write'] if cache_semantics else True,
                            'operation': cache_semantics['operation'] if cache_semantics else None,
                            'read_only': bool(
                                (isinstance(lookup_only, str) and lookup_only.strip().lower() == 'true')
                                or lookup_only is True
                                or (cache_semantics is not None and not cache_semantics['can_write'])
                            ),
                        }
                        cache_steps.append(cache_info)
                        cache_step_indices.append(idx)

                # 检测 setup actions
                for setup_action, config in self.SETUP_ACTION_CACHE.items():
                    if self._is_exact_action(step.uses, setup_action):
                        cache_enabled = False
                        version = step.uses_version

                        cache_val = step.with_.get(config['cache_param'])
                        node_state = None
                        if setup_action == 'actions/setup-node':
                            package_evidence = self.setup_node_package_json_provider(workflow, job_name, idx) if self.setup_node_package_json_provider else None
                            node_state = self._setup_node_auto_cache_state(step, package_evidence)
                            setup_cache_assessments.append({'index': idx, 'action': step.uses, **node_state})
                            if node_state['status'] == 'enabled':
                                cache_val = node_state['manager']
                        if ((node_state is not None and node_state['status'] == 'enabled')
                                or (node_state is None and self._is_setup_cache_enabled(setup_action, cache_val))):
                            cache_enabled = True
                            cache_info = {
                                'index': idx,
                                'action': step.uses,
                                'step_id': step.id,
                                'if': step.if_,
                                'key': None,
                                'path': config['default_paths'].get(cache_val, list(config['default_paths'].values())[0]),
                                'path_present': True,
                                'restore_keys': None,
                                'operation': 'restore_and_save',
                                'setup_cache': True,
                                'cache_value': cache_val,
                                'cache_reason': node_state['reason'] if node_state else 'explicit_cache_config',
                                'cache_dirs': [config['default_paths'].get(
                                    cache_val, list(config['default_paths'].values())[0]
                                )],
                            }
                            cache_steps.append(cache_info)
                            cache_step_indices.append(idx)
                        setup_steps.append({
                            'index': idx,
                            'action': step.uses,
                            'cache_enabled': cache_enabled,
                            'langs': config['langs'],
                        })

                # 检测其他 setup actions（setup-go, setup-java 等）的缓存行为
                for setup_action, config in self.SETUP_CACHE_ACTIONS.items():
                    # setup-node/setup-python 已在上面的精确配置中处理，避免重复加入 cache_steps。
                    if self._is_exact_action(step.uses, setup_action) and setup_action in self.SETUP_ACTION_CACHE:
                        continue
                    if self._is_exact_action(step.uses, setup_action):
                        cache_enabled = False
                        cache_reason = None
                        cache_value = None

                        if setup_action == 'actions/setup-go':
                            go_state = self._setup_go_cache_state(step)
                            setup_cache_assessments.append({'index': idx, 'action': step.uses, **go_state})
                            cache_enabled = go_state['status'] == 'enabled'
                            cache_reason = go_state['reason']
                            cache_value = 'go' if cache_enabled else None
                        else:
                            # 检查是否显式禁用了缓存
                            disable_flag = config.get('disable_flag')
                            if disable_flag:
                                cache_val = step.with_.get(disable_flag)
                                if cache_val is not None and (cache_val is False or cache_val == 'false'):
                                    cache_enabled = False
                                    cache_reason = 'explicitly_disabled'
                                elif config.get('default_enabled'):
                                    # 默认启用，且没有显式禁用
                                    cache_enabled = True
                                    cache_value = 'default'
                                    cache_reason = 'default_enabled'

                            # 检查是否显式启用了缓存
                            enable_flag = config.get('enable_flag')
                            if enable_flag:
                                cache_val = step.with_.get(enable_flag)
                                if self._is_setup_cache_enabled(setup_action, cache_val):
                                    cache_enabled = True
                                    cache_value = cache_val
                                    cache_reason = 'explicitly_enabled'

                        if cache_enabled:
                            # 确定缓存路径
                            cache_dirs = config.get('cache_dirs', [])
                            cache_path = cache_dirs[0] if cache_dirs else None

                            cache_info = {
                                'index': idx,
                                'action': step.uses,
                                'step_id': step.id,
                                'if': step.if_,
                                'key': None,
                                'path': cache_path,
                                'path_present': bool(cache_dirs),
                                'restore_keys': None,
                                'operation': 'restore_and_save',
                                'setup_cache': True,
                                'cache_value': cache_value,
                                'cache_reason': cache_reason,
                                'cache_dirs': cache_dirs,
                            }
                            cache_steps.append(cache_info)
                            cache_step_indices.append(idx)

                # 标记可能封装缓存行为的自定义 setup action，仅供后续审阅。
                # 该标记不作为缓存证据，不能抑制 no_dependency_cache。
                if step.uses and isinstance(step.uses, str):
                    uses_lower = step.uses.lower()
                    action_path = self._get_action_path(step.uses).lower()
                    # pnpm/action-setup 只负责安装 pnpm，本身不提供依赖缓存，
                    # 不应抑制 NDC；其余原有 setup action 兼容逻辑保持不变。
                    if (
                        not action_path.startswith('actions/')
                        and action_path != 'pnpm/action-setup'
                        and 'setup' in uses_lower
                    ):
                        if any(keyword in uses_lower for keyword in ('node', 'js', 'pnpm', 'python')):
                            nested_actions.append({
                                'index': idx,
                                'action': step.uses,
                                'nested': True,
                            })

            # 检测 checkout
            if step.uses and 'actions/checkout' in step.uses:
                if checkout_idx is None:
                    checkout_idx = idx

        analysis = {
            'dep_langs': sorted(list(dep_langs)),
            'dep_managers': sorted(list(dep_managers)),
            'cache_steps': cache_steps,
            'setup_steps': setup_steps,
            'setup_cache_assessments': setup_cache_assessments,
            'needs': list(job.needs or []),
            'runs_on': job.runs_on,
            'job_if': job.if_,
            'checkout_idx': checkout_idx,
            'dep_install_indices': dep_install_indices,
            'dep_install_steps': dep_install_steps,
            'cache_step_indices': cache_step_indices,
            'all_run_content': all_run_content,
            'nested_actions': nested_actions,
            'docker_build_detected': any(
                self._get_action_path(step.uses).lower() in {
                    'docker/build-push-action', 'docker/bake-action',
                }
                or (
                    isinstance(step.run, str)
                    and self._has_docker_build_command(step.run)
                )
                for step in job.steps
            ),
        }
        docker_assessments = self._assess_docker_layer_cache(job, analysis, workflow)
        analysis['docker_cache_assessments'] = docker_assessments
        analysis['docker_cache_counts'] = {
            state: sum(item['status'] == state for item in docker_assessments)
            for state in ('configured', 'missing', 'unknown')
        }
        analysis['docker_layer_cache_detected'] = bool(docker_assessments) and all(
            item['status'] == 'configured' for item in docker_assessments
        )

        # 检测各种异味
        smells.extend(self._detect_no_dependency_cache(job_name, analysis, workflow_mode, job, workflow))
        smells.extend(self._detect_no_restore_keys(job_name, analysis, workflow_mode, workflow))
        smells.extend(self._detect_cache_position_error(job_name, analysis, job, workflow, workflow_mode))
        smells.extend(self._detect_disposable_cache_key(job_name, analysis, workflow_mode, job, workflow))
        smells.extend(self._detect_static_cache_key(job_name, analysis, workflow_mode, job, workflow))
        smells.extend(self._detect_no_docker_layer_cache(job_name, job, analysis, workflow_mode))
        smells.extend(self._detect_conflicting_cache_sources(job_name, analysis, workflow_mode, job, workflow))
        smells.extend(self._detect_incorrect_cache_hit_condition(job_name, job, analysis, workflow_mode))
        smells.extend(self._detect_unreachable_cache_write(job_name, job, workflow, analysis, workflow_mode))
        smells.extend(self._detect_cache_path_smells(job_name, job, analysis, workflow_mode))
        smells.extend(self._detect_deprecated_cache_action(job_name, analysis, workflow_mode))

        return smells, analysis

    def _cache_path_items(self, value: Any) -> List[str]:
        """将缓存 path 统一为非空字符串列表。"""
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, (list, tuple)):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return []

    def _workflow_trigger_types(self, workflow: WorkflowModel) -> Set[str]:
        """读取工作流触发器，并兼容 PyYAML 将 ``on`` 解析为布尔键的情况。"""
        trigger_types = set(workflow.triggers.trigger_types)
        if trigger_types:
            return {str(value).lower().replace('-', '_') for value in trigger_types}

        raw_data = workflow.yaml_data or {}
        raw_on = raw_data.get('on')
        if raw_on is None:
            raw_on = raw_data.get(True)
        if isinstance(raw_on, str):
            return {raw_on.lower().replace('-', '_')}
        if isinstance(raw_on, list):
            return {
                str(value).lower().replace('-', '_')
                for value in raw_on
                if isinstance(value, str)
            }
        if isinstance(raw_on, dict):
            return {
                str(value).lower().replace('-', '_')
                for value in raw_on
            }
        return set()

    def _is_exact_cache_hit_guard(self, condition: Any, step_id: Any) -> bool:
        """判断条件是否把后续步骤限制在明确的 cache hit 上。"""
        if not isinstance(condition, str) or not isinstance(step_id, str) or not step_id.strip():
            return False
        normalized = self._normalize_condition(condition).lower()
        reference = rf"steps\.{re.escape(step_id.strip().lower())}\.outputs\.cache-hit"
        if not re.search(rf'\b{reference}\b', normalized):
            return False

        exact_hit_patterns = (
            rf'\b{reference}\b\s*==\s*[\'\"]true[\'\"]',
            rf'[\'\"]true[\'\"]\s*==\s*\b{reference}\b',
            rf'\b{reference}\b\s*!=\s*[\'\"]false[\'\"]',
            rf'[\'\"]false[\'\"]\s*!=\s*\b{reference}\b',
        )
        if any(re.search(pattern, normalized) for pattern in exact_hit_patterns):
            return True

        # ``if: steps.restore.outputs.cache-hit`` 的 truthiness 等价于只在
        # 命中时执行；带有显式比较的其他形式在这里保持保守。
        return bool(re.search(
            rf'\b{reference}\b\s*(?:$|&&|\|\||\))',
            normalized,
        ))

    def _is_required_cold_start_step(self, step) -> bool:
        """识别缓存未命中时通常仍必须执行的安装步骤。"""
        run = step.run if isinstance(step.run, str) else ''
        uses = self._get_action_path(step.uses).lower()
        text = f'{run}\n{step.name}\n{uses}'.lower()

        if re.search(r'\b(?:npm\s+(?:ci|install|i)|yarn\s+(?:install|ci)|pnpm\s+install)\b|(?m:^[ \t]*yarn[ \t]*(?:#[^\n]*)?$)', run.lower()):
            return True
        if re.search(r'\b(?:poetry\s+install|pipenv\s+install|uv\s+(?:sync|pip\s+install))\b', text):
            return not self._is_uv_bootstrap_install(run)
        if re.search(r'\b(?:pip|pip3|python\d*\s+-m\s+pip)\s+install\b', text):
            # 只把 requirements/锁定依赖安装视为项目冷启动步骤；
            # 单独安装 pytest、poetry 等 CI 工具不触发该规则。
            return bool(re.search(
                r'(?:-r\s+\S+|requirements(?:[-_][\w.-]+)?\.txt|pyproject\.toml|setup\.py)',
                text,
            ))
        if re.search(r'\b(?:npx\s+)?playwright\s+install(?:-deps)?\b', text):
            return True
        return False

    def _detect_incorrect_cache_hit_condition(
        self,
        job_name: str,
        job,
        analysis: Dict,
        workflow_mode: Dict,
    ) -> List[SmellInstance]:
        """检测把必要的冷启动步骤错误地限制在缓存命中时执行。"""
        smells = []

        for cache_info in analysis.get('cache_steps', []):
            if cache_info.get('setup_cache'):
                continue
            operation = cache_info.get('operation')
            if operation == 'save' or cache_info.get('fail_on_cache_miss'):
                continue
            step_id = cache_info.get('step_id')
            if not step_id:
                continue

            cache_index = cache_info.get('index', -1)
            for index, step in enumerate(job.steps):
                if index <= cache_index or not self._is_required_cold_start_step(step):
                    continue
                if not self._is_exact_cache_hit_guard(step.if_, step_id):
                    continue
                smells.append(self.create_smell(
                    smell_type='incorrect_cache_hit_condition',
                    severity=SeverityLevel.HIGH,
                    message=f'Job "{job_name}" 仅在缓存命中时执行必要的安装步骤，缓存未命中时可能跳过冷启动',
                    job_name=job_name,
                    step_name=step.name or f'步骤 (index: {index})',
                    details={
                        'cache_step_index': cache_index,
                        'cache_step_id': step_id,
                        'guarded_step_index': index,
                        'condition': step.if_,
                        'cache_path': cache_info.get('path'),
                        'cache_key': cache_info.get('key'),
                    },
                    suggestion='必要的安装或构建步骤应在缓存命中和未命中时都能执行；若只需命中时跳过，请将条件放在可选的补充步骤上',
                ))
        return smells

    def _event_condition_is_unreachable(
        self,
        condition: Any,
        trigger_types: Set[str],
    ) -> bool:
        """判断简单的 event_name 条件是否与工作流触发器矛盾。"""
        if not isinstance(condition, str) or not trigger_types:
            return False
        # 可复用工作流继承调用方的 github 事件上下文；本文件触发器无法限定事件。
        if 'workflow_call' in trigger_types:
            return False
        normalized = self._normalize_condition(condition).lower()
        equal_events = {
            value.replace('-', '_')
            for value in re.findall(
                r'github\.event_name\s*==\s*[\'\"]([a-z0-9_-]+)[\'\"]',
                normalized,
            )
        }
        if equal_events and not equal_events.intersection(trigger_types):
            return True

        excluded_events = {
            value.replace('-', '_')
            for value in re.findall(
                r'github\.event_name\s*!=\s*[\'\"]([a-z0-9_-]+)[\'\"]',
                normalized,
            )
        }
        return bool(excluded_events) and trigger_types.issubset(excluded_events)

    def _detect_unreachable_cache_write(
        self,
        job_name: str,
        job,
        workflow: WorkflowModel,
        analysis: Dict,
        workflow_mode: Dict,
    ) -> List[SmellInstance]:
        """检测明确不可达的 actions/cache/save 写入步骤。"""
        smells = []
        trigger_types = self._workflow_trigger_types(workflow)

        for cache_info in analysis.get('cache_steps', []):
            if cache_info.get('setup_cache') or cache_info.get('operation') != 'save':
                continue
            index = cache_info.get('index', -1)
            if not isinstance(index, int) or index < 0 or index >= len(job.steps):
                continue
            step = job.steps[index]
            reasons = []
            if self._condition_is_definitely_false(getattr(step, 'if_', None)):
                reasons.append('step_condition_is_false')
            if self._condition_is_definitely_false(getattr(job, 'if_', None)):
                reasons.append('job_condition_is_false')
            if self._event_condition_is_unreachable(step.if_, trigger_types):
                reasons.append('event_condition_not_in_workflow_triggers')
            if job.if_ and step.if_ and self._conditions_are_mutex(job.if_, step.if_):
                reasons.append('job_and_step_conditions_are_mutex')
            missing_output_steps = self._missing_cache_output_step_ids(
                cache_info.get('key'), job,
            )
            if missing_output_steps:
                key_definitely_empty = self._key_is_empty_from_missing_cache_outputs(
                    cache_info.get('key'), missing_output_steps,
                )
                analysis.setdefault('cache_write_key_assessments', []).append({
                    'cache_step_index': index,
                    'key': cache_info.get('key'),
                    'missing_output_step_ids': missing_output_steps,
                    'status': 'definitely_empty' if key_definitely_empty else 'unknown',
                })
                if key_definitely_empty:
                    reasons.append('referenced_cache_output_step_missing')

            if not reasons:
                continue
            smells.append(self.create_smell(
                smell_type='unreachable_cache_write',
                severity=SeverityLevel.HIGH,
                message=f'Job "{job_name}" 的缓存保存步骤在当前工作流条件下不可达',
                job_name=job_name,
                step_name=step.name or f'缓存保存步骤 (index: {index})',
                details={
                    'cache_step_index': index,
                    'action': step.uses,
                    'condition': step.if_,
                    'job_condition': job.if_,
                    'workflow_triggers': sorted(trigger_types),
                    'reasons': reasons,
                },
                suggestion='使 save 条件与工作流触发器及 Job 条件存在可满足的交集，或移除不会执行的保存步骤',
            ))
        return smells

    def _missing_cache_output_step_ids(self, value: Any, job) -> List[str]:
        """找出缓存 key 引用但在当前 Job 中没有定义的缓存步骤 id。

        这里只处理 actions/cache 暴露的 cache-hit/cache-primary-key/
        cache-matched-key 输出。对任意业务步骤输出不做推断，避免把普通的
        动态 key 误报为不可达写入。
        """
        if not isinstance(value, str):
            return []
        defined_ids = {
            str(step.id).strip().lower()
            for step in job.steps
            if getattr(step, 'id', None)
        }
        references = re.findall(
            r'\bsteps\.([A-Za-z_][\w-]*)\.outputs\.(?:cache-hit|cache-primary-key|cache-matched-key)\b',
            value,
            re.IGNORECASE,
        )
        return list(dict.fromkeys(
            reference for reference in references
            if reference.strip().lower() not in defined_ids
        ))

    def _key_is_empty_from_missing_cache_outputs(self, value: Any, missing_ids: List[str]) -> bool:
        """仅当整个 key 由缺失缓存输出的直接引用构成时确认其为空。

        缺失属性会展开为空串，但拼接文字、其他输出、fallback 或函数表达式
        可能提供非空内容；这些情况不能据此判定保存不可达。
        """
        if not isinstance(value, str) or not missing_ids:
            return False
        missing = {step_id.lower() for step_id in missing_ids}
        expression = re.compile(
            r'\$\{\{\s*steps\.([A-Za-z_][\w-]*)\.outputs\.'
            r'(?:cache-hit|cache-primary-key|cache-matched-key)\s*\}\}',
            re.IGNORECASE,
        )
        remainder = expression.sub(
            lambda match: '' if match.group(1).lower() in missing else match.group(0),
            value,
        )
        return remainder == ''

    def _job_platform_families(self, job) -> Set[str]:
        """返回 Job 明确覆盖的操作系统族。"""
        values = []
        runs_on = job.runs_on
        contexts = self._enumerate_matrix_contexts(job.matrix)
        if contexts is None:
            contexts = []
        matrix_match = re.fullmatch(r'\s*\${{\s*matrix\.([\w-]+)\s*}}\s*', str(runs_on or ''))
        if matrix_match and contexts:
            variable = matrix_match.group(1)
            values = [context.get(variable) for context in contexts if context.get(variable) is not None]
        elif runs_on is not None:
            values = [runs_on]

        families = set()
        for value in values:
            normalized = str(value).lower()
            if 'windows' in normalized:
                families.add('windows')
            elif 'macos' in normalized or 'darwin' in normalized:
                families.add('macos')
            elif 'ubuntu' in normalized or 'linux' in normalized:
                families.add('linux')
        return families

    def _nonportable_path_reason(self, path: str, job) -> Optional[str]:
        families = self._job_platform_families(job)
        if len(families) < 2:
            return None
        normalized = str(path).lower().replace('\\', '/')
        has_windows = 'windows' in families
        has_unix = bool(families.intersection({'linux', 'macos'}))
        if has_windows and has_unix:
            if re.search(r'(?:^|/)(?:home/|usr/|opt/|var/|tmp/)|~/(?:\.cache|\.local)/|/\.cache/|/\.local/', normalized):
                return 'unix_specific_path_in_windows_matrix'
            if re.search(r'(?:[a-z]:/|%localappdata%|appdata/|users/[^/]+/appdata)', normalized):
                return 'windows_specific_path_in_unix_matrix'
        return None

    def _is_platform_restricted_condition(self, condition: Any) -> bool:
        """判断缓存步骤是否明确只在某个平台分支执行。

        跨平台 Job 中的 Windows 专用或 Unix 专用缓存本身并不构成不可移植：
        如果该步骤只在对应平台执行，就不存在同一个缓存配置跨平台复用的问题。
        """
        if not isinstance(condition, str):
            return False
        normalized = self._normalize_condition(condition).lower()
        return bool(re.search(
            r'(?:runner\.os|matrix\.[a-z_][\w-]*)\s*(?:==|!=)\s*[\'\"](?:windows|windows-[^\'\"]+|linux|ubuntu-[^\'\"]+|macos|macos-[^\'\"]+)[\'\"]',
            normalized,
        ) or re.search(
            r'(?:startsWith|contains)\s*\(\s*(?:runner\.os|matrix\.[a-z_][\w-]*)\s*,\s*[\'\"](?:windows|linux|ubuntu|macos)',
            normalized,
        ))

    def _malformed_cache_path_reason(self, value: Any) -> Optional[str]:
        """只报告可以由 YAML 静态确定的路径语法错误。"""
        if not isinstance(value, (str, list, tuple)):
            return 'unsupported_path_type'
        items = self._cache_path_items(value)
        if not items:
            return 'empty_path'
        text = '\n'.join(items)
        if text.count('${{') != text.count('}}'):
            return 'unbalanced_expression'
        if re.search(r'\$\{\{\s*\}\}', text):
            return 'empty_expression'
        for item in items:
            if (item.startswith(('"', "'")) and not item.endswith(item[0])) or (
                item.endswith(('"', "'")) and not item.startswith(item[-1])
            ):
                return 'unbalanced_literal_quotes'
        return None

    def _detect_cache_path_smells(
        self,
        job_name: str,
        job,
        analysis: Dict,
        workflow_mode: Dict,
    ) -> List[SmellInstance]:
        """检测路径范围、污染、跨平台和语法类缓存异味。"""
        smells = []
        for cache_info in analysis.get('cache_steps', []):
            if cache_info.get('setup_cache'):
                continue
            path_value = cache_info.get('path')
            path_items = self._cache_path_items(path_value)
            if cache_info.get('path_present'):
                malformed_reason = self._malformed_cache_path_reason(path_value)
                if malformed_reason:
                    smells.append(self.create_smell(
                        smell_type='malformed_cache_path',
                        severity=SeverityLevel.HIGH,
                        message=f'Job "{job_name}" 的缓存路径存在确定的语法问题',
                        job_name=job_name,
                        step_name=f'缓存步骤 (index: {cache_info.get("index")})',
                        details={
                            'path': path_value,
                            'reason': malformed_reason,
                            'action': cache_info.get('action'),
                        },
                        suggestion='将缓存 path 设置为非空的字符串或字符串列表，并修复未闭合的表达式或字面量引号',
                    ))

            for path in path_items:
                normalized = path.strip().lower().replace('\\', '/')
                broad_reason = None
                if normalized.rstrip('/') in {
                    '.', '${{ github.workspace }}', '$github_workspace',
                    '${{ env.github_workspace }}', '~', '$home', '${{ env.home }}',
                    '/', '**', '**/*', '*',
                }:
                    broad_reason = 'workspace_or_home_root'
                elif re.fullmatch(r'\$\{\{\s*github\.workspace\s*\}\}/?[*]?', normalized):
                    broad_reason = 'workspace_root'
                elif re.fullmatch(r'(?:/home/runner|/users/runner|[a-z]:/users/[^/]+|%userprofile%)/?', normalized):
                    broad_reason = 'runner_home_root'
                elif re.search(r'(^|/)\*\*/?\*?$', normalized) and normalized.count('/') <= 2:
                    broad_reason = 'broad_recursive_glob'

                if broad_reason:
                    smells.append(self.create_smell(
                        smell_type='overly_broad_cache_path',
                        severity=SeverityLevel.MEDIUM,
                        message=f'Job "{job_name}" 的缓存路径范围过大，可能包含大量无关内容',
                        job_name=job_name,
                        step_name=f'缓存步骤 (index: {cache_info.get("index")})',
                        details={
                            'path': path,
                            'reason': broad_reason,
                            'action': cache_info.get('action'),
                        },
                        suggestion='将缓存范围收窄到明确的依赖目录、工具缓存或构建输出目录',
                    ))

                pollution_reason = None
                # /tmp/.buildx-cache 是 BuildKit 官方常见的本地层缓存目录，
                # 虽然位于 /tmp 下，但不属于运行时文件污染。
                if re.search(r'(?:^|[/.\\_-])(?:\.?)buildx-cache(?:[/._\\-]|$)', normalized):
                    pollution_reason = None
                elif re.search(r'(?:^|/)(?:tmp|temp|var/tmp|logs?|log)(?:/|$)', normalized):
                    pollution_reason = 'temporary_or_log_directory'
                elif re.search(r'(?:\*\.(?:log|pid|sock)|\.(?:log|pid|sock)$)', normalized):
                    pollution_reason = 'runtime_state_file_pattern'
                elif re.search(r'(^|/)\$runner_temp(?:/|$)', normalized):
                    pollution_reason = 'runner_temp_directory'
                elif '/tmp/' in normalized and re.search(r'\.(?:iso|qcow2|img|tar)$', normalized):
                    pollution_reason = 'temporary_directory_contains_generated_image'

                if pollution_reason:
                    smells.append(self.create_smell(
                        smell_type='cache_path_pollution',
                        severity=SeverityLevel.MEDIUM,
                        message=f'Job "{job_name}" 的缓存路径可能混入临时文件、日志或运行时状态',
                        job_name=job_name,
                        step_name=f'缓存步骤 (index: {cache_info.get("index")})',
                        details={
                            'path': path,
                            'reason': pollution_reason,
                            'action': cache_info.get('action'),
                        },
                        suggestion='将临时文件、日志和运行时状态移出长期缓存路径',
                    ))

                nonportable_reason = None
                if not self._is_platform_restricted_condition(cache_info.get('if')):
                    nonportable_reason = self._nonportable_path_reason(path, job)
                if nonportable_reason:
                    smells.append(self.create_smell(
                        smell_type='non_portable_cache_path',
                        severity=SeverityLevel.MEDIUM,
                        message=f'Job "{job_name}" 的跨平台缓存使用了操作系统相关路径',
                        job_name=job_name,
                        step_name=f'缓存步骤 (index: {cache_info.get("index")})',
                        details={
                            'path': path,
                            'reason': nonportable_reason,
                            'platform_families': sorted(self._job_platform_families(job)),
                            'action': cache_info.get('action'),
                        },
                        suggestion='使用按平台解析的缓存目录，或为不同平台使用独立且可移植的缓存配置',
                    ))
        return smells

    def _detect_deprecated_cache_action(
        self,
        job_name: str,
        analysis: Dict,
        workflow_mode: Dict,
    ) -> List[SmellInstance]:
        """按引用类型检测旧缓存服务迁移中明确废弃的版本。"""
        smells = []
        assessments = []
        for cache_info in analysis.get('cache_steps', []):
            action = cache_info.get('action')
            action_path = self._get_action_path(action).lower()
            if action_path not in {'actions/cache', 'actions/cache/restore', 'actions/cache/save'}:
                continue
            verdict = assess_cache_action_reference(action)
            assessments.append({'index': cache_info.get('index'), 'action': action, **verdict})
            if verdict['status'] != 'deprecated':
                continue
            smells.append(self.create_smell(
                smell_type='deprecated_cache_action',
                severity=SeverityLevel.MEDIUM,
                message=f'Job "{job_name}" 使用已废弃的 {action}；该版本依赖旧缓存服务，需升级到支持新服务的版本',
                job_name=job_name,
                step_name=f'缓存步骤 (index: {cache_info.get("index")})',
                details={
                    'action': action,
                    'operation': cache_info.get('operation'),
                    **verdict,
                },
                suggestion='升级到支持新缓存服务的版本；v3 至少使用 v3.4.0，v4 至少使用 v4.2.0。固定 SHA 应对应这些版本或更新版本，并核对运行器兼容性',
            ))
        analysis['cache_action_reference_assessments'] = assessments
        return smells

    def _job_depends_on(self, workflow: WorkflowModel, start: str, target: str) -> bool:
        """判断 Job 是否直接或间接依赖另一个 Job。"""
        if start == target or start not in workflow.jobs or target not in workflow.jobs:
            return False
        pending = list(workflow.jobs[start].needs or [])
        visited = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited or current not in workflow.jobs:
                continue
            visited.add(current)
            pending.extend(workflow.jobs[current].needs or [])
        return False

    def _cache_paths_overlap(self, left: Any, right: Any) -> bool:
        left_items = [value.lower().replace('\\', '/').rstrip('/') for value in self._cache_path_items(left)]
        right_items = [value.lower().replace('\\', '/').rstrip('/') for value in self._cache_path_items(right)]
        for left_item in left_items:
            for right_item in right_items:
                if left_item == right_item or left_item.startswith(right_item + '/') or right_item.startswith(left_item + '/'):
                    return True
        return False

    def _cache_keys_match(self, left: Any, right: Any) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        normalize = lambda value: re.sub(r'\s+', '', value).lower()
        return bool(normalize(left)) and normalize(left) == normalize(right)

    def _job_has_artifact_action(self, job) -> bool:
        return any(
            self._get_action_path(step.uses).lower() in {
                'actions/upload-artifact', 'actions/download-artifact',
            }
            for step in job.steps
        )

    def _job_has_producer_behavior(self, job) -> bool:
        for step in job.steps:
            action_path = self._get_action_path(step.uses).lower()
            run = step.run if isinstance(step.run, str) else ''
            text = f'{action_path}\n{run}\n{step.name}'.lower()
            if re.search(
                r'\b(?:build|compile|package|bundle|goreleaser|docker\s+save|docker\s+build|tar\s|zip\s|lcov|coverage)\b',
                text,
            ):
                return True
        return False

    def _artifact_like_cache_path(self, path: Any) -> bool:
        text = '\n'.join(self._cache_path_items(path)).lower().replace('\\', '/')
        if not text:
            return False
        if re.search(
            r'(?:^|/)(?:dist|release|out|output|coverage|lcov|artifacts?)(?:/|$)'
            r'|\.(?:tar|tar\.gz|tgz|zip|qcow2|iso|img)$'
            r'|(?:^|/)(?:target|bin)/',
            text,
        ):
            return True
        return False

    def _detect_cache_as_artifact(
        self,
        workflow: WorkflowModel,
        jobs_analysis: Dict[str, Dict[str, Any]],
    ) -> List[SmellInstance]:
        """检测通过 cache 在有明确依赖关系的 Job 间传递确定性产物。"""
        smells = []
        seen_transfers = set()
        variants = {}

        def resolved(job_name, job, cache):
            identity = (job_name, cache['index'])
            if identity not in variants:
                variants[identity] = []
                contexts = self._enumerate_matrix_contexts(job.matrix, allow_objects=True)
                if contexts is None or len(contexts) > self._MAX_KEY_MATRIX_COMBINATIONS:
                    contexts = [{}]  # Unresolved expressions remain unresolved, never wildcards.
                step = job.steps[cache['index']]
                for context in contexts:
                    if (self._evaluate_matrix_condition(job.if_, context) is False
                            or self._evaluate_matrix_condition(step.if_, context) is False):
                        continue
                    resolver = ArtifactTransferResolver(workflow, job, context, cache['index'],
                                                        self._condition_is_definitely_false)
                    paths = resolver.paths(cache.get('path'), step)
                    key = resolver.expand(cache.get('key'), step)
                    # A missing scoped env/matrix value is not a cache identity.
                    if not paths or re.search(r'\$\{\{\s*(?:env|matrix|steps)\.', key):
                        continue
                    key = EXPRESSION.sub(lambda m: '${{' + re.sub(r'\s+', '', m[1]) + '}}', key)
                    variants[identity].append({
                        'paths': paths, 'key': key,
                        'artifact_path': any(self._artifact_like_cache_path(path)
                                             and not DEPENDENCY_RESOURCE.search(path) for path in paths),
                        'snapshot_evidence': resolver.snapshot_evidence(paths, step, cache.get('operation')),
                    })
            return variants[identity]
        for consumer_name, consumer_job in workflow.jobs.items():
            consumer_analysis = (jobs_analysis.get(consumer_name) or {}).get('analysis') or {}
            consumer_steps = [
                step for step in consumer_analysis.get('cache_steps', [])
                if not step.get('setup_cache')
                and step.get('operation') in {'restore', 'restore_and_save'}
                and not step.get('lookup_only')
                and not self._condition_is_definitely_false(consumer_job.if_)
                and not self._condition_is_definitely_false(consumer_job.steps[step['index']].if_)
                and step.get('key')
            ]
            if not consumer_steps or not consumer_job.needs:
                continue

            for producer_name, producer_job in workflow.jobs.items():
                if not self._job_depends_on(workflow, consumer_name, producer_name):
                    continue
                if (self._condition_is_definitely_false(producer_job.if_)
                        or self._conditions_are_mutex(producer_job.if_, consumer_job.if_)):
                    continue
                producer_analysis = (jobs_analysis.get(producer_name) or {}).get('analysis') or {}
                producer_steps = [
                    step for step in producer_analysis.get('cache_steps', [])
                    if not step.get('setup_cache')
                    and step.get('operation') in {'save', 'restore_and_save'}
                    and step.get('can_write', True) is not False
                    and not step.get('read_only')
                    and not self._condition_is_definitely_false(producer_job.steps[step['index']].if_)
                    and step.get('key')
                ]
                if not producer_steps:
                    continue

                for consumer_cache in consumer_steps:
                    for producer_cache in producer_steps:
                        matches = [
                            (producer, consumer)
                            for producer in resolved(producer_name, producer_job, producer_cache)
                            for consumer in resolved(consumer_name, consumer_job, consumer_cache)
                            if producer['key'] == consumer['key']
                            and (consumer['artifact_path'] or producer['snapshot_evidence'])
                            and any(CacheConsumptionAnalyzer.overlaps(left, right)
                                    for left in producer['paths'] for right in consumer['paths'])
                        ]
                        if not matches:
                            continue
                        identity = (producer_name, consumer_name, producer_cache['index'], consumer_cache['index'])
                        if identity in seen_transfers:
                            continue
                        seen_transfers.add(identity)
                        key = str(consumer_cache.get('key'))
                        transfer_evidence = []
                        if consumer_cache.get('fail_on_cache_miss') is True or str(consumer_cache.get('fail_on_cache_miss')).lower() == 'true':
                            transfer_evidence.append('fail_on_cache_miss')
                        if re.search(r'\b(?:needs\.|github\.run_id|github\.sha)\b', key, re.IGNORECASE):
                            transfer_evidence.append('workflow_or_run_bound_key')
                        transfer_evidence.extend(sorted({reason for producer, _ in matches
                                                         for reason in producer['snapshot_evidence']}))
                        if any(consumer['artifact_path'] for _, consumer in matches):
                            transfer_evidence.append('artifact_like_path')
                        smells.append(self.create_smell(
                            smell_type='cache_as_artifact',
                            severity=SeverityLevel.MEDIUM,
                            message=f'工作流通过缓存从 Job "{producer_name}" 向 "{consumer_name}" 传递确定性产物',
                            job_name=consumer_name,
                            details={
                                'producer_job': producer_name,
                                'consumer_job': consumer_name,
                                'producer_cache_step_index': producer_cache['index'],
                                'consumer_cache_step_index': consumer_cache['index'],
                                'key': key,
                                'path': consumer_cache.get('path'),
                                'evidence': transfer_evidence,
                                'resolved_producer_paths': sorted({p for producer, _ in matches for p in producer['paths']}),
                                'resolved_consumer_paths': sorted({p for _, consumer in matches for p in consumer['paths']}),
                                'resolved_keys': sorted({producer['key'] for producer, _ in matches}),
                                'needs_relation': list(consumer_job.needs or []),
                            },
                            suggestion='对于同一次工作流中确定性的构建结果或中间文件，优先使用 actions/upload-artifact 与 actions/download-artifact',
                        ))

        # 如果保存步骤的 key/path 明确由名为 artifact_key 的步骤输出提供，
        # 且当前 Job 依赖上游 Job，这已经是较强的“把产物放进 cache”线索，
        # 即使当前 YAML 中没有展开对应的 restore 端也应保留。
        for job_name, job in workflow.jobs.items():
            if not job.needs:
                continue
            analysis = (jobs_analysis.get(job_name) or {}).get('analysis') or {}
            for cache_info in analysis.get('cache_steps', []):
                if (
                    cache_info.get('setup_cache')
                    or cache_info.get('operation') not in {'save', 'restore_and_save'}
                    or cache_info.get('read_only')
                ):
                    continue
                cache_text = f"{cache_info.get('key') or ''}\n{cache_info.get('path') or ''}"
                if not re.search(
                    r'(?:artifact[_-](?:key|path|file)\b|steps\.[A-Za-z_][\w-]*\.outputs\.artifact[_-]?key\b)',
                    cache_text,
                    re.IGNORECASE,
                ):
                    continue
                path_items = self._cache_path_items(cache_info.get('path'))
                if path_items and all(
                    item.strip().lower().replace('\\', '/') in {'build', './build'}
                    for item in path_items
                ):
                    # “artifact” 出现在 key 中并不足以证明整个 build/ 目录
                    # 是跨 Job 传递的确定性产物；这类 key 常用于普通构建缓存。
                    continue
                smells.append(self.create_smell(
                    smell_type='cache_as_artifact',
                    severity=SeverityLevel.MEDIUM,
                    message=f'Job "{job_name}" 使用缓存保存显式命名的工作流产物',
                    job_name=job_name,
                    details={
                        'producer_job': ', '.join(str(value) for value in job.needs),
                        'consumer_job': job_name,
                        'producer_cache_step_index': cache_info.get('index'),
                        'consumer_cache_step_index': None,
                        'key': cache_info.get('key'),
                        'path': cache_info.get('path'),
                        'evidence': ['upstream_job', 'artifact_named_cache'],
                        'needs_relation': list(job.needs or []),
                    },
                    suggestion='对于同一次工作流中确定性的构建结果或中间文件，优先使用 actions/upload-artifact 与 actions/download-artifact',
                ))

        return smells

    def _detect_no_dependency_cache(self, job_name: str, analysis: Dict, workflow_mode: Dict, job=None, workflow=None) -> List[SmellInstance]:
        """检测未缓存的依赖

        改进：
        1. 跳过预热缓存工作流
        2. 跳过辅助工具/一次性安装（非项目主依赖）
        """
        smells = []

        # 预热缓存工作流跳过此检测
        if workflow_mode.get('is_warm_cache'):
            return smells

        # 自定义 setup action 可能封装缓存行为，无法从当前 YAML 验证；
        # 跳过 NDC 报告，但不将其作为缓存证据。
        if analysis.get('nested_actions'):
            return smells

        # 如果所有依赖安装都是辅助工具/全局工具安装，不报异味
        # 原生构建项目（cmake/meson/gradle 等）中的 pip/npm 安装视为测试辅助工具
        is_native_build = any(
            self._NATIVE_BUILD_RE.search(rc or '') for rc in analysis.get('all_run_content', []))
        project_dep_steps = []
        assessments = []
        for original in analysis.get('dep_install_steps', []):
            step = dict(original)
            if job is not None:
                occurrence = sum(a['index'] == step['index'] and a['command'] == step['run'] for a in assessments)
                step['context'] = install_context(self, workflow, job, step['index'], step['run'], occurrence)
            fact = install_fact(step['run'])
            reason = non_project_reason(fact)
            if not reason and self._is_auxiliary_dependency_install(step['run'], is_native_build):
                reason = 'existing_auxiliary_command_policy'
            if not reason and is_native_build and fact and fact['requirements'] and 'context' in step:
                context = step['context']
                cwd = context['working_directory']
                if (cwd and re.search(r'(?:^|/)(?:tests?|ci)(?:/|$)', cwd, re.I)
                        and re.search(r'\b(?:pytest|nosetests|tox)\b', context['original_step_run'])):
                    reason = 'native_test_requirements_in_test_directory'
            assessments.append({'index': step['index'], 'command': step['run'],
                                'context': step.get('context'), 'skip_reason': reason})
            if not reason:
                project_dep_steps.append(step)
        analysis['ndc_install_assessments'] = assessments
        if not project_dep_steps:
            return smells

        uncached_managers = set()
        for dep_step in project_dep_steps:
            manager = dep_step.get('manager', dep_step.get('lang'))
            if any(state.get('status') == 'unknown' and manager in state.get('managers', [])
                   for state in analysis.get('setup_cache_assessments', [])):
                continue  # 无法确认自动缓存开关时，也不能确认该管理器缺少缓存。
            if not (self._is_dependency_manager_cached(manager, analysis)
                    or (job is not None and environment_cache_covers(self, dep_step, analysis, workflow, job))):
                uncached_managers.add(manager)

        if uncached_managers:
            smells.append(self.create_smell(
                smell_type='no_dependency_cache',
                severity=SeverityLevel.HIGH,
                message=f'Job "{job_name}" 存在依赖操作({", ".join(sorted(uncached_managers))})但缺少对应缓存',
                job_name=job_name,
                details={
                    'uncached_langs': sorted(list({
                        dep_step.get('lang')
                        for dep_step in project_dep_steps
                        if dep_step.get('manager', dep_step.get('lang')) in uncached_managers
                    })),
                    'uncached_managers': sorted(list(uncached_managers)),
                    'detected_dep_langs': analysis['dep_langs'],
                    'detected_dep_managers': analysis.get('dep_managers', []),
                },
                suggestion='为依赖操作配置对应的缓存'
            ))

        return smells

    # CI 辅助工具/一次性安装特征（非项目主依赖，无需缓存）
    _AUXILIARY_TOOL_NAMES = {
        # Python CI 工具
        'pre-commit', 'gcovr', 'pytest', 'pyyaml', 'requests', 'flake8', 'mypy',
        'black', 'isort', 'ruff', 'coverage', 'tox', 'nox', 'codecov', 'twine',
        'wheel', 'setuptools', 'uv', 'poetry', 'pipenv',
        # Node CI 工具（全局/临时）
        'yarn', 'js-yaml', 'typescript', 'ts-node', 'esbuild',
    }

    # 原生构建系统特征（出现说明项目主体非 JS/Python，pip 安装多为测试辅助工具）
    _NATIVE_BUILD_RE = re.compile(
        r'\b(cmake|meson|bazel|ctest|gradlew?\s|mvnw?\s|cargo\s+(build|test)|dotnet\s+build)\b', re.I)

    # 全局工具安装模式（仅当项目非 JS/Python 主导时过滤，见 _is_auxiliary_dependency_install）
    _GLOBAL_INSTALL_PATTERNS = [
        r'npm\s+install\s+-g\b',
        r'npm\s+(install|i)\s+-g\b',
        r'yarn\s+global\s+add\b',
        r'pip\s+install\s+--system\b',
        r'uv\s+pip\s+install\b[^\n]*\s--system\b',
    ]

    def _is_auxiliary_dependency_install(self, run_content: str, is_native_build: bool = False) -> bool:
        """Skip a run only when every detected dependency command is auxiliary."""
        commands = self._dependency_commands(run_content)
        return bool(commands) and all(
            self._is_auxiliary_dependency_command(command, is_native_build)
            for command in commands
        )

    def _is_auxiliary_dependency_command(self, run_content: str, is_native_build: bool = False) -> bool:
        """判断依赖安装命令是否是辅助工具/一次性安装（非项目主依赖）

        Args:
            run_content: 依赖安装命令
            is_native_build: job 是否为原生构建项目（cmake/meson/gradle 等），
                此时 pip/npm 安装多为测试辅助工具

        返回 True 表示该命令是辅助工具安装，无需缓存。
        """
        if not run_content:
            return False
        fact = install_fact(run_content)
        if non_project_reason(fact):
            return True
        # Keep dynamic version expressions intact for package-name extraction.
        run_content, _ = protect_expressions(run_content)
        run_lower = run_content.lower()

        if re.search(r'\buv\s+pip\s+install\b', run_lower):
            return bool(re.search(r'\buv\s+pip\s+install\b[^\n]*\s--system\b', run_lower))

        # 0. 原生构建项目：pip/npm 安装多为测试辅助工具，但仍需检查内容
        if is_native_build:
            # 逐行分析 pip install 命令（避免 -r 误匹配 gcovr 的参数）
            pip_named = re.search(
                r'(?:pip|pip3|python\d*\s+-m\s+pip)\s+install\b([^\n]*)', run_lower)
            if pip_named:
                cmd = re.sub(r'\$\{[^}]*\}', '', pip_named.group(1))
                # -r requirements：路径含 CI/测试特征，或同 run 内伴随测试命令时视为辅助
                req_match = re.search(r'-r\s+(\S+)', cmd)
                if req_match:
                    req_path = req_match.group(1)
                    if re.search(r'(ci/|tests?[/_.]|requirements-tests|_tests)', req_path):
                        return True
                    # 同一 run 内容里有测试执行命令（pytest 等）说明这是测试依赖
                    if re.search(r'\b(pytest|nosetests|tox|coverage\s+run)\b', run_lower):
                        return True
                    return False
                # 直接安装包：检查包名是否全为辅助工具
                pkg_names = []
                for token in re.split(r'\s+', cmd.strip()):
                    if not token or token.startswith('-'):
                        continue
                    name = token.split('==')[0].split('@')[0].split('<=')[0].split('>=')[0]
                    if name:
                        pkg_names.append(name)
                if pkg_names and all(p in self._AUXILIARY_TOOL_NAMES for p in pkg_names):
                    return True
            # npm 安装
            npm_named = re.search(r'npm\s+(install|ci|i)\b([^\n]*)', run_lower)
            if npm_named:
                cmd = npm_named.group(2).strip()
                if cmd and not cmd.startswith('-'):
                    cmd = re.sub(r'\$\{[^}]*\}', '', cmd)
                    pkg_names = []
                    for token in re.split(r'\s+', cmd):
                        if not token or token.startswith('-'):
                            continue
                        name = token.split('@')[0].split('==')[0]
                        if name:
                            pkg_names.append(name)
                    if pkg_names and all(p in self._AUXILIARY_TOOL_NAMES for p in pkg_names):
                        return True
            return False

        # 1. pip install <工具名>：安装的是已知 CI 辅助工具（gcovr、pytest 等）
        pip_install = re.search(
            r'(?:pip|pip3|python\d*\s+-m\s+pip)\s+install\b([^\n]*)', run_lower)
        if pip_install:
            cmd = pip_install.group(1)
            # 排除 -r requirements.txt / -e 项目依赖
            if '-r ' in cmd or 'requirements' in cmd or ' -e ' in cmd or ' .' in cmd:
                return False
            # 提取安装的包名（install 之后的部分），去掉 flags 和表达式
            cmd = re.sub(r'\$\{[^}]*\}', '', cmd)  # 移除 ${{ }} 表达式
            pkg_names = []
            for token in re.split(r'\s+', cmd.strip()):
                if not token:
                    continue
                if token.startswith('-'):
                    continue
                # 去掉版本号后缀（== 或 @）
                name = token.split('==')[0].split('@')[0].split('<=')[0].split('>=')[0]
                if name:
                    pkg_names.append(name)
            if pkg_names and all(p in self._AUXILIARY_TOOL_NAMES for p in pkg_names):
                return True

        # 2. npm install <工具名>：一次性脚本工具（js-yaml 等），仅当非项目依赖
        npm_install = re.search(r'npm\s+install\b([^\n]*)', run_lower)
        if npm_install:
            cmd = npm_install.group(1).strip()
            # 排除 package.json 安装（npm ci / npm install 无参数 / --save-dev 项目依赖）
            if not cmd or '--save-dev' in cmd or '--save' in cmd or '-D' in cmd:
                return False
            # 单个包名且是辅助工具
            cmd = re.sub(r'\$\{[^}]*\}', '', cmd)
            pkg_names = []
            for token in re.split(r'\s+', cmd):
                if not token:
                    continue
                if token.startswith('-'):
                    continue
                name = token.split('==')[0].split('@')[0].split('<=')[0].split('>=')[0]
                if name:
                    pkg_names.append(name)
            if pkg_names and all(p in self._AUXILIARY_TOOL_NAMES for p in pkg_names):
                return True

        return False

    def _restore_key_entries(self, restore_keys: Any) -> List[str]:
        """将 ``restore-keys`` 标准化为可比较的非空条目。"""
        if isinstance(restore_keys, str):
            values = restore_keys.splitlines()
        elif isinstance(restore_keys, (list, tuple)):
            values = restore_keys
        else:
            return []

        return [
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        ]

    def _ineffective_restore_key_entries(self, key: Any, restore_keys: Any) -> List[str]:
        """返回与主 key 完全相同的 restore-keys 条目。

        ``restore-keys`` 通过前缀匹配提供回退。与主 key 完全相同的条目
        不会扩大匹配范围，因此与未提供有效回退具有相同的检测语义。
        这里只做静态、字面量比较，不执行或猜测表达式的运行时值。
        """
        if not isinstance(key, str) or not key.strip():
            return []
        normalized_key = key.strip()
        return [
            entry
            for entry in self._restore_key_entries(restore_keys)
            if entry == normalized_key
        ]

    def _detect_no_restore_keys(self, job_name: str, analysis: Dict, workflow_mode: Dict, workflow: Optional[WorkflowModel] = None) -> List[SmellInstance]:
        """逐恢复步骤检测缺少或无效的 restore-keys。

        lookup-only 只豁免对应的查询步骤；资源名称、工作流用途和共享
        key 数量不改变恢复步骤需要回退的标注口径。仅保存操作和 setup
        内置缓存不在本规则的显式 restore-keys 检查范围内。
        """
        smells = []

        for cs in analysis['cache_steps']:
            if cs.get('setup_cache'):
                continue
            lookup_only = cs.get('lookup_only')
            if lookup_only is True or (
                isinstance(lookup_only, str) and lookup_only.strip().lower() == 'true'
            ):
                continue
            if (
                self._is_exact_action(cs.get('action', ''), 'actions/cache/save')
                or self._is_exact_action(cs.get('action', ''), 'actions/save')
            ):
                continue

            key = cs.get('key')
            restore_keys = cs.get('restore_keys')
            restore_key_entries = self._restore_key_entries(restore_keys)
            ineffective_restore_keys = self._ineffective_restore_key_entries(key, restore_keys)
            effective_restore_keys = [
                entry for entry in restore_key_entries
                if isinstance(key, str)
                and entry != key.strip()
                and key.strip().startswith(entry)
            ]
            has_no_effective_fallback = (
                not restore_key_entries
                or (bool(ineffective_restore_keys) and not effective_restore_keys)
            )
            if key and has_no_effective_fallback:
                has_static_key = isinstance(key, str) and not key.startswith('${{')
                severity = SeverityLevel.MEDIUM if has_static_key else SeverityLevel.LOW
                subtype = (
                    'ineffective_restore_key'
                    if ineffective_restore_keys
                    else 'missing_restore_keys'
                )
                if ineffective_restore_keys:
                    message = f'Job "{job_name}" 缓存步骤的 restore-keys 与主 key 完全相同，未提供有效回退'
                    suggestion = '将 restore-keys 改为主 key 的更宽前缀，或删除无效的重复条目'
                else:
                    message = f'Job "{job_name}" 缓存步骤缺少 restore-keys 回退'
                    suggestion = '添加 restore-keys 参数以提供回退缓存'

                smells.append(self.create_smell(
                    smell_type='no_restore_keys',
                    severity=severity,
                    message=message,
                    job_name=job_name,
                    details={
                        'action': cs['action'],
                        'key': key,
                        'has_static_key': has_static_key,
                        'subtype': subtype,
                        'restore_keys': restore_key_entries,
                        'effective_restore_keys': effective_restore_keys,
                        'ineffective_restore_keys': ineffective_restore_keys,
                    },
                    suggestion=suggestion
                ))

        return smells

    def _cache_resource_types(self, cache_path: Any) -> Set[str]:
        """根据缓存路径识别缓存资源，避免把工具缓存当成依赖缓存。"""
        if not cache_path:
            return set()

        path = str(cache_path).lower().replace('\\', '/')
        resources = set()
        resource_patterns = {
            'npm': (
                '.npm', 'node_modules', 'pnpm/store', '.cache/yarn',
                'npm-cache', 'yarn-cache', 'pnpm-cache',
            ),
            'python': (
                '.cache/pip', '.pip/cache', '.cache/pypoetry',
                '.local/share/virtualenvs', 'pythonlocation', 'pip-cache', '.venv',
            ),
            'playwright': ('playwright', 'pw-browsers', 'puppeteer'),
            'precommit': ('pre-commit', 'prek'),
            'go': ('go/pkg/mod', '.cache/go', 'go-build'),
            'go_bin': ('go/bin',),
            'rust': ('.cargo', 'rust/target', '/target'),
            'bazel': ('.bazel/cache', 'bazel-out'),
            'java': ('.gradle', '.m2/repository', 'maven'),
            'build': ('/build', '/dist', '/out', '/output', 'build/p'),
            'artifact': ('schema.sql', 'release_notes'),
        }
        for resource, patterns in resource_patterns.items():
            if any(pattern in path for pattern in patterns):
                resources.add(resource)

        # 动态 npm cache 输出通常无法展开，但变量名本身仍然提供可靠类型信息。
        if re.search(r'steps\.[^}]*npm[^}]*outputs\.', path):
            resources.add('npm')
        return resources

    def _cache_key_requires_checkout(self, key: Any) -> bool:
        """判断缓存 key 是否明确依赖 checkout 后的仓库内容。"""
        if not isinstance(key, str):
            return False
        return bool(re.search(
            r'hashfiles\s*\(|github\.(?:sha|ref|ref_name|head_ref)\b',
            key,
            re.IGNORECASE,
        ))

    def _condition_is_definitely_false(self, condition: Any) -> bool:
        """只过滤字面量 false；复杂条件保持保守。"""
        if not isinstance(condition, str):
            return condition is False
        normalized = condition.strip().lower()
        normalized = re.sub(r'^\$\{\{\s*|\s*\}\}$', '', normalized).strip()
        return normalized == 'false'

    def _cache_matches_dependency(self, resources: Set[str], lang: str) -> bool:
        """判断缓存资源是否与依赖安装生态匹配。"""
        if lang == 'npm':
            return 'npm' in resources
        if lang == 'python':
            return 'python' in resources
        return False

    def _is_package_manager_bootstrap(self, run_content: Any) -> bool:
        """过滤只升级 pip 本身的引导步骤。"""
        if not isinstance(run_content, str):
            return False
        return bool(re.search(
            r'(?:python\d*\s+-m\s+)?pip\s+install\s+(?:[^\n]*\s)?'
            r'(?:-u|--upgrade)\s+pip(?:[<>=!~][^\s]*)?\s*$',
            run_content.strip().lower(),
        ))

    def _relevant_dependency_steps(
        self,
        analysis: Dict,
        cache_path: Any,
    ) -> List[Dict[str, Any]]:
        """仅返回真正可能使用该缓存资源的依赖安装步骤。"""
        resources = self._cache_resource_types(cache_path)
        if not resources:
            return []
        return [
            dep_step for dep_step in analysis.get('dep_install_steps', [])
            if not self._is_package_manager_bootstrap(dep_step.get('run'))
            if self._cache_matches_dependency(resources, dep_step.get('lang', ''))
        ]

    def _cache_has_prior_producer(self, job, cache_info: Dict[str, Any], workflow: Optional[WorkflowModel] = None, analysis: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """识别恢复前已经生成缓存内容的高置信度步骤。"""
        cache_index = cache_info.get('index', -1)
        resources = self._cache_resource_types(cache_info.get('path'))
        analyzer = CacheConsumptionAnalyzer(workflow, job)
        paths, _ = analyzer.cache_paths(cache_info.get('path'), job.steps[cache_index])
        resources.update(self._cache_resource_types('\n'.join(paths)))

        for index, step in enumerate(job.steps[:cache_index]):
            if self._condition_is_definitely_false(step.if_):
                continue
            if self._conditions_are_mutex(step.if_, job.steps[cache_index].if_):
                continue
            run = step.run if isinstance(step.run, str) else ''
            run_lower = run.lower()
            action = self._get_action_path(step.uses).lower()

            written = [item for item in explicit_file_outputs(workflow, job, step, index)
                       if any(analyzer.overlaps(cached, item['path'])
                              and not cached.startswith(item['path'].rstrip('/') + '/')
                              for cached in paths)]
            if written:
                return {'index': index, 'kind': 'explicit_file_output',
                        'output_paths': [item['path'] for item in written],
                        'association': 'explicit_write_destination'}

            output = analyzer.js_build_outputs(step, index)
            if output['is_build']:
                matching = [p for p in output['paths'] if any(analyzer.overlaps(p, cached) for cached in paths)]
                evidence = {
                    'cache_step_index': cache_index, 'step_index': index,
                    'status': 'produced' if matching else 'unknown' if output['uncertain'] else 'unrelated',
                    'output_paths': output['paths'], 'matching_paths': matching,
                    'reason': output['reason'],
                }
                if analysis is not None:
                    analysis.setdefault('cache_producer_assessments', []).append(evidence)
                if matching:
                    return {'index': index, 'kind': 'js_build_output',
                            'action': run[:160], 'output_paths': matching,
                            'association': 'explicit_output_argument'}

            if 'go_bin' in resources and re.search(r'\bgo\s+install\b', run_lower):
                return {'index': index, 'kind': 'go_install', 'action': step.uses or run[:160]}
            if 'bazel' in resources and re.search(r'git-sync-deps|bazel.*fetch', run_lower):
                return {'index': index, 'kind': 'external_source_sync', 'action': step.uses or run[:160]}
            if 'rust' in resources and re.search(
                r'rust:rbs:sync|cargo\s+(?:fetch|build|check|test)', run_lower,
            ):
                return {'index': index, 'kind': 'rust_source_or_build', 'action': step.uses or run[:160]}
            if 'build' in resources or 'artifact' in resources:
                if 'goreleaser' in action or re.search(r'\bgoreleaser\b|go\s+build\b', run_lower):
                    return {'index': index, 'kind': 'build_output', 'action': step.uses or run[:160]}
        return None

    def _step_consumes_cache_resource(
        self, step, cache_path: Any, resources: Set[str],
        workflow: Optional[WorkflowModel] = None, job=None, step_index: int = 0,
    ) -> Dict[str, Any]:
        """返回资源消费三状态及理由；未展开的脚本/Action 不能证明没有消费者。"""
        analyzer = CacheConsumptionAnalyzer(workflow, job)
        paths, partial = analyzer.cache_paths(cache_path)
        result = analyzer.step(step, paths, step_index)
        if partial and result['status'] == NO_CONSUMER:
            result = {'status': UNKNOWN, 'reasons': ['cache_path_partially_unresolved']}
        return result

    def _detect_cache_position_error(self, job_name: str, analysis: Dict, job, workflow: WorkflowModel, workflow_mode: Dict) -> List[SmellInstance]:
        """检测缓存位置错误，并将缓存与实际资源消费者/生产者关联。"""
        smells = []

        if self._condition_is_definitely_false(job.if_):
            return smells

        checkout_idx = analysis.get('checkout_idx')
        dep_install_indices = analysis.get('dep_install_indices', [])
        cache_by_index = {
            cache.get('index'): cache
            for cache in analysis.get('cache_steps', [])
        }

        for cs_idx in analysis.get('cache_step_indices', []):
            cache_info = cache_by_index.get(cs_idx)
            if not cache_info:
                continue
            cache_step = job.steps[cs_idx]
            if self._condition_is_definitely_false(cache_step.if_):
                continue

            action = cache_info.get('action', '')
            cache_action = self._classify_cache_action(action)
            fields = {name: (cache_step.with_ or {}).get(name)
                      for name in ('key', 'path', 'restore-keys', 'cache-dependency-path')}
            inputs = future_cache_inputs(job, cs_idx, fields, self._condition_is_definitely_false)
            if inputs:
                smells.append(self.create_smell(
                    smell_type='cache_position_error', severity=SeverityLevel.HIGH,
                    message=f'Job "{job_name}" 缓存输入引用了后续步骤才产生的输出',
                    job_name=job_name,
                    details={'error_type': 'before_cache_input_producer', 'action': action,
                             'cache_step_index': cs_idx, 'input_dependencies': inputs},
                    suggestion='将缓存输入的生产步骤放在缓存之前，或调整输入来源',
                ))
            if cache_info.get('setup_cache'):
                dependency = setup_checkout_inputs(
                    workflow, job, cs_idx, cache_info, self._condition_is_definitely_false,
                )
                if dependency:
                    smells.append(self.create_smell(
                        smell_type='cache_position_error', severity=SeverityLevel.HIGH,
                        message=f'Job "{job_name}" setup 缓存需要仓库依赖文件但位于 checkout 之前',
                        job_name=job_name,
                        details={'error_type': 'before_checkout', 'action': action,
                                 'cache_step_index': cs_idx, **dependency},
                        suggestion='先 checkout 提供依赖文件，再执行启用缓存的 setup 步骤',
                    ))
                continue
            if workflow_mode.get('is_warm_cache'):
                continue
            if not cache_action or cache_action['operation'] == 'save':
                continue
            if cache_info.get('lookup_only') or cache_info.get('fail_on_cache_miss'):
                continue

            cache_path = cache_info.get('path', '')
            resources = self._cache_resource_types(cache_path)
            resolved_paths, _ = CacheConsumptionAnalyzer(workflow, job).cache_paths(
                cache_path, job.steps[cs_idx],
            )
            resources.update(self._cache_resource_types('\n'.join(resolved_paths)))

            # 只有 key 明确依赖仓库内容时，checkout 前恢复才是高置信度错误。
            if (
                checkout_idx is not None
                and cs_idx < checkout_idx
                and self._cache_key_requires_checkout(cache_info.get('key'))
            ):
                smells.append(self.create_smell(
                    smell_type='cache_position_error',
                    severity=SeverityLevel.HIGH,
                    message=f'Job "{job_name}" 缓存步骤位于 checkout 之前且 key 依赖仓库内容',
                    job_name=job_name,
                    details={
                        'action': action,
                        'error_type': 'before_checkout',
                        'key': cache_info.get('key'),
                        'path': cache_path,
                        'cache_resources': sorted(resources),
                    },
                    suggestion='将依赖仓库文件的缓存恢复步骤移到 checkout 之后',
                ))

            # 对 Go/Bazel/Rust/构建产物等资源，优先识别恢复前的真实生产者。
            producer = self._cache_has_prior_producer(job, cache_info, workflow, analysis)
            if producer:
                smells.append(self.create_smell(
                    smell_type='cache_position_error',
                    severity=SeverityLevel.MEDIUM,
                    message=f'Job "{job_name}" 缓存恢复步骤位于缓存内容生成之后',
                    job_name=job_name,
                    details={
                        'action': action,
                        'error_type': 'after_cache_producer',
                        'cache_step_index': cs_idx,
                        'producer': producer,
                        'path': cache_path,
                        'cache_resources': sorted(resources),
                    },
                    suggestion='将缓存恢复步骤移到对应缓存内容生成步骤之前',
                ))
                continue

            # 依赖安装位置只对资源类型和语言都能对应的缓存生效。
            relevant_dep_steps = self._relevant_dependency_steps(
                analysis, '\n'.join([str(cache_path), *resolved_paths]),
            )
            if relevant_dep_steps and any(cs_idx > step['index'] for step in relevant_dep_steps):
                is_setup_cache = cache_info.get('setup_cache', False)
                if not is_setup_cache:
                    smells.append(self.create_smell(
                        smell_type='cache_position_error',
                        severity=SeverityLevel.MEDIUM,
                        message=f'Job "{job_name}" 缓存恢复步骤位于对应依赖安装之后',
                        job_name=job_name,
                        details={
                            'action': action,
                            'error_type': 'after_dependency_install',
                            'dependency_steps': [
                                {'index': step['index'], 'lang': step['lang']}
                                for step in relevant_dep_steps
                                if cs_idx > step['index']
                            ],
                            'path': cache_path,
                            'cache_resources': sorted(resources),
                        },
                        suggestion='将对应依赖缓存的恢复步骤移到依赖安装之前',
                    ))
                    continue

            # 仅对 Runner 上的构建产物/中间产物缓存检查 no_consumer。
            # 依赖、工具和语言缓存往往由 make、脚本或第三方 action 间接消费，
            # 仅凭后续命令文本无法证明“没有消费者”，否则会产生大量误报。
            if not resources.intersection({'build', 'artifact'}):
                continue
            consumer = CacheConsumptionAnalyzer(workflow, job).summarize(
                cs_idx, cache_path, self._condition_is_definitely_false,
            )
            if cache_action['operation'] == 'restore_and_save' and consumer['status'] == NO_CONSUMER:
                # 完整 cache Action 还会在 post 阶段保存；恢复后无读取不能证明整个步骤无用途。
                consumer = {
                    **consumer,
                    'status': UNKNOWN,
                    'restore_consumer_status': NO_CONSUMER,
                    'reasons': [*consumer['reasons'], 'combined_cache_save_purpose_unknown'],
                }
            assessment = {'cache_step_index': cs_idx, 'path': cache_path, **consumer}
            # A missing read alone is an unused-restore risk, not an ordering error.
            # Only an identified earlier consumer establishes a misplaced restore.
            earlier_consumers = []
            if consumer['status'] == NO_CONSUMER:
                analyzer = CacheConsumptionAnalyzer(workflow, job)
                for index, step in enumerate(job.steps[:cs_idx]):
                    if (self._condition_is_definitely_false(step.if_)
                            or self._conditions_are_mutex(step.if_, cache_step.if_)):
                        continue
                    if analyzer.step(step, resolved_paths, index)['status'] == CONSUMED:
                        earlier_consumers.append(index)
            assessment['position_error'] = bool(earlier_consumers)
            assessment['earlier_consumer_step_indices'] = earlier_consumers
            assessment['unused_restore_risk'] = consumer['status'] == NO_CONSUMER and not earlier_consumers
            analysis.setdefault('cache_consumer_assessments', []).append(assessment)
            if consumer['status'] == NO_CONSUMER and earlier_consumers:
                smells.append(self.create_smell(
                    smell_type='cache_position_error',
                    severity=SeverityLevel.MEDIUM,
                    message=f'Job "{job_name}" 缓存恢复晚于已识别的资源消费，且没有后续消费者',
                    job_name=job_name,
                    details={
                        'action': action,
                        'error_type': 'no_consumer',
                        'path': cache_path,
                        'cache_resources': sorted(resources),
                        'consumer_assessment': consumer,
                        'earlier_consumer_step_indices': earlier_consumers,
                    },
                    suggestion='确认缓存恢复后存在使用该缓存目录的构建、测试或依赖安装步骤',
                ))

        return smells

    def _has_cache_consumer(self, job, cache_step_idx: int, cache_info: Optional[Dict[str, Any]] = None) -> bool:
        """兼容布尔接口：未知也不能视为确定没有消费者。"""
        cache_info = cache_info or {'path': ''}
        result = CacheConsumptionAnalyzer(job=job).summarize(
            cache_step_idx, cache_info.get('path'), self._condition_is_definitely_false,
        )
        return result['status'] != NO_CONSUMER

    def _uses_cache_in_action(self, with_config: Dict[str, Any]) -> bool:
        """检查 action 的 with 参数中是否使用缓存

        Args:
            with_config: 步骤的 with 配置

        Returns:
            True 如果使用了缓存，False 否则
        """
        # 缓存相关的参数名
        cache_params = [
            'cache-from',
            'cache-to',
            'cache_from',
            'cache_to',
            'cache',
            'cache-from-sources',
            'cache-to-dir',
            'cache-dir',
        ]

        # 检查参数名
        for param in cache_params:
            if param in with_config:
                # 参数存在且不为 False/空
                value = with_config[param]
                if value is not None and value is not False and value != '':
                    # 对于字符串值，检查是否包含 'cache' 或有效路径
                    if isinstance(value, str):
                        # 排除明显非缓存的值
                        if value.lower() in ['false', 'none', 'null', '0']:
                            continue
                    return True

        # 检查参数值中是否包含缓存关键字
        for key, value in with_config.items():
            if isinstance(value, str):
                value_lower = value.lower()
                # 检查是否包含缓存相关的模式
                if 'cache-from' in value_lower or 'cache-to' in value_lower:
                    return True
                # 检查是否是缓存路径引用
                if 'type=local' in value_lower and ('src=' in value_lower or 'dest=' in value_lower):
                    return True

        return False

    def _detect_redundant_cache_update(self, workflow: WorkflowModel) -> List[SmellInstance]:
        """检测实际可能并行写入同一缓存 key 的 Job 或 matrix 实例。"""
        smells = []
        key_writers = {}
        matrix_reported = set()

        for job_name, job in workflow.jobs.items():
            if self._is_read_only_cache_job(job_name, job):
                continue
            matrix_contexts = self._enumerate_matrix_contexts(job.matrix, allow_objects=True)
            if matrix_contexts is None or len(matrix_contexts) > self._MAX_KEY_MATRIX_COMBINATIONS:
                # Still compare matrix-independent writers across jobs. Unknown
                # axes cannot establish an intra-job conflict.
                matrix_contexts = [{}]

            for step_index, step in enumerate(job.steps):
                cache_action = self._classify_cache_action(step.uses)
                if not cache_action or not cache_action['can_write']:
                    continue
                if self._is_read_only_cache_step(step):
                    continue
                step_with = step.with_ or {}
                key = step_with.get('key')
                if not key:
                    continue

                instances = []
                for matrix_context in matrix_contexts:
                    if not matrix_context and job.matrix and any(
                        'matrix.' in str(value or '') for value in (
                            key, job.runs_on, job.if_, step.if_,
                        )
                    ):
                        continue
                    job_state = self._evaluate_matrix_condition(
                        getattr(job, 'if_', None), matrix_context,
                    )
                    step_state = self._evaluate_matrix_condition(
                        getattr(step, 'if_', None), matrix_context,
                    )
                    if job_state is False or step_state is False:
                        continue
                    resolved_key = self._resolve_cache_key_for_context(
                        key, job_name, job, matrix_context, step,
                        allow_python_location='pythonLocation' not in (workflow.env or {}),
                        resolve_matrix_arguments=True,
                    )
                    if resolved_key is None:
                        # 未知表达式仍然保持保守：不将该 writer 加入可证明的冲突集合。
                        continue
                    instances.append({
                        'job': job_name,
                        'step_index': step_index,
                        'key': key,
                        'resolved_key': resolved_key,
                        'matrix_context': dict(matrix_context),
                        'condition_state': 'unknown' if job_state is None or step_state is None else 'true',
                        'path': step_with.get('path'),
                    })

                by_key = {}
                for instance in instances:
                    by_key.setdefault(instance['resolved_key'], []).append(instance)
                    key_writers.setdefault(instance['resolved_key'], []).append(instance)

                # 同一 Job 的不同 matrix 实例只要确实都执行 cache writer，且 key 相同，
                # 就是矩阵内部的更新冲突。
                for resolved_key, same_key_instances in by_key.items():
                    contexts = {
                        json.dumps(instance['matrix_context'], sort_keys=True)
                        for instance in same_key_instances
                    }
                    if len(contexts) <= 1 or not job.matrix:
                        continue
                    if (job.strategy or {}).get('max-parallel') == 1:
                        continue
                    relevant_matrix_variables = self._extract_matrix_variables(job.matrix)
                    report_key = (job_name, step_index, resolved_key)
                    if report_key in matrix_reported:
                        continue
                    matrix_reported.add(report_key)
                    smells.append(self.create_smell(
                        smell_type='redundant_cache_update',
                        severity=SeverityLevel.MEDIUM,
                        message=f'Job "{job_name}" 的多个 matrix 实例会写入相同缓存 key',
                        job_name=job_name,
                        details={
                            'key': key,
                            'resolved_keys': [resolved_key],
                            'matrix_variables': sorted(self._extract_matrix_variables(job.matrix)),
                            'relevant_matrix_variables': sorted(relevant_matrix_variables),
                            'matrix_contexts': [
                                instance['matrix_context'] for instance in same_key_instances
                            ],
                            'path': step_with.get('path'),
                            'conflict_type': 'matrix_without_var',
                        },
                        suggestion='在缓存 key 中添加影响缓存内容的 matrix 变量'
                    ))

        # 对同一个 resolved key 构造“可能同时执行”的 Job 冲突图。
        for resolved_key, instances in key_writers.items():
            graph = {}
            edge_instances = {}
            for index, left in enumerate(instances):
                for right in instances[index + 1:]:
                    if left['job'] == right['job']:
                        continue
                    if not self._cache_instances_may_overlap(workflow, left, right):
                        continue
                    pair = tuple(sorted((left['job'], right['job'])))
                    graph.setdefault(pair[0], set()).add(pair[1])
                    graph.setdefault(pair[1], set()).add(pair[0])
                    edge_instances[pair] = (left, right)

            visited = set()
            for start in sorted(graph):
                if start in visited:
                    continue
                component = set()
                pending = [start]
                while pending:
                    current = pending.pop()
                    if current in visited:
                        continue
                    visited.add(current)
                    component.add(current)
                    pending.extend(graph.get(current, set()) - visited)
                if len(component) <= 1:
                    continue
                job_names = sorted(component)
                if all(self._is_lint_or_review_job(name, workflow.jobs[name]) for name in job_names):
                    continue
                if self._are_build_and_package_jobs(job_names, workflow):
                    continue

                component_instances = [
                    instance for instance in instances if instance['job'] in component
                ]
                smells.append(self.create_smell(
                    smell_type='redundant_cache_update',
                    severity=SeverityLevel.MEDIUM,
                    message=f'并行 Job({", ".join(job_names)})写入相同缓存 key，可能导致缓存更新冲突',
                    job_name=', '.join(job_names),
                    details={
                        'resolved_key': resolved_key,
                        'keys': {
                            name: sorted({
                                instance['key'] for instance in component_instances
                                if instance['job'] == name
                            })
                            for name in job_names
                        },
                        'jobs': job_names,
                        'writers': component_instances,
                        'conflict_type': 'resolved_key',
                    },
                    suggestion='为不同 Job 使用不同的缓存 key，或使用 needs 设置依赖关系使其串行执行'
                ))
        return smells

    def _cache_instances_may_overlap(
        self,
        workflow: WorkflowModel,
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> bool:
        """判断两个已经写入相同 key 的缓存实例是否可能同时执行。"""
        if left['job'] == right['job']:
            return False
        if self._are_jobs_sequential(workflow, [left['job'], right['job']]):
            return False
        if self._are_jobs_mutex(
            workflow.jobs[left['job']], workflow.jobs[right['job']],
        ):
            return False
        return True

    def _extract_matrix_variables(self, matrix_config: Dict[str, Any]) -> Set[str]:
        """从 matrix 配置中提取变量名

        排除特殊的 matrix 配置键：include, exclude；同时纳入 include-only
        矩阵中只在 include 项出现的变量。
        """
        if not isinstance(matrix_config, dict):
            return set()

        # matrix 的顶层键就是变量名，但排除特殊配置键
        special_keys = {'include', 'exclude'}
        variables = set(matrix_config.keys()) - special_keys
        include = matrix_config.get('include')
        if isinstance(include, list):
            variables.update(
                name for item in include
                if isinstance(item, dict)
                for name in item
            )
        return variables

    def _matrix_variables_affecting_cache(
        self,
        job,
        step,
        instances: List[Dict[str, Any]],
    ) -> Set[str]:
        """筛选可能改变缓存内容的 matrix 变量，过滤纯工作负载维度。"""
        variables = self._extract_matrix_variables(job.matrix)
        varying = {
            name for name in variables
            if len({instance['matrix_context'].get(name) for instance in instances}) > 1
        }
        paths = ' '.join(str(instance.get('path') or '').lower() for instance in instances)
        if any(token in paths for token in (
            '.keras', 'huggingface', 'torch', 'tensorflow', 'datasets', 'mnist',
        )):
            # 数据集/模型缓存通常由每个矩阵实例填充；即使矩阵轴是策略或场景，
            # 多个并行 writer 仍可能重复更新同一个缓存。
            return varying
        relevant = set()
        workload_tokens = (
            'shard', 'worker', 'partition', 'chunk', 'framework', 'configuration',
            'kubernetes', 'node_image', 'boot_type', 'buildname', 'format',
            'compression', 'use_config', 'scenario', 'test_type', 'config_type',
        )
        runtime_tokens = (
            'python', 'node', 'npm', 'pnpm', 'yarn', 'java', 'jdk', 'go',
            'rust', 'ruby', 'php', 'dotnet', 'compiler', 'toolchain',
            'runtime', 'version', 'arch', 'platform', 'os', 'abi',
        )
        for name in varying:
            lowered = name.lower()
            if any(token in lowered for token in workload_tokens):
                continue
            if lowered == 'os':
                # 固定 runner 上的发布目标（如 win-x64/linux-x64）不等于实际 runner。
                if isinstance(job.runs_on, str) and not re.fullmatch(
                    r'\${{\s*matrix\.[\w-]+\s*}}', job.runs_on,
                ):
                    values = [str(instance['matrix_context'].get(name, '')).lower() for instance in instances]
                    if values and not all(value.startswith(('ubuntu', 'windows', 'macos')) for value in values):
                        continue
            if any(token in lowered for token in runtime_tokens):
                relevant.add(name)
        return relevant

    def _get_matrix_vars_in_key(self, key: str, matrix_vars: Set[str]) -> Set[str]:
        """检查缓存 key 中使用了哪些 matrix 变量"""
        found_vars = set()

        for var in matrix_vars:
            # 检查是否使用 matrix 变量，如 ${{ matrix.python-version }}
            # 注意：使用 {{ 而不是 ${{ 因为 key 中已经包含了 $ 前缀
            pattern = rf'\{{\s*matrix\.{re.escape(var)}\s*\}}'
            if re.search(pattern, key):
                found_vars.add(var)

        return found_vars

    def _are_jobs_sequential(self, workflow: WorkflowModel, job_names: List[str]) -> bool:
        """检查多个 Job 是否位于同一 needs 依赖链。"""
        if len(job_names) <= 1:
            return True

        def depends_on(start_name: str, target_name: str) -> bool:
            pending = list(workflow.jobs[start_name].needs)
            seen = set()
            while pending:
                current = pending.pop()
                if current == target_name:
                    return True
                if current in seen or current not in workflow.jobs:
                    continue
                seen.add(current)
                pending.extend(workflow.jobs[current].needs)
            return False

        return all(
            depends_on(name1, name2) or depends_on(name2, name1)
            for index, name1 in enumerate(job_names)
            for name2 in job_names[index + 1:]
        )

    def _contains_matrix_var(self, key: str) -> bool:
        """检查缓存 key 是否包含 matrix 变量

        Args:
            key: 缓存 key

        Returns:
            True 如果 key 包含 matrix 变量，False 否则
        """
        if not key:
            return False
        return bool(re.search(r'\${{\s*matrix\.', key))

    def _contains_unresolved_env_var(self, key: str) -> bool:
        """检查缓存 key 是否包含未解析的环境变量

        环境变量的值在不同 Job 中可能不同，因此无法静态判断是否冲突。
        例如: ${{ env.CONFIG_NAME }} 在 Android Job 中是 'Android_Debug'，在 Windows Job 中是 'Debug'

        Args:
            key: 缓存 key

        Returns:
            True 如果 key 包含未解析的环境变量，False 否则
        """
        if not key:
            return False
        return bool(re.search(r'\${{\s*env\.', key))

    _KEY_EXPRESSION_RE = re.compile(r'\${{\s*(.*?)\s*}}')
    _MAX_KEY_MATRIX_COMBINATIONS = 32

    def _expand_cache_key(self, key: str, job_name: str, job) -> Optional[tuple]:
        """安全展开可由静态 YAML 上下文确定的缓存 key。"""
        if not isinstance(key, str):
            return None
        matrix_contexts = self._enumerate_matrix_contexts(job.matrix)
        if matrix_contexts is None or len(matrix_contexts) > self._MAX_KEY_MATRIX_COMBINATIONS:
            return None

        resolved_keys = set()
        active_instances = 0
        for matrix_context in matrix_contexts:
            if self._evaluate_matrix_condition(getattr(job, 'if_', None), matrix_context) is False:
                continue
            resolved = self._resolve_cache_key_for_context(
                key, job_name, job, matrix_context,
            )
            if resolved is None:
                return None
            resolved_keys.add(resolved)
            active_instances += 1
        return resolved_keys, active_instances

    def _resolve_cache_key_for_context(
        self,
        key: str,
        job_name: str,
        job,
        matrix_context: Dict[str, Any],
        step=None,
        allow_python_location: bool = False,
        resolve_matrix_arguments: bool = False,
    ) -> Optional[str]:
        """在一个确定的 Job/matrix 上下文中展开缓存 key。"""
        matrix_names = self._extract_matrix_variables(job.matrix) if job.matrix else set()

        def replace_expression(match):
            raw_expression = match.group(1).strip()
            expression = re.sub(r'\s+', '', raw_expression).lower()
            if expression == 'github.job':
                return job_name
            if expression == 'runner.os':
                runner_os = self._resolve_runner_os(job.runs_on, matrix_context)
                if runner_os is None:
                    raise ValueError
                return runner_os
            if expression == 'runner.arch':
                runner_arch = self._resolve_runner_arch(job.runs_on, matrix_context)
                if runner_arch is None:
                    raise ValueError
                return runner_arch
            if expression.startswith('matrix.'):
                variable = expression.split('.', 1)[1]
                if variable.split('.', 1)[0] not in matrix_names:
                    raise ValueError
                value = self._matrix_context_value(matrix_context, variable)
                if value is None or isinstance(value, (dict, list)):
                    raise ValueError
                return str(value)
            if expression.startswith('env.'):
                variable = raw_expression.split('.', 1)[1].strip()
                value = self._resolve_env_value(variable, job, None, matrix_context, step)
                if value is None and allow_python_location and variable.lower() == 'pythonlocation':
                    value = self._python_location_symbol(job, matrix_context, step)
                if value is None:
                    raise ValueError
                return value
            if expression.startswith('steps.') and '.outputs.' in expression:
                output_match = re.fullmatch(
                    r'steps\.([a-z0-9_-]+)\.outputs\.([a-z0-9_-]+)',
                    expression,
                )
                if not output_match:
                    raise ValueError
                step_id, output_name = output_match.groups()
                value = self._resolve_step_output(
                    job_name, job, step_id, output_name, matrix_context,
                )
                if value is None:
                    raise ValueError
                return value
            if expression.startswith('hashfiles(') and expression.endswith(')'):
                start = raw_expression.find('(')
                inner = raw_expression[start + 1:-1]

                def replace_hash_env(env_match):
                    value = self._resolve_env_value(
                        env_match.group(1), job, None, matrix_context, step,
                    )
                    if value is None:
                        raise ValueError
                    return value

                inner = re.sub(
                    r'\benv\.([A-Za-z_][\w-]*)\b',
                    replace_hash_env,
                    inner,
                    flags=re.IGNORECASE,
                )
                def replace_hash_matrix(matrix_match):
                    value = self._matrix_context_value(matrix_context, matrix_match.group(1))
                    if value is None or isinstance(value, (dict, list)):
                        raise ValueError
                    return "'" + str(value).replace("'", "''") + "'"
                if resolve_matrix_arguments:
                    inner = re.sub(
                        r"'[^']*(?:''[^']*)*'|\"[^\"]*\"|\bmatrix\.([A-Za-z_][\w.-]*)\b",
                        lambda m: replace_hash_matrix(m) if m.group(1) else m.group(0), inner,
                    )
                return '${{ hashFiles(' + inner + ') }}'
            raise ValueError

        try:
            return self._KEY_EXPRESSION_RE.sub(replace_expression, key)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _matrix_context_value(context: Dict[str, Any], reference: str) -> Any:
        value = context
        for component in reference.split('.'):
            if not isinstance(value, dict) or component not in value:
                return None
            value = value[component]
        return value

    def _python_location_symbol(self, job, context, cache_step) -> Optional[str]:
        """Conservative setup-python provenance, never arbitrary env equivalence."""
        scopes = [getattr(cache_step, 'env', {}), job.env]
        if any('pythonLocation' in (scope or {}) for scope in scopes):
            return None
        runner = job.runs_on
        if not isinstance(runner, str):
            return None
        runner = self._KEY_EXPRESSION_RE.sub(
            lambda m: str(self._matrix_context_value(context, m.group(1).strip()[7:]))
            if m.group(1).strip().startswith('matrix.') else m.group(0), runner,
        )
        if '${{' in runner or 'None' in runner or self._resolve_runner_os(runner, {}) is None:
            return None
        setup = None
        for previous in job.steps:
            if previous is cache_step:
                break
            # Opaque preceding code can overwrite GITHUB_ENV; do not guess.
            if previous.run:
                return None
            if previous.uses:
                action = previous.uses.split('@')[0].lower()
                if action == 'actions/setup-python':
                    if previous.if_:
                        return None
                    setup = previous
                elif action not in {'actions/checkout', 'actions/cache', 'actions/cache/restore'}:
                    return None
        if setup is None or not (setup.with_ or {}).get('python-version'):
            return None
        config = {}
        for name, raw in sorted((setup.with_ or {}).items()):
            value = self._resolve_cache_key_for_context(str(raw), '', job, context, setup)
            if value is None:
                return None
            config[name] = value
        return '<pythonLocation:' + json.dumps(
            [runner, setup.uses, config], sort_keys=True, separators=(',', ':')
        ) + '>'

    def _enumerate_matrix_contexts(
        self,
        matrix_config: Optional[Dict[str, Any]],
        allow_objects: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        """枚举普通矩阵、include 和 exclude 产生的静态实例上下文。"""
        if matrix_config is None:
            return [{}]
        if not isinstance(matrix_config, dict):
            return None

        axes = {}
        def supported(value):
            if allow_objects and isinstance(value, dict):
                return all(isinstance(k, str) and supported(v) for k, v in value.items())
            return isinstance(value, (str, int, float, bool)) and not (
                isinstance(value, str) and '${{' in value
            )

        for name, options in matrix_config.items():
            if name in {'include', 'exclude'}:
                continue
            if not isinstance(options, list) or not options or any(
                not supported(option)
                for option in options
            ):
                return None
            axes[name] = options

        if axes:
            names = list(axes)
            contexts = [
                dict(zip(names, values))
                for values in itertools.islice(
                    itertools.product(*(axes[name] for name in names)),
                    self._MAX_KEY_MATRIX_COMBINATIONS + 1,
                )
            ]
        else:
            contexts = [{}]
        base_contexts = list(contexts)

        include = matrix_config.get('include', [])
        if include is None:
            include = []
        if not isinstance(include, list):
            return None
        if not axes and include:
            # include-only matrix 不会额外产生一个空的默认实例。
            contexts = []
        for item in include:
            if not isinstance(item, dict) or any(
                not supported(value)
                for value in item.values()
            ):
                return None
            matched = False
            for context in base_contexts:
                if all(name not in context or context[name] == value for name, value in item.items()):
                    merged = dict(context)
                    merged.update(item)
                    if merged not in contexts:
                        contexts.append(merged)
                    matched = True
            if not matched:
                contexts.append(dict(item))

        exclude = matrix_config.get('exclude', [])
        if exclude is None:
            exclude = []
        if not isinstance(exclude, list):
            return None
        for item in exclude:
            if not isinstance(item, dict):
                return None
        def matches(context, pattern):
            return all(
                matches(context.get(name, {}), value)
                if allow_objects and isinstance(value, dict) and isinstance(context.get(name), dict)
                else context.get(name) == value
                for name, value in pattern.items()
            )
        contexts = [
            context for context in contexts
            if not any(
                matches(context, item)
                for item in exclude
            )
        ]

        unique_contexts = []
        for context in contexts:
            if context not in unique_contexts:
                unique_contexts.append(context)
        return unique_contexts

    def _get_static_matrix_values(self, matrix_config: Dict[str, Any]) -> Optional[Dict[str, List[Any]]]:
        """兼容旧调用者，返回静态矩阵变量的取值集合。"""
        contexts = self._enumerate_matrix_contexts(matrix_config)
        if contexts is None:
            return None
        values = {}
        for context in contexts:
            for name, value in context.items():
                values.setdefault(name, [])
                if value not in values[name]:
                    values[name].append(value)
        return values

    def _resolve_runner_os(self, runs_on: Any, matrix_context: Dict[str, Any]) -> Optional[str]:
        """从可确定的 runner 标签映射 GitHub runner.os。"""
        value = runs_on
        if isinstance(value, str):
            matrix_match = re.fullmatch(r'\${{\s*matrix\.([\w.-]+)\s*}}', value)
            if matrix_match:
                value = self._matrix_context_value(matrix_context, matrix_match.group(1))
        if not isinstance(value, str):
            return None
        value = value.lower()
        if value.startswith('ubuntu'):
            return 'Linux'
        if value.startswith('windows'):
            return 'Windows'
        if value.startswith('macos'):
            return 'macOS'
        return None

    def _resolve_runner_arch(self, runs_on: Any, matrix_context: Dict[str, Any]) -> Optional[str]:
        """从明确的 runner 标签映射 GitHub runner.arch。"""
        value = runs_on
        if isinstance(value, str):
            matrix_match = re.fullmatch(r'\${{\s*matrix\.([\w-]+)\s*}}', value)
            if matrix_match:
                value = matrix_context.get(matrix_match.group(1))
        if not isinstance(value, str):
            return None
        normalized = value.lower()
        if re.search(r'arm64|aarch64|(^|[-_])arm($|[-_])', normalized):
            return 'ARM64'
        if re.search(r'x64|amd64|x86_64', normalized):
            return 'X64'
        # GitHub 的常规 Ubuntu/Windows runner 标签默认是 x64；macOS 标签保守保留未知。
        if normalized.startswith(('ubuntu', 'windows')):
            return 'X64'
        return None

    def _resolve_env_value(
        self,
        name: str,
        job,
        workflow: Optional[WorkflowModel],
        matrix_context: Dict[str, Any],
        step=None,
        seen: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """解析工作流/Job/Step 作用域中的静态 env 常量。"""
        seen = set(seen or set())
        if name in seen:
            return None
        seen.add(name)

        scopes = []
        if step is not None and isinstance(getattr(step, 'env', None), dict):
            scopes.append(step.env)
        if job is not None and isinstance(getattr(job, 'env', None), dict):
            scopes.append(job.env)
        if workflow is not None and isinstance(getattr(workflow, 'env', None), dict):
            scopes.append(workflow.env)

        raw = None
        found = False
        for scope in scopes:
            if name in scope:
                raw = scope[name]
                found = True
                break
        if not found:
            return None
        if isinstance(raw, (bool, int, float)):
            return str(raw).lower() if isinstance(raw, bool) else str(raw)
        if not isinstance(raw, str):
            return None
        if '${{' not in raw:
            return raw

        def replace_expression(match):
            expression = re.sub(r'\s+', '', match.group(1)).lower()
            if expression.startswith('matrix.'):
                variable = expression.split('.', 1)[1]
                if variable in matrix_context:
                    return str(matrix_context[variable])
                raise ValueError
            if expression.startswith('env.'):
                nested_name = match.group(1).split('.', 1)[1].strip()
                nested = self._resolve_env_value(
                    nested_name, job, workflow, matrix_context, step, seen,
                )
                if nested is None:
                    raise ValueError
                return nested
            raise ValueError

        try:
            return self._KEY_EXPRESSION_RE.sub(replace_expression, raw)
        except (TypeError, ValueError):
            return None

    def _resolve_step_output(
        self,
        job_name: str,
        job,
        step_id: str,
        output_name: str,
        matrix_context: Dict[str, Any],
    ) -> Optional[str]:
        """为不依赖 matrix 的步骤输出建立稳定符号值。"""
        producer = next(
            (step for step in job.steps if step.id and step.id.lower() == step_id.lower()),
            None,
        )
        if producer is None:
            return None
        if self._evaluate_matrix_condition(getattr(producer, 'if_', None), matrix_context) is False:
            return None
        if producer.uses:
            # 第三方/缓存 action 的输出通常依赖运行时结果，不能假设跨 matrix 恒定。
            return None
        producer_text = producer.run if isinstance(producer.run, str) else ''
        if not producer_text or re.search(
            r'\b(?:matrix|runner|env)\.', producer_text, re.IGNORECASE,
        ):
            return None
        # 通过当前 Python/Node/Java 运行时计算的版本可能随 matrix 变化，
        # 即使命令本身没有直接引用 matrix，也不能把输出当作恒定值。
        if re.search(
            r'\b(?:python(?:\d+)?|python3?|node|java)\s+(-c|-e|--version|-V)\b',
            producer_text,
            re.IGNORECASE,
        ):
            return None
        literal = re.search(
            rf'echo\s+["\']?{re.escape(output_name)}=([^"\'\s]+)',
            producer.run or '',
            re.IGNORECASE,
        )
        if literal and '${{' not in literal.group(1) and '$(' not in literal.group(1):
            return literal.group(1)
        normalized_producer = re.sub(r'\s+', ' ', producer_text).strip().lower()
        return f'<step-output:{step_id}.{output_name}:{normalized_producer}>'

    def _normalize_key_for_comparison(self, key: str) -> str:
        """标准化缓存 key 用于比较

        将 key 中的 matrix 变量替换为占位符，便于比较不同 Job 的 key 模式。

        Args:
            key: 原始缓存 key

        Returns:
            标准化后的 key
        """
        if not key:
            return key

        # 将所有 matrix.XXX 替换为 MATRIX_VAR
        normalized = re.sub(r'\${{\s*matrix\.[^}]+\s*}}', 'MATRIX_VAR', key)

        # 将 hashFiles(...) 替换为 HASH_FILES
        normalized = re.sub(r'hashFiles\s*\([^)]+\)', 'HASH_FILES', normalized)

        return normalized

    def _key_has_runtime_discriminator(self, key: str) -> bool:
        """判断 key 是否已知能区分所有 matrix 实例。"""
        return False

    def _is_dynamic_key(self, key: str) -> bool:
        """检查缓存 key 是否是动态的（已弃用，使用 _contains_matrix_var 代替）"""
        return self._contains_matrix_var(key)

    def _is_read_only_cache_job(self, job_name: str, job) -> bool:
        """检查 Job 是否是只读缓存 Job（只读缓存不会产生冗余更新问题）

        只读缓存 Job 的特征：
        1. Matrix 包含 shard/分片变量 AND 缓存路径是构建产物/工具缓存（如 puppeteer）
        2. Job 只有测试步骤，没有依赖安装步骤

        注意：如果 Job 有依赖安装步骤（npm install, pip install等），则不是只读的。

        Args:
            job_name: Job 名称
            job: Job 对象

        Returns:
            True 如果是只读缓存 Job，False 否则
        """
        # 首先检查是否有依赖安装步骤
        has_dep_install = False
        for step in job.steps:
            if step.run and isinstance(step.run, str):
                if self._is_uv_bootstrap_install(step.run):
                    continue
                # 检查是否是依赖安装命令
                for pattern, _, _, _ in self._compiled_patterns:
                    if pattern.search(step.run):
                        has_dep_install = True
                        break
            if has_dep_install:
                break

        # 如果有依赖安装，不是只读
        if has_dep_install:
            return False

        # 检查 setup actions 是否启用了缓存（隐含依赖安装）
        for step in job.steps:
            if step.uses:
                for setup_action, config in self.SETUP_ACTION_CACHE.items():
                    if self._is_exact_action(step.uses, setup_action):
                        cache_param = config['cache_param']
                        cache_value = (step.with_ or {}).get(cache_param)
                        if self._is_setup_cache_enabled(setup_action, cache_value):
                            # setup action 启用了缓存，说明有依赖操作
                            return False

        # 没有 setup 缓存，也没有依赖安装命令
        # 检查是否是测试分片场景
        if job.matrix:
            matrix_vars = self._extract_matrix_variables(job.matrix)
            # shard/split 变量表示测试分片
            if any(v in matrix_vars for v in ['shard', 'split', 'partition', 'chunk']):
                return True

        return False

    def _is_read_only_cache_step(self, step) -> bool:
        """判断缓存步骤是否只恢复而不会在 Job 结束时写入。"""
        cache_action = self._classify_cache_action(step.uses)
        if not cache_action:
            return False
        if not cache_action['can_write']:
            return True

        step_with = step.with_ or {}
        # fail-on-cache-miss is a failure policy, not a write restriction.
        # A fallback hit can still be saved under a new primary key.
        for flag in ('lookup-only',):
            value = step_with.get(flag)
            if value is True or (
                isinstance(value, str) and value.strip().lower() == 'true'
            ):
                return True
        return False

    def _is_lang_cached(self, lang: str, analysis: Dict) -> bool:
        """检查语言是否已缓存"""
        for cs in analysis['cache_steps']:
            if cs.get('setup_cache'):
                for ss in analysis['setup_steps']:
                    # 自定义 setup action（已封装缓存逻辑）也视为已缓存
                    if ss['action'] == cs['action'] and lang in ss['langs']:
                        return True

            # 检查路径是否匹配
            path_val = cs.get('path', '')
            if path_val:
                path_str = str(path_val)
                for pattern, plang, default_paths, keywords in self._compiled_patterns:
                    if plang == lang:
                        # 1. 静态默认路径匹配
                        for dp in default_paths:
                            if dp and dp in path_str:
                                return True
                        # 2. 动态路径关键字匹配（如 ${{ env.pythonLocation }}）
                        for kw in keywords:
                            if kw and kw in path_str.lower():
                                return True

        return False

    def _is_dependency_cache_path(self, cache_path: str) -> bool:
        """检查缓存路径是否是依赖缓存路径

        Args:
            cache_path: 缓存路径

        Returns:
            True 如果是依赖缓存路径，False 否则（工具/构建产物缓存）
        """
        if not cache_path:
            return True  # 无路径信息，假设是依赖缓存

        path_lower = str(cache_path).lower()

        # 检查是否是非依赖缓存路径
        for pattern in self._non_dep_cache_patterns:
            if pattern.search(path_lower):
                return False  # 不是依赖缓存

        # 检查是否是构建产物路径
        for pattern in self._build_output_patterns:
            if pattern.search(path_lower):
                return False  # 不是依赖缓存

        return True  # 是依赖缓存路径

    def _is_restore_save_pattern(self, step) -> bool:
        """检查步骤是否是 actions/cache/save 模式

        在 restore/save 模式中，save 步骤的位置不重要，
        因为它只是保存缓存，不影响缓存恢复。

        Args:
            step: 步骤信息

        Returns:
            True 如果是 save 步骤，False 否则
        """
        if not step.uses:
            return False
        return 'actions/cache/save' in step.uses or 'actions/save' in step.uses

    def _is_lint_or_review_job(self, job_name: str, job) -> bool:
        """检查 Job 是否是 lint/review Job

        Lint/review Job 通常可以共享缓存，因为它们只是分析代码，不修改项目。

        Args:
            job_name: Job 名称
            job: Job 对象

        Returns:
            True 如果是 lint/review Job，False 否则
        """
        # 检查 Job 名称关键字
        name_lower = job_name.lower()
        lint_keywords = {
            'lint', 'review', 'reviewdog', 'golangci', 'check', 'verify',
            'analyze', 'codeql', 'sonar', 'quality'
        }
        if any(kw in name_lower for kw in lint_keywords):
            return True

        # 检查步骤中是否使用 lint/review 工具
        lint_tools = {
            'reviewdog', 'golangci-lint', 'eslint', 'pylint', 'flake8',
            'sonar-scanner', 'codeql', 'clang-tidy', 'cpplint'
        }
        for step in job.steps:
            if step.run and isinstance(step.run, str):
                run_lower = step.run.lower()
                if any(tool in run_lower for tool in lint_tools):
                    return True
            if step.uses and isinstance(step.uses, str):
                uses_lower = step.uses.lower()
                if any(tool in uses_lower for tool in lint_tools):
                    return True

        return False

    def _is_os_arch_matrix_only(self, matrix_vars: Set[str]) -> bool:
        """检查 matrix 变量是否只是 OS/arch 相关

        如果 matrix 只包含 OS 或 arch 变量，可能有意在不同平台间共享缓存。

        Args:
            matrix_vars: matrix 变量集合

        Returns:
            True 如果只是 OS/arch 相关变量，False 否则
        """
        os_arch_vars = {
            'os', 'runner', 'arch', 'architecture', 'platform',
            'ubuntu', 'windows', 'macos', 'linux'
        }
        # 检查所有 matrix 变量是否都是 OS/arch 相关
        return all(any(var.lower().startswith(os_v) or os_v in var.lower()
                      for os_v in os_arch_vars) for var in matrix_vars)

    def _is_key_sufficiently_dynamic(self, key: str) -> bool:
        """检查缓存 key 是否足够动态（不需要 restore-keys）

        当 key 包含以下动态元素时，认为已经足够灵活：
        1. hashFiles() - 文件哈希函数
        2. matrix. - matrix 策略变量
        3. runner.os - 运行时环境变量

        这些动态元素使得 key 能够适应不同的场景，不需要额外的 restore-keys 回退。

        Args:
            key: 缓存 key

        Returns:
            True 如果 key 足够动态，False 否则
        """
        if not key:
            return False

        key_str = str(key).lower()

        # 检查 hashFiles() 函数调用
        if 'hashfiles(' in key_str or 'hash_files(' in key_str:
            return True

        # 检查 matrix 变量引用
        if 'matrix.' in key_str:
            return True

        # 检查 runner 环境变量
        if 'runner.os' in key_str or 'runner.' in key_str:
            return True

        return False

    def _is_external_dependency_cache(self, key: str, cache_path: str) -> bool:
        """检查是否是外部依赖缓存（如 libsodium, conan, vcpkg）

        外部依赖缓存通常不需要 restore-keys，因为它们是预编译的二进制文件。

        Args:
            key: 缓存 key
            cache_path: 缓存路径

        Returns:
            True 如果是外部依赖缓存，False 否则
        """
        # 外部依赖关键字
        external_keywords = {
            'libsodium', 'sodium', 'conan', 'vcpkg', 'prebuilt', 'precompiled',
            'binary', 'binaries', 'artifact', 'artifacts'
        }

        key_lower = str(key).lower()
        path_lower = str(cache_path).lower()

        for keyword in external_keywords:
            if keyword in key_lower or keyword in path_lower:
                return True

        return False

    def _are_build_and_package_jobs(self, job_names: List[str], workflow: WorkflowModel) -> bool:
        """检查 Job 是否是 build 和 package 类型

        例如：build-win64 和 package-amd64 - 一个构建，一个打包
        这种情况下它们可能有意共享缓存。

        Args:
            job_names: Job 名称列表
            workflow: 工作流对象

        Returns:
            True 如果 Job 是 build 和 package 类型，False 否则
        """
        if len(job_names) != 2:
            return False

        has_build = any('build' in name.lower() for name in job_names)
        has_package = any('package' in name.lower() for name in job_names)

        return has_build and has_package

    def _detect_disposable_cache_key(self, job_name: str, analysis: Dict, workflow_mode: Dict, job=None, workflow=None) -> List[SmellInstance]:
        """检测一次性缓存key（disposable_cache_key）

        易变 key 包含每次运行都会变化的唯一标识，或把工作流文件本身作为
        失效依据，因而可能导致缓存无法稳定复用。

        检测模式：
        - github.run_id / github.run_number 等每次运行唯一的标识
        - .github/workflows/*.yml 或 .yaml
        - github.workflow_ref
        - github.workflow_sha：仅对所有路径均明确为依赖下载缓存的步骤报告
        """
        smells = []

        for cache_step in analysis['cache_steps']:
            key = cache_step.get('key', '')
            if not key:
                continue

            workflow_commit = workflow_sha_download_evidence(workflow, job, cache_step)
            read_only = cache_step.get('read_only')
            # S6 describes the key's partitioning, independently of whether
            # this operation restores, queries, or saves the cache entry.
            evidence = RuntimeCacheKeySources(workflow, job).evidence(
                key, cache_step.get('index', -1),
            )
            if (self._condition_is_definitely_false(cache_step.get('if'))
                    or self._condition_is_definitely_false(getattr(job, 'if_', None))):
                continue
            # Keep workflow-file/ref policy; workflow_sha now needs resource proof.
            legacy_workflow_dimension = not read_only and bool(re.search(
                r'\.github[\\/]workflows[\\/]|github\.workflow_ref\b',
                str(key), re.IGNORECASE,
            ))
            if evidence or workflow_commit or legacy_workflow_dimension:
                commit_only = workflow_commit and not evidence
                smells.append(self.create_smell(
                    smell_type='disposable_cache_key',
                    severity=SeverityLevel.HIGH,
                    message=(f'Job "{job_name}" 的依赖下载缓存 key 按工作流提交 SHA 分区，可能造成不必要的缓存细分'
                             if commit_only else f'Job "{job_name}" 的缓存 key 包含每次运行唯一的标识，导致缓存无法稳定复用'),
                    job_name=job_name,
                    step_name=f'缓存步骤 (index: {cache_step["index"]})',
                    details={
                        'cache_key': key,
                        'cache_action': cache_step.get('action'),
                        'runtime_key_sources': evidence,
                        **({'workflow_commit_evidence': workflow_commit} if workflow_commit else {}),
                    },
                    suggestion=('移除依赖下载缓存 key 中的 workflow_sha，保留必要的兼容性分区及依赖输入哈希'
                                if commit_only else '移除 key 中每次运行都会变化的唯一标识（如 github.run_id），使用依赖文件哈希（hashFiles）或稳定版本号代替')
                ))

        return smells

    def _detect_static_cache_key(
        self,
        job_name: str,
        analysis: Dict,
        workflow_mode: Dict,
        job=None,
        workflow: Optional[WorkflowModel] = None,
    ) -> List[SmellInstance]:
        """检测静态缓存key（static_cache_key）

        静态缓存key完全固定，依赖更新后缓存不会刷新，可能导致使用过期依赖。

        检测条件：key 只有固定值或分区维度，没有可确认的失效维度。
        """
        smells = []

        for cache_step in analysis['cache_steps']:
            key = cache_step.get('key', '')
            if not key or cache_step.get('read_only'):
                continue

            index = cache_step.get('index')
            steps = getattr(job, 'steps', [])
            step = steps[index] if isinstance(index, int) and 0 <= index < len(steps) else None
            if self._is_static_key(key, job=job, workflow=workflow, step=step):
                smells.append(self.create_smell(
                    smell_type='static_cache_key',
                    severity=SeverityLevel.MEDIUM,
                    message=f'Job "{job_name}" 的缓存 key 是静态的，依赖更新后缓存不会刷新',
                    job_name=job_name,
                    step_name=f'缓存步骤 (index: {cache_step["index"]})',
                    details={
                        'cache_key': key,
                        'cache_action': cache_step.get('action'),
                    },
                    suggestion='在 key 中添加随依赖内容变化的哈希（hashFiles）；固定版本号和 OS/架构分区不能自动更新缓存'
                ))

        return smells

    def _detect_no_docker_layer_cache(self, job_name: str, job, analysis: Dict, workflow_mode: Dict) -> List[SmellInstance]:
        """Report only individual builds lacking a usable historical cache input."""
        assessments = analysis.get('docker_cache_assessments')
        if assessments is None:
            assessments = self._assess_docker_layer_cache(job, analysis)
        missing = [item for item in assessments if item['status'] == 'missing']
        if not missing:
            return []
        return [self.create_smell(
            smell_type='no_docker_layer_cache',
            severity=SeverityLevel.MEDIUM,
            message=f'Job "{job_name}" 存在未接入历史层缓存的 Docker 构建',
            job_name=job_name,
            details={
                'docker_build_steps': missing,
                'docker_cache_assessments': assessments,
                'cache_steps': [cs.get('action') for cs in analysis.get('cache_steps', [])],
            },
            suggestion='为缺少缓存输入的构建配置 cache-from（命令使用 --cache-from）。'
                       '本地缓存的 type=local,src=... 需指向先前恢复的目录；'
                       '需要更新缓存时另配 cache-to，只读构建无需导出。',
        )]

    def _is_disposable_key(self, key: str) -> bool:
        """检查是否是一次性缓存key

        一次性 key 包含每次运行都会变化的唯一标识，因而无法复用前一次运行的缓存。
        提交 SHA 是稳定的版本标识，不属于一次性 key。
        workflow_sha 需要缓存资源证据，不能由这个无上下文辅助函数判断。
        """
        if not key:
            return False

        key_str = str(key).lower()

        # 检查每次运行都会变化的常见唯一标识。
        # github.sha、github.sha_short、github.event.after 以及 PR head SHA
        # 不在此列：同一提交的多次运行可以复用缓存。
        if RuntimeCacheKeySources().evidence(key, -1):
            return True

        # 工作流文件本身参与 key（通常出现在 hashFiles 中）会让缓存随工作流
        # 编排调整而整体失效，属于“一次性/细粒度”易变 key。
        if re.search(r'\.github[\\/]workflows[\\/]', key_str):
            return True
        if re.search(r'github\.workflow_ref\b', key_str):
            return True

        return False

    def _is_dependency_manager_cached(self, manager: str, analysis: Dict) -> bool:
        """按具体包管理器判断依赖是否有对应缓存。

        同一语言的包管理器也可能使用不同缓存；例如 uv 缓存不能覆盖
        pip 安装。setup 的显式选择和手动恢复的资源都按管理器匹配。
        """
        if manager == 'python':
            return self._is_lang_cached('python', analysis)

        python_resources = {
            'pip': ('pip',),
            'uv': ('uv',),
            'pipenv': ('pip', 'pipenv', 'virtualenvs'),
            'poetry': ('pypoetry', 'virtualenvs'),
        }
        if manager in python_resources:
            resource_paths = {
                'pip': r'(?:\.cache/pip|\.pip/cache|pip/cache)',
                'uv': r'(?:\.cache/uv|uv/cache)',
                'pipenv': r'\.cache/pipenv',
                'virtualenvs': r'\.local/share/virtualenvs',
                'pypoetry': r'(?:\.cache/pypoetry|pypoetry/cache)',
            }
            for cache_step in analysis.get('cache_steps', []):
                if (cache_step.get('operation') == 'save'
                        or cache_step.get('lookup_only') is True
                        or str(cache_step.get('lookup_only', '')).lower() == 'true'):
                    continue  # Saving or querying an entry does not restore it.
                if self._condition_is_definitely_false(cache_step.get('if')):
                    continue
                path = str(cache_step.get('path') or '').lower().replace('\\', '/')
                # Match actual cache directories rather than a tool's name in
                # an arbitrary path (e.g. dist/pip-tools is not pip's cache).
                if any(re.search(r'(?:^|/)' + resource_paths[resource] + r'(?:/|$|\s)', path)
                       for resource in python_resources[manager]):
                    return True
                if '${{' in path:
                    if any(re.search(r'(?<![a-z0-9])' + resource + r'(?![a-z0-9])', path)
                           for resource in python_resources[manager]):
                        return True
                    if manager == 'pip' and 'pythonlocation' in path:
                        return True  # Existing full Python environment cache support.
            return False

        expected_markers = {
            'npm': ('~/.npm', 'node_modules', 'npm'),
            'yarn': ('~/.cache/yarn', 'node_modules', 'yarn'),
            'pnpm': ('~/.local/share/pnpm/store', 'node_modules', 'pnpm'),
        }.get(manager)
        if not expected_markers:
            return self._is_lang_cached(manager, analysis)

        for cache_step in analysis.get('cache_steps', []):
            if cache_step.get('setup_cache'):
                cache_value = cache_step.get('cache_value')
                if isinstance(cache_value, str) and cache_value.lower() == manager:
                    return True

            path = str(cache_step.get('path') or '').lower()
            if path and any(marker in path for marker in expected_markers):
                return True

        return False

    def _is_static_key(
        self,
        key: str,
        job=None,
        workflow: Optional[WorkflowModel] = None,
        step=None,
    ) -> bool:
        """Only report keys proven fixed within their literal cache partitions."""
        return self._classify_static_cache_key(key, job, workflow, step) == 'static'

    def _classify_static_cache_key(self, key, job=None, workflow=None, step=None):
        """Return static/non_static/unknown without evaluating arbitrary expressions.

        OS/architecture and literal matrix values partition caches; they do not
        invalidate entries inside a partition. Unknown runtime values remain
        unknown, including step outputs and expressions we cannot interpret.
        """
        def classify(value, seen):
            if isinstance(value, (bool, int, float)):
                return 'static'
            if not isinstance(value, str) or not value:
                return 'unknown'
            matches = list(self._KEY_EXPRESSION_RE.finditer(value))
            remainder = self._KEY_EXPRESSION_RE.sub('', value)
            states = [expression_state(match.group(1).strip(), seen) for match in matches]
            if '${{' in remainder:
                states.append('unknown')  # Malformed expression, not a literal.
            if 'non_static' in states:
                return 'non_static'
            return 'unknown' if 'unknown' in states else 'static'

        def expression_state(expression, seen):
            if re.fullmatch(r'runner\.(?:os|arch)', expression, re.IGNORECASE):
                return 'static'
            if (re.fullmatch(r"'(?:[^']|'')*'", expression)
                    or re.fullmatch(r'-?\d+(?:\.\d+)?|true|false', expression, re.IGNORECASE)):
                return 'static'
            if (re.match(r'^hashFiles\s*\(', expression, re.IGNORECASE)
                    or re.fullmatch(r'github\.(?:sha|run_id|run_number|run_attempt|workflow_sha|event\.after|event\.pull_request\.head\.sha)', expression, re.IGNORECASE)):
                return 'non_static'
            env_match = re.fullmatch(r'env\.([A-Za-z_][\w-]*)', expression, re.IGNORECASE)
            if env_match:
                name = env_match.group(1)
                if name in seen:
                    return 'unknown'
                # A shell may override an otherwise literal YAML environment.
                for prior in getattr(job, 'steps', []):
                    if prior is step:
                        break
                    run = prior.run if isinstance(prior.run, str) else ''
                    if 'GITHUB_ENV' in run and re.search(r'\b' + re.escape(name) + r'\s*=', run):
                        return 'unknown'
                for owner in (step, job, workflow):
                    scope = getattr(owner, 'env', None)
                    if isinstance(scope, dict) and name in scope:
                        return classify(scope[name], seen | {name})
                return 'unknown'
            matrix_match = re.fullmatch(r'matrix\.([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)', expression, re.IGNORECASE)
            if matrix_match:
                matrix = getattr(job, 'matrix', None)
                if matrix is None:
                    matrix = (getattr(job, 'strategy', None) or {}).get('matrix')
                if not isinstance(matrix, dict):
                    return 'unknown'
                root, *fields = matrix_match.group(1).split('.')
                values = matrix.get(root, [])
                if not isinstance(values, list):
                    return 'unknown'
                values = list(values)
                include = matrix.get('include', [])
                if not isinstance(include, list):
                    return 'unknown'
                for item in include:
                    if not isinstance(item, dict):
                        return 'unknown'
                    if root in item:
                        values.append(item[root])
                    elif root not in matrix:
                        return 'unknown'
                if not values:
                    return 'unknown'
                for value in values:
                    for field in fields:
                        if not isinstance(value, dict) or field not in value:
                            return 'unknown'
                        value = value[field]
                    # Expressions in matrix definitions are not fixed literals.
                    if not isinstance(value, (str, bool, int, float)) or '${{' in str(value):
                        return 'unknown'
                return 'static'
            return 'unknown'

        return classify(key, set())

    def _has_docker_build_command(self, run_content: str) -> bool:
        """检查是否包含 Docker 构建命令"""
        if not run_content:
            return False

        patterns = [
            r'\bdocker\s+build(?:\s|$)',
            r'\bdocker\s+buildx\s+build(?:\s|$)',
            r'\bdocker\s+compose\s+build(?:\s|$)',
            r'\bdocker-compose\s+build(?:\s|$)',
            r'\bbuildx\s+build(?:\s|$)',
        ]

        run_lower = run_content.lower()
        return any(re.search(p, run_lower) for p in patterns)

    def _assess_docker_layer_cache(self, job, analysis, workflow=None):
        return DockerLayerCacheAnalyzer(
            job, analysis, self._shell_commands, self._condition_is_definitely_false,
            self._conditions_are_mutex, workflow=workflow,
        ).assess()

    def _has_docker_layer_cache(self, job, analysis: Dict[str, Any]) -> bool:
        """Compatibility helper: all analyzed builds must have a cache import."""
        assessments = analysis.get('docker_cache_assessments')
        if assessments is None:
            assessments = self._assess_docker_layer_cache(job, analysis)
        return bool(assessments) and all(item['status'] == 'configured' for item in assessments)

    def _detect_conflicting_cache_sources(self, job_name: str, analysis: Dict, workflow_mode: Dict,
                                         job=None, workflow=None) -> List[SmellInstance]:
        """检测缓存来源冲突（conflicting_cache_sources）

        检测 setup 内置缓存与手动缓存，以及独立手动缓存之间的目录重叠。

        冲突场景：
        1. setup-go v4 起默认启用缓存，v3 必须显式开启，v2 不支持依赖缓存
        2. 其他 setup-action 需要显式启用缓存，如果同时手动缓存相同目录会冲突

        冲突后果：
        - 两个缓存尝试恢复到同一个目录
        - tar 解压时报错 "File exists"
        - 缓存恢复失败，构建变慢

        setup/手动缓存比较沿用预热工作流豁免；独立手动机制按实际目录检查。
        """
        smells = []

        cache_steps = analysis.get('cache_steps', [])

        # 独立手动缓存也可能重复管理同一资源。lookup-only 不操作目录；
        # 同一资源的显式 restore/save 配对属于一个机制，不计为重叠。
        manual_steps = [
            step for step in cache_steps
            if not step.get('setup_cache')
            and step.get('operation') in {'restore', 'save', 'restore_and_save'}
            and self._s11_cache_step_is_active(step, job)
        ]
        # Only suppress a uniquely identified lifecycle edge, never the whole
        # job/resource. Other mechanisms must still be compared to both steps.
        candidates = [
            (i, j) for i, left in enumerate(manual_steps)
            for j, right in enumerate(manual_steps[i + 1:], i + 1)
            if not self._s11_conditions_are_mutex(left.get('if'), right.get('if'), workflow)
            and self._is_manual_cache_restore_save_pair(left, right)
        ]
        degrees = {}
        for i, j in candidates:
            degrees[i] = degrees.get(i, 0) + 1
            degrees[j] = degrees.get(j, 0) + 1
        lifecycle_pairs = {(i, j) for i, j in candidates if degrees[i] == degrees[j] == 1}
        for position, left in enumerate(manual_steps):
            for right_position, right in enumerate(manual_steps[position + 1:], position + 1):
                if self._s11_conditions_are_mutex(left.get('if'), right.get('if'), workflow):
                    continue
                if not self._cache_paths_overlap(left.get('path'), right.get('path')):
                    continue
                if (position, right_position) in lifecycle_pairs:
                    continue
                if self._s11_is_diagnostic_reread(left, right, job):
                    continue
                smells.append(self.create_smell(
                    smell_type='conflicting_cache_sources',
                    severity=SeverityLevel.HIGH,
                    message=f'Job "{job_name}" 的两个独立手动缓存管理重叠目录',
                    job_name=job_name,
                    details={
                        'conflict_reason': 'overlapping_manual_caches',
                        'cache_step_indices': [left.get('index'), right.get('index')],
                        'cache_actions': [left.get('action'), right.get('action')],
                        'cache_paths': [left.get('path'), right.get('path')],
                        'cache_keys': [left.get('key'), right.get('key')],
                    },
                    suggestion='合并管理相同目录的独立缓存，或将缓存目录划分为互不重叠的资源。',
                ))

        # 沿用原 setup 缓存规则的豁免，不抑制上面的独立手动机制检查。
        if workflow_mode.get('is_warm_cache'):
            return smells

        # 分析每个 setup-step 的缓存状态
        for cache_step in cache_steps:
            if not cache_step.get('setup_cache'):
                continue

            setup_action = cache_step.get('action', '')
            cache_value = cache_step.get('cache_value')
            cache_reason = cache_step.get('cache_reason', '')

            # 找到对应的 setup-action 配置
            setup_config = None
            setup_action_path = self._get_action_path(setup_action)
            for action, config in self.SETUP_CACHE_ACTIONS.items():
                if setup_action_path == action:
                    setup_config = config
                    break

            if not setup_config:
                continue

            # 对 setup-node/setup-python 优先使用本次 cache 参数对应的实际目录，
            # 避免把未被内置缓存覆盖的目录误认为冲突。
            setup_cache_dirs = cache_step.get('cache_dirs') or setup_config.get('cache_dirs', [])

            # 检查是否有手动缓存冲突
            for manual_cache in cache_steps:
                if manual_cache.get('setup_cache'):
                    continue  # 跳过其他 setup-step
                if (not self._s11_cache_step_is_active(cache_step, job)
                        or not self._s11_cache_step_is_active(manual_cache, job)
                        or self._s11_conditions_are_mutex(cache_step.get('if'), manual_cache.get('if'), workflow)):
                    continue

                manual_path = manual_cache.get('path', '')
                if not manual_path:
                    continue

                # 检查路径是否有重叠
                path_overlap = self._check_path_overlap(manual_path, setup_cache_dirs)
                if path_overlap:
                    # 确定冲突原因
                    if cache_reason == 'default_enabled':
                        conflict_reason = 'setup_action_default_cache'
                    elif cache_value:
                        conflict_reason = 'setup_action_explicit_cache'
                    else:
                        continue  # 没有实际冲突

                    disable_hint = (
                        'enable-cache: false'
                        if setup_config.get('enable_flag') == 'enable-cache'
                        else 'cache: false'
                    )
                    smells.append(self.create_smell(
                        smell_type='conflicting_cache_sources',
                        severity=SeverityLevel.HIGH,
                        message=f'Job "{job_name}" 同时使用手动缓存和 {setup_action} 内置缓存，可能恢复到相同目录导致冲突',
                        job_name=job_name,
                        details={
                            'setup_action': setup_action,
                            'setup_cache_dirs': setup_cache_dirs,
                            'manual_cache_action': manual_cache.get('action'),
                            'manual_cache_path': manual_path,
                            'overlap_path': path_overlap,
                            'conflict_reason': conflict_reason,
                            'cache_value': cache_value,
                        },
                        suggestion=f'移除手动缓存步骤，只使用 {setup_action} 的内置缓存。\n'
                                  f'或者如果需要自定义缓存行为，设置 {setup_action} 的 {disable_hint}'
                    ))
                    break  # 一个 setup-step 只报告一次冲突

        return smells

    def _is_manual_cache_restore_save_pair(self, left: Dict, right: Dict) -> bool:
        """Candidate lifecycle; caller also requires an unambiguous pairing."""
        if left.get('operation') != 'restore' or right.get('operation') != 'save':
            return False
        if left.get('index', -1) >= right.get('index', -1):
            return False
        paths = lambda step: {path.rstrip('/') for path in self._cache_path_items(step.get('path'))}
        normalize_key = lambda key: re.sub(r'\s+', '', key) if isinstance(key, str) else ''
        restore_key = normalize_key(left.get('key'))
        save_key = normalize_key(right.get('key'))
        restore_id = left.get('step_id')
        primary_reference = bool(restore_id and save_key == '${{steps.' + restore_id + '.outputs.cache-primary-key}}')
        if primary_reference:
            # Different exclusions can change the archive version, but these
            # explicitly linked operations remain one mechanism (not S11).
            roots = lambda step: {
                path.replace('\\', '/').rstrip('/')
                for path in self._cache_path_items(step.get('path')) if not path.startswith('!')
            }
            return bool(roots(left)) and roots(left) == roots(right)
        if not paths(left) or paths(left) != paths(right):
            return False
        if restore_key and restore_key == save_key:
            return True
        # A pure restore followed by a pure save with only execution suffixes
        # added is a rolling cache. Arbitrary lexical prefixes are insufficient.
        prefixes = [restore_key] + [normalize_key(key) for key in self._cache_path_items(left.get('restore_keys'))]
        suffix = r'(?:[-_.]*\$\{\{github\.(?:run_id|run_number|run_attempt)\}\})+'
        return any(prefix and re.fullmatch(re.escape(prefix) + suffix, save_key) for prefix in prefixes)

    def _s11_cache_step_is_active(self, step: Dict, job=None) -> bool:
        """Exclude only provably inactive directory operations from S11."""
        if str(step.get('lookup_only', '')).strip().lower() == 'true':
            return False
        if self._condition_is_definitely_false(step.get('if')):
            return False
        if job is not None:
            if self._condition_is_definitely_false(job.if_):
                return False
            missing = self._missing_cache_output_step_ids(step.get('key'), job)
            if self._key_is_empty_from_missing_cache_outputs(step.get('key'), missing):
                return False
        return True

    def _s11_conditions_are_mutex(self, left: Any, right: Any, workflow=None) -> bool:
        """Boolean normalization is local to S11 and declared boolean inputs."""
        declared = set()
        if workflow is not None:
            data = workflow.yaml_data or {}
            triggers = data.get('on', data.get(True, {}))
            if isinstance(triggers, dict):
                input_types = {}
                for kind in ('workflow_call', 'workflow_dispatch'):
                    body = triggers.get(kind)
                    inputs = body.get('inputs', {}) if isinstance(body, dict) else {}
                    if isinstance(inputs, dict):
                        for name, spec in inputs.items():
                            input_types.setdefault(str(name).lower(), set()).add(
                                spec.get('type') if isinstance(spec, dict) else None)
                declared.update('inputs.' + name for name, types in input_types.items()
                                if types == {'boolean'})

        def normalize(condition):
            normalized = self._normalize_condition(condition)
            for reference in declared:
                # Do not rewrite quoted strings, env or cache-hit outputs.
                quoted = [(match.start(), match.end()) for match in re.finditer(
                    r"'(?:[^']|'')*'|\"[^\"]*\"", normalized)]
                pattern = re.compile(r'(?<![\w.\'"!])' + re.escape(reference)
                                     + r'\s*(==|!=)\s*(true|false)\b', re.I)
                normalized = pattern.sub(
                    lambda match: match[0] if any(start <= match.start() < end for start, end in quoted)
                    else (reference if (match[1] == '==') == (match[2].lower() == 'true')
                          else '!' + reference), normalized)
            return normalized
        return self._conditions_are_mutex(normalize(left), normalize(right))

    def _s11_is_diagnostic_reread(self, left: Dict, right: Dict, job=None) -> bool:
        """Prove a failure-only lockfile comparison from visible shell commands."""
        if (job is None or left.get('operation') not in {'restore', 'restore_and_save'}
                or right.get('operation') != 'restore'
                or self._normalize_condition(right.get('if')).lower() != 'failure()'):
            return False
        normalize_key = lambda key: re.sub(r'\s+', '', key) if isinstance(key, str) else ''
        if (not left.get('key') or normalize_key(left.get('key')) != normalize_key(right.get('key'))
                or left.get('restore_keys') != right.get('restore_keys')):
            return False
        paths = self._cache_path_items(left.get('path'))
        if len(paths) != 1 or paths != self._cache_path_items(right.get('path')):
            return False
        first, last = left.get('index', -1), right.get('index', -1)
        if first < 0 or last <= first or last >= len(job.steps):
            return False
        renamed = None
        for step in job.steps[first + 1:last]:
            if self._normalize_condition(step.if_).lower() != 'failure()':
                continue
            for command in self._shell_commands(step.run):
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    continue
                if (len(tokens) == 3 and tokens[0] == 'mv' and tokens[1] == paths[0]
                        and tokens[2].startswith(paths[0] + '.')
                        and not any(char in tokens[2] for char in '$`*?')):
                    renamed = tokens[2]
        if renamed is None:
            return False
        for step in job.steps[last + 1:]:
            if self._normalize_condition(step.if_).lower() != 'failure()':
                continue
            for command in self._shell_commands(step.run):
                try:
                    tokens = shlex.split(command)
                except ValueError:
                    continue
                # Require comparison semantics plus both visible operands.
                if (tokens[:2] == ['utx', 'diff-yarn-lock'] and '--source=' + paths[0] in tokens
                        and '--target=' + renamed in tokens):
                    return True
                if tokens and tokens[0] == 'diff' and paths[0] in tokens[1:] and renamed in tokens[1:]:
                    return True
        return False

    def _check_path_overlap(self, manual_path: str, setup_cache_dirs: List[str]) -> Optional[str]:
        """检查手动缓存路径是否与 setup-action 缓存目录重叠

        Args:
            manual_path: 手动缓存的路径
            setup_cache_dirs: setup-action 的缓存目录列表

        Returns:
            重叠的路径，如果没有重叠返回 None
        """
        if not manual_path:
            return None

        def split_paths(value: Any) -> List[str]:
            if not isinstance(value, str):
                return []
            return [
                part.strip().strip('"\'')
                for part in re.split(r'[\r\n]+', value)
                if part.strip()
            ]

        def normalize(value: str) -> str:
            return re.sub(r'\s+', '', value.replace('~/', '').replace('~\\', '')).lower()

        manual_paths = split_paths(manual_path)
        setup_paths = [path for value in setup_cache_dirs for path in split_paths(value)]
        for manual_item in manual_paths:
            manual_path_normalized = normalize(manual_item)
            for cache_dir in setup_paths:
                cache_dir_normalized = normalize(cache_dir)

                # 对常见包管理器环境变量做保守别名匹配。
                dynamic_alias = (
                    ('pnpm_home' in manual_path_normalized and 'pnpm' in cache_dir_normalized)
                    or ('npm_config_cache' in manual_path_normalized and 'npm' in cache_dir_normalized)
                )
                if dynamic_alias:
                    return cache_dir

                # 检查是否是同一路径或父子路径
                if manual_path_normalized == cache_dir_normalized:
                    return cache_dir
                if cache_dir_normalized in manual_path_normalized or manual_path_normalized in cache_dir_normalized:
                    return cache_dir

        return None


# 兼容旧接口
def detect_cache_smell(yml_input: Union[str, Path, Dict]) -> Dict:
    """检测缓存异味（兼容旧接口）"""
    from src.parsers.workflow_parser import WorkflowParser

    detector = CacheSmellDetector()
    parser = WorkflowParser()

    if isinstance(yml_input, dict):
        workflow = parser._parse_yaml_dict(yml_input)
        if workflow:
            result = detector.detect(workflow)
            return result.to_dict()
        return {'has_smell': False, 'error': '解析失败'}
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        if path.exists() and path.is_file():
            result = detector.detect_from_file(path)
            return result.to_dict()
        else:
            workflow = parser.parse_content(str(yml_input))
            if workflow:
                result = detector.detect(workflow)
                return result.to_dict()

    return {'error': '无效的输入类型', 'has_smell': False, 'smells': []}
