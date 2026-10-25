"""Scale workflow-specific historical differences by audited observed calendar-month counts."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / 'observed_monthly_frequency'
SOURCE = ROOT / 'output_rq23_sample_acquisition_20260929'
MONTHS = ('2026-04', '2026-05')


def rows(p):
    with p.open(encoding='utf-8-sig') as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def save(name, x):
    (OUT / name).write_text(json.dumps(x, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def jsonl(name, xs):
    with (OUT / name).open('w', encoding='utf-8') as f:
        for x in xs:
            f.write(json.dumps(x, ensure_ascii=False, allow_nan=False) + '\n')


def table(name, xs):
    xs = list(xs)
    fields = list(dict.fromkeys(k for x in xs for k in x)) or ['status']
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in xs:
            w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in x.items()})


def quantile(xs, q):
    xs = sorted(xs)
    p = (len(xs) - 1) * q
    lo = int(p)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (p - lo)


def main():
    OUT.mkdir(exist_ok=True)
    effects = [r for r in rows(HERE / 'event_resource_differences.jsonl') if r['status'] == 'included']
    old_summary = json.loads((HERE / 'smell_resource_summary.json').read_text(encoding='utf-8'))
    repos, aliases = {}, {}
    for r in rows(ROOT / 'data_26_6_9_canonical_20260609/repositories.jsonl'):
        rid = str(r['repository_id'])
        repos[rid] = r
        for n in [r['canonical_name'], *r.get('repository_aliases', [])]:
            aliases[n.lower()] = rid
    def rid_for(r):
        return str(r.get('repository_id') or aliases.get(r['repository_name'].lower()) or 'name:' + r['repository_name'].lower())
    status = {}
    for r in rows(SOURCE / 'runs_status.jsonl'):
        status[rid_for(r)] = r['status']
    target_repos = {r['repository_id'] for r in effects}
    covered = {rid for rid in target_repos if status.get(rid) == 'ok'}
    targets = {(r['repository_id'], r['workflow_path'], str(r['workflow_id'])) for r in effects}
    counts, repo_counts = Counter(), Counter()
    seen = set()
    duplicates = 0
    h = hashlib.sha256()
    raw_rows = 0
    print('Count actual monthly runs for', len(covered), 'audited repositories', flush=True)
    with (SOURCE / 'workflow_runs.jsonl').open('rb') as f:
        for line in f:
            h.update(line)
            if not line.strip():
                continue
            raw_rows += 1
            r = json.loads(line)
            rid = rid_for(r)
            if rid not in covered or str(r.get('created_at') or '')[:7] not in MONTHS:
                continue
            if r.get('status') != 'completed' or r.get('conclusion') != 'success':
                continue
            if r.get('head_branch') != repos[rid].get('default_branch'):
                continue
            key = rid, int(r['run_id']), int(r.get('run_attempt') or 1)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            month = r['created_at'][:7]
            repo_counts[rid, month] += 1
            wp = str(r.get('workflow_path') or r.get('path') or '').split('@')[0]
            wk = rid, wp, str(r.get('workflow_id'))
            if wk in targets:
                counts[wk, month] += 1
    provenance = json.loads((HERE / 'input_provenance.json').read_text(encoding='utf-8'))
    expected = next(s['sha256'] for s in provenance['run_sources'] if Path(s['file']) == SOURCE / 'workflow_runs.jsonl')
    assert h.hexdigest() == expected, 'Audited full run file changed'
    acquisition = json.loads((SOURCE / 'acquisition_audit.json').read_text(encoding='utf-8'))
    assert raw_rows == acquisition['run_records']
    assert acquisition['runs_outside_study_window'] == 0
    assert acquisition['duplicate_run_records'] == 0 and duplicates == 0
    print('Observed run counting completed', raw_rows, 'rows', flush=True)

    # One workflow may have multiple historical switches: average its per-run
    # contrasts before applying its actual count once, avoiding duplicate frequency.
    groups = defaultdict(list)
    for r in effects:
        groups[r['smell'], r['repository_id'], r['workflow_path'], str(r['workflow_id'])].append(r)
    workflows = []
    monthly = []
    for (smell, rid, wp, wid), rs in sorted(groups.items()):
        vm = statistics.fmean(r['delta_vm_seconds'] for r in rs)
        usd = statistics.fmean(r['delta_reference_cost_usd'] for r in rs)
        base = {'smell': smell, 'scope': rs[0]['scope'], 'repository_id': rid,
                'repository_name': rs[0]['repository_name'], 'workflow_path': wp, 'workflow_id': wid,
                'historical_events': len(rs), 'event_ids': [r['event_id'] for r in rs],
                'mean_event_delta_vm_seconds': vm, 'mean_event_delta_reference_usd': usd,
                'monthly_frequency_available': rid in covered,
                'frequency_missing_reason': None if rid in covered else 'no_audited_full_calendar_month_run_archive'}
        workflows.append(base)
        for month in MONTHS:
            n = counts[(rid, wp, wid), month] if rid in covered else None
            monthly.append({**base, 'month': month, 'observed_successful_runs': n,
                'monthly_reference_cost_difference_usd': usd * n if n is not None else None,
                'monthly_vm_difference_hours': vm * n / 3600 if n is not None else None})
    repo_groups = defaultdict(list)
    for r in monthly:
        if r['monthly_frequency_available']:
            repo_groups[r['smell'], r['repository_id'], r['month']].append(r)
    repo_months = []
    for (smell, rid, month), rs in sorted(repo_groups.items()):
        repo_months.append({'smell': smell, 'scope': rs[0]['scope'], 'repository_id': rid,
            'repository_name': rs[0]['repository_name'], 'month': month,
            'matched_workflows': len(rs),
            'matched_workflows_observed_successful_runs': sum(r['observed_successful_runs'] for r in rs),
            'repository_all_workflows_observed_default_branch_successful_runs': repo_counts[rid, month],
            'monthly_reference_cost_difference_usd': sum(r['monthly_reference_cost_difference_usd'] for r in rs),
            'monthly_vm_difference_hours': sum(r['monthly_vm_difference_hours'] for r in rs)})
    summary = []
    for original in old_summary:
        s = original['smell']
        gs = defaultdict(list)
        for r in repo_months:
            if r['smell'] == s:
                gs[r['repository_id']].append(r)
        repo_averages = [{
            'usd': statistics.fmean(r['monthly_reference_cost_difference_usd'] for r in rs),
            'vm': statistics.fmean(r['monthly_vm_difference_hours'] for r in rs),
            'frequency': statistics.fmean(r['matched_workflows_observed_successful_runs'] for r in rs),
        } for rs in gs.values()]
        usd_interval = vm_interval = None
        if len(repo_averages) > 1:
            rng = random.Random(20261019)
            b_usd, b_vm = [], []
            for _ in range(2000):
                selected = rng.choices(repo_averages, k=len(repo_averages))
                b_usd.append(statistics.fmean(r['usd'] for r in selected))
                b_vm.append(statistics.fmean(r['vm'] for r in selected))
            usd_interval = [quantile(b_usd, .025), quantile(b_usd, .975)]
            vm_interval = [quantile(b_vm, .025), quantile(b_vm, .975)]
        ws = [w for w in workflows if w['smell'] == s and w['monthly_frequency_available']]
        summary.append({'smell': s, 'scope': original['scope'],
            'cost_comparison_repositories': original['repositories'],
            'observed_frequency_repositories': len(repo_averages), 'matched_workflows': len(ws),
            'historical_events': sum(w['historical_events'] for w in ws),
            'observed_months': list(MONTHS),
            'mean_matched_workflow_successful_runs_per_repository_month': statistics.fmean(r['frequency'] for r in repo_averages) if repo_averages else None,
            'mean_monthly_reference_cost_difference_usd_per_repository': statistics.fmean(r['usd'] for r in repo_averages) if repo_averages else None,
            'monthly_reference_cost_interval_lower_usd': usd_interval[0] if usd_interval else None,
            'monthly_reference_cost_interval_upper_usd': usd_interval[1] if usd_interval else None,
            'mean_monthly_vm_difference_hours_per_repository': statistics.fmean(r['vm'] for r in repo_averages) if repo_averages else None,
            'monthly_vm_interval_lower_hours': vm_interval[0] if vm_interval else None,
            'monthly_vm_interval_upper_hours': vm_interval[1] if vm_interval else None,
            'experimental': True, 'frequency_observed': True, 'actual_bill': False})
    jsonl('workflow_effects.jsonl', workflows)
    table('workflow_observed_monthly.csv', monthly)
    table('repository_observed_monthly.csv', repo_months)
    table('smell_observed_monthly_summary.csv', summary)
    save('smell_observed_monthly_summary.json', summary)
    save('frequency_audit.json', {'full_run_source': str(SOURCE / 'workflow_runs.jsonl'), 'sha256': h.hexdigest(),
        'cost_difference_source': str(HERE / 'event_resource_differences.jsonl'),
        'cost_difference_source_sha256': hashlib.sha256((HERE / 'event_resource_differences.jsonl').read_bytes()).hexdigest(),
        'repository_metadata_sha256': hashlib.sha256((ROOT / 'data_26_6_9_canonical_20260609/repositories.jsonl').read_bytes()).hexdigest(),
        'run_status_sha256': hashlib.sha256((SOURCE / 'runs_status.jsonl').read_bytes()).hexdigest(),
        'source_rows': raw_rows, 'covered_calendar_months': list(MONTHS),
        'frequency_scope': 'completed/success on research default branch; all trigger types; observed run_id/run_attempt records',
        'effect_repositories': len(target_repos), 'frequency_covered_repositories': len(covered),
        'frequency_missing_repositories': len(target_repos - covered),
        'covered_effect_events': len({eid for w in workflows if w['monthly_frequency_available'] for eid in w['event_ids']}),
        'frequency_target_duplicates': duplicates, 'api_calls': 0,
        'hypothetical_frequency_used': False, 'source': 'audited complete repository run acquisition, not job sample sizes'})
    lines = ['# 实验性：按对应工作流真实月运行频率换算', '',
        '**频率来自2026年4月、5月两个完整自然月的实际运行档案，不使用假定的100/300/1000次，也不使用采样job数作为频率。**', '',
        '## 方法', '',
        '1. 对同仓库、同workflow_id/路径、同异味的多个历史事件，先平均每成功运行差额；每条工作流的频率只乘一次。',
        '2. 对应工作流月参考美元差 = 该工作流历史每成功运行美元差 × 该工作流在该自然月的实际默认分支成功运行次数。所有触发类型均计入，单位与成本端点一致。',
        '3. 同仓库同类型异味涉及的不同工作流在月内求和，再对4月和5月平均，最后仓库等权汇总。并非把原全样本总体均值乘某个总体平均频率。',
        '4. 全仓库其他工作流的次数另列作参考，不能乘到单条工作流上；当前估计只覆盖已有成本对比的工作流，不代表整个仓库全部工作流的异味损失。',
        f'5. 原成本比较的{len(target_repos)}个仓库中，{len(covered)}个有经审计的完整运行档案；其他仓库仅有局部/候选运行记录，频率和月金额为NA。原166个历史事件不因本频率补充而被改动。完整档案中某工作流月运行数确为0时可计0，不能把缺失当0。',
        '6. 这叫“观测频率下的月度参考差额”，不是观测到的实际月损失。每运行差额可能来自更早历史事件，将其应用于4、5月真实频率仍假定差额在该负载频率下可迁移，不重建当月所有版本的实际因果负担。',
        '7. 金额沿用原历史参考价格，单位是所覆盖仓库中已配对工作流合计的月均差额。少仓库、任务/配置同变、成功条件选择和时间区间归属等原限制仍在，不同异味不可相加。',
        '8. 区间为2,000次仓库聚类bootstrap的逐项区间，单仓库不报总体区间；频率按所观察月份固定，未来月份频率不确定性不包含在内。档案按run_id/run_attempt计数，不补造API未列出的历史重试。', '',
        '月度表的频率覆盖子集和工作流汇总权重与原全样本每运行表不同，方向或大小改变不能仅归因于单位换算。原每运行结果保持原样；不以缺失频率填零来保留原表规模。', '',
        '## 原十一类结果', '',
        '| 异味 | 有频率仓库/原成本仓库 | 已配对工作流数 | 实际成功次数/月/仓库（所覆盖工作流合计） | 参考美元差/月/仓库 | VM小时差/月/仓库 | 美元逐项95%区间 |',
        '|---|---:|---:|---:|---:|---:|---|']
    def fmt(x):
        return 'NA' if x is None else f'{x:+.2f}'
    for r in summary:
        if r['scope'] != 'original_11':
            continue
        ci = '—' if r['monthly_reference_cost_interval_lower_usd'] is None else f"[{fmt(r['monthly_reference_cost_interval_lower_usd'])}, {fmt(r['monthly_reference_cost_interval_upper_usd'])}]"
        lines.append(f"| {r['smell']} | {r['observed_frequency_repositories']}/{r['cost_comparison_repositories']} | {r['matched_workflows']} | {fmt(r['mean_matched_workflow_successful_runs_per_repository_month'])} | {fmt(r['mean_monthly_reference_cost_difference_usd_per_repository'])} | {fmt(r['mean_monthly_vm_difference_hours_per_repository'])} | {ci} |")
    lines += ['', '逐仓库、逐月实测次数和金额见 repository_observed_monthly.csv；逐工作流的原差额、次数及缺失状态见 workflow_observed_monthly.csv。', '',
              '离线复算：在上一级实验目录执行 `python -B calculate_observed_monthly.py`。未修改原每运行结果和冻结输入；本次无API调用。']
    (OUT / 'method_report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'covered_repositories': len(covered), 'missing_repositories': len(target_repos - covered),
                      'covered_events': len({eid for w in workflows if w['monthly_frequency_available'] for eid in w['event_ids']}),
                      'workflow_month_rows': len(monthly), 'repository_month_rows': len(repo_months)}, ensure_ascii=False))
    for r in summary:
        if r['scope'] == 'original_11':
            print(json.dumps(r, ensure_ascii=False))


if __name__ == '__main__':
    main()
