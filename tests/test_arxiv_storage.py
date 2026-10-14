from datetime import timedelta
from pathlib import Path
import json
import pytest

from arxiv_tracker.arxiv import ArxivClient, RetrievalError, split_id, parse_atom, import_legacy, window, build_queries
from arxiv_tracker.mock import mock_papers


def atom(papers, total=None):
    from xml.sax.saxutils import escape
    entries = []
    for p in papers:
        entries.append(f'''<entry><id>https://arxiv.org/abs/{p['key']}</id><title>{escape(p['title'])}</title>
        <summary>{escape(p['abstract'])}</summary><published>{p['published']}</published><updated>{p['updated']}</updated>
        <author><name>Researcher</name></author><category term="cond-mat.str-el"/></entry>''')
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
    <o:totalResults>{len(papers) if total is None else total}</o:totalResults>{''.join(entries)}</feed>'''.encode()


def test_atom_and_old_id(moment):
    papers = mock_papers(moment)
    parsed, total = parse_atom(atom(papers))
    assert total == 3 and parsed[0]["version"] == 1
    assert parsed[0]["authors"] == ["Researcher"]
    assert parsed[0]["pdf_url"].endswith("v1")
    assert split_id("https://arxiv.org/abs/cond-mat/9901001v3") == ("cond-mat/9901001", 3)
    assert split_id("2609.12345") == ("2609.12345", None)
    with pytest.raises(RetrievalError):
        parse_atom("<broken>")
    with pytest.raises(RetrievalError):
        parse_atom('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad</id></entry></feed>')


def test_dedup_versions_and_stages(store, moment):
    paper = mock_papers(moment)[0]
    assert store.upsert(paper, "2026-10-14")
    assert not store.upsert(paper, "2026-10-15")
    store.save_stage(paper["key"], "screening", "done", {"score": 90})
    updated = dict(paper, version=2, key=paper["id"] + "v2")
    assert store.upsert(updated, "2026-10-15")
    assert store.result(paper["key"], "screening") == {"score": 90}
    assert store.result(updated["key"], "screening") is None
    assert len(store.papers()) == 2


def test_paging_does_not_cap(cfg, moment):
    papers = mock_papers(moment)
    cfg["arxiv"]["page_size"] = 2
    client = ArxivClient(cfg["arxiv"])
    calls = []
    def page(query, offset):
        calls.append(offset)
        return papers[offset:offset + 2], 3
    client.page = page
    assert len(list(client.retrieve("cat:x", moment - timedelta(days=1), moment))) == 3
    assert calls == [0, 2]
    client.page = lambda q, offset: (papers[:2], 10)
    with pytest.raises(RetrievalError, match="分页重复"):
        list(client.retrieve("x", moment - timedelta(days=1), moment))


def test_boundary_and_empty_page(cfg, moment):
    papers = mock_papers(moment)
    papers[1] = dict(papers[1], updated=(moment - timedelta(days=8)).isoformat())
    client = ArxivClient(cfg["arxiv"])
    client.page = lambda q, offset: (papers[:2], 50)
    assert len(list(client.retrieve("x", moment - timedelta(days=7), moment))) == 1
    client.page = lambda q, offset: ([], 2)
    with pytest.raises(RetrievalError, match="空结果"):
        list(client.retrieve("x", moment - timedelta(days=1), moment))
    client.page = lambda q, offset: ([], 0)
    assert list(client.retrieve("x", moment - timedelta(days=1), moment)) == []


def test_retry_and_rate_limit(cfg, moment):
    class Response:
        def __init__(self, code):
            self.status_code = code
            self.content = atom(mock_papers(moment))
    class Session:
        calls = 0
        def get(self, *a, **kw):
            self.calls += 1
            return Response(429 if self.calls == 1 else 200)
    session = Session()
    sleeps = []
    ticks = iter([0, 0, 0, 0, 0, 0])
    client = ArxivClient(cfg["arxiv"], session, sleep=sleeps.append, clock=lambda: next(ticks))
    assert len(client.page("x", 0)[0]) == 3
    assert session.calls == 2
    assert max(sleeps) >= 3


def test_history_import(store, tmp_path, moment):
    directory = tmp_path / "legacy"
    (directory / "raw").mkdir(parents=True)
    p = mock_papers(moment)[0]
    old = dict(mock_papers(moment)[1], id="2609.12345", updated=(moment - timedelta(days=30)).isoformat())
    (directory / "raw/api_0.xml").write_bytes(atom([p]))
    (directory / "papers_with_abstracts.json").write_text(json.dumps({"papers": [p, old]}), encoding="utf-8")
    assert import_legacy(store, directory, "2026-10-14", moment - timedelta(days=7), moment) == 2
    assert import_legacy(store, directory, "2026-10-14", moment - timedelta(days=7), moment) == 0
    assert len(store.papers(True)) == 1
    assert next(x for x in store.papers() if x["id"] == "2609.12345")["version"] is None
    assert store.result(p["key"], "screening") is None


def test_window_and_broad_query(cfg, moment):
    assert window(cfg, None, moment) == moment - timedelta(days=7)
    assert window(cfg, (moment - timedelta(days=5)).isoformat(), moment) == moment - timedelta(days=8)
    queries = build_queries(cfg)
    assert queries[0].startswith("cat:") and "ti:" not in queries[0]
    assert any('abs:"single crystal"' in q for q in queries)
