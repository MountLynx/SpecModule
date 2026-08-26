# module_harness/tests/test_entry.py
"""模块入口合约：ModuleEntry + discover_modules 目录发现。"""

from __future__ import annotations

import asyncio

import pytest

from module_harness.entry import ModuleEntry, discover_modules
from module_harness.module import Module
from module_harness.spec import TaskDefinition, Tasklist

GOOD = '''
from module_harness.entry import ModuleEntry

entry = ModuleEntry(
    name="hello",
    description="测试模块",
    templates={"hello": {}},
    build_registry=None,
    default_template="hello",
)
'''

GOOD_B = '''
from module_harness.entry import ModuleEntry

entry = ModuleEntry(
    name="hello",
    description="第二个 hello（覆盖用）",
    templates={},
)
'''


def _write(tmp_path, name, body, subdir="modules"):
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d


class TestDiscoverModules:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert discover_modules(tmp_path / "nope") == {}

    def test_empty_dir_returns_empty(self, tmp_path):
        d = tmp_path / "modules"
        d.mkdir()
        assert discover_modules(d) == {}

    def test_single_module(self, tmp_path):
        d = _write(tmp_path, "hello.py", GOOD)
        entries = discover_modules(d)
        assert set(entries) == {"hello"}
        assert entries["hello"].description == "测试模块"
        assert entries["hello"].default_template == "hello"

    def test_skip_file_without_entry(self, tmp_path):
        d = _write(tmp_path, "noentry.py", "x = 1\n")
        assert discover_modules(d) == {}

    def test_skip_file_with_wrong_entry_type(self, tmp_path):
        d = _write(tmp_path, "badtype.py", 'entry = "not a module"\n')
        assert discover_modules(d) == {}

    def test_skip_underscore_prefixed_file(self, tmp_path):
        d = _write(tmp_path, "_private.py", GOOD)
        assert discover_modules(d) == {}

    def test_import_error_does_not_block_discovery(self, tmp_path):
        # 导入抛异常的文件跳过，后续好文件仍被发现（不阻断整体发现）
        d = _write(tmp_path, "bad.py", "raise RuntimeError('boom')\n")
        _write(tmp_path, "good.py", GOOD)
        entries = discover_modules(d)
        assert set(entries) == {"hello"}

    def test_duplicate_name_last_wins(self, tmp_path):
        d = _write(tmp_path, "a.py", GOOD)
        _write(tmp_path, "b.py", GOOD_B)
        entries = discover_modules(d)
        assert list(entries) == ["hello"]
        assert entries["hello"].description == "第二个 hello（覆盖用）"


class TestModuleEntry:
    def test_default_template_not_in_templates_raises(self):
        with pytest.raises(ValueError):
            ModuleEntry(
                name="x", description="x", templates={}, default_template="nope"
            )

    def test_default_template_in_templates_ok(self):
        e = ModuleEntry(
            name="x", description="x", templates={"t": {}}, default_template="t"
        )
        assert e.default_template == "t"


_TL = {
    "Tasks": {
        "A": {"type": "script", "script": "echo", "inputs": {}},
        "B": {"type": "script", "script": "echo", "inputs": {"data": "A"}},
    },
    "Flow": "[A] --> B",
}


def _entry_with_registry():
    """tutorial 式 entry：script 翻译器 + echo 节点（零 LLM 依赖）。"""
    from module_harness import HarnessRegistry
    from module_harness.events import EventBus

    def build_registry(llm_client, template_name, event_bus):
        reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

        @reg.script("echo")
        def echo(view):
            return {"ok": True}

        @reg.script("tl")
        def tl(view):
            return _TL

        return reg

    return ModuleEntry(
        name="bm",
        description="build_module 测试模块",
        templates={"tpl": {"name": "tpl", "description": "固定流水线",
                           "translation": {"type": "script", "script": "tl"},
                           "tasklist": _TL}},
        build_registry=build_registry,
        default_template="tpl",
        review_harness=None,
    )


class TestBuildModule:
    def test_wires_loader_registry_and_review(self, mock_llm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = _entry_with_registry()
        mod = entry.build_module({"x": 1}, template_name="tpl",
                                 llm_client=mock_llm, module_id="bm1",
                                 base_dir=tmp_path)
        try:
            assert isinstance(mod, Module)
            assert mod.template_name == "tpl"
            assert mod._loader.get("tpl") is not None
            assert mod.review_harness is None
        finally:
            mod.close()

    def test_unknown_template_raises_with_available(self, mock_llm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = _entry_with_registry()
        with pytest.raises(ValueError, match=r"未注册.*tpl"):
            entry.build_module({"x": 1}, template_name="nope",
                               llm_client=mock_llm, base_dir=tmp_path)

    def test_tasklist_normalizes_template_none(self, mock_llm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = _entry_with_registry()
        tl = Tasklist(tasks={"A": TaskDefinition(type="script", script="echo")},
                      flow="[A]")
        mod = entry.build_module({"x": 1}, template_name="tpl", tasklist=tl,
                                 llm_client=mock_llm, base_dir=tmp_path)
        try:
            assert mod.template_name is None
            assert mod.tasklist is tl
        finally:
            mod.close()

    def test_review_false_sets_none(self, mock_llm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = ModuleEntry(name="bm2", description="",
                            templates={}, review_harness="spec_tasklist_review")
        tl = Tasklist(tasks={"A": TaskDefinition(type="script", script="echo")},
                      flow="[A]")
        mod = entry.build_module({"x": 1}, tasklist=tl,
                                 llm_client=mock_llm, review=False,
                                 base_dir=tmp_path)
        try:
            assert mod.review_harness is None
        finally:
            mod.close()

    def test_end_to_end_run(self, mock_llm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        entry = _entry_with_registry()
        mod = entry.build_module({"x": 1}, llm_client=mock_llm,
                                 module_id="bm3", base_dir=tmp_path)
        try:
            firings = asyncio.run(mod.run(max_ticks=10))
            assert {f.node for f in firings} == {"A", "B"}
        finally:
            mod.close()