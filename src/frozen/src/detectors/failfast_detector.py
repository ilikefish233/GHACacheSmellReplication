import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Union


class FailFastDetector:
    """检测GitHub Actions工作流中fail-fast和continue-on-error使用不当的问题"""

    CRITICAL_STEP_PATTERNS = [
        (r'\btest\b', '测试'),
        (r'\bunit\s*test\b', '单元测试'),
        (r'\bintegration\s*test\b', '集成测试'),
        (r'\be2e\b', '端到端测试'),
        (r'\bcoverage\b', '覆盖率检查'),
        (r'\bbuild\b', '构建'),
        (r'\bcompile\b', '编译'),
        (r'\bmake\b', 'Make构建'),
        (r'\bcargo\s+build\b', 'Cargo构建'),
        (r'\bmvn\b', 'Maven构建'),
        (r'\bgradle\b', 'Gradle构建'),
        (r'\bnpm\s+test\b', 'npm测试'),
        (r'\byarn\s+test\b', 'yarn测试'),
        (r'\bpytest\b', 'pytest测试'),
        (r'\bjest\b', 'Jest测试'),
        (r'\bmocha\b', 'Mocha测试'),
        (r'\brspec\b', 'RSpec测试'),
        (r'\bgo\s+test\b', 'Go测试'),
        (r'\bcargo\s+test\b', 'Cargo测试'),
        (r'\bdotnet\s+test\b', '.NET测试'),
    ]

    NON_CRITICAL_STEP_PATTERNS = [
        (r'\bcache\b', '缓存操作'),
        (r'\bupload\b', '上传产物'),
        (r'\bdownload\b', '下载产物'),
        (r'\bartifact\b', 'Artifact操作'),
        (r'\bnotify\b', '通知'),
        (r'\bslack\b', 'Slack通知'),
        (r'\bteams\b', 'Teams通知'),
        (r'\bemail\b', '邮件通知'),
        (r'\bcoverage\s+report\b', '覆盖率报告上传'),
        (r'\bcodecov\b', 'Codecov上传'),
        (r'\bcoveralls\b', 'Coveralls上传'),
        (r'\bdeploy\b.*\bpreview\b', '预览部署'),
        (r'\bcomment\b', '评论'),
        (r'\blabel\b', '标签操作'),
        (r'\bbadge\b', '徽章更新'),
        (r'\bcheck\s+run\b', 'Check Run更新'),
    ]

    NON_CRITICAL_ACTIONS = [
        'actions/upload-artifact',
        'actions/download-artifact',
        'actions/cache',
        'actions/save-cache',
        'codecov/codecov-action',
        'coverallsapp/github-action',
        '8398a7/action-slack',
        'slackapi/slack-github-action',
        'rtCamp/action-slack-notify',
        'peaceiris/actions-gh-pages',
        'JamesIves/github-pages-deploy-action',
        'peter-evans/create-or-update-comment',
        'peter-evans/commit-comment',
        'actions-ecosystem/action-create-comment',
        'actions/github-script',
    ]

    def __init__(self):
        self._compiled_critical = [
            (re.compile(p, re.IGNORECASE), label) for p, label in self.CRITICAL_STEP_PATTERNS
        ]
        self._compiled_non_critical = [
            (re.compile(p, re.IGNORECASE), label) for p, label in self.NON_CRITICAL_STEP_PATTERNS
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

        for job_name, job_config in workflow.get('jobs', {}).items():
            if not isinstance(job_config, dict):
                continue
            self._analyze_job(job_name, job_config, result)

        result['has_smell'] = len(result['smells']) > 0
        return result

    def _analyze_job(self, job_name: str, job_config: Dict, result: Dict):
        strategy = job_config.get('strategy', {})
        has_matrix = 'matrix' in strategy if isinstance(strategy, dict) else False
        fail_fast = strategy.get('fail-fast', True) if isinstance(strategy, dict) else True
        job_continue_on_error = job_config.get('continue-on-error', False)

        summary = {
            'job_name': job_name,
            'has_matrix': has_matrix,
            'fail_fast': fail_fast,
            'job_continue_on_error': job_continue_on_error,
        }
        result['job_summary'].append(summary)

        if has_matrix and fail_fast is True:
            matrix_size = self._estimate_matrix_size(strategy.get('matrix', {}))
            if matrix_size and matrix_size > 2:
                result['smells'].append({
                    'type': 'fail_fast_on_matrix',
                    'severity': 'medium',
                    'job_name': job_name,
                    'message': f'Job "{job_name}" 使用矩阵策略({matrix_size}个组合)但fail-fast为true(默认)，首个失败会取消其余组合',
                    'details': {
                        'fail_fast': fail_fast,
                        'matrix_size': matrix_size,
                        'suggestion': '建议设置strategy.fail-fast: false，以便看到所有平台/版本的失败信息'
                    }
                })

        if job_continue_on_error is True:
            result['smells'].append({
                'type': 'job_continue_on_error',
                'severity': 'high',
                'job_name': job_name,
                'message': f'Job "{job_name}" 设置了continue-on-error: true，所有步骤失败都会被忽略',
                'details': {
                    'suggestion': 'Job级continue-on-error会掩盖所有失败，建议移除或仅在特定步骤上使用'
                }
            })

        steps = job_config.get('steps', [])
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            self._analyze_step(job_name, step, i, result)

    def _analyze_step(self, job_name: str, step: Dict, step_index: int, result: Dict):
        step_continue_on_error = step.get('continue-on-error', False)
        step_name = step.get('name', step.get('id', f'step-{step_index + 1}'))
        uses = step.get('uses', '')
        run_content = step.get('run', '')
        step_text = f"{step_name} {uses} {run_content}"

        is_critical = self._is_critical_step(step_text, uses)
        is_non_critical = self._is_non_critical_step(step_text, uses)

        if is_critical and step_continue_on_error is True:
            matched_labels = self._get_critical_labels(step_text, uses)
            result['smells'].append({
                'type': 'critical_step_continue_on_error',
                'severity': 'high',
                'job_name': job_name,
                'message': f'Job "{job_name}" 的关键步骤 "{step_name}" 设置了continue-on-error: true，失败会被静默忽略',
                'details': {
                    'step_name': step_name,
                    'step_type': ', '.join(matched_labels),
                    'suggestion': f'关键步骤({", ".join(matched_labels)})不应忽略失败，建议移除continue-on-error'
                }
            })

        if is_non_critical and step_continue_on_error is not True:
            matched_labels = self._get_non_critical_labels(step_text, uses)
            result['smells'].append({
                'type': 'non_critical_step_missing_continue_on_error',
                'severity': 'low',
                'job_name': job_name,
                'message': f'Job "{job_name}" 的非关键步骤 "{step_name}" 未设置continue-on-error: true，其失败会导致整个workflow失败',
                'details': {
                    'step_name': step_name,
                    'step_type': ', '.join(matched_labels),
                    'suggestion': f'非关键步骤({", ".join(matched_labels)})建议设置continue-on-error: true，避免影响workflow结果'
                }
            })

    def _is_critical_step(self, step_text: str, uses: str) -> bool:
        for pattern, _ in self._compiled_critical:
            if pattern.search(step_text):
                return True
        return False

    def _is_non_critical_step(self, step_text: str, uses: str) -> bool:
        for action_prefix in self.NON_CRITICAL_ACTIONS:
            if action_prefix in uses:
                return True
        for pattern, _ in self._compiled_non_critical:
            if pattern.search(step_text):
                return True
        return False

    def _get_critical_labels(self, step_text: str, uses: str) -> List[str]:
        labels = []
        for pattern, label in self._compiled_critical:
            if pattern.search(step_text):
                labels.append(label)
        return labels if labels else ['未知关键步骤']

    def _get_non_critical_labels(self, step_text: str, uses: str) -> List[str]:
        labels = []
        for action_prefix in self.NON_CRITICAL_ACTIONS:
            if action_prefix in uses:
                labels.append(action_prefix.split('/')[-1])
        for pattern, label in self._compiled_non_critical:
            if pattern.search(step_text):
                labels.append(label)
        return labels if labels else ['未知非关键步骤']

    def _estimate_matrix_size(self, matrix_config: Dict) -> Optional[int]:
        if not isinstance(matrix_config, dict):
            return None
        if 'include' in matrix_config and not any(k != 'include' for k in matrix_config):
            entries = matrix_config.get('include', [])
            return len(entries) if isinstance(entries, list) else None

        size = 1
        has_dimension = False
        for key, value in matrix_config.items():
            if key == 'include' or key == 'exclude':
                continue
            if isinstance(value, list):
                size *= len(value)
                has_dimension = True

        return size if has_dimension else None


def detect_fail_fast(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中fail-fast和continue-on-error使用不当的问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_smell: bool, 是否存在异味
              - smells: List, 检测到的异味列表
              - job_summary: List, 各job的配置概要
              - error: str, 错误信息(如果有)
    """
    detector = FailFastDetector()

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
        print("  python fail_fast_detector.py <yml_file_path>")
        print("  python fail_fast_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_fail_fast(yml_input)

    print("\nfail-fast/continue-on-error检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在配置异味: {'是' if result['has_smell'] else '否'}")

        if result['job_summary']:
            print(f"\nJob配置概要:")
            for job in result['job_summary']:
                ff_str = str(job['fail_fast'])
                coe_str = str(job['job_continue_on_error'])
                matrix_str = '是' if job['has_matrix'] else '否'
                print(f"  {job['job_name']}: matrix={matrix_str}, fail-fast={ff_str}, continue-on-error={coe_str}")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      Job: {smell['job_name']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")