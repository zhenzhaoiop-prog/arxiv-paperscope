from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util
import json
import logging
import os
import re
import sys
from zoneinfo import ZoneInfo

from .arxiv import import_legacy
from .config import ROOT, load_config, now_shanghai, schedule_cron
from .costs import Budget
from .deepseek import redact
from .notifications import notify
from .persistence import DataBranch
from .runner import run
from .storage import Store


class SafeLog(logging.Handler):
    def __init__(self, path):
        super().__init__()
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record):
        entry = {"time": now_shanghai().isoformat(), "level": record.levelname,
                 "message": redact(record.getMessage())}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sync_schedule(cfg, check=False, root=ROOT):
    path = Path(root) / ".github/workflows/daily.yml"
    source = path.read_text(encoding="utf-8")
    pattern = r"(?m)^(\s+- cron: ')[^']+('\s*# tracker-schedule)$"
    expected = re.sub(pattern, lambda m: m[1] + schedule_cron(cfg) + m[2], source)
    if expected == source and not re.search(pattern, source):
        raise ValueError("未找到工作流调度标记")
    if check and expected != source:
        raise ValueError("YAML时间与Actions cron不一致，请运行 sync-schedule 并提交工作流")
    if not check and expected != source:
        path.write_text(expected, encoding="utf-8")
    return schedule_cron(cfg)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DeepSeek arXiv 凝聚态科研追踪")
    parser.add_argument("--config", help="独立 YAML 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "import-legacy", "search", "costs", "doctor", "sync-schedule", "notify", "restore", "persist"):
        child = sub.add_parser(command)
        child.add_argument("--directory", help="独立状态目录")
        if command in ("run", "notify"):
            child.add_argument("--date", help="报告日期 YYYY-MM-DD，费用仍按实际调用日期")
        if command == "run":
            child.add_argument("--history-days", type=int)
            child.add_argument("--retrieve-only", action="store_true")
            child.add_argument("--mock", action="store_true")
        if command == "import-legacy":
            child.add_argument("--source", default="data")
        if command == "search":
            child.add_argument("query", help="标题、摘要或 arXiv ID 子串")
        if command == "costs":
            child.add_argument("--month", help="YYYY-MM")
        if command == "sync-schedule":
            child.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    store = None
    try:
        cfg = load_config(args.config)
        directory = Path(args.directory or cfg["storage"]["directory"])
        if args.command == "run":
            if args.mock and not args.directory:
                directory = Path("demo-state")
            log = SafeLog(directory / "logs" / f"{args.date or now_shanghai().date().isoformat()}.jsonl")
            logging.getLogger("arxiv_tracker").addHandler(log)
            clock = now_shanghai
            if args.mock:
                day = args.date or now_shanghai().date().isoformat()
                moment = datetime.fromisoformat(day + "T12:30:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                clock = lambda: moment
            try:
                path, payload, code = run(cfg, directory, args.date, args.history_days, args.retrieve_only, args.mock, clock=clock)
                print(json.dumps({"report": str(path.resolve()), "new_candidates": payload["new_candidates"],
                                  "pending_screening": payload["pending_screening"], "errors": payload["errors"],
                                  "mock": args.mock}, ensure_ascii=False, indent=2))
                return code
            finally:
                logging.getLogger("arxiv_tracker").removeHandler(log)
        if args.command == "sync-schedule":
            print(sync_schedule(cfg, args.check))
            return 0
        if args.command == "doctor":
            checks = {"python_3_11": sys.version_info >= (3, 11),
                      "configuration": True, "schedule_cron": schedule_cron(cfg),
                      "deepseek_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
                      "dependencies": {m: importlib.util.find_spec(m) is not None for m in ("requests", "yaml", "pypdf", "tzdata")},
                      "github_environment_present": bool(os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN"))}
            sync_schedule(cfg, check=True)
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return 0 if checks["python_3_11"] and all(checks["dependencies"].values()) else 1
        if args.command in ("restore", "persist"):
            branch = DataBranch(directory)
            getattr(branch, args.command)()
            print(f"data branch {args.command}: OK")
            return 0
        if args.command == "notify":
            errors = notify(cfg, directory, args.date or now_shanghai().date().isoformat())
            print(json.dumps({"notification_errors": errors}, ensure_ascii=False))
            return 1 if errors else 0
        store = Store(directory / "tracker.sqlite3")
        if args.command == "import-legacy":
            end = now_shanghai().astimezone(timezone.utc)
            count = import_legacy(store, Path(args.source), end.date().isoformat(),
                                  end - timedelta(days=cfg["arxiv"]["initial_days"]), end)
            print(f"Imported {count} records; old metadata is not marked AI-analyzed.")
        elif args.command == "search":
            query = args.query.casefold()
            results = [dict(p, relevance=store.result(p["key"], "review") or store.result(p["key"], "screening"))
                       for p in store.papers() if query in (p["id"] + " " + p["title"] + " " + p["abstract"]).casefold()]
            print(json.dumps(results, ensure_ascii=False, indent=2))
        elif args.command == "costs":
            print(json.dumps(Budget(store, cfg, now_shanghai).totals(args.month), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # No traceback/raw HTTP bodies: these can contain credentials or full source text.
        message = redact(str(exc)) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(f"错误：{message}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
