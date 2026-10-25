"""
扫描工作流异味（兼容新旧数据源）

新数据源模式 (--data-dir):
  默认从 {data_dir}/workflow_files.jsonl 读取全量待扫描列表
  工作流文件在 {data_dir}/workflows/ 下
  可复用工作流映射从 {data_dir}/workflow_files.jsonl 构建（可选）

旧数据源模式 (默认):
  从 settings 中的 run_workflow_mapping.json 读取映射
  工作流文件在 settings.workflow_fetch_output_dir/workflows/ 下

用法:
  # 默认扫描新数据源（data_26_6_9）的全量逻辑工作流
  python scan_smells_new.py --detectors cache_smell

  # 强制重扫
  python scan_smells_new.py --detectors cache_smell --force

  # 显式使用早期筛选结果（需先恢复 data_26_6_9/archive/early_data_20260908/ 中的文件）
  python scan_smells_new.py --use-filtered --detectors cache_smell

  # 旧数据源
  python scan_smells_new.py --legacy --detectors cache_smell
"""
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[1]


import sys as _smell_sys
from pathlib import Path as _SmellPath
if str(_SmellPath(__file__).resolve().parents[1]) not in _smell_sys.path:
    _smell_sys.path.insert(0, str(_SmellPath(__file__).resolve().parents[1]))

from src.smell_names import loads_smell_json, load_smell_json

import hashlib
import json
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import settings

# 新架构的检测器导入
from src.detectors.core.cache_detector import CacheDetector
from src.detectors.smell.cache_smell_detector import CacheSmellDetector
from src.detectors.core.timeout_detector import TimeoutDetector

# 旧检测器（暂未迁移）
from src.detectors.deepclone_detector import DeepCloneDetector
from src.detectors.redundantTrigger_detector import RedundantTriggerDetector
from src.detectors.failfast_detector import FailFastDetector
from src.detectors.runner_detector import RunnerDetector
from src.detectors.redundantRun_detector import RedundantRunDetector
from src.detectors.parallelism_detector import ParallelismDetector


ALL_DETECTORS = {
    'cache': CacheDetector,
    'cache_smell': CacheSmellDetector,
    'deepclone': DeepCloneDetector,
    'redundant_trigger': RedundantTriggerDetector,
    'timeout': TimeoutDetector,
    'fail_fast': FailFastDetector,
    'runner': RunnerDetector,
    'redundant_run': RedundantRunDetector,
    'parallelism': ParallelismDetector,
}

DEFAULT_DETECTORS = list(ALL_DETECTORS.keys())


# ========== 复用自 scan_smells.py 的通用函数 ==========

def normalize_smells(detector_name: str, raw_result: dict) -> tuple:
    smells = raw_result.get('smells', [])
    cache_types = []
    cache_categories = []
    cache_jobs = []
    jobs = {}

    if detector_name == 'cache':
        if not raw_result.get('uses_cache', False):
            smells = [{
                'type': 'no_cache',
                'severity': 'high',
                'message': '工作流未使用缓存，依赖安装可能重复执行',
                'details': {'suggestion': '建议使用actions/cache或setup action的内置缓存功能'}
            }]
        else:
            cache_categories = raw_result.get('cache_categories', [])
            for job in raw_result.get('cache_jobs', []):
                job_info = {
                    'job_name': job.get('job_name', ''),
                    'display_name': job.get('display_name', job.get('job_name', '')),
                    'cache_categories': job.get('cache_categories', []),
                    'cache_steps': [],
                }
                for step in job.get('cache_steps', []):
                    ct = step.get('cache_type')
                    if ct:
                        cache_types.append(ct)
                    job_info['cache_steps'].append({
                        'action': step.get('action', ''),
                        'cache_type': ct,
                        'cache_category': step.get('cache_category'),
                    })
                cache_jobs.append(job_info)
            cache_types = list(dict.fromkeys(cache_types))
        jobs = raw_result.get('jobs', {})
    elif detector_name == 'cache_smell':
        jobs = raw_result.get('jobs', {})

    return smells, cache_types, cache_categories, cache_jobs, jobs


