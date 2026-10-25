import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Union


class RunnerDetector:
    """检测GitHub Actions工作流中Runner选择不合理的问题"""

    RUNNER_PROFILES = {
        'ubuntu-latest': {'os': 'linux', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 1},
        'ubuntu-22.04': {'os': 'linux', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 1},
        'ubuntu-20.04': {'os': 'linux', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 1},
        'ubuntu-latest-4-cores': {'os': 'linux', 'cores': 4, 'ram_gb': 16, 'cost_multiplier': 2},
        'ubuntu-latest-8-cores': {'os': 'linux', 'cores': 8, 'ram_gb': 32, 'cost_multiplier': 4},
        'ubuntu-latest-16-cores': {'os': 'linux', 'cores': 16, 'ram_gb': 64, 'cost_multiplier': 8},
        'ubuntu-22.04-4-cores': {'os': 'linux', 'cores': 4, 'ram_gb': 16, 'cost_multiplier': 2},
        'ubuntu-22.04-8-cores': {'os': 'linux', 'cores': 8, 'ram_gb': 32, 'cost_multiplier': 4},
        'ubuntu-22.04-16-cores': {'os': 'linux', 'cores': 16, 'ram_gb': 64, 'cost_multiplier': 8},
        'windows-latest': {'os': 'windows', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 2},
        'windows-2022': {'os': 'windows', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 2},
        'windows-2019': {'os': 'windows', 'cores': 2, 'ram_gb': 4, 'cost_multiplier': 2},
        'windows-latest-4-cores': {'os': 'windows', 'cores': 4, 'ram_gb': 16, 'cost_multiplier': 4},
        'windows-latest-8-cores': {'os': 'windows', 'cores': 8, 'ram_gb': 32, 'cost_multiplier': 8},
        'windows-latest-16-cores': {'os': 'windows', 'cores': 16, 'ram_gb': 64, 'cost_multiplier': 16},
        'macos-latest': {'os': 'macos', 'cores': 3, 'ram_gb': 14, 'cost_multiplier': 10},
        'macos-13': {'os': 'macos', 'cores': 3, 'ram_gb': 14, 'cost_multiplier': 10},
        'macos-12': {'os': 'macos', 'cores': 3, 'ram_gb': 14, 'cost_multiplier': 10},
        'macos-14': {'os': 'macos', 'cores': 3, 'ram_gb': 14, 'cost_multiplier': 10},
        'macos-latest-large': {'os': 'macos', 'cores': 6, 'ram_gb': 14, 'cost_multiplier': 20},
        'macos-latest-xlarge': {'os': 'macos', 'cores': 12, 'ram_gb': 14, 'cost_multiplier': 40},
    }

    LIGHT_TASK_KEYWORDS = [
        r'\blint\b', r'\bformat\b', r'\bstyle\b', r'\bcheck\b', r'\bvalidate\b',
        r'\btypecheck\b', r'\btype-check\b',
        r'\bshellcheck\b', r'\bhadolint\b', r'\bmarkdownlint\b', r'\beslint\b',
        r'\bprettier\b', r'\bblack\b', r'\bflake8\b', r'\bruff\b', r'\bisort\b',
        r'\bstale\b', r'\blabel\b', r'\bassign\b',
        r'\bauto-merge\b', r'\bauto-approve\b',
    ]

    HEAVY_TASK_KEYWORDS = [
        r'\bbuild\b', r'\bcompile\b', r'\bmake\b', r'\bcargo\b', r'\bmvn\b',
        r'\bgradle\b', r'\bbazel\b', r'\bninja\b', r'\bmsbuild\b',
        r'\bdocker\s+build\b', r'\bkaniko\b', r'\bbuildah\b',
        r'\btest\b', r'\be2e\b', r'\bintegration\b',
        r'\bcoverage\b', r'\bbenchmark\b', r'\bprofiling\b',
        r'\bwebpack\b', r'\brollup\b', r'\bvite\s+build\b',
        r'\brustc\b', r'\bcargo\s+build\b', r'\bcargo\s+clippy\b',
    ]

    MACOS_SPECIFIC_KEYWORDS = [
        r'\bxcode\b', r'\bxcodebuild\b', r'\bswift\b', r'\bswift-build\b',
        r'\bcocoapods\b', r'\bpod\s+install\b', r'\bfastlane\b',
        r'\bswiftui\b', r'\bobjc\b', r'\bobjective-c\b',
        r'\bmacos\b', r'\bdarwin\b', r'\b.app\b',
        r'\bhomebrew\b', r'\bbrew\b',
        r'\bnotariz\b', r'\bsigning\b', r'\bcodesign\b',
        r'\b.dmg\b', r'\b.pkg\b',
        r'\bflutter\b.*\bios\b', r'\breact-native\b.*\bios\b',
    ]

    WINDOWS_SPECIFIC_KEYWORDS = [
        r'\bmsbuild\b', r'\b\.sln\b', r'\b\.csproj\b',
        r'\bpowershell\b', r'\bnuget\b',
        r'\bvisual\s+studio\b', r'\b\.net\b', r'\b\.net\s+framework\b',
        r'\bwinget\b', r'\bchoco\b', r'\bchocolatey\b',
        r'\b\.exe\b', r'\b\.msi\b',
        r'\bflutter\b.*\bwindows\b', r'\breact-native\b.*\bwindows\b',
    ]

    def __init__(self):
        self._compiled_light = [re.compile(p, re.IGNORECASE) for p in self.LIGHT_TASK_KEYWORDS]
        self._compiled_heavy = [re.compile(p, re.IGNORECASE) for p in self.HEAVY_TASK_KEYWORDS]
        self._compiled_macos = [re.compile(p, re.IGNORECASE) for p in self.MACOS_SPECIFIC_KEYWORDS]
        self._compiled_windows = [re.compile(p, re.IGNORECASE) for p in self.WINDOWS_SPECIFIC_KEYWORDS]

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

        for job_name, job_config in workflow.get('jobs', {}).items():
            if not isinstance(job_config, dict):
                continue
            self._analyze_job(job_name, job_config, result)

        result['has_smell'] = len(result['smells']) > 0
        return result

    def _analyze_job(self, job_name: str, job_config: Dict, result: Dict):
        runs_on = job_config.get('runs-on')
        if not runs_on:
            return

        runner_label = self._resolve_runner_label(runs_on)
        profile = self._get_runner_profile(runner_label)

        steps = job_config.get('steps', [])
        step_text = self._collect_step_text(steps, job_name)

        task_type = self._classify_task(step_text)
        needs_macos = self._needs_macos(step_text)
        needs_windows = self._needs_windows(step_text)

        summary = {
            'job_name': job_name,
            'runs_on': str(runs_on),
            'runner_label': runner_label,
            'runner_profile': profile,
            'task_type': task_type,
        }
        result['job_summary'].append(summary)

        if profile is None:
            return

        self._check_oversized_runner(job_name, runner_label, profile, task_type, result)
        self._check_undersized_runner(job_name, runner_label, profile, task_type, result)
        self._check_unnecessary_macos(job_name, runner_label, profile, needs_macos, result)
        self._check_unnecessary_windows(job_name, runner_label, profile, needs_windows, result)

    def _resolve_runner_label(self, runs_on) -> Optional[str]:
        if isinstance(runs_on, str):
            return runs_on
        if isinstance(runs_on, list):
            for label in runs_on:
                if isinstance(label, str) and not label.startswith('self-'):
                    return label
        if isinstance(runs_on, dict):
            group = runs_on.get('group', '')
            labels = runs_on.get('labels', '')
            if isinstance(labels, str):
                return labels
            if isinstance(labels, list) and labels:
                return labels[0]
        return None

    def _get_runner_profile(self, runner_label: Optional[str]) -> Optional[Dict]:
        if not runner_label:
            return None
        return self.RUNNER_PROFILES.get(runner_label)

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

    def _classify_task(self, step_text: str) -> str:
        light_score = sum(1 for p in self._compiled_light if p.search(step_text))
        heavy_score = sum(1 for p in self._compiled_heavy if p.search(step_text))

        if light_score > 0 and heavy_score == 0:
            return 'light'
        if heavy_score > 0 and light_score == 0:
            return 'heavy'
        if heavy_score > 0 and light_score > 0:
            return 'mixed'
        return 'unknown'

    def _needs_macos(self, step_text: str) -> bool:
        return any(p.search(step_text) for p in self._compiled_macos)

    def _needs_windows(self, step_text: str) -> bool:
        return any(p.search(step_text) for p in self._compiled_windows)

    def _check_oversized_runner(self, job_name: str, runner_label: str, profile: Dict, task_type: str, result: Dict):
        cores = profile.get('cores', 2)
        cost_multiplier = profile.get('cost_multiplier', 1)

        if task_type == 'light' and cores >= 4:
            result['smells'].append({
                'type': 'oversized_runner',
                'severity': 'medium',
                'job_name': job_name,
                'message': f'Job "{job_name}" 是轻量级任务但使用了{cores}核runner({runner_label})，成本是标准runner的{cost_multiplier}倍',
                'details': {
                    'runner': runner_label,
                    'cores': cores,
                    'cost_multiplier': cost_multiplier,
                    'task_type': task_type,
                    'suggestion': f'轻量级任务(如lint/format)建议使用标准2核runner(如ubuntu-latest)，可节省{cost_multiplier - 1}倍成本'
                }
            })

    def _check_undersized_runner(self, job_name: str, runner_label: str, profile: Dict, task_type: str, result: Dict):
        cores = profile.get('cores', 2)

        if task_type == 'heavy' and cores <= 2:
            result['smells'].append({
                'type': 'undersized_runner',
                'severity': 'medium',
                'job_name': job_name,
                'message': f'Job "{job_name}" 是重量级任务但仅使用{cores}核runner({runner_label})，构建可能很慢',
                'details': {
                    'runner': runner_label,
                    'cores': cores,
                    'task_type': task_type,
                    'suggestion': '重量级任务(如build/compile)建议使用4核或8核runner(如ubuntu-latest-4-cores)，可显著缩短构建时间'
                }
            })

    def _check_unnecessary_macos(self, job_name: str, runner_label: str, profile: Dict, needs_macos: bool, result: Dict):
        os_type = profile.get('os', '')
        cost_multiplier = profile.get('cost_multiplier', 1)

        if os_type == 'macos' and not needs_macos:
            result['smells'].append({
                'type': 'unnecessary_macos',
                'severity': 'high',
                'job_name': job_name,
                'message': f'Job "{job_name}" 使用macOS runner({runner_label})但未检测到macOS专属操作，成本是Linux的{cost_multiplier}倍',
                'details': {
                    'runner': runner_label,
                    'cost_multiplier': cost_multiplier,
                    'suggestion': f'macOS runner计费是Linux的{cost_multiplier}倍，如无需macOS专属功能(如Xcode/Swift/CocoaPods)，建议改用ubuntu-latest'
                }
            })

    def _check_unnecessary_windows(self, job_name: str, runner_label: str, profile: Dict, needs_windows: bool, result: Dict):
        os_type = profile.get('os', '')
        cost_multiplier = profile.get('cost_multiplier', 1)

        if os_type == 'windows' and not needs_windows:
            result['smells'].append({
                'type': 'unnecessary_windows',
                'severity': 'medium',
                'job_name': job_name,
                'message': f'Job "{job_name}" 使用Windows runner({runner_label})但未检测到Windows专属操作，成本是Linux的{cost_multiplier}倍',
                'details': {
                    'runner': runner_label,
                    'cost_multiplier': cost_multiplier,
                    'suggestion': f'Windows runner计费是Linux的{cost_multiplier}倍，如无需Windows专属功能(如MSBuild/.NET/PowerShell)，建议改用ubuntu-latest'
                }
            })


def detect_runner(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中Runner选择不合理的问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_smell: bool, 是否存在异味
              - smells: List, 检测到的异味列表
              - job_summary: List, 各job的runner配置概要
              - error: str, 错误信息(如果有)
    """
    detector = RunnerDetector()

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
        print("  python runner_detector.py <yml_file_path>")
        print("  python runner_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_runner(yml_input)

    print("\nRunner选择检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在Runner选择异味: {'是' if result['has_smell'] else '否'}")

        if result['job_summary']:
            print(f"\nJob Runner配置概要:")
            for job in result['job_summary']:
                profile = job.get('runner_profile') or {}
                cores = profile.get('cores', '?')
                cost = profile.get('cost_multiplier', '?')
                print(f"  {job['job_name']}: {job['runs_on']} ({cores}核, 成本x{cost}, 任务类型: {job['task_type']})")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      Job: {smell['job_name']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")