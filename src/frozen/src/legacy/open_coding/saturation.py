import json
from pathlib import Path
from typing import Dict, List, Tuple


class SaturationChecker:
    """饱和检测：判断开放编码是否达到理论饱和"""

    def __init__(self, threshold: int = 2, min_batches: int = 3):
        """
        Args:
            threshold: 连续几批新标签数 <= 此值视为饱和
            min_batches: 至少编码几批才开始检测饱和
        """
        self.threshold = threshold
        self.min_batches = min_batches

    def check(self, new_labels_per_batch: Dict[int, int]) -> dict:
        """
        检查是否饱和

        Args:
            new_labels_per_batch: {batch_num: new_label_count}

        Returns:
            {
                'is_saturated': bool,
                'consecutive_low': int,  # 连续低新标签的批数
                'recommendation': str,
                'new_labels_per_batch': dict,
            }
        """
        if not new_labels_per_batch:
            return {
                'is_saturated': False,
                'consecutive_low': 0,
                'recommendation': '开始编码',
                'new_labels_per_batch': {},
            }

        sorted_batches = sorted(new_labels_per_batch.items())
        total_batches = len(sorted_batches)

        if total_batches < self.min_batches:
            return {
                'is_saturated': False,
                'consecutive_low': 0,
                'recommendation': f'继续采样（至少需要 {self.min_batches} 批）',
                'new_labels_per_batch': new_labels_per_batch,
            }

        # 计算连续低新标签批数
        consecutive_low = 0
        for batch_num, count in reversed(sorted_batches):
            if count <= self.threshold:
                consecutive_low += 1
            else:
                break

        is_saturated = consecutive_low >= 2  # 连续2批低新标签

        if is_saturated:
            recommendation = '已饱和，可以进入主轴编码阶段'
        elif consecutive_low == 1:
            recommendation = '接近饱和，建议再采一批验证'
        else:
            # 找出哪些桶可能还有新发现
            recommendation = '继续采样'

        return {
            'is_saturated': is_saturated,
            'consecutive_low': consecutive_low,
            'recommendation': recommendation,
            'new_labels_per_batch': new_labels_per_batch,
        }

    def suggest_theoretical_sampling(self, label_pool_data: dict,
                                      index_buckets: dict) -> List[str]:
        """
        建议理论采样的方向

        Args:
            label_pool_data: 标签池数据
            index_buckets: 索引桶数据

        Returns:
            建议采样的桶列表
        """
        suggestions = []

        # 找出标签池中样本数少的标签
        low_sample_labels = [
            label for label, info in label_pool_data.items()
            if info.get('count', 0) <= 2
        ]

        if low_sample_labels:
            suggestions.append(
                f"以下标签样本不足，建议多采: {', '.join(low_sample_labels[:10])}"
            )

        # 找出索引中未被采样的桶
        unsampled = [
            key for key, info in index_buckets.items()
            if info.get('sampled_count', 0) == 0
        ]

        if unsampled:
            suggestions.append(
                f"以下桶尚未采样: {', '.join(unsampled[:10])}"
            )

        # 兜底桶总是建议多采
        other_buckets = [
            key for key in index_buckets
            if key.startswith('OTHER|')
        ]
        if other_buckets:
            suggestions.append(
                f"兜底桶建议增加采样: {', '.join(other_buckets[:5])}"
            )

        return suggestions

    def save_report(self, report: dict, output_file: Path):
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
