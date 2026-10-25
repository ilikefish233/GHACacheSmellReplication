# scripts/data_update/filter_config.py
"""
数据过滤配置。
修改此文件即可调整过滤规则，无需修改主脚本。
"""

# ─── 仓库级过滤 ──────────────────────────────────────
# 满足任一条件的仓库将被过滤（不采集后续数据）

# 1. 停止维护的仓库
FILTER_ARCHIVED_REPOS = True

# 2. 90天内运行次数少于阈值的仓库
FILTER_BY_RECENT_RUNS = True
RECENT_RUNS_DAYS = 90           # 统计最近多少天的运行
RECENT_RUNS_MIN_COUNT = 30      # 最低运行次数阈值

# ─── 工作流文件级过滤 ────────────────────────────────
# 满足任一条件的工作流文件将被过滤

# 3. 没有对应运行记录的工作流文件
FILTER_WF_WITHOUT_RUNS = True

# ─── 过滤结果记录 ────────────────────────────────────
# 被过滤掉的仓库/文件会记录到 filtered_out.jsonl，便于审计
