"""Atomic data-branch persistence via GitHub Git API; no cache dependency."""
from __future__ import annotations

import base64
from contextlib import closing
import os
from pathlib import Path
import re
import requests

from .notifications import github_request
from .storage import Store


class DataBranch:
    def __init__(self, directory, session=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.repo, self.token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if not self.token or not self.repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            raise ValueError("缺少 GitHub 环境变量")
        self.session = session or requests.Session()
        self.prefix = f"/repos/{self.repo}"

    def api(self, method, path, body=None):
        return github_request(method, self.prefix + path, self.token, body, self.session)

    def head(self):
        response = self.session.get("https://api.github.com" + self.prefix + "/git/ref/heads/data",
                                    headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"},
                                    timeout=60, allow_redirects=False)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"读取data分支 HTTP {response.status_code}")
        return response.json()["object"]["sha"]

    def restore(self):
        head = self.head()
        if head:
            commit = self.api("GET", f"/git/commits/{head}")
            tree = self.api("GET", f"/git/trees/{commit['tree']['sha']}")
            item = next((i for i in tree["tree"] if i["path"] == "tracker.sqlite3"), None)
            if not item:
                raise RuntimeError("data分支缺少数据库，拒绝静默重置")
            blob = self.api("GET", f"/git/blobs/{item['sha']}")
            content = base64.b64decode(blob["content"])
            if not content.startswith(b"SQLite format 3\x00"):
                raise RuntimeError("远程数据库不是有效SQLite")
            temp = self.directory / "tracker.restore"
            temp.write_bytes(content)
            import sqlite3
            with closing(sqlite3.connect(temp)) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("远程数据库完整性检查失败")
            temp.replace(self.directory / "tracker.sqlite3")
        elif (self.directory / "tracker.sqlite3").exists():
            raise RuntimeError("远程data分支缺失但本地已有数据库，请手动核实后恢复")
        (self.directory / ".remote-head").write_text(head or "", encoding="ascii")

    def persist(self):
        marker = self.directory / ".remote-head"
        if not marker.exists():
            raise RuntimeError("未成功restore，禁止覆盖远程状态")
        expected = marker.read_text(encoding="ascii").strip() or None
        if self.head() != expected:
            raise RuntimeError("data分支已变化，拒绝覆盖；使用恢复artifact人工合并")
        database = self.directory / "tracker.sqlite3"
        if not database.exists():
            raise RuntimeError("没有可保存的数据库")
        snapshot = self.directory / "snapshot.sqlite3"
        store = Store(database)
        try:
            store.backup(snapshot)
        finally:
            store.close()
        files = [("tracker.sqlite3", snapshot)]
        for subdirectory, suffixes in (("reports", (".md", ".json")), ("costs", (".json",)), ("logs", (".jsonl",))):
            for path in sorted((self.directory / subdirectory).glob("*")):
                if path.is_file() and path.suffix in suffixes:
                    files.append((f"{subdirectory}/{path.name}", path))
        entries = []
        for name, path in files:
            content = path.read_bytes()
            if len(content) > 90_000_000:
                raise RuntimeError("文件接近GitHub blob限制，需要归档后恢复")
            blob = self.api("POST", "/git/blobs", {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
            entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        body = {"tree": entries}
        if expected:
            parent = self.api("GET", f"/git/commits/{expected}")
            body["base_tree"] = parent["tree"]["sha"]
        tree = self.api("POST", "/git/trees", body)
        # An initial orphan data commit contains only the allowlisted artifacts.
        commit = self.api("POST", "/git/commits", {"message": "Persist arXiv tracker state",
                          "tree": tree["sha"], "parents": [expected] if expected else []})
        if expected:
            self.api("PATCH", "/git/refs/heads/data", {"sha": commit["sha"], "force": False})
        else:
            self.api("POST", "/git/refs", {"ref": "refs/heads/data", "sha": commit["sha"]})
        marker.write_text(commit["sha"], encoding="ascii")
        snapshot.unlink(missing_ok=True)
