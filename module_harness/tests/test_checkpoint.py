"""AutoCheckpointStore 单元测试。"""

import json

import pytest

from module_harness.checkpoint import AutoCheckpointStore, _run_db_path


@pytest.fixture
def store(tmp_path):
    s = AutoCheckpointStore("mod_test", base_dir=tmp_path)
    yield s
    s.close()


class TestAutoCheckpointStore:
    def test_save_load_roundtrip(self, store):
        store.save("auto:tick:3", {"tick": 3, "marking": {"x": 1}})
        snap = store.load("auto:tick:3")
        assert snap == {"tick": 3, "marking": {"x": 1}}

    def test_load_missing_returns_none(self, store):
        assert store.load("nope") is None

    def test_list_sorted_by_tick(self, store):
        store.save("auto:tick:5", {"tick": 5})
        store.save("auto:tick:1", {"tick": 1})
        store.save("auto:tick:3", {"tick": 3})
        assert store.list() == [("auto:tick:1", 1), ("auto:tick:3", 3), ("auto:tick:5", 5)]

    def test_ring_keeps_newest_20(self, store):
        for t in range(25):
            store.save(f"auto:tick:{t}", {"tick": t})
        items = store.list()
        assert len(items) == 20
        # 保留最新 20 个 tick：5..24
        assert items[0] == ("auto:tick:5", 5)
        assert items[-1] == ("auto:tick:24", 24)

    def test_save_same_label_replaces(self, store):
        store.save("auto:tick:3", {"tick": 3, "v": 1})
        store.save("auto:tick:3", {"tick": 3, "v": 2})
        assert store.load("auto:tick:3") == {"tick": 3, "v": 2}

    def test_cross_instance_shares_db(self, tmp_path):
        a = AutoCheckpointStore("mod_test", base_dir=tmp_path)
        a.save("auto:tick:7", {"tick": 7})
        a.close()
        b = AutoCheckpointStore("mod_test", base_dir=tmp_path)
        assert b.load("auto:tick:7") == {"tick": 7}
        b.close()

    def test_corrupt_row_ignored(self, store, tmp_path):
        store.save("auto:tick:1", {"tick": 1})
        # 手动写一条损坏 JSON 的行
        import sqlite3
        conn = sqlite3.connect(_run_db_path("mod_test", tmp_path))
        conn.execute(
            "INSERT INTO auto_checkpoints(label, tick, snap, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("bad", 99, "{not json", 0.0),
        )
        conn.commit()
        conn.close()
        assert store.load("bad") is None
        assert store.list() == [("auto:tick:1", 1)]

    def test_module_inputs_roundtrip(self, store):
        store.save_module_inputs(
            {"alpha": 1}, {"Tasks": {"A": {"type": "harness"}}, "Flow": "A"}
        )
        assert store.load_module_inputs() == {
            "spec": {"alpha": 1},
            "tasklist": {"Tasks": {"A": {"type": "harness"}}, "Flow": "A"},
        }

    def test_module_inputs_overwrite(self, store):
        store.save_module_inputs({"v": 1}, {"Tasks": {}, "Flow": ""})
        store.save_module_inputs({"v": 2}, {"Tasks": {}, "Flow": "B"})
        assert store.load_module_inputs() == {
            "spec": {"v": 2},
            "tasklist": {"Tasks": {}, "Flow": "B"},
        }

    def test_module_inputs_missing_returns_none(self, store):
        assert store.load_module_inputs() is None

    def test_module_inputs_corrupt_ignored(self, store, tmp_path):
        store.save_module_inputs({"v": 1}, {"Tasks": {}, "Flow": ""})
        import sqlite3
        conn = sqlite3.connect(_run_db_path("mod_test", tmp_path))
        conn.execute("UPDATE module_inputs SET spec = '{not json' WHERE id = 1")
        conn.commit()
        conn.close()
        assert store.load_module_inputs() is None

    def test_save_unserializable_snap_does_not_raise(self, store):
        # datetime 不可 JSON 序列化 → TypeError，应仅 log 不阻断
        import datetime
        store.save("auto:tick:1", {"tick": 1, "when": datetime.datetime.now()})
        assert store.load("auto:tick:1") is None

    def test_save_module_inputs_unserializable_does_not_raise(self, store):
        import datetime
        store.save_module_inputs(
            {"when": datetime.datetime.now()}, {"Tasks": {}, "Flow": ""}
        )
        assert store.load_module_inputs() is None
