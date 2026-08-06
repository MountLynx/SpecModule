"""最小快照（S1/S3）与 restore 等价性 + SqliteBackend.latest_firings 测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tickflow.ir import Edge, Graph, InputPolicy, Node
from tickflow.persistence import SqliteBackend
from tickflow.registry import Registry
from tickflow.runner import Runner
from tickflow.state import NodeState, RunState


class TestToSnapshotDataMinimal:
    def test_minimal_keeps_only_keep_records(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1,
                            mutable_state={"k": "v"}))
        data = rs.to_snapshot_data(include_records=False, minimal=True)
        assert set(data) == {"keep_records"}
        assert data["keep_records"] is True

    def test_default_still_full(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1,
                            mutable_state={"k": "v"}))
        data = rs.to_snapshot_data()
        assert set(data) == {"edges", "fire_counts", "state", "keep_records", "records"}

    def test_include_records_false_strips_records_only(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1))
        data = rs.to_snapshot_data(include_records=False)
        assert "records" not in data
        assert "edges" in data

    def test_keep_records_false_no_records(self):
        rs = RunState(keep_records=False)
        rs.record(NodeState(tick=0, node="A", output=1))
        data = rs.to_snapshot_data()
        assert "records" not in data


def _chain_graph() -> Graph:
    """A --> B --> C 三节点链（B、C 声明输入）。"""
    g = Graph(
        nodes={
            "A": Node(name="A", is_start=True),
            "B": Node(name="B", body="echo"),
            "C": Node(name="C"),
        },
        edges=[Edge("A", "B", None), Edge("B", "C", None)],
    )
    g.nodes["B"].inputs = {"A": InputPolicy.latest()}
    g.nodes["C"].inputs = {"B": InputPolicy.latest()}
    return g


def _chain_registry() -> Registry:
    reg = Registry()
    reg.body("echo")(lambda view: {"ok": True})
    return reg


class TestMinimalSnapshotRoundtrip:
    @pytest.fixture
    def runner(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.sqlite")
        r = Runner(_chain_graph(), _chain_registry(), backend=backend, session_id="s")
        yield r
        backend.close()

    def test_persist_tick_writes_minimal_snapshot(self, runner):
        runner.tick()                       # A 执行
        runner.tick()                       # B 执行
        rows = []
        conn = sqlite3.connect(str(runner._backend._db_path))
        try:
            for (data,) in conn.execute("SELECT data FROM snapshots"):
                rows.append(json.loads(data))
        finally:
            conn.close()
        assert [r["tick"] for r in rows] == [1, 2]
        snap = rows[-1]
        assert set(snap["run_state"]) == {"keep_records"}
        assert snap["fired"] == ["B"]
        assert "records" not in snap["run_state"]
        assert "edges" not in snap["run_state"]

    def test_restore_minimal_snapshot_continues_correctly(self, runner):
        runner.tick()                       # tick 1: A
        runner.tick()                       # tick 2: B
        snap = runner.snapshot(include_records=False, minimal=True)
        snap["fired"] = ["B"]
        assert runner.tick_count == 2
        runner.run_until_idle(max_ticks=10) # 跑完 C
        assert runner.tick_count == 4       # tick 3: C，tick 4 空

        # 新 runner（同 backend/session）restore 到 tick 2 → 只重跑 C
        r2 = Runner(_chain_graph(), _chain_registry(),
                    backend=runner._backend, session_id="s")
        r2.restore(snap)
        assert r2.tick_count == 2
        assert "C" in r2.fireable()
        # 窗口/计数/state 已从 firings 重建：B 的输出可被 C 解析
        firings = r2.run_until_idle(max_ticks=10)
        assert [f.node for f in firings] == ["C"]
        assert firings[0].inputs["B"] == {"ok": True}

    def test_restore_legacy_full_snapshot_still_works(self, runner):
        runner.tick()
        snap = runner.snapshot()            # 旧格式全量快照（含 edges/state）
        r2 = Runner(_chain_graph(), _chain_registry(),
                    backend=runner._backend, session_id="s")
        r2.restore(snap)
        assert r2.tick_count == 1
        assert "B" in r2.fireable()


class TestLatestFirings:
    def test_last_firing_per_node_dedup_replay(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.sqlite")
        try:
            # A 两次 firing + B 一次；再模拟 restore-replay 写入同 (tick,node)
            for ns in [
                NodeState(tick=1, node="A", output="a1"),
                NodeState(tick=2, node="A", output="a2"),
                NodeState(tick=2, node="B", output="b1"),
            ]:
                backend.save_firing("s", ns)
            replay = NodeState(tick=2, node="A", output="a2_replay")
            backend.save_firing("s", replay)   # 重放重复行：keep-first
            rows = backend.latest_firings("s")
            by_node = {d["node"]: d for d in rows}
            assert set(by_node) == {"A", "B"}
            assert by_node["A"]["output"] == "a2"      # 最新 tick + keep-first
            assert by_node["B"]["output"] == "b1"
        finally:
            backend.close()

    def test_empty_session(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.sqlite")
        try:
            assert backend.latest_firings("s") == []
        finally:
            backend.close()
