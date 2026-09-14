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
class ProviderConfig:
    """单个 provider 的连接配置（来自 config.json providers 条目）。

    多 provider 路由的地基：models 注册表每模型的 provider 字段指向这里的 name。
    """
    name: str = ""
    sdktype: str = "openai"
    api_key: str = ""
    base_url: str | None = None
    timeout: float = 60.0
    max_retries: int = 3


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

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    """{provider_name: ProviderConfig}。from_env 解析全部 providers；
    手工构造可为空（provider_for 返回 None = 用默认顶层连接）。"""

    # ── 框架规则（来自 rules.txt）──
    system_rules: str = ""
    """框架级输出格式约束，注入每次 LLM 调用的 system prompt 最前面。"""

    def model_info(self, name: str) -> dict[str, Any]:
        """获取指定模型的能力声明。"""
        return self.models.get(name, {})

    def provider_for(self, model: str | None) -> ProviderConfig | None:
        """查模型所属 provider 的连接配置。

        models 注册表声明 provider 名且在 providers 表中 → 该 provider；
        任何一步缺失（模型未注册 / 未指明 provider / 表无此名）→ None = 默认连接。
        """
        name = self.models.get(model or "", {}).get("provider")
        if name:
            return self.providers.get(name)
        return None

    def for_provider(self, provider: ProviderConfig) -> "LLMConfig":
        """返回连接字段切到指定 provider 的配置副本（请求级字段共享）。

        RoutingClient 按 provider 建连用：client 构造签名接收 LLMConfig，
        切片把连接字段填进顶层字段，client 内 config.to_client_kwargs()
        即取到该 provider 的连接。
        """
        return LLMConfig(
            provider=provider.sdktype,
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=provider.timeout,
            max_retries=provider.max_retries,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            models=self.models,
            system_rules=self.system_rules,
            providers=self.providers,
        )

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

        # ── 解析全部 providers（name → 连接配置）──
        providers_map: dict[str, ProviderConfig] = {}
        for i, entry in enumerate(providers):
            pname = entry.get("name", "") or f"provider_{i}"
            if pname in providers_map:
                raise ValueError(f"config.json providers 名重复: {pname!r}")
            key_env = entry.get("api_key_env", "")
            providers_map[pname] = ProviderConfig(
                name=pname,
                sdktype=entry.get("sdktype", "openai"),
                api_key=os.environ.get(key_env, "") if key_env else "",
                base_url=entry.get("base_url"),
                timeout=float(entry.get("timeout", 60.0)),
                max_retries=int(entry.get("max_retries", 3)),
            )
        p = providers[0]
        default_pc = providers_map[p.get("name", "") or "provider_0"]

        # ── 解析 API key（overrides 优先，同步回默认 provider 条目）──
        api_key = overrides.pop("api_key", None)
        if api_key is not None:
            default_pc.api_key = api_key
        else:
            api_key = default_pc.api_key

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
            provider=default_pc.sdktype,
            api_key=api_key,
            base_url=default_pc.base_url,
            timeout=default_pc.timeout,
            max_retries=default_pc.max_retries,
            model=overrides.pop("model", None) or default_model,
            max_tokens=int(overrides.pop("max_tokens", None) or default_max_tokens),
            temperature=float(overrides.pop("temperature", None) or default_temperature),
            models=models_map,
            system_rules=system_rules,
            providers=providers_map,
        )
        for key, value in overrides.items():
            if hasattr(config, key) and value is not None:
                setattr(config, key, value)
        return config

    def to_client_kwargs(self) -> dict[str, Any]:
        """转为 SDK 客户端构造参数（连接级，两个 SDK 构造器同名接受）。

        仅含构造级参数：api_key / base_url / timeout / max_retries。
        model / max_tokens / temperature 是请求级参数，由 complete() 逐调用
        传入，混入构造器会 TypeError——构造层与请求层不可混。
        """
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key"""
        return bool(self.api_key)
