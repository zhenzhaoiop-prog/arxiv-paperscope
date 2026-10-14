from __future__ import annotations

from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import logging

from .arxiv import ArxivClient, RetrievalError, build_queries, import_legacy, window
from .config import now_shanghai, parse_time
from .costs import Budget, BudgetExceeded
from .deepseek import AIError, APIUnavailable, UncertainCall, DeepSeekClient, redact
from .mock import MockClient, mock_papers
from .pdf import pdf_source
from .report import build_report
from .scoring import needs_review, rank, score_messages, validate_score
from .storage import Store, workspace_lock
from .summary import abstract_source, analysis_messages, validate_analysis

LOG = logging.getLogger(__name__)


def run(cfg, directory=None, date=None, history_days=None, retrieve_only=False, mock=False,
        arxiv_client=None, client_factory=None, clock=now_shanghai, budget_factory=Budget, progress=None,
        cached_results=None, enforce_api_budget=True, analyze_only=False):
    directory = Path(directory or cfg["storage"]["directory"])
    if mock and directory == Path(cfg["storage"]["directory"]):
        raise ValueError("Mock 必须使用独立 --directory，避免污染正式数据库")
    date = date or clock().date().isoformat()
    report_date = datetime.strptime(date, "%Y-%m-%d").date()
    end = min(clock().astimezone(timezone.utc),
              datetime.combine(report_date + timedelta(days=1), time.min, ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc))
    errors = []
    with workspace_lock(directory):
        store = Store(directory / "tracker.sqlite3")
        try:
            stored_mode = store.get_meta("mock")
            if stored_mode is not None and stored_mode != mock:
                raise ValueError("禁止混用 Mock 与正式数据库")
            store.set_meta("mock", mock)
            budget = budget_factory(store, cfg, clock)
            run_id = store.start_run(date)
            start = window(cfg, store.get_meta("watermark"), end, history_days)
            arxiv_client = arxiv_client or ArxivClient(cfg["arxiv"])
            try:
                if progress:
                    progress("preparing" if analyze_only else "retrieving", 0, 0,
                             "正在准备已入库文献" if analyze_only else "正在检索 arXiv，分页请求遵守至少3秒间隔")
                if not mock and cfg["arxiv"]["import_legacy"] and not store.get_meta("legacy_imported"):
                    legacy_dir = Path(cfg["arxiv"]["legacy_directory"])
                    import_legacy(store, legacy_dir, date, start, end)
                    store.set_meta("legacy_imported", True)
                if analyze_only:
                    pass
                elif mock:
                    for paper in mock_papers(clock()):
                        store.upsert(paper, date)
                else:
                    for query in build_queries(cfg):
                        for paper in arxiv_client.retrieve(query, start, end):
                            store.upsert(paper, date)
                            if progress:
                                progress("retrieving", 0, 0, f"已接收 {paper['key']}，正在继续分页")
                previous = store.get_meta("watermark")
                if not analyze_only and (not previous or parse_time(previous) < end):
                    store.set_meta("watermark", end.isoformat())
            except Exception as exc:
                # Safe diagnostics only; raw responses and request objects never reach logs.
                message = f"检索未完成（{type(exc).__name__}），已入库论文保留，检索水位未推进。"
                errors.append(message)
                LOG.error(message)
            if not retrieve_only:
                # GUI may reuse completed cloud stages, never its cost ledger.
                if cached_results:
                    for paper in store.papers(True):
                        cached = cached_results.get(paper["key"], {})
                        for stage in ("screening", "review", "summary", "deep"):
                            if cached.get(stage) and not store.result(paper["key"], stage):
                                store.save_stage(paper["key"], stage, "done", cached[stage])
                try:
                    client = client_factory(cfg, budget) if client_factory else (MockClient(cfg, budget) if mock else DeepSeekClient(cfg, budget))
                    _analyze(store, cfg, budget, client, directory, date, arxiv_client, errors, mock, progress, enforce_api_budget)
                except APIUnavailable as exc:
                    errors.append(redact(str(exc)))
            status = "partial" if errors else "complete"
            for message in errors:
                LOG.error(redact(message))
            store.finish_run(run_id, status, "; ".join(errors) if errors else None)
            if progress:
                progress("report", 0, 0, "正在生成日报并保存结构化结果")
            path, payload = build_report(store, cfg, budget, date, directory, mock, errors)
            return path, payload, 1 if errors else 0
        except Exception as exc:
            if "run_id" in locals():
                LOG.error("运行失败（%s），阶段结果已保留。", type(exc).__name__)
                store.finish_run(run_id, "failed", type(exc).__name__)
                build_report(store, cfg, budget, date, directory, mock, [f"运行失败（{type(exc).__name__}），阶段结果已保留。"])
            raise
        finally:
            store.close()


