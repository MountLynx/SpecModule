# module_harness/tests/test_run_history.py
"""run 历史共享层测试：list_runs（枚举）+ delete_run（删除）。

run 历史管理（查看 + 单条删除）的共享层验收：CLI ``runs``/``delete-run``
与生态消费端（Web）共用同一函数。隔离：tmp_path 造 fixture run 目录
（status.json + 按需 run.sqlite），绝不触碰真实运行产物。
"""

from __future__ import annotations

import json

from module_harness.query import delete_run, list_runs
from tickflow.persistence import SqliteBackend


def _write_status(
    tmp_path,
    run_id,
    phase="done",
    module=None,
    error=None,
    updated_at=100.0,
):
    """造最小 fixture run：status.json（phase-only），返回 run 目录。"""
    run_dir = tmp_path / ".specmodule" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "module_id": run_id, "phase": phase, "error": error,
        "updated_at": updated_at,
    }
    if module is not None:
        data["module"] = module
    (run_dir / "status.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir


class TestListRuns:
    def test_empty_when_no_runs_root(self, tmp_path):
        assert list_runs(base_dir=tmp_path) == []

    def test_empty_when_root_has_no_run_dirs(self, tmp_path):
        (tmp_path / ".specmodule" / "runs").mkdir(parents=True)
        assert list_runs(base_dir=tmp_path) == []

    def test_files_skipped(self, tmp_path):
        root = tmp_path / ".specmodule" / "runs"
        root.mkdir(parents=True)
        (root / "stray.txt").write_text("x", encoding="utf-8")
        assert list_runs(base_dir=tmp_path) == []

    def test_sorted_by_updated_at_desc(self, tmp_path):
        _write_status(tmp_path, "run_old", updated_at=100.0)
        _write_status(tmp_path, "run_new", updated_at=300.0)
        _write_status(tmp_path, "run_mid", updated_at=200.0)
        runs = list_runs(base_dir=tmp_path)
        assert [r["run_id"] for r in runs] == ["run_new", "run_mid", "run_old"]

    def test_fields_from_status_json(self, tmp_path):
        run_dir = _write_status(
            tmp_path, "run_a", phase="running", module="hello",
            error="boom", updated_at=5.0,
        )
        SqliteBackend(run_dir / "run.sqlite").close()
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs == {
            "run_id": "run_a",
            "module": "hello",
            "phase": "running",
            "tick": None,          # 空 DB 无快照 → None（轻量语义）
            "error": "boom",
            "updated_at": 5.0,
            "has_sqlite": True,
        }

    def test_module_missing_is_none(self, tmp_path):
        """旧格式 status.json 无 module 键 → None（消费端回落启发式）。"""
        _write_status(tmp_path, "run_a", module=None)
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs["module"] is None

    def test_no_sqlite_has_sqlite_false(self, tmp_path):
        _write_status(tmp_path, "run_a")   # 只有 status.json（失败 run 形态）
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs["has_sqlite"] is False
        assert runs["tick"] is None

    def test_tick_from_latest_snapshot(self, tmp_path):
        """无 status.json tick 键 → latest_tick 单条查询近似。"""
        run_dir = _write_status(tmp_path, "run_t", phase="running")
        backend = SqliteBackend(run_dir / "run.sqlite")
        backend.save_snapshot("run_t", 3, {
            "tick": 3, "marking": {}, "run_state": {"keep_records": True},
            "status": "running", "fireable": [], "fired": [],
        })
        backend.close()
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs["tick"] == 3

    def test_corrupt_status_included_as_unknown(self, tmp_path):
        """status.json 损坏 → phase=unknown 收入不跳过（删除入口要可用）。"""
        run_dir = tmp_path / ".specmodule" / "runs" / "bad_run"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("not json{{", encoding="utf-8")
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs["run_id"] == "bad_run"
        assert runs["phase"] == "unknown"
        assert runs["module"] is None
        assert runs["error"] is None
        assert runs["updated_at"] == 0.0   # 排序沉底
        assert runs["has_sqlite"] is False

    def test_missing_status_included_as_unknown(self, tmp_path):
        (tmp_path / ".specmodule" / "runs" / "bare").mkdir(parents=True)
        (runs,) = list_runs(base_dir=tmp_path)
        assert runs["run_id"] == "bare"
        assert runs["phase"] == "unknown"

    def test_corrupt_sorts_last(self, tmp_path):
        """损坏 run（updated_at=0.0）排在正常 run 之后。"""
        _write_status(tmp_path, "run_ok", updated_at=1.0)
        bad = tmp_path / ".specmodule" / "runs" / "run_bad"
        bad.mkdir(parents=True)
        (bad / "status.json").write_text("{{", encoding="utf-8")
        runs = list_runs(base_dir=tmp_path)
        assert [r["run_id"] for r in runs] == ["run_ok", "run_bad"]


class TestDeleteRun:
    def test_deletes_whole_tree(self, tmp_path):
        run_dir = _write_status(tmp_path, "run_a")
        SqliteBackend(run_dir / "run.sqlite").close()
        (run_dir / "stream.log").write_text("x", encoding="utf-8")
        assert delete_run("run_a", base_dir=tmp_path) is True
        assert not run_dir.exists()

    def test_missing_dir_returns_false(self, tmp_path):
        (tmp_path / ".specmodule" / "runs").mkdir(parents=True)
        assert delete_run("ghost", base_dir=tmp_path) is False

    def test_missing_runs_root_returns_false(self, tmp_path):
        assert delete_run("ghost", base_dir=tmp_path) is False

    def test_path_traversal_rejected(self, tmp_path):
        """分隔符 / ``..`` / ``.`` / 空串 → False（runs 根与邻目录不动）。"""
        root = tmp_path / ".specmodule" / "runs"
        root.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        for bad in ("../victim.txt", "..\\victim.txt", "a/../b", "..", ".", ""):
            assert delete_run(bad, base_dir=tmp_path) is False, bad
        assert victim.exists()
        assert root.exists()

    def test_drive_and_absolute_forms_rejected(self, tmp_path):
        """盘符/绝对路径形态（pathlib join 整路径替换）→ False。"""
        root = tmp_path / ".specmodule" / "runs"
        root.mkdir(parents=True)
        for bad in ("C:foo", "C:/evil", "D:x", "/etc", "\\evil"):
            assert delete_run(bad, base_dir=tmp_path) is False, bad
        assert root.exists()
