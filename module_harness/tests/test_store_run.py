# module_harness/tests/test_store_run.py
"""store 统一枚举验收测试（module-user-store 3.x）：packed 模块可运行。

场景：pack 目录放入 store → ``run --mock`` 成功、``visualize`` 渲染、
``requires`` 失败清晰报错、同名多来源按优先级解析。
隔离：monkeypatch SPECMODULE_HOME 到临时目录（绝不触碰真实 ~/.specmodule）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from module_harness.cli import main
from module_harness.submodule import SubModule, script
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist


class PackedHello(SubModule):
    """pack 用固定 submodule：单 script 节点，无 LLM 依赖。

    spec_schema 声明 input name 必填（验收缺 spec 时 SubModule.run 的
    spec 校验报错）；script 本身不读 spec（script 节点无 spec 常量注入，
    与 graph_builder 语义一致）。
    """

    name = "packed_hello"
    version = "1.0.0"
    description = "packed hello"
    spec_schema = SpecSchema(input={"name": "str"}, output={"greeting": "str"})
    tasklist = Tasklist(
        tasks={
            "Greet": TaskDefinition(type="script", script="greet"),
        },
        flow="[Greet]",
    )

    @script("greet")
    def greet(view):
        return {"greeting": "hello packed"}


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "store"
    monkeypatch.setenv("SPECMODULE_HOME", str(home))
    return home


@pytest.fixture
def packed_dir(tmp_path):
    """在 tmp_path/pack_src 生成 packed_hello 发布目录。"""
    out = tmp_path / "pack_src"
    PackedHello().pack(out)
    return out


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _install_packed(packed_dir, store_home, name="packed_hello"):
    """把 pack 目录复制进 store/modules（模拟 specmodule install 的落盘结果）。"""
    import shutil

    dest = store_home / "modules" / name
    shutil.copytree(packed_dir, dest)
    return dest


class TestPackedRun:
    def test_run_mock_from_store(self, cwd, packed_dir, store_home, capsys):
        _install_packed(packed_dir, store_home)
        assert main([
            "run", "--module", "packed_hello",
            "--spec", '{"name": "world"}', "--mock",
        ]) == 0
        out = capsys.readouterr().out
        assert "运行完成: module=packed_hello" in out
        assert "hello packed" in out

    def test_run_default_spec_missing(self, cwd, packed_dir, store_home, capsys):
        # packed 无 default_spec：缺 spec → _resolve_spec 报错（诚实失败）
        _install_packed(packed_dir, store_home)
        assert main(["run", "--module", "packed_hello", "--mock"]) == 1
        assert "缺少 spec" in capsys.readouterr().err

    def test_visualize_packed(self, cwd, packed_dir, store_home, capsys):
        _install_packed(packed_dir, store_home)
        # 正确路径：--tasklist 传 tasklist 文件
        tl = cwd / "tl.json"
        tl.write_text(json.dumps({
            "Tasks": {"Greet": {"type": "script", "script": "greet"}},
            "Flow": "[Greet]",
        }), encoding="utf-8")
        assert main([
            "visualize", "--module", "packed_hello", "--tasklist", str(tl),
        ]) == 0
        assert "graph TD" in capsys.readouterr().out


class TestPackedRequires:
    def test_requires_failure_clear_error(self, cwd, store_home, capsys):
        # 制造 requires 无法解析的 pack：tasklist 引用未提供 harness
        import shutil

        class Broken(SubModule):
            name = "broken_mod"
            version = "1.0.0"
            description = "broken"
            requires = ["ghost_component"]
            tasklist = Tasklist(
                tasks={"A": TaskDefinition(type="script", script="x")},
                flow="[A]",
            )

        src = cwd / "broken_src"
        Broken().pack(src)
        _install_packed(src, store_home, name="broken_mod")
        assert main(["run", "--module", "broken_mod", "--mock"]) == 1
        err = capsys.readouterr().err
        assert "加载失败" in err or "requires" in err
        assert "ghost_component" in err  # 缺失项点名


class TestSameNamePriority:
    def test_entry_beats_packed(self, cwd, packed_dir, store_home, capsys, monkeypatch):
        # cwd/modules 的 entry 与 store 中 packed 同名 → entry 优先
        import shutil

        (cwd / "modules").mkdir()
        entry_py = cwd / "modules" / "packed_hello.py"
        entry_py.write_text("""\
from __future__ import annotations
from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Greet")
    def greet(view):
        return {"greeting": "from entry"}

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
            "Flow": "[Greet]",
        }

    return reg


entry = ModuleEntry(
    name="packed_hello",
    description="entry twin",
    templates={
        "packed_hello": {
            "name": "packed_hello",
            "description": "entry twin",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            },
        },
    },
    build_registry=_registry_for,
    default_spec={"name": "world"},
    default_template="packed_hello",
    review_harness=None,
)
""", encoding="utf-8")
        _install_packed(packed_dir, store_home)
        assert main(["run", "--module", "packed_hello", "--mock"]) == 0
        out = capsys.readouterr().out
        assert "from entry" in out  # entry（搜索路径序 0）优先于 store packed
