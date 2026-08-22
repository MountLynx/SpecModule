# module_harness/tests/test_store_publish.py
"""写与发测试（module-user-store 5.x）：init --as-dir 目录形态 + publish。

覆盖：骨架结构、冒烟可运行、幂等/冲突语义、publish 目录往返、
单文件形态诚实报错。
"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main
from module_harness.scaffold import scaffold_dir


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "store"
    monkeypatch.setenv("SPECMODULE_HOME", str(home))
    return home


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestInitDirForm:
    def test_scaffold_dir_structure(self, tmp_path):
        result = scaffold_dir("mymod", base_dir=tmp_path)
        mod = tmp_path / "modules" / "mymod"
        assert (mod / "module.json").is_file()
        for sub in ("scripts", "harnesses", "commands", "guards", "submodules"):
            assert (mod / sub).is_dir()
        assert (mod / "scripts" / "greet.py").is_file()
        manifest = json.loads((mod / "module.json").read_text(encoding="utf-8"))
        assert manifest["name"] == "mymod"
        assert manifest["tasklist"]["Tasks"]["Greet"]["script"] == "greet"
        # 项目文件补齐
        assert (tmp_path / "config.json").is_file()
        assert (tmp_path / ".gitignore").is_file()

    def test_invalid_name(self, tmp_path):
        with pytest.raises(ValueError):
            scaffold_dir("bad-name", base_dir=tmp_path)

    def test_conflict_without_force(self, tmp_path):
        scaffold_dir("mymod", base_dir=tmp_path)
        with pytest.raises(ValueError, match="已存在"):
            scaffold_dir("mymod", base_dir=tmp_path)
        # force 覆盖：目录仍完整
        scaffold_dir("mymod", base_dir=tmp_path, force=True)
        assert (tmp_path / "modules" / "mymod" / "module.json").is_file()

    def test_cli_init_as_dir(self, cwd, store_home, capsys):
        assert main(["init", "hello", "--as-dir"]) == 0
        out = capsys.readouterr().out
        assert "创建" in out
        assert (cwd / "modules" / "hello" / "module.json").is_file()

    def test_cli_init_plain_keeps_single_file(self, cwd, store_home, capsys):
        assert main(["init", "hello"]) == 0
        capsys.readouterr()
        assert (cwd / "modules" / "hello.py").is_file()
        assert not (cwd / "modules" / "hello").exists()


class TestPublish:
    def test_publish_dir_form_roundtrip(self, cwd, store_home, capsys):
        # init --as-dir → 补 script → publish → store 可 run
        assert main(["init", "pubmod", "--as-dir"]) == 0
        capsys.readouterr()
        assert main(["publish", "pubmod", "--from", str(cwd / "modules" / "pubmod")]) == 0
        out = capsys.readouterr().out
        assert "已发布" in out
        dest = store_home / "modules" / "pubmod"
        assert (dest / "module.json").is_file()
        manifest = json.loads(
            (store_home / "manifests" / "pubmod.json").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "pubmod"
        # 发布后 store 枚举可见
        assert main(["list"]) == 0
        assert "pubmod" in capsys.readouterr().out

    def test_publish_invalid_source(self, cwd, store_home, capsys):
        assert main(["publish", "ghost", "--from", "."]) == 1
        assert "发布源无效" in capsys.readouterr().err

    def test_publish_single_file_conversion(self, cwd, store_home, capsys):
        # 单文件 entry 形态：等价 SubModule 转化发布（D9），转化后可 run
        assert main(["init", "sfmod"]) == 0
        capsys.readouterr()
        assert main(["publish", "sfmod", "--from", "."]) == 0
        out = capsys.readouterr().out
        assert "已发布（单文件转化）" in out
        dest = store_home / "modules" / "sfmod"
        assert (dest / "module.json").is_file()
        # 转化产物含 scripts/（entry 的 echo script）
        assert (dest / "scripts").is_dir()
        # 发布后 store 枚举可见且可运行
        assert main(["list"]) == 0
        assert "sfmod" in capsys.readouterr().out
        assert main(["run", "--module", "sfmod", "--mock"]) == 0
        assert "运行完成" in capsys.readouterr().out

    def test_publish_single_file_missing_template(self, cwd, store_home, capsys):
        # entry 无 default_template → 诚实报错（无法确定驱动 tasklist）
        (cwd / "modules").mkdir()
        (cwd / "modules" / "notpl.py").write_text("""\
from __future__ import annotations
from module_harness.entry import ModuleEntry
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    return HarnessRegistry(llm_client=llm_client, event_bus=event_bus or __import__('module_harness.events', fromlist=['EventBus']).EventBus.null())


entry = ModuleEntry(
    name="notpl",
    description="no default template",
    templates={},
    build_registry=_registry_for,
    default_template=None,
    review_harness=None,
)
""", encoding="utf-8")
        assert main(["publish", "notpl", "--from", "."]) == 1
        assert "default_template" in capsys.readouterr().err

    def test_publish_requires_valid_pack(self, cwd, store_home, capsys):
        bad = cwd / "bad"
        bad.mkdir()
        (bad / "module.json").write_text("{broken", encoding="utf-8")
        assert main(["publish", "bad", "--from", str(bad)]) == 1
        assert "发布失败" in capsys.readouterr().err
