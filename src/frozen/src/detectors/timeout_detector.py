import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Union


class TimeoutDetector:
    """检测GitHub Actions工作流中超时限制配置不当的问题"""

    DEFAULT_TIMEOUT_MINUTES = 360

    WORKFLOW_TYPE_THRESHOLDS = {
        'CI': {'recommended': 30, 'max_acceptable': 60},
        'Test': {'recommended': 30, 'max_acceptable': 60},
        'Lint': {'recommended': 15, 'max_acceptable': 30},
        'Build': {'recommended': 60, 'max_acceptable': 120},
        'Release': {'recommended': 30, 'max_acceptable': 60},
        'Security': {'recommended': 30, 'max_acceptable': 60},
        'Automation': {'recommended': 10, 'max_acceptable': 30},
        'Documentation': {'recommended': 15, 'max_acceptable': 30},
        'Container': {'recommended': 60, 'max_acceptable': 120},
        'Other': {'recommended': 30, 'max_acceptable': 60},
    }

    def __init__(self):
        self.heavy_task_keywords = [
            r'build', r'compile', r'make', r'cargo', r'mvn', r'gradle',
            r'npm\s+install', r'yarn\s+install', r'pip\s+install',
            r'test', r'e2e', r'integration', r'coverage',
            r'docker\s+build', r'docker\s+push', r'kaniko',
            r'deploy', r'release', r'publish',
        ]
        self.light_task_keywords = [
            r'lint', r'format', r'style', r'check', r'validate',
            r'typecheck', r'type-check',
            r'shellcheck', r'hadolint', r'markdownlint',
            r'stale', r'label', r'assign',
        ]
        self._compiled_heavy = [re.compile(p, re.IGNORECASE) for p in self.heavy_task_keywords]
        self._compiled_light = [re.compile(p, re.IGNORECASE) for p in self.light_task_keywords]

    def detect_from_file(self, file_path: Union[str, Path]) -> Dict:
        file_path = Path(file_path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}', 'has_timeout_smell': False}
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return self.detect_from_content(content)

    def detect_from_content(self, content: str) -> Dict:
        try:
            workflow = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {'error': f'YAML解析错误: {e}', 'has_timeout_smell': False}
        if not workflow:
            return {'error': 'YAML内容为空', 'has_timeout_smell': False}
        return self._analyze_workflow(workflow)

    def _analyze_workflow(self, workflow: Dict) -> Dict:
        result = {
            'has_timeout_smell': False,
            'smells': [],
            'job_summary': [],
            'error': None
        }
        if 'jobs' not in workflow:
            result['error'] = '工作流中没有jobs定义'
            return result

        workflow_path = ''
        for job_name, job_config in workflow.get('jobs', {}).items():
            if not isinstance(job_config, dict):
                continue
            self._analyze_job(job_name, job_config, result)

        result['has_timeout_smell'] = len(result['smells']) > 0
        return result

    def _analyze_job(self, job_name: str, job_config: Dict, result: Dict):
        timeout_minutes = job_config.get('timeout-minutes')
        has_timeout = timeout_minutes is not None

        if has_timeout:
            try:
                timeout_minutes = int(timeout_minutes)
            except (ValueError, TypeError):
                if isinstance(timeout_minutes, str) and timeout_minutes.strip().startswith('${{'):
                    result['job_summary'].append({
                        'job_name': job_name,
                        'timeout_minutes': 'dynamic',
                        'has_timeout': True,
                    })
                    return
                timeout_minutes = None
                has_timeout = False

        steps = job_config.get('steps', [])
        step_commands = []
        step_uses = []
        for step in steps:
            if 'run' in step:
                step_commands.append(step['run'])
            if 'uses' in step:
                step_uses.append(step['uses'])

        task_weight = self._estimate_task_weight(step_commands, step_uses, job_name)

        summary = {
            'job_name': job_name,
            'timeout_minutes': timeout_minutes,
            'has_timeout': has_timeout,
            'task_weight': task_weight,
        }
        result['job_summary'].append(summary)

        thresholds = self.WORKFLOW_TYPE_THRESHOLDS.get(task_weight, self.WORKFLOW_TYPE_THRESHOLDS['Other'])

        if not has_timeout:
            result['smells'].append({
                'type': 'missing_timeout',
                'severity': 'high',
                'job_name': job_name,
                'message': f'Job "{job_name}" 未设置timeout-minutes，默认{self.DEFAULT_TIMEOUT_MINUTES}分钟',
                'details': {
                    'current_timeout': self.DEFAULT_TIMEOUT_MINUTES,
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                    'suggestion': f'建议设置timeout-minutes: {thresholds["recommended"]}'
                }
            })
        elif timeout_minutes > thresholds['max_acceptable']:
            result['smells'].append({
                'type': 'excessive_timeout',
                'severity': 'medium',
                'job_name': job_name,
                'message': f'Job "{job_name}" 超时设置过大({timeout_minutes}分钟)，任务类型为{task_weight}',
                'details': {
                    'current_timeout': timeout_minutes,
                    'max_acceptable': thresholds['max_acceptable'],
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                    'suggestion': f'建议降低到timeout-minutes: {thresholds["recommended"]}'
                }
            })
        elif timeout_minutes > thresholds['recommended']:
            result['smells'].append({
                'type': 'loose_timeout',
                'severity': 'low',
                'job_name': job_name,
                'message': f'Job "{job_name}" 超时设置偏大({timeout_minutes}分钟)，任务类型为{task_weight}',
                'details': {
                    'current_timeout': timeout_minutes,
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                    'suggestion': f'建议降低到timeout-minutes: {thresholds["recommended"]}'
                }
            })

        self._check_step_timeouts(job_name, steps, result)

    def _check_step_timeouts(self, job_name: str, steps: List[Dict], result: Dict):
        long_running_step_keywords = [
            r'npm\s+install', r'yarn\s+install', r'pip\s+install',
            r'cargo\s+build', r'mvn\s+package', r'gradle\s+build',
            r'docker\s+build', r'make', r'cmake\s+--build',
            r'test', r'e2e', r'integration',
        ]
        compiled = [re.compile(p, re.IGNORECASE) for p in long_running_step_keywords]

        for i, step in enumerate(steps):
            run_content = step.get('run', '')
            step_name = step.get('name', step.get('id', f'step-{i+1}'))
            has_timeout = 'timeout-minutes' in step

            is_long_running = any(p.search(run_content) for p in compiled)

            if is_long_running and not has_timeout:
                result['smells'].append({
                    'type': 'missing_step_timeout',
                    'severity': 'low',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 的步骤 "{step_name}" 可能是长时间运行的操作但未设置step级timeout-minutes',
                    'details': {
                        'step_name': step_name,
                        'suggestion': '考虑为耗时步骤设置step级timeout-minutes作为额外保护'
                    }
                })

    def _estimate_task_weight(self, step_commands: List[str], step_uses: List[str], job_name: str) -> str:
        combined = '\n'.join(step_commands) + '\n' + '\n'.join(step_uses) + '\n' + job_name

        light_score = sum(1 for p in self._compiled_light if p.search(combined))
        heavy_score = sum(1 for p in self._compiled_heavy if p.search(combined))

        if light_score > 0 and heavy_score == 0:
            return 'Lint'
        if heavy_score > 0:
            build_keywords = ['build', 'compile', 'make', 'cargo', 'docker', 'container', 'kaniko']
            if any(kw in combined.lower() for kw in build_keywords):
                return 'Build'
            deploy_keywords = ['deploy', 'release', 'publish']
            if any(kw in combined.lower() for kw in deploy_keywords):
                return 'Release'
            test_keywords = ['test', 'e2e', 'integration', 'coverage']
            if any(kw in combined.lower() for kw in test_keywords):
                return 'Test'
            return 'CI'

        return 'Other'


def detect_timeout(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中超时限制配置不当的问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_timeout_smell: bool, 是否存在超时配置异味
              - smells: List, 检测到的异味列表
              - job_summary: List, 各job的超时配置概要
              - error: str, 错误信息(如果有)
    """
    detector = TimeoutDetector()

    if isinstance(yml_input, dict):
        return detector._analyze_workflow(yml_input)
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        if path.exists() and path.is_file():
            return detector.detect_from_file(path)
        else:
            return detector.detect_from_content(str(yml_input))

    return {'error': '无效的输入类型', 'has_timeout_smell': False}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python timeout_detector.py <yml_file_path>")
        print("  python timeout_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_timeout(yml_input)

    print("\n超时限制检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在超时配置异味: {'是' if result['has_timeout_smell'] else '否'}")

        if result['job_summary']:
            print(f"\nJob超时配置概要:")
            for job in result['job_summary']:
                timeout_str = str(job['timeout_minutes']) if job['has_timeout'] else '未设置(默认360分钟)'
                print(f"  {job['job_name']}: {timeout_str} (任务类型: {job['task_weight']})")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      Job: {smell['job_name']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")