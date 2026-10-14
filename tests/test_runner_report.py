from datetime import timedelta
from copy import deepcopy
import json
from pathlib import Path
import pytest

from arxiv_tracker.runner import run
from arxiv_tracker.mock import MockClient, mock_papers
from arxiv_tracker.storage import Store, workspace_lock
from arxiv_tracker.deepseek import AIError, APIUnavailable
from arxiv_tracker.arxiv import RetrievalError
from arxiv_tracker.pdf import extract_pdf, pdf_source, PDFError


def test_two_days_and_same_day_idempotency(cfg, tmp_path, moment):
    directory = tmp_path / "demo"
    path, payload, code = run(cfg, directory, mock=True, clock=lambda: moment)
    assert code == 0 and payload["mock"] and payload["new_candidates"] == 3
    assert len(payload["summaries"]) == 2 and len(payload["deep_analyses"]) == 2
    report = path.read_text(encoding="utf-8")
    for heading in ("每日概览", "最值得关注", "中文总结", "深度分析", "其他潜在", "API 费用"):
        assert heading in report
    assert "基于摘要" in report and "MOCK" in report
    assert report.index("**研究背景及核心科学问题**") < report.index("**研究对象、材料体系和晶体结构**")
    spent = payload["costs"]["month"]["estimated_cny"]
    _, repeat, _ = run(cfg, directory, mock=True, clock=lambda: moment)
    assert repeat["costs"]["month"]["estimated_cny"] == spent
    next_day = moment + timedelta(days=1)
    _, next_payload, code = run(cfg, directory, mock=True, clock=lambda: next_day)
    assert code == 0 and next_payload["new_candidates"] == 1
    store = Store(directory / "tracker.sqlite3")
    try:
        assert len(store.papers(True)) == 4
        assert store.db.execute("SELECT COUNT(*) FROM stages WHERE stage='screening' AND status='done'").fetchone()[0] == 4
    finally:
        store.close()


def test_stage_failure_resume_without_redo(cfg, tmp_path, moment):
    directory = tmp_path / "demo"
    class Interrupted(MockClient):
        def generate(self, key, stage, messages, validator):
            if key.endswith("00001v1") and stage == "summary":
                raise APIUnavailable("测试中断")
            return super().generate(key, stage, messages, validator)
    _, first, code = run(cfg, directory, mock=True, clock=lambda: moment, client_factory=Interrupted)
    assert code == 1 and len(first["summaries"]) == 1
    calls = []
    class Resumed(MockClient):
        def generate(self, key, stage, messages, validator):
            calls.append((key, stage))
            return super().generate(key, stage, messages, validator)
    _, final, code = run(cfg, directory, mock=True, clock=lambda: moment, client_factory=Resumed)
    assert code == 0 and len(final["summaries"]) == 2
    assert not any(stage in ("screening", "review") for _, stage in calls)
    assert not any(key.endswith("00002v1") and stage == "summary" for key, stage in calls)


def test_failed_retrieval_does_not_advance_watermark(cfg, tmp_path, moment):
    p = mock_papers(moment)[0]
    class Partial:
        def retrieve(self, *args):
            yield p
            raise RetrievalError("simulated failed page")
    _, payload, code = run(cfg, tmp_path / "live", retrieve_only=True, arxiv_client=Partial(), clock=lambda: moment)
    assert code == 1
    store = Store(tmp_path / "live/tracker.sqlite3")
    try:
        assert store.get_meta("watermark") is None
        assert len(store.papers()) == 1
    finally:
        store.close()
    class Success:
        def retrieve(self, *args):
            yield p
    _, payload, code = run(cfg, tmp_path / "live", retrieve_only=True, arxiv_client=Success(), clock=lambda: moment)
    assert code == 0 and payload["new_candidates"] == 1


def test_budget_pause_and_next_month_resume(cfg, tmp_path, moment):
    cfg["budget"]["monthly_cny"] = 0.001
    directory = tmp_path / "demo"
    _, payload, code = run(cfg, directory, mock=True, clock=lambda: moment)
    assert code == 1 and payload["pending_screening"] == 3
    assert payload["costs"]["month"]["estimated_cny"] == 0
    cfg["budget"]["monthly_cny"] = 20
    _, payload, code = run(cfg, directory, mock=True, clock=lambda: moment.replace(month=11))
    assert code == 0 and payload["pending_screening"] == 0
    assert payload["costs"]["month"]["month"] == "2026-11"


def test_missing_key_report_is_honest(cfg, tmp_path, moment, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    class Success:
        def retrieve(self, *args):
            yield mock_papers(moment)[0]
    path, payload, code = run(cfg, tmp_path / "live", arxiv_client=Success(), clock=lambda: moment)
    assert code == 1 and payload["pending_screening"] == 1
    assert not payload["summaries"] and not payload["mock"]
    assert "DEEPSEEK_API_KEY" in path.read_text(encoding="utf-8")


def test_mock_cannot_mix_with_real(cfg, tmp_path, moment):
    directory = tmp_path / "demo"
    run(cfg, directory, mock=True, clock=lambda: moment)
    with pytest.raises(ValueError, match="混用"):
        run(cfg, directory, retrieve_only=True, clock=lambda: moment)


def test_local_lock_and_backup(store, tmp_path):
    with workspace_lock(tmp_path):
        with pytest.raises(RuntimeError, match="已有"):
            with workspace_lock(tmp_path):
                pass
    store.set_meta("watermark", "test")
    store.backup(tmp_path / "copy.sqlite3")
    copy = Store(tmp_path / "copy.sqlite3")
    try:
        assert copy.get_meta("watermark") == "test"
    finally:
        copy.close()


def test_pdf_invalid_and_degradation(cfg, tmp_path, moment):
    broken = tmp_path / "bad.pdf"
    broken.write_bytes(b"%PDF-invalid")
    with pytest.raises(PDFError):
        extract_pdf(broken, cfg)
    paper = dict(mock_papers(moment)[0], version=None)
    source = pdf_source(paper, cfg, tmp_path)
    assert source["label"] == "基于摘要的总结"
    assert any("版本未知" in note for note in source["limitations"])


def test_pdf_page_extraction_limits(cfg, tmp_path):
    # A real syntactically valid PDF, with a text stream, built without reportlab.
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 700 Td (Single crystals reveal a phase transition at 2 K.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = tmp_path / "valid.pdf"
    writer.write(pdf)
    cfg["pdf"]["minimum_text_characters"] = 10
    source = extract_pdf(pdf, cfg)
    assert "2 K" in source["text"] and "1" in source["pages"]
    assert not source["truncated"]
    cfg["pdf"]["max_characters"] = 30
    source = extract_pdf(pdf, cfg)
    assert source["truncated"] and "部分正文" in source["label"]
