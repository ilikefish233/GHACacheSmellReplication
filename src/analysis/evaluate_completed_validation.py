"""Evaluate the eleven formal cache smells without changing the workbook/detector.

Case-level binary metrics use one workflow per type. Job metrics are secondary:
only labels that can be resolved to YAML job IDs/names or step names are used.
Unknown labels are excluded for that type, never silently treated as negatives.
"""

from src.smell_names import loads_smell_json

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import openpyxl
import yaml
from validation_labels import (
    build_manual_labels, has_parenthesized_annotation, is_not_adopted,
    normalize_detected_smell_type, parse_manual_jobs,
)
from src.detectors.smell.cache_smell_detector import CacheSmellDetector
from src.parsers.workflow_parser import WorkflowParser

TYPES = (
    'no_dependency_cache', 'no_docker_layer_cache', 'deprecated_cache_action',
    'no_fallback_cache', 'static_cache_key', 'overly_specific_cache_key',
    'cache_as_artifact', 'cache_position_error', 'unreachable_cache_write',
    'redundant_cache_update', 'overlapping_cache_mechanisms',
)
COLS = dict(zip(TYPES, (7, 18, 20, 8, 19, 22, 21, 10, None, 9, 17)))
NEGATIVE = {'', 'n/a', 'na', 'none', '-', '0', '无'}
TYPE_ALIASES = {
    'disposable_cache_key': 'overly_specific_cache_key',
    'conflicting_cache_sources': 'overlapping_cache_mechanisms',
}


def canonical_smell_type(value):
    normalized = normalize_detected_smell_type(value)
    return TYPE_ALIASES.get(normalized, normalized)


def cache_artifact_participants(smell, job_ids):
    """Count both endpoints of an explicitly reported transfer, once per job.

    Only exact YAML job IDs from the finding are accepted. Do not infer missing
    endpoints from needs, annotations, display names, or unrelated findings.
    """
    details = smell.get('details') or {}
    participants = set()
    for value in (smell.get('job_name'), details.get('producer_job'),
                  details.get('consumer_job')):
        if not isinstance(value, str):
            continue
        names = [value] if value in job_ids else parse_manual_jobs(value)
        participants.update(str(name) for name in names if str(name) in job_ids)
    return participants


def present(value):
    return str(value or '').strip().lower() not in NEGATIVE


def split_jobs_outside_parentheses(value):
    text = str(value or '')
    parts, start, depth = [], 0, 0
    for i, char in enumerate(text):
        if char in '(（':
            depth += 1
        elif char in ')）':
            depth = max(0, depth - 1)
        elif char in ',;，；' and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [part for part in parts if present(part)]


def resolve_jobs(text, jobs):
    """Match exact delimited identifiers, display names, or cache-step names.

    This reads the annotation and YAML only, never detector predictions.
    """
    found = set()
    # Compact complete job lists remain readable in the validation workbook.
    for left, right in re.findall(r'([\w-]+\d+)\.\.([\w-]+\d+)', text):
        first = re.fullmatch(r'(.*?)(\d+)', left)
        last = re.fullmatch(r'(.*?)(\d+)', right)
        if first.group(1) == last.group(1):
            start, stop = int(first.group(2)), int(last.group(2))
            for job_id in jobs:
                match = re.fullmatch(re.escape(first.group(1)) + r'(\d+)', str(job_id))
                if match and start <= int(match.group(1)) <= stop:
                    found.add(str(job_id))
    for job_id, body in jobs.items():
        aliases = [str(job_id)]
        if isinstance(body, dict):
            aliases += [str(body.get('name') or '')]
            aliases += [str(s.get('name') or '') for s in body.get('steps', [])
                        if isinstance(s, dict)]
        for alias in aliases:
            if alias and re.search(r'(?<![\w.-])' + re.escape(alias) + r'(?![\w.-])', text):
                found.add(str(job_id))
                break
    return found


