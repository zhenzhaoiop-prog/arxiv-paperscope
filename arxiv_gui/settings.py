from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
import yaml

from arxiv_tracker.config import ROOT, load_config
from arxiv_tracker.report import atomic_write
from arxiv_tracker.storage import workspace_lock


class ConfigConflict(ValueError):
    pass


def revision(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Settings:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(os.environ.get("PAPERSCOPE_CONFIG", self.home / "config.local.yaml"))
        if not self.config_path.exists():
            with workspace_lock(self.home / "settings-lock"):
                if not self.config_path.exists():
                    atomic_write(self.config_path, (ROOT / "config.example.yaml").read_text(encoding="utf-8"))

    def read(self):
        return load_config(self.config_path), revision(self.config_path)

    def save(self, candidate, expected_revision):
        candidate = deepcopy(candidate)
        candidate.pop("_path", None)
        def reject_secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in ("api_key", "github_token", "deepseek_api_key", "password", "smtp_password"):
                        raise ValueError("配置中不得保存API密钥或密码")
                    reject_secrets(item)
            elif isinstance(value, list):
                for item in value:
                    reject_secrets(item)
        reject_secrets(candidate)
        validate_gui_config(candidate)
        text = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False)
        with workspace_lock(self.home / "settings-lock"):
            if revision(self.config_path) != expected_revision:
                raise ConfigConflict("配置已被其他窗口修改，请重新加载后再保存")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", dir=self.home, encoding="utf-8", delete=False) as temp:
                temp.write(text)
                path = Path(temp.name)
            try:
                load_config(path)
            finally:
                path.unlink(missing_ok=True)
            atomic_write(self.config_path, text)
        return revision(self.config_path)

    def preferences(self):
        path = self.home / "preferences.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"repository": ""}

    def save_repository(self, repository):
        import re
        repository = repository.strip()
        if repository and not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, flags=re.ASCII):
            raise ValueError("仓库格式应为 owner/repo，不含URL或凭证")
        atomic_write(self.home / "preferences.json", json.dumps({"repository": repository}, ensure_ascii=False))


def validate_gui_config(cfg):
    import math
    def walk(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("参数不能为 NaN 或无穷值")
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        if isinstance(value, list):
            for item in value:
                walk(item)
    walk(cfg)
    interests = cfg["research"]["interests"]
    if not interests or len({i["name"].strip() for i in interests}) != len(interests):
        raise ValueError("至少一个研究方向，名称不能重复")
    for item in interests:
        if not item["name"].strip() or not item["keywords"] or not all(isinstance(k, str) and k.strip() for k in item["keywords"]):
            raise ValueError("方向名称和关键词不能为空")
    for key in ("summaries_per_day", "deep_per_day"):
        value = cfg["selection"][key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("每日论文数量必须为非负整数")
    for spec in cfg["deepseek"]["stages"].values():
        if spec["model"] not in ("deepseek-flash", "deepseek-v4-pro"):
            raise ValueError("GUI支持DeepSeek官方模型 deepseek-flash / deepseek-v4-pro")
    if not cfg["research"]["description"].strip():
        raise ValueError("研究说明不能为空")
    local = cfg.get("local_ai", {})
    if local.get("provider", "auto") not in ("auto", "codex", "deepseek", "disabled"):
        raise ValueError("本地AI后端无效")
    codex = local.get("codex", {})
    if codex.get("reasoning_effort", "high") not in ("low", "medium", "high", "xhigh"):
        raise ValueError("Codex推理强度无效")
    if codex.get("timeout", 300) <= 0 or not isinstance(codex.get("daily_call_limit", 0), int) or codex.get("daily_call_limit", 0) < 0:
        raise ValueError("Codex超时与调用次数配置无效")
    for category in cfg["arxiv"]["categories"]:
        import re
        if not re.fullmatch(r"[A-Za-z]+(?:[.-][A-Za-z]+)+", category):
            raise ValueError("学科分类格式无效")


@dataclass(repr=False)
class Credentials:
    deepseek: str = field(default="", repr=False)
    github: str = field(default="", repr=False)

    def __repr__(self):
        return "Credentials([REDACTED])"

    @classmethod
    def load(cls, root=ROOT):
        secrets = {}
        path = Path(root) / ".streamlit/secrets.toml"
        if path.exists():
            secrets = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(str(os.environ.get("DEEPSEEK_API_KEY") or secrets.get("DEEPSEEK_API_KEY", "")),
                   str(os.environ.get("GITHUB_TOKEN") or secrets.get("GITHUB_TOKEN", "")))

    def safe_error(self, exc):
        from arxiv_tracker.deepseek import redact
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else f"操作失败（{type(exc).__name__}），请重试"
        for secret in (self.deepseek, self.github):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return redact(message)
