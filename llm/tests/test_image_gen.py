# llm/tests/test_image_gen.py
"""generate_image 适配测试:参数组装 / b64 解码 / 错误包装 / Anthropic 能力缺失。

images/generations 统一请求 response_format="b64_json"(临时 URL 不落库);
api_params 沿用 _apply_api_params(已知字段直入,未知入 extra_body——线上合并进
请求体顶层,对 images 端点同样生效)。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from conftest import _install_fake_sdk
from llm.client import AnthropicClient, LLMError, OpenAIClient
from llm.config import LLMConfig

B64_PNG = base64.b64encode(b"fake-png-bytes").decode()


class _Item:
    def __init__(self, b64: str | None, revised: str | None = None):
        self.b64_json = b64
        self.revised_prompt = revised


class _ImagesResp:
    def __init__(self, items: list, usage: Any = None):
        self.data = items
        self.usage = usage


class _Usage:
    input_tokens = 10
    output_tokens = 5


def _install_fake_openai_images(monkeypatch, capture: dict, response: Any) -> None:
    """注入带 images.generate 的假 openai 模块。"""
    import sys
    import types

    fake = types.ModuleType("openai")

    class FakeImages:
        async def generate(self, **kwargs):
            capture["generate_kwargs"] = kwargs
            if isinstance(response, Exception):
                raise response
            return response

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            capture["constructor"] = kwargs
            self.images = FakeImages()

    fake.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)


def _openai_client(monkeypatch, capture: dict, response: Any) -> OpenAIClient:
    _install_fake_openai_images(monkeypatch, capture, response)
    return OpenAIClient(LLMConfig(provider="openai", api_key="k", model="cogview-4"))


class TestOpenAIGenerateImage:
    @pytest.mark.asyncio
    async def test_requests_b64_and_decodes(self, monkeypatch):
        capture: dict = {}
        resp = _ImagesResp([_Item(B64_PNG, revised="a better prompt")], usage=_Usage())
        client = _openai_client(monkeypatch, capture, resp)
        result = await client.generate_image("一只猫", size="1024x1024")
        kw = capture["generate_kwargs"]
        assert kw["model"] == "cogview-4"           # 缺省回落 config.model
        assert kw["prompt"] == "一只猫"
        assert kw["response_format"] == "b64_json"  # 统一取字节
        assert kw["n"] == 1
        assert kw["size"] == "1024x1024"
        assert result.data == b"fake-png-bytes"
        assert result.revised_prompt == "a better prompt"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    @pytest.mark.asyncio
    async def test_model_override_and_size_omitted(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(B64_PNG)]))
        await client.generate_image("p", model="dall-e-3")
        assert capture["generate_kwargs"]["model"] == "dall-e-3"
        assert "size" not in capture["generate_kwargs"]

    @pytest.mark.asyncio
    async def test_api_params_known_direct_unknown_extra_body(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(B64_PNG)]))
        await client.generate_image("p", api_params={"n": 2, "quality": "high"})
        kw = capture["generate_kwargs"]
        assert kw["n"] == 2                              # 已知参数直入
        assert kw["extra_body"] == {"quality": "high"}   # 未知入 extra_body

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped_as_llm_error(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, RuntimeError("boom"))
        with pytest.raises(LLMError, match="images API"):
            await client.generate_image("p")

    @pytest.mark.asyncio
    async def test_missing_b64_raises(self, monkeypatch):
        capture: dict = {}
        client = _openai_client(monkeypatch, capture, _ImagesResp([_Item(None)]))
        with pytest.raises(LLMError, match="b64_json"):
            await client.generate_image("p")


class TestAnthropicGenerateImage:
    @pytest.mark.asyncio
    async def test_capability_missing_raises_llm_error(self, monkeypatch):
        capture: dict = {}
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", capture)
        client = AnthropicClient(LLMConfig(provider="anthropic", api_key="k"))
        with pytest.raises(LLMError, match="Anthropic 无图像生成"):
            await client.generate_image("p")
