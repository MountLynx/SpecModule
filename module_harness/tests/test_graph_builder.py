# module_harness/tests/test_graph_builder.py
"""Tests for TasklistTranslator (graph_builder.py)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import Graph
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend

from llm.client import LLMResponse
from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist


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


@pytest.fixture
def mock_llm_async(mock_llm):
    mock_llm.complete = AsyncMock(return_value=LLMResponse(content='{"ok": true}'))
    return mock_llm


class TestConstantTokens:
    """{spec}/{tasklist}/{node} 常量 token：注册时解析，不注册为图输入。"""

    @pytest.mark.asyncio
    async def test_tokens_resolve_to_spec_inputs(self, mock_llm_async, reg):
        """token 不注册为图输入（不进 InputPolicy），非常量 input 正常 wiring。"""
        reg.harness("align_probe", HarnessConfig(
            prompt_core="spec={spec}\ntasklist={tasklist}\npos={position}\ndata={data}"
        ))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_probe",
                    inputs={
                        "spec": "{spec}",
                        "tasklist": "{tasklist}",
                        "position": "{node}",
                        "data": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好", "target": "world"}))

        assert "spec" not in graph.nodes["C"].inputs
        assert "tasklist" not in graph.nodes["C"].inputs
        assert "position" not in graph.nodes["C"].inputs
        assert "data" in graph.nodes["C"].inputs

    @pytest.mark.asyncio
    async def test_tokens_render_into_prompt(self, mock_llm_async, reg):
        """端到端：prompt 渲染包含 spec JSON / tasklist JSON / 节点 key。"""
        reg.harness("align_probe", HarnessConfig(
            prompt_core="spec={spec}\ntasklist={tasklist}\npos={position}\ndata={data}"
        ))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_probe",
                    inputs={
                        "spec": "{spec}",
                        "tasklist": "{tasklist}",
                        "position": "{node}",
                        "data": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好", "target": "world"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        assert mock_llm_async.complete.await_count == 2
        prompt = mock_llm_async.complete.call_args_list[1].kwargs["prompt"]
        assert '"source_text": "你好"' in prompt    # {spec} → spec JSON
        assert '"Tasks"' in prompt                   # {tasklist} → tasklist JSON
        assert "pos=C" in prompt                     # {position} → 节点 key

    @pytest.mark.asyncio
    async def test_spec_token_without_spec_renders_empty(self, mock_llm_async, reg):
        """build 未传 spec 时 {spec} → 空 dict JSON（显式可见）。"""
        reg.harness("probe", HarnessConfig(prompt_core="spec={spec}"))
        tl = Tasklist(
            tasks={"A": TaskDefinition(
                type="harness", harness="probe", inputs={"spec": "{spec}"},
            )},
            flow="[A]",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl)  # 不传 spec
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=5)
        prompt = mock_llm_async.complete.call_args.kwargs["prompt"]
        assert "spec={}" in prompt

    @pytest.mark.asyncio
    async def test_non_serializable_spec_raises_clear_error(self, mock_llm_async, reg):
        """spec 含不可 JSON 序列化值时 {spec} token 抛清晰 ValueError（含 task key）。"""
        reg.harness("probe", HarnessConfig(prompt_core="spec={spec}"))
        tl = Tasklist(
            tasks={"A": TaskDefinition(
                type="harness", harness="probe", inputs={"spec": "{spec}"},
            )},
            flow="[A]",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        with pytest.raises(ValueError, match="不可 JSON 序列化") as excinfo:
            builder.build(tl, spec=Spec({"created_at": datetime.now()}))
        assert "Task 'A'" in str(excinfo.value)
        assert "{spec}" in str(excinfo.value)


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
