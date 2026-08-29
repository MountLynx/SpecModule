# module_harness/tests/test_graph_query.py
"""共享查询层：build_run_graph / graph_to_dict（运行图重建 + 序列化）。"""

from __future__ import annotations

import json

from tickflow import parse as parse_graph

from module_harness.query import build_run_graph, graph_to_dict
from module_harness.spec import Tasklist


def _mini_tasklist() -> Tasklist:
    return Tasklist.from_json({
        "Tasks": {
            "A": {"type": "script", "script": "A"},
            "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
            "C": {"type": "submodule", "submodule": "child"},
        },
        "Flow": "[A] --> B\nA --|pick|--> C",
    })


class TestGraphToDict:
    def test_shape(self):
        # registry=None 时 guard 校验会炸（_validate 查 has_guard），这里只测
        # 无 guard 图的序列化；带 guard 的经 build_run_graph 覆盖（Task 2）。
        tl = Tasklist.from_json({
            "Tasks": {
                "A": {"type": "script", "script": "A"},
                "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
            },
            "Flow": "[A] --> B",
        })
        g = parse_graph("[A] --> B", registry=None)
        d = graph_to_dict(g, tl)
        assert d["starts"] == ["A"]
        assert d["nodes"] == [
            {"id": "A", "label": "A", "type": "script", "is_start": True,
             "join": "AND", "inputs": {}},
            {"id": "B", "label": "B", "type": "script", "is_start": False,
             "join": "AND", "inputs": {"value": "A"}},
        ]
        assert d["edges"] == [{"from": "A", "to": "B", "guard": None}]


MINI_MODULE_PY = '''\
"""graph_query 测试模块：script 流水线 + guard 分支。"""
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

    @reg.script("C")
    def c(view):
        return {"note": "guarded"}

    @reg.guard("pick_c")
    def pick_c(view):
        return True

    return reg


entry = ModuleEntry(
    name="graph_mini",
    description="graph_query 测试模块",
    templates={},
    build_registry=_registry_for,
)
'''

MINI_TASKLIST_JSON = {
    "Tasks": {
        "A": {"type": "script", "script": "A"},
        "B": {"type": "script", "script": "B", "inputs": {"value": "A"}},
        "C": {"type": "script", "script": "C"},
    },
    "Flow": "[A] --> B\nA --|pick_c|--> C",
}


import pytest


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    """测试模块目录进 SPECMODULE_PATH（store 统一搜索路径发现），base=tmp_path。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPECMODULE_HOME", str(tmp_path / "home"))
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "graph_mini.py").write_text(MINI_MODULE_PY, encoding="utf-8")
    monkeypatch.setenv("SPECMODULE_PATH", str(mods))
    monkeypatch.delenv("SPECMODULE_MODULES", raising=False)
    return tmp_path


def _seed_archive(base, run_id, spec=None, tasklist=None):
    from module_harness.checkpoint import ModuleInputStore

    st = ModuleInputStore(run_id, base)
    st.save_module_inputs(spec or {}, tasklist or MINI_TASKLIST_JSON)
    st.close()


class TestBuildRunGraph:
    def test_from_archive(self, mini_env):
        _seed_archive(mini_env, "graph_mini")
        res = build_run_graph("graph_mini", "graph_mini", base_dir=mini_env)
        assert res is not None
        graph, tl = res
        assert sorted(graph.nodes) == ["A", "B", "C"]
        assert ("A", "C", "pick_c") in [(e.src, e.dst, e.guard) for e in graph.edges]
        assert tl.tasks["B"].inputs == {"value": "A"}

    def test_tasklist_dict_channel(self, mini_env):
        res = build_run_graph(
            "graph_mini", "graph_mini",
            base_dir=mini_env, tasklist=MINI_TASKLIST_JSON,
        )
        assert res is not None
        assert sorted(res[0].nodes) == ["A", "B", "C"]

    def test_no_archive_returns_none(self, mini_env):
        assert build_run_graph("graph_mini", "graph_mini", base_dir=mini_env) is None

    def test_module_not_found_raises(self, mini_env):
        with pytest.raises(ValueError, match="未找到"):
            build_run_graph("ghost", "ghost", base_dir=mini_env)

    def test_graph_to_dict_via_build(self, mini_env):
        from module_harness.query import graph_to_dict as g2d

        _seed_archive(mini_env, "graph_mini")
        graph, tl = build_run_graph("graph_mini", "graph_mini", base_dir=mini_env)
        d = g2d(graph, tl)
        types = {n["id"]: n["type"] for n in d["nodes"]}
        assert types == {"A": "script", "B": "script", "C": "script"}
        guards = [e["guard"] for e in d["edges"]]
        assert "pick_c" in guards and None in guards
