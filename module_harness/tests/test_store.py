# module_harness/tests/test_store.py
"""store 共享层测试：家目录、搜索路径、统一枚举（module-user-store 主线）。

隔离要求（design D4）：所有测试用 monkeypatch 注入 store 根（临时目录），
绝不触碰开发者机器的真实 ``~/.specmodule``。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from module_harness import store


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """把 store 根指到临时目录（防误读真实 ~/.specmodule）。"""
    home = tmp_path / "store"
    monkeypatch.setenv("SPECMODULE_HOME", str(home))
    return home


class TestStoreHome:
    def test_default_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SPECMODULE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert store.store_home() == tmp_path / ".specmodule"

    def test_env_override(self, fake_home):
        assert store.store_home() == fake_home

    def test_lazy_creation(self, fake_home):
        assert not fake_home.exists()
        store.store_home()
        assert fake_home.is_dir()

    def test_existing_reused(self, fake_home):
        fake_home.mkdir(parents=True)
        marker = fake_home / "keep.txt"
        marker.write_text("x", encoding="utf-8")
        store.store_home()
        assert marker.exists()  # 已存在目录不被重建


class TestSearchPaths:
    def test_default_order(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPECMODULE_PATH", raising=False)
        (tmp_path / "modules").mkdir()
        store.store_home()
        (fake_home / "modules").mkdir()
        paths = store.search_paths()
        assert paths == [tmp_path / "modules", fake_home / "modules"]

    def test_env_path_in_middle(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setenv("SPECMODULE_PATH", str(extra))
        (tmp_path / "modules").mkdir()
        store.store_home()
        (fake_home / "modules").mkdir()
        paths = store.search_paths()
        assert paths == [tmp_path / "modules", extra, fake_home / "modules"]

    def test_missing_dirs_skipped(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # cwd/modules 不存在
        monkeypatch.setenv("SPECMODULE_PATH", str(tmp_path / "ghost"))
        store.store_home()  # store/modules 尚未创建
        assert store.search_paths() == []

    def test_os_pathsep_split(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("SPECMODULE_PATH", os.pathsep.join([str(a), str(b)]))
        (tmp_path / "modules").mkdir()
        store.store_home()
        (fake_home / "modules").mkdir()
        assert a in store.search_paths() and b in store.search_paths()


class TestListModules:
    def _entry_py(self, d: Path, name: str = "hello") -> Path:
        py = d / f"{name}.py"
        py.write_text(f"""\
from __future__ import annotations
from module_harness.cli.entry import ModuleEntry
from module_harness.core.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    return HarnessRegistry(llm_client=llm_client, event_bus=event_bus or __import__('module_harness.infra.events', fromlist=['EventBus']).EventBus.null())


