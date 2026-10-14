from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.example.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("配置必须为 YAML 对象")
    for key in ("schedule", "storage", "arxiv", "research", "scoring", "selection",
                "deepseek", "pdf", "budget", "pricing", "notifications"):
        if key not in cfg:
            raise ValueError(f"缺少配置项 {key}")
    if cfg["schedule"]["timezone"] != "Asia/Shanghai":
        raise ValueError("当前调度配置使用 Asia/Shanghai")
    datetime.strptime(cfg["schedule"]["time"], "%H:%M")
    if cfg["deepseek"]["base_url"].rstrip("/") != "https://api.deepseek.com":
        raise ValueError("AI 服务必须为 DeepSeek 官方 API")
    if cfg["arxiv"]["endpoint"] != "https://export.arxiv.org/api/query":
        raise ValueError("检索端点必须为官方 arXiv API")
    if cfg["arxiv"]["request_interval"] < 3 or not 1 <= cfg["arxiv"]["page_size"] <= 1000:
        raise ValueError("arXiv 请求间隔至少 3 秒，页大小 1–1000")
    for section in ("arxiv", "deepseek"):
        if cfg[section]["retries"] < 0 or cfg[section]["timeout"] <= 0:
            raise ValueError("重试次数不能为负，超时必须为正")
    if cfg["arxiv"]["initial_days"] < 1 or cfg["arxiv"]["overlap_days"] < 1:
        raise ValueError("补检和重叠窗口至少 1 天")
    if cfg["budget"]["input_safety_factor"] < 1:
        raise ValueError("输入预留安全系数至少为 1")
    for model, prices in cfg["pricing"]["models"].items():
        for period in ("peak", "off_peak"):
            if any(prices[period][k] < 0 for k in ("cache_hit", "cache_miss", "output")):
                raise ValueError(f"{model} 价格不能为负")
    if any(cfg["pdf"][k] <= 0 for k in ("max_bytes", "max_pages", "max_characters", "timeout", "minimum_text_characters")):
        raise ValueError("PDF限制必须为正")
    if cfg["budget"]["monthly_cny"] <= 0:
        raise ValueError("月预算必须大于 0")
    if not isinstance(cfg["budget"].get("daily_call_limit", 0), int) or cfg["budget"].get("daily_call_limit", 0) < 0:
        raise ValueError("每日API调用上限必须为非负整数，0表示不限次数")
    if not 0 < cfg["budget"]["warning_fraction"] <= cfg["budget"]["stop_deep_fraction"] <= 1:
        raise ValueError("预算阈值应满足 0 < 预警 <= 停止深度分析 <= 1")
    dims = cfg["scoring"]["dimensions"]
    if set(dims) != {"material", "physics", "methods", "novelty", "value"} or any(v <= 0 for v in dims.values()):
        raise ValueError("五项评分权重必须为正")
    if not 0 <= cfg["scoring"]["potential_threshold"] < cfg["scoring"]["direct_threshold"] <= 100:
        raise ValueError("评分阈值无效")
    if any(i["weight"] <= 0 or not i["keywords"] for i in cfg["research"]["interests"]):
        raise ValueError("兴趣权重必须为正，关键词不能为空")
    for stage in ("screening", "review", "summary", "deep"):
        spec = cfg["deepseek"]["stages"][stage]
        if spec["model"] not in cfg["pricing"]["models"]:
            raise ValueError(f"模型 {spec['model']} 缺少价格配置")
        if spec["effort"] not in ("none", "low", "high", "max"):
            raise ValueError("思考强度无效")
        if bool(spec["thinking"]) != (spec["effort"] != "none"):
            raise ValueError("thinking 与 effort 设置矛盾")
        if not 1 <= spec["max_tokens"] <= 393216:
            raise ValueError("输出 token 上限无效")
    for key in ("summaries_per_day", "deep_per_day"):
        if cfg["selection"][key] < 0:
            raise ValueError("论文数量不能为负")
    cfg["_path"] = str(path.resolve())
    return cfg


def now_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def schedule_cron(cfg: dict) -> str:
    local = datetime.strptime(cfg["schedule"]["time"], "%H:%M") - timedelta(hours=8)
    return f"{local.minute} {local.hour} * * *"
