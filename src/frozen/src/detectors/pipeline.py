"""
检测器管道

批量运行检测器，共享解析数据，避免重复解析。
支持增量扫描、并行处理和结果聚合。
"""

from typing import Dict, List, Optional, Any, Callable, Type
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.models.workflow import WorkflowModel
from src.parsers.workflow_parser import WorkflowParser
from src.detectors.base import (
    BaseDetector,
    DetectorResult,
    DetectorConfig,
    SeverityLevel,
)


@dataclass
class PipelineConfig:
    """管道配置"""
    # 启用的检测器列表
    enabled_detectors: List[str] = field(default_factory=list)
    # 是否并行处理
    parallel: bool = False
    # 最大并行线程数
    max_workers: int = 4
    # 是否启用缓存
    enable_cache: bool = True
    # 缓存目录
    cache_dir: Optional[Path] = None
    # 工作流目录
    workflows_dir: Optional[Path] = None
    # 可复用工作流映射
    reusable_map: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """管道执行结果"""
    total_workflows: int = 0
    scanned_workflows: int = 0
    skipped_workflows: int = 0
    total_smells: int = 0
    smells_by_severity: Dict[str, int] = field(default_factory=dict)
    smells_by_detector: Dict[str, int] = field(default_factory=dict)
    smells_by_type: Dict[str, int] = field(default_factory=dict)
    scan_time_seconds: float = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'total_workflows': self.total_workflows,
            'scanned_workflows': self.scanned_workflows,
            'skipped_workflows': self.skipped_workflows,
            'total_smells': self.total_smells,
            'smells_by_severity': self.smells_by_severity,
            'smells_by_detector': self.smells_by_detector,
            'smells_by_type': self.smells_by_type,
            'scan_time_seconds': self.scan_time_seconds,
            'errors': self.errors,
        }


class WorkflowItem:
    """待扫描的工作流项"""

    def __init__(
        self,
        key: str,
        yml_path: Path,
        yml_rel_path: Optional[str] = None,
        **metadata
    ):
        """初始化

        Args:
            key: 唯一标识（用于缓存键和去重）
            yml_path: YAML 文件路径
            yml_rel_path: 相对路径（用于可复用工作流解析）
            **metadata: 额外元数据
        """
        self.key = key
        self.yml_path = Path(yml_path)
        self.yml_rel_path = yml_rel_path
        self.metadata = metadata

    @property
    def file_path(self) -> Path:
        """获取文件路径"""
        return self.yml_path

    def exists(self) -> bool:
        """检查文件是否存在"""
        return self.yml_path.exists()


