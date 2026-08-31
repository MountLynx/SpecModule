# module_harness/tests/test_resume_preflight.py
"""query.check_resume_compat_from_run：从运行产物做恢复预检（不 spawn）。"""

from __future__ import annotations

import pytest

from module_harness.query import check_resume_compat_from_run

MINI_TASKLIST_JSON = {
    "Tasks": {
        "A": {"type": "script", "script": "A"},
        "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
        "C": {"type": "script", "script": "C"},
    },
    "Flow": "[A] --> B\nA --|pick_c|--> C",
}

MINI_MODULE_PY = '''\
"""resume_preflight 测试模块：script 流水线 + guard 分支。"""
from __future__ import annotations

from module_harness.entry import ModuleEntry
from module_harness.events import EventBus
from module_harness.registry import HarnessRegistry


def _registry_for(llm_client, template_name, event_bus):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus or EventBus.null())

    @reg.script("A")
    def a(view):
        return {"value": "from A"}

    @reg.script("B")
    def b(view):
        return {"greeting": "hello"}

    @reg.script("B_v2")
    def b_v2(view):
        return {"greeting": "hello v2"}

    @reg.script("C")
    def c(view):
        return {"note": "guarded"}

    @reg.guard("pick_c")
    def pick_c(view):
        return True

    return reg


entry = ModuleEntry(
    name="preflight_mini",
    description="resume_preflight 测试模块",
    templates={},
    build_registry=_registry_for,
)
'''


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    """测试模块目录进 SPECMODULE_PATH，base=tmp_path（同 test_graph_query 范式）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPECMODULE_HOME", str(tmp_path / "home"))
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "preflight_mini.py").write_text(MINI_MODULE_PY, encoding="utf-8")
    monkeypatch.setenv("SPECMODULE_PATH", str(mods))
    monkeypatch.delenv("SPECMODULE_MODULES", raising=False)
    return tmp_path


def _seed_run(base, run_id="preflight_mini", *, tasklist=None, firings=None,
              snapshots=None, inputs=True):
    """run.sqlite（firings/snapshots/checkpoints）+ module_inputs 存档。"""
    from module_harness.checkpoint import ModuleInputStore
    from tickflow.persistence import SqliteBackend
    from tickflow.state import NodeState

    backend = SqliteBackend(base / ".specmodule" / "runs" / run_id / "run.sqlite")
    for f in firings if firings is not None else [
        {"tick": 0, "node": "A", "output": "a1"},
    ]:
        backend.save_firing(run_id, NodeState(**f))
    for tick, snap in (snapshots if snapshots is not None else {
        1: {"tick": 1, "status": "running", "fireable": ["B"], "fired": ["A"],
            "marking": {"slots": {"B|A": True}, "armed_starts": ["A"]}},
    }).items():
        backend.save_snapshot(run_id, tick, snap)
    backend.close()
    if inputs:
        st = ModuleInputStore(run_id, base)
        st.save_module_inputs({"topic": "demo"}, tasklist or MINI_TASKLIST_JSON)
        st.close()


class TestCheckResumeCompatFromRun:
    def test_default_target_clean(self, mini_env):
        """缺省（最新快照续跑、归档 tasklist）：硬错误为空，材料齐全。"""
        _seed_run(mini_env)
        d = check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         base_dir=mini_env)
        assert d is not None
        assert d["hard_errors"] == []
        assert d["target"] == "1" and d["target_tick"] == 1
        assert d["executed_nodes"] == ["A"]

    def test_manual_target_resolves(self, mini_env):
        _seed_run(mini_env)
        backend_store(mini_env, "preflight_mini", "manual:cp1")  # 打手动检查点
        d = check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         target="manual:cp1", base_dir=mini_env)
        assert d is not None
        assert d["target"] == "manual:cp1" and d["target_tick"] == 1

    def test_bad_target_is_hard_error_not_raise(self, mini_env):
        _seed_run(mini_env)
        d = check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         target="nope", base_dir=mini_env)
        assert d is not None
        assert d["target"] is None and d["target_tick"] is None
        assert any("不存在" in e for e in d["hard_errors"])

    def test_unknown_producer_hard_error(self, mini_env):
        """新 tasklist 引用图中不存在的 producer → 硬错误 1。"""
        _seed_run(mini_env)
        bad = {
            "Tasks": {
                "A": {"type": "script", "script": "A"},
                "B": {"type": "script", "script": "B", "inputs": {"value": "Z"}},
            },
            "Flow": "[A] --> B",
        }
        d = check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         new_tasklist=bad, base_dir=mini_env)
        assert d is not None
        assert any("不在新图中" in e for e in d["hard_errors"])

    def test_modified_executed_node_warns(self, mini_env):
        """已执行节点定义被改 → 警告 1（对比 module_inputs 存档）。"""
        _seed_run(mini_env, firings=[
            {"tick": 0, "node": "A", "output": "a1"},
            {"tick": 0, "node": "B", "output": "b1"},
        ])
        modified = {
            "Tasks": {
                "A": {"type": "script", "script": "A"},
                "B": {"type": "script", "script": "B_v2", "inputs": {"value": "A"}},
                "C": {"type": "script", "script": "C"},
            },
            "Flow": "[A] --> B\nA --|pick_c|--> C",
        }
        d = check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         new_tasklist=modified, base_dir=mini_env)
        assert d is not None
        assert d["hard_errors"] == []
        assert any("被修改" in w for w in d["warnings"])

    def test_no_db_returns_none(self, mini_env):
        assert check_resume_compat_from_run(
            "preflight_mini", "preflight_mini", base_dir=mini_env) is None

    def test_bad_tasklist_raises_valueerror(self, mini_env):
        _seed_run(mini_env)
        with pytest.raises(ValueError):
            check_resume_compat_from_run("preflight_mini", "preflight_mini",
                                         new_tasklist={"bogus": True},
                                         base_dir=mini_env)

    def test_module_unresolved_raises_valueerror(self, mini_env):
        _seed_run(mini_env)
        with pytest.raises(ValueError, match="未找到"):
            check_resume_compat_from_run("ghost", "preflight_mini",
                                         base_dir=mini_env)


def backend_store(base, run_id, label):
    """给最新快照打手动检查点（测试助手）。"""
    from module_harness.query import create_checkpoint

    create_checkpoint(run_id, label.removeprefix("manual:"), base_dir=base)
