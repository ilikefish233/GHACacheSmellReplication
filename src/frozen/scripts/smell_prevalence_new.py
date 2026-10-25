"""
缓存异味流行度与分布统计脚本（兼容新旧数据源）

新数据源模式（默认）:
  从 {output_dir}/smell_scan_results.jsonl 读取扫描结果
  从 {data_dir}/repo_info.jsonl 读取仓库元数据
  通过 repository_name + workflow_path 关联

旧数据源模式 (--legacy):
  从 output/smell_scan_results.jsonl 读取
  从 data/filtered_repositories.json 读取
  通过 run_ids 提取仓库名

输出:
  output_new/smell_prevalence.json   流行度统计
  output_new/smell_distribution.json 分布统计
  output_new/charts/                 可视化图表

用法:
  python scripts/smell_prevalence_new.py
  python scripts/smell_prevalence_new.py --legacy
  python scripts/smell_prevalence_new.py --data-dir e:/gha_log_analysis/data_26_6_9
"""

import json
import argparse
import re
from pathlib import Path
from collections import defaultdict

import yaml
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

BASE_DIR = Path('e:/gha_log_analysis')

SMELL_TYPES = [
    'no_dependency_cache',
    'no_restore_keys',
    'redundant_cache_update',
    'cache_position_error',
    'conflicting_cache_sources',
    'static_cache_key',
    'overly_specific_cache_key',
    'no_docker_layer_cache',
    'deprecated_cache_action',
    'unreachable_cache_write',
    'cache_as_artifact',
]

NO_RESTORE_SUBTYPES = [
    'missing_restore_keys',
    'ineffective_restore_key',
]

SMELL_TYPE_ALIASES = {
    # Historical scan files used this name; current analyses use the paper's
    # finalized name.
    'disposable_cache_key': 'overly_specific_cache_key',
}


def normalize_smell_type(value):
    """Map historical names and discard non-operationalized candidates."""

    if not isinstance(value, str):
        return None
    normalized = SMELL_TYPE_ALIASES.get(value, value)
    return normalized if normalized in SMELL_TYPES else None


# ========== 分层函数 ==========

def get_star_tier(stars):
    if stars < 100: return '低 (<100)'
    elif stars < 1000: return '中 (100-999)'
    elif stars < 5000: return '高 (1000-4999)'
    else: return '极高 (≥5000)'

def get_fork_tier(forks):
    if forks < 50: return '低 (<50)'
    elif forks < 200: return '中 (50-199)'
    elif forks < 1000: return '高 (200-999)'
    else: return '极高 (≥1000)'

def get_issue_tier(issues):
    if issues < 50: return '低 (<50)'
    elif issues < 200: return '中 (50-199)'
    elif issues < 1000: return '高 (200-999)'
    else: return '极高 (≥1000)'

def get_workflow_tier(n):
    if n <= 2: return '少 (1-2)'
    elif n <= 5: return '中 (3-5)'
    elif n <= 10: return '多 (6-10)'
    else: return '很多 (>10)'

def get_run_tier(runs):
    if runs < 100: return '低频 (<100)'
    elif runs < 300: return '中频 (100-299)'
    elif runs < 600: return '高频 (300-599)'
    else: return '极高频 (≥600)'

def get_commit_tier(commits):
    if commits < 500: return '低 (<500)'
    elif commits < 2000: return '中 (500-1999)'
    elif commits < 5000: return '高 (2000-4999)'
    else: return '极高 (≥5000)'

def get_contributor_tier(n):
    if n < 10: return '少 (<10)'
    elif n < 50: return '中 (10-49)'
    elif n < 200: return '多 (50-199)'
    else: return '很多 (≥200)'

def get_pr_tier(prs):
    if prs < 50: return '低 (<50)'
    elif prs < 200: return '中 (50-199)'
    elif prs < 1000: return '高 (200-999)'
    else: return '极高 (≥1000)'

def get_release_tier(n):
    if n == 0: return '无 (0)'
    elif n <= 10: return '少 (1-10)'
    elif n <= 50: return '中 (11-50)'
    else: return '多 (>50)'

def get_codelines_tier(lines):
    if lines < 10000: return '小 (<1万)'
    elif lines < 50000: return '中 (1万-5万)'
    elif lines < 200000: return '大 (5万-20万)'
    else: return '极大 (≥20万)'


# ========== 通用工具 ==========

