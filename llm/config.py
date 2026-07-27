"""LLM 客户端配置

支持多种LLM后端：Anthropic、OpenAI 及兼容接口。
通过项目根目录的 config.json 配置::

    {
      "providers": [
        {"name":"deepseek","sdktype":"openai","base_url":"...","api_key_env":"DEEPSEEK_API_KEY",...}
      ],
      "models": [
        {"name":"deepseek-v4-flash","provider":"deepseek","think":true,...}
      ]
    }

- providers：服务商连接信息，api_key 通过 api_key_env 指向 .env 中的变量
- models：模型注册表，provider 字段引用 providers[].name
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _load_dotenv(project_root: Path) -> None:
    """加载 .env 文件到 os.environ（若存在）。"""
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


def _load_config_json(project_root: Path) -> dict[str, Any]:
    """加载 config.json。不存在或格式错误时返回空 dict。"""
    config_path = project_root / "config.json"
    if not config_path.exists():
        log.warning("config.json 未找到: %s", config_path)
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("config.json 解析失败: %s", exc)
        return {}


@dataclass
class LLMConfig:
    """LLM 配置

    优先级：HarnessConfig.api_params > LLMConfig 默认值 > config.json > 硬编码默认值
    API key 通过 .env 中的环境变量注入（provider.api_key_env 指定变量名）。

    支持的 sdktype：
    - openai / openai-compatible: OpenAI 及兼容接口（DeepSeek 等）
    - anthropic: Anthropic Claude API
    """
    # ── 连接信息（来自 config.json providers）──
    provider: str = "openai"          # SDK 类型： "openai" | "openai-compatible" | "anthropic"
    api_key: str = ""
    base_url: str | None = None
    timeout: float = 60.0
    max_retries: int = 3

    # ── 默认模型参数（harness 未指定时兜底）──
    model: str = ""                   # 默认模型名；取 config.json models[0].name
    max_tokens: int = 4096
    temperature: float = 0.7

    # ── 模型注册表（来自 config.json models）──
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    """{model_name: {provider, think, multimodal, max_tokens, ...}}。"""

    def model_info(self, name: str) -> dict[str, Any]:
        """获取指定模型的能力声明。"""
        return self.models.get(name, {})

    @classmethod
    def from_env(cls, project_root: Path | None = None, **overrides: Any) -> "LLMConfig":
        """从 config.json + .env 加载配置。

        Args:
            project_root: 项目根目录（含 config.json 和 .env）
            **overrides: 覆盖配置项
        """
        if project_root is None:
            project_root = Path.cwd()

        # 1. 加载 .env → os.environ（API key 等密钥）
        _load_dotenv(project_root)

        # 2. 加载 config.json
        cfg = _load_config_json(project_root)
        providers: list[dict[str, Any]] = cfg.get("providers", [])
        models: list[dict[str, Any]] = cfg.get("models", [])

        if not providers:
            raise ValueError(
                "config.json 中 providers 为空或缺失。"
                "请参照 config.example.json 配置。"
            )

        # ── 选中 provider（取第一个，后续可扩展选择逻辑）──
        p = providers[0]

        # ── 解析 API key：优先 overrides，其次 api_key_env 指向的 .env 变量 ──
        api_key = overrides.pop("api_key", None)
        if api_key is None:
            key_env = p.get("api_key_env", "")
            api_key = os.environ.get(key_env, "") if key_env else ""

        # ── 构建 models 注册表 ──
        models_map: dict[str, dict[str, Any]] = {}
        default_model = ""
        default_temperature = 0.7

        for m in models:
            name = m.get("name", "")
            if name:
                models_map[name] = m
            if not default_model:
                default_model = name
                default_temperature = float(m.get("temperature", 0.7))

        config = cls(
            provider=p.get("sdktype", "openai"),
            api_key=api_key,
            base_url=p.get("base_url"),
            timeout=float(p.get("timeout", 60.0)),
            max_retries=int(p.get("max_retries", 3)),
            model=overrides.pop("model", None) or default_model,
            max_tokens=int(overrides.pop("max_tokens", None) or 4096),
            temperature=float(overrides.pop("temperature", None) or default_temperature),
            models=models_map,
        )
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
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key"""
        return bool(self.api_key)
