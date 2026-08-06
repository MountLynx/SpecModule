# module_harness/tests/test_run_status.py
"""运行状态查询 API：query_run_status / ModuleStatus 字段/降级路径。"""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm.client import LLMResponse
from module_harness.config import HarnessConfig
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.spec import Spec, TaskDefinition, Tasklist
from module_harness.status import ModuleStatus, query_run_status
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
