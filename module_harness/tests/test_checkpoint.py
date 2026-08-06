"""AutoCheckpointStore 与 check_resume_compat 单元测试。"""

import asyncio
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
from module_harness.module import Module


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


def _script_reg(mock_llm, **scripts):
    from module_harness.registry import HarnessRegistry
    from module_harness.events import EventBus
    reg = HarnessRegistry(llm_client=mock_llm, event_bus=EventBus())

    def echo(view):
        return {"ok": True}

    reg.script("echo")(echo)
    for name, fn in scripts.items():
        reg.script(name)(fn)
    return reg


def _chain_tasklist():
    """A(script) --> B(script) --> C(script) 三节点链。"""
    return Tasklist(
        tasks={
            "A": TaskDefinition(type="script", script="echo"),
            "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
            "C": TaskDefinition(type="script", script="echo", inputs={"data": "B"}),
        },
        flow="[A] --> B\nB --> C",
    )


class TestModuleSnapshotAPI:
    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, spec=None, **kw):
        monkeypatch.chdir(tmp_path)
        kw.setdefault("registry", _script_reg(mock_llm))
        return Module(
            spec={"x": 1} if spec is None else spec,
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            module_id="mod_test",
            **kw,
        )

    def test_snapshot_requires_runner(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="runner"):
            mod.snapshot()

    def test_snapshot_restore_roundtrip(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        runner = mod.build_runner()
        assert mod._runner is runner          # _build_runner_async 持有 runner

        snap = mod.snapshot()
        assert set(snap) == {"spec", "tasklist", "runner"}
        assert snap["spec"] == {"x": 1}
        assert snap["tasklist"]["Flow"] == "[A] --> B\nB --> C"
        assert "marking" in snap["runner"]

        # restore 后 spec/tasklist/runner 状态一致
        mod.restore(snap)
        assert mod.spec.to_dict() == {"x": 1}
        assert mod.tasklist.flow == "[A] --> B\nB --> C"
        assert mod._runner.tick_count == runner.tick_count

    def test_snapshot_deep_copy_independent(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        mod.build_runner()
        snap = mod.snapshot()
        snap["spec"]["x"] = 999
        snap["tasklist"]["Flow"] = "changed"
        assert mod.spec.to_dict() == {"x": 1}
        assert mod.tasklist.flow == "[A] --> B\nB --> C"

    def test_snapshot_deep_copy_nested(self, mock_llm, tmp_path, monkeypatch):
        # 嵌套 dict 也必须深拷贝：改快照不得串改 live spec
        mod = self._make_module(
            mock_llm, tmp_path, monkeypatch, spec={"x": 1, "nested": {"x": 1}},
        )
        mod.build_runner()
        snap = mod.snapshot()
        snap["spec"]["nested"]["x"] = 999
        assert mod.spec.to_dict()["nested"]["x"] == 1

    def test_checkpoint_rollback_to(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        mod.build_runner()
        mod.checkpoint("manual:start")
        assert ("manual:start", 0) in [
            (l, t) for l, t, _ in mod.list_checkpoints()
        ]
        # 手动检查点 kind 为 manual
        assert ("manual:start", 0, "manual") in mod.list_checkpoints()
        mod.rollback_to("manual:start")
        assert mod._runner.tick_count == 0

    @pytest.mark.asyncio
    async def test_restore_rewinds_executed_state(self, mock_llm, tmp_path, monkeypatch):
        # 回退到中途检查点：tick 归位、已执行状态回退、后续可继续跑
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        runner = await mod._build_runner_async()
        await runner.run_until_idle(max_ticks=2)     # A、B 已执行，C 待执行
        assert runner.tick_count == 2
        snap = mod.snapshot()
        await runner.run_until_idle(max_ticks=10)    # 跑完整个链路
        assert runner.tick_count > 2

        mod.restore(snap)
        assert mod._runner.tick_count == 2           # tick 回到快照时
        assert "C" in mod._runner.fireable()         # C 的入边仍在 marking 中
        firings = await mod._runner.run_until_idle(max_ticks=10)
        assert any(f.node == "C" for f in firings)   # 回退后继续执行 C

    def test_list_checkpoints_empty_before_run(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        mod.build_runner()
        assert mod.list_checkpoints() == []

    def test_checkpoint_without_runner_raises(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="runner"):
            mod.checkpoint("x")

    def test_checkpoint_fast_mode_raises(self, mock_llm, tmp_path, monkeypatch):
        # fast mode（persist=False，NullBackend）：检查点不可用，必须显式报错
        # （而不是静默存进内存 dict、list_checkpoints 又查不到）
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        mod.build_runner()
        with pytest.raises(RuntimeError, match="persist"):
            mod.checkpoint("x")
        with pytest.raises(RuntimeError, match="persist"):
            mod.rollback_to("x")


class TestAutoCheckpointHook:
    def _make_module(self, mock_llm, tmp_path, monkeypatch, persist=True, tasklist=None):
        monkeypatch.chdir(tmp_path)
        return Module(
            spec={"x": 1},
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            registry=_script_reg(mock_llm),
        )

    @pytest.mark.asyncio
    async def test_run_writes_auto_checkpoints(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        checkpoints = mod.list_checkpoints()
        auto = [c for c in checkpoints if c[2] == "auto"]
        # 三节点链：tick 0/1/2 各一次 firing，tick 3 空 → 自动检查点 auto:tick:0..3
        assert auto, "应有自动检查点"
        # 注：DB tick 列 = snap["tick"] = 捕获时的 tick_count（label+1）——resume
        # 语义依赖该值（计划 test_resume_mid_loop_continues_state 要求
        # auto:tick:1 的 snapshot.tick=2），故按 label 取"刚完成的 tick"断言轨迹。
        ticks = sorted(int(label.split(":")[-1]) for label, _, _ in auto)
        assert ticks == [0, 1, 2, 3]
        # Task 5 resume 依赖的精确值：auto:tick:1 的 snapshot tick == 2
        # （hook 捕获时 tick_count 已自增，DB tick 列 = label+1）
        store = AutoCheckpointStore("mod_test")
        try:
            assert store.load("auto:tick:1")["tick"] == 2
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_run_archives_module_inputs(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        store = AutoCheckpointStore("mod_test")
        inputs = store.load_module_inputs()
        store.close()
        assert inputs is not None
        assert inputs["spec"] == {"x": 1}
        assert inputs["tasklist"]["Flow"] == "[A] --> B\nB --> C"

    @pytest.mark.asyncio
    async def test_fast_mode_no_auto_checkpoints(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        await mod.run()
        assert mod.list_checkpoints() == []

    @pytest.mark.asyncio
    async def test_auto_ring_caps_at_20(self, mock_llm, tmp_path, monkeypatch):
        # 长链 25 个节点 → 25+ ticks → 环形保留 20
        tasks = {
            f"N{i}": TaskDefinition(type="script", script="echo",
                                    inputs={"data": f"N{i-1}"} if i > 0 else None)
            for i in range(25)
        }
        flow = "[N0] --> N1\n" + "\n".join(f"N{i} --> N{i+1}" for i in range(1, 24))
        tl = Tasklist(tasks=tasks, flow=flow)
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True, tasklist=tl)
        await mod.run()
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert len(auto) <= 20

    @pytest.mark.asyncio
    async def test_second_run_rehooks_auto_checkpoints(self, mock_llm, tmp_path, monkeypatch):
        # I1 回归：同一实例二次 run()（_build_runner_async 换了新 runner）必须
        # 重新注册自动检查点 hook——否则第二轮零写入（陈旧 hook 静默失效，
        # list_checkpoints() 仍显示第一轮行，无法区分新旧）。
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert len(auto) == 4          # 首轮三节点链：auto:tick:0..3
        # 换 4 节点链二次 run：新 runner 应写入 auto:tick:0..4（5 行）
        tasks = {
            f"N{i}": TaskDefinition(type="script", script="echo",
                                    inputs={"data": f"N{i-1}"} if i > 0 else None)
            for i in range(4)
        }
        mod.tasklist = Tasklist(
            tasks=tasks, flow="[N0] --> N1\nN1 --> N2\nN2 --> N3"
        )
        await mod.run()
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert len(auto) == 5
