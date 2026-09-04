# module_harness/tests/test_integration.py
"""Integration tests: harness + script + tickflow AsyncRunner end-to-end."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import parse
from tickflow.async_runner import AsyncRunner
from llm.client import LLMResponse

from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat
from module_harness.infra.events import (
    EventBus,
    LlmToken,
    OutputValidated,
    ScriptCompleted,
)
from module_harness.core.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _make_graph_text(harness_body_name: str = "translate", script_body_name: str = "process"):
    return f"""
    [A]-->B
    A.body: {harness_body_name}
    B.body: {script_body_name}
    """


class TestHarnessScriptIntegration:
    @pytest.mark.asyncio
    async def test_harness_output_flows_to_script(self, mock_llm):
        """完整流程：harness 产出 JSON → script 处理 → 返回结果。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"text": "Hello World", "lang": "en"}',
            usage={"input_tokens": 10, "output_tokens": 8},
            finish_reason="end_turn",
        )

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        # 注册 harness
        reg.harness("translate", HarnessConfig(
            prompt_core="翻译：{text}",
            output_format=OutputFormat(type="json_object"),
        ))

        # 注册 script — 处理上游 JSON
        @reg.script("process")
        def process(view):
            data = view.A.value
            return {"char_count": len(data["text"])}

        graph = parse(_make_graph_text(), registry=reg)
        runner = AsyncRunner(graph, registry=reg)

        firings = await runner.run_until_idle(max_ticks=10)

        # 两个节点都成功
        assert len(firings) == 2
        assert all(f.status == "ok" for f in firings)

        # script 节点得到正确的计算结果
        b_firing = [f for f in firings if f.node == "B"][0]
        assert b_firing.output == {"char_count": 11}

    @pytest.mark.asyncio
    async def test_harness_failure_halts_downstream(self, mock_llm):
        """harness 返回 llm 级别 Failure 时，下游 AND-join 不应触发。"""
        # 返回无法解析的内容 + json_object 约束 → Failure(type="llm")
        mock_llm.complete.return_value = LLMResponse(
            content="not json at all {{{",
            usage={"input_tokens": 5, "output_tokens": 3},
            finish_reason="end_turn",
        )

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        reg.harness("translate", HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        ))

        executed = []
        @reg.script("process")
        def process(view):
            executed.append(True)
            return "should not run"

        graph = parse(_make_graph_text(), registry=reg)
        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        # 只有 node A 触发过
        assert len(firings) == 1
        assert firings[0].node == "A"
        assert firings[0].status == "failed"
        assert len(executed) == 0  # B 从未触发

    @pytest.mark.asyncio
    async def test_infrastructure_failure_aborts_runner(self, mock_llm):
        """LLMError → ABORTED。"""
        from llm.client import LLMError
        mock_llm.complete.side_effect = LLMError("API 不可用")

        bus = EventBus()
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        reg.harness("translate", HarnessConfig(prompt_core="x"))

        @reg.script("process")
        def process(view):
            return view.A.value

        graph = parse(_make_graph_text(), registry=reg)
        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        assert runner.status.value == "aborted"
        assert firings[0].status == "aborted"

    @pytest.mark.asyncio
    async def test_events_collected_during_run(self, mock_llm):
        """EventBus 事件在整个 run 中被正确收集。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"x": 1}',
            usage={},
            finish_reason="end_turn",
        )

        bus = EventBus()
        harness_events = []
        script_events = []
        bus.subscribe(LlmToken, lambda e: harness_events.append(("token", e.chunk)))
        bus.subscribe(OutputValidated, lambda e: harness_events.append(("validated", e.passed)))
        bus.subscribe(ScriptCompleted, lambda e: script_events.append(e))

        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)
        reg.harness("translate", HarnessConfig(
            prompt_core="x",
            output_format=OutputFormat(type="json_object"),
        ))

        @reg.script("process")
        def process(view):
            return view.A.value["x"] * 2

        graph = parse(_make_graph_text(), registry=reg)
        runner = AsyncRunner(graph, registry=reg)
        await runner.run_until_idle(max_ticks=10)

        # EventBus 收集到 harness 和 script 事件
        validated = [e for e in harness_events if isinstance(e, tuple) and e[0] == "validated"]
        assert len(validated) == 1
        assert validated[0][1] is True  # passed
        assert len(script_events) == 1

    @pytest.mark.asyncio
    async def test_multiple_ticks_independent_events(self, mock_llm):
        """两个串行 harness 节点各自独立生成事件。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"step1": "done"}',
            usage={},
            finish_reason="end_turn",
        )

        rendered = []
        bus = EventBus()
        from module_harness.infra.events import PromptRendered
        bus.subscribe(PromptRendered, lambda e: rendered.append(e.node))

        reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)
        reg.harness("step1", HarnessConfig(prompt_core="第一步"))
        reg.harness("step2", HarnessConfig(prompt_core="第二步"))

        graph = parse("""
        [A]-->B
        A.body: step1
        B.body: step2
        """, registry=reg)

        runner = AsyncRunner(graph, registry=reg)
        firings = await runner.run_until_idle(max_ticks=10)

        assert len(firings) == 2
        assert rendered == ["A", "B"]