def normalize_has_smell(detector_name: str, raw_result: dict) -> bool:
    if detector_name == 'cache':
        return not raw_result.get('uses_cache', False)
    if detector_name == 'cache_smell':
        return raw_result.get('has_smell', False)
    if detector_name == 'deepclone':
        return raw_result.get('has_deep_clone', False)
    if detector_name == 'redundant_trigger':
        return raw_result.get('has_redundant_trigger', False)
    if detector_name == 'timeout':
        return raw_result.get('has_timeout_smell', False)
    return raw_result.get('has_smell', False)


def scan_yml_file(yml_path: Path, detectors: list, workflow_path: Optional[str] = None) -> dict:
    detector_results = {}
    all_smells = []

    for detector_name, detector in detectors:
        try:
            if detector_name in ('cache', 'cache_smell'):
                if workflow_path:
                    # 使用 workflow_path 参数名（与 WorkflowModel 字段匹配）
                    raw = detector.detect_from_file(yml_path, workflow_path=workflow_path)
                else:
                    raw = detector.detect_from_file(yml_path)
            else:
                raw = detector.detect_from_file(yml_path)

            # 处理新架构检测器返回的 DetectorResult 对象
            from src.detectors.base import DetectorResult
            if isinstance(raw, DetectorResult):
                raw_dict = raw.to_dict()
                # 对于 cache_smell，保持与旧格式兼容
                if detector_name == 'cache_smell':
                    raw = {
                        'has_smell': raw.has_smell,
                        'smells': [s.to_dict() for s in raw.smells],
                        'metadata': raw.metadata,
                        'error': raw.error,
                    }
                elif detector_name == 'cache':
                    raw = raw_dict  # cache 使用新格式
                else:
                    raw = raw_dict

            has_smell = normalize_has_smell(detector_name, raw)
            smells, cache_types, cache_categories, cache_jobs, jobs = normalize_smells(detector_name, raw)

            if detector_name == 'cache':
                detector_results[detector_name] = {
                    'use_cache': raw.get('uses_cache', False),
                }
                if cache_types:
                    detector_results[detector_name]['cache_types'] = cache_types
                if cache_categories:
                    detector_results[detector_name]['cache_categories'] = cache_categories
                if cache_jobs:
                    detector_results[detector_name]['cache_jobs'] = cache_jobs
                if jobs:
                    detector_results[detector_name]['jobs'] = jobs
            else:
                detector_results[detector_name] = {
                    'has_smell': has_smell,
                    'smells': smells,
                }
                if detector_name == 'cache_smell':
                    if jobs:
                        detector_results[detector_name]['jobs'] = jobs
                    # 保存 metadata 用于流行度分析
                    if 'metadata' in raw:
                        detector_results[detector_name]['metadata'] = raw['metadata']

            if raw.get('error'):
                detector_results[detector_name]['error'] = raw['error']

            if detector_name != 'cache':
                all_smells.extend(smells)
        except Exception as e:
            if detector_name == 'cache':
                detector_results[detector_name] = {
                    'use_cache': False,
                    'error': str(e),
                }
            else:
                detector_results[detector_name] = {
                    'has_smell': False,
                    'smells': [],
                    'error': str(e),
                }

    smells_by_severity = defaultdict(int)
    smells_by_type = defaultdict(int)
    for smell in all_smells:
        sev = smell.get('severity')
        typ = smell.get('type', 'unknown')
        if sev is not None:
            smells_by_severity[sev] += 1
        smells_by_type[typ] += 1

    return {
        'detectors': detector_results,
        'summary': {
            'total_smells': len(all_smells),
            **({'smells_by_severity': dict(smells_by_severity)} if smells_by_severity else {}),
            'smells_by_type': dict(smells_by_type),
        }
    }


