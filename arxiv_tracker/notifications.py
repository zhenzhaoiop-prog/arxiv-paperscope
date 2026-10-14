from __future__ import annotations

from email.message import EmailMessage
import hashlib
import json
import os
import re
import smtplib
import ssl
from pathlib import Path
import requests

from .storage import Store


def github_request(method, path, token, body=None, session=None):
    response = (session or requests).request(method, "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}, json=body, timeout=60, allow_redirects=False)
    if response.status_code >= 300:
        raise RuntimeError(f"GitHub API HTTP {response.status_code}")
    return response.json() if response.content else {}


def issue_body(repo, date, payload):
    marker = f"<!-- arxiv-tracker:{date} -->"
    lines = [marker, f"[阅读完整中文科研报告](https://github.com/{repo}/blob/data/reports/{date}.md)", "",
             f"新候选：{payload['new_candidates']}；待初筛：{payload['pending_screening']}", ""]
    if payload["mock"]:
        lines += ["> MOCK 测试数据，不代表真实文献或真实扣费。", ""]
    pairs = sorted(payload["papers"], key=lambda r: -r["relevance"]["priority"])
    for item in [r for r in pairs if r["relevance"]["label"] != "低相关"][:10]:
        p, s = item["paper"], item["relevance"]
        title = p["title"].replace("[", "\\[").replace("]", "\\]")
        lines += [f"- [{title}]({p['url']}) — {s['score']:.1f}：{s['recommendation']}"]
    costs = payload["costs"]["month"]
    lines += ["", f"本月估算：¥{costs['estimated_cny']:.4f}；未知扣费预留：¥{costs['uncertain_reserved_cny']:.4f}"]
    if payload["errors"]:
        lines += ["", "运行限制："] + [f"- {e}" for e in payload["errors"]]
    return "\n".join(lines)


def notify(cfg, directory, date, session=None):
    directory = Path(directory)
    payload = json.loads((directory / "reports" / f"{date}.json").read_text(encoding="utf-8"))
    if payload["mock"]:
        raise ValueError("Mock 演示禁止发送外部通知")
    store = Store(directory / "tracker.sqlite3")
    errors = []
    try:
        if cfg["notifications"]["github_issues"]:
            try:
                token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
                if not token or not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                    raise ValueError("缺少合法 GITHUB_TOKEN / GITHUB_REPOSITORY")
                body = issue_body(repo, date, payload)
                digest = hashlib.sha256(body.encode()).hexdigest()
                existing = store.notification(date, "github")
                if not existing or existing.get("digest") != digest:
                    number = existing.get("number") if existing else None
                    if number is None:
                        # Search all states and all pages: crash after issue creation is recoverable.
                        page = 1
                        while True:
                            issues = github_request("GET", f"/repos/{repo}/issues?state=all&per_page=100&page={page}", token, session=session)
                            hit = next((i for i in issues if f"<!-- arxiv-tracker:{date} -->" in (i.get("body") or "")), None)
                            if hit:
                                number = hit["number"]
                                break
                            if len(issues) < 100:
                                break
                            page += 1
                    endpoint = f"/repos/{repo}/issues" + (f"/{number}" if number else "")
                    result = github_request("PATCH" if number else "POST", endpoint, token,
                                            {"title": f"{date} arXiv 凝聚态科研简报", "body": body}, session)
                    store.save_notification(date, "github", {"number": result["number"], "digest": digest})
            except Exception as exc:
                errors.append(f"GitHub通知失败（{type(exc).__name__}）")
        if cfg["notifications"]["smtp"]:
            try:
                report = (directory / "reports" / f"{date}.md").read_text(encoding="utf-8")
                digest = hashlib.sha256(report.encode()).hexdigest()
                if store.notification(date, "smtp") != {"digest": digest}:
                    message = EmailMessage()
                    message["Subject"] = f"{date} arXiv 凝聚态科研日报"
                    message["From"] = os.environ["SMTP_FROM"]
                    message["To"] = os.environ["SMTP_TO"]
                    message.set_content(report)
                    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or "587"), timeout=60) as server:
                        server.starttls(context=ssl.create_default_context())
                        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                        server.send_message(message)
                    store.save_notification(date, "smtp", {"digest": digest})
            except Exception as exc:
                errors.append(f"SMTP通知失败（{type(exc).__name__}）")
        return errors
    finally:
        store.close()
