# DeepSeek arXiv 凝聚态科研追踪

Python 3.11+ 科研助手：arXiv 官方 API 检索 → DeepSeek 语义初筛/复筛 → 中文摘要总结 → 少量 PDF 正文分析 → 每日 Markdown/JSON → GitHub `data` 分支持久化与 Issues 简报。

程序日常运行不需要 Codex、ChatGPT 订阅、本地常开电脑或付费服务器。需要自行充值 DeepSeek API；20 元是可修改的软件月预算，不保证覆盖每天全部目标数量。GitHub 公共仓库中的研究兴趣、摘要、报告与费用记录均公开；API Key 只放 Secrets。

现已增加独立的 **PaperScope 中文 Streamlit GUI**。Windows双击 `启动界面.cmd`，详细步骤见 [GUI使用说明](GUI使用说明.md)。按最新需求，本地优先支持ChatGPT登录的Codex订阅，DeepSeek API可选；支持私人仓库授权同步、本地后台任务、收藏、配置编辑与费用管理。已有云端自动运行继续采用DeepSeek，与GUI独立。

## 1. 当前项目与交付

在原有组会资料项目上增加自动系统，原脚本、PPT、PDF 和 `data/` 均保留在本地。复用原 `combine_metadata.py` 的 Atom 字段提取和 `extract_pdfs.py` 的 pypdf 思路，改为无导入副作用的可测试模块。`legacy/` 只导出原资料中的公开元数据，不含 PDF、提取全文或 AI 分析结果。

|目录/文件|用途|
|---|---|
|`arxiv_tracker/`|检索、存储、API、评分、总结、PDF、报告、费用、通知、持久化、CLI|
|`config.example.yaml`|完整默认配置，Actions 默认使用此文件|
|`.github/workflows/`|每日运行、Windows/Linux CI、月度维护|
|`tests/`|离线与故障模拟测试，不调用真实 AI|
|`examples/`|明确标识的 Mock 日报、费用与测试记录|
|`legacy/papers_with_abstracts.json`|既有公开文献元数据；旧记录不视为已完成 AI 分析|
|`tools/prepare_deploy.py`|生成只含部署文件的 ZIP，防止上传本地旧附件|
|`state/`|本地正式数据库、报告、费用与私有 PDF 缓存；Git 忽略|

