"""
GitHub Actions 工作流数据模型

提供统一的工作流数据结构，用于在检测器之间共享解析结果。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any
from datetime import datetime
import re


@dataclass
class StepInfo:
    """工作流步骤信息"""
    name: str = ""
    id: Optional[str] = None
    uses: Optional[str] = None
    run: Optional[str] = None
    if_: Optional[str] = None
    with_: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, Any] = field(default_factory=dict)
    timeout_minutes: Optional[int] = None
    continue_on_error: bool = False

    @property
    def uses_action(self) -> Optional[str]:
        """获取使用的 action（不含版本）"""
        if not self.uses:
            return None
        return self.uses.split('@')[0] if '@' in self.uses else self.uses

    @property
    def uses_version(self) -> Optional[int]:
        """获取 action 版本号"""
        if not isinstance(self.uses, str) or '@' not in self.uses:
            return None
        version_part = self.uses.rsplit('@', 1)[1].strip()
        if re.fullmatch(r'[0-9a-fA-F]{40}', version_part):
            return None
        # Parse major/minor/full stable releases; branches, SHAs and prereleases
        # cannot inherit stable release capabilities merely from a prefix.
        match = re.fullmatch(r'v?(0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){0,2}(?:\+[0-9A-Za-z.-]+)?', version_part)
        return int(match.group(1)) if match else None


@dataclass
class JobInfo:
    """工作流 Job 信息"""
    name: str
    display_name: str = ""
    runs_on: Optional[Any] = None  # 可能是字符串、列表或字典
    needs: List[str] = field(default_factory=list)
    if_: Optional[str] = None
    env: Dict[str, Any] = field(default_factory=dict)
    timeout_minutes: Optional[int] = None
    steps: List[StepInfo] = field(default_factory=list)
    uses: Optional[str] = None  # 可复用工作流引用
    strategy: Optional[Dict[str, Any]] = None
    matrix: Optional[Dict[str, Any]] = None
    container: Optional[Dict[str, Any]] = None
    services: Optional[Dict[str, Any]] = None
    permissions: Optional[Dict[str, Any]] = None
    concurrency: Optional[Dict[str, Any]] = None
    continue_on_error: bool = False

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name


@dataclass
class TriggerInfo:
    """工作流触发器信息"""
    push: Optional[Dict[str, Any]] = None
    pull_request: Optional[Dict[str, Any]] = None
    workflow_dispatch: bool = False
    workflow_run: Optional[Dict[str, Any]] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    repository_dispatch: bool = False
    manual: bool = False  # legacy
    other: Dict[str, Any] = field(default_factory=dict)

    @property
    def trigger_types(self) -> Set[str]:
        """获取所有触发器类型"""
        types = set()
        if self.push is not None:
            types.add('push')
        if self.pull_request is not None:
            types.add('pull_request')
        if self.workflow_dispatch:
            types.add('workflow_dispatch')
        if self.workflow_run is not None:
            types.add('workflow_run')
        if self.schedule:
            types.add('schedule')
        if self.repository_dispatch:
            types.add('repository_dispatch')
        if self.manual:
            types.add('manual')
        types.update(self.other.keys())
        return types


@dataclass
class WorkflowModel:
    """工作流数据模型

    统一的工作流数据结构，用于在检测器之间共享解析结果。
    包含工作流的所有关键信息，避免重复解析 YAML。
    """
    name: str = ""
    filename: str = ""
    raw_content: str = ""
    yaml_data: Dict[str, Any] = field(default_factory=dict)
    jobs: Dict[str, JobInfo] = field(default_factory=dict)
    triggers: TriggerInfo = field(default_factory=TriggerInfo)
    env: Dict[str, Any] = field(default_factory=dict)
    permissions: Optional[Dict[str, Any]] = None
    concurrency: Optional[Dict[str, Any]] = None
    defaults: Optional[Dict[str, Any]] = None
    run_name: Optional[str] = None
    run_number: Optional[str] = None

    # 元数据
    file_path: Optional[str] = None
    repository_name: Optional[str] = None
    workflow_path: Optional[str] = None
    workflow_id: Optional[str] = None
    file_sha: Optional[str] = None
    parsed_at: Optional[datetime] = None

    # 解析时的辅助信息
    is_reusable: bool = False  # 是否为可复用工作流
    reusable_inputs: Optional[Dict[str, Any]] = None
    reusable_outputs: Optional[Dict[str, Any]] = None
    reusable_secrets: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if isinstance(self.triggers, dict):
            self.triggers = TriggerInfo(**self.triggers)

    @property
    def job_count(self) -> int:
        """获取 Job 数量"""
        return len(self.jobs)

    @property
    def step_count(self) -> int:
        """获取所有 Job 的步骤总数"""
        return sum(len(job.steps) for job in self.jobs.values())

    @property
    def has_jobs(self) -> bool:
        """是否包含 jobs"""
        return len(self.jobs) > 0

    @property
    def workflow_type(self) -> str:
        """推断工作流类型（基于触发器和内容）"""
        trigger_types = self.triggers.trigger_types

        if 'schedule' in trigger_types:
            return 'Scheduled'
        if 'workflow_run' in trigger_types:
            return 'Triggered'
        if 'workflow_dispatch' in trigger_types and 'push' not in trigger_types:
            return 'Manual'
        if 'pull_request' in trigger_types or 'push' in trigger_types:
            return 'CI'

        # 基于内容推断
        for job in self.jobs.values():
            job_lower = job.name.lower()
            if 'release' in job_lower or 'deploy' in job_lower:
                return 'Release'
            if 'test' in job_lower:
                return 'Test'
            if 'build' in job_lower or 'compile' in job_lower:
                return 'Build'
            if 'lint' in job_lower or 'check' in job_lower:
                return 'Lint'

        return 'Other'

    def get_job(self, job_name: str) -> Optional[JobInfo]:
        """获取指定 Job"""
        return self.jobs.get(job_name)

    def get_jobs_by_needs(self, job_name: str) -> List[JobInfo]:
        """获取依赖指定 Job 的所有 Job"""
        return [job for job in self.jobs.values() if job_name in job.needs]

    def get_setup_actions(self) -> List[tuple]:
        """获取所有 setup action 及其版本"""
        results = []
        for job in self.jobs.values():
            for step in job.steps:
                if step.uses and '/setup-' in step.uses:
                    results.append((step.uses_action, step.uses_version, step))
        return results

    def get_cache_steps(self) -> List[tuple]:
        """获取所有缓存相关步骤"""
        results = []
        cache_keywords = ['cache', 'actions/cache', 'actions/restore', 'actions/save']
        for job in self.jobs.values():
            for step in job.steps:
                if step.uses:
                    uses_lower = step.uses.lower()
                    if any(kw in uses_lower for kw in cache_keywords):
                        results.append((step, job))
                # 检查 with 中的 cache 参数
                if step.with_ and any('cache' in str(k).lower() for k in step.with_.keys()):
                    results.append((step, job))
        return results

    def get_dependency_install_commands(self) -> List[tuple]:
        """获取所有依赖安装命令"""
        results = []
        patterns = [
            r'\bnpm\s+(ci|install|i)\b',
            r'\byarn\s+(install|ci)\b',
            r'\bpnpm\s+(install|ci)\b',
            r'\bpip\s+install\b',
            r'\bpoetry\s+install\b',
            r'\bgo\s+mod\b',
            r'\bcargo\s+build\b',
            r'\bcargo\s+install\b',
        ]
        import re
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

        for job in self.jobs.values():
            for step in job.steps:
                if step.run:
                    for pattern in compiled:
                        if pattern.search(step.run):
                            results.append((step, job, pattern.pattern))
                            break
        return results

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于序列化）"""
        return {
            'name': self.name,
            'filename': self.filename,
            'jobs': {
                name: {
                    'name': job.name,
                    'display_name': job.display_name,
                    'runs_on': job.runs_on,
                    'needs': job.needs,
                    'timeout_minutes': job.timeout_minutes,
                    'step_count': len(job.steps),
                }
                for name, job in self.jobs.items()
            },
            'triggers': {
                'types': list(self.triggers.trigger_types),
            },
            'workflow_type': self.workflow_type,
            'job_count': self.job_count,
            'step_count': self.step_count,
            'repository_name': self.repository_name,
            'workflow_path': self.workflow_path,
        }