def reference(ws, row, stype, jobs, version_mode, code99):
    ignored = set()
    raw = ws.cell(row, COLS[stype]).value if COLS[stype] else None
    if stype == 'unreachable_cache_write':
        labels, _, _ = build_manual_labels(ws, row)
        raw = ';'.join(labels[stype])
    if stype == 'overly_specific_cache_key':
        raw = ';'.join(str(ws.cell(row, c).value or '') for c in (22, 29)
                       if present(ws.cell(row, c).value))
    text = str(raw or '').strip()
    if re.match(r'^待扩展(?:\s|[（(:：]|$)', text):
        return None, None, ignored, text, 'pending_detector_scope_expansion'
    if re.search(r'未判定|不确定|无法判|待确认', text):
        return None, None, ignored, text, 'uncertain_annotation'
    if text == '99':
        if code99 == 'unknown':
            return None, None, ignored, text, 'code99'
        return code99 == 'positive', (None if code99 == 'positive' else set()), ignored, text, ''
    if stype == 'no_dependency_cache':
        included = []
        for job in split_jobs_outside_parentheses(raw):
            if has_parenthesized_annotation(job):
                base = re.sub(r'[（(].*?[）)]', '', job).strip()
                ignored.update(resolve_jobs(base, jobs))
                if base in jobs:
                    ignored.add(base)
            else:
                included.append(job)
        if not included and present(raw):
            return None, None, ignored, text, 'parenthesized_ndc_only'
        text = ';'.join(included)
    if not present(text):
        return False, set(), ignored, str(raw or ''), ''
    if stype == 'deprecated_cache_action' and version_mode == 'inventory':
        refs = re.findall(r'actions/cache(?:/(?:restore|save))?@([\w.-]+)', text)
        if refs:
            old = [ref for ref in refs if re.fullmatch(r'v?[12](?:[._-].*)?', ref, re.I)]
            # SHA annotations alone do not establish an independently labelled version.
            if not old and any(re.fullmatch(r'[0-9a-f]{40}', ref, re.I) for ref in refs):
                return None, None, ignored, text, 'unresolved_action_sha'
            matched = set()
            for job_id, body in jobs.items():
                if not isinstance(body, dict):
                    continue
                for step in body.get('steps', []):
                    action = str(step.get('uses') or '') if isinstance(step, dict) else ''
                    if action.startswith('actions/cache') and '@' in action and action.split('@', 1)[1] in old:
                        matched.add(str(job_id))
            return bool(old), matched if matched or not old else None, ignored, text, ''
    resolved = resolve_jobs(text, jobs)
    # A bare positive flag or explanatory paragraph has a case label, but does
    # not establish a complete, independent list of positive jobs.
    job_labels = resolved if resolved else None
    return True, job_labels, ignored, text, ''


def metrics(counts):
    tp, fp, fn, tn = (counts.get(k, 0) for k in ('tp', 'fp', 'fn', 'tn'))
    divide = lambda a, b: a / b if b else None
    return dict(counts, precision=divide(tp, tp + fp), recall=divide(tp, tp + fn),
                f1=divide(2 * tp, 2 * tp + fp + fn),
                accuracy=divide(tp + tn, tp + fp + fn + tn))