## 2. 本地安装与演示

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m arxiv_tracker doctor
python -m pytest -q
python -m arxiv_tracker run --mock --directory demo-state --date 2026-10-14
python -m arxiv_tracker run --mock --directory demo-state --date 2026-10-15
```

Linux 使用 `python3 -m venv .venv`、`source .venv/bin/activate`，之后命令相同。Mock 数据是虚构测试论文，链接中的 `9999.*` 不是真实论文；Mock 不能发送外部通知，也不能与正式数据库混用。

正式本地运行：

```powershell
Copy-Item config.example.yaml config.local.yaml
# 在当前 PowerShell 会话设置密钥；不要把密钥写入配置、代码或提交。
$env:DEEPSEEK_API_KEY = '你的 DeepSeek API Key'
python -m arxiv_tracker --config config.local.yaml run
```

Linux 使用 `export DEEPSEEK_API_KEY='你的密钥'`。本地 `run` 生成报告但不自动发通知；`notify` 是独立命令，GitHub 工作流只在持久保存成功后调用。不配置邮箱不影响系统。

## 3. 正式启动 GitHub Actions

1. 运行 `python tools/prepare_deploy.py`，解压生成的 `deploy-bundle.zip` 到干净目录。推荐新部署目录使用 `D:\Python_learning\Codex\arxiv-tracker`。ZIP 不包含旧 PPT/PDF、本地配置、密钥、数据库或缓存。
2. 在 GitHub 创建**公共仓库**，上传 ZIP 内全部文件，包括隐藏的 `.github/`，放到默认分支。用 Git 上传时在干净目录 `git init`、`git add .`、`git commit`，再绑定你自己的仓库；不要在旧资料目录直接 `git add .`。
3. 打开仓库 **Settings → Secrets and variables → Actions → New repository secret**，增加 `DEEPSEEK_API_KEY`。GitHub 自动提供 `GITHUB_TOKEN`，不需要创建个人 token。
4. 在 **Settings → Actions → General** 启用 Actions，并允许工作流写入内容和 Issues；启用仓库 Issues。分支保护若阻止机器人写入 `data` 或默认分支，需要调整相应规则。
5. 在 **Actions → Daily arXiv research tracker → Run workflow** 手动运行一次。可先勾选 `retrieve_only`，验证检索和持久化；随后取消勾选，启动真实 AI 分析。
6. 查看运行日志、自动创建的 `data` 分支和当天 Issue。只有看到真实运行完成、数据库持久保存成功，才算正式上线。

默认北京时间 **12:30**，cron 为 UTC `30 4 * * *`。GitHub 定时任务可能排队延迟，不是精确定时服务；延迟到高峰时段时费用按实际调用时刻估算。首次补检 7 天，后续使用水位减 3 天的重叠窗口，中断后自动补偿。

每天读取 `data` 分支的 SQLite；运行结束通过 SQLite backup 获取一致性快照，用 GitHub Git API 创建原子提交。不会把临时缓存作为数据库，也不上传 PDF/全文。不同运行用同一 concurrency 串行处理；远程分支意外变化时拒绝覆盖。

**失败不丢弃已有阶段结果。** 工作流中的持久化步骤在运行失败后仍执行；持久化本身失败会让工作流报错，并保留 30 天恢复 artifact。通知在第一次持久化之后发送，再持久化通知回执；同日重跑更新同一个 Issue。

公共仓库连续 60 天没有活动可能被 GitHub 停用定时工作流。月度维护工作流在默认分支写入维护时间，降低此风险；保护规则、权限变更或平台策略仍可能让维护失败。每月检查 Actions 最近运行记录；若被停用，在 Actions 页面选择对应工作流并重新启用，随后手动运行补检。[GitHub 官方调度说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 4. 配置研究方向、数量、模型、时间与预算

本地可用 `--config config.local.yaml`；云端修改并提交 `config.example.yaml`，不要上传本地密钥配置。

|配置项|修改内容|
|---|---|
|`research.description`、`research.interests`|研究兴趣、关键词与领域优先权重；新增材料不要求命中名称|
|`arxiv.categories`|全量分类检索覆盖；关键词另外跨全部学科检索|
|`scoring.dimensions`|五维总分权重；默认等权|
|`scoring.direct_threshold/potential_threshold`|默认 70/40；领域权重只影响排序，不影响相关性标签|
|`selection.summaries_per_day/deep_per_day`|每天最多 10 篇摘要总结、2 篇深入分析|
|`selection.deep_min_score`|默认 75，避免为低相关论文下载全文|
|`deepseek.stages`|各阶段模型、thinking、effort、max_tokens；深度可选 `deepseek-v4-pro`|
|`pdf.max_characters/max_pages/max_bytes`|正文输入、页数和下载大小上限|
|`budget.monthly_cny`|默认 20 元/月；实际调用月份按北京时间统计|
|`budget.warning_fraction/stop_deep_fraction`|默认 80% 预警、90% 停止可选深度分析|
|`schedule.time`|北京时间运行时间|

修改时间后运行以下命令并一同提交工作流，不能仅改 YAML 就改变 GitHub cron：

```powershell
python -m arxiv_tracker sync-schedule
python -m arxiv_tracker sync-schedule --check
```

模型或兴趣调整只作用于未完成任务；已有成功结果不会自动重算，以免重复扣费。若要重新评估历史论文，先备份，使用独立状态目录和 `--history-days` 做受控回测。

程序按更新时间倒序分页至窗口边界，没有 50–150 篇硬上限。通常候选数量由当天实际发布量决定；宽泛关键词可能带来更多候选和较大费用。全部候选进入 DeepSeek 初筛队列，预算不足时保留待筛选状态，不把未筛选论文列为低相关。先处理所有初筛，再复筛、总结和全文。复筛失败时可保留初筛推荐，并在报告标注复筛未完成。

## 5. 科学来源与 PDF 限制

- 中文总结明确写“基于摘要的总结”；不假装阅读过全文。
- PDF 使用版本专属链接，文本带页码；没有可确认版本时降级为摘要。超过页数/长度、扫描版或下载失败均标注限制。
- 每条实验事实或作者解释必须附输入中实际存在的原文短摘录；页码和数字做校验。不匹配的结果有限重试，费用仍记录；失败任务留待重跑。
- 文字提取仅能读取正文、可提取的图注/表格文字，不能可靠解释图像、曲线和视觉表格布局；报告明确说明，不生成未经读取的图表结论。
- 事实、作者解释、AI 推测、未报告分开呈现。来源校验降低幻觉风险，无法证明 AI 对原文的物理解释完全正确；科研决策仍需核对原文。
- `pdf.supplementary_urls` 可为 arXiv ID 设置明确补充 PDF 链接，并在 `allowed_supplement_hosts` 配置来源域名。不自动爬取不确定的附件，获取失败或截断会写入限制。

arXiv API 使用单连接，间隔至少 3 秒；PDF 下载与 API 共用请求节流。[arXiv 官方使用规则](https://info.arxiv.org/help/api/tou.html)

## 6. 费用与预算控制

截至 2026-10-14 配置的人民币价格（每百万 token）：

|模型|空闲缓存命中|空闲未命中|空闲输出|高峰缓存命中|高峰未命中|高峰输出|
|---|---:|---:|---:|---:|---:|---:|
|deepseek-flash|0.02|1|4|0.04|2|8|
|deepseek-v4-pro|0.15|4.5|13.5|0.30|9|27|

官方高峰为北京时间工作日 09:00–12:00、14:00–18:00，其他时段为空闲；默认 12:30 在空闲段。法定假日可在 `pricing.holidays` 配置，调休工作日可在 `workdays` 配置；未配置的节假日保守按普通星期处理。价格可能变化，应定期核查并更新 `pricing`。[DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；[模型与思考参数](https://api-docs.deepseek.com/api/create-chat-completion/)

计算：`缓存命中token × 命中价 + 未命中token × 未命中价 + completion_tokens × 输出价`，再除以一百万。`completion_tokens` 包含思考输出，不重复加 reasoning_tokens。每日/月度统计分开列初筛、复筛、总结、深度。

调用前按输入 UTF-8 字节数、输出上限和高峰价预留保守费用；实际 usage 返回后先记账，再解析 AI JSON。跨时段或较长请求按高峰保守估算。网络超时、服务端异常或无有效 usage 时保留未知扣费预留，不立即重发该请求，暂停本轮 AI。重启后依然计入该月预算；下次手动/定时运行可恢复待处理任务，保留旧预留避免低估。

**软件预算不是 DeepSeek 服务商侧绝对扣费上限。** 价格调整、计费误差、无法确认的网络请求和你在其他程序中的调用不受本项目控制。余额/真实账单以 DeepSeek 控制台为准。接近上限停止可选深度分析，无法容纳下一次预留时暂停 AI；继续检索、保存论文、生成状态报告，次月恢复。

## 7. 查询、补检、通知与本地下载

```powershell
python -m arxiv_tracker search UTe2
python -m arxiv_tracker costs --month 2026-10
python -m arxiv_tracker run --history-days 30 --retrieve-only
python -m arxiv_tracker import-legacy --source data
python -m arxiv_tracker notify --date 2026-10-14
```

`--date` 指报告日期，真实费用始终按实际调用时间计算。历史窗口可扩大，分析仍受预算限制；报告中显示全库待初筛/复筛数量。

下载 GitHub `data` 分支 ZIP 或单独下载 `tracker.sqlite3`，放到本地独立目录，便可运行 `search --directory 本地目录`、`costs --directory 本地目录`。历史报告在 `data/reports/` 按日期保存，每日 JSON 包含结构化评分和分析；SQLite 是可检索的完整版本库。

SMTP 默认关闭；开启 `notifications.smtp` 后设置 `SMTP_HOST`、`SMTP_PORT`（默认587）、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM`、`SMTP_TO` 为环境变量或 GitHub Secrets。采用 STARTTLS。SMTP 无法保证发送成功后、保存回执前崩溃时绝不重复发送；GitHub Issue 通过日期标记检索恢复，无需邮箱。

