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
    """造全字段 entry 模块文件（详情归一测试用：模板/默认 spec/schema/
    子模块/per-template 覆盖声明——t2 只覆盖 default_spec，schema 回落）。"""
    (d / f"{name}.py").write_text(f"""\
from __future__ import annotations
from module_harness.cli.entry import ModuleEntry, TemplateSpec
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
        "t1": {{"name": "t1", "description": "模板一",
                "tasklist": {{"Tasks": {{}}, "Flow": "[]"}}}},
    }},
    build_registry=_registry_for,
    default_template="t1",
    default_spec={{"name": "world"}},
    spec_schema={{"name": "str"}},
    submodules={{"Helper": Helper}},
    template_specs={{"t2": TemplateSpec(default_spec={{"name": "override"}})}},
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

    def test_entry_spec_for_delegates(self, fake_home, tmp_path, monkeypatch):
        """ResolvedModule.spec_for → entry.spec_for（回落逻辑唯一驻点）。"""
        monkeypatch.chdir(tmp_path)
        mods = tmp_path / "modules"
        mods.mkdir()
        _detail_entry_py(mods)
        res = store.resolve_module_full("hello")
        assert res is not None
        # 覆盖模板：default_spec 取覆盖，schema 回落；其余模板/None 全回落
        assert res.spec_for("t2") == ({"name": "str"}, {"name": "override"})
        assert res.spec_for("t1") == ({"name": "str"}, {"name": "world"})
        assert res.spec_for(None) == ({"name": "str"}, {"name": "world"})
        with pytest.raises(ValueError, match="未注册"):
            res.spec_for("ghost")

    def test_packed_spec_for_passthrough(self, fake_home, tmp_path):
        """packed 无 per-template 概念：透传模块级 schema，default_spec 无键 → None。"""
        packs = tmp_path / "packs"
        _PackedMod.make().pack(packs / "packed_mod")
        res = store.resolve_module_full("packed_mod", search=[packs])
        assert res is not None
        assert res.spec_for(None) == ({"name": "str"}, None)
        assert res.spec_for("anything") == ({"name": "str"}, None)

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
            # 解析后对象列表（排序稳定）：description 取模板 JSON（缺省 ""），
            # spec 两键 = spec_for 解析结果（t2 只覆盖 default_spec，schema 回落）
            "templates": [
                {"name": "t1", "description": "模板一",
                 "spec_schema": {"name": "str"}, "default_spec": {"name": "world"}},
                {"name": "t2", "description": "",
                 "spec_schema": {"name": "str"}, "default_spec": {"name": "override"}},
            ],
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


class TestPackedDefaultSpec:
    """packed default_spec 契约：manifest 键 → ResolvedModule/detail 透出。"""

    def test_packed_default_spec_passthrough(self, fake_home, tmp_path):
        packs = tmp_path / "packs"
        out = _PackedMod.make().pack(packs / "packed_mod")
        mp = out / "module.json"
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        manifest["default_spec"] = {"name": "world"}
        mp.write_text(json.dumps(manifest), encoding="utf-8")
        res = store.resolve_module_full("packed_mod", search=[packs])
        assert res is not None
        assert res.default_spec == {"name": "world"}
        assert res.spec_for(None) == ({"name": "str"}, {"name": "world"})
        assert res.spec_for("anything") == ({"name": "str"}, {"name": "world"})
        assert store.detail_to_dict(res)["default_spec"] == {"name": "world"}


# ── install_submodules：pack 内 submodule 递归登记（编辑闭环补链）──────


class _SubFam:
    """三层嵌套 submodule 家族：parent_mod ⊃ echo_sub ⊃ leaf_sub。"""

    @staticmethod
    def make_parent():
        from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
        from module_harness.model.submodule import SubModule, script

        class LeafSub(SubModule):
            name = "leaf_sub"
            description = "叶子子模块"
            spec_schema = SpecSchema(input={"x": "str"})
            tasklist = Tasklist(
                tasks={"L": TaskDefinition(type="script", script="leaf_fn")},
                flow="[L]",
            )

            @script("leaf_fn")
            def leaf_fn(view):
                return {"leaf": "ok"}

        class EchoSub(SubModule):
            name = "echo_sub"
            description = "回声子模块"
            spec_schema = SpecSchema(input={"x": "str"})
            modules = {"leaf_sub": LeafSub}
            tasklist = Tasklist(
                tasks={"E": TaskDefinition(type="script", script="echo_fn")},
                flow="[E]",
            )

            @script("echo_fn")
            def echo_fn(view):
                return {"echo": "ok"}

        class ParentMod(SubModule):
            name = "parent_mod"
            description = "父模块"
            spec_schema = SpecSchema(input={"x": "str"})
            modules = {"echo_sub": EchoSub}
            tasklist = Tasklist(
                tasks={"P": TaskDefinition(type="script", script="parent_fn")},
                flow="[P]",
            )

            @script("parent_fn")
            def parent_fn(view):
                return {"parent": "ok"}

        return ParentMod()


class TestInstallSubmodules:
    """install_submodules：把 pack 内 submodules/** 递归登记为独立 packed 模块。

    背景：自包含 pack 的 submodule 只存在于包内目录（运行期零依赖），但反解
    编辑闭环按名从 store 解析 submodule 源包——缺失即组装失败（webview
    convert 的 submodule 缺口）。键 = manifest name 是引用键可解析的前提。
    """

    def test_installs_nested_submodules(self, fake_home, tmp_path):
        pack = tmp_path / "pack"
        _SubFam.make_parent().pack(pack)
        out = store.install_submodules(pack, source="test")
        assert out["installed"] == ["echo_sub", "leaf_sub"]
        assert out["skipped"] == []
        for name in ("echo_sub", "leaf_sub"):
            assert (store.modules_dir() / name / "module.json").is_file()
            assert (store.manifests_dir() / f"{name}.json").is_file()
            hit = store.resolve_module(name, search=[store.modules_dir()])
            assert hit is not None and hit.kind == "packed"
        # manifest source 透传
        m = json.loads((store.manifests_dir() / "leaf_sub.json").read_text("utf-8"))
        assert m["source"] == "test"

    def test_no_submodules_noop(self, fake_home, tmp_path):
        pack = tmp_path / "pack"
        _PackedMod.make().pack(pack)
        out = store.install_submodules(pack, source="test")
        assert out == {"installed": [], "skipped": []}

    def test_skip_when_resolvable_in_store(self, fake_home, tmp_path):
        pack = tmp_path / "pack"
        _SubFam.make_parent().pack(pack)
        echo_dir = store.modules_dir() / "echo_sub"
        echo_dir.mkdir(parents=True)
        marker = echo_dir / "module.json"
        marker.write_text('{"name": "echo_sub"}', encoding="utf-8")
        out = store.install_submodules(pack, source="test")
        assert out["installed"] == ["leaf_sub"]   # 未冲突的照常装
        assert [s["name"] for s in out["skipped"]] == ["echo_sub"]
        # 不覆盖：占位内容原样
        assert json.loads(marker.read_text("utf-8")) == {"name": "echo_sub"}
        assert out["skipped"][0]["path"] == str(echo_dir)
        assert "已存在" in out["skipped"][0]["reason"]

    def test_skip_checks_explicit_search_paths(self, fake_home, tmp_path):
        pack = tmp_path / "pack"
        _SubFam.make_parent().pack(pack)
        elsewhere = tmp_path / "elsewhere"
        from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
        from module_harness.model.submodule import SubModule, script

        class EchoSub2(SubModule):
            name = "echo_sub"
            spec_schema = SpecSchema(input={"x": "str"})
            tasklist = Tasklist(
                tasks={"E": TaskDefinition(type="script", script="e2")},
                flow="[E]",
            )

            @script("e2")
            def e2(view):
                return {}

        EchoSub2().pack(elsewhere / "echo_sub")
        out = store.install_submodules(pack, source="test", search=[elsewhere])
        assert out["installed"] == ["leaf_sub"]
        assert [s["name"] for s in out["skipped"]] == ["echo_sub"]
        assert not (store.modules_dir() / "echo_sub").exists()

    def test_key_name_mismatch_skipped(self, fake_home, tmp_path):
        """目录键 ≠ manifest name：装了也按引用键解析不到——跳过并给理由。"""
        from module_harness.model.spec import SpecSchema, TaskDefinition, Tasklist
        from module_harness.model.submodule import SubModule, script

        class OddlyNamed(SubModule):
            name = "real_name"
            spec_schema = SpecSchema(input={"x": "str"})
            tasklist = Tasklist(
                tasks={"O": TaskDefinition(type="script", script="o1")},
                flow="[O]",
            )

            @script("o1")
            def o1(view):
                return {}

        class KeyParent(SubModule):
            name = "key_parent"
            spec_schema = SpecSchema(input={"x": "str"})
            modules = {"ref_key": OddlyNamed}
            tasklist = Tasklist(
                tasks={"K": TaskDefinition(type="script", script="k1")},
                flow="[K]",
            )

            @script("k1")
            def k1(view):
                return {}

        pack = tmp_path / "pack"
        KeyParent().pack(pack)
        out = store.install_submodules(pack, source="test")
        assert out["installed"] == []
        assert [s["name"] for s in out["skipped"]] == ["ref_key"]
        assert "不一致" in out["skipped"][0]["reason"]
        assert not (store.modules_dir() / "real_name").exists()

    def test_invalid_submodule_aborts_zero_install(self, fake_home, tmp_path):
        """坏 submodule：整体中止且零安装（先全量校验后安装，零半状态）。"""
        pack = tmp_path / "pack"
        _SubFam.make_parent().pack(pack)
        leaf_mj = pack / "submodules" / "echo_sub" / "submodules" / "leaf_sub" / "module.json"
        leaf_mj.write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="leaf_sub"):
            store.install_submodules(pack, source="test")
        assert not (store.modules_dir() / "echo_sub").exists()
        assert not (store.modules_dir() / "leaf_sub").exists()
