# llm/tests/test_config_providers.py
"""多 provider 注册表:from_env 全量解析 + provider_for 按模型查表 + for_provider 切片。

models 注册表每模型的 provider 字段自此真正路由;顶层字段(provider/api_key/base_url)
保持 = providers[0](默认 provider),向后兼容。手工构造 LLMConfig(providers 空)
一切行为不变,provider_for 返回 None = 用默认连接。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm.config import LLMConfig, ProviderConfig


def _write(root: Path, data: dict) -> None:
    (root / "config.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def roots(tmp_path):
    project = tmp_path / "project"
    store = tmp_path / "store"
    project.mkdir()
    store.mkdir()
    return project, store


MULTI = {
    "providers": [
        {"name": "deepseek", "sdktype": "openai",
         "base_url": "https://api.deepseek.com", "api_key_env": "DS_KEY"},
        {"name": "zhipu", "sdktype": "openai",
         "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZP_KEY"},
        {"name": "anthropic", "sdktype": "anthropic", "api_key_env": "AN_KEY"},
    ],
    "models": [
        {"name": "deepseek-chat", "provider": "deepseek", "max_tokens": 8192},
        {"name": "cogview-4", "provider": "zhipu", "image_gen": True},
        {"name": "bare-model"},
    ],
}


def _multi_config(roots, monkeypatch) -> LLMConfig:
    project, store = roots
    _write(project, MULTI)
    monkeypatch.setenv("DS_KEY", "kd")
    monkeypatch.setenv("ZP_KEY", "kz")
    monkeypatch.setenv("AN_KEY", "ka")
    return LLMConfig.from_env(project_root=project, store_root=store)


class TestProvidersMap:
    def test_from_env_parses_all_providers(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert set(c.providers) == {"deepseek", "zhipu", "anthropic"}
        z = c.providers["zhipu"]
        assert z.sdktype == "openai"
        assert z.api_key == "kz"
        assert z.base_url == "https://open.bigmodel.cn/api/paas/v4"

    def test_top_level_fields_stay_default_provider(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider == "openai"        # providers[0] 的 sdktype
        assert c.api_key == "kd"             # providers[0] 的 key
        assert c.base_url == "https://api.deepseek.com"
        assert c.model == "deepseek-chat"    # models[0] 默认模型,不变

    def test_provider_entry_without_name_falls_back(self, roots, monkeypatch):
        project, _ = roots
        _write(project, {"providers": [{"sdktype": "openai", "api_key_env": "X"}]})
        c = LLMConfig.from_env(project_root=project, store_root=store_root(roots))
        assert list(c.providers) == ["provider_0"]


def store_root(roots):
    return roots[1]


class TestProviderFor:
    def test_lookup_by_model(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("cogview-4").name == "zhipu"
        assert c.provider_for("deepseek-chat").name == "deepseek"

    def test_unregistered_model_returns_none(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("unknown-model") is None

    def test_model_without_provider_field_returns_none(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.provider_for("bare-model") is None

    def test_handmade_config_returns_none(self):
        c = LLMConfig(api_key="k", model="m")  # providers 空
        assert c.provider_for("m") is None


class TestForProvider:
    def test_slice_switches_connection_shares_request_level(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        sl = c.for_provider(c.providers["zhipu"])
        assert sl.provider == "openai"
        assert sl.api_key == "kz"
        assert sl.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert sl.model == "deepseek-chat"        # 请求级字段共享
        assert sl.models is c.models
        assert sl.to_client_kwargs()["api_key"] == "kz"  # 建连即取该 provider

    def test_to_client_kwargs_unchanged_on_default(self, roots, monkeypatch):
        c = _multi_config(roots, monkeypatch)
        assert c.to_client_kwargs() == {
            "api_key": "kd",
            "base_url": "https://api.deepseek.com",
            "timeout": 60.0,
            "max_retries": 3,
        }
