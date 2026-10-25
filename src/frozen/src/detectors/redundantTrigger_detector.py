import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Union
from croniter import croniter


class RedundantTriggerDetector:
    """检测GitHub Actions工作流中的冗余触发问题"""

    def __init__(self):
        self.code_related_extensions = [
            '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.rb',
            '.c', '.cpp', '.h', '.hpp', '.cs', '.swift', '.kt', '.scala',
            '.php', '.pl', '.sh', '.bash', '.zsh', '.ps1',
            '.toml', '.yaml', '.yml', '.json', '.xml', '.gradle',
            '.cmake', '.makefile', '.dockerfile',
        ]
        self.non_code_patterns = [
            r'\.md$',
            r'\.txt$',
            r'\.rst$',
            r'\.png$',
            r'\.jpg$',
            r'\.jpeg$',
            r'\.gif$',
            r'\.svg$',
            r'\.ico$',
            r'\.pdf$',
            r'\.doc',
            r'\.xls',
            r'\.ppt',
            r'^LICENSE',
            r'^COPYING',
            r'^AUTHORS',
            r'^CONTRIBUTING',
            r'^CODE_OF_CONDUCT',
            r'^SECURITY',
        ]
        self._compiled_non_code = [re.compile(p, re.IGNORECASE) for p in self.non_code_patterns]

    def detect_from_file(self, file_path: Union[str, Path]) -> Dict:
        file_path = Path(file_path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}', 'has_redundant_trigger': False}
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return self.detect_from_content(content)

    def detect_from_content(self, content: str) -> Dict:
        try:
            workflow = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {'error': f'YAML解析错误: {e}', 'has_redundant_trigger': False}
        if not workflow:
            return {'error': 'YAML内容为空', 'has_redundant_trigger': False}
        return self._analyze_workflow(workflow)

    def _analyze_workflow(self, workflow: Dict) -> Dict:
        result = {
            'has_redundant_trigger': False,
            'smells': [],
            'trigger_summary': {},
            'error': None
        }
        if 'on' not in workflow:
            result['error'] = '工作流中没有触发器定义'
            return result

        on_config = workflow['on']
        if isinstance(on_config, str):
            result['trigger_summary'] = {'type': 'single', 'event': on_config}
            return result

        if not isinstance(on_config, dict):
            return result

        trigger_events = list(on_config.keys())
        result['trigger_summary']['events'] = trigger_events

        self._check_push_pull_request_overlap(on_config, result)
        self._check_missing_path_filter(on_config, result)
        self._check_broad_branch_filter(on_config, result)
        self._check_schedule_frequency(on_config, result)
        self._check_excessive_triggers(on_config, result)

        result['has_redundant_trigger'] = len(result['smells']) > 0
        return result

    def _check_push_pull_request_overlap(self, on_config: Dict, result: Dict):
        push_branches = self._extract_branches(on_config.get('push', {}))
        pr_branches = self._extract_branches(on_config.get('pull_request', {}))

        if push_branches is None or pr_branches is None:
            return

        overlap = set(push_branches) & set(pr_branches)
        if overlap:
            result['smells'].append({
                'type': 'push_pr_overlap',
                'severity': 'high',
                'message': f'push和pull_request在相同分支上重叠: {sorted(overlap)}',
                'details': {
                    'push_branches': sorted(push_branches),
                    'pr_branches': sorted(pr_branches),
                    'overlap': sorted(overlap),
                    'suggestion': '对来自同一仓库的PR，push和pull_request会触发两次; 考虑使用concurrency或添加paths过滤'
                }
            })

    def _check_missing_path_filter(self, on_config: Dict, result: Dict):
        for event in ['push', 'pull_request']:
            if event not in on_config:
                continue
            event_config = on_config[event]
            if not isinstance(event_config, dict):
                result['smells'].append({
                    'type': 'missing_path_filter',
                    'severity': 'medium',
                    'message': f'{event}触发器未配置paths过滤，任何文件变更都会触发',
                    'details': {
                        'event': event,
                        'suggestion': f'添加paths或paths-ignore过滤，避免文档/资源文件变更触发CI'
                    }
                })
                continue

            has_paths = 'paths' in event_config
            has_paths_ignore = 'paths-ignore' in event_config

            if not has_paths and not has_paths_ignore:
                result['smells'].append({
                    'type': 'missing_path_filter',
                    'severity': 'medium',
                    'message': f'{event}触发器未配置paths过滤，任何文件变更都会触发',
                    'details': {
                        'event': event,
                        'suggestion': f'添加paths或paths-ignore过滤，避免文档/资源文件变更触发CI'
                    }
                })

    def _check_broad_branch_filter(self, on_config: Dict, result: Dict):
        for event in ['push', 'pull_request']:
            if event not in on_config:
                continue
            event_config = on_config[event]
            branches = self._extract_branches(event_config)

            if branches is None:
                result['smells'].append({
                    'type': 'broad_branch_filter',
                    'severity': 'low',
                    'message': f'{event}触发器未指定branches过滤，所有分支都会触发',
                    'details': {
                        'event': event,
                        'suggestion': '添加branches过滤，仅监控关键分支(如main, develop)'
                    }
                })
            elif '**' in branches or '*' in branches:
                result['smells'].append({
                    'type': 'broad_branch_filter',
                    'severity': 'medium',
                    'message': f'{event}触发器使用了通配符分支: {branches}',
                    'details': {
                        'event': event,
                        'branches': branches,
                        'suggestion': '避免使用通配符，明确指定需要监控的分支'
                    }
                })

    def _check_schedule_frequency(self, on_config: Dict, result: Dict):
        if 'schedule' not in on_config:
            return

        schedule_config = on_config['schedule']
        if not isinstance(schedule_config, list):
            return

        for item in schedule_config:
            cron_expr = item.get('cron', '')
            interval_hours = self._estimate_cron_interval(cron_expr)

            if interval_hours is not None and interval_hours < 24:
                result['smells'].append({
                    'type': 'high_frequency_schedule',
                    'severity': 'medium',
                    'message': f'schedule触发频率过高(约每{interval_hours}小时)，cron: "{cron_expr}"',
                    'details': {
                        'cron': cron_expr,
                        'interval_hours': interval_hours,
                        'suggestion': '大多数CI任务每天运行一次即可(如cron: "0 0 * * *")'
                    }
                })
            elif interval_hours is not None and interval_hours < 1:
                result['smells'].append({
                    'type': 'high_frequency_schedule',
                    'severity': 'high',
                    'message': f'schedule触发频率极高(约每{interval_hours * 60:.0f}分钟)，cron: "{cron_expr}"',
                    'details': {
                        'cron': cron_expr,
                        'interval_minutes': interval_hours * 60,
                        'suggestion': '每小时多次触发几乎无必要，建议降低到每天至少一次'
                    }
                })

    def _check_excessive_triggers(self, on_config: Dict, result: Dict):
        events = list(on_config.keys())
        count = len(events)

        if count >= 4:
            result['smells'].append({
                'type': 'excessive_triggers',
                'severity': 'medium',
                'message': f'工作流配置了{count}种触发事件: {events}',
                'details': {
                    'event_count': count,
                    'events': events,
                    'suggestion': '检查是否所有触发事件都是必要的，移除冗余触发'
                }
            })

    def _extract_branches(self, event_config) -> Optional[List[str]]:
        if event_config is None or not isinstance(event_config, dict):
            return None
        branches = event_config.get('branches', event_config.get('branches-ignore'))
        if branches is None:
            return None
        if isinstance(branches, list):
            return branches
        return None

    def _estimate_cron_interval(self, cron_expr: str) -> Optional[float]:
        try:
            if not croniter.is_valid(cron_expr):
                return None
            cron = croniter(cron_expr)
            t1 = cron.get_next(float)
            t2 = cron.get_next(float)
            interval_seconds = t2 - t1
            return interval_seconds / 3600
        except Exception:
            return None


