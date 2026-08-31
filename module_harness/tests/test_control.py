# module_harness/tests/test_control.py
"""跨进程运行控制：控制文件协议 + tick 边界 hook + Module 接线 + CLI 命令。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.cli import main
from module_harness.control import (
    clear_control,
    control_path,
    control_tick_start,
    read_control,
    request_control,
)
from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import Tasklist


# ------------------------------------------------------------------
# 控制文件协议
# ------------------------------------------------------------------


class TestControlFileProtocol:
    def test_request_read_roundtrip(self, tmp_path):
        req = request_control("m1", "cancel", reason="用户取消", base_dir=tmp_path)
        assert req["action"] == "cancel"
        assert req["reason"] == "用户取消"
        assert isinstance(req["requested_at"], float)
        got = read_control("m1", base_dir=tmp_path)
        assert got == req

    def test_read_missing_returns_none(self, tmp_path):
        assert read_control("ghost", base_dir=tmp_path) is None

    def test_read_corrupt_returns_none(self, tmp_path):
        p = control_path("m1", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{broken", encoding="utf-8")
        assert read_control("m1", base_dir=tmp_path) is None

    def test_read_invalid_action_returns_none(self, tmp_path):
        p = control_path("m1", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"action": "explode"}), encoding="utf-8")
        assert read_control("m1", base_dir=tmp_path) is None

    def test_request_invalid_action_raises(self, tmp_path):
        with pytest.raises(ValueError, match="未知控制动作"):
            request_control("m1", "explode", base_dir=tmp_path)

    def test_clear(self, tmp_path):
        request_control("m1", "pause", base_dir=tmp_path)
        clear_control("m1", base_dir=tmp_path)
        assert read_control("m1", base_dir=tmp_path) is None
        clear_control("m1", base_dir=tmp_path)  # 幂等：缺失不报错


# ------------------------------------------------------------------
# tick 边界 hook
# ------------------------------------------------------------------


def _fake_runner():
    runner = MagicMock()
    runner.cancel = MagicMock()
    return runner


class TestControlTickStart:
    def test_no_request_noop(self, tmp_path):
        cb = control_tick_start(_fake_runner(), "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))  # 无文件：立即返回，不报错

    def test_cancel_consumes_request(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "cancel", reason="停", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))
        runner.cancel.assert_called_once_with("停")
        assert read_control("m1", base_dir=tmp_path) is None  # 消费即删

    def test_cancel_default_reason(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "cancel", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))
        runner.cancel.assert_called_once_with("cancelled")

    def test_pause_holds_until_unpause(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "pause", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path, poll=0.01)

        async def scenario():
            task = asyncio.create_task(cb(0, []))
            await asyncio.sleep(0.05)
            assert not task.done()          # 挂起中：tick 不放行
            assert runner.cancel.call_count == 0
            request_control("m1", "unpause", base_dir=tmp_path)
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(scenario())
        runner.cancel.assert_not_called()
        assert read_control("m1", base_dir=tmp_path) is None  # unpause 消费即删

    def test_pause_released_by_cancel(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "pause", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path, poll=0.01)

        async def scenario():
            task = asyncio.create_task(cb(0, []))
            await asyncio.sleep(0.05)
            assert not task.done()
            request_control("m1", "cancel", reason="停", base_dir=tmp_path)
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(scenario())
        runner.cancel.assert_called_once_with("停")
        assert read_control("m1", base_dir=tmp_path) is None


# ------------------------------------------------------------------
# Module 接线
# ------------------------------------------------------------------


def _mini_module(tmp_path, module_id, **kw):
    """最小可运行 Module：单 script 节点（零 LLM），tasklist 直通通道。"""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock()
    reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus.null())

    @reg.script("noop")
    def noop(view):
        return {"ok": True}

    tl = Tasklist.from_json({
        "Tasks": {"A": {"type": "script", "script": "noop"}},
        "Flow": "[A]",
    })
    return Module(
        spec={},
        tasklist=tl,
        llm_client=mock_llm,
        registry=reg,
        review_harness=None,
        module_id=module_id,
        base_dir=tmp_path,
        **kw,
    )


class TestModuleControlWiring:
    def test_default_registers_hook(self, tmp_path):
        mod = _mini_module(tmp_path, "ctl_on")
        runner = mod.build_runner()
        assert len(runner._tick_start_hooks) == 1
        mod.close()

    def test_control_false_disables(self, tmp_path):
        mod = _mini_module(tmp_path, "ctl_off", control=False)
        runner = mod.build_runner()
        assert len(runner._tick_start_hooks) == 0
        mod.close()

    def test_run_clears_stale_control(self, tmp_path):
        """新执行清场：run 前残留的 pause 请求被作废，运行正常完成。"""
        mod = _mini_module(tmp_path, "ctl_stale")
        request_control("ctl_stale", "pause", base_dir=tmp_path)
        asyncio.run(mod.run())
        assert read_control("ctl_stale", base_dir=tmp_path) is None
        mod.close()


# ------------------------------------------------------------------
# CLI cancel/pause/unpause
# ------------------------------------------------------------------


class TestControlCli:
    def _seed_run(self, tmp_path, run_id="cli_run"):
        run_dir = tmp_path / ".specmodule" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "status.json").write_text(
            json.dumps({"module_id": run_id, "phase": "running",
                        "error": None, "updated_at": 1.0}),
            encoding="utf-8",
        )

    def test_cancel_writes_control(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._seed_run(tmp_path)
        assert main(["cancel", "--run-id", "cli_run", "--reason", "手动"]) == 0
        got = read_control("cli_run", base_dir=tmp_path)
        assert got is not None and got["action"] == "cancel"
        assert got["reason"] == "手动"

    def test_pause_and_unpause(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._seed_run(tmp_path)
        assert main(["pause", "--run-id", "cli_run"]) == 0
        assert read_control("cli_run", base_dir=tmp_path)["action"] == "pause"
        assert main(["unpause", "--run-id", "cli_run"]) == 0
        assert read_control("cli_run", base_dir=tmp_path)["action"] == "unpause"

    def test_unknown_run_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["cancel", "--run-id", "ghost"]) == 1
        assert read_control("ghost", base_dir=tmp_path) is None
