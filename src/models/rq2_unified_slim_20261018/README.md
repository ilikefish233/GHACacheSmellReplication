# 实验性 RQ2 精简统一模型

本目录是独立实验入口，原版 scripts/analyze_rq2_run_level.py、scripts/analyze_rq2_regression.py 和冻结结果保持不变。本模型不替换正式结果，不执行 RQ3。

## 模型与输入

- 默认输入是 output_experimental_rq1_rq2_20261018_run3/rq2_model_input 中已经扫描过的检测标签和固定运行队列。不会重新运行当前检测器，避免把本次模型变化与后续检测器变化混合；来源 detector_version 保存在 workflow_features.jsonl。
- 同时纳入 11 类异味，统一使用 no_fallback_cache。旧标识通过 src/smell_names.py 兼容读取。
- 每个端点使用全体可分析运行，不按目标异味是否适用筛选；检测错误、配置特征解析错误、非法结局/时长仍需排除并报告。
- 三个统一端点：全部完成运行 log 时长；成功运行 log 时长（敏感性）；success 对 failure/startup_failure/timed_out 的 logistic。
- 精简控制：log1p(声明 Job 数)、log1p(非显式 actions/cache 步骤数)、matrix 标志、工作流功能、技术生态、runner OS/托管类型、实际触发事件、固定周类别。
- 不加入缓存 Job 数、版本年龄、运行顺序、首次观测标志、stars、仓库年龄等扩展控制，也不加入 CCS 总数。
- 不加入实际执行后测量的 Job 数/测试数/缓存命中信息。变量规则在 features.py；功能类别基于工作流名称、路径、Job 名称的启发式规则，OS 与功能分开，尚不是经人工验证的语义真值；引用的外部工作流内部配置不可见时保留 unknown。
- runner 的简单 matrix.os 引用可从静态列表解析；动态表达式、include/exclude 的复杂组合不强制展开。matrix 标志不是实际执行矩阵规模。
- 保留端点内版本均衡权重、仓库聚类 sandwich 标准误和各端点可估计 CCS 系数的 BH-FDR。

## 为什么更快

不再为每类目标异味重复构建设计矩阵和拟合。相同版本、事件、周的运行具有完全相同的预测变量，因此按这些键保存计数、成功数、log 时长之和与平方和。此汇总是相同设计向量的充分统计量计算，不是把不同任务随意求均值。

汇总后的拟合与展开到运行级的加权线性/二项回归、仓库聚类协方差在数值精度内一致；端点内版本权重也不变。unittest 验证上述等价性及仓库重抽样。每个仓库的一组运行仍共享同一个抽样倍数。

主推断使用聚类稳健标准误，不要求 bootstrap。默认 --bootstrap 0，明确不生成 bootstrap 敏感性区间；这不是把少量重抽样伪称为原版 B1000。可用 --bootstrap 1000 在新结果目录独立生成敏感性区间，已有分组数据可复用，输入文件与代码哈希必须匹配。

## 运行

在项目根目录运行。下面是本次结果目录名，复现时请换成新的空目录名：

```powershell
python experiments/rq2_unified_slim_20261018/run.py --output-dir output_experimental_rq2_unified_slim_20261018_results --bootstrap 0
```

可选 bootstrap（使用另一个空目录；运行可能较久）：

```powershell
python experiments/rq2_unified_slim_20261018/run.py --output-dir output_experimental_rq2_unified_slim_20261018_b1000 --reuse-prepared output_experimental_rq2_unified_slim_20261018_results --bootstrap 1000
```

统计等价性与特征测试：

```powershell
python -m unittest discover -s experiments/rq2_unified_slim_20261018 -p test_model.py -v
```

如沙箱不允许系统临时目录中的原子改名，可将测试临时目录设在工作区（不改变拟合或断言）：

```powershell
python -c "import tempfile,pathlib,sys,unittest; tempfile.tempdir=str(pathlib.Path('experiments/rq2_unified_slim_20261018').resolve()); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('experiments/rq2_unified_slim_20261018',pattern='test_model.py')); sys.exit(not result.wasSuccessful())"
```

--reuse-features 可复用已核实的特征缓存并重试缺失项；输入哈希和 features.py 规则哈希必须匹配。本次先进行全量提取，再复用缓存恢复两个规范层路径缺失但本地同哈希 YAML 尚存的工作流，最终结果写入带 _results 后缀的目录。无后缀目录是首轮诊断，_final 是一次缓存恢复实现失败的记录，均不是最终结果入口。

程序拒绝覆盖已有结果目录。manifest.json 记录输入/代码哈希、各阶段耗时、完成端点和最终状态。latest_run.log 是本次启动日志，不是原版结果。

## 成功模型的分离诊断

普通 logistic 检查收敛、信息矩阵和暴露组是否全成功/全失败。后者不是穷尽所有准分离情形的检测。若存在分离或未收敛，整个普通模型的正式 p/q 值被抑制，不把极端 OR 当成可靠效应；线性系数也需通过设计秩和别名检查。

另生成统一 Firth logistic 敏感性模型（复用原版数学函数），其区间只具有模型型含义，不声称为仓库聚类稳健区间，不进入正式 BH 家族。分离受限的普通模型不运行普通 bootstrap。不得沿用旧版逐异味 Firth 的数值。

## 输出与限制

- workflow_features.jsonl、feature_audit.json：控制变量与分类覆盖/错误。
- group_keys.jsonl、grouped_outcomes.npz：精确分组输入。
- sample_audit.json：运行排除及端点基础计数。
- all_completed_duration.json、successful_duration.json、reliability.json：统一模型系数、聚类区间、q 值和耗时。
- reliability_firth_sensitivity.json：仅在分离/不收敛触发时生成。
- manifest.json：status=complete 表示程序结束，不表示所有结局都获得有效普通回归推断；应同时查看各端点 status。

不估计因果效应；未测量代码/测试规模、动态条件分支、外部脚本和复用工作流仍是局限。unknown 是显式类别，不代表缺失值机制已经解决。检验变量变换和分类合理性、检查暴露支持与小样本风险后，才考虑将实验结果用于论文。