def load_existing_results(output_file: Path) -> tuple:
    if not output_file.exists():
        return {}, set()
    records = {}
    scanned_hashes = set()
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = loads_smell_json(line)
                    yml_hash = list(record.keys())[0]
                    records[yml_hash] = record[yml_hash]
                    scanned_hashes.add(yml_hash)
                except json.JSONDecodeError:
                    continue
        print(f"从已有结果恢复 {len(records)} 条记录")
        return records, scanned_hashes
    except Exception as e:
        print(f"已有结果文件损坏，从头开始: {e}")
        return {}, set()


def append_record(output_file: Path, yml_hash: str, record: dict):
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps({yml_hash: record}, ensure_ascii=False) + '\n')


def write_summary(summary_file: Path, config: dict, stats: dict):
    output = {
        'version': '1.0',
        'scan_timestamp': datetime.now().isoformat(),
        'config': config,
        'stats': stats,
    }
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = summary_file.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    tmp.replace(summary_file)


def build_reusable_map(run_workflow_map, workflow_runs_map):
    reusable_map = {}
    for reusable_yml, run_id_entries in workflow_runs_map.items():
        for entry in run_id_entries:
            if not isinstance(entry, str) or not entry.startswith('ref:'):
                continue
            parts = entry.split(':', 2)
            if len(parts) < 3:
                continue
            parent_run_id = parts[1]
            uses_path = parts[2]
            main_yml = run_workflow_map.get(parent_run_id)
            if not main_yml:
                continue
            if main_yml not in reusable_map:
                reusable_map[main_yml] = {}
            reusable_map[main_yml][uses_path] = reusable_yml
    return reusable_map


# ========== 新数据源：可复用工作流映射构建 ==========

