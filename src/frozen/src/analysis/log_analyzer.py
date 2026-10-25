# src/analysis/log_analyzer.py
from typing import Dict, Any, List
from collections import defaultdict
import re

class LogAnalyzer:
    def __init__(self):
        self.error_patterns = defaultdict(int)
        self.warning_patterns = defaultdict(int)
        self.logs_by_repo = defaultdict(list)
    
    def analyze_log(self, log_entry) -> Dict[str, Any]:
        content = log_entry.content
        repo = f"{log_entry.repo_owner}/{log_entry.repo_name}" if log_entry.repo_owner else None
        
        errors = self._extract_errors(content)
        warnings = self._extract_warnings(content)
        
        result = {
            'archive': log_entry.archive_path,
            'repo': repo,
            'workflow': log_entry.workflow_name,
            'error_count': len(errors),
            'warning_count': len(warnings),
            'errors': errors[:10],
            'warnings': warnings[:10],
        }
        
        if repo:
            self.logs_by_repo[repo].append(result)
        
        for err in errors:
            pattern = self._normalize_error(err)
            self.error_patterns[pattern] += 1
        
        for warn in warnings:
            pattern = self._normalize_error(warn)
            self.warning_patterns[pattern] += 1
        
        return result
    
    def _extract_errors(self, content: str) -> List[str]:
        patterns = [
            r'(?i)error[:\s]+(.{0,200})',
            r'(?i)failed[:\s]+(.{0,200})',
            r'(?i)exception[:\s]+(.{0,200})',
            r'(?i)fatal[:\s]+(.{0,200})',
        ]
        errors = []
        for pattern in patterns:
            matches = re.findall(pattern, content)
            errors.extend([m.strip() for m in matches])
        return errors
    
    def _extract_warnings(self, content: str) -> List[str]:
        pattern = r'(?i)warning[:\s]+(.{0,200})'
        matches = re.findall(pattern, content)
        return [m.strip() for m in matches]
    
    def _normalize_error(self, text: str) -> str:
        normalized = re.sub(r'\d+', 'N', text[:100])
        normalized = re.sub(r'0x[0-9a-fA-F]+', 'ADDR', normalized)
        return normalized
    
    def get_summary(self) -> Dict[str, Any]:
        return {
            'total_repos': len(self.logs_by_repo),
            'unique_error_patterns': len(self.error_patterns),
            'unique_warning_patterns': len(self.warning_patterns),
            'top_errors': sorted(self.error_patterns.items(), key=lambda x: x[1], reverse=True)[:20],
            'top_warnings': sorted(self.warning_patterns.items(), key=lambda x: x[1], reverse=True)[:20],
        }