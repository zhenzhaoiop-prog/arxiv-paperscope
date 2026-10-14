from pathlib import Path

from arxiv_tracker.config import now_shanghai
from arxiv_tracker.costs import Budget, BudgetExceeded
from arxiv_tracker.deepseek import DeepSeekClient, AIError, UncertainCall
from arxiv_tracker.mock import MockClient
from arxiv_tracker.pdf import pdf_source
from arxiv_tracker.arxiv import ArxivClient
from arxiv_tracker.storage import Store, workspace_lock
from arxiv_tracker.summary import abstract_source, analysis_messages, validate_analysis
from arxiv_tracker.report import build_report


def analyze_one(cfg, directory, record, mock=False, client_factory=None, budget_factory=Budget,
                progress=None, clock=now_shanghai, enforce_api_budget=True):
    directory = Path(directory)
    paper = record["paper"]
    if record.get("deep"):
        return {"message": "已有成功分析，无需重复扣费", "cached": True}
    with workspace_lock(directory):
        store = Store(directory / "tracker.sqlite3")
        run_id = None
        try:
            previous_mode = store.get_meta("mock")
            if previous_mode is not None and previous_mode != mock:
                raise ValueError("禁止混用演示和真实数据库")
            store.set_meta("mock", mock)
            date = clock().date().isoformat()
            store.upsert(paper, date)
            if store.result(paper["key"], "deep"):
                return {"message": "已完成分析，读取已有结果", "cached": True}
            run_id = store.start_run(date)
            for stage in ("screening", "review", "summary"):
                if record.get(stage) and not store.result(paper["key"], stage):
                    store.save_stage(paper["key"], stage, "done", record[stage])
            budget = budget_factory(store, cfg, clock)
            if enforce_api_budget and budget.totals()["committed_cny"] >= cfg["budget"]["monthly_cny"] * cfg["budget"]["stop_deep_fraction"]:
                raise BudgetExceeded("已达到停止深度分析预算阈值")
            if progress:
                progress("pdf", 0, 2, "正在下载版本PDF并提取正文；无法读取时明确降级")
            source = abstract_source(paper) if mock else pdf_source(paper, cfg, directory, ArxivClient(cfg["arxiv"]))
            store.save_stage(paper["key"], "pdf", "degraded" if "摘要" in source["label"] else "done",
                             {"source_label": source["label"], "limitations": source["limitations"]})
            if progress:
                engine = "DeepSeek" if enforce_api_budget else "Codex订阅"
                timeout = cfg["deepseek"]["timeout"] if enforce_api_budget else cfg.get("local_ai", {}).get("codex", {}).get("timeout", 300)
                progress("deep", 1, 2, f"{engine}分析中，单次超时上限{timeout}秒")
            client = client_factory(cfg, budget) if client_factory else (MockClient(cfg, budget) if mock else DeepSeekClient(cfg, budget))
            result = client.generate(paper["key"], "deep", analysis_messages(paper, cfg, source, True),
                                     lambda value: validate_analysis(value, paper, source, True))
            store.save_stage(paper["key"], "deep", "done", dict(result, analyzed_on=date))
            store.select(date, paper["key"], "deep")
            store.finish_run(run_id, "complete")
            build_report(store, cfg, budget, date, directory, mock)
            return {"message": "分析完成", "source_label": source["label"], "cached": False}
        except Exception as exc:
            status = "budget_paused" if isinstance(exc, BudgetExceeded) else "uncertain" if isinstance(exc, UncertainCall) else "failed"
            # Only safe exception types from our own API layer carry explanatory text.
            error = str(exc) if isinstance(exc, (AIError, BudgetExceeded)) else type(exc).__name__
            store.save_stage(paper["key"], "deep", status, error=error)
            if run_id is not None:
                store.finish_run(run_id, "partial", error)
            raise
        finally:
            store.close()
