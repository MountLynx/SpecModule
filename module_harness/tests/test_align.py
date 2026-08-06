# module_harness/tests/test_align.py
"""对齐检查 harness：ALIGN_CHECK_CONFIG / register_align_check_harness / 端到端。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.align import ALIGN_CHECK_CONFIG, register_align_check_harness
from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


class TestRegisterAlignCheck:
    def test_registers_via_builtins(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        assert reg.is_harness("align_check")
        cfg = reg.harness_config("align_check")
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"
        assert "{spec}" in cfg.prompt_core
        assert "{tasklist}" in cfg.prompt_core
        assert "{node}" in cfg.prompt_core

    def test_builtin_names_contains_align_check(self):
        assert "align_check" in BUILTIN_HARNESS_NAMES

    def test_custom_name(self, mock_llm):
        reg = HarnessRegistry(llm_client=mock_llm)
        register_align_check_harness(reg, name="my_align")
        assert reg.is_harness("my_align")
        assert not reg.is_harness("align_check")

    def test_config_shape(self):
        assert ALIGN_CHECK_CONFIG.name == "align_check"
        assert ALIGN_CHECK_CONFIG.temperature == 0.1


class TestAlignCheckEndToEnd:
    @pytest.mark.asyncio
    async def test_align_check_node_outputs_dict(self, mock_llm):
        """align_check 节点输出为解析后的 dict（json_object 自动提取）。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"aligned": true, "suggestions": "ok"}'
        )
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_check",
                    inputs={
                        "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
                        "output_a": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        out = runner.run_state.last_output("C")
        assert out == {"aligned": True, "suggestions": "ok"}
        # C 的 prompt 含 spec / tasklist / 当前位置
        assert mock_llm.complete.await_count == 2
        prompt = mock_llm.complete.call_args_list[1].kwargs["prompt"]
        assert '"source_text": "你好"' in prompt
        assert '"Tasks"' in prompt
        assert "当前位置: C" in prompt

    @pytest.mark.asyncio
    async def test_aligned_false_does_not_block(self, mock_llm):
        """aligned=false 是普通节点输出，不阻断 run（框架不强制）。"""
        mock_llm.complete.return_value = LLMResponse(
            content='{"aligned": false, "suggestions": "偏离目标"}'
        )
        reg = HarnessRegistry(llm_client=mock_llm)
        register_builtin_harnesses(reg)
        reg.harness("translate", HarnessConfig(prompt_core="翻译：{text}"))
        tl = Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "{spec.source_text}"},
                ),
                "C": TaskDefinition(
                    type="harness", harness="align_check",
                    inputs={
                        "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
                        "output_a": "A",
                    },
                ),
            },
            flow="[A] --> C",
        )
        builder = TasklistTranslator(reg, module_id="m1")
        graph, out_reg = builder.build(tl, spec=Spec({"source_text": "你好"}))
        runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
        await runner.run_until_idle(max_ticks=10)

        assert runner.status.value == "idle"  # run 正常结束
        assert runner.run_state.last_output("C") == {
            "aligned": False, "suggestions": "偏离目标",
        }
