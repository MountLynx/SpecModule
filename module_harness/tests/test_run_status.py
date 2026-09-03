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


def _write_status(tmp_path, module_id="mod_x", phase="running", error=None, updated_at=100.0, module=None):
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "module_id": module_id, "phase": phase, "error": error,
        "updated_at": updated_at,
    }
    if module is not None:
        data["module"] = module
    (run_dir / "status.json").write_text(
        json.dumps(data, ensure_ascii=False),
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
        """最小快照（无 edges/state）+ firings 行 → outputs/node_states 从 firings 读。"""
        run_dir = _write_status(tmp_path, phase="running", updated_at=100.0)
        backend = SqliteBackend(run_dir / "run.sqlite")
        from tickflow.state import NodeState
        backend.save_snapshot("mod_x", 2, {
            "tick": 2,
            "marking": {},
            "run_state": {"keep_records": True},
            "status": "running",
            "fireable": ["B"],
            "fired": ["A"],
        })
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="out1",
                                               mutable_state={"_prompt": "x"}))
        backend.save_firing("mod_x", NodeState(tick=2, node="A", output="out2",
                                               mutable_state={"_prompt": "x"}))
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.status == "running"
        assert st.tick == 2
        assert st.fireable == ["B"]
        assert st.fired == ["A"]
        assert st.outputs == {"A": "out2"}          # 每节点最后一 firing
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
        # 最新快照是空 tick（A 跑完后 tick 1 无 fireable）→ fired 为空，
        # 语义正确（fired = 该快照 tick 刚完成的节点列表）
        assert st.fired == []
        assert st.outputs == {"A": '{"ok": true}'}
        assert st.node_states["A"]["_llm_raw"] == '{"ok": true}'

    def test_corrupt_status_json_returns_none(self, tmp_path, caplog):
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_x"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("not json{{", encoding="utf-8")
        assert query_run_status("mod_x", base_dir=tmp_path) is None
        assert "status.json" in caplog.text

    def test_module_field_read(self, tmp_path):
        """新格式 status.json 带 module 键 → ModuleStatus.module 读出。"""
        _write_status(tmp_path, module="hello")
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.module == "hello"

    def test_module_field_old_format_none(self, tmp_path):
        """旧格式 status.json 无 module 键 → None（向后兼容）。"""
        _write_status(tmp_path)
        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.module is None

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

    def test_replayed_firings_keep_first(self, tmp_path):
        """restore-then-replay 的重复行在 latest_firings 中 keep-first。"""
        run_dir = _write_status(tmp_path, phase="done")
        backend = SqliteBackend(run_dir / "run.sqlite")
        from tickflow.state import NodeState
        backend.save_snapshot("mod_x", 2, {
            "tick": 2, "marking": {}, "run_state": {"keep_records": True},
            "status": "idle", "fireable": [], "fired": ["A"],
        })
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="orig"))
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="replay"))
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.outputs == {"A": "orig"}


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

    def test_module_field_written(self, tmp_path, monkeypatch, mock_llm):
        """module= 传入 → status.json "module" 键记录源模块名（溯源）。"""
        self._make_module(mock_llm, tmp_path, monkeypatch, module="hello")
        assert self._read_status(tmp_path)["module"] == "hello"

    def test_module_field_none_when_not_passed(self, tmp_path, monkeypatch, mock_llm):
        """直构 Module 未传 module → 键为 null（消费端回落启发式）。"""
        self._make_module(mock_llm, tmp_path, monkeypatch)
        assert self._read_status(tmp_path)["module"] is None

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
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if self._read_status(tmp_path)["phase"] == "running":
                break
            await asyncio.sleep(0.01)
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

    def test_status_file_false_no_residue(self, tmp_path, monkeypatch, mock_llm):
        """status_file=False：不写 status.json（零残留）。"""
        self._make_module(mock_llm, tmp_path, monkeypatch, status_file=False)
        assert not (tmp_path / ".specmodule").exists()

    @pytest.mark.asyncio
    async def test_persist_false_status_file_true_phase_only(self, tmp_path, monkeypatch, mock_llm):
        """persist=False + status_file=True：只写 status.json，phase 可查、tick 降级。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        await mod.run()
        assert self._read_status(tmp_path)["phase"] == "done"
        st = query_run_status("mod_test", base_dir=tmp_path)
        assert st.phase == "done"
        assert st.tick is None          # 无 DB
        assert st.outputs == {}
        assert not (tmp_path / ".specmodule" / "runs" / "mod_test" / "run.sqlite").exists()

    @pytest.mark.asyncio
    async def test_run_exception_writes_aborted(self, tmp_path, monkeypatch, mock_llm):
        """body 抛普通异常（非 Failure）→ phase=aborted + error 记录，异常继续传播。"""

        def boom(view):
            raise RuntimeError("boom")

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, boom=boom),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="boom")},
                flow="[A]",
            ),
        )
        with pytest.raises(RuntimeError, match="boom"):
            await mod.run()
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert st["error"] == "boom"

    @pytest.mark.asyncio
    async def test_build_failure_writes_aborted(self, tmp_path, monkeypatch, mock_llm):
        """构建失败（tasklist 引用了未注册 harness）→ phase=aborted + error。"""
        monkeypatch.chdir(tmp_path)
        mod = Module(
            spec={"x": 1},
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="harness", harness="nope")},
                flow="[A]",
            ),
            llm_client=mock_llm,
            registry=self._script_reg(mock_llm),
            review_harness=None,
            persist=False,
            module_id="mod_test",
        )
        with pytest.raises(ValueError):
            await mod.run()
        st = self._read_status(tmp_path)
        assert st["phase"] == "aborted"
        assert "nope" in st["error"]

    @pytest.mark.asyncio
    async def test_max_ticks_cutoff_truncated(self, tmp_path, monkeypatch, mock_llm):
        """max_ticks 截断（status 仍 RUNNING）→ 终态 phase=truncated，error 带上限。"""

        def echo(view):
            return {"ok": True}

        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, echo=echo),
            tasklist=Tasklist(
                tasks={"A": TaskDefinition(type="script", script="echo")},
                flow="[A]",
            ),
        )
        # 单节点 + max_ticks=1：tick 0 跑 A 后 tick_count=1 >= max_ticks，
        # run_until_idle 退出但 status 仍 RUNNING → 截断终态（可 resume 续跑）
        await mod.run(max_ticks=1)
        st = self._read_status(tmp_path)
        assert st["phase"] == "truncated"
        assert "max_ticks=1" in st["error"]

    @pytest.mark.asyncio
    async def test_truncated_then_resume_done(self, tmp_path, monkeypatch, mock_llm):
        """truncated 是可恢复终态：截断 → 新 Module resume 续跑 → done。"""

        def echo(view):
            return {"ok": True}

        tasklist = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="echo")},
            flow="[A]",
        )
        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, echo=echo),
            tasklist=tasklist,
        )
        await mod.run(max_ticks=1)
        assert self._read_status(tmp_path)["phase"] == "truncated"

        mod2 = self._make_module(
            mock_llm, tmp_path, monkeypatch,
            registry=self._script_reg(mock_llm, echo=echo),
            tasklist=tasklist,
        )
        await mod2.resume()
        assert self._read_status(tmp_path)["phase"] == "done"
