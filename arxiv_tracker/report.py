from __future__ import annotations

from collections import Counter
from pathlib import Path
import html
import json
import os


def text(value):
    return html.escape(str(value)).replace("[", "\\[").replace("]", "\\]")


def analysis_lines(paper, analysis):
    from .summary import SUMMARY_SECTIONS, DEEP_SECTIONS
    lines = [f"### {text(analysis['chinese_title'])}", "",
             f"**{text(paper['title'])}**", "", f"[arXiv {paper['key']}]({paper['url']})", "",
             f"作者：{text(', '.join(paper['authors']))}  ",
             f"发表：{paper['published']}；更新：{paper['updated']}", "",
             f"**信息来源：{text(analysis['source_label'])}**", ""]
    for limitation in analysis["limitations"]:
        lines += [f"> {text(limitation)}", ""]
    order = SUMMARY_SECTIONS if SUMMARY_SECTIONS[0] in analysis["sections"] else DEEP_SECTIONS
    for heading in order:
        entries = analysis["sections"][heading]
        lines += [f"**{heading}**", ""]
        for entry in entries:
            lines.append(f"- **[{entry['kind']}]** {text(entry['text'])}")
            for evidence in entry["evidence"]:
                suffix = f"，第{evidence['page']}页" if evidence.get("page") is not None else ""
                lines.append(f"  - 来源{suffix}：{text(evidence['quote'])}")
        lines.append("")
    return lines


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def build_report(store, cfg, budget, date, directory, mock=False, errors=None):
    papers = store.papers(True)
    pairs = []
    for paper in papers:
        score = store.result(paper["key"], "review") or store.result(paper["key"], "screening")
        if score:
            pairs.append((paper, score))
    from .scoring import rank, needs_review
    pairs = rank(pairs)
    # Daily content includes new candidates and backlog results selected today.
    summary_keys = set(store.selections(date, "summary"))
    deep_keys = set(store.selections(date, "deep"))
    daily = [(p, s) for p, s in pairs if p["discovered_on"] == date or p["key"] in summary_keys | deep_keys
             or s.get("analyzed_on") == date
             or (store.result(p["key"], "screening") or {}).get("analyzed_on") == date]
    recommended = [(p, s) for p, s in daily if s["label"] != "低相关"]
    pending = sum(store.result(p["key"], "screening") is None for p in papers)
    review_pending = sum(needs_review(s, cfg) and store.result(p["key"], "review") is None for p, s in pairs)
    deep_done = [(p, store.result(p["key"], "deep")) for p in papers if p["key"] in deep_keys and store.result(p["key"], "deep")]
    summary_done = [(p, store.result(p["key"], "summary")) for p in papers if p["key"] in summary_keys and store.result(p["key"], "summary")]
    priorities = {p["key"]: s["priority"] for p, s in pairs}
    deep_done.sort(key=lambda item: (-priorities.get(item[0]["key"], 0), item[0]["key"]))
    summary_done.sort(key=lambda item: (-priorities.get(item[0]["key"], 0), item[0]["key"]))
    month = budget.totals()
    day = budget.totals(date=date)
    counts = Counter(c for _, s in recommended for c in s["categories"])
    lines = [f"# {date} arXiv 凝聚态科研日报", ""]
    if mock:
        lines += ["> **MOCK 演示：论文和 AI 结果均为测试数据，费用为模拟 usage 估算；未调用真实 API。测试 ID 不代表真实论文。**", ""]
    new_count = sum(p["discovered_on"] == date for p in papers)
    lines += ["## 每日概览", "", f"- 新入库候选（含新版本）：{new_count}",
              f"- 今日推荐：{len(recommended)}；完成中文总结：{len(summary_done)}；完成深度分析：{len(deep_done)}",
              f"- 全库待初筛：{pending}；待复筛：{review_pending}",
              f"- 今日估算费用：¥{day['estimated_cny']:.6f}；本月：¥{month['estimated_cny']:.6f}",
              f"- 本月未知扣费预留：¥{month['uncertain_reserved_cny']:.6f}；预算：¥{month['limit_cny']:.2f}", "",
              "分类分布（可多标签）：" + ("；".join(f"{c} {n}" for c, n in counts.items()) or "暂无"), ""]
    if month["warning"]:
        lines += ["> 预算预警：已用及预留费用达到配置阈值。", ""]
    if errors:
        lines += ["### 运行状态与限制", ""] + [f"- {text(e)}" for e in errors] + [""]
    lines += ["## 最值得关注的论文", ""]
    for paper, score in recommended[:cfg["selection"]["summaries_per_day"]]:
        preliminary = "；复筛未完成，仅初筛" if needs_review(score, cfg) and not store.result(paper["key"], "review") else ""
        lines += [f"### [{text(paper['title'])}]({paper['url']})", "",
                  f"**{score['label']} · 相关性 {score['score']:.1f}/100 · 加权优先级 {score['priority']:.1f}{preliminary}**", "",
                  text(score["recommendation"]), "",
                  " | ".join(f"{d}: {item['score']}（{text(item['reason'])}）" for d, item in score["dimensions"].items()), ""]
    if not recommended:
        lines += ["暂无已完成 AI 筛选的推荐论文；未筛选论文不等于低相关。", ""]
    lines += ["## 重点论文中文总结", ""]
    for paper, analysis in summary_done:
        lines += analysis_lines(paper, analysis)
    if not summary_done:
        lines += ["本次未完成中文总结。", ""]
    lines += ["## 重点论文深度分析", ""]
    for paper, analysis in deep_done:
        lines += analysis_lines(paper, analysis)
    if not deep_done:
        lines += ["本次未完成深度分析（可能因预算、数量、来源或API限制）。", ""]
    lines += ["## 其他潜在相关论文", ""]
    for paper, score in recommended[cfg["selection"]["summaries_per_day"]:]:
        lines.append(f"- [{text(paper['title'])}]({paper['url']}) — {score['score']:.1f}：{text(score['recommendation'])}")
    lines += ["", "## API 费用统计", "", "|阶段|今日估算 / 元|本月估算 / 元|本月未知预留 / 元|", "|---|---:|---:|---:|"]
    for stage in ("screening", "review", "summary", "deep"):
        m = month["stages"].get(stage, {})
        d = day["stages"].get(stage, {})
        lines.append(f"|{stage}|{d.get('estimated_cny', 0):.6f}|{m.get('estimated_cny', 0):.6f}|{m.get('uncertain_reserved_cny', 0):.6f}|")
    lines += ["", "费用按实际返回usage及配置价格估算，非服务商账单；软件预算不是服务商侧绝对扣费上限。", ""]
    payload = {"date": date, "mock": mock, "new_candidates": new_count,
               "pending_screening": pending, "pending_review": review_pending,
               "papers": [{"paper": p, "relevance": s} for p, s in daily],
               "summaries": [{"paper_key": p["key"], "analysis": a} for p, a in summary_done],
               "deep_analyses": [{"paper_key": p["key"], "analysis": a} for p, a in deep_done],
               "costs": {"day": day, "month": month}, "errors": errors or []}
    target = Path(directory) / "reports" / f"{date}.md"
    atomic_write(target, "\n".join(lines))
    atomic_write(target.with_suffix(".json"), json.dumps(payload, ensure_ascii=False, indent=2))
    monthly_export = dict(month, mock=mock,
                          basis="模拟usage，非真实扣费" if mock else "API返回usage按配置费率估算，非服务商账单")
    atomic_write(Path(directory) / "costs" / f"{month['month']}.json", json.dumps(monthly_export, ensure_ascii=False, indent=2))
    return target, payload
