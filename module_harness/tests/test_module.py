"""Module 编排器集成测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.client import LLMResponse
from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.core.registry import HarnessRegistry
from module_harness.infra.events import EventBus
from module_harness.model.translator import TemplateLoader
from module_harness.model.module import Module
from module_harness.orchestrate.consistency import ConsistencyError, register_review_harness
from module_harness.infra.events import ConsistencyReviewed
from module_harness.model.spec import TaskDefinition, Tasklist


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def setup_registry(mock_llm):
    """注册最小 harness/script 集合 + 模板。"""
    bus = EventBus()
    reg = HarnessRegistry(llm_client=mock_llm, event_bus=bus)

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
        data = view.field("data")
        return {"result": "processed", "data": data}

    # 翻译 script
    @reg.script("translate_translator")
    def translate_translator(view):
        spec = view.field("spec")  # 翻译器合成视图具名字段
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

    # 审核 harness
    register_review_harness(reg)

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


class TestModuleTasklistChannel:
    def _tasklist(self):
        return Tasklist(
            tasks={
                "A": TaskDefinition(
                    type="harness", harness="translate",
                    inputs={"text": "source_text"},
                ),
                "B": TaskDefinition(
                    type="script", script="format_output", inputs={"data": "A"},
                ),
            },
            flow="[A] --> B",
        )

    def test_build_runner_with_tasklist(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_mod",
            registry=reg,
        )
        runner = mod.build_runner()
        assert runner is not None
        assert mod.review_result is not None
        assert mod.review_result.consistent is True

    @pytest.mark.asyncio
    async def test_run_with_tasklist(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        call_count = [0]

        async def fake_complete(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次调用 = 审核
                return LLMResponse(
                    content='{"consistent": true, "suggestions": ""}',
                    usage={}, finish_reason="end_turn",
                )
            # 后续 = 执行 harness
            return LLMResponse(
                content='{"translation": "你好"}',
                usage={}, finish_reason="end_turn",
            )

        mock_llm.complete = AsyncMock(side_effect=fake_complete)
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_run",
            registry=reg,
        )
        firings = await mod.run(max_ticks=10)
        assert len(firings) >= 2
        assert any(f.node == "B" for f in firings)

    def test_tasklist_inconsistent_raises(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": false, "suggestions": "flow 无法到达终点"}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_incon",
            registry=reg,
        )
        with pytest.raises(ConsistencyError) as ei:
            mod.build_runner()
        assert mod.review_result is not None
        assert "flow" in ei.value.report.suggestions

    def test_review_harness_none_skips_review(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"translation": "你好"}',
            usage={}, finish_reason="end_turn",
        )
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_norev",
            registry=reg,
            review_harness=None,
        )
        runner = mod.build_runner()
        assert runner is not None
        assert mod.review_result is None

    def test_review_event_emitted(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mock_llm.complete.return_value = LLMResponse(
            content='{"consistent": true, "suggestions": ""}',
            usage={}, finish_reason="end_turn",
        )
        seen = []
        bus.subscribe(ConsistencyReviewed, lambda e: seen.append(e))
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=self._tasklist(),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="task_evt",
            registry=reg,
        )
        mod.build_runner()
        assert len(seen) == 1
        assert seen[0].consistent is True

    def test_template_and_tasklist_mutually_exclusive(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        with pytest.raises(ValueError, match="只能传一个"):
            Module(
                spec={},
                template_name="translate",
                tasklist=self._tasklist(),
                llm_client=mock_llm,
                template_loader=loader,
            )
        with pytest.raises(ValueError, match="只能传一个"):
            Module(spec={}, llm_client=mock_llm)

    def test_tasklist_unknown_harness_rejected(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        bad = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="nope")},
            flow="[A]",
        )
        mod = Module(
            spec={},
            tasklist=bad,
            llm_client=mock_llm,
            registry=reg,
        )
        with pytest.raises(ValueError, match="校验失败"):
            mod.build_runner()

    def test_keep_records_false(self, mock_llm, setup_registry):
        reg, bus, loader = setup_registry
        mod = Module(
            spec={"source_text": "Hello"},
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="format_output", inputs={"data": "{spec.source_text}"})},
                flow="[A]",
            ),
            llm_client=mock_llm,
            event_bus=bus,
            module_id="test_kr",
            registry=reg,
            review_harness=None,   # build_runner 不触发一致性审核
            keep_records=False,
        )
        runner = mod.build_runner()
        assert runner.run_state._keep_records is False
