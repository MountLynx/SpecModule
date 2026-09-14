# llm/tests/test_client_wiring.py
"""连接参数接线测试：LLMConfig 连接参数（timeout/max_retries/base_url）必须传入 SDK 客户端构造器。

回归锚点（max_retries 未接线 bug）：to_client_kwargs() 只含构造级参数，
AnthropicClient / OpenAIClient 构造时经它传参——配置值不得被静默忽略。
SDK 依赖用假模块注入（sys.modules），测试不依赖 anthropic/openai 是否安装。
"""

from __future__ import annotations

from conftest import _install_fake_sdk
from llm.config import LLMConfig
from llm.client import AnthropicClient, OpenAIClient


class TestToClientKwargs:
    """to_client_kwargs() 只产出两个 SDK 构造器都接受的连接级参数。"""

    def test_contains_only_constructor_level_params(self):
        c = LLMConfig(
            provider="openai",
            api_key="k",
            base_url="https://api.example.com",
            timeout=120.0,
            max_retries=5,
            model="m",
            max_tokens=4096,
            temperature=0.7,
        )
        kwargs = c.to_client_kwargs()
        assert kwargs == {
            "api_key": "k",
            "base_url": "https://api.example.com",
            "timeout": 120.0,
            "max_retries": 5,
        }

    def test_omits_base_url_when_unset(self):
        c = LLMConfig(api_key="k", base_url=None)
        assert "base_url" not in c.to_client_kwargs()

    def test_excludes_request_level_params(self):
        # model/max_tokens/temperature 是请求级参数，混入构造器会 TypeError
        c = LLMConfig(api_key="k", model="m", max_tokens=4096, temperature=0.7)
        kwargs = c.to_client_kwargs()
        assert "model" not in kwargs
        assert "max_tokens" not in kwargs
        assert "temperature" not in kwargs


class TestAnthropicClientWiring:
    def test_connection_params_reach_constructor(self, monkeypatch):
        capture: dict = {}
        _install_fake_sdk(monkeypatch, "anthropic", "AsyncAnthropic", capture)
        config = LLMConfig(
            provider="anthropic",
            api_key="k-anthropic",
            base_url="https://proxy.example.com",
            timeout=120.0,
            max_retries=5,
        )
        client = AnthropicClient(config)
        assert client.ready
        assert capture == {
            "api_key": "k-anthropic",
            "base_url": "https://proxy.example.com",
            "timeout": 120.0,
            "max_retries": 5,
        }


class TestOpenAIClientWiring:
    def test_connection_params_reach_constructor(self, monkeypatch):
        capture: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", capture)
        config = LLMConfig(
            provider="openai",
            api_key="k-openai",
            base_url="https://api.deepseek.com",
            timeout=120.0,
            max_retries=5,
        )
        client = OpenAIClient(config)
        assert client.ready
        assert capture == {
            "api_key": "k-openai",
            "base_url": "https://api.deepseek.com",
            "timeout": 120.0,
            "max_retries": 5,
        }

    def test_no_base_url_when_unset(self, monkeypatch):
        capture: dict = {}
        _install_fake_sdk(monkeypatch, "openai", "AsyncOpenAI", capture)
        client = OpenAIClient(LLMConfig(provider="openai", api_key="k"))
        assert client.ready
        assert "base_url" not in capture
        assert capture["timeout"] == 60.0  # LLMConfig 默认值接线
        assert capture["max_retries"] == 3
