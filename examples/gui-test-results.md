# PaperScope GUI 验收记录

日期：2026-10-14；Windows、Python 3.11.7；当前已有 Streamlit 1.30.0。

## 实际执行的离线测试

`python -m pytest -q --tb=short --disable-warnings`：**73项通过**，包括原后台39项、新增GUI页面与服务24项、Codex适配与记账边界10项。

- 六个页面的Streamlit AppTest均成功渲染。
- 搜索、收藏、空结果与空页面、缺少凭证时按钮禁用/说明。
- 方向、论文数量、模型预算写入真实YAML；校验失败保留原文件，旧哈希保存触发冲突。
- 授权GitHub请求模拟、数据库/日报同步、文件白名单与哈希、重复同步去重、断网及提交变化保留旧快照。
- 云端费用叠加本地预算、每日API调用上限、后台Mock运行及指定论文分析。
- Codex优先选择、DeepSeek可选、只读执行参数、JSON科学契约、订阅usage独立记账、失败不转付费API、已完成云端分析复用。

测试使用Mock/模拟HTTP/模拟CLI，不会消耗真实DeepSeek费用或Codex订阅额度。本机无关Dash pytest插件不兼容，因此设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`；PDF依赖使用上次交付的 `.runtime/` 路径作本地测试支持，部署无该路径依赖。

## 真实服务验证边界

- 本机已安装 `codex-cli 0.130.0-alpha.5`；`codex login status`确认通过ChatGPT登录。
- 实际Codex小规模连通尝试未取得可用结构化回答：沙箱内受到状态目录只读限制；正常权限诊断尝试也在40秒内未完成。不能将“已登录”和“适配Mock通过”写成“真实科研分析成功”。正常本地终端运行、网络与账号支持模型仍需验证。
- 没有DeepSeek API Key、GitHub私人仓库授权：真实API筛选、私人仓库同步、Actions触发、SMTP均未验证。
- GitHub CI包含Linux/Windows及GUI依赖，线上运行待仓库启用后验证。

浏览器预览、响应式检查和交付截图使用独立Mock库；截图不代表真实科研结果或真实消费。

## 浏览器与启动验证

- 实际在浏览器切换科研总览/论文数据库，搜索 `heavy-fermion` 得到1条结果，点击收藏后数据库和按钮均显示已收藏。
- 检查1365×900桌面布局及390像素手机宽度；手机页DOM宽度与滚动宽度均为390，无整页水平溢出。手机全页截图接口未完成，但DOM布局检查通过。
- Streamlit `/_stcore/health` 返回 HTTP 200 `ok`。
- PowerShell解析器检查 `start_gui.ps1`：无语法错误。启动脚本的首次联网安装未执行，以免重复创建环境；当前预览使用已有Python依赖。
- `compileall`、默认Actions cron一致性检查通过；部署ZIP采用白名单，排除真实凭证、数据库和缓存。

## Windows 启动修复复验

- 启动脚本保留 UTF-8 BOM；依赖检查使用 find_spec 和退出码，缺包不再触发 PowerShell NativeCommandError。
- 首次安装已在 .venv-gui 中实际完成；Streamlit 1.65.0，使用 headless 参数跳过邮箱询问。
- 实际运行 start_gui.ps1，8501 健康检查返回 200 ok；首页 AppTest 无异常。
- 完整测试 76 项通过（含 3 项 Windows 启动回归）。

