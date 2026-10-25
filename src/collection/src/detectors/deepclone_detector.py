import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Union


class DeepCloneDetector:
    """检测GitHub Actions工作流中的深克隆问题"""

    def __init__(self):
        self.history_dependent_commands = [
            r'git\s+log',
            r'git\s+diff',
            r'git\s+blame',
            r'git\s+rev-list',
            r'git\s+shortlog',
            r'git\s+describe',
            r'git\s+merge-base',
            r'conventional-changelog',
            r'semantic-release',
            r'changelog',
            r'git-cliff',
            r'commitizen',
            r'lerna\s+changed',
            r'nx\s+affected',
            r'git\s+fetch',
            r'git\s+checkout\s+--',
            r'git\s+stash',
            r'git\s+cherry-pick',
            r'git\s+revert',
            r'git\s+rebase',
            r'git\s+merge',
        ]
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.history_dependent_commands]

    def detect_from_file(self, file_path: Union[str, Path]) -> Dict:
        file_path = Path(file_path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}', 'has_deep_clone': False}
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return self.detect_from_content(content)

    def detect_from_content(self, content: str) -> Dict:
        try:
            workflow = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {'error': f'YAML解析错误: {e}', 'has_deep_clone': False}
        if not workflow:
            return {'error': 'YAML内容为空', 'has_deep_clone': False}
        return self._analyze_workflow(workflow, content)

    def _analyze_workflow(self, workflow: Dict, raw_content: str = '') -> Dict:
        result = {
            'has_deep_clone': False,
            'deep_clone_jobs': [],
            'shallow_clone_jobs': [],
            'checkout_count': 0,
            'error': None
        }
        if 'jobs' not in workflow:
            result['error'] = '工作流中没有jobs定义'
            return result
        for job_name, job_config in workflow.get('jobs', {}).items():
            self._analyze_job(job_name, job_config, result)
        return result

    def _analyze_job(self, job_name: str, job_config: Dict, result: Dict):
        checkout_steps = []
        all_step_commands = []

        for step in job_config.get('steps', []):
            uses = step.get('uses', '')
            if 'actions/checkout' in uses:
                fetch_depth = None
                with_config = step.get('with', {})
                if 'fetch-depth' in with_config:
                    fd = with_config['fetch-depth']
                    try:
                        fetch_depth = int(fd)
                    except (ValueError, TypeError):
                        if isinstance(fd, str) and fd.strip().startswith('${{'):
                            fetch_depth = -1
                        else:
                            fetch_depth = None
                checkout_steps.append({
                    'uses': uses,
                    'fetch_depth': fetch_depth,
                    'step_name': step.get('name', step.get('id', '')),
                })
                result['checkout_count'] += 1

            run_content = step.get('run', '')
            if run_content:
                all_step_commands.append(run_content)

        needs_history = self._needs_git_history(all_step_commands)

        if not checkout_steps:
            return

        for cs in checkout_steps:
            fd = cs['fetch_depth']
            is_deep = fd is None or fd == 0
            is_partial = fd is not None and fd > 1
            is_shallow = fd is not None and fd == 1

            if is_deep:
                cs['clone_type'] = 'deep'
                cs['smell_severity'] = 'high' if not needs_history else 'none'
                cs['needs_history'] = needs_history
            elif is_partial:
                cs['clone_type'] = 'partial'
                cs['smell_severity'] = 'medium' if not needs_history else 'none'
                cs['needs_history'] = needs_history
            elif is_shallow:
                cs['clone_type'] = 'shallow'
                cs['smell_severity'] = 'none'
                cs['needs_history'] = needs_history

            if cs['smell_severity'] != 'none':
                result['has_deep_clone'] = True
                if job_name not in [j['job_name'] for j in result['deep_clone_jobs']]:
                    result['deep_clone_jobs'].append({
                        'job_name': job_name,
                        'checkout_steps': [cs],
                        'needs_history': needs_history,
                    })
                else:
                    for j in result['deep_clone_jobs']:
                        if j['job_name'] == job_name:
                            j['checkout_steps'].append(cs)
            else:
                if job_name not in [j['job_name'] for j in result['shallow_clone_jobs']]:
                    result['shallow_clone_jobs'].append({
                        'job_name': job_name,
                        'checkout_steps': [cs],
                        'needs_history': needs_history,
                    })

    def _needs_git_history(self, commands: List[str]) -> bool:
        combined = '\n'.join(commands)
        for pattern in self._compiled_patterns:
            if pattern.search(combined):
                return True
        return False


def detect_deep_clone(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中的深克隆问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_deep_clone: bool, 是否存在深克隆异味
              - deep_clone_jobs: List, 有深克隆问题的jobs
              - shallow_clone_jobs: List, 正确使用浅克隆的jobs
              - checkout_count: int, checkout步骤总数
              - error: str, 错误信息(如果有)
    """
    detector = DeepCloneDetector()

    if isinstance(yml_input, dict):
        return detector._analyze_workflow(yml_input)
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        if path.exists() and path.is_file():
            return detector.detect_from_file(path)
        else:
            return detector.detect_from_content(str(yml_input))

    return {'error': '无效的输入类型', 'has_deep_clone': False}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python deep_clone_detector.py <yml_file_path>")
        print("  python deep_clone_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_deep_clone(yml_input)

    print("\n深克隆检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在深克隆异味: {'是' if result['has_deep_clone'] else '否'}")
        print(f"Checkout步骤数: {result['checkout_count']}")

        if result['deep_clone_jobs']:
            print(f"\n有深克隆问题的jobs:")
            for job in result['deep_clone_jobs']:
                print(f"\n  Job: {job['job_name']}")
                print(f"  需要git历史: {'是' if job['needs_history'] else '否'}")
                for step in job['checkout_steps']:
                    print(f"    - Action: {step['uses']}")
                    print(f"      克隆类型: {step['clone_type']}")
                    print(f"      fetch-depth: {step['fetch_depth']}")
                    print(f"      严重程度: {step['smell_severity']}")

        if result['shallow_clone_jobs']:
            print(f"\n正确使用浅克隆的jobs:")
            for job in result['shallow_clone_jobs']:
                print(f"\n  Job: {job['job_name']}")
                for step in job['checkout_steps']:
                    print(f"    - Action: {step['uses']}")
                    print(f"      克隆类型: {step['clone_type']}")
                    print(f"      fetch-depth: {step['fetch_depth']}")