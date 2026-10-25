# EXPERIMENTAL：只按异味检测状态变化纳入的历史前后资源差额

2026-10-19。用户要求放宽配置匹配；旧实验、检测器、正式数据和论文未修改，无 RQ3。

## 结果与样本

本轮复用同一当前检测器历史扫描，不再限制任务、job、runner、矩阵、工具版本、触发事件一致性或对照适用性；每侧全部本地资源完整成功运行均可用，不限十次，不限30/90天。保留同工作流身份、历史有效区间和可计算资源这些基础条件。

候选 1957 个；最终 **166 个事件、88 个仓库、1268 次成功运行**，覆盖原十一类中的 9 类及额外检测器类型 3 类。逐类成员关系 203 条，不可相加为独立事件。

对照只表示“未检出该异味”。原本适用性不足、自动缓存未知等状态被保留为标记而非排除条件，不表示已经证明没有异味或修复成功。107 个最终事件主要任务改变或可比性无法确认；53 个事件至少一侧只有一次观测。

## 差额

方向固定为“检出侧 − 未检出侧”。事件内比较两侧成功运行均值；仓库内平均事件，再仓库等权；2,000 次仓库 bootstrap、种子20261019、逐项95%区间，单仓库不报告总体区间。不做同时检验确认，不将各类型相加。

| 异味 | 事件/仓库 | ΔVM秒/成功运行 | Δ参考美元/成功运行 | 成本逐项95%区间 |
|---|---:|---:|---:|---|
| no_dependency_cache | 106/59 | +17.29 | +0.00859 | [-0.02906, +0.04633] |
| no_fallback_cache | 46/24 | +161.91 | -0.01651 | [-0.10027, +0.06067] |
| redundant_cache_update | 7/6 | +183.41 | +0.08750 | [+0.00233, +0.21723] |
| cache_position_error | 2/2 | -830.75 | -0.13533 | [-0.30467, +0.03400] |
| conflicting_cache_sources | 8/6 | +60.69 | +0.00917 | [-0.00668, +0.02639] |
| static_cache_key | 17/6 | +169.99 | +0.03594 | [-0.00427, +0.10554] |
| overly_specific_cache_key | 4/4 | +431.96 | +0.06123 | [+0.01073, +0.12250] |
| no_docker_layer_cache | 6/4 | +699.78 | +0.09496 | [+0.02644, +0.18070] |
| deprecated_cache_action | 0/0 | NA | NA | — |
| unreachable_cache_write | 0/0 | NA | NA | — |
| cache_as_artifact | 1/1 | -680.00 | -0.07600 | — |
| cache_path_pollution | 1/1 | -20.67 | -0.00800 | — |
| incorrect_cache_hit_condition | 4/2 | +1204.54 | +0.16267 | [-0.01400, +0.33933] |
| malformed_cache_path | 0/0 | NA | NA | — |
| non_portable_cache_path | 0/0 | NA | NA | — |
| overly_broad_cache_path | 1/1 | +207.75 | +0.08200 | — |

## 版本与成本口径

运行以同一默认分支文件历史的有效时间区间归属，不再把缺少精确 head-SHA 证据当作排除条件。已有明确、适用于该事件类型的内容证据若与目标历史内容冲突，作为版本归属错误排除；其他事件类型不把 head 当作 merge 执行版本证明。最终运行/事件侧的版本证据计数：`{"exact_head_content_confirmed": 439, "history_interval_only": 884}`。同一运行可位于相邻事件两侧，计数不是独立运行数。

VM 为各已分配 runner 的 job 秒数之和；参考美元沿用 `0.008 * ceil(seconds / 60 * OS倍率)`，Linux/Windows/macOS=1/2/10，逐job计算。是历史统一标准金额，不是当前报价、实际账单或修复节省额；未知价格类别、缺失job/时间和空档案不能按零处理。不含存储，不还原更大runner/ARM特殊价格。资源排除：`{"runner_allocation_unknown": 3, "self_hosted_runner": 7, "unknown_pricing_category": 4}`。

## 数据覆盖和解释

API调用0、工作流重放0；仅复用现有本地job，旧采集受历史候选和资源样本选择影响，不代表完整历史或随机总体。没有前后运行/资源的候选无法通过放宽配置规则补出数值；原因计数在 run_coverage_counts.json，逐事件在 run_screening.jsonl。

本轮估计的是**检测状态变化事件伴随的成功运行成本差额**。任务、OS、矩阵、依赖、工具及其他异味均可能同变，加上时间区间归属和未知状态，不能把全部差额归因于目标异味。正负结果完整报告；NA不是零影响。此前更严格实验不被覆盖，本轮不声称仅由放宽某一个条件造成样本或差额变化。

## 复算与核对

冻结候选和运行在计算VM/美元前保存，哈希见 freeze_audit.json。必要身份、区间、去重、符号和仓库汇总核对通过，见 necessary_checks.json；没有额外代码质量测试。

首次执行命令：`python -B E:\gha_log_analysis\output_experimental_rq23_history_relaxed_20261019\run_relaxed.py --stage prepare`。已完成目录不能重新选样；离线复算命令：`python -B E:\gha_log_analysis\output_experimental_rq23_history_relaxed_20261019\run_relaxed.py --stage calculate`。复用的输入位置、源码哈希和原始档案偏移均保留，未复制大量YAML。
