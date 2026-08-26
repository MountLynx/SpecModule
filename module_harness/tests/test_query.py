# module_harness/tests/test_query.py
"""共享查询层：review 时间线组合（分组/去重/过滤/JSON）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tickflow.persistence import SqliteBackend
from tickflow.state import NodeState

from module_harness.query import (
    build_timeline,
    create_checkpoint,
    filter_failed,
    filter_node,
    filter_tick,
    load_snapshot_summary,
    run_db_path,
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

    def test_db_read_error_returns_none(self, tmp_path, monkeypatch):
        _seed(tmp_path)
        from tickflow import persistence

        def boom(*args, **kwargs):
            raise RuntimeError("db locked")

        monkeypatch.setattr(persistence.SqliteBackend, "list_firings", boom)
        assert build_timeline("mod_x", base_dir=tmp_path) is None

    def test_backend_closed_on_read_error(self, tmp_path, monkeypatch):
        _seed(tmp_path)
        from tickflow import persistence

        fake = MagicMock()
        fake.list_firings.side_effect = RuntimeError("boom")
        monkeypatch.setattr(persistence, "SqliteBackend", lambda path: fake)
        assert build_timeline("mod_x", base_dir=tmp_path) is None
        fake.close.assert_called_once()

    def test_skip_empty_node_row(self, tmp_path):
        _seed(tmp_path)
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_x"
        backend = SqliteBackend(run_dir / "run.sqlite")
        backend.save_firing("mod_x", NodeState(tick=3, node=""))
        backend.close()
        tl = build_timeline("mod_x", base_dir=tmp_path)
        assert tl is not None
        assert all(e.node for e in tl.entries)
        assert tl.latest_tick == 2   # 空 node 行被跳过，不影响最新 tick


class TestFilters:
    def test_filter_failed(self, tmp_path):
        _seed(tmp_path)
        tl = build_timeline("mod_x", base_dir=tmp_path)
        failed = filter_failed(tl)
        assert [e.node for e in failed.entries] == ["B"]
        assert failed.latest_tick == 1   # 过滤子集最新 tick（B 仅 tick 1）

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


class TestRunDbPath:
    def test_path_rule(self, tmp_path):
        p = run_db_path("mod_x", base_dir=tmp_path)
        assert p == tmp_path / ".specmodule" / "runs" / "mod_x" / "run.sqlite"

    def test_default_base_is_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert run_db_path("mod_x") == tmp_path / ".specmodule" / "runs" / "mod_x" / "run.sqlite"


def _seed_snapshots(tmp_path, module_id="mod_c", ticks=(1, 2)):
    """写 N 个 tick 快照（含 fired 字段，供 checkpoint/summary 消费）。"""
    run_dir = tmp_path / ".specmodule" / "runs" / module_id
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = SqliteBackend(run_dir / "run.sqlite")
    for t in ticks:
        backend.save_snapshot(module_id, t, {"tick": t, "status": "running",
                                             "fired": [f"n{t}"], "fireable": []})
    backend.close()


class TestCreateCheckpoint:
    def test_names_latest_by_default(self, tmp_path):
        _seed_snapshots(tmp_path)
        out = create_checkpoint("mod_c", "good", base_dir=tmp_path)
        assert out == {"label": "manual:good", "tick": 2, "overwritten": False}
        backend = SqliteBackend(run_db_path("mod_c", base_dir=tmp_path))
        try:
            assert backend.list_checkpoints("mod_c") == [("manual:good", 2)]
        finally:
            backend.close()

    def test_explicit_tick_and_prefix_passthrough(self, tmp_path):
        _seed_snapshots(tmp_path)
        out = create_checkpoint("mod_c", "manual:early", tick=1, base_dir=tmp_path)
        assert out == {"label": "manual:early", "tick": 1, "overwritten": False}

    def test_overwrite_same_label(self, tmp_path):
        _seed_snapshots(tmp_path)
        create_checkpoint("mod_c", "good", base_dir=tmp_path)
        out = create_checkpoint("mod_c", "good", tick=1, base_dir=tmp_path)
        assert out["overwritten"] is True
        backend = SqliteBackend(run_db_path("mod_c", base_dir=tmp_path))
        try:
            assert backend.list_checkpoints("mod_c") == [("manual:good", 1)]
        finally:
            backend.close()

    def test_missing_run_raises(self, tmp_path):
        with pytest.raises(KeyError, match="无运行记录"):
            create_checkpoint("mod_c", "x", base_dir=tmp_path)

    def test_no_snapshots_raises(self, tmp_path):
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_c"
        run_dir.mkdir(parents=True)
        SqliteBackend(run_dir / "run.sqlite").close()
        with pytest.raises(KeyError, match="无可恢复快照"):
            create_checkpoint("mod_c", "x", base_dir=tmp_path)

    def test_missing_tick_raises_with_available(self, tmp_path):
        _seed_snapshots(tmp_path)
        with pytest.raises(KeyError, match=r"可用: \[1, 2\]"):
            create_checkpoint("mod_c", "x", tick=5, base_dir=tmp_path)


class TestLoadSnapshotSummary:
    def test_no_db_returns_none(self, tmp_path):
        assert load_snapshot_summary("mod_c", base_dir=tmp_path) is None

    def test_latest_summary(self, tmp_path):
        _seed_snapshots(tmp_path)
        _seed(tmp_path, module_id="mod_c")
        out = load_snapshot_summary("mod_c", base_dir=tmp_path)
        assert out["tick"] == 2
        assert out["status"] == "running"
        assert out["fired"] == ["n2"]
        assert out["outputs"] == {"A": "a2", "B": "b1"}
        assert "cancel_reason" not in out

    def test_explicit_tick(self, tmp_path):
        _seed_snapshots(tmp_path)
        out = load_snapshot_summary("mod_c", tick=1, base_dir=tmp_path)
        assert out["tick"] == 1
        assert out["fired"] == ["n1"]

    def test_missing_tick_raises_with_available(self, tmp_path):
        _seed_snapshots(tmp_path)
        with pytest.raises(KeyError, match=r"可用: \[1, 2\]"):
            load_snapshot_summary("mod_c", tick=9, base_dir=tmp_path)

    def test_no_snapshots_raises(self, tmp_path):
        run_dir = tmp_path / ".specmodule" / "runs" / "mod_c"
        run_dir.mkdir(parents=True)
        SqliteBackend(run_dir / "run.sqlite").close()
        with pytest.raises(KeyError, match="无可恢复快照"):
            load_snapshot_summary("mod_c", base_dir=tmp_path)

    def test_db_read_error_returns_none(self, tmp_path, monkeypatch):
        _seed_snapshots(tmp_path)
        from tickflow import persistence

        def boom(*args, **kwargs):
            raise RuntimeError("db locked")

        monkeypatch.setattr(persistence.SqliteBackend, "list_snapshots", boom)
        assert load_snapshot_summary("mod_c", base_dir=tmp_path) is None

    def test_corrupt_snapshot_raises(self, tmp_path, monkeypatch):
        _seed_snapshots(tmp_path)
        from tickflow import persistence

        monkeypatch.setattr(
            persistence.SqliteBackend, "load_snapshot", lambda *a, **k: None
        )
        with pytest.raises(KeyError, match="读取失败"):
            load_snapshot_summary("mod_c", base_dir=tmp_path)
