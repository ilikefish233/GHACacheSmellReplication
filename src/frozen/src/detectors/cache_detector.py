import yaml
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Union


class CacheDetector:
    """检测GitHub Actions工作流中的cache使用情况，分为4类：
    1. explicit_cache: 显式缓存(actions/cache, actions/restore, actions/save)
    2. package_manager_cache: 包管理器缓存(setup actions，版本感知)
    3. docker_layer_cache: Docker层缓存(docker/build-push-action)
    4. other_cache: 其他第三方缓存action
    """

    def __init__(self, workflows_dir=None, reusable_map=None):
        self.explicit_cache_patterns = [
            'actions/cache',
            'actions/restore',
            'actions/save',
        ]
        self.setup_actions_config = {
            'actions/setup-node': {
                'default_cache_version': 5,
                'npm_default_version': 6,
            },
            'actions/setup-python': {
                'default_cache_version': None,
            },
            'actions/setup-java': {
                'default_cache_version': None,
            },
            'actions/setup-ruby': {
                'default_cache_version': None,
            },
            'actions/setup-go': {
                'default_cache_version': 4,
            },
            'actions/setup-dotnet': {
                'default_cache_version': None,
            },
        }
        self.workflows_dir = Path(workflows_dir) if workflows_dir else None
        self.reusable_map = reusable_map or {}
        self._current_yml_rel_path = None
        self._analyzed_reusable = set()
    
    def _parse_version(self, uses: str) -> Optional[int]:
        match = re.search(r'@v?(\d+)', uses)
        if match:
            return int(match.group(1))
        return None

    def _detect_package_managers(self, job_config: Dict) -> Set[str]:
        package_managers = set()
        for step in job_config.get('steps', []):
            if not isinstance(step, dict):
                continue
            run = step.get('run', '')
            if isinstance(run, str):
                if re.search(r'\bnpm\s+(ci|install|i)\b', run):
                    package_managers.add('npm')
                if re.search(r'\byarn\s+(install|ci)\b', run):
                    package_managers.add('yarn')
                if re.search(r'\bpnpm\s+(install|ci)\b', run):
                    package_managers.add('pnpm')
            uses = step.get('uses', '')
            if 'actions/setup-node' in uses:
                if 'with' in step and 'cache' in step['with']:
                    cache_val = step['with']['cache']
                    if cache_val and cache_val != 'false' and isinstance(cache_val, str):
                        package_managers.add(cache_val)
        return package_managers

    def detect_from_file(self, file_path: Union[str, Path], yml_rel_path: Optional[str] = None) -> Dict:
        """从YAML文件检测cache使用情况
        
        Args:
            file_path: yml文件路径
            yml_rel_path: 相对于output_dir的路径(如workflows/abc.yml)，用于解析可复用工作流引用
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            return {
                'error': f'文件不存在: {file_path}',
                'uses_cache': False
            }
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        self._current_yml_rel_path = yml_rel_path
        self._analyzed_reusable = set()
        
        return self.detect_from_content(content)
    
    def detect_from_content(self, content: str) -> Dict:
        """从YAML内容字符串检测cache使用情况"""
        try:
            workflow = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return {
                'error': f'YAML解析错误: {e}',
                'uses_cache': False
            }
        
        if not workflow:
            return {
                'error': 'YAML内容为空',
                'uses_cache': False
            }
        
        return self._analyze_workflow(workflow)
    
    def _analyze_workflow(self, workflow: Dict) -> Dict:
        result = {
            'uses_cache': False,
            'cache_categories': [],
            'cache_jobs': [],
            'cache_count': 0,
            'error': None,
            'jobs': {},
        }
        if 'jobs' not in workflow:
            result['error'] = '工作流中没有jobs定义'
            return result
        all_categories = set()
        jobs = workflow.get('jobs', {})
        if not isinstance(jobs, dict):
            jobs = {}
        for job_name, job_config in jobs.items():
            job_cache_info = self._analyze_job(job_name, job_config)
            if job_cache_info['uses_cache']:
                result['uses_cache'] = True
                result['cache_jobs'].append(job_cache_info)
                result['cache_count'] += len(job_cache_info['cache_steps'])
                all_categories.update(job_cache_info.get('cache_categories', []))
                result['jobs'][job_name] = {
                    'display_name': job_cache_info.get('display_name', job_name),
                    'cache_categories': job_cache_info.get('cache_categories', []),
                    'cache_steps': [
                        {
                            'action': cs.get('action', ''),
                            'cache_type': cs.get('cache_type'),
                            'cache_category': cs.get('cache_category'),
                        }
                        for cs in job_cache_info.get('cache_steps', [])
                    ],
                }
        result['cache_categories'] = sorted(list(all_categories))
        return result
    
    def _analyze_job(self, job_name: str, job_config: Dict) -> Dict:
        # 处理 job_config 不是字典的情况（如字符串引用或模板）
        if not isinstance(job_config, dict):
            return {
                'job_name': job_name,
                'display_name': job_name,
                'uses_cache': False,
                'cache_categories': [],
                'cache_steps': []
            }
        
        uses_ref = job_config.get('uses', '')
        if uses_ref.startswith('./.github/workflows/') and 'steps' not in job_config:
            return self._analyze_reusable_job(job_name, uses_ref)

        package_managers = self._detect_package_managers(job_config)
        job_cache_info = {
            'job_name': job_name,
            'display_name': job_config.get('name', job_name),
            'uses_cache': False,
            'cache_categories': [],
            'cache_steps': []
        }
        job_categories = set()
        for step in job_config.get('steps', []):
            if not isinstance(step, dict):
                continue
            cache_info = self._analyze_step(step, package_managers)
            if cache_info['uses_cache']:
                job_cache_info['uses_cache'] = True
                job_cache_info['cache_steps'].append(cache_info)
                if cache_info.get('cache_category'):
                    job_categories.add(cache_info['cache_category'])
        job_cache_info['cache_categories'] = sorted(list(job_categories))
        return job_cache_info

    def _analyze_reusable_job(self, job_name: str, uses_path: str) -> Dict:
        result = {
            'job_name': job_name,
            'display_name': job_name,
            'uses_cache': False,
            'cache_categories': [],
            'cache_steps': [],
            'reusable_workflow': uses_path,
        }

        if not self._current_yml_rel_path or not self.workflows_dir:
            return result

        main_refs = self.reusable_map.get(self._current_yml_rel_path, {})
        reusable_yml_rel = main_refs.get(uses_path)
        if not reusable_yml_rel:
            return result

        if reusable_yml_rel in self._analyzed_reusable:
            return result
        self._analyzed_reusable.add(reusable_yml_rel)

        reusable_file = self.workflows_dir / Path(reusable_yml_rel).name
        if not reusable_file.exists():
            return result

        try:
            with open(reusable_file, 'r', encoding='utf-8') as f:
                content = f.read()
            workflow = yaml.safe_load(content)
        except (yaml.YAMLError, IOError):
            return result

        if not workflow:
            return result

        prev_yml_rel = self._current_yml_rel_path
        self._current_yml_rel_path = reusable_yml_rel

        wf_result = self._analyze_workflow(workflow)

        self._current_yml_rel_path = prev_yml_rel

        if wf_result['uses_cache']:
            result['uses_cache'] = True
            result['cache_categories'] = wf_result.get('cache_categories', [])
            for job in wf_result.get('cache_jobs', []):
                for step in job.get('cache_steps', []):
                    step_copy = dict(step)
                    step_copy['from_reusable'] = uses_path
                    result['cache_steps'].append(step_copy)

        return result
    
    def _analyze_step(self, step: Dict, package_managers: Set[str]) -> Dict:
        cache_info = {
            'uses_cache': False,
            'cache_category': None,
            'cache_type': None,
            'action': None,
            'details': {}
        }
        if 'uses' not in step:
            return cache_info
        uses = step['uses']
        cache_info['action'] = uses
        version = self._parse_version(uses)

        # Category 1: Explicit cache
        for pattern in self.explicit_cache_patterns:
            if pattern in uses:
                cache_info['uses_cache'] = True
                cache_info['cache_category'] = 'explicit_cache'
                cache_info['cache_type'] = pattern
                if 'with' in step:
                    cache_info['details'] = {
                        'key': step['with'].get('key'),
                        'path': step['with'].get('path'),
                        'restore-keys': step['with'].get('restore-keys')
                    }
                return cache_info

        # Category 2: Package manager cache
        for setup_action, config in self.setup_actions_config.items():
            if setup_action in uses:
                has_explicit_cache = (
                    'with' in step
                    and step['with'].get('cache') is not None
                    and step['with'].get('cache') is not False
                )
                default_cache_version = config.get('default_cache_version')
                npm_default_version = config.get('npm_default_version')
                if has_explicit_cache:
                    cache_info['uses_cache'] = True
                    cache_info['cache_category'] = 'package_manager_cache'
                    cache_info['cache_type'] = setup_action
                    cache_info['details'] = {
                        'cache': step['with'].get('cache'),
                        'reason': 'explicit_cache_config',
                        'version': version
                    }
                elif default_cache_version is not None and version is not None and version >= default_cache_version:
                    if npm_default_version is not None and version >= npm_default_version:
                        if 'npm' in package_managers:
                            cache_info['uses_cache'] = True
                            cache_info['cache_category'] = 'package_manager_cache'
                            cache_info['cache_type'] = setup_action
                            cache_info['details'] = {
                                'reason': f'v{version}_default_cache_with_npm',
                                'version': version
                            }
                    else:
                        cache_info['uses_cache'] = True
                        cache_info['cache_category'] = 'package_manager_cache'
                        cache_info['cache_type'] = setup_action
                        cache_info['details'] = {
                            'reason': f'v{version}_default_cache',
                            'version': version
                        }
                return cache_info

        # Category 3: Docker layer cache
        if 'docker/build-push-action' in uses:
            if 'with' in step:
                with_config = step['with']
                if with_config.get('cache-from') or with_config.get('cache-to'):
                    cache_info['uses_cache'] = True
                    cache_info['cache_category'] = 'docker_layer_cache'
                    cache_info['cache_type'] = 'docker/build-push-action'
                    cache_info['details'] = {
                        'cache-from': with_config.get('cache-from'),
                        'cache-to': with_config.get('cache-to'),
                    }
            return cache_info

        # Category 4: Other (third-party actions with cache-related params)
        if 'with' in step:
            with_config = step['with']
            cache_keys = [k for k in with_config if 'cache' in k.lower()]
            if cache_keys:
                cache_info['uses_cache'] = True
                cache_info['cache_category'] = 'other_cache'
                cache_info['cache_type'] = uses
                cache_info['details'] = {
                    'cache_params': {k: with_config[k] for k in cache_keys}
                }
        return cache_info


def detect_cache(yml_input: Union[str, Path, Dict]) -> Dict:
    """
    检测GitHub Actions工作流中的cache使用情况
    
    Args:
        yml_input: 可以是以下之一：
                   - 文件路径 (str 或 Path)
                   - YAML内容字符串
                   - 已解析的字典
    
    Returns:
        Dict: 检测结果，包含:
              - uses_cache: bool, 是否使用cache
              - cache_jobs: List, 使用cache的jobs列表
              - cache_count: int, cache步骤总数
              - error: str, 错误信息(如果有)
    """
    detector = CacheDetector()
    
    if isinstance(yml_input, dict):
        return detector._analyze_workflow(yml_input)
    elif isinstance(yml_input, (str, Path)):
        path = Path(yml_input)
        
        if path.exists() and path.is_file():
            return detector.detect_from_file(path)
        else:
            return detector.detect_from_content(str(yml_input))
    
    return {
        'error': '无效的输入类型',
        'uses_cache': False
    }


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  python cache_detector.py <yml_file_path>")
        print("  python cache_detector.py '<yml_content>'")
        print("\n示例:")
        print("  python cache_detector.py workflow.yml")
        print("  python cache_detector.py '.github/workflows/ci.yml'")
        sys.exit(1)
    
    yml_input = sys.argv[1]
    result = detect_cache(yml_input)
    
    print("\nCache检测结果:")
    print("=" * 80)
    
    if result.get('error'):
        print(f"错误: {result['error']}")
    else:
        print(f"使用cache: {'是' if result['uses_cache'] else '否'}")
        print(f"Cache步骤数: {result['cache_count']}")
        
        if result['cache_jobs']:
            print(f"\n使用cache的jobs:")
            for job in result['cache_jobs']:
                print(f"\n  Job: {job['job_name']}")
                for step in job['cache_steps']:
                    print(f"    - Action: {step['action']}")
                    print(f"      类型: {', '.join(step['cache_types'])}")
                    if step['details']:
                        print(f"      详情: {step['details']}")