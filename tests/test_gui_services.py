from copy import deepcopy
from pathlib import Path
import base64
import hashlib
import json
import time
import pytest

from arxiv_tracker.storage import Store
from arxiv_tracker.runner import run
from arxiv_tracker.costs import Budget, BudgetExceeded
from arxiv_gui.settings import Settings, Credentials, ConfigConflict
from arxiv_gui.data import read_papers, sources, filter_papers, cost_totals, snapshot_directory
from arxiv_gui.budget import SharedBudget
from arxiv_gui.sync import synchronize, GitHub, SyncConflict, allowed_path
from arxiv_gui.tasks import TaskManager


def test_gui_configuration_roundtrip_and_conflict(tmp_path):
    settings = Settings(tmp_path)
    cfg, revision = settings.read()
    original = deepcopy(cfg)
    cfg["research"]["interests"][0]["keywords"].append("new crystal")
    cfg["selection"]["summaries_per_day"] = 4
    cfg["budget"]["monthly_cny"] = 12
    new_revision = settings.save(cfg, revision)
    loaded, rev = settings.read()
    assert rev == new_revision and loaded["budget"]["monthly_cny"] == 12
    assert loaded["research"]["interests"][0]["keywords"][-1] == "new crystal"
    with pytest.raises(ConfigConflict):
        settings.save(original, revision)
    bad = deepcopy(loaded)
    bad["research"]["interests"][0]["weight"] = -1
    with pytest.raises(ValueError):
        settings.save(bad, rev)
    assert settings.read()[1] == rev


@pytest.mark.parametrize("change", ["secret", "nan", "empty", "counts", "model"])
def test_gui_config_rejects_invalid(tmp_path, change):
    settings = Settings(tmp_path)
    cfg, revision = settings.read()
    if change == "secret":
        cfg["deepseek"]["api_key"] = "private"
    elif change == "nan":
        cfg["budget"]["monthly_cny"] = float("nan")
    elif change == "empty":
        cfg["research"]["interests"] = []
    elif change == "counts":
        cfg["selection"]["summaries_per_day"] = 2.5
    else:
        cfg["deepseek"]["stages"]["deep"]["model"] = "other-provider"
    with pytest.raises(ValueError):
        settings.save(cfg, revision)
    assert settings.read()[1] == revision


def test_credentials_server_only_and_redaction(tmp_path, monkeypatch):
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit/secrets.toml").write_text('DEEPSEEK_API_KEY="secret-ds"\nGITHUB_TOKEN="secret-gh"', encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    credentials = Credentials.load(tmp_path)
    assert credentials.deepseek == "secret-ds"
    assert "secret-ds" not in repr(credentials)
    assert "secret-gh" not in credentials.safe_error(RuntimeError("secret-gh"))


class FakeGitHub:
    def __init__(self, database, reports=None):
        self.current = "a" * 40
        self.blob_calls = 0
        files = {"tracker.sqlite3": database.read_bytes(), "reports/2026-10-14.md": b"# Scientific report\n"}
        files.update(reports or {})
        self.files = files
        self.blobs = {hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest(): content for content in files.values()}
    def head(self):
        return self.current
    def request(self, method, path, body=None):
        if "/commits/" in path:
            return {"tree": {"sha": "tree"}}
        if "/trees/" in path:
            return {"truncated": False, "tree": [{"path": name, "type": "blob", "size": len(data),
                    "sha": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()} for name, data in self.files.items()]}
        self.blob_calls += 1
        return {"encoding": "base64", "content": base64.b64encode(self.blobs[path.rsplit('/', 1)[-1]]).decode()}


def test_private_snapshot_sync_dedup_and_offline_preserves(cfg, tmp_path, moment):
    run(cfg, tmp_path / "remote", mock=True, clock=lambda: moment)
    gateway = FakeGitHub(tmp_path / "remote/tracker.sqlite3", {"../private.txt": b"blocked", "pdf-cache/a.pdf": b"blocked"})
    home = tmp_path / "gui"
    first = synchronize(home, "private/repo", gateway=gateway)
    assert first["files"] == 2
    source_list = sources(home, "private/repo")
    before = cost_totals(source_list, "2026-10")
    calls = gateway.blob_calls
    second = synchronize(home, "private/repo", gateway=gateway)
    assert second["unchanged"] and gateway.blob_calls == calls
    assert cost_totals(sources(home, "private/repo"), "2026-10") == before
    snapshot = snapshot_directory(home, "private/repo")
    assert (snapshot / "reports/2026-10-14.md").exists()
    assert not (snapshot / "pdf-cache").exists()
    gateway.head = lambda: (_ for _ in ()).throw(RuntimeError("断网"))
    with pytest.raises(RuntimeError):
        synchronize(home, "private/repo", gateway=gateway)
    assert snapshot_directory(home, "private/repo") == snapshot


def test_sync_repairs_locally_corrupted_same_commit(cfg, tmp_path, moment):
    run(cfg, tmp_path / "remote", mock=True, clock=lambda: moment)
    gateway = FakeGitHub(tmp_path / "remote/tracker.sqlite3")
    home = tmp_path / "gui"
    synchronize(home, "owner/repo", gateway=gateway)
    old = snapshot_directory(home, "owner/repo")
    (old / "tracker.sqlite3").write_bytes(b"corrupted")
    result = synchronize(home, "owner/repo", gateway=gateway)
    assert not result["unchanged"]
    repaired = snapshot_directory(home, "owner/repo")
    assert repaired != old and old.exists()
    assert len(read_papers(sources(home, "owner/repo"), legacy=False)) == 3


