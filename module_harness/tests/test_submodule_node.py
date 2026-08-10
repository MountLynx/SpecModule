"""submodule 节点类型测试：模型 roundtrip + 节点行为。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestTaskDefinition:
    def test_submodule_tasklist_roundtrip(self):
        tl = Tasklist(
            tasks={
                "B": TaskDefinition(
                    type="submodule", submodule="fact_review_loop",
                    inputs={"a": "{spec.x}"},
                    outputs={"s": "sum"},
                    model="deepseek-chat",
                ),
            },
            flow="B",
        )
        tl2 = Tasklist.from_json(tl.to_dict())
        b = tl2.tasks["B"]
        assert b.type == "submodule"
        assert b.submodule == "fact_review_loop"
        assert b.outputs == {"s": "sum"}
        assert b.model == "deepseek-chat"


class EchoChild(SubModule):
    """纯 script 子模块：无 spec 输入，终点输出固定 dict。"""

    name = "echo_child"
    spec_schema = SpecSchema(input={}, output={"msg": "str"})
    tasklist = Tasklist(
        tasks={"S": TaskDefinition(type="script", script="echo")},
        flow="[S]",
    )

    @script("echo")
    def echo(view):
        return {"msg": "from_child"}


class Parent(SubModule):
    """引用 EchoChild 的父模块：B = submodule 节点，C 读取其输出。"""

    name = "parent"
    modules = {"echo_child": EchoChild}
    tasklist = Tasklist(
        tasks={
            "B": TaskDefinition(type="submodule", submodule="echo_child"),
            "C": TaskDefinition(type="script", script="read", inputs={"data": "B"}),
        },
        flow="[B] --> C",
    )

    @script("read")
    def read(view):
        return {"got": view["B"].value}


class TestSubmoduleNode:
    @pytest.mark.asyncio
    async def test_basic_run(self, mock_llm):
        firings = await Parent(llm_client=mock_llm).run({"x": 1}, max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"msg": "from_child"}}

    @pytest.mark.asyncio
    async def test_undeclared_submodule_rejected(self, mock_llm):
        class Bad(SubModule):
            name = "bad"
            modules = {}
            tasklist = Tasklist(
                tasks={"B": TaskDefinition(type="submodule", submodule="nope")},
                flow="B",
            )

        with pytest.raises(ValueError, match="nope"):
            await Bad(llm_client=mock_llm).run({}, max_ticks=20)

    @pytest.mark.asyncio
    async def test_outputs_field_not_in_schema_rejected(self, mock_llm):
        class Bad(SubModule):
            name = "bad"
            modules = {"echo_child": EchoChild}
            tasklist = Tasklist(
                tasks={
                    "B": TaskDefinition(
                        type="submodule", submodule="echo_child",
                        outputs={"x": "no_such"},
                    ),
                },
                flow="B",
            )

        with pytest.raises(ValueError, match="no_such"):
            await Bad(llm_client=mock_llm).run({}, max_ticks=20)

    @pytest.mark.asyncio
    async def test_inputs_spec_and_node_refs(self, mock_llm):
        class SumChild(SubModule):
            name = "sum_child"
            spec_schema = SpecSchema(input={"a": "int", "b": "int"}, output={"sum": "int"})
            harnesses = [
                HarnessConfig(
                    name="sum", prompt_core="求和：{a} + {b}",
                    output_format=OutputFormat(type="json_object"),
                ),
            ]
            tasklist = Tasklist(
                tasks={
                    "S": TaskDefinition(
                        type="harness", harness="sum",
                        inputs={"a": "{spec.a}", "b": "{spec.b}"},
                        outputformat={"type": "json_object"},
                    ),
                },
                flow="[S]",
            )

        class P2(SubModule):
            name = "p2"
            modules = {"sum_child": SumChild}
            tasklist = Tasklist(
                tasks={
                    "A": TaskDefinition(type="script", script="gen"),
                    "B": TaskDefinition(
                        type="submodule", submodule="sum_child",
                        inputs={"a": "{spec.x}", "b": "A"},
                    ),
                },
                flow="[A] --> B",
            )

            @script("gen")
            def gen(view):
                return 7

        mock_llm.complete.return_value = LLMResponse(
            content='{"sum": 10}', usage={}, finish_reason="end_turn")
        firings = await P2(llm_client=mock_llm).run({"x": 3}, max_ticks=20)
        assert mock_llm.complete.await_args is not None
        prompt = mock_llm.complete.await_args.kwargs["prompt"]
        assert "3" in prompt and "7" in prompt
        b_out = next(f.output for f in firings if f.node == "B")
        assert b_out == {"sum": 10}

    @pytest.mark.asyncio
    async def test_outputs_mapping(self, mock_llm):
        class P3(SubModule):
            name = "p3"
            modules = {"echo_child": EchoChild}
            tasklist = Tasklist(
                tasks={
                    "B": TaskDefinition(
                        type="submodule", submodule="echo_child",
                        outputs={"renamed": "msg"},
                    ),
                    "C": TaskDefinition(type="script", script="read", inputs={"data": "B"}),
                },
                flow="[B] --> C",
            )

            @script("read")
            def read(view):
                return {"got": view["B"].value}

        firings = await P3(llm_client=mock_llm).run({}, max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"renamed": "from_child"}}

    @pytest.mark.asyncio
    async def test_child_spec_validation_failure_is_infrastructure(self, mock_llm):
        class SumChild2(SubModule):
            name = "sum_child2"
            spec_schema = SpecSchema(input={"a": "int"}, output={"sum": "int"})
            tasklist = Tasklist(
                tasks={"S": TaskDefinition(type="script", script="echo")},
                flow="[S]",
            )

            @script("echo")
            def echo(view):
                return {"sum": 0}

        class P4(SubModule):
            name = "p4"
            modules = {"sum_child2": SumChild2}
            tasklist = Tasklist(
                tasks={"B": TaskDefinition(type="submodule", submodule="sum_child2")},
                flow="B",
            )

        from tickflow import Failure
        firings = await P4(llm_client=mock_llm).run({}, max_ticks=20)
        b_out = next(f.output for f in firings if f.node == "B")
        assert isinstance(b_out, Failure)
        assert b_out.type == "infrastructure"

    @pytest.mark.asyncio
    async def test_submodule_node_in_loop(self, mock_llm):
        def until3(view):
            return view["A"].value["n"] < 3

        class LoopChild(SubModule):
            name = "loop_child"
            spec_schema = SpecSchema(input={"seed": "any"}, output={"msg": "str"})
            tasklist = Tasklist(
                tasks={"S": TaskDefinition(type="script", script="echo")},
                flow="[S]",
            )

            @script("echo")
            def echo(view):
                return {"msg": "from_child"}

        class LoopParent(SubModule):
            name = "loop_parent"
            modules = {"loop_child": LoopChild}
            guards = [("until3", until3)]
            tasklist = Tasklist(
                tasks={
                    "A": TaskDefinition(type="script", script="counter"),
                    "B": TaskDefinition(
                        type="submodule", submodule="loop_child",
                        inputs={"seed": "A"},
                    ),
                },
                flow="[A] --|until3|--> A\nA --> B",
            )

            @script("counter")
            def counter(view):
                n = view.state.get("n", 0) + 1
                view.state["n"] = n
                return {"n": n}

        firings = await LoopParent(llm_client=mock_llm).run({}, max_ticks=30)
        b_firings = [f for f in firings if f.node == "B"]
        assert len(b_firings) == 3  # A 循环 3 轮，B（submodule 节点）每轮触发一次

    @pytest.mark.asyncio
    async def test_procedural_module_api_equivalent(self, mock_llm):
        """过程式 Module(spec, tasklist, modules=...) 与类式同效。"""
        from module_harness.module import Module
        from module_harness.registry import HarnessRegistry

        # tasklist 引用的脚本需预注册进 registry（过程式 Module 不收集类内
        # @script——类式路径由 SubModule._build_registry 收集，此处手动注册）
        reg = HarnessRegistry(llm_client=mock_llm)

        @reg.script("read")
        def read(view):
            return {"got": view["B"].value}

        mod = Module(
            spec={"x": 1},
            tasklist=Parent.tasklist,
            llm_client=mock_llm,
            registry=reg,
            modules={"echo_child": EchoChild},
            review_harness=None,
        )
        firings = await mod.run(max_ticks=20)
        c_out = next(f.output for f in firings if f.node == "C")
        assert c_out == {"got": {"msg": "from_child"}}
