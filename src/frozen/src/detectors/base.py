"""
工作流异味检测器基类

定义统一的检测器接口，便于扩展和管理。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
from enum import Enum
from pathlib import Path

from src.models.workflow import WorkflowModel
from src.parsers.workflow_parser import WorkflowParser


class SeverityLevel(Enum):
    """异味严重程度"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class SmellInstance:
    """单个异味实例"""
    type: str  # 异味类型
    severity: SeverityLevel  # 严重程度
    message: str  # 描述信息
    job_name: Optional[str] = None  # 相关 Job
    step_name: Optional[str] = None  # 相关 Step
    line_number: Optional[int] = None  # 在 YAML 中的行号
    details: Dict[str, Any] = field(default_factory=dict)  # 额外详情
    suggestion: Optional[str] = None  # 修复建议
    code_snippet: Optional[str] = None  # 相关代码片段

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'type': self.type,
            'severity': self.severity.value,
            'message': self.message,
            'job_name': self.job_name,
            'step_name': self.step_name,
            'line_number': self.line_number,
            'details': self.details,
            'suggestion': self.suggestion,
            'code_snippet': self.code_snippet,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SmellInstance':
        """从字典创建"""
        severity = SeverityLevel(data.get('severity', 'info'))
        return cls(
            type=data.get('type', ''),
            severity=severity,
            message=data.get('message', ''),
            job_name=data.get('job_name'),
            step_name=data.get('step_name'),
            line_number=data.get('line_number'),
            details=data.get('details', {}),
            suggestion=data.get('suggestion'),
            code_snippet=data.get('code_snippet'),
        )


@dataclass
class DetectorResult:
    """检测器结果"""
    detector_name: str  # 检测器名称
    has_smell: bool  # 是否检测到异味
    smells: List[SmellInstance]  # 异味列表
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据
    error: Optional[str] = None  # 错误信息

    @property
    def smell_count(self) -> int:
        """异味数量"""
        return len(self.smells)

    @property
    def smells_by_severity(self) -> Dict[str, int]:
        """按严重程度分组的异味数量"""
        counts = {}
        for smell in self.smells:
            sev = smell.severity.value
            counts[sev] = counts.get(sev, 0) + 1
        return counts

    @property
    def smells_by_type(self) -> Dict[str, int]:
        """按类型分组的异味数量"""
        counts = {}
        for smell in self.smells:
            typ = smell.type
            counts[typ] = counts.get(typ, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'detector_name': self.detector_name,
            'has_smell': self.has_smell,
            'smells': [s.to_dict() for s in self.smells],
            'smell_count': self.smell_count,
            'smells_by_severity': self.smells_by_severity,
            'smells_by_type': self.smells_by_type,
            'metadata': self.metadata,
            'error': self.error,
        }


@dataclass
class DetectorConfig:
    """检测器配置"""
    enabled: bool = True  # 是否启用
    severity_threshold: Optional[SeverityLevel] = None  # 最低报告级别
    include_code_snippets: bool = False  # 是否包含代码片段
    max_smells_per_workflow: Optional[int] = None  # 单个工作流最大报告数量
    custom_rules: Dict[str, Any] = field(default_factory=dict)  # 自定义规则