## 8. 故障恢复与长期维护

|情况|处理|
|---|---|
|arXiv 暂时失败/空页异常|保留已有入库结果，水位不推进，下次补检|
|API 401/402/配置错误|暂停本轮 AI；修正密钥/充值/模型后重跑|
|非正常 JSON/来源不匹配|有限重试并记录全部 usage；未成功阶段留待恢复|
|PDF失败/文本少/截断|降级并明确来源，不编造全文结论|
|预算用尽|继续检索，保留队列，次月自动恢复|
|Issues失败|报告和数据库先保存；重跑或手动执行通知|
|持久化失败|工作流报错；下载 recovery artifact，勿让后续任务覆盖未保存成果|

持久化失败后的恢复：先停用 Daily 工作流，下载最新 recovery artifact 和当前 `data` 分支数据库，各自备份；如果远程没有后续成功运行，可用恢复 SQLite 替换 `data/tracker.sqlite3`，补上报告、费用和日志，提交后重新启用。若远程已有更新，不直接覆盖，须合并版本/阶段/费用记录后再恢复。SQLite 达到几十 MB 或仓库历史明显膨胀时，先下载完整备份再做归档；程序接近 90 MB 单文件阈值会停止上传并报错，避免静默丢失状态。

正常异常路径可以保存阶段结果；平台强制终止、整机崩溃或作业超时可能让最终持久化和artifact步骤来不及执行。此时上次云端快照之后的结果与费用可能无法恢复，重新分析也可能再次收费。应检查失败运行日志与服务商账单，再决定是否重跑；不要手动取消正在处理AI的工作流。

每月：检查 Actions 与月预算、确认 API 余额和价格；每季度：更新依赖并运行测试、查看 arXiv/DeepSeek API 变更。修改 Python/YAML 后提交默认分支，CI 会验证 Windows 与 Linux。停止使用 Codex 后这些操作完全可以通过 GitHub 网页、Git 和本地 Python 进行。

## 9. 测试与验证边界

测试覆盖 Atom、历史导入、跨查询/版本去重、分页与重试、AI JSON/来源校验、排序、报告、缓存/思考费用、预算暂停、两天连续运行、阶段恢复、PDF解析/降级、通知幂等与持久化白名单。见 `examples/test-results.md`。

交付环境没有 DeepSeek Key 和 GitHub 远程/权限：离线测试与 Mock 演示不能证明真实模型输出质量、真实扣费、SMTP投递或 GitHub线上工作流成功。正式部署需要按第3节完成并手动验证一次；未验证项目会在测试记录中明确列出。
