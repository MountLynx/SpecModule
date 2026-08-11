# module_harness/tests/test_cli.py
"""specmodule CLI 测试：run / status / review 命令路径（hello/fail 测试模块）。"""

from __future__ import annotations

import json

import pytest

from module_harness.cli import main

HELLO_PY = '''\
"""hello 测试模块：单 script 节点 Greet（无 LLM 依赖）。"""
from __future__ import annotations

from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Greet")
    def greet(view):
        return {"greeting": "hello world"}

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
            "Flow": "[Greet]",
        }

    return reg


entry = ModuleEntry(
    name="hello",
    description="hello 测试模块",
    templates={
        "hello": {
            "name": "hello",
            "description": "hello 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            },
        },
    },
    build_registry=_registry_for,
    default_spec={"name": "world"},
    default_template="hello",
    review_harness=None,
)
'''

FAIL_PY = '''\
"""fail 测试模块：单 script 节点 Boom 返回 Failure（type=llm，运行继续）。"""
from __future__ import annotations

from tickflow import Failure
from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("Boom")
    def boom(view):
        return Failure("boom failed", type="llm")

    @reg.script("tl")
    def tl(view):
        return {
            "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
            "Flow": "[Boom]",
        }

    return reg


entry = ModuleEntry(
    name="fail",
    description="fail 测试模块",
    templates={
        "fail": {
            "name": "fail",
            "description": "fail 模板",
            "translation": {"type": "script", "script": "tl"},
            "tasklist": {
                "Tasks": {"Boom": {"type": "script", "script": "Boom"}},
                "Flow": "[Boom]",
            },
        },
    },
    build_registry=_registry_for,
    default_template="fail",
    review_harness=None,
)
'''


@pytest.fixture
def modules_dir(tmp_path):
    d = tmp_path / "modules"
    d.mkdir()
    (d / "hello.py").write_text(HELLO_PY, encoding="utf-8")
    (d / "fail.py").write_text(FAIL_PY, encoding="utf-8")
    return d


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(cwd, *argv):
    """run 子命令 + 指向测试模块目录。"""
    return main(["run", *argv, "--modules-dir", str(cwd / "modules")])


class TestRun:
    def test_hello_success(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        out = capsys.readouterr().out
        assert "运行完成" in out
        assert "hello world" in out
        assert (cwd / ".specmodule" / "runs" / "hello" / "run.sqlite").exists()

    def test_default_spec(self, cwd, modules_dir, capsys):
        # 无 --spec/--spec-file，走 entry.default_spec
        assert _run(cwd, "--module", "hello", "--mock") == 0

    def test_module_not_found(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "nope", "--mock") == 1
        assert "未找到" in capsys.readouterr().err

    def test_missing_spec(self, cwd, modules_dir, capsys):
        # fail 无 default_spec 也不传 spec → 报错
        assert _run(cwd, "--module", "fail", "--mock") == 1
        assert "缺少 spec" in capsys.readouterr().err

    def test_tasklist_template_mutually_exclusive(self, cwd, modules_dir, capsys):
        assert _run(
            cwd, "--module", "hello", "--mock",
            "--template", "hello", "--tasklist", "x.json",
        ) == 1
        assert "互斥" in capsys.readouterr().err

    def test_verbose3(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock", "--verbose", "3") == 0
        out = capsys.readouterr().out
        assert "═══" in out
        assert "[ok]" in out

    def test_tasklist_file(self, cwd, modules_dir, capsys):
        tl = cwd / "tl.json"
        tl.write_text(
            json.dumps({
                "Tasks": {"Greet": {"type": "script", "script": "Greet"}},
                "Flow": "[Greet]",
            }),
            encoding="utf-8",
        )
        assert _run(cwd, "--module", "hello", "--mock", "--tasklist", str(tl)) == 0
        assert "运行完成" in capsys.readouterr().out


class TestStatus:
    def test_status_text(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello"]) == 0
        assert "phase=done" in capsys.readouterr().out

    def test_status_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["status", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["phase"] == "done"

    def test_status_no_run(self, cwd, capsys):
        assert main(["status", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err


class TestReview:
    def test_review_timeline(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello"]) == 0
        out = capsys.readouterr().out
        assert "Greet ✓" in out
        assert "最新 tick" in out

    def test_review_json(self, cwd, modules_dir, capsys):
        assert _run(cwd, "--module", "hello", "--mock") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "hello", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["module_id"] == "hello"
        assert data["entries"][0]["node"] == "Greet"

    def test_review_failed(self, cwd, modules_dir, capsys):
        # fail 无 default_spec——显式传 spec 让运行进入（Boom 失败路径）
        assert _run(cwd, "--module", "fail", "--mock", "--spec", "{}") == 0
        capsys.readouterr()
        assert main(["review", "--run-id", "fail", "--failed"]) == 0
        out = capsys.readouterr().out
        assert "Boom ✗" in out
        assert "boom failed" in out

    def test_review_no_run(self, cwd, capsys):
        assert main(["review", "--run-id", "ghost"]) == 1
        assert "无运行记录" in capsys.readouterr().err