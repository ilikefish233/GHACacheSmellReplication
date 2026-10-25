# src/analysis/run_analyzer.py
from typing import Dict, Any, List
from collections import defaultdict

class RunAnalyzer:
    def __init__(self):
        self.runs_by_repo = defaultdict(list)
        self.runs_by_workflow = defaultdict(list)
        self.runs_by_event = defaultdict(list)
        self.failure_patterns = []
    
    def add_run(self, run: Dict[str, Any]):
        repo_id = run.get('repository_id')
        workflow = run.get('workflow_name')
        event = run.get('event')
        
        if repo_id:
            self.runs_by_repo[repo_id].append(run)
        if workflow:
            self.runs_by_workflow[workflow].append(run)
        if event:
            self.runs_by_event[event].append(run)
        
        if run.get('conclusion') in ['failure', 'timed_out']:
            self.failure_patterns.append({
                'run_id': run.get('run_id'),
                'workflow': workflow,
                'event': event,
                'repo_id': repo_id,
            })
    
    def get_summary(self) -> Dict[str, Any]:
        return {
            'repos_count': len(self.runs_by_repo),
            'workflows_count': len(self.runs_by_workflow),
            'events_count': len(self.runs_by_event),
            'failures_count': len(self.failure_patterns),
            'top_workflows': self._get_top(self.runs_by_workflow, 10),
            'top_events': self._get_top(self.runs_by_event, 10),
        }
    
    def _get_top(self, data: dict, n: int) -> List[Dict]:
        sorted_items = sorted(data.items(), key=lambda x: len(x[1]), reverse=True)[:n]
        return [{'name': k, 'count': len(v)} for k, v in sorted_items]