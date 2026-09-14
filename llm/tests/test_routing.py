# llm/tests/test_routing.py
"""RoutingClient 路由门面:模型→provider 惰性建连缓存,单 provider 兼容退化。"""

from __future__ import annotations

import pytest

from conftest import _install_fake_sdk
from llm.client import AnthropicClient, LLMError, RoutingClient, create_llm_client
from llm.config import LLMConfig, ProviderConfig


def _multi_config() -> LLMConfig:
    return LLMConfig(
        provider="openai",
        api_key="kd",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        providers={
            "deepseek": ProviderConfig(name="deepseek", sdktype="openai",
                                       api_key="kd", base_url="https://api.deepseek.com"),
            "zhipu": ProviderConfig(name="zhipu", sdktype="openai",
                                    api_key="kz", base_url="https://open.bigmodel.cn/api/paas/v4"),
        },
        models={
            "deepseek-chat": {"name": "deepseek-chat", "provider": "deepseek"},
            "cogview-4": {"name": "cogview-4", "provider": "zhipu", "image_gen": True},
        },
    )


def _anthropic_added(config: LLMConfig) -> LLMConfig:
    config.models["claude-sonnet-4"] = {"name": "claude-sonnet-4", "provider": "anthropic"}
    config.providers["anthropic"] = ProviderConfig(
        name="anthropic", sdktype="anthropic", api_key="ka")
    return config


class TestRouting:
    def test_routes_by_model_provider(self, monkeypatch):
        # 注:conftest 的 capture 是 update 语义(单槽,后建覆盖先建),
        # 故首次构造后即刻断言 deepseek 构造参数,再构造 zhipu 验证切换。
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        c_ds = client._client_for("deepseek-chat")
        assert cap == {
            "api_key": "kd", "base_url": "https://api.deepseek.com",
            "timeout": 60.0, "max_retries": 3,
        }
        c_cg = client._client_for("cogview-4")
        assert cap["api_key"] == "kz"                      # zhipu 连接
        assert cap["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert c_ds is not c_cg

    def test_same_provider_cached(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        a = client._client_for("cogview-4")
        assert client._client_for("cogview-4") is a        # 同 provider 只建一次
        assert len(client._clients) == 1

    def test_unregistered_model_uses_default_connection(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        c = client._client_for("unknown-model")
        assert c.config.api_key == "kd"                    # 默认连接
        assert c.config.base_url == "https://api.deepseek.com"

    def test_handmade_config_degrades_to_single_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(LLMConfig(api_key="k", model="m"))
        assert client._client_for("m") is client._client_for(None)

    def test_generate_image_routes_to_anthropic_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", {})
        client = RoutingClient(_anthropic_added(_multi_config()))
        assert isinstance(client._client_for("claude-sonnet-4"), AnthropicClient)

    @pytest.mark.asyncio
    async def test_generate_image_anthropic_model_raises(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", {})
        client = RoutingClient(_anthropic_added(_multi_config()))
        with pytest.raises(LLMError, match="Anthropic 无图像生成"):
            await client.generate_image("p", model="claude-sonnet-4")

    @pytest.mark.asyncio
    async def test_complete_delegates_with_model_routing(self, monkeypatch):
        """complete 按调用模型路由;Anthropic 分支走 chat 语义的客户端,
        这里仅验证 OpenAI 侧委托参数透传(model 覆盖生效)。"""
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        # 未注册模型 → 默认客户端;注册模型 → 对应客户端
        assert client._client_for("cogview-4") is client._client_for("cogview-4")
        assert client._client_for("deepseek-chat") is not client._client_for("cogview-4")


class TestFactory:
    def test_create_llm_client_returns_routing_client(self):
        assert isinstance(create_llm_client(LLMConfig(api_key="k")), RoutingClient)

    def test_ready_uses_default_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        assert client.ready is True   # 假 SDK 构造成功
