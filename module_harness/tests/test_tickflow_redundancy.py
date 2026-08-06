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


class TestCheckerBranchCache:
    def test_check_unchanged_with_prior_impl(self):
        """缓存重构后 check() 输出与逐对重算实现逐项相等（差分测试）。"""
        g = _graph()
        sugs = check(g)
        # M1（A∈g1 分支、B∈g2 分支 → 互斥）+ M3（D∈g3、E∈g4）各一条；
        # M2（A、C 同在 g1 分支）无建议。
        assert len(sugs) == 2
        assert {s.node for s in sugs} == {"M1", "M3"}
        assert {s.splitter for s in sugs} == {"S1", "S2"}
        by_node = {s.node: s for s in sugs}
        assert set(by_node["M1"].producers) == {"A", "B"}
        # 差分对照：与内嵌的旧逐对重算实现逐项相等
        naive = _naive_check(g)
        assert [(s.node, s.producers, s.splitter, s.branches) for s in sugs] == naive
        # branches 内容与逐对实现一致：_branches_of 用 setdefault 只保留每个
        # guard 首条边的 reach（不合并同 guard 多边），_reachable_until_merge
        # 包含合并点但不继续展开，splitter 自身被 discard。
        assert by_node["M1"].branches == {"g1": ["A", "M1", "M2"], "g2": ["B", "M1"]}

    def test_differential_many_graphs(self):
        """多张拓扑图上的差分对照（等价性可复现，不依赖离线验证）。"""
        graphs = [_graph()]
        # 无 AND-join 的图（冷路径快退场景）
        g2 = Graph(
            nodes={"S": Node(name="S", is_start=True), "A": Node(name="A")},
            edges=[Edge("S", "A", "g1")],
        )
        graphs.append(g2)
        # 单 splitter 单 join 的图
        g3 = Graph(
            nodes={
                "S": Node(name="S", is_start=True),
                "X": Node(name="X"), "Y": Node(name="Y"), "M": Node(name="M"),
            },
            edges=[Edge("S", "X", "ga"), Edge("S", "Y", "gb"),
                   Edge("X", "M", None), Edge("Y", "M", None)],
        )
        graphs.append(g3)
        for g in graphs:
            assert [(s.node, s.producers, s.splitter, s.branches) for s in check(g)] \
                == _naive_check(g)

    def test_check_does_not_mutate(self):
        g = _graph()
        snapshot = g.copy()
        check(g)
        assert g == snapshot


def _naive_check(graph: Graph) -> list:
    """旧实现（逐对重算）：每个 AND-join × 每个 splitter 重算分支 BFS。

    与 tickflow.checker.check 重构前语义逐项等价——差分测试的参照实现。
    """
    from tickflow.checker import _branches_of, _producers_on_distinct_branches
    out = []
    for m, node in graph.nodes.items():
        if node.join != "AND":
            continue
        prods = graph.producers(m)
        if len(prods) < 2:
            continue
        for b in graph.nodes:
            if not graph.is_xor_splitter(b):
                continue
            branches = _branches_of(graph, b)
            res = _producers_on_distinct_branches(graph, m, branches)
            if res is not None:
                pair = res[0]
                out.append((m, pair, b, branches))
    seen: set[tuple[str, str]] = set()
    deduped = []
    for s in out:
        key = (s[0], s[2])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


class TestFireablePassthrough:
    def _registry(self):
        from tickflow.registry import Registry
        reg = Registry()
        reg.body("echo")(lambda view: {"ok": True})
        return reg

    def test_engine_tick_with_and_without_fireable_equal(self):
        from tickflow.state import RunState
        g = Graph(
            nodes={
                "S1": Node(name="S1", is_start=True),
                "A": Node(name="A"), "B": Node(name="B"),
            },
            edges=[Edge("S1", "A", None), Edge("A", "B", None)],
        )
        reg = self._registry()
        for n in g.nodes:
            g.nodes[n].body = "echo"
            g.nodes[n].inputs = {p: InputPolicy.latest() for p in g.producers(n)}
        m0 = bootstrap(g)
        # 不传 fireable：引擎内部计算
        rs_a = RunState(keep_records=False)
        m_a, f_a, _ = tick(g, m0, rs_a, 0, reg)
        # 传预计算 fireable（同一 marking 下与内部计算同值）
        rs_b = RunState(keep_records=False)
        fireable = [n for n in g.nodes if _join_satisfied(g, n, m0)]
        m_b, f_b, _ = tick(g, m0, rs_b, 0, reg, fireable=fireable)
        assert m_a == m_b
        assert [f.node for f in f_a] == [f.node for f in f_b]

    def test_async_tick_with_and_without_fireable_equal(self):
        import asyncio
        from tickflow.async_runner import async_tick
        g = Graph(
            nodes={
                "S1": Node(name="S1", is_start=True),
                "A": Node(name="A"), "B": Node(name="B"),
            },
            edges=[Edge("S1", "A", None), Edge("A", "B", None)],
        )
        reg = self._registry()
        for n in g.nodes:
            g.nodes[n].body = "echo"
            g.nodes[n].inputs = {p: InputPolicy.latest() for p in g.producers(n)}
        m0 = bootstrap(g)

        async def run_with(fireable):
            from tickflow.state import RunState
            rs = RunState(keep_records=False)
            return await async_tick(g, m0, rs, 0, reg, fireable=fireable)

        m_a, f_a, _ = asyncio.run(run_with(None))
        fireable = [n for n in g.nodes if _join_satisfied(g, n, m0)]
        m_b, f_b, _ = asyncio.run(run_with(fireable))
        assert m_a == m_b
        assert [f.node for f in f_a] == [f.node for f in f_b]
