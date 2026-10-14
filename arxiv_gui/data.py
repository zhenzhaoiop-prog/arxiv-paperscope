from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from arxiv_tracker.config import ROOT, now_shanghai
from arxiv_tracker.arxiv import split_id, paper_key


@contextmanager
def readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    try:
        yield connection
    finally:
        connection.close()


def validate_database(path):
    required = {"papers": {"key", "arxiv_id", "version", "metadata", "eligible", "discovered_on"},
                "stages": {"paper_key", "stage", "status", "result", "error", "attempts", "updated"},
                "costs": {"id", "timestamp", "month", "date", "paper_key", "stage", "model", "status", "reserved", "cost", "usage", "rates"},
                "runs": {"id", "date", "started", "finished", "status", "error"},
                "meta": {"key", "value"}}
    with readonly(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite完整性检查失败")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in required.items():
            if table not in tables:
                raise ValueError(f"数据库缺少{table}表")
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise ValueError(f"数据库{table}结构不兼容")


def snapshot_directory(home, repository):
    import hashlib
    base = Path(home) / "cloud" / hashlib.sha256(repository.encode()).hexdigest()[:16]
    manifest = base / "current.json"
    if not repository or not manifest.exists():
        return None
    current = json.loads(manifest.read_text(encoding="utf-8"))
    folder = base / "snapshots" / current.get("snapshot_name", current["head"])
    return folder if (folder / "tracker.sqlite3").exists() else None


def sources(home, repository="", demo=False):
    home = Path(home)
    result = []
    local = home / ("demo" if demo else "local")
    if (local / "tracker.sqlite3").exists():
        result.append(("本地演示" if demo else "本地任务", local / "tracker.sqlite3"))
    if not demo:
        cloud = snapshot_directory(home, repository)
        if cloud:
            result.append((f"GitHub · {repository}", cloud / "tracker.sqlite3"))
    return result


def read_papers(source_list, legacy=True):
    records = {}
    for origin, path in source_list:
        with readonly(path) as connection:
            mode = connection.execute("SELECT value FROM meta WHERE key='mock'").fetchone()
            mock = bool(json.loads(mode[0])) if mode else False
            stages = {}
            for row in connection.execute("SELECT * FROM stages"):
                stages.setdefault(row["paper_key"], {})[row["stage"]] = dict(row)
            for row in connection.execute("SELECT * FROM papers"):
                paper = json.loads(row["metadata"])
                key = paper["key"]
                existing = records.setdefault(key, {"paper": dict(paper, discovered_on=row["discovered_on"]),
                                                     "stages": {}, "origins": [], "mock": mock, "eligible": bool(row["eligible"])})
                existing["mock"] = existing["mock"] or mock
                existing["eligible"] = existing["eligible"] or bool(row["eligible"])
                if paper["updated"] >= existing["paper"]["updated"]:
                    existing["paper"] = dict(paper, discovered_on=row["discovered_on"])
                existing["origins"].append(origin)
                for stage, value in stages.get(key, {}).items():
                    prior = existing["stages"].get(stage)
                    if not prior or (value["status"] == "done" and prior["status"] != "done") or (value["status"] == prior["status"] and value["updated"] > prior["updated"]):
                        existing["stages"][stage] = value
    if legacy and not records:
        path = ROOT / "legacy/papers_with_abstracts.json"
        if path.exists():
            for paper in json.loads(path.read_text(encoding="utf-8")).get("papers", []):
                aid, version = split_id(paper["id"])
                paper = dict(paper, id=aid, version=version, key=paper_key(aid, version),
                             discovered_on=paper.get("published", "")[:10])
                records[paper["key"]] = {"paper": paper, "stages": {}, "origins": ["历史元数据（尚未AI分析）"], "mock": False, "eligible": False}
    for record in records.values():
        for stage in ("screening", "review", "summary", "deep"):
            row = record["stages"].get(stage)
            record[stage] = json.loads(row["result"]) if row and row["status"] == "done" and row["result"] else None
        record["score"] = record["review"] or record["screening"]
    return list(records.values())


def filter_papers(records, query="", fields=None, label="全部", favorite_ids=None, latest=True, order="相关度优先"):
    if latest:
        newest = {}
        for record in records:
            p = record["paper"]
            newest[p["id"]] = max(newest.get(p["id"], -1), p["version"] or 0)
        records = [r for r in records if (r["paper"]["version"] or 0) == newest[r["paper"]["id"]]]
    result = []
    for record in records:
        score = record["score"] or {}
        corpus = json.dumps({"paper": record["paper"], "score": score, "summary": record["summary"], "deep": record["deep"]}, ensure_ascii=False).casefold()
        if query.casefold().strip() not in corpus:
            continue
        if fields and not set(fields).intersection(score.get("categories", []) + record["paper"].get("categories", [])):
            continue
        if label != "全部" and score.get("label", "待分析") != label:
            continue
        if favorite_ids is not None and record["paper"]["id"] not in favorite_ids:
            continue
        result.append(record)
    if order == "日期优先":
        return sorted(result, key=lambda r: r["paper"]["updated"], reverse=True)
    return sorted(result, key=lambda r: (-(r["score"] or {}).get("priority", 0), -(r["score"] or {}).get("score", 0), r["paper"]["key"]))


def cost_totals(source_list, month=None, date=None):
    month = month or now_shanghai().strftime("%Y-%m")
    actual = reserved = 0
    groups = {}
    seen = set()
    for origin, path in source_list:
        with readonly(path) as connection:
            sql = "SELECT * FROM costs WHERE month=?" + (" AND date=?" if date else "")
            params = (month, date) if date else (month,)
            for row in connection.execute(sql, params):
                # Snapshot identity excludes auto-increment ID, preventing repeated sync duplication.
                identity = (origin, row["id"], row["timestamp"])
                if identity in seen:
                    continue
                seen.add(identity)
                cost = row["cost"]
                uncertainty = row["reserved"] if row["status"] in ("reserved", "uncertain") else 0
                actual += cost
                reserved += uncertainty
                group = groups.setdefault(row["stage"], {"estimated_cny": 0, "uncertain_reserved_cny": 0, "calls": 0})
                group["estimated_cny"] += cost
                group["uncertain_reserved_cny"] += uncertainty
                group["calls"] += 1
    return {"month": month, "estimated_cny": round(actual, 12), "uncertain_reserved_cny": round(reserved, 12),
            "committed_cny": round(actual + reserved, 12), "stages": groups}


def run_records(source_list):
    result = []
    for origin, path in source_list:
        with readonly(path) as connection:
            result.extend(dict(row, origin=origin) for row in connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30"))
    return sorted(result, key=lambda r: r["started"], reverse=True)


def subscription_stats(source_list):
    rows = []
    for origin, path in source_list:
        with readonly(path) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscription_calls'").fetchone():
                rows.extend(dict(row, origin=origin) for row in connection.execute("SELECT * FROM subscription_calls ORDER BY id DESC LIMIT 100"))
    return rows
