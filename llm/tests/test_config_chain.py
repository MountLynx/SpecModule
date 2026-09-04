# llm/tests/test_config_chain.py
"""配置回退链测试（module-user-store D4）：os.environ > 项目根 > store 根。

隔离要求：所有用例注入临时 store 根（SPECMODULE_HOME），绝不读真实
``~/.specmodule``；env 用例显式清理相关键。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm import config as cfg


@pytest.fixture
def roots(tmp_path):
    """项目根 + store 根（临时目录）。"""
    project = tmp_path / "project"
    store = tmp_path / "store"
    project.mkdir()
    store.mkdir()
    return project, store


def _write_config(root: Path, providers, models=None) -> None:
    (root / "config.json").write_text(json.dumps({
        "providers": providers,
        "models": models or [],
    }), encoding="utf-8")


class TestConfigFallbackChain:
    def test_project_only(self, roots):
        project, store = roots
        _write_config(project, [{"sdktype": "openai", "api_key_env": "K1"}])
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.provider == "openai"

    def test_store_only_when_project_empty(self, roots, monkeypatch):
        project, store = roots
        _write_config(store, [{"sdktype": "anthropic", "api_key_env": "K2"}])
        monkeypatch.setenv("K2", "store-key")
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.provider == "anthropic"
        assert c.api_key == "store-key"

    def test_project_overrides_store(self, roots, monkeypatch):
        project, store = roots
        _write_config(project, [{"sdktype": "openai", "api_key_env": "KP"}])
        _write_config(store, [{"sdktype": "anthropic", "api_key_env": "KS"}])
        monkeypatch.setenv("KP", "project-key")
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.provider == "openai"  # 项目级优先于 store 级

    def test_env_overrides_dotenv(self, roots, monkeypatch):
        project, store = roots
        (project / ".env").write_text("MY_KEY=from-dotenv\n", encoding="utf-8")
        monkeypatch.setenv("MY_KEY", "from-env")
        cfg._load_dotenv([project, store])
        assert cfg.os.environ["MY_KEY"] == "from-env"  # env 最高，不覆盖

    def test_dotenv_fills_missing(self, roots, monkeypatch):
        project, store = roots
        (store / ".env").write_text("MY_KEY=from-store\n", encoding="utf-8")
        monkeypatch.delenv("MY_KEY", raising=False)
        cfg._load_dotenv([project, store])
        assert cfg.os.environ["MY_KEY"] == "from-store"

    def test_rules_store_fallback(self, roots):
        project, store = roots
        (store / "rules.txt").write_text("STORE RULES\n", encoding="utf-8")
        assert cfg._load_rules_txt([project, store]) == "STORE RULES"

    def test_rules_project_priority(self, roots):
        project, store = roots
        (project / "rules.txt").write_text("PROJECT RULES\n", encoding="utf-8")
        (store / "rules.txt").write_text("STORE RULES\n", encoding="utf-8")
        assert cfg._load_rules_txt([project, store]) == "PROJECT RULES"

    def test_no_config_raises(self, roots):
        project, store = roots
        with pytest.raises(ValueError, match="providers 为空或缺失"):
            cfg.LLMConfig.from_env(project_root=project, store_root=store)

    def test_config_json_fallback_used(self, roots):
        # store 有 config.json、项目没有 → store 生效（不走"未找到"警告路径）
        project, store = roots
        _write_config(store, [{"sdktype": "openai", "api_key_env": ""}])
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.provider == "openai"


class TestDefaultGenerationParams:
    """默认生成参数（max_tokens / temperature）来自 models[0]，config.json 可达。"""

    def test_max_tokens_from_first_model(self, roots):
        # 推理模型思考耗尽 4096 上限的解法：models[0] 抬高默认 max_tokens
        project, store = roots
        _write_config(
            project,
            [{"sdktype": "openai", "api_key_env": "K1"}],
            models=[{"name": "reasoner", "max_tokens": 32768}],
        )
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.max_tokens == 32768

    def test_max_tokens_default_without_model_entry(self, roots):
        project, store = roots
        _write_config(project, [{"sdktype": "openai", "api_key_env": "K1"}])
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.max_tokens == 4096

    def test_max_tokens_default_without_key_in_entry(self, roots):
        project, store = roots
        _write_config(
            project,
            [{"sdktype": "openai", "api_key_env": "K1"}],
            models=[{"name": "plain", "multimodal": False}],
        )
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.max_tokens == 4096

    def test_explicit_override_beats_models0(self, roots):
        project, store = roots
        _write_config(
            project,
            [{"sdktype": "openai", "api_key_env": "K1"}],
            models=[{"name": "reasoner", "max_tokens": 32768}],
        )
        c = cfg.LLMConfig.from_env(
            project_root=project, store_root=store, max_tokens=8192
        )
        assert c.max_tokens == 8192

    def test_only_first_model_counts(self, roots):
        # 默认参数只取 models[0]（与 temperature 同规则）；后续条目是能力注册表
        project, store = roots
        _write_config(
            project,
            [{"sdktype": "openai", "api_key_env": "K1"}],
            models=[
                {"name": "a", "max_tokens": 4096},
                {"name": "b", "max_tokens": 65536},
            ],
        )
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.max_tokens == 4096
        assert c.model == "a"

    def test_temperature_still_from_first_model(self, roots):
        # 既有 temperature 规则回归锚点：与 max_tokens 同源同构
        project, store = roots
        _write_config(
            project,
            [{"sdktype": "openai", "api_key_env": "K1"}],
            models=[{"name": "a", "temperature": 0.2, "max_tokens": 32768}],
        )
        c = cfg.LLMConfig.from_env(project_root=project, store_root=store)
        assert c.temperature == 0.2
        assert c.max_tokens == 32768