class BaseDetector(ABC):
    """检测器基类

    所有工作流异味检测器应继承此类并实现 detect 方法。
    """

    # 检测器名称（需在子类中定义）
    NAME: str = "base_detector"

    # 检测器描述
    DESCRIPTION: str = "基类检测器"

    # 检测的异味类型
    SMELL_TYPES: Set[str] = set()

    # 默认配置
    DEFAULT_CONFIG: DetectorConfig = DetectorConfig()

    def __init__(self, config: Optional[DetectorConfig] = None):
        """初始化检测器

        Args:
            config: 检测器配置
        """
        self.config = config or self.DEFAULT_CONFIG
        self.parser: Optional[WorkflowParser] = None

    def set_parser(self, parser: WorkflowParser):
        """设置共享的解析器

        Args:
            parser: 工作流解析器
        """
        self.parser = parser

    @abstractmethod
    def detect(self, workflow: WorkflowModel) -> DetectorResult:
        """检测工作流中的异味

        Args:
            workflow: 已解析的工作流模型

        Returns:
            DetectorResult: 检测结果
        """
        raise NotImplementedError("子类必须实现 detect 方法")

    def detect_from_file(
        self,
        file_path: Path,
        **metadata
    ) -> DetectorResult:
        """从文件检测（兼容旧接口）

        Args:
            file_path: YAML 文件路径
            **metadata: 额外元数据

        Returns:
            DetectorResult: 检测结果
        """
        if not self.parser:
            # 创建临时解析器
            self.parser = WorkflowParser()

        workflow = self.parser.parse_file(file_path, **metadata)
        if not workflow:
            return DetectorResult(
                detector_name=self.NAME,
                has_smell=False,
                smells=[],
                error=f"无法解析文件: {file_path}"
            )

        return self.detect(workflow)

    def detect_from_content(self, content: str, **metadata) -> DetectorResult:
        """从内容字符串检测（兼容旧接口）

        Args:
            content: YAML 内容字符串
            **metadata: 额外元数据

        Returns:
            DetectorResult: 检测结果
        """
        if not self.parser:
            # 创建临时解析器
            self.parser = WorkflowParser()

        workflow = self.parser.parse_content(content, **metadata)
        if not workflow:
            return DetectorResult(
                detector_name=self.NAME,
                has_smell=False,
                smells=[],
                error="无法解析内容"
            )

        return self.detect(workflow)

    def filter_smells(self, smells: List[SmellInstance]) -> List[SmellInstance]:
        """根据配置过滤异味

        Args:
            smells: 原始异味列表

        Returns:
            过滤后的异味列表
        """
        filtered = smells

        # 按严重程度过滤
        if self.config.severity_threshold:
            threshold_order = {
                SeverityLevel.CRITICAL: 0,
                SeverityLevel.HIGH: 1,
                SeverityLevel.MEDIUM: 2,
                SeverityLevel.LOW: 3,
                SeverityLevel.INFO: 4,
            }
            threshold_value = threshold_order.get(self.config.severity_threshold, 4)
            filtered = [
                s for s in filtered
                if threshold_order.get(s.severity, 4) <= threshold_value
            ]

        # 按数量限制
        if self.config.max_smells_per_workflow:
            filtered = list(filtered)[:self.config.max_smells_per_workflow]

        return filtered

    def create_smell(
        self,
        smell_type: str,
        severity: SeverityLevel,
        message: str,
        **kwargs
    ) -> SmellInstance:
        """创建异味实例的辅助方法

        Args:
            smell_type: 异味类型
            severity: 严重程度
            message: 描述信息
            **kwargs: 其他 SmellInstance 属性

        Returns:
            SmellInstance: 异味实例
        """
        return SmellInstance(
            type=smell_type,
            severity=severity,
            message=message,
            **kwargs
        )

    def get_code_snippet(
        self,
        workflow: WorkflowModel,
        job_name: Optional[str] = None,
        step_name: Optional[str] = None,
        context_lines: int = 2
    ) -> Optional[str]:
        """获取代码片段（如果配置启用）

        Args:
            workflow: 工作流模型
            job_name: Job 名称
            step_name: Step 名称
            context_lines: 上下文行数

        Returns:
            代码片段字符串或 None
        """
        if not self.config.include_code_snippets:
            return None

        # 从原始 YAML 内容中提取相关片段
        # 这里可以实现更复杂的逻辑来精确定位代码位置
        if workflow.raw_content:
            lines = workflow.raw_content.split('\n')
            # 简化实现：返回整个内容
            # 实际应该根据 job_name/step_name 定位到具体行
            return workflow.raw_content[:500] + '...' if len(workflow.raw_content) > 500 else workflow.raw_content

        return None


class FileBasedDetector(BaseDetector):
    """基于文件的检测器基类

    用于需要访问原始文件的检测器。
    """

    def __init__(
        self,
        config: Optional[DetectorConfig] = None,
        workflows_dir: Optional[Path] = None
    ):
        """初始化

        Args:
            config: 检测器配置
            workflows_dir: 工作流文件目录
        """
        super().__init__(config)
        self.workflows_dir = workflows_dir

        if workflows_dir:
            self.parser = WorkflowParser(workflows_dir)


class ReusableWorkflowDetector(FileBasedDetector):
    """支持可复用工作流的检测器基类

    用于需要解析可复用工作流引用的检测器。
    """

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
        super().__init__(config, workflows_dir)
        self.reusable_map = reusable_map or {}
        self._analyzed_reusables: Set[str] = set()

    def analyze_reusable_workflow(
        self,
        workflow: WorkflowModel,
        uses_path: str
    ) -> List[SmellInstance]:
        """分析可复用工作流中的异味

        Args:
            workflow: 父工作流模型
            uses_path: 可复用工作流路径

        Returns:
            异味列表
        """
        if not self.parser or not workflow.workflow_path:
            return []

        # 避免重复分析
        cache_key = f"{workflow.workflow_path}:{uses_path}"
        if cache_key in self._analyzed_reusables:
            return []
        self._analyzed_reusables.add(cache_key)

        # 解析可复用工作流
        reusable_model = self.parser.parse_reusable_workflow(uses_path, workflow)
        if not reusable_model:
            return []

        # 递归检测
        return self.detect(reusable_model).smells


def create_detector_result(
    detector_name: str,
    smells: List[SmellInstance],
    metadata: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None
) -> DetectorResult:
    """创建检测器结果的辅助函数

    Args:
        detector_name: 检测器名称
        smells: 异味列表
        metadata: 元数据
        error: 错误信息

    Returns:
        DetectorResult: 检测结果
    """
    return DetectorResult(
        detector_name=detector_name,
        has_smell=len(smells) > 0,
        smells=smells,
        metadata=metadata or {},
        error=error,
    )