def _analyze(store, cfg, budget, client, directory, date, arxiv_client, errors, mock, progress=None, enforce_api_budget=True):
    stop = False
    completed = 0
    total = max(1, len(store.papers(True)))

    def execute(paper, stage, messages, validator):
        nonlocal stop, completed
        if stop:
            return None
        try:
            if progress:
                subscription = getattr(client, "provider", "") == "codex"
                timeout = cfg.get("local_ai", {}).get("codex", {}).get("timeout", 300) if subscription else cfg["deepseek"]["timeout"]
                progress(stage, completed, total, f"{'Codex订阅' if subscription else 'DeepSeek'}正在处理 {paper['key']}；单次超时上限 {timeout} 秒")
            value = client.generate(paper["key"], stage, messages, validator)
            value = dict(value, analyzed_on=date)
            store.save_stage(paper["key"], stage, "done", value)
            completed += 1
            return value
        except BudgetExceeded as exc:
            store.save_stage(paper["key"], stage, "budget_paused", error=str(exc))
            errors.append(f"{stage} 暂停：{exc}")
            if stage != "deep":
                stop = True
        except (APIUnavailable, UncertainCall) as exc:
            store.save_stage(paper["key"], stage, "uncertain" if isinstance(exc, UncertainCall) else "failed", error=redact(str(exc)))
            errors.append(redact(str(exc)))
            stop = True
        except AIError as exc:
            store.save_stage(paper["key"], stage, "failed", error=redact(str(exc)))
            errors.append(f"{paper['key']} {stage}：{redact(str(exc))}")
        return None

    papers = store.papers(True)
    # All candidates are screened before any optional summaries/full-text work.
    for paper in papers:
        if stop:
            break
        if not store.result(paper["key"], "screening"):
            execute(paper, "screening", score_messages(paper, cfg), lambda v, p=paper: validate_score(v, p, cfg))
    for paper in papers:
        if stop:
            break
        score = store.result(paper["key"], "screening")
        if score and needs_review(score, cfg) and not store.result(paper["key"], "review"):
            execute(paper, "review", score_messages(paper, cfg, score), lambda v, p=paper: validate_score(v, p, cfg))
    candidates = []
    # Recommend the newest known version of each paper, while retaining older analysis.
    latest = {}
    for paper in papers:
        latest[paper["id"]] = max(latest.get(paper["id"], 0), paper["version"] or 0)
    for paper in papers:
        score = store.result(paper["key"], "review") or store.result(paper["key"], "screening")
        if score and score["label"] != "低相关" and (paper["version"] or 0) == latest[paper["id"]]:
            candidates.append((paper, score))
    candidates = rank(candidates)
    for kind, limit in (("summary", cfg["selection"]["summaries_per_day"]), ("deep", cfg["selection"]["deep_per_day"])):
        if stop:
            break
        selected = store.selections(date, kind)
        lookup = {p["key"]: p for p in papers}
        # Include prior interrupted assignments, then fill remaining daily slots.
        for paper, score in candidates:
            if len(selected) >= limit:
                break
            if kind == "deep" and score["score"] < cfg["selection"]["deep_min_score"]:
                continue
            if paper["key"] in selected or store.result(paper["key"], kind):
                continue
            store.select(date, paper["key"], kind)
            selected.append(paper["key"])
        for key in selected:
            if stop:
                break
            paper = lookup[key]
            if store.result(key, kind):
                continue
            if kind == "deep":
                if enforce_api_budget and budget.totals()["committed_cny"] >= cfg["budget"]["monthly_cny"] * cfg["budget"]["stop_deep_fraction"]:
                    store.save_stage(key, kind, "budget_paused", error="达到深度分析预算阈值")
                    errors.append("已停止可选深度分析：达到预算阈值。")
                    break
                source = abstract_source(paper) if mock else pdf_source(paper, cfg, directory, arxiv_client)
                # Persist provenance only, never full text in the public database.
                store.save_stage(key, "pdf", "degraded" if "摘要" in source["label"] else "done",
                                 {"source_label": source["label"], "limitations": source["limitations"]})
            else:
                source = abstract_source(paper)
            execute(paper, kind, analysis_messages(paper, cfg, source, kind == "deep"),
                    lambda v, p=paper, s=source, d=(kind == "deep"): validate_analysis(v, p, s, d))
