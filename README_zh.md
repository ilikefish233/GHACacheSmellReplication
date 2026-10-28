# GitHub Actions 缓存配置异味研究复现包

本仓库为论文 **Cache Configuration Smells in GitHub Actions: A Large-Scale
Empirical Study** 提供代码、人工验证材料和小型结果表，用于 *Empirical Software
Engineering*（EMSE）投稿。英文主说明见 [README.md](README.md)。

## 概览

目录参照 [GHALincesepack](https://github.com/xchu7504-sys/GHALincesepack)，按研究问题
及子问题组织，支持复现：

- 检测器人工验证集评估。
- RQ1：异味流行度、使用情境、多重异味和共现。
- RQ2.1：异味与工作流运行时长的调整后关联。
- RQ2.2：异味与工作流运行成功率的调整后关联。
- RQ2.3：探索性的资源消耗和历史参照成本比较。
- 数据采集、候选筛选、补抓和离线清洗流程。

本包使用论文采用的 10 月 run3 冻结检测器、统一时长模型和成功率 LPM。
正式范围仅 RQ1 和 RQ2；RQ2.3 属于 RQ2，已封存的 RQ3 不恢复。

**这是 GitHub 代码版。** 代码、人工验证工作簿和小型预期结果已提供。
RQ 分析需要的大型数据由单独的完整 ZIP 接入；验证集评估可以直接运行。

## 目录内容

顶层统一为 `RQ1/`、`RQ2/`、`Validation/`、`Pipeline/`、`src/`、`data/`、`docs/` 和 `LICENSES/`。共享实现全部归入 `src/`，来源和报告归入 `docs/`。

| 目录或文件 | 内容与用途 |
| --- | --- |
| `Validation/` | 383 个工作流的标注、完整 YAML 补充快照、冻结检测器评估和预期指标；主指标按工作流匹配 |
| `RQ1/RQ1_1/` | 流行度/情境入口 `analysis.py` 和流行度预期结果 `data.csv` |
| `RQ1/RQ1_2/` | 多重异味/共现入口、预期多重异味表 `data.csv` 和 `cooccurrence.csv` |
| `RQ2/RQ2_1/` | 时长入口，以及两个时长端点的预期系数 `data.csv` |
| `RQ2/RQ2_2/` | 成功率入口，以及 LPM 预期系数和推断字段 `data.csv` |
| `RQ2/RQ2_3/` | 探索性资源比较入口、预期汇总 `data.csv` 和其他预期结果 |
| `RQ1/expected/`、`RQ2/expected/` | 用于核对的预期输出 |
| `src/` | 共享实现：研究选用的 run3 检测器版本 `frozen/`、模型 `models/`、分析辅助 `analysis/`、资源比较 `resources/`、采集源码 `collection/`，以及扫描、绘图、工具和测试 |
| `Pipeline/` | 采集、补抓和离线筛选清洗的对外入口 |
| `data/` | 外部数据获取位置和 `sources.json` 来源配置 |
| `docs/` | 字段和图表映射；`paper/`、`provenance/`、`verification/` 分别保存论文材料、来源和核对报告 |
| `LICENSES/` | 数据/文档完整许可证；代码许可证在根目录 `LICENSE` |
| `data_setup.py`、`data/sources.json` | 大型数据接入脚本和来源配置 |
| `reproduce.py`、`verify.py` | 全部论文分析入口和结果比较脚本 |
| `requirements.txt` | 固定版本的分析依赖 |

各子目录中的 `data.csv` 是供查看的**冻结预期结果**。重新分析使用共享的逐工作流、
逐运行输入，不是拿汇总 CSV 重新拟合模型。具体表格来源见各子目录 README。

## 使用步骤

### 1. 解压并安装依赖

解压 GitHub 版 ZIP，或克隆仓库。打开终端，进入包含 `reproduce.py` 和
`requirements.txt` 的根目录。以下命令均从这个目录执行。

已验证环境为 **Windows / Python 3.12.3**；尚未测试全新环境安装和 Linux/macOS。
默认统计复现建议预留至少 **20 GB**，用于恢复数据、环境和新结果；完整采集清洗
重放需要更多工作空间。

```powershell
python -m venv .venv
# Windows PowerShell 激活环境
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux/macOS 的环境激活命令为 `source .venv/bin/activate`。
安装依赖通常需要联网；数据接入完成后的论文统计分析无需 GitHub 账号、Token 或网络。

### 2. 获取并接入大型数据

**数据存档状态：已创建 Zenodo 草稿并预留 DOI，数据尚未发布。**
预留数据 DOI：[10.5281/zenodo.23640810](https://doi.org/10.5281/zenodo.23640810)。
该 DOI 在 Zenodo 正式发布后才注册，发布前可能无法解析；草稿目前不提供公开数据下载。
`data/sources.json` 已填写预留的 `doi`，`archive_url` 继续保持 `null`。
正式发布后，由作者补入实际数据 ZIP 下载直链；复现者无需自行填写这些配置。
Zenodo 仅保存本 GitHub 版缺少的数据，代码在本仓库提供。

已有完整包时，可把完整 ZIP 放到本仓库文件夹的上一级，然后执行：

```powershell
python data_setup.py --status
python data_setup.py --archive "../replication_package_emse_20261025.zip"
python data_setup.py --status
```

请将路径替换成完整 ZIP 的实际位置。恢复成功后会打印恢复文件数；最后的状态应为
`"missing_required_files": []`。状态检查只判断所需文件是否存在。

脚本只补入外部数据，不覆盖仓库代码、说明和预期结果。它检查存档的研究版本信息、
所选文件数量和总大小；已有同大小文件会跳过，并未独立鉴定其内容。
恢复后的大数据目录已被 `.gitignore` 排除，不要强制加入 Git。

将来填入实际 ZIP 下载直链后，可直接运行 `python data_setup.py` 下载并恢复。
DOI 展示页地址不能代替 ZIP 下载直链。默认统计分析不需要另行下载 GHALogs 候选列表。

### 3. 一次运行全部采用的分析

```powershell
python reproduce.py --output results_reproduced
```

该命令依次运行验证集评估、RQ1、RQ2 时长/成功率模型和 RQ2.3，然后核对结果。
输出目录为：

```text
results_reproduced/
  validation/evaluation.json
  rq1/                         # 流行度、共现和情境等表格
  rq2/all_completed_duration.json
  rq2/successful_duration.json
  rq2/reliability_lpm.json
  rq2_3/                       # 资源比较和观察频率结果
  verification.json
```

核对成功时，`verification.json` 中包含 `"passed": true`。
**每次必须使用新的输出目录**；重复执行时可改为 `results_reproduced_2`。

### 4. 按研究问题分别运行

下面这组命令生成可统一核对的目录结构：

```powershell
python Validation/analysis.py --output results_manual/validation
python verify.py --results results_manual --validation-only
python RQ1/RQ1_1/analysis.py --output results_manual/rq1
python RQ2/RQ2_1/analysis.py --output results_manual/rq2
python RQ2/RQ2_3/analysis.py --output results_manual/rq2_3
python verify.py --results results_manual
```

- **只做验证集评估**：运行第一条即可，不需要恢复大型数据。
- **RQ1 的两个子入口共用一次计算**：`RQ1_1/analysis.py` 和 `RQ1_2/analysis.py`
  都会生成流行度、情境、多重异味和共现结果，选择一个执行即可。
- **RQ2.1 和 RQ2.2 共用一次模型计算**：`RQ2_1/analysis.py` 和 `RQ2_2/analysis.py`
  都会拟合两个时长端点和成功率 LPM，选择一个执行即可。
- **RQ2.3 独立运行**：使用其自己的 `analysis.py`。
- 所有分析入口都需要 `--output`。单独运行部分分析后，不能立即用 `verify.py`
  核对全部结果；完整核对要求同一父目录下有上述四个结果子目录。

### 5. 可选：逐运行重建、全量检测和绘图

默认 RQ2 从完整包保存的精确充分统计量重新拟合模型。若还要从每条运行记录重建
分组和结局汇总，可使用：

```powershell
python RQ2/analysis.py --output results_rq2_rebuilt --rebuild-groups
```

或对完整复现执行 `python reproduce.py --output results_rebuilt --rebuild-groups`。

恢复工作流快照后，以下命令只检测 100 个工作流，用于快速检查检测入口：

```powershell
python src/scan.py --output scan_smoke --workers 4 --limit 100
```

若要完整重新检测并用新标签重算 RQ1，去掉 `--limit`：

```powershell
python src/scan.py --output scan_full --workers 4
python RQ1/analysis.py --scan scan_full/rq1_scan_direct.jsonl --output results_rq1_rescanned
```

100 个工作流的快速检查不能用于复现总体统计。绘制两幅主要定量图时，使用恢复后的
图表源数据及已生成的 RQ2 结果：

```powershell
python src/render_figures.py --results results_reproduced/rq2 --output figures_new
```

绘图输出包括 PDF/SVG/PNG 文件和 `figure_contract.json`。

## 数据采集、筛选和清洗

只复算论文统计时可跳过本节。重放数据构建流程时，先恢复完整数据包，再获取 GHALogs
原始候选列表。只需从[原始记录](https://zenodo.org/records/14796970)下载
[`repositories.json.gz`](https://zenodo.org/records/14796970/files/repositories.json.gz?download=1)，
无需下载约 142 GB 的日志包，也不需要 `runs.json.gz`。

在仓库根目录执行：

```powershell
python -c "from pathlib import Path; import urllib.request; p=Path('data/upstream/ghalogs/repositories.json.gz'); p.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve('https://zenodo.org/records/14796970/files/repositories.json.gz?download=1', str(p))"
python -c "import gzip, shutil; from pathlib import Path; p=Path('data/upstream/ghalogs'); shutil.copyfileobj(gzip.open(p/'repositories.json.gz', 'rb'), (p/'repositories.json').open('wb'))"
python -m pip install -r Pipeline/requirements.txt
python Pipeline/run.py offline-all --work-dir ../emse_pipeline_work
python Pipeline/verify_handoff.py --work-dir ../emse_pipeline_work
```

若已有有效文件，跳过下载解压命令；解压会覆盖同名文件。最终路径必须是
`data/upstream/ghalogs/repositories.json`，实际格式为 JSON Lines，不要转成 JSON 数组。
该第三方文件不随本包分发，也不由本包重新授权；使用时请引用原始数据集和论文。

`offline-all` 依次执行 `filter → canonical → consolidate → coverage → queue → model-input`。
工作目录必须位于仓库之外，日志保存在 `../emse_pipeline_work/logs/`。
这些阶段重放保存的历史输入；最终交接检查针对 10 月标签更新前的工作流身份和运行顺序。
论文采用的 10 月统计结果仍使用前述根目录/RQ 分析命令生成。

重新在线采集是独立选项，需要 `GITHUB_TOKEN`、流水线前置输入及显式 `--execute`。
历史仓库和运行的可用性会变化，不能保证重新采集得到相同历史数据。
逐阶段说明和在线命令见 [Pipeline/README.md](Pipeline/README.md)。

## 数据和统计口径

- 当前名称为 `no_fallback_cache`；历史 `no_restore_keys` 仅为兼容输入，不另计一种异味。
- 流行度分母按各类异味的适用条件确定，不能统一使用工作流总数。
- 验证集包含 383 个工作流，无整例排除；`no_dependency_cache` 因带括号的覆盖范围标注纳入 341 例，其他十类各纳入 383 例。主指标按工作流计算。
- 时长模型使用对数时长；成功率使用加权 LPM，系数表示概率差，不能解释为优势比。
- 模型推断采用仓库聚类及端点内多重比较校正；结果是调整后观察性关联。
- RQ2.3 为探索性历史比较，不作为因果效应解释。
- 冻结文件中的 `experimental` 是历史标记，不表示这里替换了论文采用的模型。

字段和论文对应关系见 [数据字典](docs/data_dictionary.md) 与
[图表映射](docs/paper_artifact_map.csv)。

`src/frozen` 只是研究过程中选用的代码版本名称，并非不可修改的归档。
源码保留与清理以当前入口依赖和论文结果核对为依据。

## 核对与常见问题

当前验证检查见 `docs/verification/github_layout.json` (section `validation_383`)：11 类工作流级指标与论文完全一致。
`docs/verification/github_layout.json` 保留旧 176 例验证、11 项原始模型测试和数据恢复测试的历史记录。
`docs/verification/original/` 和 `docs/verification/pipeline/` 中其他报告保留原完整包的历史核对证据，不代表在代码版中
重新执行了全部数据分析。尚未重新全量扫描检测器或验证全新环境和其他操作系统。

| 情况 | 处理方法 |
| --- | --- |
| 提示缺少研究数据 | 使用 `data_setup.py --archive ...` 恢复匹配的完整 ZIP |
| 没有配置下载地址 | 当前存档未发布，使用已有本地完整 ZIP；公开发布后填入真实地址 |
| 输出目录已存在 | 换一个新的 `--output` 路径 |
| 核对时缺少某个结果子目录 | 完成四组分析，或直接使用 `reproduce.py` |
| 流水线找不到 `repositories.json` | 按上方步骤单独获取 GHALogs 候选文件 |
| GitHub 版没有完整校验清单 | 完整文件校验入口属于完整离线包，本仓库不附带那份大数据清单 |

可选网页工具见 [src/tool/README.md](src/tool/README.md)，论文复现不需要启动它。

## 许可证和引用

自有代码采用 **MIT**，自有派生数据和文档采用 **CC BY 4.0**，允许按各自条款用于
研究、商业使用、修改和分发。完整许可文本见 `LICENSE`、`LICENSES/CC-BY-4.0.txt`；
适用范围及第三方/论文材料例外见 [docs/license_scope.md](docs/license_scope.md)。
第三方工作流授权审查按作者要求暂缓，不在此对上游材料授予新许可。

请引用配套论文和所使用的具体数据版本。作者元数据见 `CITATION.cff`。预留数据 DOI 为
[10.5281/zenodo.23640810](https://doi.org/10.5281/zenodo.23640810)，发布状态见上文；
论文 DOI 待正式分配后补入。作者存档步骤见 [Zenodo 指南](docs/zenodo_deposit.md)。
