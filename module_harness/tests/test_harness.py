import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from tickflow import Failure
from tickflow.views import NodeView

from llm.client import ImageResult, LLMError
from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat
from module_harness.infra.events import (
    EventBus, PromptRendered, LlmCallStarted, LlmToken,
    LlmCallCompleted, OutputValidated, HarnessFailed, HarnessEvent, ImageSaved,
)
from module_harness.core.harness import Harness


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def basic_config():
    return HarnessConfig(
        prompt_core="翻译：{text}",
        output_format=OutputFormat(type="json_object"),
    )


def _make_view(**inputs) -> NodeView:
    """构造一个测试用 NodeView：模拟引擎对具名 bind body 供数的视图
    （字段名 → 值经 v.named 消费）。"""
    return NodeView(
        node="test_node",
        fields=tuple((k, k) for k in inputs),
        values=tuple(inputs.values()),
    )


class TestHarnessBuildBody:
    @pytest.mark.asyncio
    async def test_successful_call_returns_parsed_output(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content='{"result": "translated text"}',
            usage={"input_tokens": 10, "output_tokens": 5},
            finish_reason="end_turn",
        )
        bus = EventBus()
        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view(text="Hello"))

        assert result == {"result": "translated text"}
        mock_llm.complete.assert_called_once()
        call_kwargs = mock_llm.complete.call_args.kwargs
        assert "Hello" in call_kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_validation_failure_returns_failure(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all, completely invalid {{{",
            usage={"input_tokens": 5, "output_tokens": 5},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "llm"

    @pytest.mark.asyncio
    async def test_infrastructure_error_returns_abort_failure(self, mock_llm, basic_config):
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("网络超时")
        bus = EventBus()
        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()

        result = await body(_make_view(text="Hello"))

        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert "网络超时" in result.error

    @pytest.mark.asyncio
    async def test_events_emitted_on_success(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content='{"ok": true}',
            usage={"input_tokens": 5, "output_tokens": 3},
            finish_reason="end_turn",
        )
        event_names = []
        bus = EventBus()
        bus.subscribe(HarnessEvent, lambda e: event_names.append(type(e).__name__))

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view(text="test"))

        assert "PromptRendered" in event_names
        assert "LlmCallStarted" in event_names
        assert "LlmCallCompleted" in event_names
        assert "OutputValidated" in event_names

    @pytest.mark.asyncio
    async def test_harness_failed_event_on_infrastructure(self, mock_llm, basic_config):
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("API 鉴权失败")
        failed_events = []
        bus = EventBus()
        bus.subscribe(HarnessFailed, failed_events.append)

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view())

        assert len(failed_events) == 1
        assert failed_events[0].failure_type == "infrastructure"

    @pytest.mark.asyncio
    async def test_llm_token_events_emitted(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        chunks = ["Hello", " ", "World"]
        mock_llm.complete.return_value = LLMResponse(
            content="Hello World",
            usage={},
            finish_reason="end_turn",
        )

        # 模拟 on_token 回调
        async def fake_complete(*args, **kwargs):
            on_token = kwargs.get("on_token")
            if on_token:
                for c in chunks:
                    on_token(c)
            return LLMResponse(
                content="Hello World",
                usage={},
                finish_reason="end_turn",
            )

        mock_llm.complete = AsyncMock(side_effect=fake_complete)

        tokens = []
        bus = EventBus()
        bus.subscribe(LlmToken, lambda e: tokens.append(e.chunk))

        h = Harness(basic_config, mock_llm, bus)
        body = h.build_body()
        await body(_make_view(text="test"))

        assert tokens == ["Hello", " ", "World"]

    @pytest.mark.asyncio
    async def test_promptmode_passed_to_renderer(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="plain text response",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="核心：{text}",
            prompt_modes={"extra": "额外指令"},
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body(promptmode="extra")

        await body(_make_view(text="test"))

        call_prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "额外指令" in call_prompt

    @pytest.mark.asyncio
    async def test_notdo_passed_to_llm(self, mock_llm, basic_config):
        from llm.client import LLMResponse
        mock_llm.complete.return_value = LLMResponse(
            content="ok",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(
            prompt_core="核心。",
            notdo=["不要废话", "不要重复"],
        )
        bus = EventBus()
        h = Harness(cfg, mock_llm, bus)
        body = h.build_body()

        await body(_make_view())

        # notdo 通过 notdo= 参数传递，由 LLM client 内部 _build_system() 拼入 system
        passed_notdo = mock_llm.complete.call_args.kwargs.get("notdo") or []
        assert "不要废话" in passed_notdo
        assert "不要重复" in passed_notdo


@pytest.fixture
def mock_image_llm():
    client = MagicMock()
    client.generate_image = AsyncMock(
        return_value=ImageResult(data=b"\x89PNG\r\n\x1a\nfake-bytes",
                                 usage={"input_tokens": 3, "output_tokens": 100})
    )
    return client


class TestHarnessImageMode:
    """mode="image"：渲染→生成→落盘→事件→返回路径。"""

    @pytest.mark.asyncio
    async def test_returns_path_and_writes_file(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_dir=str(tmp_path / "imgs"))
        h = Harness(cfg, mock_image_llm, EventBus())
        result = await h.build_body()(_make_view(title="封面"))
        p = Path(result)
        assert p.parent == tmp_path / "imgs"
        assert p.name.startswith("test_node-") and p.suffix == ".png"
        assert p.read_bytes() == b"\x89PNG\r\n\x1a\nfake-bytes"

    @pytest.mark.asyncio
    async def test_prompt_and_params_reach_client(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_size="1024x1024", image_dir=str(tmp_path))
        h = Harness(cfg, mock_image_llm, EventBus())
        await h.build_body()(_make_view(title="封面"))
        kw = mock_image_llm.generate_image.call_args.kwargs
        assert kw["prompt"] == "画:封面"
        assert kw["model"] is None          # HarnessConfig 未指定 model
        assert kw["size"] == "1024x1024"
        assert kw["api_params"] is None

    @pytest.mark.asyncio
    async def test_no_token_callback_and_no_output_validation(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        bus.subscribe(LlmToken, lambda e: seen.append(e))
        bus.subscribe(OutputValidated, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        await h.build_body()(_make_view())
        assert seen == []                    # 图像模式不发 LlmToken / OutputValidated

    @pytest.mark.asyncio
    async def test_events_and_state(self, mock_image_llm, tmp_path):
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        for evt in (PromptRendered, LlmCallStarted, LlmCallCompleted, ImageSaved):
            bus.subscribe(evt, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        state: dict = {}
        view = NodeView(node="img_node", fields=(), values=(), state=state)
        result = await h.build_body()(view)
        assert [type(e) for e in seen] == [PromptRendered, LlmCallStarted, LlmCallCompleted, ImageSaved]
        saved = seen[-1]
        assert saved.path == result
        assert saved.bytes_len == len(b"\x89PNG\r\n\x1a\nfake-bytes")
        assert state["_image_path"] == result
        assert state["_usage"] == {"input_tokens": 3, "output_tokens": 100}

    @pytest.mark.asyncio
    async def test_llm_error_maps_to_infrastructure_failure(self, mock_image_llm, tmp_path):
        mock_image_llm.generate_image.side_effect = LLMError("provider 挂了")
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(tmp_path))
        bus = EventBus()
        seen: list = []
        bus.subscribe(HarnessFailed, lambda e: seen.append(e))
        h = Harness(cfg, mock_image_llm, bus)
        result = await h.build_body()(_make_view())
        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert seen and seen[0].failure_type == "infrastructure"

    @pytest.mark.asyncio
    async def test_disk_error_maps_to_infrastructure_failure(self, mock_image_llm, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        cfg = HarnessConfig(prompt_core="x", mode="image", image_dir=str(blocker))
        h = Harness(cfg, mock_image_llm, EventBus())
        state: dict = {}
        view = NodeView(node="test_node", fields=(), values=(), state=state)
        result = await h.build_body()(view)
        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert state["_usage"] == {"input_tokens": 3, "output_tokens": 100}  # 已计费调用的审计不因落盘失败丢失
        assert "_image_path" not in state
