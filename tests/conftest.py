from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from arxiv_tracker.config import load_config
from arxiv_tracker.storage import Store


@pytest.fixture(autouse=True)
def no_live_codex_in_tests(monkeypatch):
    # Never spend the user's subscription quota through automated tests.
    monkeypatch.setattr("arxiv_gui.codex_client.codex_status", lambda cfg, fresh=False: {"ready": False, "message": "测试：未配置Codex登录"})


@pytest.fixture
def cfg():
    result = deepcopy(load_config())
    result["arxiv"]["import_legacy"] = False
    return result


@pytest.fixture
def moment():
    return datetime(2026, 10, 14, 12, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "tracker.sqlite3")
    yield instance
    instance.close()
