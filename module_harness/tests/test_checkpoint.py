"""AutoCheckpointStore 与 check_resume_compat 单元测试。"""

import json

import pytest

from module_harness.checkpoint import (
    AutoCheckpointStore,
    ResumeCheck,
    _run_db_path,
    check_resume_compat,
)
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.spec import TaskDefinition, Tasklist
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.events import EventBus


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


def _graph_for(tl, module_id="mod_test"):
    """构建真实 Graph（复用 TasklistTranslator），registry 只含占位 body。"""
    reg = HarnessRegistry(llm_client=object(), event_bus=EventBus())
    reg.harness("h", HarnessConfig(
        prompt_core="p", output_format=OutputFormat(type="text"),
    ))
    reg.script("s")(lambda view: {"ok": True})
    builder = TasklistTranslator(reg, module_id)
    graph, _ = builder.build(tl)
    return graph


def _tl(tasks, flow):
    return Tasklist(
        tasks={k: TaskDefinition(**v) for k, v in tasks.items()},
        flow=flow,
    )


class TestCheckResumeCompat:
    def test_ok_when_new_nodes_reference_executed(self):
        # A 已执行；C 引用 A（已执行）+ B（未执行但拓扑上游 [A] --> B
        # B --> C）。
        # A 是 start 且有历史——但旧 flow 也是 [A]（start 未变）→ 不警告。
        old_tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\nB --> C",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert check.warnings == []

    def test_hard_error_producer_not_in_graph(self):
        tl = _tl(
            {
                "A": {"type": "script", "script": "s", "inputs": {"data": "GHOST"}},
            },
            "[A]",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes=set())
        assert any("GHOST" in e for e in check.hard_errors)

    def test_hard_error_new_start_with_history(self):
        # B 旧图不是 start（旧 flow 无 [B]），新图用裸行 [B] 声明为 start
        # 且有历史 → 硬错误
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"}},
            "[A] --> B",
        )
        tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"}},
            "[A] --> B\n[B]",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A", "B"}, old_tasklist=old_tl)
        assert any("start" in e.lower() for e in check.hard_errors)

    def test_no_hard_error_when_start_unchanged(self):
        # A 新旧图都是 start 且有历史 = 正常 resume 场景 → 不误报
        old_tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []

    def test_bracketless_flow_no_false_new_start(self):
        # 旧 flow "A" 无 [A] 标记——prepare_flow 会包成 [A]，与新 flow "[A]"
        # 是同一 tasklist → 不应误报"新成为 start"
        old_tl = _tl({"A": {"type": "script", "script": "s"}}, "A")
        tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []

    def test_start_with_history_no_archive_warns(self):
        # 无存档（old_tasklist=None）时降级为警告，不阻断
        tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"})
        assert check.hard_errors == []
        assert any("A" in w for w in check.warnings)

    def test_warning_executed_node_modified(self):
        old_tl = _tl({"A": {"type": "script", "script": "s", "promptmode": "x"}}, "[A]")
        new_tl = _tl({"A": {"type": "script", "script": "s", "promptmode": "y"}}, "[A]")
        graph = _graph_for(new_tl)
        check = check_resume_compat(new_tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert any("A" in w for w in check.warnings)

    def test_no_warning_when_executed_node_unchanged(self):
        old_tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        new_tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        graph = _graph_for(new_tl)
        check = check_resume_compat(new_tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert check.warnings == []

    def test_warning_producer_unexecuted_and_not_upstream(self):
        # B 在图中但未执行，且 flow 无 B → C 边：C 引用 B 会在运行时 Missing
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B\n[A] --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s"},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\n[A] --> C",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert any("B" in w for w in check.warnings)

    def test_no_warning_producer_unexecuted_but_topological_upstream(self):
        # B 未执行但 flow 保证先于 C 执行：[A] --> B\nB --> C
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\nB --> C",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert check.warnings == []

    def test_spec_constant_ref_skipped(self):
        # {spec.xxx} 常量引用不参与图节点校验
        tl = _tl(
            {
                "A": {"type": "script", "script": "s",
                      "inputs": {"text": "{spec.title}", "data": "A"}},
            },
            "[A]",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes=set())
        assert check.hard_errors == []

    def test_bare_constant_token_skipped(self):
        # {node} 裸常量 token 与 {spec.xxx} 一样在注册时解析为字面值，
        # 不应被硬错误 1 误判为"不在新图中"
        tl = _tl(
            {
                "A": {"type": "script", "script": "s",
                      "inputs": {"self": "{node}", "data": "A"}},
            },
            "[A]",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes=set())
        assert check.hard_errors == []

    def test_warning_new_node_in_edges_unmet(self):
        # D 是新增节点（入边 (D,B) 不在旧 marking）→ 永不 fire → 警告
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
                "D": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\nB --> C\nB --> D",
        )
        graph = _graph_for(tl)
        # 检查点 marking：A、B 已执行；C 的入边 (C,B)=True（B 刚执行完未消费），
        # D 的入边 (D,B) 不存在（新边）→ D 永不 fire
        marking_slots = {"C|B": True}
        check = check_resume_compat(
            tl, graph, executed_nodes={"A", "B"},
            old_tasklist=old_tl,
            marking_slots=marking_slots,
        )
        assert check.hard_errors == []
        assert any("D" in w for w in check.warnings)

    def test_no_warning_when_in_edge_satisfied(self):
        # C 未执行但其入边 (C,B) 在检查点已满足 → C 会执行 → 不警告
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B\nB --> C",
        )
        graph = _graph_for(tl)
        marking_slots = {"C|B": True}
        check = check_resume_compat(
            tl, graph, executed_nodes={"A", "B"},
            old_tasklist=old_tl,
            marking_slots=marking_slots,
        )
        assert check.hard_errors == []
        assert check.warnings == []

    def test_no_warning_markslot_none(self):
        # marking_slots=None 时跳过入边检查（单元测试不带 snapshot 的用法）
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"}},
            "[A] --> B",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
            },
            "[A] --> B",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert check.hard_errors == []
        assert check.warnings == []
