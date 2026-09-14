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
    async def test_complete_passthrough_reaches_routed_client(self):
        """complete 全参透传到被路由客户端,且按模型路由到正确 provider 槽位。"""
        from unittest.mock import AsyncMock

        from llm.client import LLMResponse

        client = RoutingClient(_multi_config())
        # 注:facade 在槽位对象上调用 .complete(),return_value 须配置在该子方法上
        # (配置在父 AsyncMock 上对 .complete 属性调用不生效)
        zhipu_mock = AsyncMock()
        zhipu_mock.complete.return_value = LLMResponse(content="y")
        client._clients[None] = AsyncMock()
        client._clients["zhipu"] = zhipu_mock

        token_calls: list[str] = []
        resp = await client.complete(
            "p", system="s", model="cogview-4", temperature=0.3,
            think=True, output_format={"type": "json_object"},
            notdo=["a"], on_token=token_calls.append, api_params={"k": "v"},
        )
        assert resp.content == "y"
        assert zhipu_mock.complete.call_args.args[0] == "p"
        kw = zhipu_mock.complete.call_args.kwargs
        assert kw["system"] == "s"
        assert kw["model"] == "cogview-4"
        assert kw["temperature"] == 0.3
        assert kw["think"] is True
        assert kw["output_format"] == {"type": "json_object"}
        assert kw["notdo"] == ["a"]
        assert callable(kw["on_token"])
        assert kw["api_params"] == {"k": "v"}

    @pytest.mark.asyncio
    async def test_complete_default_slot_and_chat_passthrough(self):
        """model=None 走默认槽位;chat 透传 messages/tools 到默认客户端。"""
        from unittest.mock import AsyncMock

        from llm.client import LLMResponse, Message

        client = RoutingClient(_multi_config())
        default_mock = AsyncMock(return_value=LLMResponse(content="d"))
        client._clients[None] = default_mock

        await client.complete("p")
        default_mock.complete.assert_awaited_once()
        assert default_mock.complete.call_args.kwargs["model"] is None

        msgs = [Message(role="user", content="hi")]
        await client.chat(msgs, tools=[{"name": "t"}])
        default_mock.chat.assert_awaited_once_with(msgs, [{"name": "t"}])

    def test_ready_uses_default_client(self, monkeypatch):
        cap: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
        client = RoutingClient(_multi_config())
        assert client.ready is True   # 假 SDK 构造成功


class TestFactory:
    def test_create_llm_client_returns_routing_client(self):
        assert isinstance(create_llm_client(LLMConfig(api_key="k")), RoutingClient)
