# module_harness/tests/test_tickflow_redundancy.py
"""tickflow 重复计算清理的等价性测试：ir 邻接索引 / checker 分支缓存 / fireable 透传。"""

from __future__ import annotations

from tickflow.checker import check
from tickflow.engine import _join_satisfied, bootstrap, tick
from tickflow.ir import Edge, Graph, InputPolicy, Node


def _graph() -> Graph:
    """2 个 XOR-splitter + 3 个 AND-join：覆盖分支缓存与索引路径。

    S1 的 g1/g2 两条不同 guard 分支 → M1（A∈g1、B∈g2）死锁建议；
    A、C 同在 g1 分支 → M2 无建议（同分支不互斥）。
    """
    return Graph(
        nodes={
            "S1": Node(name="S1", is_start=True),
            "A": Node(name="A"), "B": Node(name="B"), "C": Node(name="C"),
            "M1": Node(name="M1"), "M2": Node(name="M2"),
            "S2": Node(name="S2", is_start=True),
            "D": Node(name="D"), "E": Node(name="E"), "M3": Node(name="M3"),
        },
        edges=[
            Edge("S1", "A", "g1"), Edge("S1", "B", "g2"), Edge("S1", "C", "g1"),
            Edge("A", "M1", None), Edge("B", "M1", None),
            Edge("A", "M2", None), Edge("C", "M2", None),
            Edge("S2", "D", "g3"), Edge("S2", "E", "g4"),
            Edge("D", "M3", None), Edge("E", "M3", None),
        ],
    )


def _naive_producers(g: Graph, node: str) -> list[str]:
    return sorted({e.src for e in g.edges if e.dst == node})


def _naive_out_edges(g: Graph, node: str) -> list[Edge]:
    return [e for e in g.edges if e.src == node]


def _naive_consumers(g: Graph, node: str) -> list[str]:
    return sorted({e.dst for e in g.edges if e.src == node})


class TestGraphAdjacencyIndex:
    def test_matches_naive_scan(self):
        g = _graph()
        for n in g.nodes:
            assert g.producers(n) == _naive_producers(g, n)
            assert [e.src for e in g.out_edges(n)] == \
                   [e.src for e in _naive_out_edges(g, n)]
            assert g.consumers(n) == _naive_consumers(g, n)

    def test_append_invalidates_index(self):
        """追加边后索引自动重建（len 版本化，覆盖 parser 边追加场景）。"""
        g = _graph()
        _ = g.producers("A")          # 首次调用建索引
        g.edges.append(Edge("C", "A", None))
        assert g.producers("A") == ["C", "S1"]

    def test_copy_rebuilds_fresh_index(self):
        g = _graph()
        _ = g.producers("M1")
        g2 = g.copy()
        g2.edges.append(Edge("C", "M1", None))
        assert g2.producers("M1") == ["A", "B", "C"]
        assert g.producers("M1") == ["A", "B"]   # 原图不受影响

    def test_graph_eq_ignores_index(self):
        g1, g2 = _graph(), _graph()
        _ = g1.producers("A")
        assert g1 == g2
