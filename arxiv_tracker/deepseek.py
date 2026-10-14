from __future__ import annotations

import json
import os
import re
import time
import requests


class AIError(RuntimeError):
    pass


class APIUnavailable(AIError):
    pass


class UncertainCall(AIError):
    pass


def redact(message: str) -> str:
    for name in ("DEEPSEEK_API_KEY", "GITHUB_TOKEN", "SMTP_PASSWORD"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)


def parse_json(content: str) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("AI 返回空内容")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    def reject_constant(_):
        raise ValueError("JSON 不允许 NaN/Infinity")
    value = json.loads(content, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("AI JSON 必须为对象")
    return value


class DeepSeekClient:
    def __init__(self, cfg, budget, session=None, sleep=time.sleep, api_key=None):
        self.cfg, self.budget = cfg, budget
        self.session, self.sleep = session or requests.Session(), sleep
        self.key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self.key:
            raise APIUnavailable("未配置 DEEPSEEK_API_KEY；可使用 --mock 演示")

    def generate(self, key, stage, messages, validator):
        spec = self.cfg["deepseek"]["stages"][stage]
        payload = {"model": spec["model"], "messages": messages,
                   "thinking": {"type": "enabled" if spec["thinking"] else "disabled"},
                   "reasoning_effort": spec["effort"], "max_tokens": spec["max_tokens"],
                   "response_format": {"type": "json_object"}, "stream": False}
        if not spec["thinking"]:
            payload["temperature"] = 0.2
        for attempt in range(self.cfg["deepseek"]["retries"] + 1):
            request_id, started = self.budget.reserve(key, stage, spec["model"], messages, spec["max_tokens"])
            try:
                response = self.session.post(self.cfg["deepseek"]["base_url"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json=payload, timeout=self.cfg["deepseek"]["timeout"], allow_redirects=False)
            except requests.RequestException as exc:
                self.budget.uncertain(request_id)
                # Never automatically resend a request whose billing status is unknown.
                raise UncertainCall(f"AI 网络异常（{type(exc).__name__}）；保留费用预留") from exc
            if response.status_code == 429:
                self.budget.release(request_id)
                if attempt < self.cfg["deepseek"]["retries"]:
                    self.sleep(min(60, 3 * 2 ** attempt))
                    continue
                raise APIUnavailable("DeepSeek 限流，稍后重跑")
            if response.status_code in (400, 401, 402, 403, 404):
                self.budget.release(request_id)
                raise APIUnavailable(f"DeepSeek HTTP {response.status_code}；检查模型配置、密钥或余额")
            if response.status_code != 200:
                self.budget.uncertain(request_id)
                raise UncertainCall(f"DeepSeek HTTP {response.status_code}；扣费未知，保留预留")
            try:
                body = response.json()
                # Settle BEFORE looking at choices/content, including invalid JSON responses.
                self.budget.settle(request_id, spec["model"], started, body["usage"])
            except (ValueError, KeyError, TypeError) as exc:
                self.budget.uncertain(request_id)
                raise UncertainCall("无法验证 usage；保留预留，不自动重发") from exc
            try:
                choice = body["choices"][0]
                if choice["finish_reason"] != "stop":
                    raise ValueError("AI 输出未正常结束")
                result = parse_json(choice["message"]["content"])
                return validator(result)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                if attempt == self.cfg["deepseek"]["retries"]:
                    raise AIError(f"AI 结构或来源校验失败（{type(exc).__name__}），已记录费用") from exc
        raise AIError("AI 重试耗尽")
