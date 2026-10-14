from copy import deepcopy
from datetime import timedelta
import json
import requests
import pytest

from arxiv_tracker.costs import Budget, BudgetExceeded, calculate_cost, peak_time
from arxiv_tracker.deepseek import DeepSeekClient, AIError, APIUnavailable, UncertainCall, parse_json, redact
from arxiv_tracker.mock import MockClient, mock_papers
from arxiv_tracker.scoring import score_messages, validate_score, needs_review, rank
from arxiv_tracker.summary import abstract_source, analysis_messages, validate_analysis


def test_json():
    assert parse_json('```json\n{"ok":true}\n```') == {"ok": True}
    for invalid in ("", "[]", '{"x":NaN}', "not json"):
        with pytest.raises(ValueError):
            parse_json(invalid)


def test_scoring_semantic_fields(cfg, store, moment):
    p = mock_papers(moment)[0]
    client = MockClient(cfg, Budget(store, cfg, lambda: moment))
    score = client.generate(p["key"], "screening", score_messages(p, cfg), lambda v: validate_score(v, p, cfg))
    assert score["label"] == "直接相关" and needs_review(score, cfg)
    other = dict(score, score=80, priority=110)
    assert rank([(p, score), (dict(p, key="other"), other)])[0][1] == other
    invalid = deepcopy(score)
    invalid["dimensions"]["material"]["score"] = 101
    with pytest.raises(ValueError):
        validate_score(invalid, p, cfg)
    invalid = deepcopy(score)
    invalid["paper_key"] = "other"
    with pytest.raises(ValueError):
        validate_score(invalid, p, cfg)


def test_evidence_and_scientific_numbers(cfg, store, moment):
    p = mock_papers(moment)[0]
    source = abstract_source(p)
    client = MockClient(cfg, Budget(store, cfg, lambda: moment))
    result = client.generate(p["key"], "summary", analysis_messages(p, cfg, source), lambda v: validate_analysis(v, p, source))
    first = next(iter(result["sections"].values()))[0]
    first.update(kind="实验事实", text="观察到相变", evidence=[{"quote": "Resistivity and specific heat reveal a pressure-tuned phase transition.", "page": None}])
    assert validate_analysis(result, p, source)
    first["text"] = "转变温度为99 K"
    with pytest.raises(ValueError, match="数值"):
        validate_analysis(result, p, source)
    first["text"] = "观察到相变"
    first["evidence"][0]["quote"] = "An invented sentence."
    with pytest.raises(ValueError, match="来源"):
        validate_analysis(result, p, source)


def test_cost_cache_and_reasoning():
    rates = {"cache_hit": 0.02, "cache_miss": 1, "output": 4}
    usage = {"prompt_tokens": 1000000, "prompt_cache_hit_tokens": 250000,
             "prompt_cache_miss_tokens": 750000, "completion_tokens": 1000000,
             "completion_tokens_details": {"reasoning_tokens": 500000}}
    assert calculate_cost(usage, rates) == pytest.approx(4.755)
    usage["prompt_cache_miss_tokens"] = 1
    with pytest.raises(ValueError):
        calculate_cost(usage, rates)


def test_peak_and_holiday(cfg, moment):
    assert not peak_time(moment, cfg["pricing"])
    assert peak_time(moment.replace(hour=10), cfg["pricing"])
    assert not peak_time(moment.replace(hour=19), cfg["pricing"])
    cfg["pricing"]["holidays"] = ["2026-10-14"]
    assert not peak_time(moment.replace(hour=10), cfg["pricing"])


def test_budget_reservation_and_next_month(cfg, store, moment):
    cfg["budget"]["monthly_cny"] = 0.01
    clock = [moment]
    budget = Budget(store, cfg, lambda: clock[0])
    request, _ = budget.reserve("key", "screening", "deepseek-flash", [], 1000)
    budget.uncertain(request)
    with pytest.raises(BudgetExceeded):
        budget.reserve("key", "screening", "deepseek-flash", [], 1000)
    clock[0] = moment.replace(month=11)
    assert budget.totals()["committed_cny"] == 0
    assert budget.reserve("key", "screening", "deepseek-flash", [], 1000)


def test_deep_threshold(cfg, store, moment):
    cfg["budget"]["monthly_cny"] = 1
    budget = Budget(store, cfg, lambda: moment)
    request, _ = budget.reserve("key", "summary", "deepseek-flash", [], 100)
    budget.settle(request, "deepseek-flash", moment, {"prompt_tokens": 0, "completion_tokens": 230000})
    with pytest.raises(BudgetExceeded, match="深度"):
        budget.reserve("key", "deep", "deepseek-flash", [], 100)


class FakeResponse:
    def __init__(self, status=200, content="{}", usage=None):
        self.status_code, self.content_value = status, content
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 200}
    def json(self):
        return {"usage": self.usage, "choices": [{"finish_reason": "stop", "message": {"content": self.content_value}}]}


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_real_client_payload_and_usage_before_json(cfg, store, moment, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    cfg["deepseek"]["retries"] = 1
    budget = Budget(store, cfg, lambda: moment)
    session = FakeSession([FakeResponse(content="invalid"), FakeResponse(content='{"ok":true}')])
    client = DeepSeekClient(cfg, budget, session, sleep=lambda _: None)
    assert client.generate("key", "screening", [{"role": "user", "content": "JSON"}], lambda v: v) == {"ok": True}
    assert budget.totals()["stages"]["screening"]["calls"] == 2
    assert budget.totals()["estimated_cny"] > 0
    assert session.calls[0]["json"]["thinking"] == {"type": "disabled"}
    assert session.calls[0]["allow_redirects"] is False
    cfg["deepseek"]["retries"] = 0
    client.session = FakeSession([FakeResponse(content="invalid")])
    with pytest.raises(AIError):
        client.generate("key", "review", [], lambda v: v)
    assert client.session.calls[0]["json"]["reasoning_effort"] == "high"


def test_uncertain_timeout_no_retry(cfg, store, moment, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    budget = Budget(store, cfg, lambda: moment)
    session = FakeSession([requests.Timeout("test-key must never log")])
    client = DeepSeekClient(cfg, budget, session)
    with pytest.raises(UncertainCall):
        client.generate("key", "deep", [], lambda v: v)
    assert len(session.calls) == 1
    assert budget.totals()["uncertain_reserved_cny"] > 0
    assert "test-key" not in redact("test-key")


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_credential_or_balance_rejected(cfg, store, moment, monkeypatch, status):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    budget = Budget(store, cfg, lambda: moment)
    client = DeepSeekClient(cfg, budget, FakeSession([FakeResponse(status)]))
    with pytest.raises(APIUnavailable):
        client.generate("key", "screening", [], lambda v: v)
    assert budget.totals()["committed_cny"] == 0
