import os
from pathlib import Path
import pytest

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest
from arxiv_tracker.config import ROOT
from arxiv_tracker.runner import run
from arxiv_gui.settings import Settings
from arxiv_gui.app import PAGES, manager


@pytest.fixture
def gui_home(tmp_path, monkeypatch, moment):
    monkeypatch.setenv("PAPERSCOPE_HOME", str(tmp_path))
    monkeypatch.setenv("PAPERSCOPE_DEMO", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    settings = Settings(tmp_path)
    cfg, revision = settings.read()
    cfg["selection"]["deep_per_day"] = 0
    cfg["arxiv"]["import_legacy"] = False
    settings.save(cfg, revision)
    run(cfg, tmp_path / "demo", mock=True, clock=lambda: moment)
    yield tmp_path
    manager(str(tmp_path)).close()


@pytest.mark.parametrize("page", PAGES)
def test_all_gui_pages_render(gui_home, page):
    app = AppTest.from_file(str(ROOT / "gui_app.py"), default_timeout=25).run()
    app.radio(key="navigation").set_value(page).run()
    assert not app.exception
    assert app.title[0].value == page
    assert any("MOCK" in item.value for item in app.warning)


def test_search_favorite_and_empty_state(gui_home):
    app = AppTest.from_file(str(ROOT / "gui_app.py"), default_timeout=25).run()
    app.radio(key="navigation").set_value("论文数据库").run()
    app.text_input(key="paper_search").set_value("heavy-fermion").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    app.button(key="favorite_toggle").click().run()
    # Streamlit 1.30 AppTest exposes the interrupted rerun tree; finish that rerun.
    app.run()
    assert "9999.00001" in manager(str(gui_home)).favorites()
    app.text_input(key="paper_search").set_value("no-such-paper-xyz").run()
    assert not app.exception
    assert any("没有符合条件" in item.value for item in app.info)


def test_gui_model_and_research_save_real_yaml(gui_home):
    app = AppTest.from_file(str(ROOT / "gui_app.py"), default_timeout=25).run()
    app.radio(key="navigation").set_value("追踪方向").run()
    app.text_area(key="directions_description").set_value("重点研究压力调控的新型单晶")
    app.number_input(key="directions_summaries").set_value(5)
    submit = next(b for b in app.button if b.label == "验证并保存追踪配置")
    submit.click().run()
    assert not app.exception
    cfg, _ = Settings(gui_home).read()
    assert cfg["selection"]["summaries_per_day"] == 5
    assert cfg["research"]["description"] == "重点研究压力调控的新型单晶"
    app.radio(key="navigation").set_value("模型与费用").run()
    app.number_input(key="models_budget").set_value(15.0)
    app.number_input(key="models_calls").set_value(20)
    next(b for b in app.button if b.label == "验证并保存模型与预算").click().run()
    assert not app.exception
    cfg, _ = Settings(gui_home).read()
    assert cfg["budget"]["monthly_cny"] == 15 and cfg["budget"]["daily_call_limit"] == 20


def test_gui_missing_credentials_buttons_explain(gui_home):
    app = AppTest.from_file(str(ROOT / "gui_app.py"), default_timeout=25).run()
    app.toggle(key="demo_mode").set_value(False).run()
    app.radio(key="navigation").set_value("自动任务").run()
    assert not app.exception
    assert app.button(key="local_run").disabled
    assert app.button(key="remote_run").disabled
    assert any("未配置" in item.value for item in app.caption)
    app.checkbox(key="retrieve_only").set_value(True).run()
    assert not app.button(key="local_run").disabled


def test_demo_empty_page(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERSCOPE_HOME", str(tmp_path))
    monkeypatch.setenv("PAPERSCOPE_DEMO", "1")
    app = AppTest.from_file(str(ROOT / "gui_app.py"), default_timeout=25).run()
    app.radio(key="navigation").set_value("深度分析").run()
    assert not app.exception
    assert any("暂无论文" in item.value for item in app.info)
    manager(str(tmp_path)).close()
