# src/processors/log_processor.py
from typing import Dict, Any
from .base import BaseProcessor
from ..loaders.log_loader import LogEntry

class LogProcessor(BaseProcessor):
    def __init__(self):
        self.stats = {
            'total_files': 0,
            'total_lines': 0,
            'total_size_bytes': 0,
            'error_files': 0,
            'repos': set(),
            'workflows': set(),
        }
    
    def process(self, entry: LogEntry) -> Dict[str, Any]:
        self.stats['total_files'] += 1
        self.stats['total_lines'] += entry.content.count('\n') + 1
        self.stats['total_size_bytes'] += len(entry.content)
        
        if entry.repo_owner and entry.repo_name:
            self.stats['repos'].add(f"{entry.repo_owner}/{entry.repo_name}")
        if entry.workflow_name:
            self.stats['workflows'].add(entry.workflow_name)
        
        has_error = self._detect_error(entry.content)
        if has_error:
            self.stats['error_files'] += 1
        
        return {
            'archive': entry.archive_path,
            'file': entry.file_name,
            'repo': f"{entry.repo_owner}/{entry.repo_name}" if entry.repo_owner else None,
            'workflow': entry.workflow_name,
            'run_id': entry.run_id,
            'job_id': entry.job_id,
            'lines': entry.content.count('\n') + 1,
            'size_bytes': len(entry.content),
            'has_error': has_error,
        }
    
    def _detect_error(self, content: str) -> bool:
        error_keywords = ['error', 'failed', 'exception', 'fatal', 'timeout']
        content_lower = content.lower()
        return any(kw in content_lower for kw in error_keywords)
    
    def finalize(self) -> Dict[str, Any]:
        return {
            'total_files': self.stats['total_files'],
            'total_lines': self.stats['total_lines'],
            'total_size_gb': round(self.stats['total_size_bytes'] / (1024**3), 2),
            'error_files': self.stats['error_files'],
            'unique_repos': len(self.stats['repos']),
            'unique_workflows': len(self.stats['workflows']),
            'repos': list(self.stats['repos']),
            'workflows': list(self.stats['workflows']),
        }