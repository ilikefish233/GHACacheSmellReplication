# 实验性：统一成功率线性概率模型

仅替换实验性成功率模型。原 `experiments/rq2_unified_slim_20261018` 的 logistic/Firth 代码和结果保留，时长模型不重跑。

## 运行

在仓库根目录执行（输出目录必须不存在）：

```powershell
python experiments/rq2_unified_lpm_20261019/test_lpm.py
python experiments/rq2_unified_lpm_20261019/run.py --output-dir output_experimental_rq2_unified_lpm_20261019_final
```

复用旧实验 `output_experimental_rq2_unified_slim_20261018_results` 的精确分组统计量，并校验旧代码、原始运行记录及检测结果的 SHA256。旧实验代码是显式依赖，保持原样，不复制修改。

## 统计设计

- 端点：success=1；failure/startup_failure/timed_out=0；不把 cancelled/skipped/neutral 等算作失败。
- 全总体统一模型；同时考虑 11 个异味，不按异味适用性分别筛选。
- 每个工作流版本在该端点的运行权重之和为 1，与旧成功率模型一致。
- 控制变量、分类编码和设计矩阵沿用旧精简模型：job 数、非显式缓存步骤数（log1p 后标准化）、matrix、工作流角色、依赖生态、runner OS/hosting、触发事件和日历周。
- 加权最小二乘；按仓库聚类 sandwich 协方差，有限簇修正 G/(G-1)；双侧 t(G-1) 检验及点态 95% 区间。此处明确采用该修正，不混用 grouped-row 和 run-row 的自由度修正。
- 仅对可辨识的异味系数进行 BH-FDR；当前 unreachable_cache_write 在端点内无暴露，不能检验，因此可检验的假设为 10 个。CSV/JSON 仍列出全部 11 个异味。
- 不运行 bootstrap；区间是点态区间，显著性按 q<0.05 判定。
- 二元数据的平方和等于成功数，复用精确分组不损失逐运行 WLS 系数或仓库聚类分数。测试独立展开 0/1 运行，用加权最小二乘及手工 cluster score 核对，并包含暴露组全失败的情况。
- 输出概率差和百分点差，不指数化系数，不把 LPM 系数解释成 OR。

## 边界

这是观察性调整后线性关联；并未证明异味导致失败。线性加性设定可能导致预测值超出 [0,1]，原样报告其比例，不裁剪后重新计算。仓库聚类稳健标准误不解决遗漏混杂或模型设定错误。对于废弃 action 等只出现在少量仓库的暴露，常规聚类检验存在额外不确定性；暴露仓库数随结果一并报告。

运行输出包括 `reliability_lpm.json`、`smell_coefficients.csv`、结果说明 `README.md`、输入/代码哈希 `manifest.json`、原结果未改动验证 `original_result_integrity.json`。旧时长结果仍通过 manifest 指向原实验目录，不覆盖旧结果。
