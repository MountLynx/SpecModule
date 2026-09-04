"""smoke test 共享 fixtures — 真实 LLM 客户端、EventBus。"""

import pytest
from llm.config import LLMConfig
from llm.client import create_llm_client
from module_harness.infra.events import EventBus


@pytest.fixture(scope="module")
def llm_config():
    """真实配置：从 config.json + .env 加载。"""
    return LLMConfig.from_env()


@pytest.fixture(scope="module")
def llm_client(llm_config):
    """真实 LLM 客户端。module scope 复用连接。"""
    return create_llm_client(llm_config)


@pytest.fixture
def event_bus():
    """带录制功能的 EventBus — 每次测试独立。"""

    class RecordingBus(EventBus):
        def __init__(self):
            super().__init__()
            self.recorded: list = []

        def emit(self, event):
            self.recorded.append(event)
            super().emit(event)

    return RecordingBus()