entry = ModuleEntry(
    name={name!r},
    description="hello entry",
    templates={{}},
    build_registry=_registry_for,
    default_template=None,
    review_harness=None,
)
""", encoding="utf-8")
        return py

    def _packed(self, d: Path, name: str = "packed_mod") -> Path:
        p = d / name
        p.mkdir(parents=True)
        (p / "module.json").write_text(json.dumps({
            "name": name,
            "version": "1.2.3",
            "description": "packed desc",
            "tasklist": {"Tasks": {}, "Flow": "[A]"},
        }), encoding="utf-8")
        return p

    def test_three_sources(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # entry 在 cwd/modules
        cwd_mods = tmp_path / "modules"
        cwd_mods.mkdir()
        self._entry_py(cwd_mods)
        # packed 在 store/modules
        store_mods = fake_home / "modules"
        store_mods.mkdir(parents=True)
        self._packed(store_mods)
        # pip 来源
        pip_pack = self._packed(tmp_path / "pip_dist", name="pip_mod")
        monkeypatch.setattr(
            store, "pip_entry_point_dirs", lambda: [pip_pack]
        )
        mods = store.list_modules()
        assert set(mods) == {"hello", "packed_mod", "pip_mod"}
        hello = mods["hello"][0]
        assert hello.kind == "entry"
        assert hello.description == "hello entry"
        packed = mods["packed_mod"][0]
        assert packed.kind == "packed"
        assert packed.version == "1.2.3"
        pip = mods["pip_mod"][0]
        assert pip.kind == "pip"
        assert pip.priority > packed.priority  # pip 附加来源排最后

    def test_same_name_priority(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cwd_mods = tmp_path / "modules"
        cwd_mods.mkdir()
        self._entry_py(cwd_mods, name="dup")
        store_mods = fake_home / "modules"
        store_mods.mkdir(parents=True)
        self._packed(store_mods, name="dup")
        mods = store.list_modules()
        assert len(mods["dup"]) == 2  # 同名全量展示，不静默改名
        assert mods["dup"][0].kind == "entry"  # cwd/modules 优先

    def test_resolve_first_hit(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cwd_mods = tmp_path / "modules"
        cwd_mods.mkdir()
        self._entry_py(cwd_mods, name="dup")
        store_mods = fake_home / "modules"
        store_mods.mkdir(parents=True)
        self._packed(store_mods, name="dup")
        src = store.resolve_module("dup")
        assert src is not None and src.kind == "entry"
        assert store.resolve_module("ghost") is None

    def test_pip_dirs_skipped_on_failure(self, fake_home, monkeypatch):
        def boom():
            raise RuntimeError("broken ep")
        monkeypatch.setattr(store, "pip_entry_point_dirs", boom)
        assert store.list_modules() == {}


def _detail_entry_py(d: Path, name: str = "hello") -> None:
    """造全字段 entry 模块文件（详情归一测试用：模板/默认 spec/schema/子模块）。"""
    (d / f"{name}.py").write_text(f"""\
from __future__ import annotations
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus
from module_harness.core.registry import HarnessRegistry
from module_harness.model.submodule import SubModule


class Helper(SubModule):
    name = "helper"


def _registry_for(llm_client, template_name, event_bus):
    return HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())


entry = ModuleEntry(
    name={name!r},
    description="hello entry",
    templates={{
        "t2": {{"name": "t2", "tasklist": {{"Tasks": {{}}, "Flow": "[]"}}}},
        "t1": {{"name": "t1", "tasklist": {{"Tasks": {{}}, "Flow": "[]"}}}},
    }},
    build_registry=_registry_for,
    default_template="t1",
    default_spec={{"name": "world"}},
    spec_schema={{"name": "str"}},
    submodules={{"Helper": Helper}},
    review_harness=None,
)
""", encoding="utf-8")


class _PackedMod:
    """pack 用固定 submodule（resolve_module_full packed 命中测试用）。"""

    @staticmethod
    def make():
        from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
        from module_harness.model.submodule import SubModule, script

        class PackedMod(SubModule):
            name = "packed_mod"
            version = "1.2.3"
            description = "packed desc"
            spec_schema = SpecSchema(input={"name": "str"}, output={})
            tasklist = Tasklist(
                tasks={"Greet": TaskDefinition(type="script", script="greet")},
                flow="[Greet]",
            )

            @script("greet")
            def greet(view):
                return {"greeting": "hi"}

        return PackedMod()


class _BrokenPacked:
    """requires 无法解析的 submodule（加载失败 → ValueError 测试用）。"""

    @staticmethod
    def make():
        from module_harness.model.spec import TaskDefinition, Tasklist
        from module_harness.model.submodule import SubModule

        class Broken(SubModule):
            name = "broken_mod"
            version = "1.0.0"
            description = "broken"
            requires = ["ghost_component"]
            tasklist = Tasklist(
                tasks={"A": TaskDefinition(type="script", script="x")},
                flow="[A]",
            )

        return Broken()


class TestSearchPathsBaseDir:
    """search_paths(base_dir) 发现锚定：server 模块视图 ≡ 子进程 CLI 视图。"""

    def test_base_dir_anchors_cwd_slot(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)          # cwd 下故意无 modules/
        monkeypatch.delenv("SPECMODULE_PATH", raising=False)
        root = tmp_path / "runroot"
        (root / "modules").mkdir(parents=True)
        assert store.search_paths(base_dir=root) == [root / "modules"]

    def test_none_keeps_cwd_behavior(self, fake_home, tmp_path, monkeypatch):
        """base_dir=None = 现行为（cwd/modules），完全向后兼容。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPECMODULE_PATH", raising=False)
        (tmp_path / "modules").mkdir()
        other = tmp_path / "elsewhere"
        (other / "modules").mkdir(parents=True)
        assert store.search_paths() == [tmp_path / "modules"]
        assert store.search_paths(base_dir=other) == [other / "modules"]

    def test_priority_order_unchanged(self, fake_home, tmp_path, monkeypatch):
        """优先序不变：base_dir/modules → $SPECMODULE_PATH → store/modules。"""
        monkeypatch.chdir(tmp_path)
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setenv("SPECMODULE_PATH", str(extra))
        root = tmp_path / "runroot"
        (root / "modules").mkdir(parents=True)
        store.store_home()
        (fake_home / "modules").mkdir()
        assert store.search_paths(base_dir=root) == [
            root / "modules", extra, fake_home / "modules",
        ]


