import json
import re
from pathlib import Path
from collections import defaultdict, Counter
from scipy import stats


BASE_DIR = Path('e:/gha_log_analysis')
CI_WORKFLOW_PATTERN = re.compile(r'(?:ci|test|build)', re.IGNORECASE)
REPO_FROM_RUN_ID = re.compile(r'^(.+?)_\.github/workflows/(.+?)_\d+_\d+$')

METRIC_TIERS = {
    'commits': {
        'label': '提交数 (活跃度)',
        'tiers': [
            ('低 (<100)', lambda v: v < 100),
            ('中 (100-1000)', lambda v: 100 <= v < 1000),
            ('高 (1000-5000)', lambda v: 1000 <= v < 5000),
            ('极高 (>=5000)', lambda v: v >= 5000),
        ],
    },
    'contributors': {
        'label': '贡献者数 (活跃度)',
        'tiers': [
            ('低 (<10)', lambda v: v < 10),
            ('中 (10-50)', lambda v: 10 <= v < 50),
            ('高 (50-200)', lambda v: 50 <= v < 200),
            ('极高 (>=200)', lambda v: v >= 200),
        ],
    },
    'stargazers': {
        'label': 'Star数 (社区健康度)',
        'tiers': [
            ('低 (<100)', lambda v: v < 100),
            ('中 (100-1000)', lambda v: 100 <= v < 1000),
            ('高 (1000-5000)', lambda v: 1000 <= v < 5000),
            ('极高 (>=5000)', lambda v: v >= 5000),
        ],
    },
    'forks': {
        'label': 'Fork数 (社区健康度)',
        'tiers': [
            ('低 (<30)', lambda v: v < 30),
            ('中 (30-200)', lambda v: 30 <= v < 200),
            ('高 (200-1000)', lambda v: 200 <= v < 1000),
            ('极高 (>=1000)', lambda v: v >= 1000),
        ],
    },
    'totalIssues': {
        'label': 'Issue总数 (社区健康度)',
        'tiers': [
            ('低 (<50)', lambda v: v < 50),
            ('中 (50-300)', lambda v: 50 <= v < 300),
            ('高 (300-1000)', lambda v: 300 <= v < 1000),
            ('极高 (>=1000)', lambda v: v >= 1000),
        ],
    },
    'totalPullRequests': {
        'label': 'PR总数 (社区活跃度)',
        'tiers': [
            ('低 (<50)', lambda v: v < 50),
            ('中 (50-300)', lambda v: 50 <= v < 300),
            ('高 (300-1000)', lambda v: 300 <= v < 1000),
            ('极高 (>=1000)', lambda v: v >= 1000),
        ],
    },
    'releases': {
        'label': 'Release数 (项目成熟度)',
        'tiers': [
            ('无 (0)', lambda v: v == 0),
            ('少 (1-10)', lambda v: 1 <= v <= 10),
            ('多 (11-50)', lambda v: 11 <= v <= 50),
            ('极多 (>50)', lambda v: v > 50),
        ],
    },
    'codeLines': {
        'label': '代码行数 (项目规模)',
        'tiers': [
            ('小 (<10K)', lambda v: v < 10000),
            ('中 (10K-50K)', lambda v: 10000 <= v < 50000),
            ('大 (50K-200K)', lambda v: 50000 <= v < 200000),
            ('极大 (>=200K)', lambda v: v >= 200000),
        ],
    },
}


def cliffs_delta(x, y):
    import bisect
    nx, ny = len(x), len(y)
    if nx == 0 or ny == 0:
        return None
    sx = sorted(x)
    sy = sorted(y)
    more = 0
    less = 0
    for xi in sx:
        pos = bisect.bisect_left(sy, xi)
        less += pos
        pos_right = bisect.bisect_right(sy, xi)
        more += ny - pos_right
    d = (more - less) / (nx * ny)
    if abs(d) < 0.147:
        magnitude = 'negligible'
    elif abs(d) < 0.33:
        magnitude = 'small'
    elif abs(d) < 0.474:
        magnitude = 'medium'
    else:
        magnitude = 'large'
    return d, magnitude


