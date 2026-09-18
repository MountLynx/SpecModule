# llm/tests/test_stream_thinking.py
"""on_thinking 通道：reasoning 方言分派 + 内联 <think> 剥离 + Anthropic thinking_delta。

SDK 不进依赖（conftest 假 SDK 建构造级客户端）——流行为用桩对象替换 client._client
（_stream 只触 chat.completions.create / messages.stream 两个入口）。
"""

from __future__ import annotations

import pytest

from llm.client import AnthropicClient, OpenAIClient, RoutingClient, _ThinkTagStripper
from llm.config import LLMConfig


# ── OpenAI 兼容桩 ──────────────────────────────────────────────

class _Delta:
    def __init__(self, content=None, reasoning_content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.reasoning = reasoning


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, delta, finish_reason=None, usage=None):
        self.choices = [_Choice(delta, finish_reason)]
        self.usage = usage


class _StubCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs: dict | None = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs        # 门控断言用：记下最近一次请求参数
        return _AsyncChunks(self._chunks)


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


class _StubOpenAISDK:
    """桩到 `self._client.chat.completions.create(**kwargs)` 这条链。"""

    def __init__(self, chunks):
        self.chat = type("Chat", (), {"completions": _StubCompletions(chunks)})()


# ── 非流式桩 ──────────────────────────────────────────────────

class _StubMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls      # None → _nonstream 跳过 tool_calls 分支


class _StubNonStreamChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _StubNonStreamResponse:
    """_nonstream 读 response.choices[0].message/.finish_reason 与 response.usage。"""

    def __init__(self, content, finish_reason="stop"):
        self.choices = [_StubNonStreamChoice(_StubMessage(content), finish_reason)]
        self.usage = None                 # → usage 计为 0/0


class _StubNonStreamCompletions:
    def __init__(self, response):
        self._response = response

    async def create(self, **kwargs):
        return self._response             # 非流式：返回完整 response，不迭代


class _StubNonStreamSDK:
    def __init__(self, response):
        self.chat = type("Chat", (), {"completions": _StubNonStreamCompletions(response)})()


def _openai_client_nonstream(monkeypatch, content) -> OpenAIClient:
    """非流式客户端桩：complete 不传任何回调 → 走 _nonstream 路径。"""
    from conftest import _install_fake_sdk

    cap: dict = {}
    _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
    c = OpenAIClient(LLMConfig(provider="openai", api_key="k", model="m"))
    c._client = _StubNonStreamSDK(_StubNonStreamResponse(content))
    return c


def _openai_client(monkeypatch, chunks) -> OpenAIClient:
    """真构造（config 完整，_stream/complete 读 config.base_url）+ 流桩换 _client。"""
    from conftest import _install_fake_sdk

    cap: dict = {}
    _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", cap)
    c = OpenAIClient(LLMConfig(provider="openai", api_key="k", model="m"))
    c._client = _StubOpenAISDK(chunks)
    return c


# ── 用例 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_reasoning_content_dialect(monkeypatch):
    """reasoning_content 方言 → on_thinking；content → on_token。"""
    c = _openai_client(monkeypatch, [
        _Chunk(_Delta(reasoning_content="想")),
        _Chunk(_Delta(reasoning_content="清楚")),
        _Chunk(_Delta(content="答"), finish_reason="stop"),
    ])
    think, toks = [], []
    content, _, usage, finish = await c._stream({"model": "m"}, toks.append, think.append)
    assert "".join(think) == "想清楚"
    assert "".join(toks) == "答"
    assert content == "答" and finish == "stop" and usage == {}


@pytest.mark.asyncio
async def test_openai_reasoning_attr_fallback(monkeypatch):
    """无 reasoning_content 时回落 delta.reasoning（部分网关方言）。"""
    c = _openai_client(monkeypatch, [_Chunk(_Delta(reasoning="推理中"))])
    think, _ = [], []
    await c._stream({"model": "m"}, None, think.append)
    assert "".join(think) == "推理中"


@pytest.mark.asyncio
async def test_openai_inline_think_split_across_chunks(monkeypatch):
    """内联 <think> 标签被 chunk 劈开也能完整剥离：外→token，内→thinking。"""
    c = _openai_client(monkeypatch, [
        _Chunk(_Delta(content="a<th")),
        _Chunk(_Delta(content="ink>隐藏的</th")),
        _Chunk(_Delta(content="ink>b"), finish_reason="stop"),
    ])
    think, toks = [], []
    content, *_ = await c._stream({"model": "m"}, toks.append, think.append)
    assert "".join(toks) == "ab"
    assert "".join(think) == "隐藏的"
    assert content == "ab"          # 返回 content 不含思考文本


@pytest.mark.asyncio
async def test_openai_unclosed_think_flush(monkeypatch):
    """流结束仍未闭合的 <think>：缓冲整体按思考吐出（flush 路径）。"""
    c = _openai_client(monkeypatch, [
        _Chunk(_Delta(content="答案<think>内幕")),
    ])
    think, toks = [], []
    content, *_ = await c._stream({"model": "m"}, toks.append, think.append)
    assert "".join(toks) == "答案"
    assert "".join(think) == "内幕"
    assert content == "答案"


@pytest.mark.asyncio
async def test_on_thinking_alone_triggers_stream(monkeypatch):
    """只传 on_thinking 也走流式接口（complete 门控：任一回调即流式）。"""
    c = _openai_client(monkeypatch, [_Chunk(_Delta(reasoning_content="想"), finish_reason="stop")])
    think = []
    await c.complete("p", model="m", on_thinking=think.append)
    assert c._client.chat.completions.last_kwargs is not None
    assert c._client.chat.completions.last_kwargs.get("stream") is True
    assert think == ["想"]


@pytest.mark.asyncio
async def test_nonstream_strips_inline_think(monkeypatch):
    """非流式路径与流式剥离对称：complete 不传回调，返回 content 不含思考文本。"""
    c = _openai_client_nonstream(monkeypatch, "答案<think>内幕</think>收尾")
    resp = await c.complete("p", model="m")
    assert resp.content == "答案收尾"


def test_stripper_literal_bracket_not_tag():
    """普通文本里的零散 '<' 不误剥（hold-back 后原样吐出）。"""
    s = _ThinkTagStripper()
    c1, t1 = s.feed("1 < 2 且 <b>加粗</b>")
    c2, t2 = s.flush()
    assert (c1 + c2) == "1 < 2 且 <b>加粗</b>"
    assert (t1 + t2) == ""
