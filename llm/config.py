"""LLM 客户端配置

支持多种LLM后端：Anthropic、OpenAI 及兼容接口。
通过项目根目录的 config.json 和 rules.txt 配置::

    config.json — Provider + Model 注册表
    rules.txt  — 框架级输出格式约束（注入 system prompt）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _load_dotenv(roots: Path | list[Path]) -> None:
    """按候选根顺序加载 .env 到 os.environ（若存在）。

    roots：单个根（旧签名兼容）或候选根列表（store 根 → 项目根，前者优先）。
    既有约定保持：已存在于 os.environ 的键不被 .env 覆盖。
    """
    if isinstance(roots, Path):
        roots = [roots]
    for root in roots:
        env_path = root / ".env"
        if not env_path.exists():
            continue
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


def _load_config_json(roots: Path | list[Path]) -> dict[str, Any]:
    """按候选根顺序加载 config.json。全部缺失/格式错误时返回空 dict。"""
    if isinstance(roots, Path):
        roots = [roots]
    for root in roots:
        config_path = root / "config.json"
        if not config_path.exists():
            continue
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("config.json 解析失败: %s", exc)
            return {}
    log.warning("config.json 未找到（候选: %s）", ", ".join(str(r) for r in roots))
    return {}


def _load_rules_txt(roots: Path | list[Path]) -> str:
    """按候选根顺序加载 rules.txt（取第一个存在的）。"""
    if isinstance(roots, Path):
        roots = [roots]
    for root in roots:
        rules_path = root / "rules.txt"
        if not rules_path.exists():
            continue
        try:
            return rules_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


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
    provider: str = "openai"
    api_key: str = ""
    base_url: str | None = None
    timeout: float = 60.0
    max_retries: int = 3

    # ── 默认模型参数（harness 未指定时兜底；model/max_tokens/temperature 来自 config.json models[0]）──
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7

    # ── 模型注册表（来自 config.json models）──
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    """{model_name: {provider, think, multimodal, max_tokens, ...}}。"""

    # ── 框架规则（来自 rules.txt）──
    system_rules: str = ""
    """框架级输出格式约束，注入每次 LLM 调用的 system prompt 最前面。"""

    def model_info(self, name: str) -> dict[str, Any]:
        """获取指定模型的能力声明。"""
        return self.models.get(name, {})

    @classmethod
    def from_env(
        cls,
        project_root: Path | None = None,
        store_root: Path | None = None,
        **overrides: Any,
    ) -> "LLMConfig":
        """从 config.json + rules.txt + .env 加载配置（配置回退链）。

        Args:
            project_root: 项目根目录（最高候选）
            store_root: store 家目录（用户级回退；项目根缺失时生效）
            **overrides: 覆盖配置项

        回退链：os.environ（最高，不覆盖已有键）→ 项目根 → store 根。
        """
        if project_root is None:
            project_root = Path.cwd()

        # 候选根：项目根优先，store 根兜底（None 过滤）
        roots = [project_root]
        if store_root is not None:
            roots.append(store_root)

        # 1. 加载 .env -> os.environ（API key 等密钥）
        _load_dotenv(roots)

        # 2. 加载 config.json
        cfg = _load_config_json(roots)
        providers: list[dict[str, Any]] = cfg.get("providers", [])
        models: list[dict[str, Any]] = cfg.get("models", [])

        if not providers:
            raise ValueError(
                "config.json 中 providers 为空或缺失。"
                "请参照 config.example.json 配置。"
            )

        # 3. 加载 rules.txt
        system_rules = _load_rules_txt(roots)

        # ── 选中 provider（取第一个）──
        p = providers[0]

        # ── 解析 API key ──
        api_key = overrides.pop("api_key", None)
        if api_key is None:
            key_env = p.get("api_key_env", "")
            api_key = os.environ.get(key_env, "") if key_env else ""

        # ── 构建 models 注册表 ──
        models_map: dict[str, dict[str, Any]] = {}
        default_model = ""
        default_temperature = 0.7
        default_max_tokens = 4096

        for m in models:
            name = m.get("name", "")
            if name:
                models_map[name] = m
            if not default_model:
                default_model = name
                default_temperature = float(m.get("temperature", 0.7))
                default_max_tokens = int(m.get("max_tokens", 4096))

        config = cls(
            provider=p.get("sdktype", "openai"),
            api_key=api_key,
            base_url=p.get("base_url"),
            timeout=float(p.get("timeout", 60.0)),
            max_retries=int(p.get("max_retries", 3)),
            model=overrides.pop("model", None) or default_model,
            max_tokens=int(overrides.pop("max_tokens", None) or default_max_tokens),
            temperature=float(overrides.pop("temperature", None) or default_temperature),
            models=models_map,
            system_rules=system_rules,
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
