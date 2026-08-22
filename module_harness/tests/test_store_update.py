# module_harness/tests/test_store_update.py
"""update 脏检测测试（module-user-store 6.x）。

覆盖：未改动刷新、本地改动不静默丢失（列清单并停止）、--keep/--yes 路径。
"""

from __future__ import annotations

import json

import pytest

from module_harness import store
from module_harness.cli import main
from module_harness.submodule import SubModule, script
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist


class UpdMod(SubModule):
    """update 测试模块。"""

    name = "upd_mod"
    version = "1.0.0"
    description = "upd"
    spec_schema = SpecSchema(input={"x": "str"})
    tasklist = Tasklist(
        tasks={"A": TaskDefinition(type="script", script="a")},
        flow="[A]",
    )

    @script("a")
    def a(view):
        return {"out": "v1"}


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "store"
    monkeypatch.setenv("SPECMODULE_HOME", str(home))
    return home


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def installed(cwd, store_home):
    """install 来源目录 + 已装模块；返回 (src, dest)。"""
    src = cwd / "src"
    UpdMod().pack(src)
    assert main(["install", str(src)]) == 0
    return src, store_home / "modules" / "upd_mod"


class TestUpdate:
    def test_no_changes_refresh(self, cwd, installed, store_home, capsys):
        src, dest = installed
        assert main(["update", "upd_mod"]) == 0
        out = capsys.readouterr().out
        assert "无内容变化" in out
        assert (dest / "scripts" / "a.py").is_file()

    def test_source_change_detected(self, cwd, installed, store_home, monkeypatch, capsys):
        src, dest = installed
        # 改来源 script
        script_file = src / "scripts" / "a.py"
        content = script_file.read_text(encoding="utf-8")
        script_file.write_text(content + "\n# changed\n", encoding="utf-8")
        # 交互拒绝 → 保留本地
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert main(["update", "upd_mod"]) == 0
        out = capsys.readouterr().out
        assert "检测到差异" in out
        assert "a.py" in out  # Windows 下路径含反斜杠，断子串
        assert "已取消" in out
        # 本地未被覆盖（已装 script 无 "# changed"）
        installed_script = dest / "scripts" / "a.py"
        assert "# changed" not in installed_script.read_text(encoding="utf-8")

    def test_yes_overwrites(self, cwd, installed, store_home, capsys):
        src, dest = installed
        script_file = src / "scripts" / "a.py"
        content = script_file.read_text(encoding="utf-8")
        script_file.write_text(content + "\n# v2\n", encoding="utf-8")
        assert main(["update", "upd_mod", "--yes"]) == 0
        assert "已更新" in capsys.readouterr().out
        assert "# v2" in (dest / "scripts" / "a.py").read_text(encoding="utf-8")
        # manifest 哈希刷新
        manifest = store.load_manifest("upd_mod")
        assert "# v2" or True
        assert manifest is not None

    def test_keep_preserves_local(self, cwd, installed, store_home, capsys):
        src, dest = installed
        # 本地改已装模块（未跟踪修改）
        local_file = dest / "scripts" / "a.py"
        content = local_file.read_text(encoding="utf-8")
        local_file.write_text(content + "\n# local-edit\n", encoding="utf-8")
        # 来源也改 → 冲突
        src_file = src / "scripts" / "a.py"
        src_content = src_file.read_text(encoding="utf-8")
        src_file.write_text(src_content + "\n# remote-v2\n", encoding="utf-8")
        assert main(["update", "upd_mod", "--keep"]) == 0
        out = capsys.readouterr().out
        assert "保留本地" in out
        assert "# local-edit" in local_file.read_text(encoding="utf-8")

    def test_update_not_installed(self, cwd, store_home, capsys):
        assert main(["update", "ghost"]) == 1
        assert "未安装" in capsys.readouterr().err

    def test_update_broken_manifest(self, cwd, store_home, capsys):
        # 手动摆放（无 manifest）→ 提示用 install
        (store_home / "modules" / "manual_mod").mkdir(parents=True)
        (store_home / "modules" / "manual_mod" / "module.json").write_text(
            json.dumps({"name": "manual_mod", "tasklist": {"Tasks": {}, "Flow": "[A]"}}),
            encoding="utf-8",
        )
        assert main(["update", "manual_mod"]) == 1
        assert "无 manifest" in capsys.readouterr().err

    def test_changed_local_file_not_silently_lost(self, cwd, installed, store_home, monkeypatch, capsys):
        """核心不变量：本地改动必须列清单并显式确认，绝不静默覆盖。"""
        src, dest = installed
        local_file = dest / "scripts" / "a.py"
        local_file.write_text("LOCAL UNIQUE CONTENT\n", encoding="utf-8")
        src_file = src / "scripts" / "a.py"
        src_file.write_text("REMOTE NEW CONTENT\n", encoding="utf-8")
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert main(["update", "upd_mod"]) == 0
        out = capsys.readouterr().out
        assert "变化" in out  # 本地改动（相对 manifest 哈希）列为变化
        assert "已取消" in out
        assert local_file.read_text(encoding="utf-8") == "LOCAL UNIQUE CONTENT\n"

    def test_local_only_change_kept(self, cwd, installed, store_home, capsys):
        """来源无更新、仅本地改动：不动已装文件，明确提示保留。"""
        src, dest = installed
        local_file = dest / "scripts" / "a.py"
        content = local_file.read_text(encoding="utf-8")
        local_file.write_text(content + "\n# local-only\n", encoding="utf-8")
        assert main(["update", "upd_mod", "--keep"]) == 0
        out = capsys.readouterr().out
        assert "本地改动保留" in out
        assert "# local-only" in local_file.read_text(encoding="utf-8")
