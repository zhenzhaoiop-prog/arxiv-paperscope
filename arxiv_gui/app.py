from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import html
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from arxiv_tracker.config import ROOT, now_shanghai
from arxiv_tracker.report import analysis_lines
from .data import sources, read_papers, filter_papers, cost_totals, run_records, snapshot_directory, subscription_stats
from .settings import Settings, Credentials, ConfigConflict
from .tasks import get_manager
from .codex_client import choose_provider, codex_status

PAGES = ["科研总览", "论文数据库", "深度分析", "追踪方向", "自动任务", "模型与费用"]
STATUS = {"pending": "等待中", "running": "运行中", "done": "已完成", "complete": "已完成", "partial": "部分完成", "failed": "失败", "interrupted": "已中断"}
PHASE = {"waiting": "等待", "preparing": "准备数据", "retrieving": "检索", "screening": "初筛", "review": "复筛", "summary": "中文总结", "deep": "深度分析", "pdf": "PDF提取", "sync": "同步", "report": "报告生成", "coordination": "云端状态核对"}
KINDS = {"run": "检索与分析", "deep": "指定论文分析", "sync": "数据同步", "dispatch": "远程触发"}
CSS = """
<style>
.stApp {background:#f5f7fb;color:#16233a;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;}
[data-testid="stToolbar"] {display:none;}
[data-testid="stSidebar"] {background:#111e32;}
[data-testid="stSidebar"] p,[data-testid="stSidebar"] label,[data-testid="stSidebar"] span,
[data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3 {color:#edf2ff!important;}
[data-testid="stSidebar"] input {color:#17243b!important;}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color:#9daecb!important;}
[data-testid="stSidebar"] .stButton button {background:#243655;color:#e7efff;border:1px solid #3c5277;}
[data-testid="stSidebar"] label[data-baseweb="radio"] {padding:10px 13px;border-radius:10px;margin:3px 0;}
[data-testid="stSidebar"] label[data-baseweb="radio"] > div:first-child {display:none;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) {background:#24334b;box-shadow:inset 3px 0 #7896ff;}
[data-testid="stSidebar"] label[data-baseweb="radio"] p {font-size:13px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(1) p:before {content:'◫';margin-right:11px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(2) p:before {content:'▤';margin-right:11px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(3) p:before {content:'◈';margin-right:11px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(4) p:before {content:'◎';margin-right:11px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(5) p:before {content:'◷';margin-right:11px;}
[data-testid="stSidebar"] label[data-baseweb="radio"]:nth-of-type(6) p:before {content:'▥';margin-right:11px;}
.block-container {padding-top:2.2rem;padding-bottom:3rem;max-width:1440px;}
[data-testid="stMetric"] {background:white;border:1px solid #e4e9f2;border-radius:14px;padding:18px 22px;box-shadow:0 3px 12px #1c315008;}
[data-testid="stMetricLabel"] {color:#62718a;}
h1 {font-size:2rem!important;letter-spacing:-.04rem;}
h2 {font-size:1.3rem!important;}
.ps-brand {font-size:21px;font-weight:750;color:white;margin:8px 0 4px;display:flex;align-items:center;gap:10px;}
.ps-brand span {color:white!important;display:inline-grid;place-items:center;width:37px;height:37px;background:linear-gradient(135deg,#6288ff,#8c65e9);border-radius:12px;}
.ps-subtitle {color:#7887a0;font-size:14px;margin-top:-8px;margin-bottom:24px;}
.ps-topbar {display:flex;justify-content:space-between;border-bottom:1px solid #e7eaf1;padding:0 0 22px;margin-bottom:24px;color:#8a95a8;font-size:12px;gap:12px;}
.ps-topbar b {color:#344261;}
.ps-topbar i {font-style:normal;background:#e6eaff;padding:9px;border-radius:10px;color:#5064c1;margin-left:14px;}
.ps-eyebrow {font-size:10px;letter-spacing:1.5px;color:#486de3;font-weight:800;margin:6px 0 8px;}
.ps-tag {display:inline-block;padding:3px 9px;background:#eaf0ff;color:#3468c4;border-radius:6px;font-size:12px;margin-right:6px;}
.ps-card {background:white;border:1px solid #e4e9f2;border-radius:14px;padding:22px;margin-bottom:14px;}
.ps-paper-row {display:flex;gap:15px;}
.ps-score {width:48px;height:48px;flex:0 0 48px;border-radius:12px;background:#e9f7f2;color:#168b6b;text-align:center;font-weight:750;padding-top:5px;font-size:19px;}
.ps-score small {display:block;font-size:10px;}
.ps-card .ps-english {font-size:11px;color:#8591a2;}
.ps-bar-label {display:flex;justify-content:space-between;color:#52627d;font-size:12px;margin:12px 0 8px;}
.ps-bar-track {height:8px;border-radius:50px;background:#e7ecf5;overflow:hidden;margin-bottom:20px;}
.ps-bar-fill {height:100%;border-radius:50px;background:linear-gradient(90deg,#6283ec,#6c5bd7);}
.ps-card h3 {margin:9px 0;font-size:17px;line-height:1.6;color:#1b2b47;}
.ps-card p {color:#65738d;font-size:14px;line-height:1.7;margin-bottom:4px;}
.ps-card a {text-decoration:none;color:#3468e8;}
.stButton button {border-radius:9px;min-height:39px;}
@media(min-width:1000px) {[data-testid="stSidebar"][aria-expanded="true"] {min-width:242px!important;width:242px!important;}}
@media(max-width:760px) {.block-container {padding:1.2rem 1rem;} h1 {font-size:1.6rem!important;} [data-testid="stMetric"] {padding:12px;} .ps-topbar {padding-top:28px;} .ps-topbar .ps-date {display:none;}}
</style>
"""


