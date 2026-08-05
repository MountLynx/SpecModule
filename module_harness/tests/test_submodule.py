"""SubModule / builtins / pack / ModuleLoader 测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.events import EventBus, ScriptCompleted
from module_harness.registry import HarnessRegistry
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SpecValidationError, SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestBuiltins:
    def test_names(self):
        assert BUILTIN_HARNESS_NAMES == frozenset({"spec_to_tasklist", "spec_tasklist_review"})

    def test_register_builtins(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())
        register_builtin_harnesses(reg)
        for name in BUILTIN_HARNESS_NAMES:
            assert reg.harness_config(name) is not None

    def test_register_builtins_idempotent(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        register_builtin_harnesses(reg)  # 重复注册不抛异常


class Translator(SubModule):
    """测试用固定翻译 submodule。"""

    name = "test_translator"
    version = "1.0.0"
    spec_schema = SpecSchema(
        input={"source_text": "str", "style": "str"},
        output={"translation": "str"},
    )
    harnesses = [
        HarnessConfig(
            name="translate",
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式", "casual": "随意"},
            output_format=OutputFormat(type="json_object"),
        ),
    ]
    tasklist = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                promptmode="{spec.style}",
                inputs={"text": "{spec.source_text}"},
                outputformat={"type": "json_object"},
            ),
            "B": TaskDefinition(
                type="script", script="format_output", inputs={"data": "A"},
            ),
        },
        flow="A --> B",
    )

    @script("format_output")
    def format_output(view):
        return {"translation": view.A.value["translation"].strip()}


class TestSubModule:
    def test_scripts_collected(self):
        assert set(Translator._scripts) == {"format_output"}

    def test_no_scripts_when_none_declared(self):
        class Empty(SubModule):
            name = "empty"
        assert Empty._scripts == {}

    @pytest.mark.asyncio
    async def test_run_fixed_tasklist(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        sm = Translator(llm_client=mock_llm)
        firings = await sm.run({"source_text": "Hello", "style": "formal"}, max_ticks=10)
        assert len(firings) >= 2
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"translation": "你好世界"}

    @pytest.mark.asyncio
    async def test_spec_validation_failure(self, mock_llm):
        sm = Translator(llm_client=mock_llm)
        with pytest.raises(SpecValidationError) as ei:
            await sm.run({"source_text": "Hello"})  # 缺 style
        assert "style" in str(ei.value)

    @pytest.mark.asyncio
    async def test_run_without_tasklist_raises(self, mock_llm):
        class NoTask(SubModule):
            name = "no_task"
        with pytest.raises(ValueError, match="tasklist"):
            await NoTask(llm_client=mock_llm).run({"a": 1})

    @pytest.mark.asyncio
    async def test_audit_mode_emits_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=True, max_ticks=10)
        assert any(isinstance(e, ScriptCompleted) for e in got)

    @pytest.mark.asyncio
    async def test_embedded_mode_no_events(self, mock_llm):
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}', usage={}, finish_reason="end_turn")
        bus = EventBus()
        got: list = []
        bus.subscribe(ScriptCompleted, lambda e: got.append(e))
        sm = Translator(llm_client=mock_llm, event_bus=bus)
        await sm.run({"source_text": "Hello", "style": "formal"}, audit=False, max_ticks=10)
        assert got == []

    @pytest.mark.asyncio
    async def test_custom_tasklist_with_review(self, mock_llm):
        async def fake_complete(*args, **kwargs):
            return LLMResponse(
                content='{"consistent": true, "suggestions": ""}',
                usage={}, finish_reason="end_turn",
            )
        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        sm = Translator(llm_client=mock_llm)
        custom = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                    outputformat={"type": "json_object"},
                ),
            },
            flow="[A]",
        )
        firings = await sm.run(
            {"source_text": "Hello", "style": "formal"}, tasklist=custom, max_ticks=10)
        assert any(f.node == "A" for f in firings)
