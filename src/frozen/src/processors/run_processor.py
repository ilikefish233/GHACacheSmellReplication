# src/processors/run_processor.py
from typing import Dict, Any
from .base import BaseProcessor
from datetime import datetime

class RunProcessor(BaseProcessor):
    def __init__(self):
        self.stats = {
            'total_runs': 0,
            'success_runs': 0,
            'failed_runs': 0,
            'total_jobs': 0,
            'total_steps': 0,
            'events': {},
        }
    
    def process(self, record: Dict[str, Any]) -> Dict[str, Any]:
        self.stats['total_runs'] += 1
        
        conclusion = record.get('conclusion', '')
        if conclusion == 'success':
            self.stats['success_runs'] += 1
        elif conclusion in ['failure', 'timed_out']:
            self.stats['failed_runs'] += 1
        
        event = record.get('event', 'unknown')
        self.stats['events'][event] = self.stats['events'].get(event, 0) + 1
        
        jobs = record.get('jobs', [])
        self.stats['total_jobs'] += len(jobs)
        
        for job in jobs:
            self.stats['total_steps'] += len(job.get('steps', []))
        
        duration = self._calculate_duration(record)
        
        return {
            'run_id': record.get('run_id'),
            'repository_id': record.get('repository_id'),
            'workflow_name': record.get('workflow_name'),
            'event': event,
            'status': record.get('status'),
            'conclusion': conclusion,
            'duration_seconds': duration,
            'jobs_count': len(jobs),
            'created_at': record.get('created_at'),
        }
    
    def _calculate_duration(self, record: Dict) -> int:
        try:
            created = record.get('created_at')
            updated = record.get('updated_at')
            if created and updated:
                start = datetime.fromisoformat(created.replace('Z', '+00:00'))
                end = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                return int((end - start).total_seconds())
        except:
            pass
        return 0
    
    def finalize(self) -> Dict[str, Any]:
        return {
            'total_runs': self.stats['total_runs'],
            'success_runs': self.stats['success_runs'],
            'failed_runs': self.stats['failed_runs'],
            'success_rate': round(self.stats['success_runs'] / max(self.stats['total_runs'], 1) * 100, 2),
            'total_jobs': self.stats['total_jobs'],
            'total_steps': self.stats['total_steps'],
            'events': self.stats['events'],
        }