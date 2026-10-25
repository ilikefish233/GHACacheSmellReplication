"""
缓存使用检测器（重构版）

检测 GitHub Actions 工作流中的缓存使用情况。
支持显式缓存、包管理器缓存、Docker 层缓存等类型。
"""

import re
from typing import Dict, List, Optional, Set, Any, Union
from pathlib import Path

from src.models.workflow import WorkflowModel
from src.detectors.base import (
    ReusableWorkflowDetector,
    DetectorConfig,
    SmellInstance,
    DetectorResult,
    SeverityLevel,
    create_detector_result,
)


class CacheDetector(ReusableWorkflowDetector):
    """缓存使用检测器

    检测工作流中是否使用了缓存，以及缓存的类型和配置。

    检测的缓存类型：
    1. explicit_cache: 显式缓存 (actions/cache, actions/restore, actions/save)
    2. package_manager_cache: 包管理器缓存 (setup actions 的内置缓存)
    3. docker_layer_cache: Docker 层缓存 (docker/build-push-action)
    4. other_cache: 其他第三方缓存 action
    """

    NAME = "cache"
    DESCRIPTION = "检测工作流中的缓存使用情况"
    SMELL_TYPES = {"no_cache"}

    # Setup actions 配置
    SETUP_ACTIONS_CONFIG = {
        'actions/setup-node': {
            'default_cache_version': 5,
            'npm_default_version': 6,
        },
        'actions/setup-python': {
            'default_cache_version': None,
        },
        'actions/setup-java': {
            'default_cache_version': None,
        },
        'actions/setup-ruby': {
            'default_cache_version': None,
        },
        'actions/setup-go': {
            'default_cache_version': 4,
        },
        'actions/setup-dotnet': {
            'default_cache_version': None,
        },
    }

    # 显式缓存模式
    EXPLICIT_CACHE_PATTERNS = [
        'actions/cache',
        'actions/restore',
        'actions/save',
    ]

    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        workflows_dir: Optional[Path] = None,
        reusable_map: Optional[Dict[str, Dict[str, str]]] = None
    ):
        """初始化

        Args:
            config: 检测器配置
            workflows_dir: 工作流文件目录
            reusable_map: 可复用工作流映射
        """
        super().__init__(config, workflows_dir, reusable_map)

    def detect(self, workflow: WorkflowModel) -> DetectorResult:
        """检测工作流中的缓存使用

        Args:
            workflow: 已解析的工作流模型

        Returns:
            DetectorResult: 检测结果
        """
        if not workflow.has_jobs:
            return create_detector_result(
                detector_name=self.NAME,
                smells=[],
                error='工作流中没有 jobs 定义'
            )

        cache_info = self._analyze_workflow(workflow)

        # 如果没有使用缓存，创建一个"无缓存"异味
        smells = []
        if not cache_info['uses_cache']:
            smells.append(self.create_smell(
                smell_type='no_cache',
                severity=SeverityLevel.HIGH,
                message='工作流未使用缓存，依赖安装可能重复执行',
                details={
                    'suggestion': '建议使用 actions/cache 或 setup action 的内置缓存功能'
                }
            ))

        return create_detector_result(
            detector_name=self.NAME,
            smells=smells,
            metadata={
                'uses_cache': cache_info['uses_cache'],
                'cache_categories': cache_info['cache_categories'],
                'cache_count': cache_info['cache_count'],
                'cache_jobs': cache_info['cache_jobs'],
                'jobs': cache_info.get('jobs', {}),
            }
        )

    def _analyze_workflow(self, workflow: WorkflowModel) -> Dict[str, Any]:
        """分析工作流的缓存使用情况"""
        result = {
            'uses_cache': False,
            'cache_categories': [],
            'cache_jobs': [],
            'cache_count': 0,
            'jobs': {},
        }

        all_categories = set()

        for job_name, job in workflow.jobs.items():
            job_cache_info = self._analyze_job(job, workflow)

            if job_cache_info['uses_cache']:
                result['uses_cache'] = True
                result['cache_jobs'].append(job_cache_info)
                result['cache_count'] += len(job_cache_info['cache_steps'])
                all_categories.update(job_cache_info.get('cache_categories', []))

                result['jobs'][job_name] = {
                    'display_name': job.display_name,
                    'cache_categories': job_cache_info.get('cache_categories', []),
                    'cache_steps': [
                        {
                            'action': cs.get('action', ''),
                            'cache_type': cs.get('cache_type'),
                            'cache_category': cs.get('cache_category'),
                        }
                        for cs in job_cache_info.get('cache_steps', [])
                    ],
                }

        result['cache_categories'] = sorted(list(all_categories))
        return result

    def _analyze_job(self, job, workflow: WorkflowModel) -> Dict[str, Any]:
        """分析单个 Job 的缓存使用"""
        # 处理可复用工作流引用
        if job.uses and job.uses.startswith('./.github/workflows/'):
            return self._analyze_reusable_job(job, workflow)

        # 检测包管理器
        package_managers = self._detect_package_managers(job)

        job_cache_info = {
            'job_name': job.name,
            'display_name': job.display_name,
            'uses_cache': False,
            'cache_categories': [],
            'cache_steps': []
        }

        job_categories = set()

        for step in job.steps:
            cache_info = self._analyze_step(step, package_managers)
            if cache_info['uses_cache']:
                job_cache_info['uses_cache'] = True
                job_cache_info['cache_steps'].append(cache_info)
                if cache_info.get('cache_category'):
                    job_categories.add(cache_info['cache_category'])

        job_cache_info['cache_categories'] = sorted(list(job_categories))
        return job_cache_info

    def _analyze_reusable_job(self, job, workflow: WorkflowModel) -> Dict[str, Any]:
        """分析可复用工作流引用的缓存使用"""
        result = {
            'job_name': job.name,
            'display_name': job.display_name,
            'uses_cache': False,
            'cache_categories': [],
            'cache_steps': [],
            'reusable_workflow': job.uses,
        }

        if not self.parser or not workflow.workflow_path:
            return result

        # 解析可复用工作流
        reusable_model = self.parser.parse_reusable_workflow(job.uses, workflow)
        if not reusable_model:
            return result

        # 递归分析
        wf_result = self._analyze_workflow(reusable_model)

        if wf_result['uses_cache']:
            result['uses_cache'] = True
            result['cache_categories'] = wf_result.get('cache_categories', [])
            for job_data in wf_result.get('cache_jobs', []):
                for step in job_data.get('cache_steps', []):
                    step_copy = dict(step)
                    step_copy['from_reusable'] = job.uses
                    result['cache_steps'].append(step_copy)

        return result

    def _detect_package_managers(self, job) -> Set[str]:
        """检测 Job 中使用的包管理器"""
        package_managers = set()

        for step in job.steps:
            if step.run:
                if re.search(r'\bnpm\s+(ci|install|i)\b', step.run):
                    package_managers.add('npm')
                if re.search(r'\byarn\s+(install|ci)\b', step.run):
                    package_managers.add('yarn')
                if re.search(r'\bpnpm\s+(install|ci)\b', step.run):
                    package_managers.add('pnpm')

            if step.uses and 'actions/setup-node' in step.uses:
                cache_val = step.with_.get('cache')
                if cache_val and cache_val != 'false' and isinstance(cache_val, str):
                    package_managers.add(cache_val)

        return package_managers

    def _analyze_step(self, step, package_managers: Set[str]) -> Dict[str, Any]:
        """分析单个步骤的缓存使用"""
        cache_info = {
            'uses_cache': False,
            'cache_category': None,
            'cache_type': None,
            'action': None,
            'details': {}
        }

        if not step.uses:
            return cache_info

        cache_info['action'] = step.uses
        version = step.uses_version

        # Category 1: Explicit cache
        for pattern in self.EXPLICIT_CACHE_PATTERNS:
            if pattern in step.uses:
                cache_info['uses_cache'] = True
                cache_info['cache_category'] = 'explicit_cache'
                cache_info['cache_type'] = pattern
                cache_info['details'] = {
                    'key': step.with_.get('key'),
                    'path': step.with_.get('path'),
                    'restore-keys': step.with_.get('restore-keys')
                }
                return cache_info

        # Category 2: Package manager cache
        for setup_action, config in self.SETUP_ACTIONS_CONFIG.items():
            if setup_action in step.uses:
                has_explicit_cache = (
                    step.with_.get('cache') is not None
                    and step.with_.get('cache') is not False
                )
                default_cache_version = config.get('default_cache_version')
                npm_default_version = config.get('npm_default_version')

                if has_explicit_cache:
                    cache_info['uses_cache'] = True
                    cache_info['cache_category'] = 'package_manager_cache'
                    cache_info['cache_type'] = setup_action
                    cache_info['details'] = {
                        'cache': step.with_.get('cache'),
                        'reason': 'explicit_cache_config',
                        'version': version
                    }
                elif default_cache_version is not None and version is not None and version >= default_cache_version:
                    if npm_default_version is not None and version >= npm_default_version:
                        if 'npm' in package_managers:
                            cache_info['uses_cache'] = True
                            cache_info['cache_category'] = 'package_manager_cache'
                            cache_info['cache_type'] = setup_action
                            cache_info['details'] = {
                                'reason': f'v{version}_default_cache_with_npm',
                                'version': version
                            }
                    else:
                        cache_info['uses_cache'] = True
                        cache_info['cache_category'] = 'package_manager_cache'
                        cache_info['cache_type'] = setup_action
                        cache_info['details'] = {
                            'reason': f'v{version}_default_cache',
                            'version': version
                        }
                return cache_info

        # Category 3: Docker layer cache
        if 'docker/build-push-action' in step.uses:
            if step.with_.get('cache-from') or step.with_.get('cache-to'):
                cache_info['uses_cache'] = True
                cache_info['cache_category'] = 'docker_layer_cache'
                cache_info['cache_type'] = 'docker/build-push-action'
                cache_info['details'] = {
                    'cache-from': step.with_.get('cache-from'),
                    'cache-to': step.with_.get('cache-to'),
                }
            return cache_info

        # Category 4: Other (third-party actions with cache-related params)
        if step.with_:
            cache_keys = [k for k in step.with_ if 'cache' in k.lower()]
            if cache_keys:
                cache_info['uses_cache'] = True
                cache_info['cache_category'] = 'other_cache'
                cache_info['cache_type'] = step.uses
                cache_info['details'] = {
                    'cache_params': {k: step.with_[k] for k in cache_keys}
                }

        return cache_info


# 兼容旧接口的独立函数
def detect_cache(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测 GitHub Actions 工作流中的 cache 使用情况（兼容旧接口）

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果
    """
    from src.parsers.workflow_parser import WorkflowParser

    detector = CacheDetector()
    parser = WorkflowParser()

    if isinstance(yml_input, dict):
        workflow = parser._parse_yaml_dict(yml_input)
        if workflow:
            result = detector.detect(workflow)
            return result.to_dict()
        return {'uses_cache': False, 'error': '解析失败'}
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

    return {'error': '无效的输入类型', 'uses_cache': False}
