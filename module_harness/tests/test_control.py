# module_harness/tests/test_control.py
"""跨进程运行控制：控制文件协议 + tick 边界 hook + Module 接线 + CLI 命令。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.cli import main
from module_harness.infra.control import (
    clear_control,
    control_path,
    control_tick_end,
    control_tick_start,
    read_control,
    request_control,
)
from module_harness.infra.events import EventBus
from module_harness.model.module import Module
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import Tasklist


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
# tick 边界 hook（cancel 在 tick_end 消费；pause 在 tick_start 挂起）
# ------------------------------------------------------------------


def _fake_runner():
    runner = MagicMock()
    runner.cancel = MagicMock()
    return runner


class TestControlTickEnd:
    """cancel 消费点：tick 末尾（引擎状态赋值之后，终态不被冲掉）。"""

    def test_no_request_noop(self, tmp_path):
        cb = control_tick_end(_fake_runner(), "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))  # 无文件：立即返回，不报错

    def test_cancel_consumes_request(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "cancel", reason="停", base_dir=tmp_path)
        cb = control_tick_end(runner, "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))
        runner.cancel.assert_called_once_with("停")
        assert read_control("m1", base_dir=tmp_path) is None  # 消费即删

    def test_cancel_default_reason(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "cancel", base_dir=tmp_path)
        cb = control_tick_end(runner, "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))
        runner.cancel.assert_called_once_with("cancelled")

    def test_tick_start_ignores_cancel(self, tmp_path):
        """tick_start 不消费 cancel（留给 tick_end，见终态冲掉说明）。"""
        runner = _fake_runner()
        request_control("m1", "cancel", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))
        runner.cancel.assert_not_called()
        got = read_control("m1", base_dir=tmp_path)
        assert got is not None and got["action"] == "cancel"  # 文件原样保留


class TestControlTickStart:
    def test_no_request_noop(self, tmp_path):
        cb = control_tick_start(_fake_runner(), "m1", base_dir=tmp_path)
        asyncio.run(cb(0, []))  # 无文件：立即返回，不报错

    def test_pause_holds_until_unpause(self, tmp_path):
        runner = _fake_runner()
        request_control("m1", "pause", base_dir=tmp_path)
        cb = control_tick_start(runner, "m1", base_dir=tmp_path, poll=0.01)

        async def scenario():
            task = asyncio.create_task(cb(0, []))
            await asyncio.sleep(0.05)
            assert not task.done()          # 挂起中：tick 不放行
            request_control("m1", "unpause", base_dir=tmp_path)
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(scenario())
        runner.cancel.assert_not_called()
        assert read_control("m1", base_dir=tmp_path) is None  # unpause 消费即删

    def test_pause_released_by_cancel_leaves_file(self, tmp_path):
        """挂起中见到 cancel：放行但不消费（tick_end 统一消费，终态不被冲掉）。"""
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
        runner.cancel.assert_not_called()    # tick_start 不调 cancel
        got = read_control("m1", base_dir=tmp_path)
        assert got is not None and got["action"] == "cancel"  # 留给 tick_end


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


def _mini_loop_module(tmp_path, module_id, **kw):
    """可循环 Module：A→B→A 带 guard（零 LLM、零 sleep），供 cancel 回归。"""
    mock_llm = MagicMock()
    mock_llm.complete = AsyncMock()
    reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus.null())

    @reg.script("A")
    def a(view):
        return {"value": "from A"}

    @reg.script("B")
    def b(view):
        return {"greeting": "hello " + view.field("value")["value"]}

    @reg.guard("g")
    def g(view):
        return True

    tl = Tasklist.from_json({
        "Tasks": {
            "A": {"type": "script", "script": "A"},
            "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
        },
        "Flow": "[A] --> B\nB --|g|--> A",
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
    def test_default_registers_hooks(self, tmp_path):
        mod = _mini_module(tmp_path, "ctl_on")
        runner = mod.build_runner()
        assert len(runner._tick_start_hooks) == 1   # pause hook
        assert len(runner._tick_end_hooks) == 1     # cancel hook
        mod.close()

    def test_control_false_disables(self, tmp_path):
        mod = _mini_module(tmp_path, "ctl_off", control=False)
        runner = mod.build_runner()
        assert len(runner._tick_start_hooks) == 0
        assert len(runner._tick_end_hooks) == 0
        mod.close()

    def test_run_clears_stale_control(self, tmp_path):
        """新执行清场：run 前残留的 pause 请求被作废，运行正常完成。"""
        mod = _mini_module(tmp_path, "ctl_stale")
        request_control("ctl_stale", "pause", base_dir=tmp_path)
        asyncio.run(mod.run())
        assert read_control("ctl_stale", base_dir=tmp_path) is None
        mod.close()

    def test_real_run_cancels_mid_run(self, tmp_path):
        """回归（真实 AsyncRunner）：tick 期写入的 cancel 必须真正停机。

        引擎每 tick 末尾无条件重写 runner.status——若 cancel 在 tick_start
        消费，CANCELLED 会被同 tick 赋值冲掉，循环跑到 max_ticks（E2E 实测
        缺陷）。tick_end 消费点对此免疫。
        """
        def request_cancel_at_tick2(tick, fireable):
            if tick == 2:
                request_control("ctl_cancel", "cancel", reason="回归", base_dir=tmp_path)

        mod = _mini_loop_module(
            tmp_path, "ctl_cancel", hooks={"on_tick_start": request_cancel_at_tick2}
        )
        asyncio.run(mod.run(max_ticks=100))
        st = json.loads(
            (tmp_path / ".specmodule" / "runs" / "ctl_cancel" / "status.json")
            .read_text(encoding="utf-8")
        )
        assert st["phase"] == "cancelled"
        assert st["error"] == "回归"
        assert read_control("ctl_cancel", base_dir=tmp_path) is None
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
