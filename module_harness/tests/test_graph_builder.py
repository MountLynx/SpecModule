# module_harness/tests/test_graph_builder.py
"""Tests for TasklistTranslator (graph_builder.py)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import Graph

from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.spec import TaskDefinition, Tasklist


@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def reg(mock_llm):
    r = HarnessRegistry(llm_client=mock_llm)
    r.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
    r.harness("analyze", HarnessConfig(prompt_core="分析：{data}"))

    @r.script("post_process")
    def post_process(view):
        return {"result": "processed"}

    return r


class TestTasklistTranslator:
    def test_build_minimal_graph(self, reg):
        """Single script task with a producer input reference."""
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="script", script="post_process", inputs={"data": "src"},
                ),
            },
            flow="A",
        )
        builder = TasklistTranslator(reg, module_id="test1")
        graph, out_reg = builder.build(tl)

        assert graph is not None
        assert isinstance(graph, Graph)
        assert "A" in graph.nodes

        # Body name uses module_id prefix.
        node_a = graph.nodes["A"]
        assert node_a.body == "test1:A"

        # Body is registered in the registry.
        assert out_reg.has_body("test1:A")

    def test_build_harness_and_script_graph(self, reg):
        """Two tasks with a sequential flow: harness -> script."""
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate", inputs={"text": "src"},
                ),
                "B": TaskDefinition(
                    type="script", script="post_process", inputs={"data": "A"},
                ),
            },
            flow="A --> B",
        )
        builder = TasklistTranslator(reg, module_id="mod2")
        graph, out_reg = builder.build(tl)

        assert "A" in graph.nodes
        assert "B" in graph.nodes
        assert out_reg.is_harness("mod2:A")
        assert out_reg.has_body("mod2:B")

        # Graph structure is correct.
        assert graph.nodes["A"].body == "mod2:A"
        assert graph.nodes["B"].body == "mod2:B"

    def test_build_with_start_node(self, reg):
        """Flow with an explicit start bracket [A]."""
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate"),
                "B": TaskDefinition(type="script", script="post_process"),
            },
            flow="[A]-->B",
        )
        builder = TasklistTranslator(reg, module_id="mod3")
        graph, _ = builder.build(tl)

        assert "A" in graph.starts

    def test_build_with_guarded_edge(self, reg):
        """Edge with a guard function."""
        @reg.guard("quality_check")
        def quality_check(view):
            return True

        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="harness", harness="translate"),
                "B": TaskDefinition(
                    type="script", script="post_process", inputs={"data": "A"},
                ),
            },
            flow="A--|quality_check|-->B",
        )
        builder = TasklistTranslator(reg, module_id="mod4")
        graph, _ = builder.build(tl)

        # Guard is correctly attached to the edge.
        edges = [e for e in graph.edges if e.dst == "B"]
        assert len(edges) == 1
        assert edges[0].guard == "quality_check"

    def test_module_id_isolation(self, reg):
        """Two modules with same task key produce non-colliding bodies."""
        tl1 = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="post_process")},
            flow="A",
        )
        tl2 = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="post_process")},
            flow="A",
        )

        b1 = TasklistTranslator(reg, module_id="mod_a")
        b2 = TasklistTranslator(reg, module_id="mod_b")

        _, out_reg1 = b1.build(tl1)
        _, out_reg2 = b2.build(tl2)

        # Each module has its own isolated body.
        assert out_reg1.has_body("mod_a:A")
        assert out_reg2.has_body("mod_a:A")  # shared registry
        assert out_reg2.has_body("mod_b:A")
        assert out_reg2.has_body("mod_b:B") is False  # different key

        # mod_b's body does not appear under mod_a's prefix.
        assert out_reg1.has_body("mod_b:B") is False

    def test_inputs_wired_to_graph_nodes(self, reg):
        """Task inputs are propagated to graph node inputs."""
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="script", script="post_process", inputs={"text": "src"},
                ),
            },
            flow="A",
        )
        builder = TasklistTranslator(reg, module_id="test_inputs")
        graph, _ = builder.build(tl)

        assert "A" in graph.nodes
        assert graph.nodes["A"].inputs["text"].kind == "latest"


class TestHarnessInputAlias:
    """跨节点 harness 输入：task.inputs 的 field 名应能渲染进 prompt。

    回归：graph_builder 把 {field: producer} 注册为输入 key 后，view[field]
    解析为 Missing（field 不是节点名），prompt 的 {field} 占位符原样保留。
    """

    @pytest.mark.asyncio
    async def test_prompt_renders_producer_value(self, mock_llm):
        from llm.client import LLMResponse
        from tickflow.async_runner import AsyncRunner
        from module_harness.events import EventBus, PromptRendered

        bus = EventBus()
        rendered: list[str] = []
        bus.subscribe(PromptRendered, lambda e: rendered.append(e.rendered))
        r = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

        @r.script("produce")
        def produce(view):
            return "HELLO-FROM-A"

        r.harness("consume", HarnessConfig(prompt_core="输入是：{log}"))
        mock_llm.complete = AsyncMock(return_value=LLMResponse(
            content='{"ok": true}', usage={}, finish_reason="end_turn"))

        tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="produce"),
                "B": TaskDefinition(
                    type="harness", harness="consume", inputs={"log": "A"},
                ),
            },
            flow="A --> B",
        )
        graph, reg = TasklistTranslator(r, "alias_test").build(tl)
        runner = AsyncRunner(graph, registry=reg, keep_records=True)
        await runner.run_until_idle(max_ticks=10)
        assert any(
            "HELLO-FROM-A" in p for p in rendered
        ), f"prompt 未渲染 producer 值: {rendered!r}"
