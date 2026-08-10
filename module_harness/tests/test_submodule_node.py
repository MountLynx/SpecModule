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