class TestResolveModuleFull:
    def test_entry_hit(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mods = tmp_path / "modules"
        mods.mkdir()
        _detail_entry_py(mods)
        res = store.resolve_module_full("hello")
        assert res is not None
        assert res.kind == "entry"
        assert res.entry is not None
        assert res.submodule is None
        assert res.description == "hello entry"
        assert res.default_template == "t1"
        assert res.default_spec == {"name": "world"}
        assert res.spec_schema == {"name": "str"}
        assert res.templates.keys() == {"t1", "t2"}
        assert res.submodules.keys() == {"Helper"}

    def test_packed_hit(self, fake_home, tmp_path, monkeypatch):
        packs = tmp_path / "packs"
        _PackedMod.make().pack(packs / "packed_mod")
        res = store.resolve_module_full("packed_mod", search=[packs])
        assert res is not None
        assert res.kind == "packed"
        assert res.submodule is not None
        assert res.entry is None
        assert res.description == "packed desc"
        assert res.default_template is None      # packed 无模板概念
        assert res.default_spec is None
        assert res.spec_schema == {"name": "str"}  # schema.input 归一
        assert res.templates == {}
        assert res.submodules == {}

    def test_not_found_returns_none(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPECMODULE_PATH", raising=False)
        assert store.resolve_module_full("ghost") is None

    def test_packed_load_failure_raises_valueerror(self, tmp_path):
        packs = tmp_path / "packs"
        _BrokenPacked.make().pack(packs / "broken_mod")
        with pytest.raises(ValueError, match="加载失败") as ei:
            store.resolve_module_full("broken_mod", search=[packs])
        assert "ghost_component" in str(ei.value)   # 原因点名

    def test_search_param_passthrough(self, fake_home, tmp_path, monkeypatch):
        """search= 显式传入时用之，不回落统一搜索路径。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SPECMODULE_PATH", raising=False)
        packs = tmp_path / "packs"
        _PackedMod.make().pack(packs / "packed_mod")
        assert store.resolve_module_full("packed_mod") is None   # 默认搜索找不到
        assert store.resolve_module_full(
            "packed_mod", search=[packs]
        ) is not None


class TestDetailToDict:
    def test_entry_fields_complete(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mods = tmp_path / "modules"
        mods.mkdir()
        _detail_entry_py(mods)
        d = store.detail_to_dict(store.resolve_module_full("hello"))
        assert d == {
            "name": "hello",
            "kind": "entry",
            "path": str(mods / "hello.py"),
            "version": "",
            "description": "hello entry",
            "default_template": "t1",
            "templates": ["t1", "t2"],     # 出名列表，排序稳定
            "default_spec": {"name": "world"},
            "spec_schema": {"name": "str"},
            "submodules": ["Helper"],
        }

    def test_packed_fields_complete(self, tmp_path):
        packs = tmp_path / "packs"
        _PackedMod.make().pack(packs / "packed_mod")
        d = store.detail_to_dict(
            store.resolve_module_full("packed_mod", search=[packs])
        )
        assert d == {
            "name": "packed_mod",
            "kind": "packed",
            "path": str(packs / "packed_mod"),
            "version": "1.2.3",
            "description": "packed desc",
            "default_template": None,
            "templates": [],
            "default_spec": None,
            "spec_schema": {"name": "str"},
            "submodules": [],
        }
