"""LLM 客户端配置

支持多种LLM后端：Anthropic、OpenAI 及兼容接口。
通过环境变量和 .env 文件配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_dotenv(project_root: Path) -> None:
    """加载 .env 文件（若存在）"""
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _parse_bool(value: Any) -> bool | None:
    """宽松解析布尔值：1/true/yes/on → True，0/false/no/off → False，其余 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "disabled"):
        return False
    return None


def _parse_think(override: Any, env_value: str | None) -> bool | dict | None:
    """解析 think 配置。

    - dict 原样保留（可含 ``budget_tokens``）
    - bool 原样保留
    - 字符串 "true"/"false" → bool
    - 其余（None）→ None
    优先 override，其次环境变量。
    """
    if isinstance(override, dict):
        return override
    if isinstance(override, bool):
        return override
    if override is not None:
        return _parse_bool(override)
    return _parse_bool(env_value)


@dataclass
class LLMConfig:
    """LLM 配置

    优先级：实例值 > 环境变量 > 默认值

    支持的 provider：
    - anthropic: 使用 ANTHROPIC_API_KEY
    - openai: 使用 OPENAI_API_KEY
    - openai-compatible: 任意兼容接口，通过 base_url 指定
    """
    provider: str = "openai"          # "anthropic" | "openai" | "openai-compatible"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout: float = 60.0
    max_retries: int = 3
    think: bool | dict | None = None  # 扩展思考：True=默认 budget；dict={"budget_tokens":N}；None=关

    @classmethod
    def from_env(cls, project_root: Path | None = None, **overrides: Any) -> "LLMConfig":
        """从环境变量加载配置

        Args:
            project_root: 项目根目录（用于加载 .env）
            **overrides: 覆盖配置项
        """
        if project_root is None:
            project_root = Path.cwd()
        _load_dotenv(project_root)

        provider = overrides.pop("provider", None) or os.environ.get(
            "LLM_PROVIDER", "openai"
        )

        api_key = overrides.pop("api_key", None)
        if api_key is None:
            if provider == "anthropic":
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            elif provider == "openai":
                api_key = (
                    os.environ.get("OPENAI_API_KEY")
                    or os.environ.get("LLM_APIKEY")
                    or os.environ.get("LLM_API_KEY", "")
                )
            else:
                # 兼容常见的变量名变体
                api_key = (
                    os.environ.get("LLM_APIKEY")
                    or os.environ.get("LLM_API_KEY")
                    or os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("OPENAI_API_KEY", "")
                )

        config = cls(
            provider=provider,
            model=overrides.pop("model", None) or os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            api_key=api_key,
            base_url=(
                overrides.pop("base_url", None)
                or os.environ.get("LLM_BASEURL")       # 兼容无下划线版本
                or os.environ.get("LLM_BASE_URL")
            ),
            max_tokens=int(overrides.pop("max_tokens", None) or os.environ.get("LLM_MAX_TOKENS", "4096")),
            temperature=float(overrides.pop("temperature", None) or os.environ.get("LLM_TEMPERATURE", "0.7")),
            timeout=float(overrides.pop("timeout", None) or os.environ.get("LLM_TIMEOUT", "60.0")),
            max_retries=int(overrides.pop("max_retries", None) or os.environ.get("LLM_MAX_RETRIES", "3")),
            think=_parse_think(overrides.pop("think", None), os.environ.get("LLM_THINK")),
        )
        # 应用剩余覆盖项
        for key, value in overrides.items():
            if hasattr(config, key) and value is not None:
                setattr(config, key, value)
        return config

    def to_client_kwargs(self) -> dict[str, Any]:
        """转为客户端构造参数"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "api_key": self.api_key,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "think": self.think,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key"""
        return bool(self.api_key)
