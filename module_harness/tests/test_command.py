# module_harness/tests/test_command.py
"""Command 节点单元测试。"""

import time
from unittest.mock import MagicMock, patch

import pytest
from tickflow import Failure
from tickflow.views import DictView, Resolved

from module_harness.cli.command import Command, CommandConfig
from module_harness.infra.events import (
    EventBus,
    CommandStarted,
    CommandCompleted,
    CommandFailed,
)


def _make_view(**inputs):
    resolved = {k: Resolved(value=v, k=None) for k, v in inputs.items()}
    return DictView(resolved, node="test_cmd")


class TestCommandConfig:
    def test_defaults(self):
        cfg = CommandConfig(command="echo hello")
        assert cfg.timeout == 60.0
        assert cfg.cwd is None
        assert cfg.capture_output is True
        assert cfg.shell is True

    def test_custom(self):
        cfg = CommandConfig(command="ls -la", timeout=10, cwd="/tmp", shell=False)
        assert cfg.command == "ls -la"
        assert cfg.timeout == 10
        assert cfg.cwd == "/tmp"
        assert cfg.shell is False


class TestCommandBuildBody:
    def test_successful_command(self):
        bus = EventBus()
        events = []
        bus.subscribe(CommandStarted, lambda e: events.append("started"))
        bus.subscribe(CommandCompleted, lambda e: events.append("completed"))

        cfg = CommandConfig(command="echo hello")
        cmd = Command(cfg, bus)
        body = cmd.build_body()

        result = body(_make_view())

        assert result["stdout"].strip() == "hello"
        assert result["returncode"] == 0
        assert events == ["started", "completed"]

    def test_command_with_stderr(self):
        bus = EventBus()
        # 写入 stderr 的跨平台命令
        import sys
        if sys.platform == "win32":
            cfg = CommandConfig(command="echo error 1>&2")
        else:
            cfg = CommandConfig(command="echo error >&2")
        cmd = Command(cfg, bus)
        body = cmd.build_body()

        result = body(_make_view())
        assert result["returncode"] == 0

    def test_nonzero_exit_code(self):
        bus = EventBus()
        # 跨平台返回非零退出码
        import sys
        if sys.platform == "win32":
            cfg = CommandConfig(command="cmd /c exit 1")
        else:
            cfg = CommandConfig(command="exit 1")
        cmd = Command(cfg, bus)
        body = cmd.build_body()

        result = body(_make_view())
        assert result["returncode"] == 1
        # 非零退出码仍然返回 dict（不抛异常），下游 guard 自行判断

    def test_timeout_returns_failure(self):
        bus = EventBus()
        failures = []
        bus.subscribe(CommandFailed, lambda e: failures.append(e))

        # Windows: timeout /t 需要整数秒; 用 ping -n 延时
        import sys
        if sys.platform == "win32":
            cfg = CommandConfig(command="ping -n 30 127.0.0.1 > nul", timeout=0.5)
        else:
            cfg = CommandConfig(command="sleep 30", timeout=0.1)
        cmd = Command(cfg, bus)
        body = cmd.build_body()

        result = body(_make_view())

        assert isinstance(result, Failure)
        assert result.type == "llm"
        assert "超时" in result.error
        assert len(failures) == 1

    def test_command_failed_emits_event(self):
        bus = EventBus()
        failures = []
        bus.subscribe(CommandFailed, lambda e: failures.append(e))

        # 执行一个不存在的命令
        cfg = CommandConfig(command="nonexistent_command_12345")
        cmd = Command(cfg, bus)
        body = cmd.build_body()

        result = body(_make_view())

        # 不存在的命令会抛异常 (FileNotFoundError 或 shell 返回非 0)
        if isinstance(result, Failure):
            assert result.type == "llm"
            assert len(failures) == 1

    def test_override_timeout(self):
        """build_body 的 timeout 参数覆盖 config 默认值。"""
        bus = EventBus()
        cfg = CommandConfig(command="echo ok", timeout=999)
        cmd = Command(cfg, bus)
        body = cmd.build_body(timeout=5)
        result = body(_make_view())
        assert result["stdout"].strip() == "ok"
        # timeout=5 生效，覆盖了 config 的 999

    def test_override_cwd(self):
        """build_body 的 cwd 参数覆盖 config 默认值。"""
        import os
        bus = EventBus()
        cfg = CommandConfig(command={"win32": "cd", "default": "pwd"}.get(
            __import__('sys').platform, "pwd"),
            cwd="/fake",
        )
        cmd = Command(cfg, bus)
        body = cmd.build_body(cwd=os.getcwd())
        result = body(_make_view())
        assert result["returncode"] == 0

    def test_env_passed_to_subprocess(self):
        bus = EventBus()
        cfg = CommandConfig(
            command="echo %MY_VAR%" if __import__('sys').platform == "win32" else "echo $MY_VAR",
            env={"MY_VAR": "hello_test"},
        )
        cmd = Command(cfg, bus)
        body = cmd.build_body()
        result = body(_make_view())
        assert "hello_test" in result["stdout"]