def format_duration(seconds):
    if seconds is None:
        return 'N/A'
    if seconds < 60:
        return f'{seconds:.1f}s'
    elif seconds < 3600:
        return f'{seconds/60:.1f}m'
    else:
        return f'{seconds/3600:.2f}h'


def load_jsonl(file_path):
    data = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                data.append(json.loads(line))
    return data


def load_json(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def extract_repo_and_workflow(run_id):
    m = REPO_FROM_RUN_ID.match(run_id)
    if m:
        repo = m.group(1).replace('_', '/', 1)
        workflow_file = m.group(2)
        return repo, workflow_file
    return None, None


def is_ci_workflow(workflow_file):
    return bool(CI_WORKFLOW_PATTERN.search(workflow_file))


def calc_tier_distribution(repos, metric_key):
    tier_config = METRIC_TIERS[metric_key]
    total = len(repos)
    dist = {}
    for tier_name, tier_fn in tier_config['tiers']:
        count = sum(1 for r in repos if tier_fn(r[metric_key]))
        dist[tier_name] = {
            'count': count,
            'pct': count / total * 100 if total else 0,
        }
    return dist


def _analyze_cache_repo_characteristics(smell_data, repos_data, run_durations, ci_only=True):
    repos_by_name = {}
    for item in repos_data:
        repo_name = item.get('_id', '')
        if repo_name:
            repos_by_name[repo_name] = item.get('repo', {})

    repo_cache_status = defaultdict(lambda: {'has_cache': False, 'cache_types': set(), 'cache_categories': set()})

    for item in smell_data:
        for hash_key, inner_data in item.items():
            run_ids = inner_data.get('run_ids', [])
            cache_detector = inner_data.get('detectors', {}).get('cache', {})
            use_cache = cache_detector.get('use_cache', False)
            cache_types = cache_detector.get('cache_types', [])
            cache_categories = cache_detector.get('cache_categories', [])

            if ci_only:
                ci_run_found = False
                for run_id in run_ids:
                    repo_name, workflow_file = extract_repo_and_workflow(run_id)
                    if repo_name and workflow_file and is_ci_workflow(workflow_file):
                        ci_run_found = True
                        break
                if not ci_run_found:
                    continue

            for run_id in run_ids:
                repo_name, workflow_file = extract_repo_and_workflow(run_id)
                if not repo_name:
                    continue
                status = repo_cache_status[repo_name]
                if use_cache:
                    status['has_cache'] = True
                    status['cache_types'].update(cache_types)
                    status['cache_categories'].update(cache_categories)

    repos_with_cache = []
    repos_without_cache = []

    for repo_name, status in repo_cache_status.items():
        repo_info = repos_by_name.get(repo_name)
        if not repo_info:
            continue

        entry = {
            'repo': repo_name,
            'mainLanguage': repo_info.get('mainLanguage') or 'Unknown',
            'commits': repo_info.get('commits') or 0,
            'contributors': repo_info.get('contributors') or 0,
            'stargazers': repo_info.get('stargazers') or 0,
            'forks': repo_info.get('forks') or 0,
            'totalIssues': repo_info.get('totalIssues') or 0,
            'openIssues': repo_info.get('openIssues') or 0,
            'totalPullRequests': repo_info.get('totalPullRequests') or 0,
            'releases': repo_info.get('releases') or 0,
            'license': repo_info.get('license') or '',
            'size': repo_info.get('size') or 0,
            'codeLines': repo_info.get('codeLines') or 0,
            'cache_types': list(status['cache_types']),
            'cache_categories': list(status['cache_categories']),
        }

        if status['has_cache']:
            repos_with_cache.append(entry)
        else:
            repos_without_cache.append(entry)

    filter_label = 'only ci/test/build workflows' if ci_only else 'all workflows'
    print(f"\n{'='*70}")
    print(f"Cache 使用与仓库特点分析 ({filter_label})")
    print(f"{'='*70}")
    print(f"\n仓库分类:")
    print(f"  使用缓存的仓库: {len(repos_with_cache)}")
    print(f"  未使用缓存的仓库: {len(repos_without_cache)}")

    # --- 1. mainLanguage ---
    def analyze_language_distribution(repos, label):
        lang_counter = Counter(r['mainLanguage'] for r in repos)
        total = len(repos)
        print(f"\n  {label} mainLanguage 分布 (共 {total} 个仓库):")
        for lang, count in lang_counter.most_common(15):
            pct = count / total * 100 if total else 0
            print(f"    {lang:<25} {count:>4} ({pct:>5.1f}%)")
        return dict(lang_counter)

    print(f"\n{'='*70}")
    print("1. mainLanguage 分布对比")
    print(f"{'='*70}")
    lang_with = analyze_language_distribution(repos_with_cache, "使用缓存")
    lang_without = analyze_language_distribution(repos_without_cache, "未使用缓存")

    all_langs = set(list(lang_with.keys()) + list(lang_without.keys()))
    print(f"\n  语言分布差异 (使用缓存 vs 未使用缓存):")
    print(f"    {'语言':<25} {'使用缓存%':>10} {'未使用缓存%':>12} {'差异':>10}")
    print(f"    {'-'*25} {'-'*10} {'-'*12} {'-'*10}")
    total_with = len(repos_with_cache) or 1
    total_without = len(repos_without_cache) or 1
    for lang in sorted(all_langs, key=lambda l: lang_with.get(l, 0) + lang_without.get(l, 0), reverse=True)[:15]:
        pct_with = lang_with.get(lang, 0) / total_with * 100
        pct_without = lang_without.get(lang, 0) / total_without * 100
        diff = pct_with - pct_without
        print(f"    {lang:<25} {pct_with:>9.1f}% {pct_without:>11.1f}% {diff:>+9.1f}%")

    # --- 2. 活跃度与社区健康度分层对比 ---
    print(f"\n{'='*70}")
    print("2. 活跃度与社区健康度分层对比")
    print(f"{'='*70}")

    tier_results = {}
    for metric_key, tier_config in METRIC_TIERS.items():
        dist_with = calc_tier_distribution(repos_with_cache, metric_key)
        dist_without = calc_tier_distribution(repos_without_cache, metric_key)

        print(f"\n  {tier_config['label']}:")
        print(f"    {'层次':<20} {'使用缓存':>12} {'未使用缓存':>12} {'占比差异':>10}")
        print(f"    {'-'*20} {'-'*12} {'-'*12} {'-'*10}")
        for tier_name, _ in tier_config['tiers']:
            pct_with = dist_with[tier_name]['pct']
            pct_without = dist_without[tier_name]['pct']
            diff = pct_with - pct_without
            cnt_with = dist_with[tier_name]['count']
            cnt_without = dist_without[tier_name]['count']
            print(f"    {tier_name:<20} {cnt_with:>4}({pct_with:>5.1f}%) {cnt_without:>4}({pct_without:>5.1f}%) {diff:>+9.1f}%")

        tier_results[metric_key] = {
            'label': tier_config['label'],
            'with_cache': dist_with,
            'without_cache': dist_without,
        }

    # --- 3. 缓存类型分布 ---
    cache_type_counter = Counter()
    cache_cat_counter = Counter()
    for r in repos_with_cache:
        for ct in r['cache_types']:
            cache_type_counter[ct] += 1
        for cc in r['cache_categories']:
            cache_cat_counter[cc] += 1

    if repos_with_cache:
        pass

    # --- 4. 各语言缓存使用率 ---
    def lang_cache_rate():
        lang_total = Counter()
        lang_cached = Counter()
        for r in repos_with_cache + repos_without_cache:
            lang = r['mainLanguage']
            lang_total[lang] += 1
        for r in repos_with_cache:
            lang = r['mainLanguage']
            lang_cached[lang] += 1
        print(f"\n  {'语言':<25} {'仓库数':>8} {'使用缓存数':>10} {'缓存使用率':>10}")
        print(f"  {'-'*25} {'-'*8} {'-'*10} {'-'*10}")
        for lang, total in lang_total.most_common(15):
            cached = lang_cached.get(lang, 0)
            rate = cached / total * 100 if total else 0
            print(f"  {lang:<25} {total:>8} {cached:>10} {rate:>9.1f}%")

    print(f"\n{'='*70}")
    print("4. 各语言缓存使用率")
    print(f"{'='*70}")
    lang_cache_rate()

    # --- 5. 工作流执行时长与缓存关联分析 ---
    print(f"\n{'='*70}")
    print("5. 工作流执行时长与缓存关联分析 (Wilcoxon Rank-Sum & Cliff's Delta)")
    print(f"{'='*70}")

    durations_file = BASE_DIR / 'output' / 'run_durations.jsonl'
    print("加载 run 时长数据...")
    durations_data = load_jsonl(durations_file)
    run_durations = {r['run_id']: r for r in durations_data}

    if ci_only:
        WF_CATEGORIES = {
            'ci': {'pattern': re.compile(r'\bci\b', re.IGNORECASE), 'label': 'CI 工作流'},
            'test': {'pattern': re.compile(r'\btest\b', re.IGNORECASE), 'label': 'Test 工作流'},
            'build': {'pattern': re.compile(r'\bbuild\b', re.IGNORECASE), 'label': 'Build 工作流'},
        }
    else:
        WF_CATEGORIES = {
            'all': {'pattern': re.compile(r'.', re.IGNORECASE), 'label': '全部工作流'},
        }

    CACHE_CATEGORIES = {
        'explicit_cache': '显式缓存 (actions/cache)',
        'package_manager_cache': '包管理器缓存 (setup-* action)',
        'docker_layer_cache': 'Docker层缓存',
        'other_cache': '第三方缓存',
        'no_cache': '未使用缓存',
    }

    run_category_cache = {}
    for item in smell_data:
        for hash_key, inner_data in item.items():
            run_ids = inner_data.get('run_ids', [])
            cache_detector = inner_data.get('detectors', {}).get('cache', {})
            use_cache = cache_detector.get('use_cache', False)
            cache_categories = cache_detector.get('cache_categories', [])

            for run_id in run_ids:
                _, workflow_file = extract_repo_and_workflow(run_id)
                if not workflow_file:
                    continue
                for cat_key, cat_cfg in WF_CATEGORIES.items():
                    if cat_cfg['pattern'].search(workflow_file):
                        if run_id not in run_category_cache:
                            run_category_cache[run_id] = {'categories': set(), 'cache_categories': set()}
                        run_category_cache[run_id]['categories'].add(cat_key)
                        if not use_cache:
                            run_category_cache[run_id]['cache_categories'].add('no_cache')
                        else:
                            run_category_cache[run_id]['cache_categories'].update(cache_categories)

    def median_val(vals):
        s = sorted(vals)
        n = len(s)
        if n == 0:
            return 0
        if n % 2 == 1:
            return s[n // 2]
        return (s[n // 2 - 1] + s[n // 2]) / 2

    def mean_val(vals):
        return sum(vals) / len(vals) if vals else 0

    def pct_val(vals, p):
        s = sorted(vals)
        n = len(s)
        if n == 0:
            return 0
        idx = int(n * p / 100)
        return s[min(idx, n - 1)]

    def print_multi_group_comparison(label, group_data, baseline_key='no_cache'):
        baseline_vals = group_data.get(baseline_key, [])
        baseline_label = CACHE_CATEGORIES.get(baseline_key, baseline_key)
        print(f"\n  {label}:")
        print(f"    {'分组':<30} {'样本量':>8} {'均值':>12} {'中位数':>12} {'P25':>12} {'P75':>12} {'P90':>12}")
        print(f"    {'-'*30} {'-'*8} {'-'*12} {'-'*12} {'-'*12} {'-'*12} {'-'*12}")
        for gk in CACHE_CATEGORIES:
            vals = group_data.get(gk, [])
            if not vals:
                continue
            print(f"    {CACHE_CATEGORIES[gk]:<30} {len(vals):>8} {format_duration(mean_val(vals)):>12} {format_duration(median_val(vals)):>12} {format_duration(pct_val(vals, 25)):>12} {format_duration(pct_val(vals, 75)):>12} {format_duration(pct_val(vals, 90)):>12}")

        stat_results = {}
        for gk, glabel in CACHE_CATEGORIES.items():
            if gk == baseline_key:
                continue
            vals = group_data.get(gk, [])
            if not vals or not baseline_vals:
                continue
            if len(vals) < 2 or len(baseline_vals) < 2:
                continue
            try:
                stat, p_value = stats.mannwhitneyu(vals, baseline_vals, alternative='two-sided')
                cd_result = cliffs_delta(vals, baseline_vals)
                if cd_result:
                    cd_val, cd_mag = cd_result
                else:
                    cd_val, cd_mag = None, 'N/A'
                sig = '***' if p_value < 0.001 else '**' if p_value < 0.01 else '*' if p_value < 0.05 else 'ns'
                if p_value > 0:
                    p_str = f'{p_value:.2e}'
                else:
                    import math
                    n1, n2 = len(vals), len(baseline_vals)
                    mu = n1 * n2 / 2
                    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
                    z = (stat - mu) / sigma if sigma > 0 else 0
                    log_p = stats.norm.logsf(abs(z)) + math.log(2)
                    log10_p = log_p / math.log(10)
                    p_str = f'10^({log10_p:.1f})'
                print(f"\n    {glabel} vs {baseline_label}:")
                print(f"      Wilcoxon U={stat:.1f}, p={p_str} {sig}")
                print(f"      Cliff's delta={cd_val if cd_val is not None else 'N/A'} ({cd_mag})")
                stat_results[gk] = {
                    'u_stat': round(stat, 2),
                    'p_value': round(p_value, 6) if p_value > 0 else 0,
                    'p_display': p_str,
                    'significance': sig,
                    'cliffs_delta': round(cd_val, 4) if cd_val is not None else None,
                    'effect_size': cd_mag,
                }
            except Exception as e:
                print(f"\n    {glabel} vs {baseline_label}: 统计检验失败 ({e})")
        return stat_results

    duration_analysis_results = {}
    for cat_key, cat_cfg in WF_CATEGORIES.items():
        print(f"\n  {'─'*60}")
        print(f"  {cat_cfg['label']} (文件名含 '{cat_key}')")
        print(f"  {'─'*60}")

        group_wf_dur = defaultdict(list)
        group_job_dur = defaultdict(list)
        group_avg_job_dur = defaultdict(list)
        group_success = defaultdict(lambda: {'total': 0, 'success': 0})

        for run_id, info in run_category_cache.items():
            if cat_key not in info['categories']:
                continue
            rd = run_durations.get(run_id)
            if not rd:
                continue
            wf_dur = rd.get('workflow_duration_sec')
            job_dur = rd.get('jobs_duration_sum_sec')
            nj = rd.get('num_jobs')
            conclusion = rd.get('conclusion', '')
            for cc in info['cache_categories']:
                if wf_dur is not None:
                    group_wf_dur[cc].append(wf_dur)
                if job_dur is not None:
                    group_job_dur[cc].append(job_dur)
                if job_dur is not None and nj is not None and nj > 0:
                    group_avg_job_dur[cc].append(job_dur / nj)
                group_success[cc]['total'] += 1
                if conclusion == 'success':
                    group_success[cc]['success'] += 1

        print(f"  各缓存类别样本量:")
        for cc, clabel in CACHE_CATEGORIES.items():
            print(f"    {clabel:<30} {len(group_wf_dur.get(cc, [])):>6} runs")

        print(f"\n  成功率对比:")
        print(f"    {'分组':<30} {'总数':>8} {'成功':>8} {'成功率':>10}")
        print(f"    {'-'*30} {'-'*8} {'-'*8} {'-'*10}")
        success_results = {}
        for cc, clabel in CACHE_CATEGORIES.items():
            gs = group_success.get(cc)
            if not gs or gs['total'] == 0:
                continue
            rate = gs['success'] / gs['total'] * 100
            print(f"    {clabel:<30} {gs['total']:>8} {gs['success']:>8} {rate:>9.1f}%")
            success_results[cc] = {'total': gs['total'], 'success': gs['success'], 'rate': round(rate, 2)}

        wf_stats = print_multi_group_comparison('工作流总时长 (并行视角)', dict(group_wf_dur))
        job_stats = print_multi_group_comparison('Job时长加和 (串行视角)', dict(group_job_dur))
        avg_job_stats = print_multi_group_comparison('Job平均执行时长', dict(group_avg_job_dur))

        duration_analysis_results[cat_key] = {
            'label': cat_cfg['label'],
            'workflow_duration': wf_stats,
            'jobs_duration_sum': job_stats,
            'avg_job_duration': avg_job_stats,
            'success_rate': success_results,
            'sample_sizes': {cc: len(group_wf_dur.get(cc, [])) for cc in CACHE_CATEGORIES},
        }

    # --- 输出JSON ---
    output_file = BASE_DIR / 'output' / ('cache_repo_characteristics_ci.json' if ci_only else 'cache_repo_characteristics_all.json')
    tier_results_serializable = {}
    for mk, mv in tier_results.items():
        tier_results_serializable[mk] = {
            'label': mv['label'],
            'with_cache': {tn: {'count': td['count'], 'pct': round(td['pct'], 2)} for tn, td in mv['with_cache'].items()},
            'without_cache': {tn: {'count': td['count'], 'pct': round(td['pct'], 2)} for tn, td in mv['without_cache'].items()},
        }

    result = {
        'filter': filter_label,
        'summary': {
            'repos_with_cache': len(repos_with_cache),
            'repos_without_cache': len(repos_without_cache),
        },
        'language_distribution': {
            'with_cache': lang_with,
            'without_cache': lang_without,
        },
        'tier_comparison': tier_results_serializable,
        'cache_type_distribution': dict(cache_type_counter),
        'cache_category_distribution': dict(cache_cat_counter),
        'repos_with_cache': repos_with_cache,
        'repos_without_cache': repos_without_cache,
        'duration_analysis': duration_analysis_results,
    }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def analyze_cache_repo_characteristics():
    smell_file = BASE_DIR / 'output' / 'smell_scan_results.jsonl'
    repos_file = BASE_DIR / 'data' / 'filtered_repositories.json'
    durations_file = BASE_DIR / 'output' / 'run_durations.jsonl'

    print("加载 smell 扫描结果...")
    smell_data = load_jsonl(smell_file)

    print("加载仓库数据...")
    repos_data = load_jsonl(repos_file)

    print("加载 run 时长数据...")
    durations_data = load_jsonl(durations_file)
    run_durations = {r['run_id']: r for r in durations_data}

    print("\n" + "#" * 70)
    print("# 分析 1: 仅 CI/Test/Build 工作流")
    print("#" * 70)
    _analyze_cache_repo_characteristics(smell_data, repos_data, run_durations, ci_only=True)

    print("\n" + "#" * 70)
    print("# 分析 2: 全部工作流")
    print("#" * 70)
    _analyze_cache_repo_characteristics(smell_data, repos_data, run_durations, ci_only=False)


if __name__ == '__main__':
    analyze_cache_repo_characteristics()