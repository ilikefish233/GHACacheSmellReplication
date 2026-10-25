"""Direct before/after comparison for every current-detector smell switch."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
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
PRIOR = ROOT / 'output_experimental_rq23_history_current_detector_20261019'
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('historical_experiment_helpers', PRIOR / 'run_experiment.py')
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)
base.HERE = HERE
from src.detectors.smell.cache_smell_detector import CacheSmellDetector

FORMAL = tuple(base.FORMAL)
SMELLS = FORMAL + tuple(sorted({base.norm(s) for s in CacheSmellDetector.SMELL_TYPES} - set(FORMAL)))
END = base.END
rows, dump, jout, csvout, sha, dt = base.rows, base.dump, base.jout, base.csvout, base.sha, base.dt


def prepare():
    if (HERE / 'frozen_run_manifest.jsonl').exists():
        raise RuntimeError('Already frozen; use --stage calculate')
    source_hashes = json.loads((PRIOR / 'detector_source_hashes.json').read_text(encoding='utf-8'))
    if base.fingerprint() != source_hashes:
        raise RuntimeError('Current detector differs from the cached scan; rescan instead of mixing definitions')
    byname, repos = base.aliases()
    events = list(rows(PRIOR / 'candidate_transitions.jsonl'))
    # Raw detector switches are the only configuration criterion.
    for e in events:
        old = {m['smell']: m for m in e['members']}
        e['members'] = []
        for s in e['changed_detector_types']:
            present = 'after' if s in e['after_all_detector_types'] else 'before'
            previous = old.get(s, {})
            e['members'].append({'event_id': e['event_id'], 'smell': s,
                'scope': 'original_11' if s in FORMAL else 'additional_detector_type',
                'direction': 'introduced' if present == 'after' else 'removed',
                'present_side': present, 'absent_side': 'before' if present == 'after' else 'after',
                'other_smells_changed': sorted(set(e['changed_detector_types']) - {s}),
                'previous_control_eligible': previous.get('control_eligible'),
                'previous_control_exclusion_reason': previous.get('exclusion_reason'),
                'comparison_label': 'detected_vs_not_detected'})
    dump('candidate_transitions.jsonl', events)
    targets = {(e['repository_id'], e['workflow_path']) for e in events}
    jobindex = {tuple(x['run_key']): x for x in rows(PRIOR / 'job_archive_index.jsonl')}
    paths = [ROOT / 'output_full_logical_20260915_complete/rq2_input_merged_round4/workflow_runs.jsonl',
             ROOT / 'data_26_6_9_complete_20260609/workflow_runs.jsonl']
    for b in base.BATCHES:
        paths += [ROOT / b / n for n in ('workflow_runs.jsonl', 'candidate_workflow_runs.jsonl', 'archived_candidate_runs.jsonl', 'live_candidate_runs.jsonl') if (ROOT / b / n).exists()]
    runmap, conflicts, audits = {}, set(), []
    for p in paths:
        raw = added = 0
        for r in rows(p):
            raw += 1
            rid = base.rid_for(r, byname)
            wp = str(r.get('workflow_path') or r.get('path') or '').split('@')[0]
            if (rid, wp) not in targets or r.get('status') != 'completed' or r.get('conclusion') != 'success':
                continue
            branch = repos.get(rid, {}).get('default_branch')
            if not branch or r.get('head_branch') != branch:
                continue
            if not r.get('created_at') or r['created_at'] >= END or not r.get('workflow_id'):
                continue
            key = base.runkey(r, byname)
            val = {k: r.get(k) for k in ('run_id', 'run_attempt', 'created_at', 'run_started_at', 'event', 'head_branch', 'head_sha', 'status', 'conclusion')}
            val.update(repository_id=rid, repository_name=repos.get(rid, {}).get('canonical_name', r['repository_name']).lower(),
                       workflow_path=wp, workflow_id=str(r['workflow_id']), run_attempt=key[2], run_source=str(p))
            if key in runmap:
                if any(runmap[key].get(k) != val.get(k) for k in ('workflow_id', 'workflow_path', 'created_at', 'head_sha', 'head_branch', 'event')):
                    conflicts.add(key)
            else:
                runmap[key] = val
                added += 1
        audits.append({'file': str(p), 'sha256': sha(p), 'raw_rows': raw, 'new_target_success_runs': added})
        print('Read', p.parent.name, raw, 'new', added, flush=True)
    for key in conflicts:
        runmap.pop(key, None)
    refs = {(r['repository_id'], r['workflow_path'], r.get('head_sha')) for k, r in runmap.items() if k in jobindex}
    evidence, ref_conflicts = {}, set()
    for p, kind in [(PRIOR / 'historical_exact_content_index.jsonl', 'history_commit_yaml'),
                    (PRIOR / 'execution_content_evidence.jsonl', 'previous_exact_head_yaml')]:
        for x in rows(p):
            key = x['repository_id'], x['workflow_path'], x.get('head_sha') or x.get('commit_sha')
            if key not in refs:
                continue
            digest = x['content_sha256']
            if key in evidence and evidence[key]['content_sha256'] != digest:
                ref_conflicts.add(key)
            evidence.setdefault(key, {'content_sha256': digest, 'evidence': kind})
    for k in ref_conflicts:
        evidence.pop(k, None)
    byworkflow = defaultdict(list)
    for key, r in runmap.items():
        byworkflow[(r['repository_id'], r['workflow_path'])].append((key, r))
    frozen, selected, screening = {}, [], []
    for e in events:
        sides, counts = {'before': [], 'after': []}, Counter()
        for key, r in byworkflow[(e['repository_id'], e['workflow_path'])]:
            for side, lo, hi in [('before', e['before_start'], e['change_time']), ('after', e['change_time'], e['after_end'])]:
                if not lo <= r['created_at'] < hi:
                    continue
                if r['run_attempt'] > 1 and not lo <= (r.get('run_started_at') or r['created_at']) < hi:
                    counts[side + '_retry_outside_interval'] += 1
                    continue
                counts[side + '_success_runs'] += 1
                if key not in jobindex:
                    counts[side + '_missing_local_exact_jobs'] += 1
                    continue
                proof = evidence.get((r['repository_id'], r['workflow_path'], r.get('head_sha')))
                # For PR/other events, head content is not proof of executed merge/default YAML.
                proof_applicable = r['event'] in {'push', 'schedule', 'workflow_dispatch'}
                if proof and proof_applicable and proof['content_sha256'] != e[side + '_content_sha256']:
                    counts[side + '_known_actual_content_mismatch'] += 1
                    continue
                attribution = 'exact_head_content_confirmed' if proof and proof_applicable else 'history_interval_only'
                sides[side].append((key, {**r, 'version_attribution': attribution}, proof))
        common = {r['workflow_id'] for _, r, _ in sides['before']} & {r['workflow_id'] for _, r, _ in sides['after']}
        sr = {'event_id': e['event_id'], 'counts': dict(counts), 'status': 'excluded_missing_two_sided_resource_data'}
        if common:
            wid = min(common, key=lambda w: (-min(sum(r['workflow_id'] == w for _, r, _ in sides[s]) for s in sides), w))
            e['workflow_id'] = wid
            e['selected_counts'] = {}
            for side, rs in sides.items():
                chosen = sorted((x for x in rs if x[1]['workflow_id'] == wid), key=lambda x: (x[1]['created_at'], x[0]))
                e['selected_counts'][side] = len(chosen)
                for key, r, proof in chosen:
                    entry = frozen.setdefault(key, {**r, 'job_archive': jobindex[key], 'memberships': []})
                    entry['memberships'].append({'event_id': e['event_id'], 'side': side,
                        'interval_start': e['before_start'] if side == 'before' else e['change_time'],
                        'interval_end': e['change_time'] if side == 'before' else e['after_end'],
                        'expected_content_sha256': e[side + '_content_sha256'],
                        'version_attribution': r['version_attribution'], 'available_exact_evidence': proof,
                        'distance_days': abs((dt(r['created_at']) - dt(e['change_time'])).total_seconds()) / 86400})
            selected.append(e)
            sr.update(status='frozen', selected_counts=e['selected_counts'])
        screening.append(sr)
    dump('run_screening.jsonl', screening)
    dump('frozen_events.jsonl', selected)
    dump('frozen_run_manifest.jsonl', (v for _, v in sorted(frozen.items())))
    provenance = {'prior_experiment': str(PRIOR), 'reused_source_hashes': source_hashes,
                  'source_files': {str(PRIOR / n): sha(PRIOR / n) for n in (
                      'candidate_transitions.jsonl', 'job_archive_index.jsonl', 'job_archive_audit.json',
                      'historical_exact_content_index.jsonl', 'execution_content_evidence.jsonl')},
                  'run_sources': audits, 'conflicting_run_keys': len(conflicts), 'conflicting_exact_refs': len(ref_conflicts)}
    jout('input_provenance.json', provenance)
    jout('freeze_audit.json', {'experimental': True, 'selection_before_resource_computation': True,
          'candidate_events': len(events), 'frozen_events': len(selected), 'frozen_runs': len(frozen),
          'smell_types': list(SMELLS),
          'sha256': {n: sha(HERE / n) for n in ('analysis_plan.md', 'candidate_transitions.jsonl', 'frozen_events.jsonl', 'frozen_run_manifest.jsonl')}})
    print('Frozen', len(selected), 'events', len(frozen), 'runs', flush=True)
    calculate()


def calculate():
    legacy = base.import_file('relaxed_resource_math', base.OLD / 'recompute.py')
    freeze = json.loads((HERE / 'freeze_audit.json').read_text(encoding='utf-8'))
    for n, h in freeze['sha256'].items():
        if sha(HERE / n) != h:
            raise RuntimeError('Frozen input changed: ' + n)
    for p in json.loads((PRIOR / 'job_archive_audit.json').read_text(encoding='utf-8'))['sources']:
        if sha(p['file']) != p['sha256']:
            raise RuntimeError('Original job data changed: ' + p['file'])
    byevent = defaultdict(lambda: defaultdict(list))
    runs = []
    for r in rows(HERE / 'frozen_run_manifest.jsonl'):
        ref = r['job_archive']
        with Path(ref['source_file']).open('rb') as f:
            f.seek(ref['byte_offset'])
            meta = json.loads(f.readline().decode('utf-8-sig'))
        if str(meta['run_id']) != str(r['run_id']) or int(meta.get('run_attempt') or 1) != r['run_attempt']:
            raise RuntimeError('Original job offset no longer matches run key')
        cr = legacy.cost(meta['jobs'])
        reasons = list(cr['reasons'])
        if cr.get('allocated_jobs', 0) == 0:
            reasons.append('success_without_allocated_runner')
        result = {**r, 'resource_complete': not reasons, 'resource_exclusion_reasons': reasons,
                  'vm_seconds': cr['vm_seconds'] if not reasons else None,
                  'reference_cost_usd': cr['reference_cost_usd'] if not reasons else None}
        runs.append(result)
        if not reasons:
            for m in r['memberships']:
                byevent[m['event_id']][m['side']].append({**result, **m})
    results, statuses = [], []
    for e in rows(HERE / 'frozen_events.jsonl'):
        ss = byevent[e['event_id']]
        ok = bool(ss['before'] and ss['after'])
        b = {k: e.get(k) for k in ('event_id', 'repository_id', 'repository_name', 'workflow_path', 'workflow_id',
             'before_commit_sha', 'after_commit_sha', 'change_time', 'before_content_sha256', 'after_content_sha256',
             'task_comparable', 'task_exclusion_reasons', 'action_or_tool_version_changed', 'matrix_values_changed',
             'dependency_installation_commands_changed', 'changed_detector_types')}
        for side in ('before', 'after'):
            rs = ss[side]
            b[side + '_runs'] = len(rs)
            b[side + '_mean_vm_seconds'] = statistics.fmean(r['vm_seconds'] for r in rs) if rs else None
            b[side + '_mean_reference_cost_usd'] = statistics.fmean(r['reference_cost_usd'] for r in rs) if rs else None
            b[side + '_events'] = dict(Counter(str(r['event']) for r in rs))
            b[side + '_attribution'] = dict(Counter(r['version_attribution'] for r in rs))
            b[side + '_max_distance_days'] = max((r['distance_days'] for r in rs), default=None)
            b[side + '_run_keys'] = [[r['repository_id'], r['run_id'], r['run_attempt']] for r in rs]
        b['single_observation_side'] = any(len(ss[s]) == 1 for s in ('before', 'after'))
        b['trigger_counts_changed'] = b['before_events'] != b['after_events']
        b['status'] = 'included' if ok else 'excluded_missing_complete_resource_side'
        statuses.append(b)
        for m in e['members']:
            p, a = m['present_side'], m['absent_side']
            results.append({**b, **m,
                'delta_vm_seconds': b[p + '_mean_vm_seconds'] - b[a + '_mean_vm_seconds'] if ok else None,
                'delta_reference_cost_usd': b[p + '_mean_reference_cost_usd'] - b[a + '_mean_reference_cost_usd'] if ok else None})
    valid = [r for r in results if r['status'] == 'included']
    summary = []
    for s in freeze.get('smell_types', SMELLS):
        es = [r for r in valid if r['smell'] == s]
        summary.append({'smell': s, 'scope': 'original_11' if s in FORMAL else 'additional_detector_type',
            **legacy.aggregate(es, random.Random(20261019)),
            'single_observation_events': sum(r['single_observation_side'] for r in es),
            'other_smell_change_events': sum(bool(r['other_smells_changed']) for r in es),
            'task_changed_or_unconfirmed_events': sum(not r['task_comparable'] for r in es),
            'previously_ineligible_or_ambiguous_control_events': sum(r['previous_control_eligible'] is False for r in es),
            'experimental': True, 'causal_effect': False})
    dump('resource_run_records.jsonl', runs)
    dump('event_status.jsonl', statuses)
    dump('event_resource_differences.jsonl', results)
    csvout('event_resource_differences.csv', results)
    jout('smell_resource_summary.json', summary)
    csvout('smell_resource_summary.csv', summary)
    csvout('original_11_smell_resource_summary.csv', (r for r in summary if r['scope'] == 'original_11'))
    used = {tuple(k) for e in statuses if e['status'] == 'included' for side in ('before', 'after') for k in e[side + '_run_keys']}
    audit = {'experimental': True, 'final_events': sum(e['status'] == 'included' for e in statuses),
             'final_repositories': len({e['repository_id'] for e in statuses if e['status'] == 'included'}),
             'unique_success_runs': len(used), 'smell_memberships': len(valid),
             'covered_original_smells': sum(r['scope'] == 'original_11' and r['events'] > 0 for r in summary),
             'covered_additional_smells': sum(r['scope'] != 'original_11' and r['events'] > 0 for r in summary),
             'single_observation_events': sum(e['single_observation_side'] for e in statuses if e['status'] == 'included'),
             'task_changed_or_unconfirmed_events': sum(not e['task_comparable'] for e in statuses if e['status'] == 'included'),
             'version_attribution_memberships': dict(Counter(m['version_attribution'] for r in runs if r['resource_complete'] for m in r['memberships'])),
             'resource_exclusions': dict(Counter(x for r in runs for x in r['resource_exclusion_reasons'])),
             'api_calls': 0}
    jout('result_audit.json', audit)
    necessary_checks(runs, results, summary)
    write_report(audit, summary)
    print(json.dumps(audit, ensure_ascii=False), flush=True)


def necessary_checks(runs, results, summary):
    keys = [(r['repository_id'], r['run_id'], r['run_attempt']) for r in runs]
    assert len(keys) == len(set(keys))
    checks = Counter(unique_run_keys=len(keys))
    for r in runs:
        assert r['status'] == 'completed' and r['conclusion'] == 'success'
        for m in r['memberships']:
            assert m['interval_start'] <= r['created_at'] < m['interval_end']
            checks['version_interval_memberships'] += 1
    for r in results:
        if r['status'] != 'included':
            continue
        p, a = r['present_side'], r['absent_side']
        for field in ('vm_seconds', 'reference_cost_usd'):
            assert math.isclose(r['delta_' + field], r[p + '_mean_' + field] - r[a + '_mean_' + field], abs_tol=1e-8)
        checks['difference_sign_and_math'] += 1
    for r in summary:
        es = [x for x in results if x['smell'] == r['smell'] and x['status'] == 'included']
        groups = defaultdict(list)
        for x in es:
            groups[x['repository_id']].append(x)
        for field in ('delta_vm_seconds', 'delta_reference_cost_usd'):
            v = statistics.fmean(statistics.fmean(x[field] for x in g) for g in groups.values()) if groups else None
            assert v is None and r[field] is None or v is not None and math.isclose(v, r[field], abs_tol=1e-8)
            checks['repository_aggregate'] += 1
    jout('necessary_checks.json', {'status': 'passed', 'checks': dict(checks), 'no_optional_code_quality_tests': True})


def write_report(audit, summary):
    candidates = list(rows(HERE / 'candidate_transitions.jsonl'))
    screen = list(rows(HERE / 'run_screening.jsonl'))
    counts = Counter()
    for r in screen:
        counts.update(r['counts'])
    table = ['| 异味 | 事件/仓库 | ΔVM秒/成功运行 | Δ参考美元/成功运行 | 成本逐项95%区间 |', '|---|---:|---:|---:|---|']
    for r in summary:
        v = 'NA' if r['delta_vm_seconds'] is None else f"{r['delta_vm_seconds']:+.2f}"
        c = 'NA' if r['delta_reference_cost_usd'] is None else f"{r['delta_reference_cost_usd']:+.5f}"
        ci = '—' if r['cost_interval_lower_usd'] is None else f"[{r['cost_interval_lower_usd']:+.5f}, {r['cost_interval_upper_usd']:+.5f}]"
        table.append(f"| {r['smell']} | {r['events']}/{r['repositories']} | {v} | {c} | {ci} |")
    flow = [{'stage': 'all_detector_switch_events', 'events': len(candidates)},
            {'stage': 'with_success_runs_both_sides', 'events': sum(bool(r['counts'].get('before_success_runs')) and bool(r['counts'].get('after_success_runs')) for r in screen)},
            {'stage': 'frozen_with_local_exact_jobs', 'events': sum(r['status'] == 'frozen' for r in screen)},
            {'stage': 'final_complete_resources', 'events': audit['final_events']}]
    csvout('sample_flow.csv', flow)
    jout('run_coverage_counts.json', dict(counts))
    report = f'''# EXPERIMENTAL：只按异味检测状态变化纳入的历史前后资源差额

2026-10-19。用户要求放宽配置匹配；旧实验、检测器、正式数据和论文未修改，无 RQ3。

## 结果与样本

本轮复用同一当前检测器历史扫描，不再限制任务、job、runner、矩阵、工具版本、触发事件一致性或对照适用性；每侧全部本地资源完整成功运行均可用，不限十次，不限30/90天。保留同工作流身份、历史有效区间和可计算资源这些基础条件。

候选 {len(candidates)} 个；最终 **{audit['final_events']} 个事件、{audit['final_repositories']} 个仓库、{audit['unique_success_runs']} 次成功运行**，覆盖原十一类中的 {audit['covered_original_smells']} 类及额外检测器类型 {audit['covered_additional_smells']} 类。逐类成员关系 {audit['smell_memberships']} 条，不可相加为独立事件。

对照只表示“未检出该异味”。原本适用性不足、自动缓存未知等状态被保留为标记而非排除条件，不表示已经证明没有异味或修复成功。{audit['task_changed_or_unconfirmed_events']} 个最终事件主要任务改变或可比性无法确认；{audit['single_observation_events']} 个事件至少一侧只有一次观测。

## 差额

方向固定为“检出侧 − 未检出侧”。事件内比较两侧成功运行均值；仓库内平均事件，再仓库等权；2,000 次仓库 bootstrap、种子20261019、逐项95%区间，单仓库不报告总体区间。不做同时检验确认，不将各类型相加。

{chr(10).join(table)}

## 版本与成本口径

运行以同一默认分支文件历史的有效时间区间归属，不再把缺少精确 head-SHA 证据当作排除条件。已有明确、适用于该事件类型的内容证据若与目标历史内容冲突，作为版本归属错误排除；其他事件类型不把 head 当作 merge 执行版本证明。最终运行/事件侧的版本证据计数：`{json.dumps(audit['version_attribution_memberships'], ensure_ascii=False)}`。同一运行可位于相邻事件两侧，计数不是独立运行数。

VM 为各已分配 runner 的 job 秒数之和；参考美元沿用 `0.008 * ceil(seconds / 60 * OS倍率)`，Linux/Windows/macOS=1/2/10，逐job计算。是历史统一标准金额，不是当前报价、实际账单或修复节省额；未知价格类别、缺失job/时间和空档案不能按零处理。不含存储，不还原更大runner/ARM特殊价格。资源排除：`{json.dumps(audit['resource_exclusions'], ensure_ascii=False)}`。

## 数据覆盖和解释

API调用0、工作流重放0；仅复用现有本地job，旧采集受历史候选和资源样本选择影响，不代表完整历史或随机总体。没有前后运行/资源的候选无法通过放宽配置规则补出数值；原因计数在 run_coverage_counts.json，逐事件在 run_screening.jsonl。

本轮估计的是**检测状态变化事件伴随的成功运行成本差额**。任务、OS、矩阵、依赖、工具及其他异味均可能同变，加上时间区间归属和未知状态，不能把全部差额归因于目标异味。正负结果完整报告；NA不是零影响。此前更严格实验不被覆盖，本轮不声称仅由放宽某一个条件造成样本或差额变化。

## 复算与核对

冻结候选和运行在计算VM/美元前保存，哈希见 freeze_audit.json。必要身份、区间、去重、符号和仓库汇总核对通过，见 necessary_checks.json；没有额外代码质量测试。

首次执行命令：`python -B {HERE / 'run_relaxed.py'} --stage prepare`。已完成目录不能重新选样；离线复算命令：`python -B {HERE / 'run_relaxed.py'} --stage calculate`。复用的输入位置、源码哈希和原始档案偏移均保留，未复制大量YAML。
'''
    (HERE / 'method_report.md').write_text(report, encoding='utf-8')
    (HERE / 'EXPERIMENTAL_README.md').write_text('# 实验性结果 / EXPERIMENTAL\n\n按用户指定的检测状态变化直接比较；无配置匹配门槛。详见 method_report.md 和 analysis_plan.md。\n\n不能解释为单一异味的净因果成本；旧实验及正式结果未覆盖。\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', choices=['prepare', 'calculate'], required=True)
    args = parser.parse_args()
    if args.stage == 'prepare':
        prepare()
    else:
        calculate()
