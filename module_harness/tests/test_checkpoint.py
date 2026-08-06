"""ModuleInputStore 与 check_resume_compat 单元测试。"""

import asyncio
import json

import pytest

from module_harness.checkpoint import (
    ModuleInputStore,
    ResumeCheck,
    ResumeError,
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
    s = ModuleInputStore("mod_test", base_dir=tmp_path)
    yield s
    s.close()


class TestModuleInputStore:
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

    def test_no_warning_deep_rollback_chain(self):
        # 深回退（resume 到 auto:tick:0，A 刚执行完）：B 的入边 (B,A)=True
        # 将 fire，C 的入边 (C,B) 虽在检查点为 False（B 未执行），但 B fire
        # 后会产出——不动点模拟下 B、C 均可达 → 不得误报警告 3。
        # （旧实现只看节点自身入边在检查点的直接值，C 会被误报）
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --> C",
        )
        graph = _graph_for(tl)
        # auto:tick:0 检查点（A 刚执行完）：(B,A)=True，(C,B)=False
        check = check_resume_compat(
            tl, graph, executed_nodes={"A"}, old_tasklist=old_tl,
            marking_slots={"B|A": True, "C|B": False},
        )
        assert check.hard_errors == []
        assert check.warnings == []

    def test_no_warning_deep_rollback_guarded_edge(self):
        # guard 出边乐观传播：B --|g|--> C 的 slot 在检查点为 False（B 未
        # 执行），但 B 将 fire 并重写该 slot——乐观传播下 C 可达 → 不误报。
        # （保守处理会重新引入深回退误报；取舍说明见 _reachable_from_marking）
        def _guarded_graph(tl):
            reg = HarnessRegistry(llm_client=object(), event_bus=EventBus())
            reg.harness("h", HarnessConfig(
                prompt_core="p", output_format=OutputFormat(type="text"),
            ))
            reg.script("s")(lambda view: {"ok": True})
            reg.guard("g")(lambda view: True)
            graph, _ = TasklistTranslator(reg, "mod_test").build(tl)
            return graph

        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --|g|--> C",
        )
        tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --|g|--> C",
        )
        graph = _guarded_graph(tl)
        check = check_resume_compat(
            tl, graph, executed_nodes={"A"}, old_tasklist=old_tl,
            marking_slots={"B|A": True, "C|B": False},
        )
        assert check.hard_errors == []
        assert check.warnings == []

    def test_no_warning_armed_start_propagates(self):
        # 运行前手动检查点（armed_starts=['A']、slots 全空）：武装的 start
        # 无条件 fire 并写下游 slot（engine._join_satisfied 首分支）→ B、C
        # 经 A 的出边传播均可达 → 不误报警告 3。
        # （不并入 armed_starts 分支时 B、C 各报一条误报）
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --> C",
        )
        tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
             "C": {"type": "script", "script": "s", "inputs": {"data": "B"}}},
            "[A] --> B\nB --> C",
        )
        graph = _graph_for(tl)
        check = check_resume_compat(
            tl, graph, executed_nodes=set(), old_tasklist=old_tl,
            marking_slots={}, armed_starts=["A"],
        )
        assert check.hard_errors == []
        assert check.warnings == []

    def test_or_join_reachable_via_any(self):
        # OR join：任一入边满足即可达（engine._join_satisfied OR 分支）。
        # 同一 marking 下 AND join 不满足（需全部）→ 警告；OR 满足 → 不警告，
        # 钉住不动点模拟的 OR 分支。
        def _or_graph(tl):
            graph = _graph_for(tl)
            assert graph.nodes["C"].join == "OR"   # DSL 的 C.join: OR 声明生效
            return graph

        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> C\nB --> C\nC.join: OR",
        )
        tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> C\nB --> C\nC.join: OR",
        )
        graph = _or_graph(tl)
        marking_slots = {"C|A": False, "C|B": True}
        check = check_resume_compat(
            tl, graph, executed_nodes=set(), old_tasklist=old_tl,
            marking_slots=marking_slots,
        )
        assert check.hard_errors == []
        assert check.warnings == []
        # 对照：同一 marking 下 AND join 的 C 不可达 → 警告
        and_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> C\nB --> C",
        )
        and_graph = _graph_for(and_tl)
        assert and_graph.nodes["C"].join == "AND"
        and_check = check_resume_compat(
            and_tl, and_graph, executed_nodes=set(), old_tasklist=old_tl,
            marking_slots=marking_slots,
        )
        assert any("C" in w for w in and_check.warnings)

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
    @pytest.fixture(autouse=True)
    def _close_created_modules(self):
        """teardown 关闭本类测试创建的 Module（释放懒创建的 store 连接）。"""
        self._created_modules: list[Module] = []
        yield
        for mod in self._created_modules:
            mod.close()

    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, spec=None, **kw):
        monkeypatch.chdir(tmp_path)
        kw.setdefault("registry", _script_reg(mock_llm))
        mod = Module(
            spec={"x": 1} if spec is None else spec,
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            module_id="mod_test",
            **kw,
        )
        self._created_modules.append(mod)
        return mod

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
    @pytest.fixture(autouse=True)
    def _close_created_modules(self):
        """teardown 关闭本类测试创建的 Module（释放懒创建的 store 连接）。

        Module.close() 是显式 API；测试内实例局部创建、fixture 拿不到引用，
        由 _make_module 记录后统一在 teardown 关闭——避免 -W error 下
        unclosed database 计数随 persist 测试数量线性增长。
        """
        self._created_modules: list[Module] = []
        yield
        for mod in self._created_modules:
            mod.close()

    def _make_module(self, mock_llm, tmp_path, monkeypatch, persist=True, tasklist=None):
        monkeypatch.chdir(tmp_path)
        mod = Module(
            spec={"x": 1},
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            registry=_script_reg(mock_llm),
        )
        self._created_modules.append(mod)
        return mod

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

    @pytest.mark.asyncio
    async def test_module_close_releases_store(self, mock_llm, tmp_path, monkeypatch):
        """Module.close() 释放懒创建的 _checkpoint_store 连接；幂等。"""
        import sqlite3
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        assert mod._checkpoint_store is not None      # run() 后 store 已懒创建
        conn = mod._checkpoint_store._conn            # 实证：连接真实关闭
        mod.close()
        assert mod._checkpoint_store is None
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")                  # 已关闭的连接不可再操作
        mod.close()                                   # 幂等：重复调用不抛异常
        # close 后再 run()：store 懒重建，自动检查点照常写入
        await mod.run()
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert auto


