"""query_value：运行中 dict 细粒度查询（MCP peek 工具库侧实现）。"""

import json

import pytest

from module_harness.query import QueryValueResult, query_value
from tickflow.persistence import SqliteBackend
from tickflow.state import NodeState


def _seed_run(tmp_path, module_id="mod_x"):
    """构造真实可查的运行库：status.json + 快照 + firings（同 test_run_status 模式）。"""
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(json.dumps({
        "module_id": module_id, "phase": "running", "error": None,
        "updated_at": 100.0,
    }, ensure_ascii=False), encoding="utf-8")
    backend = SqliteBackend(run_dir / "run.sqlite")
    backend.save_snapshot(module_id, 3, {
        "tick": 3, "marking": {}, "run_state": {},
        "status": "running", "fireable": [], "fired": ["A"],
    })
    backend.save_firing(module_id, NodeState(
        tick=3, node="A",
        output={"summary": "hello", "sections": [{"t": 1}, {"t": 2}]},
        mutable_state={"_llm_raw": '{"summary": "hello"}'},
    ))
    backend.close()
    return run_dir


class TestQueryValue:
    def test_missing_run_returns_none(self, tmp_path):
        assert query_value("mod_nope", "phase", base_dir=tmp_path) is None

    def test_top_level_fields(self, tmp_path):
        _seed_run(tmp_path)
        res = query_value("mod_x", "phase", base_dir=tmp_path)
        assert res == QueryValueResult(tick=3, value="running")
        assert query_value("mod_x", "tick", base_dir=tmp_path).value == 3
        assert query_value("mod_x", "fireable", base_dir=tmp_path).value == []

    def test_outputs_nested_and_list_index(self, tmp_path):
        _seed_run(tmp_path)
        res = query_value("mod_x", "outputs.A.summary", base_dir=tmp_path)
        assert res.found and res.value == "hello"
        res = query_value("mod_x", "outputs.A.sections.1.t", base_dir=tmp_path)
        assert res.found and res.value == 2

    def test_state_path_includes_llm_raw(self, tmp_path):
        _seed_run(tmp_path)
        res = query_value("mod_x", "state.A._llm_raw", base_dir=tmp_path)
        assert res.found and res.value == '{"summary": "hello"}'

    def test_missing_path_reports_available(self, tmp_path):
        _seed_run(tmp_path)
        res = query_value("mod_x", "outputs.A.nope", base_dir=tmp_path)
        assert not res.found and res.available == ["sections", "summary"]
        res = query_value("mod_x", "bogus.x", base_dir=tmp_path)
        assert not res.found
        assert sorted(res.available) == [
            "error", "fireable", "fired", "outputs", "phase",
            "state", "status", "tick", "updated_at",
        ]

    def test_list_index_out_of_range(self, tmp_path):
        _seed_run(tmp_path)
        res = query_value("mod_x", "outputs.A.sections.9", base_dir=tmp_path)
        assert not res.found and res.available == ["0", "1"]

    def test_empty_path_raises_value_error(self, tmp_path):
        _seed_run(tmp_path)
        with pytest.raises(ValueError):
            query_value("mod_x", "", base_dir=tmp_path)
