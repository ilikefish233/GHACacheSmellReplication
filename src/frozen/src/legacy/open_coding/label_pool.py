import json
from pathlib import Path
from typing import Dict, List, Optional


class LabelPool:
    """开放编码标签池管理"""

    def __init__(self, pool_file: Optional[Path] = None):
        self.pool_file = pool_file
        self.labels: Dict[str, dict] = {}
        if pool_file and pool_file.exists():
            self.load()

    def load(self):
        with open(self.pool_file, 'r', encoding='utf-8') as f:
            self.labels = json.load(f)

    def save(self):
        if not self.pool_file:
            return
        self.pool_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.pool_file.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.labels, f, indent=2, ensure_ascii=False)
        tmp.replace(self.pool_file)

    def add_label(self, label: str, batch_num: int, description: str,
                  yml_hash: str, severity: str = None):
        """添加一个标签实例"""
        if label in self.labels:
            entry = self.labels[label]
            entry['count'] += 1
            if description and description not in entry['descriptions']:
                entry['descriptions'].append(description)
            if yml_hash not in entry['sample_hashes']:
                entry['sample_hashes'].append(yml_hash)
            if severity and severity not in entry.get('severities', []):
                entry.setdefault('severities', []).append(severity)
        else:
            self.labels[label] = {
                'count': 1,
                'first_seen_batch': batch_num,
                'descriptions': [description] if description else [],
                'sample_hashes': [yml_hash],
                'severities': [severity] if severity else [],
            }

    def add_labels_from_batch(self, batch_results: List[dict], batch_num: int):
        """从一批LLM编码结果中提取标签并合并"""
        new_labels = set()
        for result in batch_results:
            yml_hash = result.get('yml_hash', '')
            for issue in result.get('issues', []):
                label = issue.get('label', '').strip()
                if not label or label == 'no_issue':
                    continue
                desc = issue.get('description', '')
                severity = issue.get('severity')
                if label not in self.labels:
                    new_labels.add(label)
                self.add_label(label, batch_num, desc, yml_hash, severity)
        return new_labels

    def get_new_labels_in_batch(self, batch_num: int) -> List[str]:
        """获取某批次首次出现的标签"""
        return [
            label for label, info in self.labels.items()
            if info['first_seen_batch'] == batch_num
        ]

    def get_label_count(self) -> int:
        return len(self.labels)

    def get_total_instances(self) -> int:
        return sum(info['count'] for info in self.labels.values())

    def get_sorted_labels(self, by='count') -> List[tuple]:
        if by == 'count':
            return sorted(self.labels.items(), key=lambda x: x[1]['count'], reverse=True)
        elif by == 'first_seen':
            return sorted(self.labels.items(), key=lambda x: x[1]['first_seen_batch'])
        return list(self.labels.items())

    def export_for_axial_coding(self) -> str:
        """导出标签列表供主轴编码使用"""
        lines = []
        for label, info in sorted(self.labels.items(), key=lambda x: x[1]['count'], reverse=True):
            descs = info['descriptions'][:3]
            lines.append(f"  {label}: {info['count']}次 (首次: batch {info['first_seen_batch']})")
            for d in descs:
                lines.append(f"    - {d}")
        return '\n'.join(lines)

    def summary(self) -> dict:
        return {
            'total_labels': self.get_label_count(),
            'total_instances': self.get_total_instances(),
            'labels': {
                label: {
                    'count': info['count'],
                    'first_seen_batch': info['first_seen_batch'],
                    'sample_count': len(info['sample_hashes']),
                }
                for label, info in self.get_sorted_labels()
            }
        }