def load_case_yaml(ws, row, sources):
    """Use an explicitly recorded snapshot; reject stale provenance instead of
    silently evaluating a truncated cell or a different workflow version.
    """
    embedded = str(ws.cell(row, 5).value or '')
    entry = sources.get(str(ws.cell(row, 1).value))
    if entry is None:
        return embedded, {'kind': 'embedded', 'characters': len(embedded)}
    for key, col in (('repo', 2), ('workflow', 3), ('case_hash', 4)):
        if entry[key] != ws.cell(row, col).value:
            raise ValueError(f'YAML provenance identity mismatch: {key}')
    root = Path(__file__).resolve().parents[2]
    path = (root / entry['path']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('YAML provenance path outside project')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry['sha256']:
        raise ValueError('YAML provenance SHA256 mismatch')
    content = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    if not embedded or not content.startswith(embedded):
        raise ValueError('YAML provenance does not match embedded snapshot prefix')
    return content, {'kind': 'verified_snapshot_file', 'path': path.relative_to(root).as_posix(),
                     'sha256': digest, 'characters': len(content),
                     'embedded_characters': len(embedded)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--xlsx', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--version-labels', choices=('inventory', 'positive'), default='inventory')
    ap.add_argument('--code99', choices=('unknown', 'positive', 'negative'), default='unknown')
    ap.add_argument('--yaml-source-manifest', help='Defaults to the workbook sibling .yaml_sources.json')
    args = ap.parse_args()
    source = Path(args.xlsx).resolve()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ws = openpyxl.load_workbook(source, data_only=True)['Sheet']
    manifest_path = Path(args.yaml_source_manifest) if args.yaml_source_manifest else source.with_suffix('.yaml_sources.json')
    if args.yaml_source_manifest and not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    sources = loads_smell_json(manifest_path.read_text(encoding='utf-8'))['cases'] if manifest_path.exists() else {}
    assert ws.cell(1, 8).value in {'no_fallback_cache', 'no_restore_keys'}, 'Unexpected fallback annotation column'
    parser, detector = WorkflowParser(), CacheSmellDetector()
    cases = {t: Counter(tp=0, fp=0, fn=0, tn=0) for t in TYPES}
    job_stats = {t: Counter(tp=0, fp=0, fn=0, tn=0) for t in TYPES}
    rows, excluded, failed = [], [], []
    skips = {t: [] for t in TYPES}
    job_skips = {t: [] for t in TYPES}
    for r in range(2, ws.max_row + 1):
        cid = ws.cell(r, 1).value
        if is_not_adopted(ws.cell(r, 13).value):
            excluded.append(cid)
            continue
        try:
            content, yaml_source = load_case_yaml(ws, r, sources)
        except (ValueError, OSError, KeyError) as exc:
            failed.append({'id': cid, 'excel_row': r, 'reason': str(exc)})
            continue
        model = parser.parse_content(content)
        if model is None:
            failed.append({'id': cid, 'excel_row': r, 'reason': 'YAML parse failure'})
            continue
        result = detector.detect(model).to_dict()
        if result.get('error'):
            failed.append({'id': cid, 'excel_row': r, 'reason': result['error']})
            continue
        raw_jobs = (yaml.safe_load(content) or {}).get('jobs', {})
        predictions = {t: set() for t in TYPES}
        artifact_participants = set()
        for smell in result.get('smells', []):
            stype = canonical_smell_type(smell.get('type', ''))
            if stype == 'cache_as_artifact':
                artifact_participants.update(cache_artifact_participants(smell, raw_jobs))
            if stype in predictions:
                if stype == 'redundant_cache_update':
                    names = (smell.get('details') or {}).get('jobs')
                    if not isinstance(names, list):
                        names = parse_manual_jobs(smell.get('job_name'))
                    predictions[stype].update(str(n) for n in names)
                else:
                    predictions[stype].add(str(smell.get('job_name') or ''))
        item = {'id': cid, 'excel_row': r, 'repo': ws.cell(r, 2).value,
                'workflow': ws.cell(r, 3).value, 'yaml_source': yaml_source,
                'by_type': {}, 'findings': result.get('smells', [])}
        for t in TYPES:
            positive, labelled_jobs, ignored, annotation, reason = reference(
                ws, r, t, raw_jobs, args.version_labels, args.code99)
            reported_jobs = predictions[t] - ignored
            pred = ((artifact_participants - ignored) if t == 'cache_as_artifact'
                    else reported_jobs)
            data = {'raw_annotation': annotation, 'manual_positive': positive,
                    'manual_jobs': sorted(labelled_jobs) if labelled_jobs is not None else None,
                    'predicted_jobs': sorted(pred), 'ignored_jobs': sorted(ignored)}
            if t == 'cache_as_artifact':
                data['reported_jobs'] = sorted(reported_jobs)
            if positive is None:
                skips[t].append({'id': cid, 'reason': reason, 'annotation': annotation})
                data['status'] = 'excluded'
            else:
                status = ('tp' if reported_jobs else 'fn') if positive else ('fp' if reported_jobs else 'tn')
                cases[t][status] += 1
                data['status'] = status
                if labelled_jobs is None:
                    job_skips[t].append(cid)
                else:
                    labelled_jobs -= ignored
                    universe = set(str(j) for j in raw_jobs) - ignored
                    job_stats[t].update(tp=len(pred & labelled_jobs), fp=len(pred - labelled_jobs),
                                        fn=len(labelled_jobs - pred), tn=len(universe - labelled_jobs - pred))
                    data['job_fp'] = sorted(pred - labelled_jobs)
                    data['job_fn'] = sorted(labelled_jobs - pred)
            item['by_type'][t] = data
        rows.append(item)
    report = {
        'source': source.relative_to(Path(__file__).resolve().parents[2]).as_posix(), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'yaml_source_manifest': manifest_path.relative_to(Path(__file__).resolve().parents[2]).as_posix() if manifest_path.exists() else None,
        'physical_cases': ws.max_row - 1, 'excluded_adoption_zero': len(excluded),
        'excluded_ids': excluded, 'parsed_and_detected_cases': len(rows), 'failures': failed,
        'policy': {'version_labels': args.version_labels, 'code99': args.code99,
                   'columns': COLS, 'extra_overly_specific_column': 29,
                   'unreachable_source_columns': [11, 12],
                   'primary_unit': 'workflow case per smell type',
                   'detector_type_aliases': TYPE_ALIASES,
                   'secondary_unit': 'unique job per smell type; only resolvable annotations',
                   'cache_as_artifact_job_matching': 'unique exact YAML job IDs from reported job_name, producer_job and consumer_job; case-level matching unchanged',
                   'ndc_parentheses': 'exclude parenthesized jobs per AGENTS.md; all-ignored cases excluded for NDC',
                   'pending_expansion': 'leading 待扩展 marker excludes only this smell type; never the whole workflow',
                   'no_extra_id_exclusions': True,
                   'blank_or_NA': 'negative',
                   'unused_columns': [16, 23, 24, 25, 26, 27, 28, 30, 31]},
        'by_smell_type': {t: {'case_level': metrics(cases[t]), 'job_level': metrics(job_stats[t]),
                             'excluded_case_labels': skips[t], 'unresolved_job_case_ids': job_skips[t]}
                          for t in TYPES},
        'cases': rows,
    }
    (out / 'evaluation.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    fields = ['type', 'evaluated_cases', 'excluded_cases', 'tp', 'fp', 'fn', 'tn', 'precision', 'recall', 'f1', 'accuracy']
    with (out / 'case_metrics.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for t in TYPES:
            writer.writerow(dict(type=t, evaluated_cases=sum(cases[t].values()), excluded_cases=len(skips[t]), **metrics(cases[t])))
    with (out / 'job_metrics.csv').open('w', encoding='utf-8-sig', newline='') as f:
        job_fields = ['type', 'tp', 'fp', 'fn', 'tn', 'precision', 'recall', 'f1', 'accuracy', 'unresolved_positive_cases']
        writer = csv.DictWriter(f, job_fields)
        writer.writeheader()
        for t in TYPES:
            writer.writerow(dict(type=t, **metrics(job_stats[t]), unresolved_positive_cases=len(job_skips[t])))
    with (out / 'disagreements.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields2 = ['id', 'excel_row', 'repo', 'workflow', 'type', 'status', 'annotation', 'predicted_jobs']
        writer = csv.DictWriter(f, fields2)
        writer.writeheader()
        for row in rows:
            for t, data in row['by_type'].items():
                if data['status'] in ('fp', 'fn'):
                    writer.writerow({**{k: row[k] for k in fields2[:4]}, 'type': t, 'status': data['status'],
                                     'annotation': data['raw_annotation'], 'predicted_jobs': ';'.join(data['predicted_jobs'])})
    def pct(x):
        return '—' if x is None else f'{x * 100:.2f}%'
    lines = ['# 十一种缓存异味检测器验证', '',
             f'共 {ws.max_row - 1} 个案例；采用=0 排除 {len(excluded)} 个；成功运行检测器 {len(rows)} 个；失败 {len(failed)} 个。', '',
             '主表按工作流案例逐类型进行二分类。Accuracy=(TP+TN)/N；Precision=TP/(TP+FP)；Recall=TP/(TP+FN)。',
             '未判定及待扩展标签只排除对应异味；NDC 括号标注遵循项目已有排除规则。嵌套列不排除整例，按各类型可见标注评估。没有额外排除 ID44。',
             f'版本列解释：{args.version_labels}；99 解释：{args.code99}。', '',
             '|异味|纳入|TP|FP|FN|TN|Precision|Recall|F1|Accuracy|',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for t in TYPES:
        m = metrics(cases[t])
        lines.append('|' + '|'.join([t, str(sum(cases[t].values()))] + [str(m[k]) for k in ('tp', 'fp', 'fn', 'tn')] + [pct(m[k]) for k in ('precision', 'recall', 'f1', 'accuracy')]) + '|')
    lines += ['', '## Job 级补充结果', '',
              '仅对能独立解析为 YAML job ID、显示名称或步骤名的标注计数。描述性阳性不能确定完整 job 清单时，排除该案例的 job 级比较，仍纳入案例级主表。RCU 按参与 job 去重，不按冲突组或步骤计数。S7 从检测结果的 job_name、producer_job、consumer_job 提取并去重实际 YAML job ID，同时计入生产者与消费者；不推断未报告的传递关系，案例级判定保持原口径。', '',
              '|异味|TP|FP|FN|Precision|Recall|F1|无法解析阳性案例数|',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    for t in TYPES:
        m = metrics(job_stats[t])
        lines.append('|' + '|'.join([t] + [str(m[k]) for k in ('tp', 'fp', 'fn')] + [pct(m[k]) for k in ('precision', 'recall', 'f1')] + [str(len(job_skips[t]))]) + '|')
    lines += ['', '## 口径与边界', '',
              '- 源文件保持不变；使用研究冻结的 run3 CacheSmellDetector，未优化检测规则。',
              '- 旧“静态检测器结果”列不作为人工真值；未采用任何表内指令。',
              '- unreachable_cache_write 无独立专列，从说明/其他潜在异味中的明确“无效缓存”等标签派生；空白按阴性，因此该类型的真值完整性有限。',
              '- 缓存重复列对应 overlapping_cache_mechanisms（代码旧名 conflicting_cache_sources）；当前实现仅覆盖 setup 内置缓存与手动缓存的路径重叠，未证明覆盖该正式定义的所有机制组合。',
              '- 一次性/细粒度缓存键两列合并对应 overly_specific_cache_key。',
              '- 回退键不一致、路径问题和其他列不是额外正式异味，未自动并入十一类标签。',
              '- 案例级 TP 只表示工作流中存在同类异味，不证明缓存步骤、资源、子类型或 job 完全匹配。',
              '- 稀有异味补充样本经过富集；本结果描述此验证集，不能直接推算总体流行度或总体准确率。',
              '- evaluation.json 保存逐案例真值、检测结果、排除原因及原始 findings；disagreements.csv 保存案例级 FP/FN。', '',
              '## 逐类型排除', '']
    for t in TYPES:
        if skips[t]:
            lines.append(f'- {t}: ' + '; '.join(f"ID{x['id']} ({x['reason']})" for x in skips[t]))
    (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines[:22]))
    print(f'Results: {out.resolve()}')


if __name__ == '__main__':
    main()
