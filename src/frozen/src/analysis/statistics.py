# src/analysis/statistics.py
from typing import Dict, Any, List
import statistics

class StatisticsCalculator:
    @staticmethod
    def calculate_duration_stats(durations: List[int]) -> Dict[str, Any]:
        if not durations:
            return {}
        
        return {
            'mean': round(statistics.mean(durations), 2),
            'median': round(statistics.median(durations), 2),
            'std_dev': round(statistics.stdev(durations), 2) if len(durations) > 1 else 0,
            'min': min(durations),
            'max': max(durations),
            'percentile_95': round(sorted(durations)[int(len(durations) * 0.95)], 2) if durations else 0,
        }
    
    @staticmethod
    def calculate_success_rate(success: int, total: int) -> float:
        return round(success / max(total, 1) * 100, 2)
    
    @staticmethod
    def calculate_distribution(values: List[Any]) -> Dict[str, int]:
        dist = {}
        for v in values:
            key = str(v) if v is not None else 'unknown'
            dist[key] = dist.get(key, 0) + 1
        return dist