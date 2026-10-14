# 交付测试记录

日期：2026-10-14（北京时间）。执行环境：Windows，Python 3.11.7。

## 已实际执行

- `python -m pytest -q`：**39 项通过**，覆盖 Atom、分页、旧式 ID、重试/节流、历史导入、版本去重、AI JSON/来源/数字校验、相关性排序、中文报告、成本/缓存/思考token、预算暂停、次月恢复、两天连续运行、重复执行、阶段中断、检索水位、PDF解析/降级、本地锁、通知幂等和 GitHub 持久化白名单。
- `python -m compileall -q arxiv_tracker tools`：通过。
- `python -m arxiv_tracker sync-schedule --check`：通过，默认 UTC `30 4 * * *`。
- `python -m arxiv_tracker doctor`：Python和所需依赖检查通过；DeepSeek Key 与 GitHub 环境变量不存在。
- 连续运行 Mock `2026-10-14`、`2026-10-15`，再次重复第二天：全部成功；新候选分别为3、1；重复运行费用不增加。
- 从原有公开 XML/JSON 导出 **343 条历史元数据**，不包含 PDF 或提取全文，未标记为已完成 AI 分析。
- 使用现有真实 `2609.11985.pdf` 实测提取：9 页、30,714 字符，保留页码；未执行 AI 全文解读或图像解释。
- 小规模 arXiv 官方 API 网络探测：**HTTP 429**，未取得成功响应；停止进一步密集请求。解析和失败处理通过离线样本验证，不能声称真实检索已成功。

## 环境处理

当前环境默认 pip 无索引，安装 pypdf 未成功。为本地验证，从桌面应用已有运行时复制纯 Python 的 pypdf 6.10.0 与 tzdata 到 Git 忽略的 `.runtime/`，仅测试时通过 `PYTHONPATH` 使用。部署代码没有引用此路径，正式使用 `requirements.txt` 安装依赖。

本机 pytest 自动加载的无关 Dash 插件启动失败，因此本地测试设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。该问题不属于项目；CI 采用干净环境安装开发依赖。

本地复现命令：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) '.runtime')
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTHONIOENCODING = 'utf-8'
python -m pytest -q
```

## 未验证

- 缺少 DeepSeek API Key：真实模型调用、输出质量、真实 token/扣费及 Max 思考模式效果未验证；所有 AI 链路使用 Mock 或模拟 HTTP 响应。
- 缺少 GitHub 远程仓库及认证：线上定时运行、data分支实际写入、真实 Issues 投递、月度维护未执行；工作流配置和 Git API 请求流程通过离线测试。
- 未配置 SMTP：真实邮件投递未验证。
- Linux 线上运行未执行；已提供 Linux/Windows GitHub CI，等待仓库启用后验证。

样例费用来自 Mock usage，不是 DeepSeek 账单，不用于证明20元能覆盖全部真实工作量。当前交付为开发完成和部署准备完成，**尚未上线**。
