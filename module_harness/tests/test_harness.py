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


class TestValidationRetry:
    """validate_retries：校验失败带反馈重问（预算内循环；LLMError 不消耗预算）。"""

    BAD = "not json at all, completely invalid {{{"

    @staticmethod
    def _cfg(**overrides) -> HarnessConfig:
        kwargs: dict = {
            "prompt_core": "P",
            "output_format": OutputFormat(type="json_object"),
        }
        kwargs.update(overrides)
        return HarnessConfig(**kwargs)

    @staticmethod
    def _resp(content: str, usage: dict | None = None):
        from llm.client import LLMResponse
        return LLMResponse(
            content=content,
            usage=usage if usage is not None else {"input_tokens": 1, "output_tokens": 1},
            finish_reason="end_turn",
        )

    @staticmethod
    def _state_view():
        """挂了 state dict 的视图（模拟引擎供数的 mutable_state 审计链）。"""
        state: dict = {}
        view = NodeView(node="test_node", fields=(), values=(), state=state)
        return view, state

    @pytest.mark.asyncio
    async def test_first_fail_second_pass(self, mock_llm):
        """首败次过：2 次 complete、第二次 prompt 含反馈（带首错文本）、参数原样保留、usage 累计。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD, usage={"input_tokens": 1, "output_tokens": 2}),
            self._resp('{"ok": 1}', usage={"input_tokens": 3, "output_tokens": 4}),
        ])
        h = Harness(self._cfg(validate_retries=1, temperature=0.3), mock_llm, EventBus())
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert result == {"ok": 1}
        assert mock_llm.complete.await_count == 2
        calls = mock_llm.complete.await_args_list
        assert "上一次输出未通过校验" not in calls[0].kwargs["prompt"]
        assert "上一次输出未通过校验" in calls[1].kwargs["prompt"]
        assert "输出格式校验失败" in calls[1].kwargs["prompt"]  # 反馈携带首错 error
        # LLM 参数重试时原样保留（不降温不换模型）
        assert calls[1].kwargs["temperature"] == calls[0].kwargs["temperature"] == 0.3
        # usage 累计、raw 取最后一次
        assert state["_usage"] == {"input_tokens": 4, "output_tokens": 6}
        assert state["_llm_raw"] == '{"ok": 1}'

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_last_failure(self, mock_llm):
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), self._resp(self.BAD),
        ])
        h = Harness(self._cfg(validate_retries=1), mock_llm, EventBus())
        result = await h.build_body()(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert mock_llm.complete.await_count == 2

    @pytest.mark.asyncio
    async def test_default_zero_no_retry_no_new_state_keys(self, mock_llm):
        """缺省 0：1 次调用即返 Failure；不新增状态键（与无此字段时逐字节一致）。"""
        mock_llm.complete = AsyncMock(side_effect=[self._resp(self.BAD)])
        h = Harness(self._cfg(), mock_llm, EventBus())
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert mock_llm.complete.await_count == 1
        assert "_validation_attempts" not in state
        assert "_validation_retry_errors" not in state

    @pytest.mark.asyncio
    async def test_llm_error_does_not_consume_budget(self, mock_llm):
        """第 1 次坏输出、第 2 次 LLMError：infrastructure Failure，complete 恰 2 次。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), LLMError("连接耗尽"),
        ])
        h = Harness(self._cfg(validate_retries=2), mock_llm, EventBus())
        result = await h.build_body()(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "infrastructure"
        assert mock_llm.complete.await_count == 2  # LLMError 即返，不续问

    @pytest.mark.asyncio
    async def test_events_and_state_on_retry(self, mock_llm):
        """事件序列含两次 LlmCallStarted；state 记 _validation_attempts / _validation_retry_errors。"""
        mock_llm.complete = AsyncMock(side_effect=[
            self._resp(self.BAD), self._resp('{"ok": 1}'),
        ])
        started = []
        bus = EventBus()
        bus.subscribe(LlmCallStarted, lambda e: started.append(e))
        h = Harness(self._cfg(validate_retries=1), mock_llm, bus)
        view, state = self._state_view()
        result = await h.build_body()(view)

        assert result == {"ok": 1}
        assert len(started) == 2
        assert state["_validation_attempts"] == 2
        assert len(state["_validation_retry_errors"]) == 1
        assert "输出格式校验失败" in state["_validation_retry_errors"][0]
