"""
工作流调用类型分析器

分析工作流的调用特征：
- calls_workflow: 是否调用其他工作流
- uses_custom_action: 是否使用自定义 action
- runs_shell_script: 是否运行 shell 脚本
"""

from typing import Dict, Any, Set
from pathlib import Path

from src.models.workflow import WorkflowModel


class WorkflowCallAnalyzer:
    """工作流调用类型分析器"""

    def __init__(self):
        """初始化"""
        self.reusable_workflow_patterns = [
            './.github/workflows/',
            '.github/workflows/',
        ]

    def analyze(self, workflow: WorkflowModel) -> Dict[str, Any]:
        """分析工作流调用类型

        Args:
            workflow: 工作流模型

        Returns:
            分析结果字典
        """
        result = {
            'calls_workflow': False,
            'uses_custom_action': False,
            'runs_shell_script': False,
            'custom_actions': set(),
            'called_workflows': set(),
        }

        for job in workflow.jobs.values():
            # 检查可复用工作流调用
            if job.uses:
                for pattern in self.reusable_workflow_patterns:
                    if pattern in job.uses:
                        result['calls_workflow'] = True
                        result['called_workflows'].add(job.uses)
                        break

            # 检查自定义 action 和 shell 脚本
            for step in job.steps:
                if step.uses:
                    # 检查是否为自定义 action（非 actions/* 和其他官方 action）
                    if self._is_custom_action(step.uses):
                        result['uses_custom_action'] = True
                        result['custom_actions'].add(step.uses)

                # 检查是否运行 shell 脚本
                if step.run:
                    result['runs_shell_script'] = True

        # 转换 set 为 list 以便 JSON 序列化
        result['custom_actions'] = list(result['custom_actions'])
        result['called_workflows'] = list(result['called_workflows'])

        return result

    def _is_custom_action(self, uses: str) -> bool:
        """检查是否为自定义 action"""
        if not uses:
            return False

        # 官方 action 前缀
        official_prefixes = [
            'actions/',
            'github/',
            'aws/',
            'azure/',
            'docker/',
            'google/',
            'microsoft/',
            'docker://',
        ]

        uses_lower = uses.lower()
        for prefix in official_prefixes:
            if uses_lower.startswith(prefix):
                return False

        # 如果包含 @ 可能是 action 引用
        if '@' in uses and '/' in uses:
            return True

        return False

    def analyze_batch(self, workflows: list) -> Dict[str, Any]:
        """批量分析工作流

        Args:
            workflows: 工作流模型列表

        Returns:
            统计结果
        """
        stats = {
            'total': len(workflows),
            'calls_workflow': 0,
            'uses_custom_action': 0,
            'runs_shell_script': 0,
            'overlap': {
                'calls_and_custom': 0,
                'calls_and_script': 0,
                'custom_and_script': 0,
                'all_three': 0,
            }
        }

        for workflow in workflows:
            result = self.analyze(workflow)

            if result['calls_workflow']:
                stats['calls_workflow'] += 1
            if result['uses_custom_action']:
                stats['uses_custom_action'] += 1
            if result['runs_shell_script']:
                stats['runs_shell_script'] += 1

            # 统计重叠
            has_calls = result['calls_workflow']
            has_custom = result['uses_custom_action']
            has_script = result['runs_shell_script']

            if has_calls and has_custom:
                stats['overlap']['calls_and_custom'] += 1
            if has_calls and has_script:
                stats['overlap']['calls_and_script'] += 1
            if has_custom and has_script:
                stats['overlap']['custom_and_script'] += 1
            if has_calls and has_custom and has_script:
                stats['overlap']['all_three'] += 1

        # 计算百分比
        if stats['total'] > 0:
            for key in ['calls_workflow', 'uses_custom_action', 'runs_shell_script']:
                stats[f'{key}_pct'] = round(stats[key] / stats['total'] * 100, 1)

        return stats