@st.cache_resource
def manager(home):
    return get_manager(home)


def heading(title, subtitle):
    day = now_shanghai().strftime("%Y 年 %m 月 %d 日")
    st.markdown(f'<div class="ps-topbar"><span>科研工作台 / <b>{html.escape(title)}</b></span><span class="ps-date">{day}<i>PS</i></span></div><div class="ps-eyebrow">RESEARCH INTELLIGENCE · CODEX / DEEPSEEK</div>', unsafe_allow_html=True)
    st.title(title)
    st.markdown(f'<div class="ps-subtitle">{html.escape(subtitle)}</div>', unsafe_allow_html=True)


def safe_url(paper):
    from arxiv_tracker.arxiv import split_id
    aid, _ = split_id(paper["id"])
    version = f"v{paper['version']}" if paper.get("version") else ""
    return f"https://arxiv.org/abs/{aid}{version}"


def submit(tasks, kind, payload):
    try:
        identifier = tasks.submit(kind, payload)
        st.session_state["notice"] = f"任务已提交（{identifier[:8]}），可在自动任务页查看进度。"
        st.rerun()
    except (ValueError, RuntimeError) as exc:
        st.error(Credentials.load().safe_error(exc))


def task_status(tasks):
    active = next((j for j in tasks.jobs() if j["status"] in ("pending", "running")), None)
    if active:
        with st.container(border=True):
            st.markdown("**后台任务运行中**")
            st.write(active["message"])
            fraction = min(0.95, active["completed"] / active["total"]) if active["total"] else 0.05
            st.progress(fraction)
            st.caption(f"阶段：{PHASE.get(active['phase'], active['phase'])} · 更新时间：{active['updated']} · 可继续浏览其他页面")
    elif tasks.jobs():
        recent = tasks.jobs()[0]
        if recent["status"] == "done":
            st.success(recent["message"])
            if st.button("刷新论文和报告", key="refresh_completed"):
                st.rerun()
        elif recent["status"] in ("failed", "partial", "interrupted"):
            st.warning(recent["error"] or recent["message"])


def show_analysis(record, stage):
    analysis = record.get(stage)
    if not analysis:
        st.info("暂无已完成分析；未分析不表示低相关。")
        return
    source = analysis.get("source_label", "信息来源未知")
    if "摘要" in source:
        st.warning(f"{source}：不能作为已阅读全文的分析。")
    else:
        st.info(source)
    paper = dict(record["paper"], url=safe_url(record["paper"]))
    st.markdown("\n".join(analysis_lines(paper, analysis)))


def deep_button(record, tasks, demo, credentials, key, cfg):
    selected, reason = choose_provider(cfg, credentials)
    ready = demo or selected != "none"
    disabled = tasks.active() or not ready or bool(record.get("deep"))
    if st.button("提交 PDF 深度分析", key=key, type="primary", disabled=disabled):
        submit(tasks, "deep", {"record": record, "mock": demo})
    if record.get("deep"):
        st.caption("此版本已有成功分析，避免重复调用；来源限制见下方。")
    elif not ready:
        st.caption("不可用：" + reason)
    elif tasks.active():
        st.caption("已有任务运行，完成后可提交。")


