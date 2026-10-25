# 工作流缓存异味网页工具

本工具调用仓库现有的 `CacheSmellDetector`，只展示论文当前采用的十一类缓存异味。检测器直接输出 `no_fallback_cache`，兼容识别历史名称 `no_restore_keys`。`conflicting_cache_sources` 和 `disposable_cache_key` 分别显示为论文名称 `overlapping_cache_mechanisms` 和 `overly_specific_cache_key`。其余实验性类型不纳入页面总数。

## 启动

在项目根目录运行：

```powershell
python -m pip install -r workflow_smell_web/requirements.txt
python -m workflow_smell_web
```

在浏览器打开 `http://127.0.0.1:8765`。可使用 `--port 9000` 修改端口。输入 `owner/repo` 和 `.github/workflows/ci.yml` 这类路径。工具读取该仓库**默认分支的当前文件**，不读取历史版本，也不运行工作流。

公共仓库可直接使用。GitHub API 限额耗尽时，工具会尝试读取公开原始文件，这种情况下报告只提供内容 SHA-256，不提供 GitHub blob SHA。私有仓库或较多请求时，可在启动前设置具有相应读取权限的 `GITHUB_TOKEN` 环境变量；令牌仅由本地服务端发送给 GitHub API，不写入网页。

报告包含命中类别、位置、检测器说明及建议，并提供 JSON 下载。缓存异味不提供严重度分级，页面按类型和 Job 排列发现，不显示等级徽标。每条发现可展开“检测依据与详情”查看检测器提供的子类型、缓存键及资源关联等信息；下载的 JSON 同样保留完整的 `details`。GitHub blob SHA 和文件内容 SHA-256 可用于记录所扫描的文件。规则是静态启发式判断；报告中的发现需要结合工作流上下文核对，未命中也不等于不存在缓存配置问题。本工具定位为论文研究原型。
