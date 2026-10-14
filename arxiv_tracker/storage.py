from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
from datetime import datetime, timezone


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS papers(
          key TEXT PRIMARY KEY, arxiv_id TEXT NOT NULL, version INTEGER,
          metadata TEXT NOT NULL, eligible INTEGER NOT NULL DEFAULT 0,
          discovered_on TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS paper_id ON papers(arxiv_id);
        CREATE TABLE IF NOT EXISTS stages(
          paper_key TEXT NOT NULL REFERENCES papers(key), stage TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          updated TEXT NOT NULL, PRIMARY KEY(paper_key, stage));
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY, date TEXT NOT NULL, started TEXT NOT NULL,
          finished TEXT, status TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS costs(
          id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, month TEXT NOT NULL,
          date TEXT NOT NULL, paper_key TEXT NOT NULL, stage TEXT NOT NULL,
          model TEXT NOT NULL, status TEXT NOT NULL, reserved REAL NOT NULL,
          cost REAL NOT NULL DEFAULT 0, usage TEXT, rates TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS selections(
          date TEXT NOT NULL, paper_key TEXT NOT NULL REFERENCES papers(key),
          kind TEXT NOT NULL, PRIMARY KEY(date,paper_key,kind));
        CREATE TABLE IF NOT EXISTS notifications(
          date TEXT NOT NULL, channel TEXT NOT NULL, result TEXT NOT NULL,
          PRIMARY KEY(date,channel));
        """)
        # A reservation survives interruption: we cannot know whether the provider charged it.
        self.db.execute("UPDATE costs SET status='uncertain' WHERE status='reserved'")
        self.db.commit()

    def close(self):
        self.db.close()

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, encode(value)))

    def upsert(self, paper: dict, date: str, eligible: bool = True) -> bool:
        key = paper["key"]
        existing = self.db.execute("SELECT eligible FROM papers WHERE key=?", (key,)).fetchone()
        with self.db:
            if existing:
                self.db.execute("UPDATE papers SET metadata=?,eligible=MAX(eligible,?) WHERE key=?",
                                (encode(paper), int(eligible), key))
            else:
                self.db.execute("INSERT INTO papers VALUES (?,?,?,?,?,?)",
                                (key, paper["id"], paper["version"], encode(paper), int(eligible), date))
        return existing is None

    def papers(self, eligible: bool | None = None) -> list[dict]:
        query = "SELECT metadata, discovered_on FROM papers"
        params = ()
        if eligible is not None:
            query += " WHERE eligible=?"
            params = (int(eligible),)
        query += " ORDER BY discovered_on, key"
        return [dict(json.loads(r[0]), discovered_on=r[1]) for r in self.db.execute(query, params)]

    def result(self, key: str, stage: str):
        row = self.db.execute("SELECT result FROM stages WHERE paper_key=? AND stage=? AND status='done'",
                              (key, stage)).fetchone()
        return json.loads(row[0]) if row else None

    def stage_status(self, key, stage):
        row = self.db.execute("SELECT status FROM stages WHERE paper_key=? AND stage=?", (key, stage)).fetchone()
        return row[0] if row else "pending"

    def save_stage(self, key, stage, status, result=None, error=None):
        with self.db:
            self.db.execute("""INSERT INTO stages VALUES (?,?,?,?,?,1,?)
            ON CONFLICT(paper_key,stage) DO UPDATE SET status=excluded.status,result=excluded.result,
            error=excluded.error,attempts=stages.attempts+1,updated=excluded.updated""",
                            (key, stage, status, encode(result) if result is not None else None,
                             error, datetime.now(timezone.utc).isoformat()))

    def start_run(self, date):
        with self.db:
            cur = self.db.execute("INSERT INTO runs(date,started,status) VALUES (?,?,'running')",
                                  (date, datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid

    def finish_run(self, run_id, status, error=None):
        with self.db:
            self.db.execute("UPDATE runs SET finished=?,status=?,error=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), status, error, run_id))

    def select(self, date, key, kind):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO selections VALUES (?,?,?)", (date, key, kind))

    def selections(self, date, kind):
        return [r[0] for r in self.db.execute("SELECT paper_key FROM selections WHERE date=? AND kind=?",
                                            (date, kind))]

    def notification(self, date, channel):
        row = self.db.execute("SELECT result FROM notifications WHERE date=? AND channel=?", (date, channel)).fetchone()
        return json.loads(row[0]) if row else None

    def save_notification(self, date, channel, result):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO notifications VALUES (?,?,?)", (date, channel, encode(result)))

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)


@contextmanager
def workspace_lock(directory: Path):
    """OS advisory lock releases on crash, unlike a stale lock-file sentinel."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+b") as handle:
        try:
            # Reading a byte locked by another Windows process can itself fail.
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有本地任务运行，稍后重试") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