def build_reusable_map_from_jsonl(data_dir: Path) -> dict:
    """
    从 workflow_files.jsonl 构建可复用工作流映射。

    新数据源中，同一仓库的工作流文件都在 workflows/{owner}/{repo}/ 下，
    可以通过匹配 uses: ./.github/workflows/xxx.yml 来关联。

    Returns:
        dict: main_content_file -> {uses_path -> reusable_content_file}
    """
    workflow_files_jsonl = data_dir / 'workflow_files.jsonl'
    if not workflow_files_jsonl.exists():
        return {}

    # 按 (owner, repo) 分组收集工作流
    repo_workflows = defaultdict(list)  # (owner, repo) -> [{workflow_path, content_file}, ...]
    with open(workflow_files_jsonl, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = loads_smell_json(line)
            repo_name = entry.get('repository_name', '')
            if '/' in repo_name:
                owner, repo = repo_name.split('/', 1)
            else:
                owner, repo = repo_name, ''
            repo_workflows[(owner, repo)].append({
                'workflow_path': entry.get('workflow_path', ''),
                'content_file': entry.get('content_file', ''),
            })

    # 构建映射：对于每个仓库，建立 workflow_path -> content_file 的查找表
    reusable_map = {}
    for (owner, repo), workflows in repo_workflows.items():
        path_to_content = {}
        for wf in workflows:
            # workflow_path 如 ".github/workflows/ci.yml"
            path_to_content[wf['workflow_path']] = wf['content_file']

        # 对于该仓库的每个工作流，如果它引用了本地可复用工作流，建立映射
        for wf in workflows:
            content_file = wf['content_file']
            # 这里只能建立静态映射，实际 uses 引用需要解析 YAML 才能知道
            # 但我们可以在扫描时动态解析，这里先返回空映射
            # 实际的可复用工作流解析在检测器中通过 yml_rel_path 完成

    return reusable_map


# ========== 新数据源：文件列表加载 ==========

def load_new_data_source(data_dir: Path, use_filtered: bool = False) -> tuple:
    """
    加载新数据源的待扫描工作流列表。

    Args:
        data_dir: 数据目录 (如 data_26_6_9)
        use_filtered: 是否使用归档前的 cache_relevant_workflows.jsonl，否则使用当前全量索引

    Returns:
        (workflows_list, workflows_dir, reusable_map)
        workflows_list: [{content_file, repository_name, workflow_path, ...}, ...]
        workflows_dir: Path
        reusable_map: dict
    """
    workflows_dir = data_dir / 'workflows'

    if use_filtered:
        input_file = data_dir / 'archive' / 'early_data_20260908' / 'cache_relevant_workflows.jsonl'
        if not input_file.exists():
            # 兼容归档前的本地布局；当前默认路径仍然是全量索引。
            old_input_file = data_dir / 'cache_relevant_workflows.jsonl'
            if old_input_file.exists():
                input_file = old_input_file
            else:
                print(f"归档筛选文件不存在: {input_file}，回退到全量数据")
                input_file = data_dir / 'workflow_files.jsonl'
    else:
        input_file = data_dir / 'workflow_files.jsonl'

    if not input_file.exists():
        print(f"错误: 数据文件不存在 {input_file}")
        return [], workflows_dir, {}

    print(f"读取工作流列表: {input_file}")
    workflows_list = []
    seen = set()  # 按 (repository_name, workflow_path) 去重
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = loads_smell_json(line)
            content_file = entry.get('content_file', '')
            workflow_key = logical_workflow_key(entry)
            if content_file and workflow_key not in seen:
                seen.add(workflow_key)
                workflows_list.append(entry)

    print(f"  待扫描工作流数: {len(workflows_list)}")

    # 构建可复用工作流映射
    reusable_map = build_reusable_map_from_jsonl(data_dir)

    return workflows_list, workflows_dir, reusable_map


# ========== 旧数据源：文件列表加载 ==========

def load_legacy_data_source() -> tuple:
    """
    加载旧数据源的待扫描工作流列表（兼容原有逻辑）。

    Returns:
        (yml_to_run_ids, workflows_dir, reusable_map)
        yml_to_run_ids: {yml_rel_path: [run_id, ...]}
        workflows_dir: Path
        reusable_map: dict
    """
    mapping_file = settings.workflow_coverage_mapping_file or settings.workflow_fetch_output_dir / 'run_workflow_mapping.json'
    workflows_dir = settings.workflow_fetch_output_dir / 'workflows'

    print(f"映射文件: {mapping_file}")
    print(f"工作流目录: {workflows_dir}")

    if not mapping_file.exists():
        print(f"错误: 映射文件不存在 {mapping_file}")
        return {}, workflows_dir, {}

    print("读取映射文件...")
    with open(mapping_file, 'r', encoding='utf-8') as f:
        run_workflow_map = load_smell_json(f)

    workflow_map_file = settings.workflow_fetch_output_dir / 'workflow_runs_mapping.json'
    reusable_map = {}
    if workflow_map_file.exists():
        print("读取工作流映射文件...")
        with open(workflow_map_file, 'r', encoding='utf-8') as f:
            workflow_runs_map_data = load_smell_json(f)
        reusable_map = build_reusable_map(run_workflow_map, workflow_runs_map_data)
        print(f"可复用工作流映射: {sum(len(v) for v in reusable_map.values())} 条引用关系")

    yml_to_run_ids = defaultdict(list)
    for run_id, yml_rel_path in run_workflow_map.items():
        yml_to_run_ids[yml_rel_path].append(run_id)

    return yml_to_run_ids, workflows_dir, reusable_map


# ========== 主流程 ==========

def compute_hash(key: str) -> str:
    return hashlib.md5(key.encode('utf-8')).hexdigest()[:12]


def logical_workflow_key(entry: dict) -> str:
    """返回逻辑工作流键，不把共享内容文件误当成工作流身份。"""
    repository_name = entry.get('repository_name', '')
    workflow_path = entry.get('workflow_path', '')
    if repository_name or workflow_path:
        return f"{repository_name}|{workflow_path}"
    return entry.get('content_file', '')


def main():
    parser = argparse.ArgumentParser(description='扫描工作流异味（兼容新旧数据源）')
    parser.add_argument('--data-dir', type=str, default=str(_PORTABLE_PROJECT_ROOT / 'data_26_6_9'),
                        help='数据源目录，默认新数据源')
    parser.add_argument('--output-dir', type=str, default=str(_PORTABLE_PROJECT_ROOT / 'output_new'),
                        help='输出目录，默认 output_new')
    parser.add_argument('--legacy', action='store_true',
                        help='使用旧数据源（settings 中的配置）')
    parser.add_argument('--use-filtered', action='store_true',
                        help='使用早期 cache_relevant_workflows.jsonl 筛选结果（默认使用全量 workflow_files.jsonl）')
    parser.add_argument('--no-filter', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--detectors', nargs='*', default=DEFAULT_DETECTORS,
                        choices=list(ALL_DETECTORS.keys()),
                        help=f'指定使用的检测器，默认全部。可选: {list(ALL_DETECTORS.keys())}')
    parser.add_argument('--force', action='store_true',
                        help='强制重新扫描所有文件，忽略已有结果')
    args = parser.parse_args()

    print(f"使用检测器: {args.detectors}", flush=True)
    if args.force:
        print("强制模式: 忽略已有结果，重新扫描所有文件", flush=True)

    is_new_source = not args.legacy

    if is_new_source:
        data_dir = Path(args.data_dir)
        use_filtered = args.use_filtered and not args.no_filter
        workflows_list, workflows_dir, reusable_map = load_new_data_source(data_dir, use_filtered)

        if not workflows_list:
            print("没有待扫描的工作流")
            return

        output_file = Path(args.output_dir) / 'smell_scan_results.jsonl'
        summary_file = Path(args.output_dir) / 'smell_scan_summary.json'

        # 构建扫描项：key = 逻辑工作流，内容从 content_file 读取。
        # 同一 YAML 内容可能被多个仓库工作流共享，不能按 content_file 合并。
        scan_items = []
        for entry in workflows_list:
            content_file = entry.get('content_file', '')
            if not content_file:
                continue
            workflow_key = logical_workflow_key(entry)
            scan_items.append({
                'key': workflow_key,
                'content_file': content_file,
                'yml_path': data_dir / content_file,
                'yml_rel_path': content_file,  # 传给检测器用于可复用工作流解析
                'repository_name': entry.get('repository_name', ''),
                'workflow_path': entry.get('workflow_path', ''),
                'workflow_id': entry.get('workflow_id'),
                'file_sha': entry.get('file_sha'),
            })
    else:
        yml_to_run_ids, workflows_dir, reusable_map = load_legacy_data_source()

        if not yml_to_run_ids:
            print("没有待扫描的工作流")
            return

        output_file = settings.output_dir / 'smell_scan_results.jsonl'
        summary_file = settings.output_dir / 'smell_scan_summary.json'

        scan_items = []
        for yml_rel_path, run_ids in yml_to_run_ids.items():
            yml_path = workflows_dir / Path(yml_rel_path).name
            scan_items.append({
                'key': yml_rel_path,
                'yml_path': yml_path,
                'yml_rel_path': yml_rel_path,
                'run_ids': run_ids,
            })

    print(f"工作流目录: {workflows_dir}", flush=True)
    print(f"输出文件: {output_file}", flush=True)
    print(f"待扫描: {len(scan_items)}", flush=True)

    # 初始化检测器
    detectors = []
    for name in args.detectors:
        if name == 'cache':
            detector = CacheDetector(workflows_dir=workflows_dir, reusable_map=reusable_map)
        elif name == 'cache_smell':
            detector = CacheSmellDetector(workflows_dir=workflows_dir, reusable_map=reusable_map)
        else:
            detector = ALL_DETECTORS[name]()
        detectors.append((name, detector))

    # 增量扫描
    valid_hashes = set()
    for item in scan_items:
        valid_hashes.add(compute_hash(item['key']))

    if args.force:
        already_scanned = set()
        if output_file.exists():
            output_file.unlink()
    else:
        _, already_scanned = load_existing_results(output_file)
        stale = already_scanned - valid_hashes
        if stale:
            print(f"移除 {len(stale)} 条过期记录", flush=True)
            already_scanned -= stale

    scanned = len(already_scanned)
    skipped = 0
    newly_scanned = 0
    total_smells = 0
    smells_by_severity = defaultdict(int)
    smells_by_detector = defaultdict(int)

    config = {
        'data_source': 'new' if is_new_source else 'legacy',
        'data_dir': str(args.data_dir) if is_new_source else str(settings.workflow_fetch_output_dir),
        'workflows_dir': str(workflows_dir),
        'detectors': args.detectors,
        'use_filtered': use_filtered if is_new_source else False,
    }

    start_time = time.time()

    for item in scan_items:
        yml_hash = compute_hash(item['key'])

        if yml_hash in already_scanned:
            continue

        yml_path = item['yml_path']
        if not yml_path.exists():
            skipped += 1
            continue

        scan_result = scan_yml_file(yml_path, detectors, workflow_path=item.get('yml_rel_path'))

        record = {
            'yml_file': item.get('content_file', item['key']),
            'workflow_key': item['key'],
            'detectors': scan_result['detectors'],
            'summary': scan_result['summary'],
        }

        # 附加数据源特有字段
        if is_new_source:
            record['repository_name'] = item.get('repository_name', '')
            record['workflow_path'] = item.get('workflow_path', '')
            if item.get('workflow_id'):
                record['workflow_id'] = item['workflow_id']
            if item.get('file_sha'):
                record['file_sha'] = item['file_sha']
        else:
            record['run_ids'] = item.get('run_ids', [])

        append_record(output_file, yml_hash, record)
        already_scanned.add(yml_hash)

        total_smells += scan_result['summary']['total_smells']
        for sev, count in scan_result['summary'].get('smells_by_severity', {}).items():
            smells_by_severity[sev] += count
        for det_name, det_result in scan_result['detectors'].items():
            smells_by_detector[det_name] += len(det_result.get('smells', []))

        scanned += 1
        newly_scanned += 1
        if newly_scanned % 2000 == 0:
            stats = {
                'total_yml_files': len(scan_items),
                'scanned_yml_files': scanned,
                'skipped_yml_files': skipped,
                'total_smells': total_smells,
                **({'smells_by_severity': dict(smells_by_severity)} if smells_by_severity else {}),
                'smells_by_detector': dict(smells_by_detector),
                'scan_time_seconds': round(time.time() - start_time, 1),
            }
            write_summary(summary_file, config, stats)
            elapsed = time.time() - start_time
            rate = newly_scanned / elapsed if elapsed > 0 else 0
            remaining = (len(scan_items) - scanned) / rate if rate > 0 else 0
            print(f"  已扫描 {scanned}/{len(scan_items)} (新增{newly_scanned}, {rate:.0f}/s, 剩余~{remaining:.0f}s)", flush=True)

    elapsed = time.time() - start_time

    stats = {
        'total_yml_files': len(scan_items),
        'scanned_yml_files': scanned,
        'skipped_yml_files': skipped,
        'total_smells': total_smells,
        **({'smells_by_severity': dict(smells_by_severity)} if smells_by_severity else {}),
        'smells_by_detector': dict(smells_by_detector),
        'scan_time_seconds': round(elapsed, 1),
    }
    write_summary(summary_file, config, stats)

    print(f"\n扫描完成 ({elapsed:.1f}s)", flush=True)
    print(f"  扫描: {scanned} (新增{newly_scanned}), 跳过: {skipped}", flush=True)
    print(f"  总异味数: {total_smells}", flush=True)
    if smells_by_severity:
        print(f"  按严重程度: {dict(smells_by_severity)}", flush=True)
    print(f"  按检测器: {dict(smells_by_detector)}", flush=True)
    print(f"  结果文件: {output_file}", flush=True)
    print(f"  汇总文件: {summary_file}", flush=True)


if __name__ == '__main__':
    main()
