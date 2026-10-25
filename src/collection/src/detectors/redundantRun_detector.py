import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union


class RedundantRunDetector:
    """检测GitHub Actions工作流中冗余运行的问题"""

    DEPENDENCY_INSTALL_PATTERNS = [
        (r'\bnpm\s+(install|ci|i)\b', 'npm'),
        (r'\byarn\s+(install|ci)\b', 'yarn'),
        (r'\bpnpm\s+(install|ci)\b', 'pnpm'),
        (r'\bpip\s+install\b', 'pip'),
        (r'\bpip3\s+install\b', 'pip3'),
        (r'\bpoetry\s+install\b', 'poetry'),
        (r'\bpipenv\s+install\b', 'pipenv'),
        (r'\bbundle\s+install\b', 'bundle'),
        (r'\bgem\s+install\b', 'gem'),
        (r'\bcargo\s+build\b', 'cargo'),
        (r'\bgo\s+(mod\s+)?download\b', 'go'),
        (r'\bgo\s+install\b', 'go'),
        (r'\bmvn\s+(install|compile|package)\b', 'maven'),
        (r'\bgradle\s+(build|install|assemble)\b', 'gradle'),
        (r'\bdotnet\s+(restore|build)\b', 'dotnet'),
        (r'\bnuget\s+restore\b', 'nuget'),
        (r'\bcomposer\s+install\b', 'composer'),
        (r'\bbower\s+install\b', 'bower'),
        (r'\bapt-get\s+install\b', 'apt'),
        (r'\bbrew\s+install\b', 'brew'),
        (r'\bchoco\s+install\b', 'choco'),
    ]

    SETUP_ACTIONS = [
        'actions/setup-node',
        'actions/setup-python',
        'actions/setup-java',
        'actions/setup-go',
        'actions/setup-dotnet',
        'actions/setup-ruby',
        'actions/setup-haskell',
        'actions/setup-elixir',
        'actions/setup-php',
        'actions/setup-rust',
    ]

    DUPLICATE_PRONE_ACTIONS = {
        'actions/checkout': 'checkout',
        'actions/cache': 'cache',
        'actions/upload-artifact': 'upload-artifact',
        'actions/download-artifact': 'download-artifact',
        'actions/setup-node': 'setup-node',
        'actions/setup-python': 'setup-python',
        'actions/setup-java': 'setup-java',
        'actions/setup-go': 'setup-go',
        'actions/setup-dotnet': 'setup-dotnet',
        'actions/setup-ruby': 'setup-ruby',
    }

    def __init__(self):
        self._compiled_dep = [
            (re.compile(p, re.IGNORECASE), label) for p, label in self.DEPENDENCY_INSTALL_PATTERNS
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
                'dep_installs': analysis['dep_installs'],
                'setup_actions': analysis['setup_actions'],
                'checkout_count': analysis['checkout_count'],
                'needs': analysis['needs'],
            })

        self._check_duplicate_dep_installs(job_analyses, result)
        self._check_duplicate_setup_actions(job_analyses, result)
        self._check_duplicate_checkout(job_analyses, result)
        self._check_missing_concurrency(workflow, result)
        self._check_push_pr_double_run(workflow, result)

        result['has_smell'] = len(result['smells']) > 0
        return result

    def _analyze_job(self, job_name: str, job_config: Dict) -> Dict:
        dep_installs = []
        setup_actions = []
        checkout_count = 0
        needs = job_config.get('needs', [])
        if isinstance(needs, str):
            needs = [needs]

        for step in job_config.get('steps', []):
            if not isinstance(step, dict):
                continue

            run_content = step.get('run', '')
            uses = step.get('uses', '')

            for pattern, label in self._compiled_dep:
                if pattern.search(run_content):
                    dep_installs.append(label)

            for setup_action in self.SETUP_ACTIONS:
                if setup_action in uses:
                    setup_actions.append(setup_action)

            if 'actions/checkout' in uses:
                checkout_count += 1

        return {
            'dep_installs': dep_installs,
            'setup_actions': setup_actions,
            'checkout_count': checkout_count,
            'needs': needs,
        }

    def _check_duplicate_dep_installs(self, job_analyses: Dict, result: Dict):
        dep_job_map: Dict[str, List[str]] = {}
        for job_name, analysis in job_analyses.items():
            for dep in analysis['dep_installs']:
                if dep not in dep_job_map:
                    dep_job_map[dep] = []
                dep_job_map[dep].append(job_name)

        for dep, job_names in dep_job_map.items():
            if len(job_names) > 1:
                independent_jobs = self._filter_independent_jobs(job_names, job_analyses)
                if len(independent_jobs) > 1:
                    result['smells'].append({
                        'type': 'duplicate_dep_install',
                        'severity': 'medium',
                        'job_name': ', '.join(independent_jobs),
                        'message': f'{len(independent_jobs)}个独立Job重复安装{dep}依赖: {", ".join(independent_jobs)}',
                        'details': {
                            'package_manager': dep,
                            'jobs': independent_jobs,
                            'suggestion': f'考虑使用缓存或artifact共享{dep}依赖，避免每个Job都重新安装'
                        }
                    })

    def _check_duplicate_setup_actions(self, job_analyses: Dict, result: Dict):
        setup_job_map: Dict[str, List[str]] = {}
        for job_name, analysis in job_analyses.items():
            for action in analysis['setup_actions']:
                if action not in setup_job_map:
                    setup_job_map[action] = []
                setup_job_map[action].append(job_name)

        for action, job_names in setup_job_map.items():
            if len(job_names) > 1:
                independent_jobs = self._filter_independent_jobs(job_names, job_analyses)
                if len(independent_jobs) > 1:
                    result['smells'].append({
                        'type': 'duplicate_setup_action',
                        'severity': 'low',
                        'job_name': ', '.join(independent_jobs),
                        'message': f'{len(independent_jobs)}个独立Job重复使用{action}',
                        'details': {
                            'action': action,
                            'jobs': independent_jobs,
                            'suggestion': f'如果{action}配置了缓存，重复使用的开销较小；否则考虑合并Job或共享缓存'
                        }
                    })

    def _check_duplicate_checkout(self, job_analyses: Dict, result: Dict):
        for job_name, analysis in job_analyses.items():
            if analysis['checkout_count'] > 1:
                result['smells'].append({
                    'type': 'duplicate_checkout',
                    'severity': 'medium',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 包含{analysis["checkout_count"]}次actions/checkout',
                    'details': {
                        'checkout_count': analysis['checkout_count'],
                        'suggestion': '同一Job内多次checkout通常不必要，检查是否可以复用已checkout的代码'
                    }
                })

    def _check_missing_concurrency(self, workflow: Dict, result: Dict):
        has_concurrency = 'concurrency' in workflow
        on_config = workflow.get('on', {})
        if isinstance(on_config, dict):
            has_push = 'push' in on_config
            has_pr = 'pull_request' in on_config
            has_schedule = 'schedule' in on_config
        else:
            has_push = has_pr = has_schedule = False

        needs_concurrency = has_push or has_pr or has_schedule

        if needs_concurrency and not has_concurrency:
            result['smells'].append({
                'type': 'missing_concurrency',
                'severity': 'medium',
                'job_name': '(workflow)',
                'message': '工作流未配置concurrency，同一分支的多次push/PR可能同时运行造成冗余',
                'details': {
                    'suggestion': '添加concurrency配置以取消同一分支的旧运行:\n  concurrency:\n    group: ${{ github.workflow }}-${{ github.ref }}\n    cancel-in-progress: true'
                }
            })

    def _check_push_pr_double_run(self, workflow: Dict, result: Dict):
        on_config = workflow.get('on', {})
        if not isinstance(on_config, dict):
            return

        push_branches = self._extract_branches(on_config.get('push', {}))
        pr_branches = self._extract_branches(on_config.get('pull_request', {}))

        if push_branches is not None and pr_branches is not None:
            overlap = set(push_branches) & set(pr_branches)
            if not overlap:
                return

            has_concurrency = 'concurrency' in workflow
            concurrency_cancel = False
            if has_concurrency:
                concurrency_config = workflow.get('concurrency', {})
                if isinstance(concurrency_config, dict):
                    concurrency_cancel = concurrency_config.get('cancel-in-progress', False)

            if not concurrency_cancel:
                result['smells'].append({
                    'type': 'push_pr_double_run',
                    'severity': 'high',
                    'job_name': '(workflow)',
                    'message': f'push和pull_request在相同分支重叠({sorted(overlap)})，同仓库PR会触发两次运行且未配置concurrency取消',
                    'details': {
                        'overlap_branches': sorted(overlap),
                        'suggestion': '方案1: 对push添加paths-ignore过滤PR不会修改的文件; 方案2: 添加concurrency配置并设置cancel-in-progress: true'
                    }
                })

    def _filter_independent_jobs(self, job_names: List[str], job_analyses: Dict) -> List[str]:
        all_needs = set()
        for job_name in job_names:
            if job_name in job_analyses:
                for need in job_analyses[job_name]['needs']:
                    all_needs.add(need)

        dependent = set()
        for job_name in job_names:
            if job_name in all_needs:
                dependent.add(job_name)

        return [j for j in job_names if j not in dependent]

    def _extract_branches(self, event_config) -> Optional[List[str]]:
        if event_config is None or not isinstance(event_config, dict):
            return None
        branches = event_config.get('branches', event_config.get('branches-ignore'))
        if branches is None:
            return None
        if isinstance(branches, list):
            return branches
        return None


def detect_redundant_run(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中冗余运行的问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_smell: bool, 是否存在异味
              - smells: List, 检测到的异味列表
              - job_summary: List, 各job的运行概要
              - error: str, 错误信息(如果有)
    """
    detector = RedundantRunDetector()

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
        print("  python redundant_run_detector.py <yml_file_path>")
        print("  python redundant_run_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_redundant_run(yml_input)

    print("\n冗余运行检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在冗余运行异味: {'是' if result['has_smell'] else '否'}")

        if result['job_summary']:
            print(f"\nJob运行概要:")
            for job in result['job_summary']:
                deps = ', '.join(job['dep_installs']) if job['dep_installs'] else '无'
                setups = ', '.join(job['setup_actions']) if job['setup_actions'] else '无'
                needs = ', '.join(job['needs']) if job['needs'] else '无'
                print(f"  {job['job_name']}: 依赖安装=[{deps}], setup=[{setups}], checkout={job['checkout_count']}次, needs=[{needs}]")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      Job: {smell['job_name']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")