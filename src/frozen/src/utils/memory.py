# src/utils/memory.py
import psutil
from typing import Dict

class MemoryMonitor:
    def __init__(self, limit_mb: int = 4096):
        self.limit_mb = limit_mb
        self.process = psutil.Process()
    
    def get_usage_mb(self) -> float:
        return self.process.memory_info().rss / (1024 * 1024)
    
    def get_system_info(self) -> Dict[str, float]:
        mem = psutil.virtual_memory()
        return {
            'process_mb': round(self.get_usage_mb(), 2),
            'total_gb': round(mem.total / (1024**3), 2),
            'available_gb': round(mem.available / (1024**3), 2),
            'used_percent': mem.percent,
        }
    
    def is_over_limit(self) -> bool:
        return self.get_usage_mb() > self.limit_mb
    
    def check_and_warn(self) -> bool:
        usage = self.get_usage_mb()
        if usage > self.limit_mb * 0.9:
            print(f"⚠️ 内存使用: {usage:.0f}MB / {self.limit_mb}MB")
            return True
        return False