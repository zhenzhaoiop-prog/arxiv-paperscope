from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import uuid
import threading

from arxiv_tracker.config import now_shanghai
from arxiv_tracker.deepseek import DeepSeekClient
from arxiv_tracker.runner import run
from arxiv_tracker.storage import workspace_lock, Store
from .budget import SharedBudget
from .data import sources, read_papers
from .manual import analyze_one
from .settings import Credentials, Settings
from .sync import GitHub, synchronize
from .codex_client import CodexClient, choose_provider

_MANAGERS = {}
_MANAGER_LOCK = threading.Lock()


def get_manager(home):
    key = str(Path(home).resolve())
    with _MANAGER_LOCK:
        current = _MANAGERS.get(key)
        if current is None or current.closed:
            current = TaskManager(key)
            _MANAGERS[key] = current
        return current


class TaskManager:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        # One app process per home. Browser sessions share this manager.
        self._lock = workspace_lock(self.home / "manager-lock")
        self._lock.__enter__()
        self.closed = False
        self.path = self.home / "gui.sqlite3"
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS favorites(arxiv_id TEXT PRIMARY KEY, added TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL, phase TEXT NOT NULL, completed INTEGER NOT NULL, total INTEGER NOT NULL,
              message TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, error TEXT, result TEXT);
            UPDATE jobs SET status='interrupted',error='GUI进程已重启，任务未完成；可重试已保存阶段'
              WHERE status IN ('pending','running');
            """)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperscope")
        atexit.register(self.close)

    def connection(self):
        # SQLite's own context manager commits, but doesn't close: use a wrapper.
        from contextlib import contextmanager
        @contextmanager
        def context():
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                with db:
                    yield db
        return context()

    def close(self):
        if not self.closed:
            self.executor.shutdown(wait=True)
            self._lock.__exit__(None, None, None)
            self.closed = True

    def favorites(self):
        with self.connection() as db:
            return {r[0] for r in db.execute("SELECT arxiv_id FROM favorites")}

    def favorite(self, arxiv_id, enabled):
        with self.connection() as db:
            if enabled:
                db.execute("INSERT OR IGNORE INTO favorites VALUES (?,?)", (arxiv_id, self.timestamp()))
            else:
                db.execute("DELETE FROM favorites WHERE arxiv_id=?", (arxiv_id,))

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat()

    def jobs(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 40")]

    def active(self):
        return any(j["status"] in ("pending", "running") for j in self.jobs())

    def submit(self, kind, payload=None):
        if kind not in ("run", "deep", "sync", "dispatch"):
            raise ValueError("未知任务类型")
        payload = payload or {}
        if kind in ("run", "deep") and not payload.get("mock") and not payload.get("retrieve_only"):
            cfg, _ = Settings(self.home).read()
            selected, reason = choose_provider(cfg, Credentials.load())
            if selected == "none":
                raise ValueError(reason)
        if kind == "dispatch" and not Credentials.load().github:
            raise ValueError("未配置服务端GITHUB_TOKEN，不能远程触发")
        identifier = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running')").fetchone():
                raise RuntimeError("已有任务运行，请等待完成后再操作")
            db.execute("INSERT INTO jobs VALUES (?,?,?,'pending','waiting',0,0,?,?,?,NULL,NULL)",
                       (identifier, kind, json.dumps(payload, ensure_ascii=False), "任务已提交，正在准备", self.timestamp(), self.timestamp()))
        self.executor.submit(self._execute, identifier, kind, payload)
        return identifier

    def update(self, identifier, **values):
        values["updated"] = self.timestamp()
        # Keys are internal constants only, values always bound parameters.
        with self.connection() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", tuple(values.values()) + (identifier,))

    def retry(self, identifier):
        job = next((j for j in self.jobs() if j["id"] == identifier), None)
        if not job or job["status"] not in ("failed", "partial", "interrupted"):
            raise ValueError("只有失败、部分完成或中断任务可以重试")
        return self.submit(job["kind"], json.loads(job["payload"]))

    def _execute(self, identifier, kind, payload):
        credentials = Credentials.load()
        self.update(identifier, status="running", message="任务运行中")
        def progress(phase, completed, total, message):
            self.update(identifier, phase=phase, completed=completed, total=total, message=message)
        try:
            settings = Settings(self.home)
            cfg, _ = settings.read()
            repository = settings.preferences()["repository"]
            mock = bool(payload.get("mock"))
            selected, reason = ("mock", "Mock演示") if mock else choose_provider(cfg, credentials)
            if kind in ("run", "deep") and not payload.get("retrieve_only") and selected == "none":
                raise ValueError(reason)
            local = self.home / ("demo" if mock else "local")
            cloud_sources = [(name, path) for name, path in sources(self.home, repository, False) if name.startswith("GitHub")]
            if kind in ("run", "deep") and selected == "deepseek" and not payload.get("retrieve_only") and repository:
                progress("coordination", 0, 0, "正在核对云端任务状态与最新费用快照")
                api = GitHub(repository, credentials.github)
                if api.active_runs():
                    raise RuntimeError("云端工作流正在运行，本地AI任务暂不启动；完成后同步再试")
                # New remote calls may have consumed budget since the last local sync.
                synchronize(self.home, repository, credentials.github, progress=progress)
                cloud_sources = [(name, path) for name, path in sources(self.home, repository, False) if name.startswith("GitHub")]
            factory = lambda store, config, clock: SharedBudget(store, config, clock, cloud_sources if not mock else [])
            client_factory = None if mock else (lambda config, budget: CodexClient(config, budget)) if selected == "codex" else lambda config, budget: DeepSeekClient(config, budget, api_key=credentials.deepseek)
            if kind == "run":
                cached = {r["paper"]["key"]: r for r in read_papers(cloud_sources, legacy=False) if not r.get("mock")} if not mock else None
                if cached:
                    # Transfer metadata and completed stages, never cloud costs or watermarks.
                    with workspace_lock(local):
                        seed = Store(local / "tracker.sqlite3")
                        try:
                            if seed.get_meta("mock") is True:
                                raise ValueError("真实任务目录不能含演示数据")
                            for record in cached.values():
                                if record["eligible"]:
                                    seed.upsert(record["paper"], record["paper"]["discovered_on"], True)
                        finally:
                            seed.close()
                path, data, code = run(cfg, local, retrieve_only=payload.get("retrieve_only", False), mock=mock,
                                       budget_factory=factory, client_factory=client_factory, progress=progress, cached_results=cached,
                                       enforce_api_budget=selected != "codex", analyze_only=payload.get("analyze_only", False))
                result = {"report": str(path), "message": "；".join(data["errors"]) if code else "检索与报告已完成"}
                self.update(identifier, status="partial" if code else "done", result=json.dumps(result, ensure_ascii=False),
                            error=result["message"] if code else None, message=result["message"])
            elif kind == "deep":
                record = payload["record"]
                fresh = read_papers(sources(self.home, repository, mock), legacy=False)
                record = next((r for r in fresh if r["paper"]["key"] == record["paper"]["key"]), record)
                if bool(record.get("mock")) != mock:
                    raise ValueError("论文数据来源与演示模式不一致，禁止混用真实AI和Mock数据")
                result = analyze_one(cfg, local, record, mock=mock, client_factory=client_factory,
                                     budget_factory=factory, progress=progress, enforce_api_budget=selected != "codex")
                self.update(identifier, status="done", message=result["message"], result=json.dumps(result, ensure_ascii=False))
            elif kind == "sync":
                result = synchronize(self.home, payload["repository"], credentials.github, progress=progress)
                self.update(identifier, status="done", message="已同步最新数据库、日报和费用记录", result=json.dumps(result))
            elif kind == "dispatch":
                url = GitHub(payload["repository"], credentials.github).dispatch(payload.get("retrieve_only", False))
                self.update(identifier, status="done", message="GitHub已接受触发请求；完成状态请在Actions查看", result=json.dumps({"url": url}))
        except Exception as exc:
            self.update(identifier, status="failed", error=credentials.safe_error(exc), message=credentials.safe_error(exc))
