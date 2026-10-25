"""评估活动检测器在人工标注验证集上的表现。

数据来源：data_26_6_9/verify/cases_review_full_yaml_with_repo.xlsx
  - 默认评估前 200 个物理数据行（Excel 行 2-201）
  - G-J 列是四种原有缓存异味的人工标注（job 名，逗号/分号分隔；N/A 或空表示无）
  - R、T、U、Y、Z 列分别补充 no_docker_layer_cache、deprecated_cache_action、
    cache_as_artifact、malformed_cache_path、incorrect_cache_hit_condition
  - Q、S、V、AC 列分别是“缓存重复”“静态缓存键”“一次性缓存”“细粒度缓存”，
    其中 V、AC 合并用于评估 overly_specific_cache_key
  - M 列“采用”为 0 的案例完全不参与统计

评估口径（job 级匹配）：
  - TP: 检测器报告某 job 有异味，且人工标注该 job
  - FP: 检测器报告某 job 有异味，但人工未标注
  - FN: 人工标注某 job 有异味，但检测器未报告
  - no_dependency_cache 标注中带括号的 job（如 smoke（N/A）、build(cargo)）
    表示检测器不覆盖的语言；该 job 及其对应检测结果均不参与计数

用法：
  python scripts/evaluate_verify_xlsx.py [--xlsx <path>] [--limit 200]
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

import openpyxl

from src.detectors.smell.cache_smell_detector import CacheSmellDetector
from src.parsers.workflow_parser import WorkflowParser

SMELL_COLS = {
    'no_dependency_cache': 7,
    'no_restore_keys': 8,
    'redundant_cache_update': 9,
    'cache_position_error': 10,
    'no_docker_layer_cache': 18,
    'deprecated_cache_action': 20,
    'cache_as_artifact': 21,
    # 新扩展异味的人工标注列：Q、S、V。
    # “缓存重复”是人工标注口径，当前作为 conflicting_cache_sources 的近似标签。
    'conflicting_cache_sources': 17,
    'static_cache_key': 19,
    'overly_specific_cache_key': 22,
    'malformed_cache_path': 24,
    'incorrect_cache_hit_condition': 25,
}
ADOPTION_COL = 13
DISPOSABLE_EXTRA_COLS = (29,)
PATH_GROUP_COL = 23
CASE_LEVEL_SENTINEL = '__case__'

# 这些类型不是当前验证表中的原有四类，而是本轮扩展的验证对象。
# unreachable_cache_write 以及路径三分类来自“其他潜在异味”/路径分组列，
# 因此在读取标注时单独构造，不能假设 Excel 有同名专列。
DERIVED_SMELL_TYPES = (
    'unreachable_cache_write',
    'overly_broad_cache_path',
    'cache_path_pollution',
    'non_portable_cache_path',
)
EVALUATED_SMELL_TYPES = tuple(SMELL_COLS) + DERIVED_SMELL_TYPES

# 完全排除的案例（不纳入评估）
EXCLUDED_CASE_IDS = {44}

# 分隔符：逗号、分号（含全角）
_SEP_RE = re.compile(r'[,;，；]')


def parse_manual_jobs(value) -> List[str]:
    """解析人工标注的 job 名列表

    no_dependency_cache 列中带括号的值（如 build(cargo)）表示非 JS/Python，
    返回 (job, excluded) 标记，由调用方决定是否排除。
    """
    if value is None:
        return []
    s = str(value).strip()
    if not s or s in ('N/A', 'None', 'NA', '-'):
        return []
    # 按分隔符拆分
    parts = _SEP_RE.split(s)
    jobs = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 去掉末尾多余的分号
        p = p.rstrip(';；')
        if p:
            jobs.append(p)
    return jobs


def parse_manual_rcu_groups(value):
    """解析 RCU 的冲突组标注。

    当标注使用分号时，分号表示不同冲突组，逗号表示同一组内的 Job。
    没有分号的历史标注仍沿用旧的扁平 Job 列表口径。
    """
    if value is None:
        return None
    raw = str(value).strip()
    if ';' not in raw:
        return None
    groups = []
    for group_text in raw.split(';'):
        jobs = {
            normalize_manual_job(job)
            for job in re.split(r'[,，]', group_text)
            if normalize_manual_job(job)
        }
        if jobs:
            groups.append(jobs)
    return groups


def has_parenthesized_annotation(job: str) -> bool:
    """检查单个 job 标注是否带语言覆盖范围的括号注释。"""
    return bool(re.search(r'[（(].*?[）)]', job))


def is_not_adopted(value) -> bool:
    """判断案例是否在“采用”列中标记为不采用。"""
    return value == 0 or (isinstance(value, str) and value.strip() == '0')


def is_positive_flag(value) -> bool:
    """识别扩展标注列中的案例级正标签（如 1、1.0、True）。"""
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {'1', '1.0', 'true', 'yes', '是'}
    return False


def _annotation_text(ws, row: int) -> str:
    """拼接用于派生标签的人工说明，不包含 YAML 正文。"""
    return ' | '.join(
        str(ws.cell(row, col).value or '')
        for col in (11, 12, PATH_GROUP_COL, 24, 25)
    )


def _parse_case_or_jobs(value) -> List[str]:
    """把扩展列解析成 Job 列表；1/True 表示案例级标签。"""
    return [CASE_LEVEL_SENTINEL] if is_positive_flag(value) else parse_manual_jobs(value)


def build_manual_labels(ws, row: int):
    """读取一行人工标注，并补充没有独立列的扩展类型。

    返回 manual_by_type、ignored_jobs_by_type、manual_groups_by_type。
    ``cache_as_artifact`` 等案例级标注使用 CASE_LEVEL_SENTINEL，避免同一案例
    因多个 Job 或多个缓存步骤被重复计数。
    """
    manual_by_type = {}
    ignored_jobs_by_type = {}
    manual_groups_by_type = {}

    for smell_type, col in SMELL_COLS.items():
        value = ws.cell(row, col).value
        if smell_type in {'cache_as_artifact', 'deprecated_cache_action'}:
            jobs = _parse_case_or_jobs(value)
        else:
            jobs = parse_manual_jobs(value)

        if smell_type == 'overly_specific_cache_key':
            for extra_col in DISPOSABLE_EXTRA_COLS:
                jobs.extend(parse_manual_jobs(ws.cell(row, extra_col).value))
            jobs = list(dict.fromkeys(jobs))

        if smell_type == 'no_dependency_cache':
            ignored_jobs = [job for job in jobs if has_parenthesized_annotation(job)]
            manual_by_type[smell_type] = [
                job for job in jobs if not has_parenthesized_annotation(job)
            ]
            ignored_jobs_by_type[smell_type] = ignored_jobs
        else:
            manual_by_type[smell_type] = jobs

        if smell_type == 'redundant_cache_update':
            groups = parse_manual_rcu_groups(value)
            if groups is not None:
                manual_groups_by_type[smell_type] = groups

    annotation = _annotation_text(ws, row)

    # “无效缓存”是人工对不可执行 save 或无效写入的描述；如果说明中带
    # job 名，保留 job 级匹配，否则使用案例级标签。
    if re.search(
        r'(?:无效缓存|unreachable_cache_write|save缓存的条件|save\s+条件)',
        annotation,
        re.IGNORECASE,
    ):
        invalid_jobs = []
        for match in re.finditer(r'(?:无效缓存|unreachable_cache_write)\s*[（(]([^）)]+)', annotation, re.IGNORECASE):
            candidate = match.group(1).strip()
            # 只有括号内容本身像 Job 标识时才用于 Job 级匹配；
            # “无效缓存（save 条件是……）”是解释文字，应保留为案例级标签。
            if re.fullmatch(r'[A-Za-z0-9_.-]+', candidate):
                invalid_jobs.extend(parse_manual_jobs(candidate))
        manual_by_type['unreachable_cache_write'] = list(dict.fromkeys(invalid_jobs)) or [CASE_LEVEL_SENTINEL]
    else:
        manual_by_type['unreachable_cache_write'] = []

    # ID180 一类说明明确指出“缓存命中条件逻辑错误”，但没有在专列中填 Job；
    # 作为案例级参考标签处理，避免因标注位置不一致而把已确认案例当作 FN。
    if not manual_by_type.get('incorrect_cache_hit_condition') and re.search(
        r'缓存命中条件逻辑错误|条件反转风险|incorrect[_ -]?cache[_ -]?hit[_ -]?condition',
        annotation,
        re.IGNORECASE,
    ):
        manual_by_type['incorrect_cache_hit_condition'] = [CASE_LEVEL_SENTINEL]

    # 路径分组列的 Job 名与说明中的中文标记共同决定具体路径异味，
    # 不把同一个“路径问题”同时计入三个类型。
    path_jobs = parse_manual_jobs(ws.cell(row, PATH_GROUP_COL).value)
    path_text = annotation.lower()
    path_type = None
    if re.search(r'跨平台不兼容|路径跨平台|non[_ -]?portable|cross[_ -]?platform', path_text):
        path_type = 'non_portable_cache_path'
    elif re.search(r'路径污染|临时路径|cache[_ -]?path[_ -]?pollution|pollution', path_text):
        path_type = 'cache_path_pollution'
    elif re.search(r'路径宽泛|overly[_ -]?broad|缓存路径过宽', path_text):
        path_type = 'overly_broad_cache_path'
    for smell_type in DERIVED_SMELL_TYPES[1:]:
        manual_by_type[smell_type] = path_jobs if smell_type == path_type else []

    return manual_by_type, ignored_jobs_by_type, manual_groups_by_type


def normalize_detected_smell_type(value: str) -> str:
    """将检测器历史名称映射到论文当前使用的名称。"""
    if value == 'disposable_cache_key':
        return 'overly_specific_cache_key'
    return value


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> List[float]:
    """Return a two-sided Wilson interval for a binomial proportion."""
    if trials <= 0:
        return [None, None]
    p = successes / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials)
        / denominator
    )
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _is_writable_cache_step(step: Dict) -> bool:
    return (
        step.get('can_write', True) is not False
        and not bool(step.get('read_only'))
        and step.get('lookup_only') is not True
        and step.get('fail_on_cache_miss') is not True
    )


def opportunity_jobs(result: Dict, smell_type: str) -> List[str]:
    """Extract conservative eligible job/opportunity units for validation.

    The workbook labels positive jobs but does not enumerate every negative
    opportunity. These units are therefore reported as a supplementary TN
    denominator, not substituted for the TP/FP/FN matching protocol.
    """
    metadata = result.get('metadata') or {}
    jobs = metadata.get('jobs') or {}
    opportunities: List[str] = []
    for job_name, job_data in jobs.items():
        analysis = (job_data or {}).get('analysis') or {}
        cache_steps = [
            step for step in (analysis.get('cache_steps') or [])
            if isinstance(step, dict)
        ]
        manual_steps = [
            step for step in cache_steps
            if str(step.get('action') or '').lower().startswith('actions/cache')
            and step.get('key') not in (None, '')
        ]
        writable_manual_steps = [
            step for step in manual_steps if _is_writable_cache_step(step)
        ]
        setup_cache = any(bool(step.get('setup_cache')) for step in cache_steps)

        eligible = False
        if smell_type == 'no_dependency_cache':
            eligible = bool(analysis.get('dep_langs')) and not bool(
                analysis.get('nested_actions')
            )
        elif smell_type == 'no_restore_keys':
            eligible = bool(manual_steps)
        elif smell_type == 'cache_position_error':
            eligible = any(step.get('index') is not None for step in cache_steps)
        elif smell_type == 'conflicting_cache_sources':
            eligible = bool(manual_steps) and setup_cache
        elif smell_type in {'static_cache_key', 'overly_specific_cache_key'}:
            eligible = bool(writable_manual_steps)
        elif smell_type == 'redundant_cache_update':
            # This is a cross-job relation, so it is handled as a workflow
            # opportunity below rather than pretending each writer is a TN.
            continue
        if eligible:
            opportunities.append(str(job_name))

    if smell_type == 'redundant_cache_update':
        writer_jobs = []
        for job_name, job_data in jobs.items():
            analysis = (job_data or {}).get('analysis') or {}
            if any(
                isinstance(step, dict)
                and _is_writable_cache_step(step)
                and str(step.get('action') or '').lower().startswith('actions/cache')
                for step in (analysis.get('cache_steps') or [])
            ):
                writer_jobs.append(str(job_name))
        return ['__workflow__'] if len(writer_jobs) >= 2 else []
    return opportunities


def normalize_job(detected_job: str) -> str:
    """标准化检测器输出的 job_name（redundant_cache_update 可能是逗号拼接的多个 job）"""
    if not detected_job:
        return ''
    return str(detected_job).strip().rstrip(',;；')


def normalize_manual_job(m_job: str) -> str:
    """标准化人工标注的 job 名（去掉括号注释，如 build(cargo) -> build）"""
    m = re.sub(r'[（(].*?[）)]', '', m_job)
    return m.strip().rstrip(',;；')


def match_job(detected_job: str, manual_job: str) -> bool:
    """判断检测的 job 是否匹配人工标注的 job（子串匹配）"""
    if not detected_job or not manual_job:
        return False
    d = normalize_job(detected_job)
    m = normalize_manual_job(manual_job)
    if d == m:
        return True
    # 子串匹配：覆盖 job 名是列表拼接的情况
    return d in m or m in d


def _deduplicate_detected_smells(smells: List[Dict]) -> List[Dict]:
    """按异味类型和 Job 去重，匹配人工标注的 Job 级单位。

    一个 Job 可能因多个缓存步骤、多个路径或多个旧版本 action 产生多条
    同类 finding；验证表通常只标注一次该 Job，因此不能把这些 finding
    重复计为多个 FP/TP。
    """
    deduplicated = []
    seen = set()
    for smell in smells:
        smell_type = str(smell.get('type') or '')
        if smell_type == 'redundant_cache_update':
            deduplicated.append(smell)
            continue
        key = (smell_type, normalize_job(smell.get('job_name', '')))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(smell)
    return deduplicated


def compare_case(detected_smells: List[Dict], manual_by_type: Dict[str, List[str]],
                 ignored_jobs_by_type: Dict[str, List[str]] = None,
                 manual_groups_by_type: Dict[str, List[set]] = None) -> Dict[str, Dict]:
    """比较单个案例的检测结果与人工标注，并跳过指定 job。"""
    ignored_jobs_by_type = ignored_jobs_by_type or {}
    manual_groups_by_type = manual_groups_by_type or {}
    results = defaultdict(lambda: {'tp': [], 'fp': [], 'fn': [], 'skipped': []})

    detected_by_type = defaultdict(list)
    for smell in _deduplicate_detected_smells(detected_smells):
        detected_by_type[smell.get('type', '')].append(smell)

    for smell_type in EVALUATED_SMELL_TYPES:
        detected = detected_by_type.get(smell_type, [])
        manual = manual_by_type.get(smell_type, [])
        ignored_jobs = ignored_jobs_by_type.get(smell_type, [])
        res = results[smell_type]

        # RCU 新标注支持“分号分组、逗号分隔组内 Job”。
        # 检测器的 matrix 单实例结果允许作为人工冲突组的子集匹配。
        if smell_type == 'redundant_cache_update' and smell_type in manual_groups_by_type:
            manual_groups = manual_groups_by_type[smell_type]
            detected_groups = []
            for detected_smell in detected:
                details = detected_smell.get('details') or {}
                detected_jobs = details.get('jobs')
                if not isinstance(detected_jobs, list):
                    detected_jobs = parse_manual_jobs(detected_smell.get('job_name', ''))
                detected_group = {
                    normalize_manual_job(job)
                    for job in detected_jobs
                    if normalize_manual_job(job)
                }
                detected_groups.append(detected_group)
                if any(detected_group and detected_group.issubset(group) for group in manual_groups):
                    res['tp'].append({'job': detected_smell.get('job_name', '')})
                else:
                    res['fp'].append({
                        'job': detected_smell.get('job_name', ''),
                        'message': detected_smell.get('message', '')[:200],
                    })

            for manual_group in manual_groups:
                if not any(group and group.issubset(manual_group) for group in detected_groups):
                    res['fn'].append({'job': ', '.join(sorted(manual_group))})
            continue

        # 部分扩展标注是案例级的 1，而检测器输出仍然指向一个具体 Job。
        # 只比较“该案例是否出现 finding”，避免把多个 finding 当成多个样本。
        if CASE_LEVEL_SENTINEL in manual:
            if detected:
                res['tp'].append({
                    'job': CASE_LEVEL_SENTINEL,
                    'detected_jobs': [item.get('job_name', '') for item in detected],
                })
            else:
                res['fn'].append({'job': CASE_LEVEL_SENTINEL})
            continue
        if not manual and detected and smell_type in {
            'cache_as_artifact', 'unreachable_cache_write',
        }:
            # 对没有案例级正标签的扩展规则，仍按一个案例计 FP。
            res['fp'].append({
                'job': CASE_LEVEL_SENTINEL,
                'message': detected[0].get('message', '')[:200],
                'detected_jobs': [item.get('job_name', '') for item in detected],
            })
            continue

        evaluated_detected = []
        for detected_smell in detected:
            detected_job = detected_smell.get('job_name', '')
            ignored_job = next(
                (job for job in ignored_jobs if match_job(detected_job, job)),
                None,
            )
            if ignored_job is not None:
                res['skipped'].append({'job': detected_job, 'manual_job': ignored_job})
            else:
                evaluated_detected.append(detected_smell)

        for detected_smell in evaluated_detected:
            detected_job = detected_smell.get('job_name', '')
            if any(match_job(detected_job, manual_job) for manual_job in manual):
                res['tp'].append({'job': detected_job})
            else:
                res['fp'].append({
                    'job': detected_job,
                    'message': detected_smell.get('message', '')[:200],
                })

        for manual_job in manual:
            if not any(match_job(detected_smell.get('job_name', ''), manual_job)
                       for detected_smell in evaluated_detected):
                res['fn'].append({'job': manual_job})

    return results


def main():
    parser = argparse.ArgumentParser(description='评估检测器在人工标注验证集上的表现')
    parser.add_argument('--xlsx', default='data_26_6_9/verify/cases_review_full_yaml_with_repo.xlsx',
                        help='验证集 Excel 路径')
    parser.add_argument('--limit', type=int, default=200, help='评估前 N 个案例')
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f'错误: 文件不存在 {xlsx_path}')
        sys.exit(1)

    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    ws = wb['Sheet']

    # 初始化检测器
    detector = CacheSmellDetector()
    parser_ = WorkflowParser()

    total = 0
    parsed_ok = 0
    parse_fail = []
    scanned_rows = 0
    excluded_not_adopted = 0
    excluded_case_ids = 0
    missing_yaml = 0

    # 汇总统计
    stats = defaultdict(lambda: {
        'tp': 0,
        'fp': 0,
        'fn': 0,
        'opportunities': 0,
        'reference_positive_opportunities': 0,
        'true_negative_opportunities': 0,
    })
    fp_details = []
    fn_details = []
    ignored_ndc_jobs = 0
    ignored_ndc_cases = 0

    for r in range(2, args.limit + 2):
        scanned_rows += 1
        id_val = ws.cell(r, 1).value
        repo = ws.cell(r, 2).value
        workflow = ws.cell(r, 3).value
        yaml_content = ws.cell(r, 5).value
        adopted = ws.cell(r, ADOPTION_COL).value

        if is_not_adopted(adopted):
            excluded_not_adopted += 1
            continue
        if id_val in EXCLUDED_CASE_IDS:
            excluded_case_ids += 1
            continue
        if not yaml_content:
            missing_yaml += 1
            continue
        total += 1

        # 解析工作流
        workflow_model = parser_.parse_content(str(yaml_content))
        if not workflow_model:
            parse_fail.append((id_val, repo, workflow, '解析失败'))
            continue
        parsed_ok += 1

        # 运行检测器
        result = detector.detect(workflow_model)
        result_dict = result.to_dict()
        detected = []
        for smell in result_dict.get('smells', []):
            normalized = dict(smell)
            normalized['type'] = normalize_detected_smell_type(
                str(normalized.get('type') or '')
            )
            detected.append(normalized)

        # 读取人工标注。除原有列外，build_manual_labels 会从“其他潜在异味”、
        # 路径分组列和案例级 1 值中构造本轮新增类型的参考标签。
        manual_by_type, ignored_jobs_by_type, manual_groups_by_type = build_manual_labels(ws, r)
        ignored_jobs = ignored_jobs_by_type.get('no_dependency_cache', [])
        ignored_ndc_jobs += len(ignored_jobs)
        ignored_ndc_cases += bool(ignored_jobs)

        # 比较
        comparison = compare_case(
            detected,
            manual_by_type,
            ignored_jobs_by_type,
            manual_groups_by_type,
        )

        for smell_type in EVALUATED_SMELL_TYPES:
            res = comparison[smell_type]
            stats[smell_type]['tp'] += len(res['tp'])
            stats[smell_type]['fp'] += len(res['fp'])
            stats[smell_type]['fn'] += len(res['fn'])
            opportunities = opportunity_jobs(result_dict, smell_type)
            detected_jobs = [
                smell.get('job_name', '')
                for smell in detected
                if smell.get('type') == smell_type
            ]
            ignored_jobs = ignored_jobs_by_type.get(smell_type, [])
            for opportunity in opportunities:
                if smell_type == 'no_dependency_cache' and any(
                    match_job(opportunity, ignored)
                    for ignored in ignored_jobs
                ):
                    continue
                stats[smell_type]['opportunities'] += 1
                reference_positive = any(
                    match_job(opportunity, manual_job)
                    for manual_job in manual_by_type.get(smell_type, [])
                )
                prediction_positive = any(
                    match_job(opportunity, detected_job)
                    for detected_job in detected_jobs
                )
                if reference_positive:
                    stats[smell_type]['reference_positive_opportunities'] += 1
                elif not prediction_positive:
                    stats[smell_type]['true_negative_opportunities'] += 1
            for fp in res['fp']:
                fp_details.append((id_val, repo, workflow, smell_type, fp['job'], fp['message']))
            for fn in res['fn']:
                fn_details.append((id_val, repo, workflow, smell_type, fn['job']))

    # 输出结果
    print('=' * 80)
    print(f'检测器评估结果（人工标注前 {args.limit} 个案例）')
    print('=' * 80)
    print(f'物理扫描行数: {scanned_rows} (Excel 行 2-{args.limit + 1})')
    print(f'采用=0 排除: {excluded_not_adopted}, 指定 ID 排除: {excluded_case_ids}, 空 YAML: {missing_yaml}')
    print(f'参与解析案例: {total}, 解析成功: {parsed_ok}, 解析失败: {len(parse_fail)}')
    if parse_fail:
        for pid, prepo, pwf, err in parse_fail:
            print(f'  [解析失败] ID={pid} {prepo}/{pwf}: {err}')
    print(
        'no_dependency_cache 带括号注释而忽略的 job: '
        f'{ignored_ndc_jobs}（涉及 {ignored_ndc_cases} 个案例）'
    )
    print()

    print(f'{"异味类型":<28}{"TP":>5}{"FP":>5}{"FN":>5}{"Precision":>12}{"Recall":>10}{"F1":>10}')
    print('-' * 80)
    results_out = {}
    for smell_type in EVALUATED_SMELL_TYPES:
        s = stats[smell_type]
        tp, fp, fn = s['tp'], s['fp'], s['fn']
        precision = tp / (tp + fp) if tp + fp > 0 else 0.0
        recall = tp / (tp + fn) if tp + fn > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0
        precision_ci = wilson_interval(tp, tp + fp)
        recall_ci = wilson_interval(tp, tp + fn)
        print(f'{smell_type:<28}{tp:>5}{fp:>5}{fn:>5}{precision*100:>10.2f}%{recall*100:>8.2f}%{f1*100:>8.2f}%')
        results_out[smell_type] = {
            'tp': tp, 'fp': fp, 'fn': fn,
            'precision': round(precision, 4),
            'recall': round(recall, 4),
            'f1': round(f1, 4),
            'precision_wilson_ci_95': [round(value, 4) if value is not None else None for value in precision_ci],
            'recall_wilson_ci_95': [round(value, 4) if value is not None else None for value in recall_ci],
            'opportunity_unit': (
                'workflow_relation' if smell_type == 'redundant_cache_update'
                else 'case' if smell_type in {'cache_as_artifact', 'unreachable_cache_write'}
                else 'job'
            ),
            'eligible_opportunities': s['opportunities'],
            'reference_positive_opportunities': (
                None if smell_type == 'redundant_cache_update'
                else s['reference_positive_opportunities']
            ),
            'true_negative_opportunities': (
                None if smell_type == 'redundant_cache_update'
                else s['true_negative_opportunities']
            ),
        }

    # 输出 FP/FN 明细
    print()
    if fp_details:
        print(f'--- 误报 (FP) 共 {len(fp_details)} 条 ---')
        for pid, repo, wf, stype, job, msg in fp_details:
            print(f'  ID={pid} {repo}/{wf} [{stype}] job={job}')
    if fn_details:
        print(f'\n--- 漏报 (FN) 共 {len(fn_details)} 条 ---')
        for pid, repo, wf, stype, job in fn_details:
            print(f'  ID={pid} {repo}/{wf} [{stype}] job={job}')

    # 保存 JSON
    output = {
        'evaluation_scope': {
            'physical_row_start': 2,
            'physical_row_end': args.limit + 1,
            'row_limit': args.limit,
            'manual_label_columns': SMELL_COLS,
            'adoption_column': ADOPTION_COL,
            'evaluated_smell_types': list(EVALUATED_SMELL_TYPES),
            'case_level_sentinel': CASE_LEVEL_SENTINEL,
            'derived_label_columns': {
                'unreachable_cache_write': {
                    'source_columns': [11, 12],
                    'source_headers': ['说明', '其他潜在异味'],
                    'rule': '从“无效缓存”或等价明确描述派生；没有明确 Job 时按案例级标签计数',
                },
                'overly_broad_cache_path': {
                    'source_column': PATH_GROUP_COL,
                    'source_header': '缓存路径宽泛/路径污染风险/跨平台不兼容',
                    'marker': 'overly_broad_cache_path/路径宽泛',
                },
                'cache_path_pollution': {
                    'source_column': PATH_GROUP_COL,
                    'source_header': '缓存路径宽泛/路径污染风险/跨平台不兼容',
                    'marker': 'cache_path_pollution/路径污染风险',
                },
                'non_portable_cache_path': {
                    'source_column': PATH_GROUP_COL,
                    'source_header': '缓存路径宽泛/路径污染风险/跨平台不兼容',
                    'marker': 'non_portable_cache_path/路径跨平台不兼容',
                },
            },
            'extended_label_mapping': {
                'conflicting_cache_sources': {
                    'column': 17,
                    'header': '缓存重复',
                    'note': '人工“缓存重复”作为不同缓存机制冲突的近似标签，可能混入其他重复缓存情形',
                },
                'static_cache_key': {'column': 19, 'header': '静态缓存键'},
                'overly_specific_cache_key': {
                    'columns': [22, 29],
                    'headers': ['一次性缓存', '细粒度缓存'],
                    'note': '两列合并为易变缓存键标签；细粒度缓存指 key 使用工作流文件本身作为失效依据',
                },
                'no_docker_layer_cache': {'column': 18, 'header': '无docker层缓存'},
                'deprecated_cache_action': {'column': 20, 'header': '过时缓存版本'},
                'cache_as_artifact': {
                    'column': 21,
                    'header': '缓存误作产物传递',
                    'note': '1 按案例级标签处理；若填写 Job 名则按 Job 级匹配',
                },
                'malformed_cache_path': {
                    'column': 24,
                    'header': '语法错误（路径带引号\\缺少变量）',
                },
                'incorrect_cache_hit_condition': {
                    'column': 25,
                    'header': '无命中条件防护',
                },
            },
        },
        'scanned_rows': scanned_rows,
        'excluded_not_adopted_cases': excluded_not_adopted,
        'excluded_case_id_cases': excluded_case_ids,
        'missing_yaml_cases': missing_yaml,
        'total_cases': total,
        'parsed_successfully': parsed_ok,
        'parse_failures': len(parse_fail),
        'ignored_ndc_jobs_with_parenthesized_annotations': ignored_ndc_jobs,
        'ignored_ndc_cases_with_parenthesized_annotations': ignored_ndc_cases,
        'by_smell_type': results_out,
        'fp_details': [{'id': x[0], 'repo': x[1], 'workflow': x[2], 'type': x[3], 'job': x[4], 'message': x[5]}
                       for x in fp_details],
        'fn_details': [{'id': x[0], 'repo': x[1], 'workflow': x[2], 'type': x[3], 'job': x[4]}
                       for x in fn_details],
    }
    out_file = 'evaluation_results_verify200.json'
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f'\n结果已保存至: {out_file}')


if __name__ == '__main__':
    main()
