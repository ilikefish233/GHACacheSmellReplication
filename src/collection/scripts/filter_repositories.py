# scripts/filter_repositories.py
from pathlib import Path as _PortablePath
_PORTABLE_PROJECT_ROOT = _PortablePath(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(_PORTABLE_PROJECT_ROOT))

import json
from pathlib import Path
from src.loaders.json_loader import JSONLoader
from src.utils.helpers import save_json, ensure_dir
from config.settings import Settings
from datetime import datetime

def filter_repositories(data_dir: Path, output_dir: Path):
    print("=" * 60)
    print("仓库筛选 - 策略C")
    print("=" * 60)
    
    repos_file = data_dir / "repositories.json"
    if not repos_file.exists():
        print(f"错误: 文件不存在 {repos_file}")
        return
    
    loader = JSONLoader(repos_file)
    
    print(f"\n加载数据: {repos_file}")
    print("筛选条件:")
    print("  - selected == True")
    print("  - processed == True")
    print("  - repo.isFork == False")
    print("  - repo.isArchived == False")
    print("  - repo.stargazers > 100")
    print("  - total_runs_90d > 50")
    print("  - repo.commits > 100")
    
    filtered = []
    total_count = 0
    stats = {
        'total': 0,
        'filtered': 0,
        'rejected_reasons': {
            'not_selected': 0,
            'not_processed': 0,
            'is_fork': 0,
            'is_archived': 0,
            'low_stars': 0,
            'low_runs': 0,
            'low_commits': 0,
        }
    }
    
    for repo in loader.iter_records():
        stats['total'] += 1
        total_count += 1
        
        if total_count % 10000 == 0:
            print(f"已处理 {total_count} 条记录...")
        
        if not repo.get('selected', False):
            stats['rejected_reasons']['not_selected'] += 1
            continue
        
        if not repo.get('processed', False):
            stats['rejected_reasons']['not_processed'] += 1
            continue
        
        repo_info = repo.get('repo') or {}
        
        if repo_info.get('isFork'):
            stats['rejected_reasons']['is_fork'] += 1
            continue
        
        if repo_info.get('isArchived'):
            stats['rejected_reasons']['is_archived'] += 1
            continue
        
        if (repo_info.get('stargazers') or 0) <= 100:
            stats['rejected_reasons']['low_stars'] += 1
            continue
        
        if (repo.get('total_runs_90d') or 0) <= 50:
            stats['rejected_reasons']['low_runs'] += 1
            continue
        
        if (repo_info.get('commits') or 0) <= 100:
            stats['rejected_reasons']['low_commits'] += 1
            continue
        
        filtered.append(repo)
        stats['filtered'] += 1
    
    print(f"\n筛选完成!")
    print(f"原始仓库数: {stats['total']}")
    print(f"筛选后仓库数: {stats['filtered']}")
    print(f"筛选比例: {stats['filtered'] / max(stats['total'], 1) * 100:.2f}%")
    
    print(f"\n拒绝原因统计:")
    for reason, count in stats['rejected_reasons'].items():
        print(f"  - {reason}: {count}")
    
    output_dir = ensure_dir(output_dir)
    
    output_file = output_dir / "filtered_repositories.json"
    with open(output_file, 'w', encoding='utf-8', newline='\n') as f:
        for repo in filtered:
            f.write(json.dumps(repo, ensure_ascii=False) + '\n')
    print(f"\n筛选结果已保存: {output_file}")
    
    stats_file = output_dir / "filter_stats.json"
    stats['filter_time'] = datetime.now().isoformat()
    stats['filter_criteria'] = {
        'selected': True,
        'processed': True,
        'isFork': False,
        'isArchived': False,
        'stargazers': '> 100',
        'total_runs_90d': '> 50',
        'commits': '> 100',
    }
    save_json(stats, stats_file)
    print(f"统计信息已保存: {stats_file}")
    
    return filtered, stats

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="筛选仓库")
    parser.add_argument('--data-dir', type=str, help='数据目录路径（可选，默认使用配置文件）')
    parser.add_argument('--output-dir', type=str, help='输出目录路径（可选，默认使用配置文件）')
    args = parser.parse_args()
    
    settings = Settings()
    
    data_dir = Path(args.data_dir) if args.data_dir else settings.data_dir
    output_dir = Path(args.output_dir) if args.output_dir else settings.processed_data_dir
    
    filter_repositories(
        data_dir=data_dir,
        output_dir=output_dir
    )

if __name__ == "__main__":
    main()