def overview(cfg, records, source_list, tasks, home, repository, demo):
    heading("科研总览", "从新文献中发现值得追踪的材料、物理问题与实验线索")
    days = sorted({r["paper"].get("discovered_on", "")[:10] for r in records if r["paper"].get("discovered_on")}, reverse=True)
    today = now_shanghai().date().isoformat()
    options = list(dict.fromkeys([today] + days))
    date = st.selectbox("统计日期", options, key="overview_date")
    daily = [r for r in records if r["paper"].get("discovered_on", "")[:10] == date]
    scores = [r for r in daily if r["score"] and r["score"]["label"] == "直接相关"]
    month = cost_totals(source_list)
    columns = st.columns(4)
    for col, label, value, caption in zip(columns, ["当日入库论文", "当日高相关", "文献库总量", "本月 API 费用"],
                                         [len(daily), len(scores), len(records), f"¥{month['estimated_cny']:.3f}"],
                                         ["含新版本", "AI语义评分", "包含历史与待分析", f"DeepSeek预算 ¥{cfg['budget']['monthly_cny']:.0f}"]):
        col.metric(label, value)
        col.caption(caption)
    st.write("")
    left, right = st.columns([2, 1])
    recommended = filter_papers([r for r in records if r["score"] and r["score"]["label"] != "低相关"])
    with left:
        st.subheader("最值得关注")
        if not recommended:
            st.info("尚无AI推荐结果。已有历史资料可以搜索；运行筛选后将显示推荐。")
        for record in recommended[:5]:
            p, score = record["paper"], record["score"]
            tags = "".join(f'<span class="ps-tag">{html.escape(t)}</span>' for t in score["categories"][:3])
            title = (record["summary"] or record["deep"] or {}).get("chinese_title", p["title"])
            st.markdown(f'<div class="ps-card ps-paper-row"><div class="ps-score">{score["score"]:.0f}<small>/ 100</small></div><div>{tags}<h3><a href="{safe_url(p)}" target="_blank" rel="noopener noreferrer">{html.escape(title)}</a></h3><p class="ps-english">{html.escape(p["title"])} · {p["updated"][:10]}</p><p>{html.escape(score["recommendation"])}</p><p>{html.escape(score["label"])}</p></div></div>', unsafe_allow_html=True)
    with right:
        st.subheader("研究方向分布")
        counts = Counter(c for record in recommended for c in record["score"]["categories"])
        if counts:
            with st.container(border=True):
                for category, count in counts.items():
                    percentage = count / sum(counts.values()) * 100
                    st.markdown(f'<div class="ps-bar-label"><span>{html.escape(category)}</span><b>{count} 篇</b></div><div class="ps-bar-track"><div class="ps-bar-fill" style="width:{percentage:.1f}%"></div></div>', unsafe_allow_html=True)
        else:
            st.caption("完成筛选后显示分类分布。")
        st.subheader("运行与预算")
        runs = run_records(source_list)
        st.write("最近运行：" + (STATUS.get(runs[0]["status"], runs[0]["status"]) if runs else "尚无运行记录"))
        st.caption(f"待初筛：{sum(r['screening'] is None for r in records)}")
        st.progress(min(1.0, month["committed_cny"] / cfg["budget"]["monthly_cny"]))
        st.caption(f"已用及未知扣费预留 ¥{month['committed_cny']:.3f}")
        if demo and st.button("生成演示日报", disabled=tasks.active(), key="overview_demo"):
            submit(tasks, "run", {"mock": True})
        if not demo:
            if st.button("同步最新数据", disabled=tasks.active() or not repository, key="overview_sync"):
                submit(tasks, "sync", {"repository": repository})
            if not repository:
                st.caption("同步不可用：请先在自动任务页设置 GitHub 仓库。")
    report_sources = [(label, path.parent) for label, path in source_list]
    reports = [(label, path / "reports" / f"{date}.md") for label, path in report_sources if (path / "reports" / f"{date}.md").exists()]
    if reports:
        with st.expander("查看与下载当日科研报告"):
            labels = [label for label, _ in reports]
            choice = st.selectbox("报告来源", labels)
            path = reports[labels.index(choice)][1]
            content = path.read_text(encoding="utf-8")
            st.download_button("下载 Markdown 日报", content, file_name=f"{date}.md", mime="text/markdown")
            st.markdown(content)


