# module_harness/tests/test_registry.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from tickflow import Registry
from tickflow.views import DictView, Resolved
from module_harness.core.config import HarnessConfig
from module_harness.infra.events import (
    EventBus, ScriptStarted, ScriptCompleted, ScriptFailed,
)
from module_harness.core.registry import HarnessRegistry


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def reg(mock_llm):
    bus = EventBus()
    return HarnessRegistry(llm_client=mock_llm, event_bus=bus)


def _make_view(**inputs) -> DictView:
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_node")


class TestHarnessRegistryInheritance:
    def test_is_subclass_of_registry(self):
        assert issubclass(HarnessRegistry, Registry)

    def test_inherited_body_registration_works(self, reg):
        @reg.body("my_body")
        def my_body(view):
            return "result"

        fn = reg.get_body("my_body")
        assert fn is not None


class TestHarnessRegistration:
    def test_harness_registers_body(self, reg):
        cfg = HarnessConfig(prompt_core="翻译：{text}")
        reg.harness("translate", cfg)

        assert reg.has_body("translate")
        assert reg.is_harness("translate")
        assert not reg.is_script("translate")

    def test_harness_chain_calls(self, reg):
        cfg1 = HarnessConfig(prompt_core="A")
        cfg2 = HarnessConfig(prompt_core="B")
        reg.harness("a", cfg1).harness("b", cfg2)

        assert reg.has_body("a")
        assert reg.has_body("b")

    def test_harness_config_retrieval(self, reg):
        cfg = HarnessConfig(prompt_core="核心", model="gpt-4o")
        reg.harness("mine", cfg)

        retrieved = reg.harness_config("mine")
        assert retrieved is cfg
        assert retrieved.model == "gpt-4o"

    def test_harness_config_none_for_unknown(self, reg):
        assert reg.harness_config("nope") is None

    def test_is_harness_false_for_regular_body(self, reg):
        reg.body("regular", lambda v: "ok")
        assert not reg.is_harness("regular")

    @pytest.mark.asyncio
    async def test_harness_body_callable(self, reg):
        from llm.client import LLMResponse
        reg._llm_client.complete.return_value = LLMResponse(
            content="直接文本响应",
            usage={},
            finish_reason="end_turn",
        )
        cfg = HarnessConfig(prompt_core="测试：{input}")
        reg.harness("test_h", cfg)

        body = reg.get_body("test_h")
        result = await body(_make_view(input="hello"))

        assert result == "直接文本响应"


class TestScriptRegistration:
    def test_script_registers_body(self, reg):
        @reg.script("compute")
        def compute(view):
            return {"count": len(view.data.value)}

        assert reg.has_body("compute")
        assert reg.is_script("compute")
        assert not reg.is_harness("compute")

    def test_script_emits_start_and_complete(self, reg):
        events = []
        reg._event_bus.subscribe(ScriptStarted, lambda e: events.append("start"))
        reg._event_bus.subscribe(ScriptCompleted, lambda e: events.append("complete"))

        @reg.script("my_script")
        def my_script(view):
            return view["input"].value * 2  # "input" 是保留名，经 getitem 访问（tickflow 0.2 约定）

        body = reg.get_body("my_script")
        result = body(_make_view(input=21))

        assert result == 42
        assert events == ["start", "complete"]

    def test_script_emits_failed_on_exception(self, reg):
        failures = []
        reg._event_bus.subscribe(ScriptFailed, lambda e: failures.append(e))

        @reg.script("bad")
        def bad(view):
            raise ValueError("故意的错误")

        body = reg.get_body("bad")
        with pytest.raises(ValueError, match="故意的错误"):
            body(_make_view())

        assert len(failures) == 1
        assert failures[0].error == "故意的错误"

    def test_script_with_no_event_bus_does_not_raise(self, mock_llm):
        reg2 = HarnessRegistry(llm_client=mock_llm, event_bus=None)

        @reg2.script("silent")
        def silent(view):
            return 1

        body = reg2.get_body("silent")
        result = body(_make_view())
        assert result == 1

    def test_is_script_false_for_regular_body(self, reg):
        reg.body("plain", lambda v: "x")
        assert not reg.is_script("plain")
