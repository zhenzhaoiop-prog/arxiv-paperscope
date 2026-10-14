import pytest
from arxiv_tracker.costs import Budget
from arxiv_gui.data import cost_totals
from arxiv_gui.codex_client import failure_reason


def test_same_timestamp_distinct_calls_not_deduplicated(cfg, store, moment):
    budget = Budget(store, cfg, lambda: moment)
    for _ in range(2):
        call, _ = budget.reserve("same-paper", "screening", "deepseek-flash", [], 700)
        budget.settle(call, "deepseek-flash", moment, {"prompt_tokens": 100, "completion_tokens": 100})
    totals = cost_totals([("local", store.path)], "2026-10")
    assert totals["stages"]["screening"]["calls"] == 2
    assert totals["estimated_cny"] == pytest.approx(budget.totals()["estimated_cny"])


def test_codex_diagnostic_ignores_benign_startup_warnings():
    text = 'WARNING: 拒绝访问 stale temporary folder\n{"type":"error","message":"usage limit exceeded"}'
    assert "额度" in failure_reason(text)
    assert "状态目录" in failure_reason("Error: failed initialize: 拒绝访问")
