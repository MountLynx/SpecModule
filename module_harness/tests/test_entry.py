# module_harness/tests/test_entry.py
"""模块入口合约：ModuleEntry + discover_modules 目录发现。"""

from __future__ import annotations

import pytest

from module_harness.entry import ModuleEntry, discover_modules

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