class TestResume:
    @pytest.fixture(autouse=True)
    def _close_created_modules(self):
        """teardown 关闭本类测试创建的 Module（释放懒创建的 store 连接）。"""
        self._created_modules: list[Module] = []
        yield
        for mod in self._created_modules:
            mod.close()

    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, persist=True, spec=None):
        monkeypatch.chdir(tmp_path)
        mod = Module(
            spec=spec if spec is not None else {"x": 1},
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            registry=_script_reg(mock_llm),
        )
        self._created_modules.append(mod)
        return mod

    def _read_status(self):
        """读取 status.json（当前 cwd 下该 module 最近写入的 phase）。"""
        from pathlib import Path
        return json.loads(
            Path.cwd().joinpath(".specmodule", "runs", "mod_test", "status.json")
            .read_text(encoding="utf-8")
        )

    @pytest.mark.asyncio
    async def test_resume_continues_from_checkpoint(self, mock_llm, tmp_path, monkeypatch):
        """第一轮跑完 3 节点链；新实例微调后 resume 到 auto:tick:1。

        auto:tick:1 = B 已执行、C 未执行。resume 后只应重跑 C（用新定义）。
        """
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        assert any(c[2] == "auto" and c[1] == 1 for c in mod.list_checkpoints())

        # 新 Module 实例（模拟跨进程）：微调 C 的 prompt
        new_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
                "C": TaskDefinition(type="script", script="echo",
                                    inputs={"data": "B"}, prompt="微调后的 prompt"),
            },
            flow="[A] --> B\nB --> C",
        )
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch, tasklist=new_tl)
        firings = await mod2.resume(rollback_to="auto:tick:1")
        nodes = [f.node for f in firings]
        assert nodes == ["C"], f"resume 应只重跑 C，实际 {nodes}"
        # 运行结束状态
        from tickflow.runner import RunStatus
        assert mod2._runner.status == RunStatus.IDLE
        # resume 期间 phase 写盘（running → done），跨进程消费者可查询
        assert self._read_status()["phase"] == "done"

    @pytest.mark.asyncio
    async def test_resume_deep_rollback_no_false_warning(self, mock_llm, tmp_path, monkeypatch, caplog):
        """深回退到 auto:tick:0（A 刚执行完）：B、C 续跑，且无警告 3 误报。

        I2 回归：旧实现只查节点自身入边在检查点的 slot 值，C 的 (C,B)=False
        会被误报"不会自动执行"——实际 B 将 fire 并产出该 slot，C 正常执行。
        """
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # 注：DB tick 列 = snapshot.tick（label+1），auto:tick:0 的 tick 列是 1
        assert any(c[2] == "auto" and c[0] == "auto:tick:0" for c in mod.list_checkpoints())

        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch)
        import logging
        with caplog.at_level(logging.WARNING, logger="module_harness.module"):
            firings = await mod2.resume(rollback_to="auto:tick:0")
        assert [f.node for f in firings] == ["B", "C"]
        assert not any("不会自动执行" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_resume_preserves_executed_outputs(self, mock_llm, tmp_path, monkeypatch):
        """resume 后已执行节点的输出保留，可被新节点通过 inputs 消费。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # 新图：C 换成 record script，读取 B（已执行）的输出。
        # 注：view 键用 producer 名（view["B"]）——field 名键（view["data"]）
        # 按 graph_builder 设计恒为 Missing（graph_builder.py:88-93 注释）。
        reg = _script_reg(mock_llm, record=lambda view: {"echo": view["B"].value})
        monkeypatch.chdir(tmp_path)
        new_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
                "C": TaskDefinition(type="script", script="record", inputs={"data": "B"}),
            },
            flow="[A] --> B\nB --> C",
        )
        mod2 = Module(
            spec={"x": 1},
            tasklist=new_tl,
            llm_client=mock_llm,
            review_harness=None,
            persist=True,
            module_id="mod_test",
            registry=reg,
        )
        self._created_modules.append(mod2)   # 直接构造的实例也纳入 teardown 关闭
        firings = await mod2.resume(rollback_to="auto:tick:1")
        assert [f.node for f in firings] == ["C"]
        # C 读到的 B 输出是 resume 前已执行的结果 {"ok": True}
        assert firings[0].output == {"echo": {"ok": True}}

    @pytest.mark.asyncio
    async def test_resume_new_node_after_executed_warns(self, mock_llm, tmp_path, monkeypatch, caplog):
        """新节点挂在已执行节点之后（入边为新边）→ 警告，且该节点不执行。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        new_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
                "C": TaskDefinition(type="script", script="echo", inputs={"data": "B"}),
                "D": TaskDefinition(type="script", script="echo", inputs={"data": "B"}),
            },
            flow="[A] --> B\nB --> C\nB --> D",
        )
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch, tasklist=new_tl)
        import logging
        with caplog.at_level(logging.WARNING, logger="module_harness.module"):
            firings = await mod2.resume(rollback_to="auto:tick:2")
        assert "D" not in [f.node for f in firings]
        assert any("D" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_resume_fast_mode_raises(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        await mod.run()
        with pytest.raises(RuntimeError, match="persist=True"):
            await mod.resume(rollback_to="auto:tick:1")

    @pytest.mark.asyncio
    async def test_resume_missing_checkpoint_raises_keyerror(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        with pytest.raises(KeyError, match="nope"):
            await mod.resume(rollback_to="nope")

    @pytest.mark.asyncio
    async def test_resume_hard_error_rejects(self, mock_llm, tmp_path, monkeypatch):
        """硬错误（引用不存在 producer）→ ResumeError，runner 未被 restore。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        bad_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "Z": TaskDefinition(type="script", script="echo", inputs={"data": "GHOST"}),
            },
            flow="[A] --> Z",
        )
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch, tasklist=bad_tl)
        with pytest.raises(ResumeError, match="GHOST"):
            await mod2.resume(rollback_to="auto:tick:1")
        # runner 未被触碰：仍为构建后初始状态（tick 0）
        assert mod2._runner.tick_count == 0
        # 硬错误路径 phase 写盘为 aborted（M4：跨进程消费者不会误读 "ready"）
        status = self._read_status()
        assert status["phase"] == "aborted"
        assert "resume 兼容性校验失败" in status["error"]

    @pytest.mark.asyncio
    async def test_resume_manual_checkpoint(self, mock_llm, tmp_path, monkeypatch):
        """resume 也能回退到手动检查点（backend 表）。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        # 注：async 测试内不能用同步 build_runner()（Python 3.12+ 禁止在运行中
        # 的 loop 里再起新 loop 跑 run_until_complete），用其 async 等价形式。
        await mod._build_runner_async()
        mod.checkpoint("manual:before")
        await mod.run()
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch)
        firings = await mod2.resume(rollback_to="manual:before")
        assert [f.node for f in firings] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_resume_manual_checkpoint_no_false_warning(self, mock_llm, tmp_path, monkeypatch, caplog):
        """resume 到"运行前手动检查点"：armed start 无条件 fire 传播下游 slot。

        I2 同类残留：检查点打在建 runner 后、run 前（armed_starts=['A']、
        slots 全空）→ 实际续跑 ['A','B','C'] 全部执行；不动点模拟须并入
        armed_starts 分支，否则 B、C 被误报"不会自动执行"。
        """
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod._build_runner_async()
        mod.checkpoint("manual:before")
        await mod.run()

        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch)
        import logging
        with caplog.at_level(logging.WARNING, logger="module_harness.module"):
            firings = await mod2.resume(rollback_to="manual:before")
        assert [f.node for f in firings] == ["A", "B", "C"]
        assert not any("不会自动执行" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_resume_overwrites_module_inputs(self, mock_llm, tmp_path, monkeypatch):
        """resume 后 module_inputs 存档被新输入覆盖（后续 resume 以新 tasklist 为准）。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        new_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
                "C": TaskDefinition(type="script", script="echo",
                                    inputs={"data": "B"}, prompt="微调后的 prompt"),
            },
            flow="[A] --> B\nB --> C",
        )
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch, tasklist=new_tl)
        await mod2.resume(rollback_to="auto:tick:1")
        store = AutoCheckpointStore("mod_test")
        try:
            inputs = store.load_module_inputs()
        finally:
            store.close()
        assert inputs is not None
        assert inputs["tasklist"]["Tasks"]["C"]["prompt"] == "微调后的 prompt"


class TestResumeLoop:
    """loop（自循环）场景的检查点/resume 集成测试。

    自循环语义（README retry_loop 惯例）：counter 节点每 tick fire 一次，
    在 view.state 中计数并返回 {"n": n}；guard until3 读
    view["counter"].value["n"]（firing 节点输出在自己名下可见，
    engine._guard_view，engine.py:235-250）与 view.state
    （_NodeStateView 有 .get，engine.py:285）。
    tick0→n=1、tick1→n=2、tick2→n=3（guard n<3 在 n=3 时放行退出）。
    """

    @pytest.fixture(autouse=True)
    def _cleanup_default_registry(self):
        """测试后清理默认 registry 上注册的 until3 guard。

        _loop_module 为通过 TasklistValidator 语法预检，把 guard 注册进了
        tickflow 模块级默认 registry（单例）——这里在 teardown 时 pop，
        防止 guard 泄漏到其他测试。_guards 是 Registry 的私有 dict
        （tickflow/registry.py:42）。
        """
        yield
        from tickflow import registry as default_registry
        default_registry._guards.pop("until3", None)

    @pytest.fixture(autouse=True)
    def _close_created_modules(self):
        """teardown 关闭本类测试创建的 Module（释放懒创建的 store 连接）。"""
        self._created_modules: list[Module] = []
        yield
        for mod in self._created_modules:
            mod.close()

    def _loop_module(self, mock_llm, tmp_path, monkeypatch):
        """counter 节点自循环：n 从 0 递增，n<3 时 guard 放行继续。

        注 1：tickflow 的 guard 边语法是 ``--|guard|-->``（parser.py:67，
        不是 ``-->|guard|``）；body 输出是 {"n": n} 字典，guard 需取下标的
        "n" 再比较（dict 与 int 不能直接比较）。
        注 2：guard 需同时注册到 tickflow 模块级默认 registry ——
        TasklistValidator._check_flow 的语法预检用无 registry 的
        parse_graph()（translator.py:129），只认默认 registry 里的 guard。
        """
        monkeypatch.chdir(tmp_path)

        def counter(view):
            n = view.state.get("n", 0) + 1
            view.state["n"] = n
            return {"n": n}

        reg = _script_reg(mock_llm, counter=counter)

        def until3(view):
            return view["counter"].value["n"] < 3

        reg.guard("until3")(until3)
        from tickflow import registry as default_registry
        default_registry.guard("until3")(until3)

        tl = Tasklist(
            tasks={"counter": TaskDefinition(type="script", script="counter")},
            flow="[counter] --|until3|--> counter",
        )
        mod = Module(
            spec={"x": 1},
            tasklist=tl,
            llm_client=mock_llm,
            review_harness=None,
            persist=True,
            module_id="mod_loop",
            registry=reg,
        )
        self._created_modules.append(mod)
        return mod

    @pytest.mark.asyncio
    async def test_loop_runs_until_guard_opens(self, mock_llm, tmp_path, monkeypatch):
        mod = self._loop_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # n 从 0 递增：tick0→1, tick1→2, tick2→3（guard n<3 在 n=3 时放行退出），
        # tick3 空 → auto:tick:0..3 共 4 个
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert len(auto) == 4
        # 自循环正常终止（guard 放行退出），而非 max_ticks 截断
        from tickflow.runner import RunStatus
        assert mod._runner.status == RunStatus.IDLE

    @pytest.mark.asyncio
    async def test_resume_mid_loop_continues_state(self, mock_llm, tmp_path, monkeypatch):
        """回退到循环中途（n=2 处），重跑后 view.state 从该迭代继续。"""
        mod = self._loop_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # auto:tick:1 的 snapshot.tick=2，truncate_after(1) 保留 n=1、n=2 记录
        mod2 = self._loop_module(mock_llm, tmp_path, monkeypatch)
        firings = await mod2.resume(rollback_to="auto:tick:1")
        assert [f.node for f in firings] == ["counter"]
        # 重跑的 counter 输出应为 n=3（state 从 2 继续），而非从 1 重来
        assert firings[0].output == {"n": 3}
