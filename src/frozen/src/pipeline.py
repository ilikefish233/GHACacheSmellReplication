# src/pipeline.py
from pathlib import Path
from typing import Optional, Callable, Any, Dict
import json
from tqdm import tqdm

from .loaders.log_loader import LogLoader
from .loaders.json_loader import JSONLoader
from .processors.base import BaseProcessor
from .utils.memory import MemoryMonitor
from .utils.checkpoint import CheckpointManager
from .utils.helpers import save_json, ensure_dir

class Pipeline:
    def __init__(
        self,
        output_dir: Path,
        memory_limit_mb: int = 4096,
        checkpoint_interval: int = 1000,
    ):
        self.output_dir = ensure_dir(Path(output_dir))
        self.memory_monitor = MemoryMonitor(memory_limit_mb)
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint = CheckpointManager(self.output_dir / "checkpoints")
        self.results = []
        self.processed_count = 0
    
    def process_logs(
        self,
        archive_path: Path,
        processor: BaseProcessor,
        batch_size: int = 100,
        progress_callback: Optional[Callable[[int], None]] = None,
    ):
        loader = LogLoader(archive_path)
        total = loader.count_tar_files()
        
        print(f"开始处理日志，共 {total} 个压缩包")
        
        for entry in tqdm(loader.iter_log_entries(), desc="处理日志", total=total):
            entry_id = f"{entry.archive_path}:{entry.file_name}"
            
            if self.checkpoint.is_processed(entry_id):
                continue
            
            try:
                result = processor.process(entry)
                if result:
                    self.results.append(result)
                
                self.checkpoint.mark_processed(entry_id)
                self.processed_count += 1
                
                if self.processed_count % batch_size == 0:
                    self._flush_results()
                
                if self.processed_count % self.checkpoint_interval == 0:
                    self.checkpoint.save()
                    self.memory_monitor.check_and_warn()
                
                if progress_callback:
                    progress_callback(self.processed_count)
                    
            except Exception as e:
                print(f"\n处理失败 {entry_id}: {e}")
                continue
        
        self._flush_results()
        self.checkpoint.save()
        
        stats = processor.finalize()
        save_json(stats, self.output_dir / "statistics.json")
        
        print(f"\n处理完成! 共处理 {self.processed_count} 个文件")
        return stats
    
    def process_json(
        self,
        file_path: Path,
        processor: BaseProcessor,
        batch_size: int = 1000,
    ):
        loader = JSONLoader(file_path)
        
        print(f"开始处理 JSON: {file_path}")
        
        for batch in tqdm(loader.iter_batches(batch_size), desc="处理记录"):
            results = processor.process_batch(batch)
            self.results.extend(results)
            self.processed_count += len(batch)
            
            if self.processed_count % (batch_size * 10) == 0:
                self._flush_results()
                self.memory_monitor.check_and_warn()
        
        self._flush_results()
        
        stats = processor.finalize()
        save_json(stats, self.output_dir / "statistics.json")
        
        print(f"\n处理完成! 共处理 {self.processed_count} 条记录")
        return stats
    
    def _flush_results(self):
        if not self.results:
            return
        
        batch_file = self.output_dir / "results" / f"batch_{self.processed_count}.json"
        save_json(self.results, batch_file)
        self.results = []