def test_sync_changed_head_and_invalid_database_preserve(cfg, tmp_path, moment):
    run(cfg, tmp_path / "remote", mock=True, clock=lambda: moment)
    gateway = FakeGitHub(tmp_path / "remote/tracker.sqlite3")
    home = tmp_path / "gui"
    synchronize(home, "owner/repo", gateway=gateway)
    old = snapshot_directory(home, "owner/repo")
    heads = iter(["b" * 40, "c" * 40])
    gateway.head = lambda: next(heads)
    with pytest.raises(SyncConflict):
        synchronize(home, "owner/repo", gateway=gateway)
    assert snapshot_directory(home, "owner/repo") == old
    bad = tmp_path / "invalid.sqlite3"
    bad.write_bytes(b"invalid")
    gateway = FakeGitHub(bad)
    gateway.current = "d" * 40
    with pytest.raises(Exception):
        synchronize(home, "owner/repo", gateway=gateway)
    assert snapshot_directory(home, "owner/repo") == old


def test_search_favorites_and_readonly_budget(cfg, tmp_path, moment):
    run(cfg, tmp_path / "demo", mock=True, clock=lambda: moment)
    source_list = [("local", tmp_path / "demo/tracker.sqlite3")]
    records = read_papers(source_list, legacy=False)
    assert len(filter_papers(records, "配对对称性")) == 1
    assert len(filter_papers(records, fields=["笼目晶格"])) == 1
    assert len(filter_papers(records, favorite_ids={"9999.00001"})) == 1
    original = (tmp_path / "demo/tracker.sqlite3").read_bytes()
    cost_totals(source_list, "2026-10")
    assert original == (tmp_path / "demo/tracker.sqlite3").read_bytes()
    tasks = TaskManager(tmp_path / "gui")
    try:
        tasks.favorite("9999.00001", True)
        assert "9999.00001" in tasks.favorites()
        tasks.favorite("9999.00001", False)
        assert not tasks.favorites()
    finally:
        tasks.close()


def test_cloud_costs_enforce_local_budget_and_daily_limit(cfg, tmp_path, moment):
    cloud = Store(tmp_path / "cloud.sqlite3")
    budget = Budget(cloud, cfg, lambda: moment)
    identifier, _ = budget.reserve("paper", "screening", "deepseek-flash", [], 700)
    budget.settle(identifier, "deepseek-flash", moment, {"prompt_tokens": 100, "completion_tokens": 100})
    cloud.close()
    local = Store(tmp_path / "local.sqlite3")
    try:
        cfg["budget"]["monthly_cny"] = 0.001
        combined = SharedBudget(local, cfg, lambda: moment, [("cloud", tmp_path / "cloud.sqlite3")])
        assert combined.totals()["estimated_cny"] > 0
        with pytest.raises(BudgetExceeded):
            combined.reserve("paper", "screening", "deepseek-flash", [], 700)
        cfg["budget"]["monthly_cny"] = 20
        cfg["budget"]["daily_call_limit"] = 1
        with pytest.raises(BudgetExceeded, match="每日"):
            combined.reserve("paper", "screening", "deepseek-flash", [], 700)
    finally:
        local.close()


def wait_job(tasks, identifier):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = next(j for j in tasks.jobs() if j["id"] == identifier)
        if job["status"] not in ("running", "pending"):
            return job
        time.sleep(0.03)
    raise AssertionError("Job did not complete")


def test_background_mock_and_manual_deep(cfg, tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    cfg, rev = settings.read()
    cfg["selection"]["deep_per_day"] = 0
    settings.save(cfg, rev)
    tasks = TaskManager(tmp_path)
    try:
        identifier = tasks.submit("run", {"mock": True})
        assert wait_job(tasks, identifier)["status"] == "done"
        records = read_papers(sources(tmp_path, demo=True), legacy=False)
        record = next(r for r in records if "00001" in r["paper"]["id"])
        identifier = tasks.submit("deep", {"mock": True, "record": record})
        assert wait_job(tasks, identifier)["status"] == "done"
        records = read_papers(sources(tmp_path, demo=True), legacy=False)
        assert next(r for r in records if "00001" in r["paper"]["id"])["deep"]
    finally:
        tasks.close()


def test_dispatch_authenticated_server_side():
    class Response:
        status_code = 204
        content = b""
    class Session:
        calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "GET":
                class Repo(Response):
                    content = b"x"
                    def json(self):
                        return {"default_branch": "main"}
                return Repo()
            return Response()
    session = Session()
    with pytest.raises(ValueError):
        GitHub("owner/repo").dispatch()
    url = GitHub("owner/repo", "server-only-token", session).dispatch(True)
    assert url.endswith("/daily.yml")
    assert session.calls[-1][2]["headers"]["Authorization"] == "Bearer server-only-token"
    assert session.calls[-1][2]["json"]["inputs"]["retrieve_only"] == "true"
    assert session.calls[-1][2]["allow_redirects"] is False
