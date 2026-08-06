# module_harness/tests/test_run_status.py
"""运行状态查询 API：query_run_status / ModuleStatus 字段/降级路径。"""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.client import LLMResponse
from module_harness.config import HarnessConfig
from module_harness.events import EventBus
from module_harness.graph_builder import TasklistTranslator
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist
from module_harness.status import ModuleStatus, query_run_status
from tickflow import Failure
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import SqliteBackend


def _write_status(tmp_path, module_id="mod_x", phase="running", error=None, updated_at=100.0):
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(
        json.dumps({
            "module_id": module_id, "phase": phase, "error": error,
            "updated_at": updated_at,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return run_dir


class TestQueryRunStatus:
    def test_missing_status_returns_none(self, tmp_path):
        assert query_run_status("mod_x", base_dir=tmp_path) is None

    def test_phase_only_without_db(self, tmp_path):
        _write_status(tmp_path, phase="running", updated_at=100.0)
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st is not None
        assert isinstance(st, ModuleStatus)
        assert st.module_id == "mod_x"
        assert st.phase == "running"
        assert st.status is None
        assert st.tick is None
        assert st.outputs == {}
        assert st.node_states == {}
        assert st.fireable == []
        assert st.updated_at == 100.0

    def test_full_snapshot_query(self, tmp_path):
        run_dir = _write_status(tmp_path, phase="running", updated_at=100.0)
        backend = SqliteBackend(run_dir / "run.sqlite")
        # 真实 Runner.snapshot() 形状：edges/state 嵌套在 run_state 下
        backend.save_snapshot("mod_x", 2, {
            "tick": 2,
            "marking": {},
            "run_state": {
                "edges": {"A": [[1, "out1"], [2, "out2"]]},
                "state": {"A": {"_prompt": "x"}},
            },
            "status": "running",
            "fireable": ["B"],
        })
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.status == "running"
        assert st.tick == 2
        assert st.fireable == ["B"]
        assert st.outputs == {"A": "out2"}          # run_state.edges 窗口最新值
        assert st.node_states == {"A": {"_prompt": "x"}}

    @pytest.mark.asyncio
    async def test_real_runner_snapshot_roundtrip(self, tmp_path):
        """真实 runner 快照 → query_run_status 能读到 outputs/node_states。

        回归：快照的 edges/state 嵌套在 ``run_state`` 键下，若读顶层键则
        outputs/node_states 恒为空（无 output_format 时输出为原始字符串）。
        """
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value=LLMResponse(content='{"ok": true}'))
        reg = HarnessRegistry(llm_client=mock_llm)
        reg.harness("probe", HarnessConfig(prompt_core="x={spec}"))
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="harness", harness="probe", inputs={"spec": "{spec}"})},
            flow="[A]",
        )
        builder = TasklistTranslator(reg, module_id="mod_x")
        graph, out_reg = builder.build(tl, spec=Spec({"a": 1}))
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_x"
        run_dir.mkdir(parents=True, exist_ok=True)
        backend = SqliteBackend(run_dir / "run.sqlite")
        runner = AsyncRunner(graph, registry=out_reg, backend=backend, session_id="mod_x")
        await runner.run_until_idle(max_ticks=5)   # 每 tick 内部 _persist_tick
        backend.close()

        _write_status(tmp_path, module_id="mod_x", phase="done")
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.tick is not None                 # 快照已 persist
        assert st.outputs == {"A": '{"ok": true}'}  # 读顶层 edges 会得到 {} → 捕获嵌套回归
        assert st.node_states["A"]["_llm_raw"] == '{"ok": true}'

    def test_corrupt_status_json_returns_none(self, tmp_path, caplog):
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_x"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("not json{{", encoding="utf-8")
        assert query_run_status("mod_x", base_dir=tmp_path) is None
        assert "status.json" in caplog.text

    def test_db_failure_degrades_to_phase_only(self, tmp_path, monkeypatch):
        run_dir = _write_status(tmp_path, phase="done", updated_at=1.0)
        SqliteBackend(run_dir / "run.sqlite").close()

        def boom(self, session_id):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(SqliteBackend, "latest_tick", boom)
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.phase == "done"          # 降级为 phase-only
        assert st.status is None
        assert st.tick is None


class TestModulePhase:
    """Module 阶段机：status.json 原子写。"""

    def _read_status(self, tmp_path, module_id="mod_test"):
        return json.loads(
            (tmp_path / ".specmodule" / "runs" / module_id / "status.json")
            .read_text(encoding="utf-8")
        )

    def _script_reg(self, mock_llm, **scripts):
        reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())

        def echo(view):
            return {"ok": True}

        reg.script("echo")(echo)
        for name, fn in scripts.items():
            reg.script(name)(fn)
        return reg

    def _script_tasklist(self):
        return Tasklist(
            tasks={"A": TaskDefinition(type="script", script="echo")},
            flow="[A]",
        )

    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, persist=True, **kw):
        monkeypatch.chdir(tmp_path)
        kw.setdefault("registry", self._script_reg(mock_llm))
        return Module(
            spec={"x": 1},
            tasklist=tasklist or self._script_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            **kw,
        )

    def test_init_writes_idle(self, tmp_path, monkeypatch, mock_llm):
        self._make_module(mock_llm, tmp_path, monkeypatch)
        assert self._read_status(tmp_path)["phase"] == "idle"

    def test_build_runner_writes_ready(self, tmp_path, monkeypatch, mock_llm):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        mod.build_runner()
        assert self._read_status(tmp_path)["phase"] == "ready"

    @pytest.mark.asyncio
    async def test_run_writes_done(self, tmp_path, monkeypatch, mock_llm):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        assert self._read_status(tmp_path)["phase"] == "done"

    @pytest.mark.asyncio
    async def test_phase_running_mid_run(self, tmp_path, monkeypatch, mock_llm):
        """运行中（手动 tick 循环）phase 应为 running。"""

        async def slow(view):
            await asyncio.sleep(0.2)   # 真实阻塞点：让 run() 停在 running
            return {"ok": True}

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch, persist=True,
            registry=self._script_reg(mock_llm, slow=slow),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="slow")},
                flow="[A]",
            ),
        )
        task = asyncio.create_task(mod.run())
        await asyncio.sleep(0.05)
        assert self._read_status(tmp_path)["phase"] == "running"
        await task
        assert self._read_status(tmp_path)["phase"] == "done"

    @pytest.mark.asyncio
    async def test_run_aborted_phase(self, tmp_path, monkeypatch, mock_llm):
        """基础设施 Failure → aborted + error 记录。"""

        def boom(view):
            return Failure("infra down", type="infrastructure")

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, boom=boom),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="boom")},
                flow="[A]",
            ),
        )
        await mod.run()
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert st["error"] == "aborted"

    @pytest.mark.asyncio
    async def test_persist_mode_end_to_end_query(self, tmp_path, monkeypatch, mock_llm):
        """persist=True：run 后 status.json + run.sqlite 都在，query 读全字段。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()

        st = query_run_status("mod_test", base_dir=tmp_path)
        assert st.phase == "done"
        assert st.status == "idle"        # 运行结束后 runner status
        assert st.tick is not None
        assert st.outputs == {"A": {"ok": True}}
        assert st.node_states == {"A": {}}
