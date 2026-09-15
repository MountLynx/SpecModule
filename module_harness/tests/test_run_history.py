# module_harness/tests/test_run_history.py
"""run 历史共享层测试：list_runs（枚举）+ delete_run（删除）。

run 历史管理（查看 + 单条删除）的共享层验收：CLI ``runs``/``delete-run``
与生态消费端（Web）共用同一函数。隔离：tmp_path 造 fixture run 目录
（status.json + 按需 run.sqlite），绝不触碰真实运行产物。
"""

from __future__ import annotations

import json

from module_harness.infra.query import delete_run, list_runs, recent_runs
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

    def test_tick_key_in_status_takes_priority(self, tmp_path):
        """status.json 携带 tick 键时优先（前瞻兼容），不查 sqlite。"""
        run_dir = _write_status(tmp_path, "run_k", phase="running")
        data = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
        data["tick"] = 7
        (run_dir / "status.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        backend = SqliteBackend(run_dir / "run.sqlite")
        backend.save_snapshot("run_k", 3, {
            "tick": 3, "marking": {}, "run_state": {"keep_records": True},
            "status": "running", "fireable": [], "fired": [],
        })
        backend.close()
        (row,) = list_runs(base_dir=tmp_path)
        assert row["tick"] == 7   # status.json 键优先，sqlite 里的 3 不生效

    def test_latest_tick_corrupt_db_is_none(self, tmp_path):
        """_latest_tick_light 容错：坏 db → tick=None，列表照常返回该 run。"""
        run_dir = tmp_path / ".specmodule" / "runs" / "run_bad"
        run_dir.mkdir(parents=True)
        (run_dir / "run.sqlite").write_text("not a db{{", encoding="utf-8")
        _write_status(tmp_path, "run_bad", updated_at=1.0)
        (row,) = list_runs(base_dir=tmp_path)
        assert row["run_id"] == "run_bad"
        assert row["tick"] is None


class TestRecentRuns:
    def test_empty_when_no_runs_root(self, tmp_path):
        assert recent_runs(base_dir=tmp_path) == {"runs": [], "total": 0}

    def test_files_skipped(self, tmp_path):
        root = tmp_path / ".specmodule" / "runs"
        root.mkdir(parents=True)
        (root / "stray.txt").write_text("x", encoding="utf-8")
        assert recent_runs(base_dir=tmp_path) == {"runs": [], "total": 0}

    def test_limit_and_total(self, tmp_path):
        """尾部只计不展开；total = 全量 run 目录数。"""
        for i in range(5):
            _write_status(tmp_path, f"run_{i}", updated_at=100.0 + i)
        out = recent_runs(base_dir=tmp_path, limit=2)
        assert out["total"] == 5
        assert [r["run_id"] for r in out["runs"]] == ["run_4", "run_3"]

    def test_mtime_sort_desc_tie_by_name(self, tmp_path):
        """按 status.json mtime 降序（与 updated_at 字段值无关）；同值按名降序。"""
        import os
        import time
        _write_status(tmp_path, "run_a", updated_at=1.0)
        _write_status(tmp_path, "run_b", updated_at=2.0)
        _write_status(tmp_path, "run_c", updated_at=3.0)
        now = time.time()
        for run_id, age in (("run_a", 600), ("run_b", 60), ("run_c", 600)):
            p = tmp_path / ".specmodule" / "runs" / run_id / "status.json"
            os.utime(p, (now - age, now - age))
        out = recent_runs(base_dir=tmp_path)
        # run_b mtime 最新居首；run_a/run_c 同 mtime 按名降序 → run_c 先
        assert [r["run_id"] for r in out["runs"]] == ["run_b", "run_c", "run_a"]

    def test_tick_no_sqlite_fallback(self, tmp_path):
        """recent_runs 不做 sqlite tick 近似：无 status.json tick 键 → None。"""
        run_dir = _write_status(tmp_path, "run_t", phase="running")
        backend = SqliteBackend(run_dir / "run.sqlite")
        backend.save_snapshot("run_t", 3, {
            "tick": 3, "marking": {}, "run_state": {"keep_records": True},
            "status": "running", "fireable": [], "fired": [],
        })
        backend.close()
        (row,) = recent_runs(base_dir=tmp_path)["runs"]
        assert row["tick"] is None
        # 全量语义不变：list_runs 仍近似出 3
        assert list_runs(base_dir=tmp_path)[0]["tick"] == 3

    def test_corrupt_status_in_top_n_included_as_unknown(self, tmp_path):
        """status.json 损坏的 run 在前 N 条内 → phase=unknown 收入不跳过。"""
        run_dir = tmp_path / ".specmodule" / "runs" / "bad_run"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("{{", encoding="utf-8")
        out = recent_runs(base_dir=tmp_path)
        assert out["total"] == 1
        assert out["runs"][0]["run_id"] == "bad_run"
        assert out["runs"][0]["phase"] == "unknown"

    def test_missing_status_falls_back_to_dir_mtime(self, tmp_path):
        """空 run 目录（无 status.json）→ 计入 total、phase=unknown、按目录自身 mtime 排序。"""
        import os
        import time
        run_dir = _write_status(tmp_path, "run_ok")
        bare = tmp_path / ".specmodule" / "runs" / "run_bare"
        bare.mkdir(parents=True)
        now = time.time()
        os.utime(run_dir / "status.json", (now - 600, now - 600))
        out = recent_runs(base_dir=tmp_path)
        # run_ok 锚定 status.json mtime（now-600）；bare 无 status.json → 目录 mtime（刚创建，更新近）
        assert out["total"] == 2
        assert [r["run_id"] for r in out["runs"]] == ["run_bare", "run_ok"]
        assert out["runs"][0]["phase"] == "unknown"


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
