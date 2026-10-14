import json
from pathlib import Path
from copy import deepcopy
import pytest

from arxiv_gui.codex_client import CodexClient, choose_provider, output_schema, clean_environment
from arxiv_gui.settings import Credentials
from arxiv_gui.data import read_papers, cost_totals
from arxiv_tracker.costs import Budget
from arxiv_tracker.deepseek import AIError, APIUnavailable
from arxiv_tracker.mock import mock_papers, MockClient
from arxiv_tracker.scoring import score_messages, validate_score
from arxiv_tracker.runner import run


def test_provider_preference_and_optional_api(cfg, monkeypatch):
    monkeypatch.setattr("arxiv_gui.codex_client.codex_status", lambda cfg: {"ready": True, "message": "ChatGPT"})
    assert choose_provider(cfg, Credentials())[0] == "codex"
    cfg["local_ai"]["provider"] = "deepseek"
    assert choose_provider(cfg, Credentials())[0] == "none"
    assert choose_provider(cfg, Credentials(deepseek="key"))[0] == "deepseek"
    cfg["local_ai"]["provider"] = "auto"
    monkeypatch.setattr("arxiv_gui.codex_client.codex_status", lambda cfg: {"ready": False, "message": "Not logged in"})
    assert choose_provider(cfg, Credentials(deepseek="key"))[0] == "deepseek"
    cfg["local_ai"]["provider"] = "disabled"
    assert choose_provider(cfg, Credentials(deepseek="key"))[0] == "none"


def test_codex_json_subscription_usage_and_safe_command(cfg, store, moment, monkeypatch):
    monkeypatch.setattr("arxiv_gui.codex_client.executable_command", lambda cfg: ["codex.exe"])
    paper = mock_papers(moment)[0]
    sample = {"paper_key": paper["key"], "dimensions": {d: {"score": 80, "reason": "测试"} for d in cfg["scoring"]["dimensions"]},
              "categories": [], "new_material": False, "recommendation": "测试科研相关性"}
    commands = []
    def runner(command, prompt, directory, timeout):
        commands.append(command)
        path = command[command.index("--output-last-message") + 1]
        Path(path).write_text(json.dumps(sample), encoding="utf-8")
        return 0, json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 200}})
    client = CodexClient(cfg, Budget(store, cfg, lambda: moment), runner=runner, probe=lambda cfg: {"ready": True})
    result = client.generate(paper["key"], "screening", score_messages(paper, cfg), lambda v: validate_score(v, paper, cfg))
    assert result["provider"] == "codex"
    assert store.db.execute("SELECT status FROM subscription_calls").fetchone()[0] == "done"
    assert store.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
    command = commands[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert "shell_tool" in command and "multi_agent" in command
    assert not any("dangerously" in value for value in command)
    cfg["local_ai"]["codex"]["daily_call_limit"] = 1
    with pytest.raises(APIUnavailable, match="每日"):
        client.generate(paper["key"], "screening", [], lambda v: v)


def test_codex_failure_no_paid_fallback(cfg, store, moment, monkeypatch):
    monkeypatch.setattr("arxiv_gui.codex_client.executable_command", lambda cfg: ["codex.exe"])
    calls = []
    def runner(*args):
        calls.append(1)
        return 1, '{"type":"error","message":"quota exceeded"}'
    client = CodexClient(cfg, Budget(store, cfg, lambda: moment), runner=runner, probe=lambda cfg: {"ready": True})
    with pytest.raises(APIUnavailable):
        client.generate("paper", "summary", [], lambda v: v)
    assert len(calls) == 1
    assert store.db.execute("SELECT status FROM subscription_calls").fetchone()[0] == "failed"
    assert store.db.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0


def test_codex_malformed_json_is_recoverable_ai_error(cfg, store, moment, monkeypatch):
    monkeypatch.setattr("arxiv_gui.codex_client.executable_command", lambda cfg: ["codex.exe"])
    def runner(command, *args):
        Path(command[command.index("--output-last-message") + 1]).write_text("invalid JSON", encoding="utf-8")
        return 0, '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}'
    client = CodexClient(cfg, Budget(store, cfg, lambda: moment), runner=runner, probe=lambda cfg: {"ready": True})
    with pytest.raises(AIError, match="JSON"):
        client.generate("paper", "screening", [], lambda v: v)
    row = store.db.execute("SELECT status,usage FROM subscription_calls").fetchone()
    assert row[0] == "failed" and json.loads(row[1])["output_tokens"] == 5


def test_codex_env_removes_other_service_credentials(monkeypatch):
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "SMTP_PASSWORD"):
        monkeypatch.setenv(name, "must-not-leak")
    environment = clean_environment()
    assert "PATH" in environment
    assert "must-not-leak" not in environment.values()


def test_schemas_keep_same_scientific_contract(cfg):
    schema = output_schema("deep", cfg)
    assert schema["additionalProperties"] is False
    assert "值得进一步开展的实验" in schema["properties"]["sections"]["properties"]
    assert output_schema("screening", cfg)["properties"]["new_material"] == {"type": "boolean"}


def test_cloud_results_reused_without_cost_rows(cfg, tmp_path, moment):
    run(cfg, tmp_path / "cloud", mock=True, clock=lambda: moment)
    cached = {r["paper"]["key"]: r for r in read_papers([("cloud", tmp_path / "cloud/tracker.sqlite3")], legacy=False)}
    class Retrieval:
        def retrieve(self, *args):
            yield from mock_papers(moment)
    calls = []
    class Count(MockClient):
        def generate(self, *args):
            calls.append(1)
            return super().generate(*args)
    _, _, code = run(cfg, tmp_path / "local", arxiv_client=Retrieval(), client_factory=Count,
                     cached_results=cached, clock=lambda: moment)
    assert code == 0 and not calls
    assert cost_totals([("local", tmp_path / "local/tracker.sqlite3")], "2026-10")["estimated_cny"] == 0


def test_analyze_existing_does_not_retrieve_or_advance_watermark(cfg, tmp_path, moment):
    from arxiv_tracker.storage import Store
    directory = tmp_path / "local"
    store = Store(directory / "tracker.sqlite3")
    store.upsert(mock_papers(moment)[0], "2026-10-14")
    store.close()
    cfg["selection"]["deep_per_day"] = 0
    cfg["selection"]["summaries_per_day"] = 0
    class NoRetrieval:
        def retrieve(self, *args):
            raise AssertionError("Must not retrieve")
    _, payload, code = run(cfg, directory, client_factory=MockClient, arxiv_client=NoRetrieval(),
                           analyze_only=True, clock=lambda: moment)
    assert code == 0 and payload["pending_screening"] == 0
    store = Store(directory / "tracker.sqlite3")
    try:
        assert store.get_meta("watermark") is None
    finally:
        store.close()