def iter_jsonl(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def parse_trigger_types(yml_path: Path, workflows_dir: Path) -> set:
    """
    解析工作流的触发方式，返回触发类型集合

    常见触发类型:
    - push: push 事件
    - pull_request: PR 事件
    - schedule: 定时触发
    - workflow_dispatch: 手动触发
    - release: 发布相关
    - other: 其他类型
    """
    triggers = set()

    try:
        # 尝试读取 YAML 文件
        if not yml_path.exists():
            return triggers

        with open(yml_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # PyYAML 将 'on' 解析为 True，需要替换为其他名称
        # 使用正则替换，只替换顶级的 'on:' 开头的行
        # 更安全的方法是使用 ruamel.yaml，但这里用简单替换
        import re
        # 替换 on: 为 trigger:，避免 YAML 解析问题
        # 需要小心只替换文件顶级的 on:，不是嵌套的
        content_modified = content
        # 简单方法：将 "on:" 替换为 "trigger:"，"on " 替换为 "trigger "
        # 但只处理行首或缩进的 on:
        lines = content.split('\n')
        modified_lines = []
        for line in lines:
            # 匹配行首或缩进后的 "on:" 或 "on "
            if re.match(r'^(\s*)on\s*:', line):
                # 替换为 trigger:
                line = re.sub(r'\bon\s*:', 'trigger:', line, count=1)
            modified_lines.append(line)
        content_modified = '\n'.join(modified_lines)

        data = yaml.safe_load(content_modified)

        if not data:
            return triggers

        # 获取触发配置
        on_config = data.get('trigger')

        if on_config is None:
            # 没有显式定义触发方式，默认为所有可用类型
            return triggers

        # 处理不同的触发方式格式
        if isinstance(on_config, list):
            # 简单数组形式: on: [push, pull_request]
            for item in on_config:
                if isinstance(item, str):
                    triggers.add(_normalize_trigger_type(item))

        elif isinstance(on_config, dict):
            # 映射形式: on: push: branches: [...]
            for key in on_config.keys():
                trigger_type = _normalize_trigger_type(key)
                triggers.add(trigger_type)

    except Exception as e:
        # 解析失败时返回空集合
        pass

    return triggers


def parse_trigger_types_fast(yml_path: Path) -> set:
    """Extract top-level GitHub Actions triggers without constructing YAML.

    The canonical rerun already has detector metadata and only needs the
    trigger cross-tab.  A line-oriented parser avoids loading PyYAML for more
    than one hundred thousand files while retaining the common scalar, list,
    and mapping forms of ``on``.
    """
    triggers = set()
    try:
        if not yml_path.exists():
            return triggers
        lines = yml_path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return triggers

    on_seen = False
    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith('#'):
            continue
        line = raw_line.split('#', 1)[0].rstrip()
        if not on_seen:
            if re.match(r"^\s*['\"]?on['\"]?\s*:", line, re.IGNORECASE):
                on_seen = True
                value = re.split(r":", line, maxsplit=1)[1].strip()
                if value.startswith('[') and value.endswith(']'):
                    for item in value[1:-1].split(','):
                        item = item.strip().strip("'\"")
                        if item:
                            triggers.add(_normalize_trigger_type(item))
                    return triggers
                if value.startswith('{') and value.endswith('}'):
                    for item in value[1:-1].split(','):
                        item = item.split(':', 1)[0].strip().strip("'\"")
                        if item:
                            triggers.add(_normalize_trigger_type(item))
                    return triggers
                if value:
                    triggers.add(_normalize_trigger_type(value.strip("'\"")))
                    return triggers
            continue

        # Mapping-form trigger keys are normally two spaces below ``on``.
        if raw_line.startswith(' ') and len(raw_line) - len(raw_line.lstrip()) >= 2:
            stripped = line.strip()
            match = re.match(r"^['\"]?([A-Za-z0-9_]+)['\"]?\s*:", stripped)
            if match:
                triggers.add(_normalize_trigger_type(match.group(1)))
            continue
        break
    return triggers


def _normalize_trigger_type(trigger: str) -> str:
    """标准化触发类型名称"""
    trigger_lower = trigger.lower()

    # 映射到标准类型
    if trigger_lower in ['push', 'pushes']:
        return 'push'
    elif trigger_lower in ['pull_request', 'pull_request_target', 'pull_request']:
        return 'pull_request'
    elif trigger_lower in ['schedule', 'scheduled']:
        return 'schedule'
    elif trigger_lower in ['workflow_dispatch', 'workflow_call']:
        return 'workflow_dispatch'
    elif trigger_lower in ['release', 'release', 'published', 'created', 'published']:
        return 'release'
    elif trigger_lower in ['create', 'delete', 'fork', 'issue', 'issue_comment',
                           'label', 'milestone', 'page_build', 'status', 'watch',
                           'repository_dispatch', 'registry_package']:
        return 'other'
    else:
        return 'other'


def extract_repo_from_run_id(run_id):
    """从 run_id 提取仓库名，格式如 'owner/repo_.github/workflows/xxx.yml_N_M'"""
    if not run_id:
        return None
    parts = run_id.split('_.github/')
    if len(parts) >= 2:
        return parts[0]
    return None


# ========== 数据源加载 ==========

def load_repo_meta_new(data_dir: Path) -> dict:
    """新数据源：从 repo_info.jsonl 加载仓库元数据"""
    repos_file = data_dir / 'repo_info.jsonl'
    repo_meta = {}
    if not repos_file.exists():
        print(f"  警告: {repos_file} 不存在，仓库特征分布将不可用")
        return repo_meta
    for item in iter_jsonl(repos_file):
        repo_id = item.get('_id', '')
        repo_info = item.get('repo', {})
        repo_meta[repo_id] = {
            'mainLanguage': repo_info.get('mainLanguage') or 'Unknown',
            'stargazers': repo_info.get('stargazers') or 0,
            'forks': repo_info.get('forks') or 0,
            'totalIssues': repo_info.get('totalIssues') or 0,
            'commits': repo_info.get('commits') or 0,
            'contributors': repo_info.get('contributors') or 0,
            'totalPullRequests': repo_info.get('totalPullRequests') or 0,
            'releases': repo_info.get('releases') or 0,
            'codeLines': repo_info.get('codeLines') or 0,
            'total_runs_90d': item.get('total_runs_90d') or 0,
            'nb_workflows': item.get('nb_workflows') or 0,
        }
    return repo_meta


def load_repo_meta_legacy() -> dict:
    """旧数据源：从 filtered_repositories.json 加载仓库元数据"""
    repos_file = BASE_DIR / 'data' / 'filtered_repositories.json'
    repo_meta = {}
    if not repos_file.exists():
        return repo_meta
    for item in iter_jsonl(repos_file):
        repo_id = item.get('_id', '')
        repo_info = item.get('repo', {})
        repo_meta[repo_id] = {
            'mainLanguage': repo_info.get('mainLanguage') or 'Unknown',
            'stargazers': repo_info.get('stargazers') or 0,
            'forks': repo_info.get('forks') or 0,
            'totalIssues': repo_info.get('totalIssues') or 0,
            'commits': repo_info.get('commits') or 0,
            'contributors': repo_info.get('contributors') or 0,
            'totalPullRequests': repo_info.get('totalPullRequests') or 0,
            'releases': repo_info.get('releases') or 0,
            'codeLines': repo_info.get('codeLines') or 0,
            'total_runs_90d': item.get('total_runs_90d') or 0,
            'nb_workflows': item.get('nb_workflows') or 0,
        }
    return repo_meta


def iter_smell_records_new(smell_file: Path):
    """新数据源：直接返回 (yml_hash, repo_name, yml_file, detectors, summary, inner_data)"""
    for item in iter_jsonl(smell_file):
        for yml_hash, inner_data in item.items():
            repo_name = inner_data.get('repository_name', '')
            yml_file = inner_data.get('yml_file', '')  # 使用 yml_file 而不是 workflow_path
            detectors = inner_data.get('detectors', {})
            summary = inner_data.get('summary', {})
            yield yml_hash, repo_name, yml_file, detectors, summary, inner_data


def iter_smell_records_legacy(smell_file: Path):
    """旧数据源：从 run_ids 提取仓库名"""
    for item in iter_jsonl(smell_file):
        for yml_hash, inner_data in item.items():
            run_ids = inner_data.get('run_ids', [])
            repo_name = extract_repo_from_run_id(run_ids[0]) if run_ids else None
            workflow_path = inner_data.get('yml_file', '')
            detectors = inner_data.get('detectors', {})
            summary = inner_data.get('summary', {})
            yield yml_hash, repo_name or '', workflow_path, detectors, summary, inner_data


# ========== 主流程 ==========

def main():
    parser = argparse.ArgumentParser(description='缓存异味流行度与分布统计')
    parser.add_argument('--data-dir', type=str, default=r'e:/gha_log_analysis/data_26_6_9',
                        help='新数据源目录')
    parser.add_argument('--output-dir', type=str, default=r'e:/gha_log_analysis/output_new',
                        help='输出目录')
    parser.add_argument('--smell-scan-dir', type=str, default=None,
                        help='扫描结果所在目录；可与统计输出目录分离')
    parser.add_argument('--legacy', action='store_true',
                        help='使用旧数据源')
    parser.add_argument('--fast-triggers', action='store_true',
                        help='使用行级解析提取 on 触发器，避免逐文件 PyYAML 构造')
    args = parser.parse_args()

    is_legacy = args.legacy
    data_dir = Path(args.data_dir) if not is_legacy else BASE_DIR / 'data'
    output_dir = Path(args.output_dir) if not is_legacy else BASE_DIR / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)

    # 确定输入文件
    if is_legacy:
        smell_file = BASE_DIR / 'output' / 'smell_scan_results.jsonl'
    else:
        smell_scan_dir = Path(args.smell_scan_dir) if args.smell_scan_dir else output_dir
        smell_file = smell_scan_dir / 'smell_scan_results.jsonl'

    prevalence_file = output_dir / 'smell_prevalence.json'
    distribution_file = output_dir / 'smell_distribution.json'
    charts_dir = output_dir / 'charts'
    charts_dir.mkdir(parents=True, exist_ok=True)

    print(f"模式: {'旧数据源' if is_legacy else '新数据源'}")
    print(f"扫描结果: {smell_file}")

    # 加载仓库元数据
    print("加载仓库元数据...")
    if is_legacy:
        repo_meta = load_repo_meta_legacy()
    else:
        repo_meta = load_repo_meta_new(data_dir)
    print(f"  仓库数: {len(repo_meta)}")

    # 各特征分层的仓库总数在读取扫描结果后再统计，确保分母只包含
    # 当前扫描语料中的仓库，而不是 repo_info.jsonl 中未进入扫描的仓库。
    tier_totals = defaultdict(lambda: defaultdict(int))

    # 选择迭代器
    iter_smell = iter_smell_records_legacy if is_legacy else iter_smell_records_new

    # 计数器
    total_workflows = 0
    detector_error_workflows = 0
    total_repos = set()

    smell_instance_count = defaultdict(int)
    smell_workflow_set = defaultdict(set)
    smell_repo_count = defaultdict(set)
    smell_subtype_instance_count = defaultdict(lambda: defaultdict(int))
    smell_subtype_workflow_set = defaultdict(lambda: defaultdict(set))
    # Conditional-prevalence denominators.  Each smell is evaluated only
    # among workflows where its predicate is observable, rather than against
    # the complete workflow corpus.
    eligible_workflows = {stype: set() for stype in SMELL_TYPES}
    eligible_repos = {stype: set() for stype in SMELL_TYPES}
    any_smell_wf_count = 0
    any_smell_repos = set()

    # 分布
    smell_by_lang = defaultdict(lambda: defaultdict(set))
    lang_repo_total = defaultdict(int)
    repo_lang = {}
    smell_by_cache_usage_repos = defaultdict(lambda: defaultdict(set))
    smell_by_cache_usage_instances = defaultdict(lambda: defaultdict(int))
    smell_by_cache_category_repos = defaultdict(lambda: defaultdict(set))
    smell_by_cache_category_instances = defaultdict(lambda: defaultdict(int))
    smell_by_job_name_repos = defaultdict(lambda: defaultdict(set))
    smell_by_job_name_instances = defaultdict(lambda: defaultdict(int))

    # 触发方式分布
    TRIGGER_TYPES = ['push', 'pull_request', 'schedule', 'workflow_dispatch', 'release', 'other']
    trigger_by_workflows = defaultdict(set)  # trigger_type -> set(yml_hash)
    smell_by_trigger_repos = defaultdict(lambda: defaultdict(set))  # trigger_type -> smell_type -> repos
    smell_by_trigger_instances = defaultdict(lambda: defaultdict(int))  # trigger_type -> smell_type -> count
    workflows_with_trigger = {}  # yml_hash -> set(trigger_types)

    # 触发方式组合统计
    trigger_combinations = defaultdict(int)  # (trigger1,trigger2) -> count

    # 仓库级特征分布
    # Repository popularity stratification is intentionally omitted.  The
    # paper reports overall prevalence over the complete scanned corpus; this
    # dictionary is reserved for non-popularity contextual distributions.
    feature_tier_funcs = {
        'forks': (get_fork_tier, 'forks'),
        'issues': (get_issue_tier, 'totalIssues'),
        'workflows': (get_workflow_tier, 'nb_workflows'),
        'runs': (get_run_tier, 'total_runs_90d'),
        'commits': (get_commit_tier, 'commits'),
        'contributors': (get_contributor_tier, 'contributors'),
        'pull_requests': (get_pr_tier, 'totalPullRequests'),
        'releases': (get_release_tier, 'releases'),
        'code_lines': (get_codelines_tier, 'codeLines'),
    }
    smell_by_feature_repos = {feat: defaultdict(lambda: defaultdict(set)) for feat in feature_tier_funcs}
    smell_by_feature_instances = {feat: defaultdict(lambda: defaultdict(int)) for feat in feature_tier_funcs}

    repo_has_smell = defaultdict(set)
    repo_uses_cache = {}

    # 新增：工作流复杂度分析
    # 按Job数量分层
    smell_by_job_count = defaultdict(lambda: defaultdict(int))  # job_tier -> smell_type -> count
    smell_by_job_count_repos = defaultdict(lambda: defaultdict(set))  # job_tier -> smell_type -> repos
    workflow_job_counts = {}  # yml_hash -> job数量

    # 按步骤数量分层（总步骤数）
    smell_by_step_count = defaultdict(lambda: defaultdict(int))
    smell_by_step_count_repos = defaultdict(lambda: defaultdict(set))
    workflow_step_counts = {}  # yml_hash -> 步骤数量

    # 定义复杂度分层函数
    def get_job_count_tier(n):
        if n == 1: return '1个Job'
        elif n <= 3: return '2-3个Job'
        elif n <= 5: return '4-5个Job'
        else: return '>5个Job'

    def get_step_count_tier(n):
        if n <= 5: return '短(≤5步)'
        elif n <= 15: return '中(6-15步)'
        elif n <= 30: return '长(16-30步)'
        else: return '很长(>30步)'

    print("处理 smell 扫描结果...")
    processed = 0
    for yml_hash, repo_name, yml_file, detectors, summary, inner_data in iter_smell(smell_file):
        processed += 1
        if processed % 5000 == 0:
            print(f"  已处理 {processed} 条...", flush=True)

        cache_smell_detector = detectors.get('cache_smell', {}) or {}
        if cache_smell_detector.get('error'):
            # A failed detector result is not evidence that the workflow has
            # no smell.  Exclude it from prevalence denominators and report it
            # explicitly for coverage accounting.
            detector_error_workflows += 1
            continue

        total_workflows += 1
        cache_detector = detectors.get('cache', {})
        cache_smell_detector = detectors.get('cache_smell', {})

        uses_cache = cache_detector.get('use_cache', False) if cache_detector else False
        cache_categories = cache_detector.get('cache_categories', []) if cache_detector else []

        if repo_name:
            total_repos.add(repo_name)
            repo_uses_cache[repo_name] = uses_cache

        # 从扫描结果中获取 metadata。
        # 流行度统一使用全部扫描工作流作为分母；metadata 仅用于复杂度分布，
        # 不再参与异味流行度资格筛选。
        cache_smell_detector = detectors.get('cache_smell', {})
        metadata = cache_smell_detector.get('metadata', {})

        # Determine the observable opportunity set for each smell type from
        # the same detector metadata used by the adjusted RQ2 profiles.
        jobs = metadata.get('jobs', {}) if isinstance(metadata, dict) else {}
        cache_job_names = []
        has_dependency_install = False
        has_cache_step = False
        has_parallel_cache_jobs = False
        has_docker_build = False
        if isinstance(jobs, dict):
            for job_name, job_data in jobs.items():
                if not isinstance(job_data, dict):
                    continue
                analysis = job_data.get('analysis', {}) or {}
                if not isinstance(analysis, dict):
                    continue
                dep_langs = analysis.get('dep_langs') or []
                dep_managers = analysis.get('dep_managers') or []
                dep_indices = analysis.get('dep_install_indices') or []
                dep_steps = analysis.get('dep_install_steps') or []
                if dep_langs or dep_managers or dep_indices or dep_steps:
                    has_dependency_install = True
                cache_steps = analysis.get('cache_steps') or []
                if cache_steps:
                    has_cache_step = True
                    cache_job_names.append((str(job_name), analysis.get('needs') or []))
                if analysis.get('docker_build_detected'):
                    has_docker_build = True
        if len(cache_job_names) >= 2:
            # Jobs with no dependency on another cache job are observable
            # parallel-cache candidates.  This is deliberately conservative;
            # the smell detector itself remains responsible for the numerator.
            has_parallel_cache_jobs = any(not needs for _, needs in cache_job_names)

        if has_dependency_install:
            eligible_workflows['no_dependency_cache'].add(yml_hash)
            if repo_name:
                eligible_repos['no_dependency_cache'].add(repo_name)
        if has_cache_step:
            for stype in (
                'no_restore_keys',
                'cache_position_error',
                'conflicting_cache_sources',
                'static_cache_key',
                'overly_specific_cache_key',
                'deprecated_cache_action',
                'unreachable_cache_write',
                'cache_as_artifact',
            ):
                eligible_workflows[stype].add(yml_hash)
                if repo_name:
                    eligible_repos[stype].add(repo_name)
        if has_docker_build:
            eligible_workflows['no_docker_layer_cache'].add(yml_hash)
            if repo_name:
                eligible_repos['no_docker_layer_cache'].add(repo_name)
        if has_parallel_cache_jobs:
            eligible_workflows['redundant_cache_update'].add(yml_hash)
            if repo_name:
                eligible_repos['redundant_cache_update'].add(repo_name)

        if metadata and 'jobs' in metadata:
            jobs_analysis = metadata['jobs']

            # 收集工作流复杂度数据
            job_count = len(jobs_analysis)
            total_steps = 0
            for job_name, job_data in jobs_analysis.items():
                # 估算步骤数量：从 analysis 中获取
                # cache_steps, setup_steps, dep_install_indices 等都是索引，可以估算
                analysis = job_data.get('analysis', {})
                step_indices = set()
                step_indices.update(analysis.get('cache_step_indices', []))
                step_indices.update(analysis.get('dep_install_indices', []))
                if 'checkout_idx' in analysis and analysis['checkout_idx'] is not None:
                    step_indices.add(analysis['checkout_idx'])
                # 估算总步骤数（最大索引 + 1）
                if step_indices:
                    total_steps = max(total_steps, max(step_indices) + 1)
                else:
                    total_steps += 1  # 至少有一个步骤

            workflow_job_counts[yml_hash] = job_count
            workflow_step_counts[yml_hash] = total_steps

        # 语言统计
        if repo_name and repo_name in repo_meta:
            lang = repo_meta[repo_name]['mainLanguage']
            if repo_name not in repo_lang:
                repo_lang[repo_name] = lang
                lang_repo_total[lang] += 1

        # ===== 触发方式解析 =====
        # 只在新数据源模式下解析触发方式（需要访问原始 YAML 文件）
        if not is_legacy and yml_file:
            # yml_file 格式: workflows/repo_name/workflow_name.yml
            # 构建完整路径: data_dir/yml_file
            workflow_file = data_dir / yml_file
            triggers = (
                parse_trigger_types_fast(workflow_file)
                if args.fast_triggers
                else parse_trigger_types(workflow_file, data_dir / 'workflows')
            )
            workflows_with_trigger[yml_hash] = triggers

            # 统计各触发方式的工作流
            for trigger in triggers:
                trigger_by_workflows[trigger].add(yml_hash)

            # 统计触发方式组合
            if len(triggers) > 1:
                sorted_triggers = sorted(triggers)
                # 记录所有组合对
                for i in range(len(sorted_triggers)):
                    for j in range(i + 1, len(sorted_triggers)):
                        combo = (sorted_triggers[i], sorted_triggers[j])
                        trigger_combinations[combo] += 1

        # 收集异味
        smells = cache_smell_detector.get('smells', [])
        workflow_smell_types = set()

        for s in smells:
            stype = normalize_smell_type(s.get('type', ''))
            if stype is None:
                continue
            workflow_smell_types.add(stype)

            # job 名称
            job_name = s.get('job_name', '')
            smell_by_job_name_instances[stype][job_name] += 1
            if repo_name:
                smell_by_job_name_repos[stype][job_name].add(repo_name)

            # 仓库特征分布
            if repo_name and repo_name in repo_meta:
                meta = repo_meta[repo_name]
                smell_by_lang[stype][meta['mainLanguage']].add(repo_name)

                for feat, (tier_func, meta_key) in feature_tier_funcs.items():
                    tier = tier_func(meta[meta_key])
                    smell_by_feature_repos[feat][stype][tier].add(repo_name)
                    smell_by_feature_instances[feat][stype][tier] += 1
            elif repo_name:
                # 无元数据时仍统计实例数
                for feat in feature_tier_funcs:
                    pass  # 无法分层

            smell_by_cache_usage_instances[stype][str(uses_cache)] += 1
            if repo_name:
                smell_by_cache_usage_repos[stype][str(uses_cache)].add(repo_name)
            for cc in cache_categories:
                smell_by_cache_category_instances[stype][cc] += 1
                if repo_name:
                    smell_by_cache_category_repos[stype][cc].add(repo_name)

            # 触发方式分布统计
            if yml_hash in workflows_with_trigger:
                for trigger in workflows_with_trigger[yml_hash]:
                    smell_by_trigger_instances[trigger][stype] += 1
                    if repo_name:
                        smell_by_trigger_repos[trigger][stype].add(repo_name)

            smell_instance_count[stype] += 1
            smell_workflow_set[stype].add(yml_hash)
            if stype == 'no_restore_keys':
                details = s.get('details', {}) if isinstance(s, dict) else {}
                subtype = details.get('subtype') if isinstance(details, dict) else None
                if subtype not in NO_RESTORE_SUBTYPES:
                    # Older scan records did not emit a subtype.  They are
                    # historical missing-restore-keys findings unless the
                    # detector explicitly identifies the ineffective form.
                    subtype = 'missing_restore_keys'
                smell_subtype_instance_count[stype][subtype] += 1
                smell_subtype_workflow_set[stype][subtype].add(yml_hash)
            if repo_name:
                smell_repo_count[stype].add(repo_name)
                repo_has_smell[stype].add(repo_name)

            # 新增：工作流复杂度分层统计
            if yml_hash in workflow_job_counts:
                job_tier = get_job_count_tier(workflow_job_counts[yml_hash])
                smell_by_job_count[job_tier][stype] += 1
                if repo_name:
                    smell_by_job_count_repos[job_tier][stype].add(repo_name)

            if yml_hash in workflow_step_counts:
                step_tier = get_step_count_tier(workflow_step_counts[yml_hash])
                smell_by_step_count[step_tier][stype] += 1
                if repo_name:
                    smell_by_step_count_repos[step_tier][stype].add(repo_name)

        if workflow_smell_types:
            any_smell_wf_count += 1
            if repo_name:
                any_smell_repos.add(repo_name)

    # ---- 后处理：计算触发方式与异味的整体分布 ----
    print("计算触发方式与异味的关联...")

    total_repos_count = len(total_repos)
    print(
        f"  总工作流: {total_workflows}, 总仓库: {total_repos_count}, "
        f"检测器错误排除: {detector_error_workflows}"
    )

    # 只使用扫描语料中的仓库作为分层分母。repo_info.jsonl 还包含大量
    # 未进入本次扫描的仓库，直接使用其总数会稀释所有分层比例。
    for repo_id in total_repos:
        meta = repo_meta.get(repo_id)
        if not meta:
            continue
        tier_totals['forks'][get_fork_tier(meta['forks'])] += 1
        tier_totals['issues'][get_issue_tier(meta['totalIssues'])] += 1
        tier_totals['workflows'][get_workflow_tier(meta['nb_workflows'])] += 1
        tier_totals['runs'][get_run_tier(meta['total_runs_90d'])] += 1
        tier_totals['commits'][get_commit_tier(meta['commits'])] += 1
        tier_totals['contributors'][get_contributor_tier(meta['contributors'])] += 1
        tier_totals['pull_requests'][get_pr_tier(meta['totalPullRequests'])] += 1
        tier_totals['releases'][get_release_tier(meta['releases'])] += 1
        tier_totals['code_lines'][get_codelines_tier(meta['codeLines'])] += 1

    # ---- 构建流行度输出 ----
    prevalence = {
        'total_workflows': total_workflows,
        'scan_records_processed': processed,
        'detector_error_workflows_excluded': detector_error_workflows,
        'total_repos': total_repos_count,
        'denominator_definition': (
            'conditional workflow opportunity set per smell type; detector-error '
            'records excluded from both numerator and denominator'
        ),
        'operationalized_smell_types': list(SMELL_TYPES),
        'any_smell_definition': 'at least one operationalized smell type; non-operationalized catalog candidates are excluded',
        'popularity_stratification': 'omitted by study design; only overall prevalence is reported',
        'trigger_parser': 'fast_line_parser' if args.fast_triggers else 'pyyaml',
        'any_smell': {
            'workflow_count': any_smell_wf_count,
            'workflow_pct': round(any_smell_wf_count / total_workflows * 100, 2) if total_workflows else 0,
            'repo_count': len(any_smell_repos),
            'repo_pct': round(len(any_smell_repos) / total_repos_count * 100, 2) if total_repos_count else 0,
        },
        'by_smell_type': {},
    }

    for stype in SMELL_TYPES:
        wf_count = len(smell_workflow_set.get(stype, set()))
        instance_count = smell_instance_count.get(stype, 0)
        repo_set = smell_repo_count.get(stype, set())
        eligible_count = len(eligible_workflows.get(stype, set()))
        eligible_repo_count = len(eligible_repos.get(stype, set()))
        avg_instances = round(instance_count / wf_count, 2) if wf_count > 0 else 0

        # Conditional prevalence: only workflows satisfying the observable
        # opportunity predicate are eligible for this smell's denominator.
        workflow_pct = round(wf_count / eligible_count * 100, 2) if eligible_count else 0
        instance_pct = round(instance_count / eligible_count * 100, 2) if eligible_count else 0

        prevalence['by_smell_type'][stype] = {
            'workflow_count': wf_count,
            'eligible_workflows': eligible_count,
            'workflow_pct': workflow_pct,
            'instance_count': instance_count,
            'instance_pct': instance_pct,
            'avg_instances_per_workflow': avg_instances,
            'repo_count': len(repo_set),
            'eligible_repos': eligible_repo_count,
            'repo_pct': round(len(repo_set) / eligible_repo_count * 100, 2) if eligible_repo_count else 0,
        }
        if stype == 'no_restore_keys':
            prevalence['by_smell_type'][stype]['subtypes'] = {
                subtype: {
                    'instance_count': smell_subtype_instance_count[stype].get(subtype, 0),
                    'workflow_count': len(smell_subtype_workflow_set[stype].get(subtype, set())),
                }
                for subtype in NO_RESTORE_SUBTYPES
            }

    # ---- 构建分布输出 ----
    distribution = {
        'by_language': {},
        'by_cache_usage': {},
        'by_cache_category': {},
        'by_job_name': {},
        'by_repo_feature': {},
        # 新增：工作流复杂度分析
        'by_job_count': {},
        'by_step_count': {},
        # 新增：触发方式分析
        'by_trigger_type': {},
        'trigger_overview': {},
    }

    # 按语言
    # no_dependency_cache 只统计支持的语言（JS/Python/TypeScript）
    SUPPORTED_LANGS_FOR_NDC = {'JavaScript', 'TypeScript', 'Python'}
    for stype in SMELL_TYPES:
        lang_dist = smell_by_lang.get(stype, {})
        sorted_langs = sorted(lang_dist.items(), key=lambda x: len(x[1]), reverse=True)

        # 对于 no_dependency_cache，过滤只支持的语言
        if stype == 'no_dependency_cache':
            filtered_langs = [(lang, repos) for lang, repos in sorted_langs if lang in SUPPORTED_LANGS_FOR_NDC]
        else:
            filtered_langs = sorted_langs

        distribution['by_language'][stype] = [
            {'language': lang, 'count': len(repo_set),
             'pct': round(len(repo_set) / lang_repo_total.get(lang, 1) * 100, 2)}
            for lang, repo_set in filtered_langs
        ]

    # 按缓存使用状态
    for stype in SMELL_TYPES:
        cache_dist_repos = smell_by_cache_usage_repos.get(stype, {})
        cache_dist_instances = smell_by_cache_usage_instances.get(stype, {})
        all_keys = set(cache_dist_repos.keys()) | set(cache_dist_instances.keys())
        distribution['by_cache_usage'][stype] = [
            {'uses_cache': k, 'repo_count': len(cache_dist_repos.get(k, set())),
             'instance_count': cache_dist_instances.get(k, 0)}
            for k in sorted(all_keys)
        ]

    # 按缓存类别
    for stype in SMELL_TYPES:
        cat_dist_repos = smell_by_cache_category_repos.get(stype, {})
        cat_dist_instances = smell_by_cache_category_instances.get(stype, {})
        all_keys = set(cat_dist_repos.keys()) | set(cat_dist_instances.keys())
        sorted_cats = sorted(all_keys, key=lambda x: cat_dist_instances.get(x, 0), reverse=True)
        distribution['by_cache_category'][stype] = [
            {'cache_category': cat, 'repo_count': len(cat_dist_repos.get(cat, set())),
             'instance_count': cat_dist_instances.get(cat, 0)}
            for cat in sorted_cats
        ]

    # 按 job 名称 (top 30)
    for stype in SMELL_TYPES:
        job_dist_repos = smell_by_job_name_repos.get(stype, {})
        job_dist_instances = smell_by_job_name_instances.get(stype, {})
        all_jobs = set(job_dist_repos.keys()) | set(job_dist_instances.keys())
        sorted_jobs = sorted(all_jobs, key=lambda x: job_dist_instances.get(x, 0), reverse=True)
        total_instances = smell_instance_count.get(stype, 1)
        distribution['by_job_name'][stype] = [
            {'job_name': jn if jn else '(unnamed)', 'repo_count': len(job_dist_repos.get(jn, set())),
             'instance_count': job_dist_instances.get(jn, 0),
             'instance_pct': round(job_dist_instances.get(jn, 0) / total_instances * 100, 2)}
            for jn in sorted_jobs[:30]
        ]

    # 新增：按工作流复杂度 - Job数量
    for stype in SMELL_TYPES:
        job_count_dist = smell_by_job_count.get(stype, {})
        sorted_tiers = sorted(job_count_dist.items(), key=lambda x: x[1], reverse=True)
        distribution['by_job_count'][stype] = [
            {'tier': tier, 'count': count,
             'pct': round(count / sum(job_count_dist.values()) * 100, 2) if job_count_dist else 0}
            for tier, count in sorted_tiers
        ]

    # 新增：按工作流复杂度 - 步骤数量
    for stype in SMELL_TYPES:
        step_count_dist = smell_by_step_count.get(stype, {})
        sorted_tiers = sorted(step_count_dist.items(), key=lambda x: x[1], reverse=True)
        distribution['by_step_count'][stype] = [
            {'tier': tier, 'count': count,
             'pct': round(count / sum(step_count_dist.values()) * 100, 2) if step_count_dist else 0}
            for tier, count in sorted_tiers
        ]

    # 新增：按触发方式分布
    for stype in SMELL_TYPES:
        trigger_dist_repos = smell_by_trigger_repos.get(stype, {})
        trigger_dist_instances = smell_by_trigger_instances.get(stype, {})
        all_triggers = set(trigger_dist_repos.keys()) | set(trigger_dist_instances.keys())
        distribution['by_trigger_type'][stype] = [
            {'trigger_type': trigger,
             'repo_count': len(trigger_dist_repos.get(trigger, set())),
             'instance_count': trigger_dist_instances.get(trigger, 0)}
            for trigger in sorted(all_triggers)
        ]

    # 触发方式概览统计
    for trigger in TRIGGER_TYPES:
        wf_count = len(trigger_by_workflows.get(trigger, set()))
        trigger_overview = {
            'workflow_count': wf_count,
            'workflow_pct': round(wf_count / total_workflows * 100, 2) if total_workflows > 0 else 0,
        }
        # 统计该触发方式下的异味工作流数
        smell_wf_counts = {}
        smell_overall_pcts = {}
        for stype in SMELL_TYPES:
            smell_wfs = trigger_by_workflows.get(trigger, set()) & smell_workflow_set.get(stype, set())
            smell_wf_counts[stype] = len(smell_wfs)
            smell_overall_pcts[stype] = round(len(smell_wfs) / total_workflows * 100, 2) if total_workflows else 0
        trigger_overview['smell_workflow_counts'] = smell_wf_counts
        trigger_overview['smell_workflow_pct'] = smell_overall_pcts
        distribution['trigger_overview'][trigger] = trigger_overview

    # 触发方式组合统计
    combination_list = [
        {'triggers': f'{t1}+{t2}', 'count': count}
        for (t1, t2), count in sorted(trigger_combinations.items(), key=lambda x: x[1], reverse=True)
    ]
    distribution['trigger_combinations'] = combination_list[:20]  # Top 20


    # 按仓库特征
    any_smell_by_feature = {feat: defaultdict(set) for feat in feature_tier_funcs}
    for repo_name in any_smell_repos:
        if repo_name not in repo_meta:
            continue
        meta = repo_meta[repo_name]
        for feat, (tier_func, meta_key) in feature_tier_funcs.items():
            any_smell_by_feature[feat][tier_func(meta[meta_key])].add(repo_name)

    for feat in feature_tier_funcs:
        feature_data = {}
        # 任意异味
        any_dist = any_smell_by_feature[feat]
        feature_data['any_smell'] = [
            {'tier': tier, 'repo_count': len(repos),
             'repo_pct': round(len(repos) / tier_totals[feat].get(tier, 1) * 100, 2)}
            for tier, repos in sorted(any_dist.items())
        ]
        # 各异味类型
        for stype in SMELL_TYPES:
            stype_dist_repos = smell_by_feature_repos[feat].get(stype, {})
            stype_dist_instances = smell_by_feature_instances[feat].get(stype, {})
            all_tiers = set(stype_dist_repos.keys()) | set(stype_dist_instances.keys())
            feature_data[stype] = [
                {'tier': tier, 'repo_count': len(stype_dist_repos.get(tier, set())),
                 'instance_count': stype_dist_instances.get(tier, 0),
                 'repo_pct': round(len(stype_dist_repos.get(tier, set())) / tier_totals[feat].get(tier, 1) * 100, 2)}
                for tier in sorted(all_tiers)
            ]
        distribution['by_repo_feature'][feat] = feature_data

    # ---- 保存结果 ----
    with open(prevalence_file, 'w', encoding='utf-8') as f:
        json.dump(prevalence, f, indent=2, ensure_ascii=False)
    with open(distribution_file, 'w', encoding='utf-8') as f:
        json.dump(distribution, f, indent=2, ensure_ascii=False)

    print(f"\n流行度统计: {prevalence_file}")
    print(f"分布统计: {distribution_file}")

    # ---- 打印摘要 ----
    print("\n=== 流行度摘要（整体流行度） ===")
    print(f"总工作流: {total_workflows}, 总仓库: {total_repos_count}")
    any_pct = prevalence['any_smell']['workflow_pct']
    print(f"有异味的工作流: {any_smell_wf_count} ({any_pct}%)")

    print("\n异味整体流行度（分母为全部扫描工作流）:")
    for stype in SMELL_TYPES:
        info = prevalence['by_smell_type'][stype]
        print(f"  {stype}: {info['workflow_count']} / {total_workflows} 工作流 = {info['workflow_pct']}%, "
              f"{info['repo_count']} 仓库 ({info['repo_pct']}%)")

    # 打印触发方式摘要
    if distribution.get('trigger_overview'):
        print("\n=== 触发方式分布 ===")
        for trigger, info in distribution['trigger_overview'].items():
            print(f"  {trigger}: {info['workflow_count']} 工作流 ({info['workflow_pct']}%)")
            if info.get('smell_workflow_pct'):
                print("    异味整体流行度（分母为全部扫描工作流）:")
                for stype, overall_pct in info['smell_workflow_pct'].items():
                    print(f"      - {stype}: {info['smell_workflow_counts'][stype]} / {total_workflows} = {overall_pct}%")

    # 打印触发方式组合
    if distribution.get('trigger_combinations'):
        print("\n=== 触发方式组合 (Top 10) ===")
        for item in distribution['trigger_combinations'][:10]:
            print(f"  {item['triggers']}: {item['count']} 工作流")

    # ---- 生成图表 ----
    print("\n生成图表...")
    try:
        # 流行度条形图
        fig, ax = plt.subplots(figsize=(10, 6))
        names = SMELL_TYPES
        wf_pcts = [prevalence['by_smell_type'][s]['workflow_pct'] for s in names]
        repo_pcts = [prevalence['by_smell_type'][s]['repo_pct'] for s in names]
        x = range(len(names))
        width = 0.35
        ax.bar([i - width/2 for i in x], wf_pcts, width, label='Workflow share (%)')
        ax.bar([i + width/2 for i in x], repo_pcts, width, label='Repository share (%)')
        ax.set_xticks(list(x))
        ax.set_xticklabels(names, rotation=30, ha='right')
        ax.set_ylabel('Share (%)')
        ax.set_title('Cache-smell prevalence')
        ax.legend()
        plt.tight_layout()
        fig.savefig(charts_dir / 'smell_prevalence.png')
        plt.close(fig)

        # 按语言分布图 (top 10)
        for stype in SMELL_TYPES:
            lang_data = distribution['by_language'].get(stype, [])
            if not lang_data:
                continue
            top_langs = lang_data[:10]
            fig, ax = plt.subplots(figsize=(10, 6))
            langs = [d['language'] for d in top_langs]
            pcts = [d['pct'] for d in top_langs]
            ax.barh(langs[::-1], pcts[::-1])
            ax.set_xlabel('Affected repositories (%)')
            ax.set_title(f'{stype} - Repository-language distribution (Top 10)')
            plt.tight_layout()
            fig.savefig(charts_dir / f'smell_lang_{stype}.png')
            plt.close(fig)

        # 触发方式分布图
        if distribution.get('trigger_overview'):
            triggers = list(distribution['trigger_overview'].keys())
            wf_counts = [distribution['trigger_overview'][t]['workflow_count'] for t in triggers]
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.bar(triggers, wf_counts)
            ax.set_xlabel('Trigger')
            ax.set_ylabel('Workflow count')
            ax.set_title('Workflow trigger distribution')
            plt.tight_layout()
            fig.savefig(charts_dir / 'trigger_distribution.png')
            plt.close(fig)

        print(f"  图表目录: {charts_dir}")
    except Exception as e:
        print(f"  图表生成失败: {e}")


if __name__ == '__main__':
    main()
