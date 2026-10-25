# config/settings.py
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[1]

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import json

@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'archive/legacy_log_analysis_data/gha_log_dataset')))
    output_dir: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'output')))
    processed_data_dir: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'data')))
    
    batch_size: int = 100
    max_workers: int = 4
    memory_limit_mb: int = 4096
    checkpoint_interval: int = 1000
    
    # Workflow Analysis 配置
    workflow_analysis_input: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'data/filtered_runs_sample.json')))
    workflow_analysis_output: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'output/workflow_feature_analysis.json')))
    workflow_analysis_sample_size: Optional[int] = field(default_factory=lambda: None)
    workflow_analysis_checkpoint_interval: int = field(default_factory=lambda: 50)
    workflow_analysis_long_run: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'output/long_running_workflows.json')))
    workflow_analysis_long_run_stats: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'output/long_running_stats.json')))

    # Workflow Fetcher 配置
    workflow_fetch_input: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'data/filtered_runs.json')))
    workflow_fetch_output_dir: Path = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'data')))
    workflow_fetch_limit: Optional[int] = field(default_factory=lambda: None)
    workflow_fetch_offset: int = field(default_factory=lambda: 10000)
    
    # Workflow Coverage 分析配置
    workflow_coverage_mapping_file: Optional[Path] = field(default_factory=lambda: Path(str(_PORTABLE_PROJECT_ROOT / 'data/run_workflow_mapping.json')))
    
    @property
    def runs_file(self) -> Path:
        return self.data_dir / "runs.json"
    
    @property
    def repos_file(self) -> Path:
        return self.data_dir / "repositories.json"
    
    @property
    def logs_archive(self) -> Path:
        return self.data_dir / "github_run_logs.zip"
    
    @classmethod
    def from_file(cls, config_path: Path) -> 'Settings':
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls(
            data_dir=Path(data.get('data_dir', str(_PORTABLE_PROJECT_ROOT / 'archive/legacy_log_analysis_data/gha_log_dataset'))),
            output_dir=Path(data.get('output_dir', str(_PORTABLE_PROJECT_ROOT / 'output'))),
            processed_data_dir=Path(data.get('processed_data_dir', str(_PORTABLE_PROJECT_ROOT / 'data'))),
            batch_size=data.get('batch_size', 100),
            max_workers=data.get('max_workers', 4),
            memory_limit_mb=data.get('memory_limit_mb', 4096),
        )
    
    def to_file(self, config_path: Path):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump({
                'data_dir': str(self.data_dir),
                'output_dir': str(self.output_dir),
                'processed_data_dir': str(self.processed_data_dir),
                'batch_size': self.batch_size,
                'max_workers': self.max_workers,
                'memory_limit_mb': self.memory_limit_mb,
            }, f, indent=2)

settings = Settings()