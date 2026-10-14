from copy import deepcopy
from datetime import timedelta
import json
import pytest

from arxiv_tracker.costs import Budget
from arxiv_tracker.deepseek import DeepSeekClient, UncertainCall, APIUnavailable
from arxiv_tracker.mock import MockClient, mock_papers
from arxiv_tracker.runner import run
from arxiv_tracker.scoring import score_messages, validate_score
from arxiv_tracker.summary import abstract_source, analysis_messages, validate_analysis
from arxiv_tracker.storage import Store


def test_malformed_schema_cannot_crash_validation(cfg, store, moment):
    paper = mock_papers(moment)[0]
    client = MockClient(cfg, Budget(store, cfg, lambda: moment))
    score = client.generate(paper["key"], "screening", score_messages(paper, cfg), lambda v: v)
    for dims in (None, [], {"material": 4, "physics": 4, "methods": 4, "novelty": 4, "value": 4}):
        invalid = dict(score, dimensions=dims)
        with pytest.raises(ValueError):
            validate_score(invalid, paper, cfg)
    source = abstract_source(paper)
    result = client.generate(paper["key"], "summary", analysis_messages(paper, cfg, source), lambda v: v)
    heading = next(iter(result["sections"]))
    result["sections"][heading] = ["invalid"]
    with pytest.raises(ValueError):
        validate_analysis(result, paper, source)


def test_interrupted_reservation_survives_restart(cfg, tmp_path, moment):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    budget = Budget(store, cfg, lambda: moment)
    request, _ = budget.reserve("key", "screening", "deepseek-flash", [], 700)
    reserved = budget.totals()["committed_cny"]
    store.close()
    store = Store(path)
    try:
        assert store.db.execute("SELECT status FROM costs WHERE id=?", (request,)).fetchone()[0] == "uncertain"
        assert Budget(store, cfg, lambda: moment).totals()["committed_cny"] == reserved
    finally:
        store.close()


def test_completed_backlog_appears_in_later_report(cfg, tmp_path, moment):
    directory = tmp_path / "demo"
    cfg["budget"]["monthly_cny"] = 0.001
    run(cfg, directory, mock=True, clock=lambda: moment)
    cfg["budget"]["monthly_cny"] = 20
    cfg["selection"]["summaries_per_day"] = 0
    cfg["selection"]["deep_per_day"] = 0
    _, payload, code = run(cfg, directory, mock=True, clock=lambda: moment + timedelta(days=1))
    assert code == 0
    # No summaries selected: today's newly screened older backlog still must be listed.
    assert len(payload["papers"]) == 4


def test_api_429_retries_and_usage_missing_reserves(cfg, store, moment, monkeypatch):
    from test_ai_costs import FakeResponse, FakeSession
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    budget = Budget(store, cfg, lambda: moment)
    session = FakeSession([FakeResponse(429), FakeResponse(content='{"ok":true}')])
    client = DeepSeekClient(cfg, budget, session, sleep=lambda _: None)
    assert client.generate("key", "screening", [], lambda v: v) == {"ok": True}
    assert len(session.calls) == 2
    class Missing:
        status_code = 200
        def json(self):
            return {"choices": []}
    client.session = FakeSession([Missing()])
    with pytest.raises(UncertainCall, match="usage"):
        client.generate("key", "screening", [], lambda v: v)
    assert budget.totals()["uncertain_reserved_cny"] > 0
