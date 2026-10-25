import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Union


class ParallelismDetector:
    """检测GitHub Actions工作流中并行度低的问题"""

    MULTI_VERSION_LANGUAGES = {
        'python': {
            'setup_action': 'actions/setup-python',
            'version_hint_patterns': [r'python-version', r'python_version'],
        },
        'node': {
            'setup_action': 'actions/setup-node',
            'version_hint_patterns': [r'node-version', r'node_version'],
        },
        'java': {
            'setup_action': 'actions/setup-java',
            'version_hint_patterns': [r'java-version', r'java_version', r'jdk-version'],
        },
        'go': {
            'setup_action': 'actions/setup-go',
            'version_hint_patterns': [r'go-version', r'go_version'],
        },
        'ruby': {
            'setup_action': 'actions/setup-ruby',
            'version_hint_patterns': [r'ruby-version', r'ruby_version'],
        },
        'dotnet': {
            'setup_action': 'actions/setup-dotnet',
            'version_hint_patterns': [r'dotnet-version', r'dotnet_version'],
        },
        'php': {
            'setup_action': 'actions/setup-php',
            'version_hint_patterns': [r'php-version', r'php_version'],
        },
    }

    OS_CANDIDATES = ['ubuntu-latest', 'windows-latest', 'macos-latest',
                     'ubuntu-22.04', 'windows-2022', 'macos-13', 'macos-14']

    HEAVY_JOB_PATTERNS = [
        r'\bbuild\b', r'\bcompile\b', r'\bmake\b', r'\bcargo\b',
        r'\bmvn\b', r'\bgradle\b', r'\bbazel\b', r'\bninja\b',
        r'\bdocker\s+build\b', r'\bwebpack\b', r'\brollup\b',
    ]

    SPLITTABLE_STEP_PATTERNS = [
        (r'\bunit\s*test\b', '单元测试'),
        (r'\bintegration\s*test\b', '集成测试'),
        (r'\be2e\s*test\b', '端到端测试'),
        (r'\bsmoke\s*test\b', '冒烟测试'),
        (r'\bapi\s*test\b', 'API测试'),
        (r'\bregression\s*test\b', '回归测试'),
        (r'\bperformance\s*test\b', '性能测试'),
        (r'\bsecurity\s*test\b', '安全测试'),
        (r'\blint\b', '代码检查'),
        (r'\bformat\b', '格式化'),
        (r'\btypecheck\b', '类型检查'),
        (r'\bcoverage\b', '覆盖率'),
        (r'\bbenchmark\b', '基准测试'),
    ]

    def __init__(self):
        self._compiled_heavy = [re.compile(p, re.IGNORECASE) for p in self.HEAVY_JOB_PATTERNS]
        self._compiled_splittable = [
            (re.compile(p, re.IGNORECASE), label) for p, label in self.SPLITTABLE_STEP_PATTERNS
        ]

    def detect_from_file(self, file_path: Union[str, Path]) -> Dict:
        file_path = Path(file_path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}', 'has_smell': False}
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return self.detect_from_content(content)

    def detect_from_content(self, content: str) -> Dict:
        try:
            workflow = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {'error': f'YAML解析错误: {e}', 'has_smell': False}
        if not workflow:
            return {'error': 'YAML内容为空', 'has_smell': False}
        return self._analyze_workflow(workflow)

    def _analyze_workflow(self, workflow: Dict) -> Dict:
        result = {
            'has_smell': False,
            'smells': [],
            'job_summary': [],
            'error': None
        }
        if 'jobs' not in workflow:
            result['error'] = '工作流中没有jobs定义'
            return result

        jobs = workflow.get('jobs', {})
        job_analyses = {}

        for job_name, job_config in jobs.items():
            if not isinstance(job_config, dict):
                continue
            analysis = self._analyze_job(job_name, job_config)
            job_analyses[job_name] = analysis
            result['job_summary'].append({
                'job_name': job_name,
                'step_count': analysis['step_count'],
                'has_matrix': analysis['has_matrix'],
                'matrix_combos': analysis['matrix_combos'],
                'needs': analysis['needs'],
                'is_heavy': analysis['is_heavy'],
            })

        self._check_serial_chain(job_analyses, result)
        self._check_missing_matrix(job_analyses, result)
        self._check_single_combo_matrix(job_analyses, result)
        self._check_oversized_job(job_analyses, result)

        result['has_smell'] = len(result['smells']) > 0
        return result

    def _analyze_job(self, job_name: str, job_config: Dict) -> Dict:
        steps = job_config.get('steps', [])
        step_count = len([s for s in steps if isinstance(s, dict)])
        needs = job_config.get('needs', [])
        if isinstance(needs, str):
            needs = [needs]

        strategy = job_config.get('strategy', {})
        has_matrix = isinstance(strategy, dict) and 'matrix' in strategy
        matrix_combos = self._estimate_matrix_size(strategy.get('matrix', {})) if has_matrix else 0

        step_text = self._collect_step_text(steps, job_name)
        is_heavy = any(p.search(step_text) for p in self._compiled_heavy)

        return {
            'step_count': step_count,
            'has_matrix': has_matrix,
            'matrix_combos': matrix_combos,
            'needs': needs,
            'is_heavy': is_heavy,
            'step_text': step_text,
            'steps': steps,
        }

    def _check_serial_chain(self, job_analyses: Dict, result: Dict):
        dag = {}
        for job_name, analysis in job_analyses.items():
            dag[job_name] = analysis['needs']

        chains = self._find_long_chains(dag, min_length=3)
        for chain in chains:
            chain_tasks = []
            for job_name in chain:
                a = job_analyses[job_name]
                chain_tasks.append(f"{job_name}({a['step_count']}步)")

            unnecessary_links = self._find_unnecessary_deps(chain, job_analyses)
            if unnecessary_links:
                result['smells'].append({
                    'type': 'unnecessary_serial_chain',
                    'severity': 'medium',
                    'job_name': ' → '.join(chain),
                    'message': f'存在{len(chain)}个Job串行链: {" → ".join(chain_tasks)}，部分依赖可能不必要',
                    'details': {
                        'chain': chain,
                        'unnecessary_links': unnecessary_links,
                        'suggestion': f'检查Job间的needs依赖，移除不必要的串行关系: {unnecessary_links}'
                    }
                })

    def _check_missing_matrix(self, job_analyses: Dict, result: Dict):
        for job_name, analysis in job_analyses.items():
            if analysis['has_matrix']:
                continue
            if not analysis['is_heavy']:
                continue

            step_text = analysis['step_text']
            multi_version_hints = self._detect_multi_version_hint(step_text)
            multi_os_hints = self._detect_multi_os_hint(step_text)

            if multi_version_hints:
                result['smells'].append({
                    'type': 'missing_matrix_version',
                    'severity': 'medium',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 检测到多版本暗示({", ".join(multi_version_hints)})但未使用矩阵策略',
                    'details': {
                        'hints': multi_version_hints,
                        'suggestion': '考虑使用strategy.matrix并行测试多个版本，例如:\n  strategy:\n    matrix:\n      python-version: ["3.8", "3.9", "3.10", "3.11"]'
                    }
                })

            if multi_os_hints:
                result['smells'].append({
                    'type': 'missing_matrix_os',
                    'severity': 'low',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 检测到跨平台暗示({", ".join(multi_os_hints)})但未使用矩阵策略',
                    'details': {
                        'hints': multi_os_hints,
                        'suggestion': '考虑使用strategy.matrix并行测试多个操作系统，例如:\n  strategy:\n    matrix:\n      os: [ubuntu-latest, windows-latest, macos-latest]'
                    }
                })

    def _check_single_combo_matrix(self, job_analyses: Dict, result: Dict):
        for job_name, analysis in job_analyses.items():
            if not analysis['has_matrix']:
                continue
            if analysis['matrix_combos'] == 1:
                result['smells'].append({
                    'type': 'single_combo_matrix',
                    'severity': 'low',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 使用矩阵策略但只有1个组合，等于没有并行',
                    'details': {
                        'matrix_combos': 1,
                        'suggestion': '矩阵只有1个组合时无并行效果，考虑添加更多版本/OS组合或移除矩阵'
                    }
                })

    def _check_oversized_job(self, job_analyses: Dict, result: Dict):
        for job_name, analysis in job_analyses.items():
            if analysis['step_count'] < 15:
                continue

            splittable = self._find_splittable_steps(analysis['steps'], job_name)
            if splittable:
                result['smells'].append({
                    'type': 'oversized_job',
                    'severity': 'medium',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 包含{analysis["step_count"]}个步骤，可拆分为并行Job',
                    'details': {
                        'step_count': analysis['step_count'],
                        'splittable_tasks': splittable,
                        'suggestion': f'可将以下独立任务拆分为并行Job: {", ".join(splittable)}'
                    }
                })

    def _estimate_matrix_size(self, matrix_config) -> Optional[int]:
        if not isinstance(matrix_config, dict):
            return None
        if 'include' in matrix_config and not any(k != 'include' for k in matrix_config):
            entries = matrix_config.get('include', [])
            return len(entries) if isinstance(entries, list) else None

        size = 1
        has_dimension = False
        for key, value in matrix_config.items():
            if key in ('include', 'exclude'):
                continue
            if isinstance(value, list):
                size *= len(value)
                has_dimension = True

        return size if has_dimension else None

    def _collect_step_text(self, steps: List[Dict], job_name: str) -> str:
        parts = [job_name]
        for step in steps:
            if not isinstance(step, dict):
                continue
            if 'run' in step:
                parts.append(step['run'])
            if 'uses' in step:
                parts.append(step['uses'])
            if 'name' in step:
                parts.append(step['name'])
        return '\n'.join(parts)

    def _find_long_chains(self, dag: Dict, min_length: int = 3) -> List[List[str]]:
        no_deps = [j for j, needs in dag.items() if not needs]
        all_chains = []

        def dfs(node, path):
            children = [j for j, needs in dag.items() if node in needs]
            if not children:
                if len(path) >= min_length:
                    all_chains.append(path[:])
                return
            for child in children:
                path.append(child)
                dfs(child, path)
                path.pop()

        for start in no_deps:
            dfs(start, [start])

        unique = []
        seen = set()
        for chain in all_chains:
            key = tuple(chain)
            if key not in seen:
                seen.add(key)
                unique.append(chain)
        return unique

    def _find_unnecessary_deps(self, chain: List[str], job_analyses: Dict) -> List[str]:
        unnecessary = []
        for i in range(len(chain) - 1):
            current = chain[i]
            next_job = chain[i + 1]
            current_produces = self._infer_outputs(job_analyses[current])
            next_consumes = self._infer_inputs(job_analyses[next_job])

            if not current_produces.intersection(next_consumes):
                unnecessary.append(f"{current} → {next_job}")

        return unnecessary

    def _infer_outputs(self, analysis: Dict) -> Set[str]:
        outputs = set()
        for step in analysis.get('steps', []):
            if not isinstance(step, dict):
                continue
            uses = step.get('uses', '')
            if 'upload-artifact' in uses:
                with_config = step.get('with', {})
                name = with_config.get('name', with_config.get('path', ''))
                if name:
                    outputs.add(f'artifact:{name}')
            if 'cache' in uses:
                outputs.add('cache')
        return outputs

    def _infer_inputs(self, analysis: Dict) -> Set[str]:
        inputs = set()
        for step in analysis.get('steps', []):
            if not isinstance(step, dict):
                continue
            uses = step.get('uses', '')
            if 'download-artifact' in uses:
                with_config = step.get('with', {})
                name = with_config.get('name', with_config.get('path', ''))
                if name:
                    inputs.add(f'artifact:{name}')
            if 'cache' in uses or 'restore' in uses:
                inputs.add('cache')
        return inputs

    def _detect_multi_version_hint(self, step_text: str) -> List[str]:
        hints = []
        for lang, config in self.MULTI_VERSION_LANGUAGES.items():
            if config['setup_action'] in step_text:
                for pattern in config['version_hint_patterns']:
                    if re.search(pattern, step_text, re.IGNORECASE):
                        hints.append(lang)
                        break
        return hints

    def _detect_multi_os_hint(self, step_text: str) -> List[str]:
        hints = []
        os_count = sum(1 for os_label in self.OS_CANDIDATES if os_label in step_text)
        if os_count >= 2:
            hints.append(f'引用了{os_count}种OS')
        cross_platform_keywords = [r'cross-platform', r'cross_platform', r'multi-os', r'multi_os']
        for kw in cross_platform_keywords:
            if re.search(kw, step_text, re.IGNORECASE):
                hints.append('跨平台关键词')
                break
        return hints

    def _find_splittable_steps(self, steps: List[Dict], job_name: str) -> List[str]:
        found = []
        step_text = self._collect_step_text(steps, job_name)
        seen_labels = set()
        for pattern, label in self._compiled_splittable:
            if pattern.search(step_text) and label not in seen_labels:
                found.append(label)
                seen_labels.add(label)
        return found


def detect_parallelism(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中并行度低的问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_smell: bool, 是否存在异味
              - smells: List, 检测到的异味列表
              - job_summary: List, 各job的并行度概要
              - error: str, 错误信息(如果有)
    """
    detector = ParallelismDetector()

    if isinstance(yml_input, dict):
        return detector._analyze_workflow(yml_input)
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        if path.exists() and path.is_file():
            return detector.detect_from_file(path)
        else:
            return detector.detect_from_content(str(yml_input))

    return {'error': '无效的输入类型', 'has_smell': False}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python parallelism_detector.py <yml_file_path>")
        print("  python parallelism_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_parallelism(yml_input)

    print("\n并行度检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在并行度低异味: {'是' if result['has_smell'] else '否'}")

        if result['job_summary']:
            print(f"\nJob并行度概要:")
            for job in result['job_summary']:
                matrix_str = f"{job['matrix_combos']}组合" if job['has_matrix'] else '无矩阵'
                needs_str = ', '.join(job['needs']) if job['needs'] else '无'
                print(f"  {job['job_name']}: {job['step_count']}步, 矩阵={matrix_str}, needs=[{needs_str}], 重量级={'是' if job['is_heavy'] else '否'}")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      Job: {smell['job_name']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")