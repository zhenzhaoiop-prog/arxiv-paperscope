from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests

from .config import parse_time

NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom",
      "o": "http://a9.com/-/spec/opensearch/1.1/"}


class RetrievalError(RuntimeError):
    pass


def split_id(value: str) -> tuple[str, int | None]:
    value = unquote(value).strip()
    value = re.sub(r"^https?://[^/]+/(?:abs|pdf)/", "", value)
    value = value.removeprefix("arXiv:").removesuffix(".pdf")
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]+/\d{7})(?:v(\d+))?", value)
    if not match:
        raise ValueError("无效 arXiv ID")
    return match[1], int(match[2]) if match[2] else None


def paper_key(aid: str, version: int | None) -> str:
    return f"{aid}v{version if version is not None else 'unknown'}"


def parse_atom(content: bytes | str) -> tuple[list[dict], int | None]:
    try:
        root = ET.fromstring(content)
        if root.tag != f"{{{NS['a']}}}feed":
            raise ValueError("非 Atom feed")
        total = root.findtext("o:totalResults", namespaces=NS)
        papers = []
        for entry in root.findall("a:entry", NS):
            raw_id = entry.findtext("a:id", "", NS)
            if "api/errors" in raw_id:
                raise ValueError("arXiv 返回 API 错误")
            aid, version = split_id(raw_id)
            published = entry.findtext("a:published", "", NS)
            updated = entry.findtext("a:updated", "", NS)
            parse_time(published)
            parse_time(updated)
            title = " ".join(entry.findtext("a:title", "", NS).split())
            abstract = " ".join(entry.findtext("a:summary", "", NS).split())
            if not title or not abstract:
                raise ValueError("论文标题或摘要缺失")
            suffix = f"v{version}" if version else ""
            papers.append({"key": paper_key(aid, version), "id": aid, "version": version,
                           "title": title, "abstract": abstract,
                           "authors": [a.findtext("a:name", "", NS) for a in entry.findall("a:author", NS)],
                           "published": published, "updated": updated,
                           "categories": [c.attrib["term"] for c in entry.findall("a:category", NS)],
                           "primary_category": (entry.find("x:primary_category", NS).attrib.get("term", "")
                                                if entry.find("x:primary_category", NS) is not None else ""),
                           "url": f"https://arxiv.org/abs/{aid}{suffix}",
                           "pdf_url": f"https://arxiv.org/pdf/{aid}{suffix}",
                           "doi": entry.findtext("x:doi", "", NS)})
        return papers, int(total) if total is not None else None
    except (ET.ParseError, ValueError, KeyError) as exc:
        raise RetrievalError(f"Atom 解析失败: {type(exc).__name__}") from exc


class ArxivClient:
    def __init__(self, cfg: dict, session=None, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self.last_request = None

    def wait(self):
        if self.last_request is not None:
            self.sleep(max(0, self.cfg["request_interval"] - (self.clock() - self.last_request)))
        self.last_request = self.clock()

    def page(self, query, start):
        for attempt in range(self.cfg["retries"] + 1):
            self.wait()
            try:
                response = self.session.get(self.cfg["endpoint"], params={
                    "search_query": query, "start": start, "max_results": self.cfg["page_size"],
                    "sortBy": "lastUpdatedDate", "sortOrder": "descending"},
                    headers={"User-Agent": self.cfg["user_agent"]}, timeout=self.cfg["timeout"])
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetrievalError(f"arXiv HTTP {response.status_code}")
                if response.status_code != 200:
                    raise RetrievalError(f"arXiv HTTP {response.status_code}")
                return parse_atom(response.content)
            except (requests.RequestException, RetrievalError) as exc:
                if attempt == self.cfg["retries"]:
                    raise RetrievalError(f"检索失败（{type(exc).__name__}）") from exc
                self.sleep(min(60, 3 * 2 ** attempt))
        raise RetrievalError("检索重试耗尽")

    def retrieve(self, query, start_time: datetime, end_time: datetime):
        offset = 0
        previous_oldest = None
        page_signatures = set()
        while True:
            papers, total = self.page(query, offset)
            if not papers:
                if total is not None and offset < total:
                    raise RetrievalError("分页提前返回空结果")
                return
            dates = [parse_time(p["updated"]) for p in papers]
            if dates != sorted(dates, reverse=True) or (previous_oldest and dates[0] > previous_oldest):
                raise RetrievalError("更新时间分页顺序不一致，请重试")
            signature = tuple(p["key"] for p in papers)
            if signature in page_signatures:
                raise RetrievalError("分页重复，拒绝推进检索水位")
            page_signatures.add(signature)
            for paper, updated in zip(papers, dates):
                if start_time <= updated <= end_time:
                    yield paper
            if dates[-1] < start_time:
                return
            previous_oldest = dates[-1]
            offset += len(papers)
            if total is not None and offset >= total:
                return
            # Do not assume a short page means EOF when totalResults says otherwise.
            if total is None and len(papers) < self.cfg["page_size"]:
                return


def build_queries(cfg: dict) -> list[str]:
    categories = cfg["arxiv"]["categories"]
    queries = [" OR ".join(f"cat:{c}" for c in categories)] if categories else []
    keywords = list(dict.fromkeys(k for i in cfg["research"]["interests"] for k in i["keywords"]))
    for offset in range(0, len(keywords), 8):
        terms = []
        for keyword in keywords[offset:offset + 8]:
            keyword = keyword.replace('"', " ").replace("\n", " ")
            terms.extend([f'ti:"{keyword}"', f'abs:"{keyword}"'])
        queries.append(" OR ".join(terms))
    if not queries:
        raise ValueError("检索条件为空")
    return queries


def window(cfg: dict, watermark: str | None, end: datetime, history_days=None):
    if history_days is not None:
        if history_days < 1:
            raise ValueError("历史窗口至少 1 天")
        return end - timedelta(days=history_days)
    if watermark:
        return min(parse_time(watermark), end) - timedelta(days=cfg["arxiv"]["overlap_days"])
    return end - timedelta(days=cfg["arxiv"]["initial_days"])


def import_legacy(store, directory: Path, date: str, start: datetime, end: datetime) -> int:
    """Preserve all known metadata, queue only the requested recent time window."""
    by_id = {}
    for path in sorted((directory / "raw").glob("api_*.xml")):
        papers, _ = parse_atom(path.read_bytes())
        for paper in papers:
            previous = by_id.get(paper["id"])
            if not previous or parse_time(paper["updated"]) > parse_time(previous["updated"]):
                by_id[paper["id"]] = paper
    source = directory / "papers_with_abstracts.json"
    if source.exists():
        data = json.loads(source.read_text(encoding="utf-8"))
        for original in data.get("papers", []):
            aid, version = split_id(original["id"])
            if aid in by_id or not original.get("abstract") or not original.get("updated"):
                continue
            authors = original.get("api_authors", original.get("authors", []))
            if isinstance(authors, str):
                authors = [authors]
            suffix = f"v{version}" if version else ""
            by_id[aid] = {"key": paper_key(aid, version), "id": aid, "version": version,
                          "title": original["title"], "authors": authors,
                          "abstract": original["abstract"], "published": original["published"],
                          "updated": original["updated"], "categories": original.get("categories", []),
                          "primary_category": "", "doi": original.get("doi", ""),
                          "url": f"https://arxiv.org/abs/{aid}{suffix}",
                          "pdf_url": f"https://arxiv.org/pdf/{aid}{suffix}"}
    count = 0
    for paper in by_id.values():
        count += store.upsert(paper, date, start <= parse_time(paper["updated"]) <= end)
    return count
