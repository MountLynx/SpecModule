# module_harness/tests/test_query.py
"""共享查询层：review 时间线组合（分组/去重/过滤/JSON）。"""

from __future__ import annotations

from tickflow.persistence import SqliteBackend
from tickflow.state import NodeState

from module_harness.query import (
    build_timeline,
    filter_failed,
    filter_node,
    filter_tick,
    timeline_to_dict,
)


def _seed(tmp_path, module_id="mod_x"):
    """写 4 条 firings：A@1 ok、B@1 failed、A@2 ok（含一条 (2,A) 重复）。"""
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(run_dir / "run.sqlite")
    backend.save_firing(module_id, NodeState(tick=1, node="A", output="a1"))
    backend.save_firing(
        module_id,
        NodeState(tick=1, node="B", output="b1", status="failed", error="boom"),
    )
    backend.save_firing(module_id, NodeState(tick=2, node="A", output="a2"))
    backend.save_firing(module_id, NodeState(tick=2, node="A", output="a2dup"))
    backend.close()
    return tmp_path


class TestBuildTimeline:
    def test_no_db_returns_none(self, tmp_path):
        assert build_timeline("mod_x", base_dir=tmp_path) is None

    def test_dedup_and_order(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        assert tl is not None
        assert tl.module_id == "mod_x"
        assert [(e.tick, e.node) for e in tl.entries] == [(1, "A"), (1, "B"), (2, "A")]
        assert tl.entries[1].status == "failed"
        assert tl.entries[1].error == "boom"
        assert tl.entries[2].output == "a2"      # 同 (tick,node) 保留首条
        assert tl.latest_tick == 2


class TestFilters:
    def test_filter_failed(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        failed = filter_failed(tl)
        assert [e.node for e in failed.entries] == ["B"]
        assert failed.latest_tick == 2

    def test_filter_tick(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        tick1 = filter_tick(tl, 1)
        assert [e.node for e in tick1.entries] == ["A", "B"]

    def test_filter_node(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        node_a = filter_node(tl, "A")
        assert [e.tick for e in node_a.entries] == [1, 2]


class TestTimelineToDict:
    def test_structure(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        d = timeline_to_dict(tl)
        assert d["module_id"] == "mod_x"
        assert d["latest_tick"] == 2
        assert d["entries"][0] == {
            "tick": 1, "node": "A", "status": "ok", "output": "a1", "error": None,
        }