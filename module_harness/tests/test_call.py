# module_harness/tests/test_call.py
"""call_harness — task 级 API 地板：独立调用 harness（嵌入者消费面）。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMError, LLMResponse

from module_harness.call import HarnessCallError, HarnessCallResult, call_harness
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.events import EventBus, OutputValidated, PromptRendered
from module_harness.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestCallHarnessText:
    @pytest.mark.asyncio
    async def test_text_value_equals_raw(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="你好", usage={"input_tokens": 1}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(prompt_core="翻译：{text}"),
            {"text": "hello"},
            llm_client=mock_llm,
        )
        assert isinstance(result, HarnessCallResult)
        assert result.value == "你好"
        assert result.raw == "你好"
        assert result.usage == {"input_tokens": 1}

    @pytest.mark.asyncio
    async def test_values_rendered_into_prompt(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="x", usage={}, finish_reason="end_turn",
        )
        await call_harness(
            HarnessConfig(prompt_core="翻译：{text}"),
            {"text": "hello"},
            llm_client=mock_llm,
        )
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert prompt == "翻译：hello"


class TestCallHarnessJson:
    @pytest.mark.asyncio
    async def test_json_value_parsed_raw_literal(self, mock_llm):
        fenced = '```json\n{"a": 1}\n```'
        mock_llm.complete.return_value = LLMResponse(
            content=fenced, usage={}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(
                prompt_core="输出 JSON：{x}",
                output_format=OutputFormat(type="json_object"),
            ),
            {"x": "1"},
            llm_client=mock_llm,
        )
        assert result.value == {"a": 1}   # 校验+提取后的解析值
        assert result.raw == fenced       # 原始输出（审计链）

    @pytest.mark.asyncio
    async def test_validation_failure_raises_with_chain(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="not json", usage={}, finish_reason="end_turn",
        )
        with pytest.raises(HarnessCallError) as exc_info:
            await call_harness(
                HarnessConfig(
                    prompt_core="P：{x}",
                    output_format=OutputFormat(type="json_object"),
                ),
                {"x": "1"},
                llm_client=mock_llm,
            )
        err = exc_info.value
        assert err.failure is not None
        assert err.failure.type == "llm"
        assert err.prompt == "P：1"
        assert err.raw == "not json"

    @pytest.mark.asyncio
    async def test_llm_error_infrastructure(self, mock_llm):
        mock_llm.complete.side_effect = LLMError("API 不可用")
        with pytest.raises(HarnessCallError) as exc_info:
            await call_harness(
                HarnessConfig(prompt_core="P"),
                {},
                llm_client=mock_llm,
            )
        err = exc_info.value
        assert err.failure.type == "infrastructure"
        assert err.raw is None
        assert "API 不可用" in str(err)


class TestCallHarnessPromptmode:
    @pytest.mark.asyncio
    async def test_promptmode_renders(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="x", usage={}, finish_reason="end_turn",
        )
        await call_harness(
            HarnessConfig(prompt_core="P：{x}", prompt_modes={"formal": "正式语域"}),
            {"x": "1"},
            llm_client=mock_llm,
            promptmode="formal",
        )
        prompt = mock_llm.complete.call_args.kwargs["prompt"]
        assert "正式语域" in prompt

    @pytest.mark.asyncio
    async def test_promptmode_missing_key_raises_keyerror(self, mock_llm):
        """缺 promptmode key → KeyError 原样冒出（框架不猜）。"""
        with pytest.raises(KeyError):
            await call_harness(
                HarnessConfig(prompt_core="P", prompt_modes={"formal": "正式"}),
                {},
                llm_client=mock_llm,
                promptmode="casual",
            )


class TestCallHarnessEvents:
    @pytest.mark.asyncio
    async def test_events_collected_when_bus_passed(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content="hi", usage={}, finish_reason="end_turn",
        )
        bus = EventBus()
        seen = []
        bus.subscribe(PromptRendered, lambda e: seen.append(e))
        bus.subscribe(OutputValidated, lambda e: seen.append(e))
        await call_harness(
            # 显式 text 格式：OutputValidated 仅在配置 output_format（存在 validator）时发射
            HarnessConfig(prompt_core="P：{x}", output_format=OutputFormat(type="text")),
            {"x": "1"},
            llm_client=mock_llm,
            event_bus=bus,
        )
        kinds = [type(e).__name__ for e in seen]
        assert "PromptRendered" in kinds
        assert "OutputValidated" in kinds
        assert all(e.node == "__call__" for e in seen)  # 保留字面量

    @pytest.mark.asyncio
    async def test_no_bus_zero_cost(self, mock_llm):
        """不传 bus → EventBus.null()，静默零开销。"""
        mock_llm.complete.return_value = LLMResponse(
            content="hi", usage={}, finish_reason="end_turn",
        )
        result = await call_harness(
            HarnessConfig(prompt_core="P"), {}, llm_client=mock_llm,
        )
        assert result.value == "hi"
