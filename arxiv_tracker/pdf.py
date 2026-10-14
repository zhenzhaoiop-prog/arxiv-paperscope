from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse
import requests

from .summary import abstract_source


class PDFError(RuntimeError):
    pass


def download(url, destination, cfg, session, arxiv_client=None, supplementary=False):
    parsed = urlparse(url)
    hosts = cfg["pdf"]["allowed_supplement_hosts"] if supplementary else ["arxiv.org", "export.arxiv.org"]
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise PDFError("PDF URL 不在允许的 HTTPS 来源中")
    if arxiv_client and parsed.hostname in ("arxiv.org", "export.arxiv.org"):
        arxiv_client.wait()
    # Follow only validated redirects, never credentials or arbitrary external URLs.
    for _ in range(5):
        with session.get(url, headers={"User-Agent": cfg["arxiv"]["user_agent"]},
                         timeout=cfg["pdf"]["timeout"], stream=True, allow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get("Location", ""))
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
                    raise PDFError("PDF 重定向来源不被允许")
                if arxiv_client and parsed.hostname in ("arxiv.org", "export.arxiv.org"):
                    arxiv_client.wait()
                continue
            if response.status_code != 200:
                raise PDFError(f"PDF HTTP {response.status_code}")
            maximum = cfg["pdf"]["max_bytes"]
            if int(response.headers.get("Content-Length", "0")) > maximum:
                raise PDFError("PDF 超过大小上限")
            temp = destination.with_suffix(".part")
            try:
                size = 0
                with temp.open("wb") as handle:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > maximum:
                            raise PDFError("PDF 超过大小上限")
                        handle.write(chunk)
                if not temp.read_bytes().startswith(b"%PDF-"):
                    raise PDFError("下载内容不是 PDF")
                temp.replace(destination)
                return
            finally:
                temp.unlink(missing_ok=True)
    raise PDFError("PDF 重定向次数过多")


def extract_pdf(path, cfg):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PDFError("未安装 pypdf") from exc
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(path)
        count = len(reader.pages)
        parts, pages = [], {}
        available = cfg["pdf"]["max_characters"]
        truncated = count > cfg["pdf"]["max_pages"]
        for index, page in enumerate(reader.pages[:cfg["pdf"]["max_pages"]], 1):
            text = page.extract_text() or ""
            marker = f"\n[第{index}页]\n"
            if len(text) + len(marker) > available:
                text = text[:max(0, available - len(marker))]
                truncated = True
            if text:
                pages[str(index)] = text
                parts.append(marker + text)
                available -= len(marker) + len(text)
            if available <= len(marker):
                truncated = True
                break
        full = "".join(parts)
        if len(full.strip()) < cfg["pdf"]["minimum_text_characters"]:
            raise PDFError("正文文本过少，可能是扫描版或解析失败")
        limitations = ["仅分析可提取文字与文字图注；未解析图像、曲线坐标和表格视觉布局，不从图像推断数值。"]
        if truncated:
            limitations.append("输入因页数或长度限制被截断，未覆盖完整全文。")
        return {"text": full, "pages": pages,
                "label": "基于部分正文的分析" if truncated else "基于PDF提取正文的分析",
                "limitations": limitations, "page_count": count, "truncated": truncated}
    except PDFError:
        raise
    except Exception as exc:
        raise PDFError(f"PDF 解析失败（{type(exc).__name__}）") from exc


def pdf_source(paper, cfg, directory, arxiv_client=None, session=None):
    cache = Path(directory) / "pdf-cache"
    cache.mkdir(parents=True, exist_ok=True)
    session = session or requests.Session()
    # Unknown version metadata should not accidentally claim a version-specific full text.
    if paper["version"] is None:
        source = abstract_source(paper)
        source["limitations"].append("版本未知，未下载可能已变化的最新PDF。")
        return source
    token = hashlib.sha256(paper["key"].encode()).hexdigest()[:24]
    path = cache / f"{token}.pdf"
    try:
        if not path.exists():
            download(paper["pdf_url"], path, cfg, session, arxiv_client)
        source = extract_pdf(path, cfg)
    except (PDFError, requests.RequestException, ValueError) as exc:
        source = abstract_source(paper)
        source["limitations"].append(f"PDF不可用，降级为摘要（{type(exc).__name__}）。")
        return source
    urls = cfg["pdf"].get("supplementary_urls", {}).get(paper["id"], [])
    if not urls:
        source["limitations"].append("未获取补充材料。")
    for index, url in enumerate(urls, 1):
        extra_path = cache / f"{token}-supp-{index}.pdf"
        try:
            if not extra_path.exists():
                download(url, extra_path, cfg, session, arxiv_client, supplementary=True)
            extra = extract_pdf(extra_path, cfg)
            remaining = cfg["pdf"]["max_characters"] - len(source["text"])
            if remaining < 100:
                source["limitations"].append("补充材料因总长度上限未纳入。")
                continue
            marker = f"\n[补充材料{index}，引用页码请设null]\n"
            addition = extra["text"][:max(0, remaining - len(marker))]
            # Supplemental quotes validated against the combined corpus, page numbers unset.
            source["text"] += marker + addition
            source["limitations"].append(f"补充材料{index}仅提取文字，可能截断；来源：{url}")
        except (PDFError, requests.RequestException, ValueError) as exc:
            source["limitations"].append(f"补充材料{index}未能获取（{type(exc).__name__}）。")
    return source
