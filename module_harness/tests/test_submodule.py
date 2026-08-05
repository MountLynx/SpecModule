"""SubModule / builtins / pack / ModuleLoader 测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from module_harness.builtins import BUILTIN_HARNESS_NAMES, register_builtin_harnesses
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


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
