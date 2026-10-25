"""
GitHub Actions 工作流 YAML 解析器

将 YAML 文件解析为 WorkflowModel，支持批量解析和缓存。
"""

import yaml
from pathlib import Path
from typing import Dict, List, Optional, Union, Any
from datetime import datetime

from src.models.workflow import (
    WorkflowModel,
    JobInfo,
    StepInfo,
    TriggerInfo,
)


class WorkflowParser:
    """工作流 YAML 解析器

    将 YAML 文件解析为统一的 WorkflowModel，避免在各个检测器中重复解析。
    支持缓存已解析的结果，提高性能。
    """

    def __init__(self, workflows_dir: Optional[Path] = None):
        """初始化解析器

        Args:
            workflows_dir: 工作流文件根目录（用于解析可复用工作流引用）
        """
        self.workflows_dir = workflows_dir
        self._cache: Dict[str, WorkflowModel] = {}
        self._reusable_cache: Dict[str, Any] = {}  # 可复用工作流缓存

    def parse_file(
        self,
        file_path: Union[str, Path],
        **metadata
    ) -> Optional[WorkflowModel]:
        """解析 YAML 文件

        Args:
            file_path: YAML 文件路径
            **metadata: 额外的元数据（repository_name, workflow_path, workflow_id, file_sha 等）

        Returns:
            WorkflowModel 或 None（解析失败时）
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return None

        # 检查缓存
        cache_key = str(file_path)
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except (IOError, UnicodeDecodeError):
            return None

        model = self.parse_content(content, file_path=file_path, **metadata)
        if model:
            self._cache[cache_key] = model

        return model

    def parse_content(
        self,
        content: str,
        **metadata
    ) -> Optional[WorkflowModel]:
        """解析 YAML 内容字符串

        Args:
            content: YAML 内容字符串
            **metadata: 额外的元数据

        Returns:
            WorkflowModel 或 None（解析失败时）
        """
        try:
            yaml_data = yaml.safe_load(content)
        except yaml.YAMLError:
            return None

        if not yaml_data or not isinstance(yaml_data, dict):
            return None

        return self._parse_yaml_dict(yaml_data, raw_content=content, **metadata)

    def _parse_yaml_dict(
        self,
        yaml_data: Dict[str, Any],
        raw_content: str = "",
        **metadata
    ) -> Optional[WorkflowModel]:
        """从已解析的 YAML 字典构建 WorkflowModel"""
        model = WorkflowModel(
            name=yaml_data.get('name', ''),
            raw_content=raw_content,
            yaml_data=yaml_data,
            parsed_at=datetime.now(),
            **metadata
        )

        # 解析触发器
        model.triggers = self._parse_triggers(yaml_data)

        # 检查是否为可复用工作流
        if 'on' in yaml_data and isinstance(yaml_data['on'], dict):
            if 'workflow_call' in yaml_data['on']:
                model.is_reusable = True
                call_config = yaml_data['on']['workflow_call']
                if isinstance(call_config, dict):
                    model.reusable_inputs = call_config.get('inputs')
                    model.reusable_outputs = call_config.get('outputs')
                    model.reusable_secrets = call_config.get('secrets')

        # 解析环境变量和权限
        model.env = yaml_data.get('env', {})
        model.permissions = yaml_data.get('permissions')
        model.concurrency = yaml_data.get('concurrency')
        model.defaults = yaml_data.get('defaults')
        model.run_name = yaml_data.get('run-name')
        model.run_number = yaml_data.get('run-number')

        # 解析 Jobs
        jobs_data = yaml_data.get('jobs', {})
        if isinstance(jobs_data, dict):
            model.jobs = self._parse_jobs(jobs_data, model)

        return model

    def _parse_triggers(self, yaml_data: Dict[str, Any]) -> TriggerInfo:
        """解析触发器配置"""
        on_config = yaml_data.get('on')

        if not on_config:
            return TriggerInfo()

        if isinstance(on_config, str):
            # 简单字符串形式的触发器
            return TriggerInfo(other={on_config: {}})

        if isinstance(on_config, list):
            # 列表形式的触发器
            other = {}
            for item in on_config:
                if isinstance(item, str):
                    other[item] = {}
            return TriggerInfo(other=other)

        if not isinstance(on_config, dict):
            return TriggerInfo()

        triggers = TriggerInfo()

        # 解析各种触发器类型
        for key, value in on_config.items():
            key_lower = key.lower().replace('-', '_')

            if key_lower == 'push':
                triggers.push = value if isinstance(value, dict) else {}
            elif key_lower in ('pull_request', 'pull_request_target'):
                triggers.pull_request = value if isinstance(value, dict) else {}
            elif key_lower == 'workflow_dispatch':
                triggers.workflow_dispatch = True
            elif key_lower == 'workflow_run':
                triggers.workflow_run = value if isinstance(value, dict) else {}
            elif key_lower == 'schedule':
                triggers.schedule = value if isinstance(value, list) else []
            elif key_lower == 'repository_dispatch':
                triggers.repository_dispatch = True
            elif key_lower == 'manual':
                triggers.manual = True
            else:
                triggers.other[key] = value if isinstance(value, dict) else {}

        return triggers

    def _parse_jobs(self, jobs_data: Dict[str, Any], model: WorkflowModel) -> Dict[str, JobInfo]:
        """解析 Jobs"""
        jobs = {}

        for job_name, job_config in jobs_data.items():
            if not isinstance(job_config, dict):
                # 处理字符串引用或其他类型
                jobs[job_name] = JobInfo(name=job_name)
                continue

            # 处理可复用工作流引用
            if 'uses' in job_config:
                uses_ref = job_config.get('uses', '')
                if uses_ref.startswith('./.github/workflows/'):
                    job_info = JobInfo(
                        name=job_name,
                        display_name=job_config.get('name', job_name),
                        uses=uses_ref,
                        runs_on=None,  # 可复用工作流不直接定义 runs-on
                    )
                    # 复制其他属性
                    self._copy_common_job_properties(job_config, job_info)
                    jobs[job_name] = job_info
                    continue

            # 解析 steps
            steps = self._parse_steps(job_config.get('steps', []))

            # 处理 needs 依赖
            needs = job_config.get('needs', [])
            if isinstance(needs, str):
                needs = [needs]

            # 提取 strategy 和 matrix
            strategy = job_config.get('strategy')
            matrix = None
            if isinstance(strategy, dict):
                matrix = strategy.get('matrix')

            job_info = JobInfo(
                name=job_name,
                display_name=job_config.get('name', job_name),
                runs_on=job_config.get('runs-on'),
                needs=needs,
                if_=job_config.get('if'),
                env=job_config.get('env', {}),
                timeout_minutes=job_config.get('timeout-minutes'),
                steps=steps,
                strategy=strategy,
                matrix=matrix,
                container=job_config.get('container'),
                services=job_config.get('services'),
                permissions=job_config.get('permissions'),
                concurrency=job_config.get('concurrency'),
                continue_on_error=job_config.get('continue-on-error', False),
            )

            jobs[job_name] = job_info

        return jobs

    def _parse_steps(self, steps_data: List[Any]) -> List[StepInfo]:
        """解析步骤列表"""
        steps = []

        for step_data in steps_data:
            if not isinstance(step_data, dict):
                continue

            # 处理 run 命令
            run = step_data.get('run')
            if isinstance(run, str):
                # 多行命令处理
                run = run.strip()

            step_info = StepInfo(
                name=step_data.get('name', ''),
                id=step_data.get('id'),
                uses=step_data.get('uses'),
                run=run,
                if_=step_data.get('if'),
                with_=step_data.get('with', {}),
                env=step_data.get('env', {}),
                timeout_minutes=step_data.get('timeout-minutes'),
                continue_on_error=step_data.get('continue-on-error', False),
            )

            steps.append(step_info)

        return steps

    def _copy_common_job_properties(self, source: Dict[str, Any], target: JobInfo):
        """复制 Job 的通用属性"""
        if 'needs' in source:
            needs = source['needs']
            target.needs = [needs] if isinstance(needs, str) else needs
        if 'if' in source:
            target.if_ = source['if']
        if 'permissions' in source:
            target.permissions = source['permissions']
        if 'concurrency' in source:
            target.concurrency = source['concurrency']

    def parse_reusable_workflow(
        self,
        uses_path: str,
        parent_model: WorkflowModel
    ) -> Optional[WorkflowModel]:
        """解析可复用工作流

        Args:
            uses_path: 可复用工作流路径（如 ./.github/workflows/reusable.yml）
            parent_model: 父工作流模型（用于解析相对路径）

        Returns:
            解析后的 WorkflowModel 或 None
        """
        if not self.workflows_dir or not parent_model.workflow_path:
            return None

        # 构建可复用工作流的文件路径
        # 从 workflows/{owner}/{repo}/{filename} 解析出 owner/repo
        path_parts = parent_model.workflow_path.split('/')
        if len(path_parts) < 3:
            return None

        # 假设路径格式为: workflows/{owner}/{repo}/{filename}
        owner_repo = '/'.join(path_parts[:2])  # {owner}/{repo}
        reusable_filename = Path(uses_path).name  # 从 ./.github/workflows/xxx.yml 提取文件名
        reusable_path = f"{owner_repo}/{reusable_filename}"

        # 检查缓存
        if reusable_path in self._reusable_cache:
            cached = self._reusable_cache[reusable_path]
            if cached is None:
                return None
            # 从缓存恢复模型
            return self._parse_yaml_dict(cached, workflow_path=reusable_path)

        # 读取文件
        reusable_file = self.workflows_dir / reusable_path
        if not reusable_file.exists():
            self._reusable_cache[reusable_path] = None
            return None

        try:
            with open(reusable_file, 'r', encoding='utf-8') as f:
                content = f.read()
            yaml_data = yaml.safe_load(content)
            if not yaml_data:
                self._reusable_cache[reusable_path] = None
                return None

            # 缓存原始 YAML 数据
            self._reusable_cache[reusable_path] = yaml_data

            return self._parse_yaml_dict(yaml_data, raw_content=content, workflow_path=reusable_path)
        except (IOError, yaml.YAMLError):
            self._reusable_cache[reusable_path] = None
            return None

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self._reusable_cache.clear()

    def get_cache_size(self) -> int:
        """获取缓存大小"""
        return len(self._cache)


class WorkflowParserCache:
    """工作流解析器持久化缓存

    将解析结果持久化到磁盘，支持增量扫描。
    """

    def __init__(self, cache_dir: Optional[Path] = None):
        """初始化缓存

        Args:
            cache_dir: 缓存目录
        """
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """获取缓存"""
        # 先查内存缓存
        if key in self._memory_cache:
            return self._memory_cache[key]

        if not self.cache_dir:
            return None

        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None

        try:
            import json
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._memory_cache[key] = data
            return data
        except (IOError, json.JSONDecodeError):
            return None

    def set(self, key: str, data: Dict[str, Any]):
        """设置缓存"""
        self._memory_cache[key] = data

        if not self.cache_dir:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"{key}.json"

        try:
            import json
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except (IOError, TypeError):
            pass

    def clear(self):
        """清空缓存"""
        self._memory_cache.clear()

    def clear_persistent(self):
        """清空持久化缓存"""
        self.clear()
        if self.cache_dir and self.cache_dir.exists():
            import shutil
            shutil.rmtree(self.cache_dir, ignore_errors=True)
