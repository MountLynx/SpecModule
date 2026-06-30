"""Module 编排器集成测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry
from module_harness.events import EventBus
from module_harness.translator import TemplateLoader
from module_harness.module import Module


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def setup_registry(mock_llm):
    """注册最小 harness/script 集合 + 模板。"""
    reg = HarnessRegistry(llm_client=mock_llm)
    bus = EventBus()

    # 翻译 harness
    reg.harness("spec_to_tasklist", HarnessConfig(
        prompt_core="生成 tasklist JSON",
        output_format=OutputFormat(type="json_object"),
    ))

    # 执行 harness
    reg.harness("translate", HarnessConfig(
        prompt_core="翻译：{text}",
        prompt_modes={"formal": "请使用正式语气", "casual": "请使用随意语气"},
        output_format=OutputFormat(type="json_object"),
    ))

    # 后处理 script
    @reg.script("format_output")
    def format_output(view):
        data = view.data.value
        return {"result": "processed", "data": data}

    # 翻译 script
    @reg.script("translate_translator")
    def translate_translator(view):
        spec = view.spec.value
        return {
            "A": {
                "type": "harness",
                "harness": spec["harness_name"],
                "promptmode": spec.get("style", "formal"),
                "inputs": {"text": spec["source_text"]},
                "outputformat": {"type": "json_object"},
            },
            "B": {
                "type": "script",
                "script": "format_output",
                "inputs": {"data": "A"},
            },
        }

    # 模板
    loader = TemplateLoader()
    loader.register("translate", {
        "name": "translate",
        "description": "翻译模块",
        "translation": {"type": "script", "script": "translate_translator"},
        "tasklist": {
            "Tasks": {},
            "Flow": "[A] --> B",
        },
    })

    loader.register("translate_llm", {
        "name": "translate_llm",
        "description": "翻译模块 (LLM翻译)",
        "translation": {"type": "harness", "harness": "spec_to_tasklist", "prompt": "根据 spec 生成 tasklist JSON"},
        "tasklist": {
            "Tasks": {},
            "Flow": "[A] --> B",
        },
    })

    return reg, bus, loader


class TestModule:
    def test_build_runner_script_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}',
            usage={},
            finish_reason="end_turn",
        )

        mod = Module(
            spec={"harness_name": "translate", "source_text": "Hello", "style": "formal"},
            template_name="translate",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_mod",
            registry=reg,
        )

        runner = mod.build_runner()
        assert runner is not None
        # runner 应可通过 AsyncRunner 方法操作
        assert runner.is_idle()

    @pytest.mark.asyncio
    async def test_run_script_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好世界"}',
            usage={},
            finish_reason="end_turn",
        )

        mod = Module(
            spec={"harness_name": "translate", "source_text": "Hello", "style": "formal"},
            template_name="translate",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_run",
            registry=reg,
        )

        firings = await mod.run(max_ticks=10)
        # 至少 A 节点触发
        assert len(firings) >= 1
        assert any(f.node == "A" for f in firings)

    @pytest.mark.asyncio
    async def test_run_harness_translation(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry

        # LLM 翻译返回 + LLM 执行返回
        call_count = [0]

        async def fake_complete(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 翻译 harness 返回 tasklist
                return LLMResponse(
                    content='{"A": {"type": "harness", "harness": "translate", "inputs": {"text": "Hello"}, "outputformat": {"type": "json_object"}}, "B": {"type": "script", "script": "format_output", "inputs": {"data": "A"}}}',
                    usage={},
                    finish_reason="end_turn",
                )
            else:
                # 执行 harness 返回翻译结果
                return LLMResponse(
                    content='{"translation": "Bonjour"}',
                    usage={},
                    finish_reason="end_turn",
                )

        mock_llm.complete = AsyncMock(side_effect=fake_complete)

        mod = Module(
            spec={"task_type": "translate", "source_text": "Hello"},
            template_name="translate_llm",
            llm_client=mock_llm,
            event_bus=bus,
            template_loader=loader,
            module_id="test_llm",
            registry=reg,
        )

        firings = await mod.run(max_ticks=10)
        assert len(firings) >= 1

    def test_missing_template_raises(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={},
            template_name="nonexistent",
            llm_client=mock_llm,
            template_loader=loader,
        )
        with pytest.raises(ValueError, match="nonexistent"):
            mod.build_runner()

    def test_auto_module_id(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={},
            template_name="translate",
            llm_client=mock_llm,
            template_loader=loader,
        )
        assert mod.module_id is not None
        assert len(mod.module_id) > 0
