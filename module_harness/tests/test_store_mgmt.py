# module_harness/tests/test_store_mgmt.py
"""CLI 管理面测试（module-user-store 4.x）：install/list/info/uninstall/setup。

隔离：monkeypatch SPECMODULE_HOME 到临时目录；setup 用 monkeypatch 的
``input`` 驱动交互。
"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main
from module_harness.submodule import SubModule, script
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist


class MgmtMod(SubModule):
    """管理测试用模块：单 script 节点。"""

    name = "mgmt_mod"
    version = "2.0.0"
    description = "mgmt test module"
    spec_schema = SpecSchema(input={"x": "str"})
    tasklist = Tasklist(
        tasks={"A": TaskDefinition(type="script", script="a")},
        flow="[A]",
    )

    @script("a")
    def a(view):
        return {"out": "mgmt"}


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
def pack_src(tmp_path):
    out = tmp_path / "pack_src"
    MgmtMod().pack(out)
    return out


class TestInstall:
    def test_install_local_pack(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        out = capsys.readouterr().out
        assert "已安装: mgmt_mod" in out
        dest = store_home / "modules" / "mgmt_mod"
        assert (dest / "module.json").is_file()
        manifest = json.loads(
            (store_home / "manifests" / "mgmt_mod.json").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "mgmt_mod"
        assert manifest["version"] == "2.0.0"
        assert manifest["source"] == str(pack_src)
        assert "module.json" in manifest["files"]  # 每文件 sha256
        assert "installed_at" in manifest

    def test_install_invalid_dir_zero_write(self, cwd, store_home, capsys):
        # 非 pack 目录：校验失败，store 零落盘
        bad = cwd / "bad"
        bad.mkdir()
        (bad / "random.txt").write_text("x", encoding="utf-8")
        assert main(["install", str(bad)]) == 1
        assert "缺少 module.json" in capsys.readouterr().err
        assert not (store_home / "modules").exists()
        assert not (store_home / "manifests").exists()

    def test_install_broken_manifest(self, cwd, store_home, capsys):
        bad = cwd / "bad2"
        bad.mkdir()
        (bad / "module.json").write_text("{not json", encoding="utf-8")
        assert main(["install", str(bad)]) == 1
        assert "解析失败" in capsys.readouterr().err
        assert not (store_home / "modules").exists()

    def test_install_same_name_conflict(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        capsys.readouterr()
        assert main(["install", str(pack_src)]) == 1
        assert "已存在" in capsys.readouterr().err

    def test_install_missing_source(self, cwd, store_home, capsys):
        assert main(["install", str(cwd / "ghost")]) == 1
        assert "不存在" in capsys.readouterr().err


class TestListInfoUninstall:
    def test_list_after_install(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        capsys.readouterr()
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "mgmt_mod" in out
        assert "2.0.0" in out
        assert "packed" in out

    def test_list_json(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        capsys.readouterr()
        assert main(["list", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert any(m["name"] == "mgmt_mod" and m["kind"] == "packed" for m in data)

    def test_info_shows_manifest(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        capsys.readouterr()
        assert main(["info", "mgmt_mod"]) == 0
        out = capsys.readouterr().out
        assert "mgmt_mod" in out
        assert "来源" in out and "安装时间" in out

    def test_info_not_found(self, cwd, store_home, capsys):
        assert main(["info", "ghost"]) == 1
        assert "未找到" in capsys.readouterr().err

    def test_uninstall_removes_both(self, cwd, pack_src, store_home, capsys):
        assert main(["install", str(pack_src)]) == 0
        capsys.readouterr()
        assert main(["uninstall", "mgmt_mod"]) == 0
        assert "已卸载" in capsys.readouterr().out
        assert not (store_home / "modules" / "mgmt_mod").exists()
        assert not (store_home / "manifests" / "mgmt_mod.json").exists()

    def test_uninstall_missing(self, cwd, store_home, capsys):
        assert main(["uninstall", "ghost"]) == 1
        assert "未安装" in capsys.readouterr().err


class TestSetup:
    def _setup_inputs(self, monkeypatch, answers):
        monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))

    def test_setup_writes_store_config(self, cwd, store_home, monkeypatch, capsys):
        self._setup_inputs(monkeypatch, [
            "openai",           # provider（首次无现有配置，无覆盖询问）
            "gpt-4o-mini",      # model
            "OPENAI_API_KEY",   # key env
            "sk-test-123",      # key value
            "",                 # base_url 留空
        ])
        assert main(["setup"]) == 0
        env = store_home / ".env"
        cfg = store_home / "config.json"
        assert env.is_file()
        assert "OPENAI_API_KEY=sk-test-123" in env.read_text(encoding="utf-8")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["providers"][0]["sdktype"] == "openai"
        assert data["providers"][0]["api_key_env"] == "OPENAI_API_KEY"
        assert "gpt-4o-mini" in [m["name"] for m in data["models"]]

    def test_setup_decline_keeps_existing(self, cwd, store_home, monkeypatch, capsys):
        # 预置现有配置 → setup 询问覆盖 → 拒绝 → 保留
        store_home.mkdir(parents=True, exist_ok=True)
        (store_home / ".env").write_text("OPENAI_API_KEY=old-key\n", encoding="utf-8")
        (store_home / "config.json").write_text(json.dumps({
            "providers": [{"name": "openai", "sdktype": "openai",
                           "api_key_env": "OPENAI_API_KEY"}],
            "models": [],
        }), encoding="utf-8")
        self._setup_inputs(monkeypatch, ["n"])
        assert main(["setup"]) == 0
        assert "保留现有配置" in capsys.readouterr().out
        assert "old-key" in (store_home / ".env").read_text(encoding="utf-8")

    def test_setup_confirm_overwrites(self, cwd, store_home, monkeypatch, capsys):
        store_home.mkdir(parents=True, exist_ok=True)
        (store_home / ".env").write_text("OPENAI_API_KEY=old-key\n", encoding="utf-8")
        (store_home / "config.json").write_text(json.dumps({
            "providers": [{"name": "openai", "sdktype": "openai",
                           "api_key_env": "OPENAI_API_KEY"}],
            "models": [],
        }), encoding="utf-8")
        self._setup_inputs(monkeypatch, [
            "y", "openai", "gpt-4o-mini", "OPENAI_API_KEY", "new-key", "",
        ])
        assert main(["setup"]) == 0
        assert "new-key" in (store_home / ".env").read_text(encoding="utf-8")