class DetectorPipeline:
    """检测器管道

    管理多个检测器的批量执行，支持：
    - 共享 WorkflowParser，避免重复解析
    - 并行处理提高性能
    - 增量扫描，跳过已处理的文件
    - 结果聚合和统计
    """

    def __init__(
        self,
        detectors: Dict[str, BaseDetector],
        config: Optional[PipelineConfig] = None
    ):
        """初始化管道

        Args:
            detectors: 检测器字典 {name: detector}
            config: 管道配置
        """
        self.detectors = detectors
        self.config = config or PipelineConfig()
        self.parser: Optional[WorkflowParser] = None

        # 创建共享解析器
        if self.config.workflows_dir:
            self.parser = WorkflowParser(
                workflows_dir=self.config.workflows_dir
            )
            # 设置到各个检测器
            for detector in self.detectors.values():
                if isinstance(detector, FileBasedDetector):
                    detector.set_parser(self.parser)

        # 过滤启用的检测器
        if self.config.enabled_detectors:
            self.active_detectors = {
                name: det
                for name, det in self.detectors.items()
                if name in self.config.enabled_detectors
            }
        else:
            self.active_detectors = self.detectors

    def scan_workflow(
        self,
        item: WorkflowItem,
        force: bool = False
    ) -> Dict[str, Any]:
        """扫描单个工作流

        Args:
            item: 工作流项
            force: 是否强制重新扫描

        Returns:
            扫描结果字典
        """
        # 检查文件是否存在
        if not item.exists():
            return {
                'key': item.key,
                'error': f'文件不存在: {item.yml_path}',
                'detectors': {},
            }

        # 解析工作流
        workflow = self.parser.parse_file(
            item.yml_path,
            workflow_path=item.yml_rel_path,
            **item.metadata
        )

        if not workflow:
            return {
                'key': item.key,
                'error': f'无法解析文件: {item.yml_path}',
                'detectors': {},
            }

        # 运行所有检测器
        detector_results = {}
        all_smells = []
        smells_by_severity = {}
        smells_by_type = {}

        for detector_name, detector in self.active_detectors.items():
            try:
                result = detector.detect(workflow)
                detector_results[detector_name] = result.to_dict()

                # 收集统计信息
                for smell in result.smells:
                    all_smells.append(smell.to_dict())
                    sev = smell.severity.value
                    smells_by_severity[sev] = smells_by_severity.get(sev, 0) + 1
                    typ = smell.type
                    smells_by_type[typ] = smells_by_type.get(typ, 0) + 1

            except Exception as e:
                detector_results[detector_name] = {
                    'detector_name': detector_name,
                    'has_smell': False,
                    'smells': [],
                    'error': str(e),
                }

        return {
            'key': item.key,
            'detectors': detector_results,
            'summary': {
                'total_smells': len(all_smells),
                'smells_by_severity': smells_by_severity,
                'smells_by_type': smells_by_type,
            },
            'metadata': {
                'repository_name': item.metadata.get('repository_name'),
                'workflow_path': item.yml_rel_path,
                'workflow_id': item.metadata.get('workflow_id'),
                'file_sha': item.metadata.get('file_sha'),
            },
        }

    def scan_batch(
        self,
        items: List[WorkflowItem],
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> PipelineResult:
        """批量扫描工作流

        Args:
            items: 工作流项列表
            force: 是否强制重新扫描
            progress_callback: 进度回调 (current, total)

        Returns:
            PipelineResult: 管道执行结果
        """
        import time
        start_time = time.time()

        result = PipelineResult(
            total_workflows=len(items),
        )

        if self.config.parallel and len(items) > 1:
            # 并行处理
            scanned = self._scan_parallel(items, force, result, progress_callback)
        else:
            # 顺序处理
            scanned = self._scan_sequential(items, force, result, progress_callback)

        result.scanned_workflows = scanned
        result.skipped_workflows = result.total_workflows - scanned
        result.scan_time_seconds = time.time() - start_time

        return result

    def _scan_sequential(
        self,
        items: List[WorkflowItem],
        force: bool,
        result: PipelineResult,
        progress_callback: Optional[Callable[[int, int], None]]
    ) -> int:
        """顺序扫描"""
        scanned = 0

        for i, item in enumerate(items):
            scan_result = self.scan_workflow(item, force)

            # 聚合结果
            self._aggregate_result(scan_result, result)

            scanned += 1
            if progress_callback:
                progress_callback(scanned, len(items))

        return scanned

    def _scan_parallel(
        self,
        items: List[WorkflowItem],
        force: bool,
        result: PipelineResult,
        progress_callback: Optional[Callable[[int, int], None]]
    ) -> int:
        """并行扫描"""
        scanned = 0

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            # 提交所有任务
            futures = {
                executor.submit(self.scan_workflow, item, force): item
                for item in items
            }

            # 收集结果
            for future in as_completed(futures):
                try:
                    scan_result = future.result()
                    self._aggregate_result(scan_result, result)
                    scanned += 1

                    if progress_callback:
                        progress_callback(scanned, len(items))

                except Exception as e:
                    result.errors.append(str(e))

        return scanned

    def _aggregate_result(self, scan_result: Dict[str, Any], result: PipelineResult):
        """聚合单个扫描结果到总结果"""
        summary = scan_result.get('summary', {})

        result.total_smells += summary.get('total_smells', 0)

        # 按严重程度聚合
        for sev, count in summary.get('smells_by_severity', {}).items():
            result.smells_by_severity[sev] = result.smells_by_severity.get(sev, 0) + count

        # 按类型聚合
        for typ, count in summary.get('smells_by_type', {}).items():
            result.smells_by_type[typ] = result.smells_by_type.get(typ, 0) + count

        # 按检测器聚合
        for det_name, det_result in scan_result.get('detectors', {}).items():
            smell_count = det_result.get('smell_count', 0)
            result.smells_by_detector[det_name] = result.smells_by_detector.get(det_name, 0) + smell_count

        # 收集错误
        if scan_result.get('error'):
            result.errors.append(scan_result['error'])


class IncrementalScanner:
    """增量扫描器

    支持跳过已处理的文件，只扫描新增或修改的文件。
    """

    def __init__(
        self,
        pipeline: DetectorPipeline,
        output_file: Path,
        hash_func: Optional[Callable[[str], str]] = None
    ):
        """初始化

        Args:
            pipeline: 检测器管道
            output_file: 输出文件路径
            hash_func: 哈希函数，用于生成文件标识
        """
        self.pipeline = pipeline
        self.output_file = output_file
        self.hash_func = hash_func or self._default_hash
        self._scanned_hashes: set = set()

    def _default_hash(self, key: str) -> str:
        """默认哈希函数"""
        import hashlib
        return hashlib.md5(key.encode('utf-8')).hexdigest()[:12]

    def load_existing_results(self) -> Dict[str, Dict[str, Any]]:
        """加载已有结果

        Returns:
            {hash: record} 字典
        """
        self._scanned_hashes.clear()

        if not self.output_file.exists():
            return {}

        records = {}
        try:
            with open(self.output_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        # 假设每行格式为 {hash: {...}}
                        if isinstance(record, dict):
                            hash_key = list(record.keys())[0]
                            records[hash_key] = record[hash_key]
                            self._scanned_hashes.add(hash_key)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"加载已有结果失败: {e}")

        return records

    def is_scanned(self, key: str) -> bool:
        """检查是否已扫描"""
        return self.hash_func(key) in self._scanned_hashes

    def append_result(self, item: WorkflowItem, scan_result: Dict[str, Any]):
        """追加扫描结果到文件"""
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

        hash_key = self.hash_func(item.key)
        record = {hash_key: scan_result}

        with open(self.output_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

        self._scanned_hashes.add(hash_key)

    def scan_incremental(
        self,
        items: List[WorkflowItem],
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> PipelineResult:
        """增量扫描

        Args:
            items: 工作流项列表
            force: 是否强制重新扫描
            progress_callback: 进度回调

        Returns:
            PipelineResult: 扫描结果
        """
        if not force:
            self.load_existing_results()

        # 过滤出需要扫描的项
        items_to_scan = [item for item in items if not self.is_scanned(item.key)]

        print(f"待扫描: {len(items_to_scan)}/{len(items)}")

        if not items_to_scan:
            return PipelineResult(total_workflows=len(items))

        # 执行扫描
        result = self.pipeline.scan_batch(items_to_scan, force, progress_callback)

        # 更新总数
        result.total_workflows = len(items)
        result.skipped_workflows = len(items) - result.scanned_workflows

        # 保存结果
        for item in items_to_scan:
            scan_result = self.pipeline.scan_workflow(item, force)
            self.append_result(item, scan_result)

        return result


# 导入 FileBasedDetector（在末尾避免循环导入）
from src.detectors.base import FileBasedDetector
