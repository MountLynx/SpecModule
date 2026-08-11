# module_harness/tests/test_entry.py
"""模块入口合约：ModuleEntry + discover_modules 目录发现。"""

from __future__ import annotations

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

    def test_skip_double_underscore_file(self, tmp_path):
        d = _write(tmp_path, "_private.py", GOOD)
        assert discover_modules(d) == {}

    def test_duplicate_name_last_wins(self, tmp_path):
        d = _write(tmp_path, "a.py", GOOD)
        _write(tmp_path, "b.py", GOOD_B)
        entries = discover_modules(d)
        assert list(entries) == ["hello"]
        assert entries["hello"].description == "第二个 hello（覆盖用）"