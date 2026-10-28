"""Study annotation helpers, extracted without changing their scoring logic."""

import re

from typing import List

from src.smell_names import canonical_smell_name

SMELL_COLS = {
    'no_dependency_cache': 7,
    'no_fallback_cache': 8,
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

DISPOSABLE_EXTRA_COLS = (29,)

PATH_GROUP_COL = 23

CASE_LEVEL_SENTINEL = '__case__'

DERIVED_SMELL_TYPES = (
    'unreachable_cache_write',
    'overly_broad_cache_path',
    'cache_path_pollution',
    'non_portable_cache_path',
)

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
    value = canonical_smell_name(value)
    if value == 'disposable_cache_key':
        return 'overly_specific_cache_key'
    return value

def normalize_manual_job(m_job: str) -> str:
    """标准化人工标注的 job 名（去掉括号注释，如 build(cargo) -> build）"""
    m = re.sub(r'[（(].*?[）)]', '', m_job)
    return m.strip().rstrip(',;；')
