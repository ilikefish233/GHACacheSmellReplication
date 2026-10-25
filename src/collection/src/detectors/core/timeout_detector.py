"""
超时限制检测器（重构版）

检测 GitHub Actions 工作流中超时限制配置不当的问题。
"""

import re
from typing import Dict, List, Optional, Any, Union
from pathlib import Path

from src.models.workflow import WorkflowModel
from src.detectors.base import (
    BaseDetector,
    DetectorConfig,
    SmellInstance,
    DetectorResult,
    SeverityLevel,
    create_detector_result,
)


class TimeoutDetector(BaseDetector):
    """超时限制检测器

    检测工作流中是否存在超时配置不当的问题：
    1. missing_timeout: 未设置 timeout-minutes
    2. excessive_timeout: 超时设置过大
    3. loose_timeout: 超时设置偏大
    4. missing_step_timeout: 长时间运行步骤未设置 step 级超时
    """

    NAME = "timeout"
    DESCRIPTION = "检测工作流中超时限制配置不当的问题"
    SMELL_TYPES = {
        "missing_timeout",
        "excessive_timeout",
        "loose_timeout",
        "missing_step_timeout",
    }

    # 默认超时（GitHub Actions 默认值）
    DEFAULT_TIMEOUT_MINUTES = 360

    # 工作流类型阈值（分钟）
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

    # 重任务关键词
    HEAVY_TASK_KEYWORDS = [
        r'build', r'compile', r'make', r'cargo', r'mvn', r'gradle',
        r'npm\s+install', r'yarn\s+install', r'pip\s+install',
        r'test', r'e2e', r'integration', r'coverage',
        r'docker\s+build', r'docker\s+push', r'kaniko',
        r'deploy', r'release', r'publish',
    ]

    # 轻量任务关键词
    LIGHT_TASK_KEYWORDS = [
        r'lint', r'format', r'style', r'check', r'validate',
        r'typecheck', r'type-check',
        r'shellcheck', r'hadolint', r'markdownlint',
        r'stale', r'label', r'assign',
    ]

    # 长时间运行步骤关键词
    LONG_RUNNING_STEP_KEYWORDS = [
        r'npm\s+install', r'yarn\s+install', r'pip\s+install',
        r'cargo\s+build', r'mvn\s+package', r'gradle\s+build',
        r'docker\s+build', r'make', r'cmake\s+--build',
        r'test', r'e2e', r'integration',
    ]

    def __init__(self, config: Optional[DetectorConfig] = None):
        """初始化"""
        super().__init__(config)

        # 预编译正则表达式
        self._compiled_heavy = [re.compile(p, re.IGNORECASE) for p in self.HEAVY_TASK_KEYWORDS]
        self._compiled_light = [re.compile(p, re.IGNORECASE) for p in self.LIGHT_TASK_KEYWORDS]
        self._compiled_long_running = [re.compile(p, re.IGNORECASE) for p in self.LONG_RUNNING_STEP_KEYWORDS]

    def detect(self, workflow: WorkflowModel) -> DetectorResult:
        """检测工作流中的超时配置问题"""
        if not workflow.has_jobs:
            return create_detector_result(
                detector_name=self.NAME,
                smells=[],
                error='工作流中没有 jobs 定义'
            )

        smells = []
        job_summary = []

        for job_name, job in workflow.jobs.items():
            job_smells, summary = self._analyze_job(job_name, job)
            smells.extend(job_smells)
            job_summary.append(summary)

        return create_detector_result(
            detector_name=self.NAME,
            smells=self.filter_smells(smells),
            metadata={
                'job_summary': job_summary,
            }
        )

    def _analyze_job(self, job_name: str, job) -> tuple:
        """分析单个 Job 的超时配置"""
        smells = []
        summary = {
            'job_name': job_name,
            'timeout_minutes': job.timeout_minutes,
            'has_timeout': job.timeout_minutes is not None,
            'task_weight': 'Other',
        }

        # 估算任务权重
        task_weight = self._estimate_task_weight(job, job_name)
        summary['task_weight'] = task_weight

        # 获取阈值
        thresholds = self.WORKFLOW_TYPE_THRESHOLDS.get(
            task_weight,
            self.WORKFLOW_TYPE_THRESHOLDS['Other']
        )

        # 检查是否缺少超时设置
        if job.timeout_minutes is None:
            smells.append(self.create_smell(
                smell_type='missing_timeout',
                severity=SeverityLevel.HIGH,
                message=f'Job "{job_name}" 未设置 timeout-minutes，默认 {self.DEFAULT_TIMEOUT_MINUTES} 分钟',
                job_name=job_name,
                details={
                    'current_timeout': self.DEFAULT_TIMEOUT_MINUTES,
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                },
                suggestion=f'建议设置 timeout-minutes: {thresholds["recommended"]}'
            ))
        elif job.timeout_minutes > thresholds['max_acceptable']:
            smells.append(self.create_smell(
                smell_type='excessive_timeout',
                severity=SeverityLevel.MEDIUM,
                message=f'Job "{job_name}" 超时设置过大 ({job.timeout_minutes} 分钟)，任务类型为 {task_weight}',
                job_name=job_name,
                details={
                    'current_timeout': job.timeout_minutes,
                    'max_acceptable': thresholds['max_acceptable'],
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                },
                suggestion=f'建议降低到 timeout-minutes: {thresholds["recommended"]}'
            ))
        elif job.timeout_minutes > thresholds['recommended']:
            smells.append(self.create_smell(
                smell_type='loose_timeout',
                severity=SeverityLevel.LOW,
                message=f'Job "{job_name}" 超时设置偏大 ({job.timeout_minutes} 分钟)，任务类型为 {task_weight}',
                job_name=job_name,
                details={
                    'current_timeout': job.timeout_minutes,
                    'recommended_timeout': thresholds['recommended'],
                    'task_weight': task_weight,
                },
                suggestion=f'建议降低到 timeout-minutes: {thresholds["recommended"]}'
            ))

        # 检查步骤级别的超时
        step_smells = self._check_step_timeouts(job_name, job)
        smells.extend(step_smells)

        return smells, summary

    def _check_step_timeouts(self, job_name: str, job) -> List[SmellInstance]:
        """检查步骤级别的超时配置"""
        smells = []

        for i, step in enumerate(job.steps):
            step_name = step.name or step.id or f'step-{i+1}'
            has_timeout = step.timeout_minutes is not None

            # 检查是否为长时间运行步骤
            is_long_running = False
            if step.run:
                is_long_running = any(
                    pattern.search(step.run)
                    for pattern in self._compiled_long_running
                )

            if is_long_running and not has_timeout:
                smells.append(self.create_smell(
                    smell_type='missing_step_timeout',
                    severity=SeverityLevel.LOW,
                    message=f'Job "{job_name}" 的步骤 "{step_name}" 可能是长时间运行的操作但未设置 step 级 timeout-minutes',
                    job_name=job_name,
                    step_name=step_name,
                    details={
                        'suggestion': '考虑为耗时步骤设置 step 级 timeout-minutes 作为额外保护'
                    }
                ))

        return smells

    def _estimate_task_weight(self, job, job_name: str) -> str:
        """估算任务权重/类型"""
        # 收集所有文本内容
        combined_parts = [job_name]
        for step in job.steps:
            if step.run:
                combined_parts.append(step.run)
            if step.uses:
                combined_parts.append(step.uses)
        combined = '\n'.join(combined_parts)

        light_score = sum(1 for p in self._compiled_light if p.search(combined))
        heavy_score = sum(1 for p in self._compiled_heavy if p.search(combined))

        if light_score > 0 and heavy_score == 0:
            return 'Lint'

        if heavy_score > 0:
            combined_lower = combined.lower()

            build_keywords = ['build', 'compile', 'make', 'cargo', 'docker', 'container', 'kaniko']
            if any(kw in combined_lower for kw in build_keywords):
                return 'Build'

            deploy_keywords = ['deploy', 'release', 'publish']
            if any(kw in combined_lower for kw in deploy_keywords):
                return 'Release'

            test_keywords = ['test', 'e2e', 'integration', 'coverage']
            if any(kw in combined_lower for kw in test_keywords):
                return 'Test'

            return 'CI'

        return 'Other'


# 兼容旧接口的独立函数
def detect_timeout(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测 GitHub Actions 工作流中超时限制配置不当的问题（兼容旧接口）

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果
    """
    from src.parsers.workflow_parser import WorkflowParser

    detector = TimeoutDetector()
    parser = WorkflowParser()

    if isinstance(yml_input, dict):
        workflow = parser._parse_yaml_dict(yml_input)
        if workflow:
            result = detector.detect(workflow)
            return result.to_dict()
        return {'has_timeout_smell': False, 'error': '解析失败'}
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

    return {'error': '无效的输入类型', 'has_timeout_smell': False}
