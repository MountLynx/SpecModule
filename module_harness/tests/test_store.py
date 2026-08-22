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
from module_harness.entry import ModuleEntry
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    return HarnessRegistry(llm_client=llm_client, event_bus=event_bus or __import__('module_harness.events', fromlist=['EventBus']).EventBus.null())


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
