from pathlib import Path
from copy import deepcopy
import base64
import json
import shutil
import yaml
import pytest

from arxiv_tracker.cli import sync_schedule
from arxiv_tracker.config import ROOT, schedule_cron
from arxiv_tracker.runner import run
from arxiv_tracker.notifications import issue_body, notify
from arxiv_tracker.persistence import DataBranch
from arxiv_tracker.storage import Store


def test_actions_configuration(cfg, tmp_path):
    workflows = ROOT / ".github/workflows"
    daily = yaml.safe_load((workflows / "daily.yml").read_text(encoding="utf-8"))
    assert daily["on"]["schedule"][0]["cron"] == schedule_cron(cfg)
    assert "workflow_dispatch" in daily["on"]
    assert daily["permissions"] == {"contents": "write", "issues": "write"}
    assert daily["concurrency"]["cancel-in-progress"] is False
    steps = daily["jobs"]["track"]["steps"]
    persist = next(s for s in steps if s.get("id") == "persist")
    assert "always()" in persist["if"]
    notification = next(s for s in steps if s.get("id") == "notify")
    assert "steps.persist.outcome == 'success'" in notification["if"]
    artifact = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert "pdf" not in artifact["with"]["path"]
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    assert set(ci["jobs"]["test"]["strategy"]["matrix"]["os"]) == {"windows-latest", "ubuntu-latest"}
    assert sync_schedule(cfg, check=True) == "30 4 * * *"
    root = tmp_path / "project"
    (root / ".github/workflows").mkdir(parents=True)
    shutil.copyfile(workflows / "daily.yml", root / ".github/workflows/daily.yml")
    cfg["schedule"]["time"] = "20:15"
    with pytest.raises(ValueError, match="不一致"):
        sync_schedule(cfg, check=True, root=root)
    assert sync_schedule(cfg, root=root) == "15 12 * * *"
    assert sync_schedule(cfg, check=True, root=root)


def test_issue_body_and_mock_no_external_send(cfg, tmp_path, moment):
    _, payload, _ = run(cfg, tmp_path / "demo", mock=True, clock=lambda: moment)
    body = issue_body("owner/repo", "2026-10-14", payload)
    assert "<!-- arxiv-tracker:2026-10-14 -->" in body
    assert "/blob/data/reports/2026-10-14.md" in body
    assert "MOCK" in body
    with pytest.raises(ValueError, match="Mock"):
        notify(cfg, tmp_path / "demo", "2026-10-14")


def test_issue_idempotency_after_missing_receipt(cfg, tmp_path, moment, monkeypatch):
    directory = tmp_path / "demo"
    path, payload, _ = run(cfg, directory, mock=True, clock=lambda: moment)
    payload["mock"] = False
    path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls = []
    def request(method, endpoint, token, body=None, session=None):
        calls.append((method, endpoint))
        if method == "GET":
            return [{"number": 7, "body": "<!-- arxiv-tracker:2026-10-14 -->"}]
        return {"number": 7}
    monkeypatch.setattr("arxiv_tracker.notifications.github_request", request)
    assert notify(cfg, directory, "2026-10-14") == []
    assert any(method == "PATCH" for method, _ in calls)
    assert not any(method == "POST" for method, _ in calls)
    before = len(calls)
    assert notify(cfg, directory, "2026-10-14") == []
    assert len(calls) == before


def test_persistence_whitelist_and_atomic_commit(cfg, tmp_path, moment, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    directory = tmp_path / "state"
    run(cfg, directory, mock=True, clock=lambda: moment)
    (directory / ".remote-head").write_text("", encoding="ascii")
    (directory / "pdf-cache").mkdir()
    (directory / "pdf-cache/private.pdf").write_bytes(b"secret full text")
    branch = DataBranch(directory)
    branch.head = lambda: None
    calls = []
    def api(method, path, body=None):
        calls.append((method, path, body))
        return {"sha": "new-sha"}
    branch.api = api
    branch.persist()
    tree = next(body for _, path, body in calls if path == "/git/trees")
    paths = [entry["path"] for entry in tree["tree"]]
    assert "tracker.sqlite3" in paths and any(p.startswith("reports/") for p in paths)
    assert not any("pdf" in p or "private" in p for p in paths)
    assert calls[-1][1] == "/git/refs"
    assert (directory / ".remote-head").read_text() == "new-sha"
    with pytest.raises(RuntimeError, match="已变化"):
        branch.persist()


def test_restore_validates_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    original = Store(tmp_path / "original.sqlite3")
    original.set_meta("watermark", "saved")
    original.close()
    content = base64.b64encode((tmp_path / "original.sqlite3").read_bytes()).decode()
    directory = tmp_path / "restore"
    branch = DataBranch(directory)
    branch.head = lambda: "head-sha"
    def api(method, path, body=None):
        if "/commits/" in path:
            return {"tree": {"sha": "tree-sha"}}
        if "/trees/" in path:
            return {"tree": [{"path": "tracker.sqlite3", "sha": "blob-sha"}]}
        return {"content": content}
    branch.api = api
    branch.restore()
    restored = Store(directory / "tracker.sqlite3")
    try:
        assert restored.get_meta("watermark") == "saved"
    finally:
        restored.close()
