from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import math

from .storage import encode


class BudgetExceeded(RuntimeError):
    pass


def peak_time(moment: datetime, cfg: dict) -> bool:
    local = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    day = local.date().isoformat()
    if day in cfg.get("holidays", []):
        return False
    weekday = local.weekday() < 5 or day in cfg.get("workdays", [])
    hour = local.hour + local.minute / 60
    return weekday and (9 <= hour < 12 or 14 <= hour < 18)


def rates_for(model: str, start: datetime, end: datetime, cfg: dict) -> dict:
    # Crossing a billing boundary is conservatively estimated at peak rates.
    if start.date() != end.date() or (end - start).total_seconds() > 1800:
        period = "peak"
    else:
        period = "peak" if peak_time(start, cfg) or peak_time(end, cfg) else "off_peak"
    return dict(cfg["models"][model][period], period=period,
                verified_on=cfg["verified_on"])


def calculate_cost(usage: dict, rates: dict) -> float:
    prompt = usage["prompt_tokens"]
    completion = usage["completion_tokens"]
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", prompt - hit)
    values = (prompt, completion, hit, miss)
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        raise ValueError("usage token 计数无效")
    if hit + miss != prompt:
        raise ValueError("缓存用量与 prompt_tokens 不一致")
    # completion_tokens already includes reasoning_tokens. Do NOT add it twice.
    reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    if reasoning > completion or reasoning < 0:
        raise ValueError("思考用量超过 completion_tokens")
    return (hit * rates["cache_hit"] + miss * rates["cache_miss"] + completion * rates["output"]) / 1_000_000


class Budget:
    def __init__(self, store, cfg, clock):
        self.store, self.cfg, self.clock = store, cfg, clock

    def totals(self, month=None, date=None):
        month = month or self.clock().strftime("%Y-%m")
        params = [month]
        where = "month=?"
        if date:
            where += " AND date=?"
            params.append(date)
        rows = self.store.db.execute(f"SELECT * FROM costs WHERE {where}", params).fetchall()
        stages = {}
        actual = reserved = 0.0
        for row in rows:
            actual += row["cost"]
            uncertain = row["reserved"] if row["status"] in ("reserved", "uncertain") else 0
            reserved += uncertain
            group = stages.setdefault(row["stage"], {"estimated_cny": 0, "uncertain_reserved_cny": 0, "calls": 0})
            group["estimated_cny"] += row["cost"]
            group["uncertain_reserved_cny"] += uncertain
            group["calls"] += 1
        for group in stages.values():
            for key in ("estimated_cny", "uncertain_reserved_cny"):
                group[key] = round(group[key], 12)
        return {"month": month, "date": date, "estimated_cny": round(actual, 12),
                "uncertain_reserved_cny": round(reserved, 12), "committed_cny": round(actual + reserved, 12),
                "limit_cny": self.cfg["budget"]["monthly_cny"], "stages": stages,
                "warning": actual + reserved >= self.cfg["budget"]["monthly_cny"] * self.cfg["budget"]["warning_fraction"]}

    def reserve(self, key, stage, model, messages, max_tokens):
        moment = self.clock()
        # UTF-8 byte count is a conservative upper bound, without a vendor tokenizer.
        input_bound = math.ceil((len(encode(messages).encode("utf-8")) + 256)
                                * self.cfg["budget"]["input_safety_factor"])
        rates = dict(self.cfg["pricing"]["models"][model]["peak"], period="peak",
                     verified_on=self.cfg["pricing"]["verified_on"])
        reservation = (input_bound * rates["cache_miss"] + max_tokens * rates["output"]) / 1_000_000
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            totals = self.totals()
            limit = self.cfg["budget"]["monthly_cny"]
            daily_limit = self.cfg["budget"].get("daily_call_limit", 0)
            if daily_limit and sum(s["calls"] for s in self.totals(date=moment.date().isoformat())["stages"].values()) >= daily_limit:
                raise BudgetExceeded("达到每日API调用次数上限，保留任务待次日恢复")
            if stage == "deep" and totals["committed_cny"] >= limit * self.cfg["budget"]["stop_deep_fraction"]:
                raise BudgetExceeded("已达到停止深度分析阈值")
            if totals["committed_cny"] + reservation > limit:
                raise BudgetExceeded("预算不足以预留下一次 AI 调用")
            cur = self.store.db.execute("""INSERT INTO costs
            (timestamp,month,date,paper_key,stage,model,status,reserved,rates)
            VALUES (?,?,?,?,?,?,'reserved',?,?)""",
                (moment.isoformat(), moment.strftime("%Y-%m"), moment.date().isoformat(),
                 key, stage, model, reservation, encode(rates)))
        return cur.lastrowid, moment

    def settle(self, request_id, model, start, usage):
        rates = rates_for(model, start, self.clock(), self.cfg["pricing"])
        cost = calculate_cost(usage, rates)
        with self.store.db:
            self.store.db.execute("UPDATE costs SET status='settled',cost=?,usage=?,rates=? WHERE id=?",
                                  (cost, encode(usage), encode(rates), request_id))

    def uncertain(self, request_id):
        with self.store.db:
            self.store.db.execute("UPDATE costs SET status='uncertain' WHERE id=?", (request_id,))

    def release(self, request_id):
        with self.store.db:
            self.store.db.execute("UPDATE costs SET status='rejected',reserved=0 WHERE id=?", (request_id,))
