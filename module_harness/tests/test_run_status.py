# module_harness/tests/test_run_status.py
"""运行状态查询 API：query_run_status / ModuleStatus 字段/降级路径。"""

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.status import ModuleStatus, query_run_status
from tickflow.persistence import SqliteBackend


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


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
        backend.save_snapshot("mod_x", 2, {
            "tick": 2,
            "status": "running",
            "fireable": ["B"],
            "edges": {"A": [[1, "out1"], [2, "out2"]]},
            "state": {"A": {"_prompt": "x"}},
        })
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.status == "running"
        assert st.tick == 2
        assert st.fireable == ["B"]
        assert st.outputs == {"A": "out2"}          # edges 窗口最新值
        assert st.node_states == {"A": {"_prompt": "x"}}

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
