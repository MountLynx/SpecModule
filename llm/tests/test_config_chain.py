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
