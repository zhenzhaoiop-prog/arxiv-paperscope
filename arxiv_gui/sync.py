from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid
import sqlite3
from urllib.parse import quote
import requests

from arxiv_tracker.report import atomic_write
from .data import validate_database


class SyncConflict(RuntimeError):
    pass


class GitHub:
    def __init__(self, repository, token="", session=None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, flags=re.ASCII):
            raise ValueError("仓库格式应为 owner/repo")
        self.repository, self.token, self.session = repository, token, session or requests.Session()

    def request(self, method, endpoint, body=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.session.request(method, f"https://api.github.com/repos/{self.repository}" + endpoint,
                                        headers=headers, json=body, timeout=45, allow_redirects=False)
        if response.status_code >= 300:
            reason = {401: "凭证无效", 403: "权限不足或API限流", 404: "仓库/分支不存在，或私人仓库未授权", 409: "远程状态冲突", 422: "参数或分支状态无效"}.get(response.status_code, "服务暂时不可用")
            raise RuntimeError(f"GitHub HTTP {response.status_code}：{reason}")
        return response.json() if response.content else {}

    def head(self):
        sha = self.request("GET", "/git/ref/heads/data")["object"]["sha"]
        if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
            raise ValueError("无效远程提交ID")
        return sha

    def active_runs(self):
        if not self.token:
            raise ValueError("云端任务协调需要服务端GITHUB_TOKEN及Actions读取权限")
        runs = self.request("GET", "/actions/workflows/daily.yml/runs?per_page=30")["workflow_runs"]
        return [r for r in runs if r["status"] in ("queued", "in_progress", "waiting", "pending", "requested")]

    def dispatch(self, retrieve_only=False):
        if not self.token:
            raise ValueError("远程触发需要服务端GITHUB_TOKEN及Actions写入权限")
        repository = self.request("GET", "")
        self.request("POST", "/actions/workflows/daily.yml/dispatches",
                     {"ref": repository["default_branch"], "inputs": {"retrieve_only": "true" if retrieve_only else "false"}})
        return f"https://github.com/{self.repository}/actions/workflows/daily.yml"


def allowed_path(name):
    if name == "tracker.sqlite3":
        return True
    return bool(re.fullmatch(r"reports/\d{4}-\d{2}-\d{2}\.(?:md|json)|costs/\d{4}-\d{2}\.json|logs/\d{4}-\d{2}-\d{2}\.jsonl", name))


def synchronize(home, repository, token="", gateway=None, progress=None):
    api = gateway or GitHub(repository, token)
    base = Path(home) / "cloud" / hashlib.sha256(repository.encode()).hexdigest()[:16]
    base.mkdir(parents=True, exist_ok=True)
    head = api.head()
    current_path = base / "current.json"
    previous = json.loads(current_path.read_text(encoding="utf-8")) if current_path.exists() else {}
    previous_dir = base / "snapshots" / previous.get("snapshot_name", previous.get("head", "none"))
    if previous.get("head") == head and (previous_dir / "tracker.sqlite3").exists():
        try:
            validate_database(previous_dir / "tracker.sqlite3")
            for name, expected in previous.get("files", {}).items():
                content = (previous_dir / PurePosixPath(name)).read_bytes()
                if hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() != expected:
                    raise ValueError("本地快照文件损坏")
        except (OSError, ValueError, sqlite3.Error):
            pass # Re-download into a new immutable repair snapshot.
        else:
            if api.head() != head:
                raise SyncConflict("下载期间云端提交已变化，请重试同步")
            previous["synced_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write(current_path, json.dumps(previous))
            return {"head": head, "files": len(previous.get("files", {})), "unchanged": True}
    commit = api.request("GET", f"/git/commits/{head}")
    tree = api.request("GET", f"/git/trees/{commit['tree']['sha']}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("远程文件树被截断，拒绝不完整同步")
    files = [r for r in tree["tree"] if r["type"] == "blob" and allowed_path(r["path"])]
    if not any(r["path"] == "tracker.sqlite3" for r in files):
        raise RuntimeError("云端data分支没有有效数据库，请先完成后台运行")
    with tempfile.TemporaryDirectory(prefix="sync-", dir=base) as temporary:
        staging = Path(temporary)
        sha_map = {}
        for index, item in enumerate(files, 1):
            if progress:
                progress("sync", index - 1, len(files), f"正在同步 {item['path']}")
            destination = staging / PurePosixPath(item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            sha_map[item["path"]] = item["sha"]
            existing = previous_dir / PurePosixPath(item["path"])
            if previous.get("files", {}).get(item["path"]) == item["sha"] and existing.exists():
                cached = existing.read_bytes()
                if hashlib.sha1(f"blob {len(cached)}\0".encode() + cached).hexdigest() == item["sha"]:
                    shutil.copyfile(existing, destination)
                    continue
            if item.get("size", 0) > 90_000_000:
                raise ValueError("云端文件超过同步大小上限")
            blob = api.request("GET", f"/git/blobs/{item['sha']}")
            if blob.get("encoding") != "base64":
                raise ValueError("远程blob编码不支持")
            content = base64.b64decode(blob["content"])
            if len(content) > 90_000_000:
                raise ValueError("云端文件超过同步大小上限")
            # Verify Git blob identity, not just HTTP success.
            digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            if len(item["sha"]) == 40 and digest != item["sha"]:
                raise ValueError("远程文件校验和不匹配")
            destination.write_bytes(content)
            if destination.suffix in (".md", ".json", ".jsonl"):
                decoded = content.decode("utf-8")
                if destination.suffix == ".json" and not isinstance(json.loads(decoded), dict):
                    raise ValueError("远程JSON报告或费用文件必须为对象")
                if destination.suffix == ".jsonl":
                    for line in decoded.splitlines():
                        if line.strip() and not isinstance(json.loads(line), dict):
                            raise ValueError("远程日志格式无效")
        validate_database(staging / "tracker.sqlite3")
        if api.head() != head:
            raise SyncConflict("下载期间云端提交已变化，保留本地旧快照，请重试")
        snapshot_name = head
        if (base / "snapshots" / snapshot_name).exists():
            snapshot_name += "-" + uuid.uuid4().hex[:8]
        destination = base / "snapshots" / snapshot_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(destination)
        atomic_write(current_path, json.dumps({"head": head, "repository": repository, "files": sha_map,
                                               "snapshot_name": snapshot_name,
                                               "synced_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return {"head": head, "files": len(files), "unchanged": False}