def detect_redundant_trigger(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中的冗余触发问题

    Args:
        yml_input: 文件路径 / YAML内容字符串 / 已解析的字典

    Returns:
        Dict: 检测结果，包含:
              - has_redundant_trigger: bool, 是否存在冗余触发
              - smells: List, 检测到的异味列表
              - trigger_summary: Dict, 触发器概要
              - error: str, 错误信息(如果有)
    """
    detector = RedundantTriggerDetector()

    if isinstance(yml_input, dict):
        return detector._analyze_workflow(yml_input)
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        if path.exists() and path.is_file():
            return detector.detect_from_file(path)
        else:
            return detector.detect_from_content(str(yml_input))

    return {'error': '无效的输入类型', 'has_redundant_trigger': False}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python redundant_trigger_detector.py <yml_file_path>")
        print("  python redundant_trigger_detector.py '<yml_content>'")
        sys.exit(1)

    yml_input = sys.argv[1]
    result = detect_redundant_trigger(yml_input)

    print("\n冗余触发检测结果:")
    print("=" * 80)

    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"存在冗余触发: {'是' if result['has_redundant_trigger'] else '否'}")
        print(f"触发器概要: {result['trigger_summary']}")

        if result['smells']:
            print(f"\n检测到的问题 ({len(result['smells'])}个):")
            for i, smell in enumerate(result['smells'], 1):
                print(f"\n  [{i}] 类型: {smell['type']}")
                print(f"      严重程度: {smell['severity']}")
                print(f"      描述: {smell['message']}")
                if smell.get('details', {}).get('suggestion'):
                    print(f"      建议: {smell['details']['suggestion']}")