def paper_database(records, tasks, demo, credentials, cfg):
    heading("论文数据库", "搜索标题、摘要、作者、中文总结和分析字段；收藏保存在本机")
    columns = st.columns([2, 1, 1])
    query = columns[0].text_input("全字段搜索", placeholder="材料名称、物理问题、作者或实验方法", key="paper_search")
    fields = sorted({c for r in records for c in ((r["score"] or {}).get("categories", []) + r["paper"].get("categories", []))})
    chosen = columns[1].multiselect("研究领域", fields)
    label = columns[2].selectbox("相关程度", ["全部", "直接相关", "潜在相关", "低相关", "待分析"])
    a, b, c = st.columns(3)
    only_favorites = a.checkbox("只看收藏", key="favorites_only")
    latest = b.checkbox("只看最新版本", value=True)
    order = c.selectbox("排序方式", ["相关度优先", "日期优先"])
    filtered = filter_papers(records, query, chosen, label, tasks.favorites() if only_favorites else None, latest, order)
    st.caption(f"找到 {len(filtered)} 篇 · 关键词未命中不等于不相关")
    if not filtered:
        st.info("没有符合条件的论文。可清空筛选条件或同步最新数据。")
        return
    page = st.number_input("列表页码（每页30篇）", min_value=1, max_value=max(1, (len(filtered) + 29) // 30), value=1, step=1)
    visible = filtered[(page - 1) * 30:page * 30]
    table = [{"收藏": "★" if r["paper"]["id"] in tasks.favorites() else "", "arXiv": r["paper"]["key"],
              "英文标题": r["paper"]["title"], "中文标题": (r["summary"] or r["deep"] or {}).get("chinese_title", "待总结"),
              "相关性": (r["score"] or {}).get("score"), "相关程度": (r["score"] or {}).get("label", "待分析"),
              "更新日期": r["paper"]["updated"][:10]} for r in visible]
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
    choices = {r["paper"]["title"] + " · " + r["paper"]["key"]: r for r in visible}
    selected = st.selectbox("查看论文详情", list(choices), key="paper_detail")
    record = choices[selected]
    p = record["paper"]
    with st.container(border=True):
        st.subheader((record["summary"] or record["deep"] or {}).get("chinese_title", p["title"]))
        st.write(p["title"])
        st.caption("作者：" + ", ".join(p["authors"]))
        st.caption(f"发表 {p['published']} · 更新 {p['updated']} · 来源 {' / '.join(record['origins'])}")
        a, b = st.columns([1, 3])
        a.link_button("打开 arXiv", safe_url(p))
        favorite = p["id"] in tasks.favorites()
        if b.button("★ 取消收藏" if favorite else "☆ 收藏论文", key="favorite_toggle"):
            tasks.favorite(p["id"], not favorite)
            st.rerun()
        tabs = st.tabs(["摘要", "相关性评分", "中文科研总结", "PDF 深度分析"])
        with tabs[0]:
            st.write(p["abstract"])
        with tabs[1]:
            if record["score"]:
                st.write(record["score"]["recommendation"])
                labels = {"material": "材料体系", "physics": "核心物理问题", "methods": "实验与计算方法", "novelty": "创新性", "value": "科研价值"}
                st.dataframe(pd.DataFrame([{"维度": labels.get(dim, dim), "得分": value["score"], "理由": value["reason"]} for dim, value in record["score"]["dimensions"].items()]), hide_index=True, use_container_width=True)
            else:
                st.info("尚未通过AI分析。可使用Codex订阅或DeepSeek。")
        with tabs[2]:
            show_analysis(record, "summary")
        with tabs[3]:
            deep_button(record, tasks, demo, credentials, "paper_deep", cfg)
            show_analysis(record, "deep")


def deep_page(records, tasks, demo, credentials, cfg):
    heading("深度分析", "按需读取PDF正文，严格标注事实、作者解释、推测及来源限制")
    if not records:
        st.info("暂无论文，请先检索或同步。")
        return
    query = st.text_input("搜索待分析论文", key="deep_search")
    filtered = filter_papers(records, query)
    if not filtered:
        st.info("未找到论文。")
        return
    choices = {r["paper"]["title"] + " · " + r["paper"]["key"]: r for r in filtered}
    selected = st.selectbox("选择论文", list(choices), key="deep_paper")
    record = choices[selected]
    key = record["paper"]["key"]
    st.caption(f"{key} · {' / '.join(record['origins'])}")
    deep_button(record, tasks, demo, credentials, "deep_submit", cfg)
    st.caption("任务在后台执行，费用计入同一软件预算；PDF失败时会降级为摘要，不声称读过完整全文。")
    show_analysis(record, "deep")


def config_reload(prefix):
    if st.button("重新加载配置", key=prefix + "reload"):
        for key in list(st.session_state):
            if key.startswith(prefix):
                del st.session_state[key]
        st.rerun()


def save_config(settings, candidate, expected, prefix):
    try:
        new_revision = settings.save(candidate, expected)
        st.session_state[prefix + "revision"] = new_revision
        st.success(f"已保存并验证真实配置文件：{settings.config_path}")
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        st.error(Credentials.load().safe_error(exc))


def directions(cfg, revision, settings, tasks):
    heading("追踪方向", "研究兴趣、关键词与处理数量写入真实 YAML，下次本地任务生效")
    prefix = "directions_"
    config_reload(prefix)
    expected = st.session_state.setdefault(prefix + "revision", revision)
    with st.form("directions_form"):
        description = st.text_area("研究兴趣说明", cfg["research"]["description"], height=130, key=prefix + "description")
        rows = [{"方向名称": i["name"], "权重": float(i["weight"]), "关键词（逗号分隔）": ", ".join(i["keywords"])} for i in cfg["research"]["interests"]]
        edited = st.data_editor(pd.DataFrame(rows), num_rows="dynamic", hide_index=True, use_container_width=True, key=prefix + "editor")
        categories = st.text_input("arXiv 学科分类（逗号分隔）", ", ".join(cfg["arxiv"]["categories"]), key=prefix + "categories")
        a, b = st.columns(2)
        summaries = a.number_input("每日中文总结数量", min_value=0, max_value=500, value=int(cfg["selection"]["summaries_per_day"]), key=prefix + "summaries")
        deep = b.number_input("每日深度分析数量", min_value=0, max_value=100, value=int(cfg["selection"]["deep_per_day"]), key=prefix + "deep")
        saved = st.form_submit_button("验证并保存追踪配置", disabled=tasks.active(), type="primary")
    if saved:
        try:
            if any(not isinstance(row["方向名称"], str) or not isinstance(row["关键词（逗号分隔）"], str) for row in edited.to_dict("records")):
                raise ValueError("方向名称及关键词不能为空，请补全新增行或删除空行")
            candidate = deepcopy(cfg)
            candidate["research"] = {"description": description, "interests": [
                {"name": str(row["方向名称"]).strip(), "weight": float(row["权重"]),
                 "keywords": [k.strip() for k in str(row["关键词（逗号分隔）"]).replace("，", ",").split(",") if k.strip()]}
                for row in edited.to_dict("records")]}
            candidate["arxiv"]["categories"] = [c.strip() for c in categories.replace("，", ",").split(",") if c.strip()]
            candidate["selection"]["summaries_per_day"] = int(summaries)
            candidate["selection"]["deep_per_day"] = int(deep)
            save_config(settings, candidate, expected, prefix)
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
    st.caption("修改仅作用于本机配置；云端配置不会被静默覆盖。下载后提交到GitHub默认分支的 config.example.yaml 可用于云端。")
    st.download_button("下载当前 YAML 配置", settings.config_path.read_bytes(), file_name="config.example.yaml", mime="text/yaml", key="directions_download")


def automation(cfg, settings, source_list, tasks, demo, credentials):
    heading("自动任务", "本地任务异步执行；云端持续运行不依赖 GUI 或 Codex")
    if st.button("刷新运行状态", key="jobs_refresh"):
        st.rerun()
    a, b = st.columns(2)
    with a:
        st.subheader("本地运行")
        only = st.checkbox("仅检索，不调用AI", key="retrieve_only")
        analyze_existing = st.checkbox("仅分析已入库文献，不重复检索", disabled=only, key="analyze_existing")
        selected, reason = choose_provider(cfg, credentials)
        available = demo or only or selected != "none"
        st.caption("本地AI：" + reason)
        if st.button("启动本地任务", type="primary", disabled=tasks.active() or not available, key="local_run"):
            submit(tasks, "run", {"mock": demo, "retrieve_only": only, "analyze_only": bool(analyze_existing and not only)})
        if not available:
            st.caption("不可用：" + reason + "。可选择仅检索或演示。")
        st.caption(f"配置：{settings.config_path}；本地任务不会写入云端data分支。")
    with b:
        st.subheader("GitHub 连接与同步")
        with st.form("repository_form"):
            repository = st.text_input("GitHub 仓库（owner/repo）", settings.preferences()["repository"], key="repository")
            if st.form_submit_button("保存仓库连接", disabled=tasks.active()):
                try:
                    settings.save_repository(repository)
                    st.success("仓库连接已保存；凭证仍仅从服务端读取。")
                except ValueError as exc:
                    st.error(str(exc))
        repository = settings.preferences()["repository"]
        st.caption("GitHub凭证：" + ("已配置（不显示明文）" if credentials.github else "未配置；私人仓库需要服务端GITHUB_TOKEN"))
        if st.button("同步最新数据", disabled=tasks.active() or not repository or demo, key="sync_data"):
            submit(tasks, "sync", {"repository": repository})
        if demo:
            st.caption("演示模式禁用远程同步和触发，避免混入真实数据。")
        elif not repository:
            st.caption("请先保存仓库连接。")
        only_remote = st.checkbox("远程仅检索", key="remote_only")
        if st.button("触发 GitHub Actions", disabled=tasks.active() or not repository or not credentials.github or demo, key="remote_run"):
            submit(tasks, "dispatch", {"repository": repository, "retrieve_only": only_remote})
        if not credentials.github:
            st.caption("远程触发不可用：需要Actions写入权限的服务端凭证。")
    st.subheader("GUI 任务与失败重试")
    jobs = tasks.jobs()
    if jobs:
        st.dataframe(pd.DataFrame([{ "任务": KINDS[j["kind"]], "状态": STATUS.get(j["status"], j["status"]), "阶段": PHASE.get(j["phase"], j["phase"]), "说明": j["message"], "更新时间": j["updated"]} for j in jobs]), use_container_width=True, hide_index=True)
        failed = [j for j in jobs if j["status"] in ("failed", "partial", "interrupted")]
        if failed:
            choices = {j["id"][:8] + " · " + j["kind"] + " · " + (j["error"] or j["message"])[:80]: j["id"] for j in failed}
            identifier = choices[st.selectbox("可重试任务", list(choices))]
            if st.button("重试失败任务", disabled=tasks.active(), key="retry_task"):
                try:
                    tasks.retry(identifier)
                    st.rerun()
                except (ValueError, RuntimeError) as exc:
                    st.error(credentials.safe_error(exc))
        for j in jobs:
            if j["result"]:
                result = json.loads(j["result"])
                if result.get("url"):
                    st.link_button("查看远程 Actions", result["url"])
    else:
        st.info("尚未提交GUI任务。")
    st.subheader("后台运行记录与错误日志")
    runs = run_records(source_list)
    if runs:
        st.dataframe(pd.DataFrame([{ "日期": r["date"], "来源": r["origin"], "状态": STATUS.get(r["status"], r["status"]), "开始": r["started"], "错误": r["error"] or ""} for r in runs]), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无后台运行记录。")
    logs = []
    for label, path in source_list:
        logs.extend((label, log) for log in sorted((path.parent / "logs").glob("*.jsonl"), reverse=True)[:5])
    if logs:
        with st.expander("查看最近脱敏日志"):
            for label, path in logs:
                st.caption(f"{label} · {path.name}")
                st.code(path.read_text(encoding="utf-8")[-12000:], language="json")


def models(cfg, revision, settings, source_list, tasks, credentials):
    heading("模型与费用", "本地优先使用ChatGPT登录的Codex；DeepSeek API可选，人民币费用单独统计")
    selected, reason = choose_provider(cfg, credentials)
    st.info("当前本地AI：" + reason)
    if st.button("检测 Codex 登录状态", key="probe_codex", disabled=tasks.active()):
        with st.spinner("正在检查本机CLI登录状态..."):
            status = codex_status(cfg, fresh=True)
        (st.success if status["ready"] else st.warning)(status["message"])
    st.caption("Codex使用ChatGPT订阅用量，登录状态不代表剩余额度；不折算成DeepSeek人民币费用。云端Actions继续使用DeepSeek。")
    month = st.text_input("统计月份（YYYY-MM）", now_shanghai().strftime("%Y-%m"), key="cost_month")
    import re
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        st.error("月份格式应为 YYYY-MM")
        return
    totals = cost_totals(source_list, month)
    a, b, c = st.columns(3)
    a.metric("累计估算费用", f"¥{totals['estimated_cny']:.4f}")
    b.metric("未知扣费预留", f"¥{totals['uncertain_reserved_cny']:.4f}")
    c.metric("DeepSeek API月预算", f"¥{cfg['budget']['monthly_cny']:.2f}")
    if totals["stages"]:
        st.dataframe(pd.DataFrame([{ "阶段": stage, "调用次数": data["calls"], "估算费用 / 元": data["estimated_cny"], "未知预留 / 元": data["uncertain_reserved_cny"]} for stage, data in totals["stages"].items()]), hide_index=True, use_container_width=True)
    else:
        st.info("所选月份暂无调用记录。")
    if totals["committed_cny"] >= cfg["budget"]["monthly_cny"] * cfg["budget"]["warning_fraction"]:
        st.warning("已达到预算预警阈值；可选深度分析将优先暂停。")
    st.caption("DeepSeek密钥：" + ("已配置（不显示明文）" if credentials.deepseek else "未配置；仍可使用已登录的Codex订阅"))
    subscription = subscription_stats(source_list)
    if subscription:
        with st.expander("Codex 订阅调用记录（最近100次/每个数据源）", expanded=True):
            st.dataframe(pd.DataFrame([{"日期": row["date"], "阶段": PHASE.get(row["stage"], row["stage"]), "模型": row["model"],
                "状态": STATUS.get(row["status"], row["status"]), "用量": row["usage"] or "未取得usage；无法估计订阅消耗比例"} for row in subscription]), hide_index=True, use_container_width=True)
    prefix = "models_"
    config_reload(prefix)
    expected = st.session_state.setdefault(prefix + "revision", revision)
    with st.form("models_form"):
        providers = {"auto": "自动：优先Codex订阅，否则DeepSeek", "codex": "Codex · ChatGPT订阅", "deepseek": "DeepSeek API", "disabled": "禁用AI（仅检索）"}
        local_ai = cfg.get("local_ai", {})
        provider_names = list(providers.values())
        name = st.selectbox("本地AI后端", provider_names, index=list(providers).index(local_ai.get("provider", "auto")), key=prefix + "provider")
        selected_provider = next(k for k, v in providers.items() if v == name)
        codex_spec = local_ai.get("codex", {})
        executable = st.text_input("Codex CLI路径或命令名", codex_spec.get("executable", "codex"), key=prefix + "codex_exe")
        codex_model = st.text_input("Codex模型（留空使用CLI默认）", codex_spec.get("model", ""), key=prefix + "codex_model")
        a, b, c = st.columns(3)
        efforts_codex = ["low", "medium", "high", "xhigh"]
        effort_codex = a.selectbox("Codex推理强度", efforts_codex, index=efforts_codex.index(codex_spec.get("reasoning_effort", "high")), key=prefix + "codex_effort")
        calls_codex = b.number_input("Codex每日调用上限（0不限）", min_value=0, value=int(codex_spec.get("daily_call_limit", 0)), key=prefix + "codex_calls")
        timeout_codex = c.number_input("Codex单次超时（秒）", min_value=10, value=int(codex_spec.get("timeout", 300)), key=prefix + "codex_timeout")
        st.markdown("**以下为DeepSeek API专用参数（云端与选用API时生效）**")
        specs = {}
        for stage, label in (("screening", "初筛"), ("review", "复筛"), ("summary", "中文总结"), ("deep", "深度分析")):
            st.markdown(f"**{label}**")
            current = cfg["deepseek"]["stages"][stage]
            a, b, c = st.columns(3)
            model_options = ["deepseek-flash", "deepseek-v4-pro"]
            selected = a.selectbox(label + "模型", model_options, index=model_options.index(current["model"]), key=prefix + stage + "model")
            efforts = ["none", "low", "high", "max"]
            effort = b.selectbox(label + "推理强度", efforts, index=efforts.index(current["effort"]), key=prefix + stage + "effort")
            tokens = c.number_input(label + "输出token上限", min_value=1, max_value=393216, value=int(current["max_tokens"]), key=prefix + stage + "tokens")
            specs[stage] = {"model": selected, "thinking": effort != "none", "effort": effort, "max_tokens": int(tokens)}
        a, b = st.columns(2)
        budget = a.number_input("人民币月预算", min_value=0.01, value=float(cfg["budget"]["monthly_cny"]), step=1.0, key=prefix + "budget")
        calls = b.number_input("每日API调用上限（0不限）", min_value=0, value=int(cfg["budget"].get("daily_call_limit", 0)), key=prefix + "calls")
        a, b = st.columns(2)
        warning = a.number_input("预算预警比例（%）", min_value=1, max_value=100, value=int(cfg["budget"]["warning_fraction"] * 100), key=prefix + "warning")
        stop = b.number_input("停止深度分析比例（%）", min_value=1, max_value=100, value=int(cfg["budget"]["stop_deep_fraction"] * 100), key=prefix + "stop")
        if st.form_submit_button("验证并保存模型与预算", disabled=tasks.active(), type="primary"):
            candidate = deepcopy(cfg)
            candidate["local_ai"] = {"provider": selected_provider, "codex": {"executable": executable.strip(), "model": codex_model.strip(),
                "reasoning_effort": effort_codex, "daily_call_limit": int(calls_codex), "timeout": int(timeout_codex)}}
            candidate["deepseek"]["stages"] = specs
            candidate["budget"].update(monthly_cny=budget, daily_call_limit=int(calls), warning_fraction=warning / 100, stop_deep_fraction=stop / 100)
            save_config(settings, candidate, expected, prefix)
    st.caption("软件预算不是服务商扣费上限。本地预算叠加最近同步的云端费用；其他设备未同步的调用可能存在时差。")
    st.caption(f"价格核实日期：{cfg['pricing']['verified_on']}；价格变化请更新YAML中的pricing。")


def main():
    st.set_page_config(page_title="PaperScope 科研文献", page_icon="🔬", layout="wide", initial_sidebar_state="auto")
    st.markdown(CSS, unsafe_allow_html=True)
    home = Path(os.environ.get("PAPERSCOPE_HOME", ROOT / "gui-state")).resolve()
    try:
        settings = Settings(home)
        cfg, revision = settings.read()
        tasks = manager(str(home))
        credentials = Credentials.load()
        repository = settings.preferences()["repository"]
    except Exception as exc:
        st.error(Credentials().safe_error(exc))
        st.stop()
    with st.sidebar:
        st.markdown('<div class="ps-brand"><span>◈</span> PaperScope</div>', unsafe_allow_html=True)
        st.caption("凝聚态科研文献工作台")
        st.write("")
        page = st.radio("页面导航", PAGES, label_visibility="collapsed", key="navigation")
        st.divider()
        demo = st.toggle("演示模式 · 不调用真实AI", value=os.environ.get("PAPERSCOPE_DEMO") == "1", key="demo_mode")
        st.caption("Codex / DeepSeek · SQLite · GitHub Actions")
        st.caption("本地界面；云端后台独立运行")
        if st.button("刷新界面", key="refresh_all", use_container_width=True):
            st.rerun()
    if demo:
        st.warning("MOCK 演示模式：虚构测试论文与模拟费用，非真实科研结论。远程操作已禁用。")
    if "notice" in st.session_state:
        st.info(st.session_state.pop("notice"))
    try:
        source_list = sources(home, repository, demo)
        records = read_papers(source_list, legacy=not demo)
    except (sqlite3.Error, ValueError, KeyError, OSError) as exc:
        st.error(credentials.safe_error(exc))
        st.info("本地快照读取失败，请在自动任务页重新同步；不会自动覆盖现有数据。")
        records = []
        source_list = []
    task_status(tasks)
    if tasks.active():
        st.caption("任务在后台运行，可继续浏览；点击侧栏“刷新界面”查看最新进度。")
    if page == "科研总览":
        overview(cfg, records, source_list, tasks, home, repository, demo)
    elif page == "论文数据库":
        paper_database(records, tasks, demo, credentials, cfg)
    elif page == "深度分析":
        deep_page(records, tasks, demo, credentials, cfg)
    elif page == "追踪方向":
        directions(cfg, revision, settings, tasks)
    elif page == "自动任务":
        automation(cfg, settings, source_list, tasks, demo, credentials)
    else:
        models(cfg, revision, settings, source_list, tasks, credentials)
