# 冗余清理（重复存储 + 重复计算）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次性消除全代码库的重复存储（S1-S4）与重复计算（C1-C5）冗余，判定标准为性能/功能必要性。

**Architecture:** tickflow 侧（ir/checker/engine/async_runner/state/runner/persistence）直接修改（用户已确认，后续随 sync 机制同步 Graph 仓库）；module_harness 侧（prompt/spec/consistency/graph_builder/checkpoint/module/status）适配与退役。核心存储变革：持久路径每 tick 快照从"全量（含 records + edges/state）"降为"最小快照 {tick, marking, run_state:{keep_records}, status, cancel_reason, fireable, fired}"——restore 的 `truncate_after` 已从 firings 重建窗口/计数/state，快照中的副本是死数据。

**Tech Stack:** Python 3.13, pytest + unittest.mock, sqlite3（WAL）, asyncio。

**Spec:** `docs/superpowers/specs/2026-08-06-redundancy-cleanup-design.md`

> **执行修订（2026-08-06，落地后）**：Task 4/5 的"最小快照"（剥离
> edges/fire_counts/state）在同步到上游 Graph 仓库时被其自有测试
> （`test_persistence.py` 跨 session restore 到新 runner）证伪——快照自包含
> 契约需要窗口/状态，S3 判定修正为"功能必要 → 保留"。最终实现 = **轻量快照**
> （仅剥离 records，含 `fired`）；`to_snapshot_data`/`snapshot` 无 `minimal`
> 参数。本计划各 Task 中涉及 `minimal` 的代码以提交
> `daddf9a`（SpecModule）为准。

---

## 文件结构

| 文件 | 改动 |
|------|------|
| `tickflow/ir.py` | Graph 邻接惰性索引（C4/D8） |
| `tickflow/checker.py` | splitter 分支预计算缓存（C3/D7） |
| `tickflow/engine.py` | `tick(..., fireable=None)`（C1/D4） |
| `tickflow/async_runner.py` | `async_tick(..., fireable=None)` + tick 循环传参（C1/D4） |
| `tickflow/state.py` | `to_snapshot_data(include_records=True, minimal=False)`（S1+S3/D1） |
| `tickflow/runner.py` | `snapshot(include_records, minimal)` + `_persist_tick(fired)`（D1） |
| `tickflow/persistence.py` | `SqliteBackend.latest_firings()`（D2） |
| `module_harness/prompt.py` | 常量正则模块级编译（C5） |
| `module_harness/spec.py` | `Tasklist.to_dict()` 单一事实源（S4/D6） |
| `module_harness/consistency.py` | 改用 `Tasklist.to_dict()`（S4） |
| `module_harness/graph_builder.py` | 改用 `Tasklist.to_dict()`（S4） |
| `module_harness/checkpoint.py` | `AutoCheckpointStore` → `ModuleInputStore`；`tasklist_to_dict` 变薄封装（S2+S4/D3） |
| `module_harness/module.py` | 退役 auto hook；`_archive_module_inputs`；resume tick 回退；list_checkpoints 新形状；删 remap no-op（S2+C2/D2/D3/D5） |
| `module_harness/status.py` | outputs/node_states 从 firings 读；`ModuleStatus.fired`（D2） |
| `module_harness/__init__.py` | 导出 `ModuleInputStore` |
| 新建 `module_harness/tests/test_tickflow_redundancy.py` | ir 索引/checker 缓存/fireable 透传/最小快照等价测试 |
| 新建 `module_harness/tests/test_snapshot_storage.py` | 每 tick 最小快照落盘/restore 等价/latest_firings 测试 |
| `module_harness/tests/test_checkpoint.py` | AutoCheckpointStore 块重写；resume 改 tick 号；hook 测试改写 |
| `module_harness/tests/test_run_status.py` | query_run_status 测试改写 + fired 字段 |
| `docs/progress/module-roadmap.md` + 旧 spec 标注 | 文档更新 |

---

## Task 1: Graph 邻接惰性索引（C4/D8，tickflow/ir.py）

**Files:**
- Modify: `tickflow/ir.py:89-113`
- Test: `module_harness/tests/test_tickflow_redundancy.py`（新建）

- [ ] **Step 1: 写失败测试（索引与全扫等价）**

新建 `module_harness/tests/test_tickflow_redundancy.py`：

```python
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
        """parser 中途追加边后索引自动重建（len 版本化）。"""
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
```

- [ ] **Step 2: 运行确认等价基线通过（测试先于实现写好，实现后必须仍全绿）**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py -q`
Expected: PASS（4 passed——等价性测试在重构前后都通过，锁定行为不变）

- [ ] **Step 3: 实现惰性索引**

`tickflow/ir.py` Graph dataclass 改为：

```python
@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    # 邻接惰性索引（C4）：edges 在本代码库只追加不替换（parser.py:183），
    # len 变化即失效重建；不参与 repr/eq（保持旧 Graph 比较语义）。
    _adj: tuple[
        dict[str, list[Edge]], dict[str, set[str]], dict[str, set[str]]
    ] | None = field(default=None, repr=False, compare=False)
    _adj_len: int = field(default=-1, repr=False, compare=False)

    @property
    def starts(self) -> list[str]:
        return [n for n, node in self.nodes.items() if node.is_start]

    # --- adjacency helpers (used by engine + checker) ---------------------

    def _ensure_adj(self) -> None:
        """重建邻接索引（首次调用或 edges 长度变化时）。"""
        if self._adj is not None and len(self.edges) == self._adj_len:
            return
        out: dict[str, list[Edge]] = {}
        prod: dict[str, set[str]] = {}
        cons: dict[str, set[str]] = {}
        for e in self.edges:
            out.setdefault(e.src, []).append(e)
            prod.setdefault(e.dst, set()).add(e.src)
            cons.setdefault(e.src, set()).add(e.dst)
        self._adj = (out, prod, cons)
        self._adj_len = len(self.edges)

    def producers(self, node: str) -> list[str]:
        """Distinct producer nodes with at least one edge into ``node``."""
        self._ensure_adj()
        return sorted(self._adj[1].get(node, ()))

    def out_edges(self, node: str) -> list[Edge]:
        """Out-edges of ``node``（返回新列表，调用方可安全持有）。"""
        self._ensure_adj()
        return list(self._adj[0].get(node, ()))

    def consumers(self, node: str) -> list[str]:
        self._ensure_adj()
        return sorted(self._adj[2].get(node, ()))
```

`copy()`（ir.py:115-129）无需改动：构造新 `Graph()` 时 `_adj` 为 None，首次邻接调用惰性重建。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 回归既有测试 + 提交**

Run: `python -m pytest module_harness/tests/test_graph_builder.py module_harness/tests/test_align.py -q`
Expected: PASS

```bash
git add tickflow/ir.py module_harness/tests/test_tickflow_redundancy.py
git commit -m "perf(tickflow): Graph 邻接惰性索引——每 tick O(V·E+F·E) 全扫降为 O(V+F+E)（C4）"
```

---

## Task 2: checker 分支预计算缓存（C3/D7，tickflow/checker.py）

**Files:**
- Modify: `tickflow/checker.py:147-183`
- Test: `module_harness/tests/test_tickflow_redundancy.py`（追加）

- [ ] **Step 1: 写等价性测试（追加到 test_tickflow_redundancy.py）**

```python
class TestCheckerBranchCache:
    def test_check_unchanged_with_prior_impl(self):
        """缓存重构后 check() 输出与逐对重算实现逐项相等。"""
        g = _graph()
        sugs = check(g)
        # M1（A∈g1 分支、B∈g2 分支 → 互斥）+ M3（D∈g3、E∈g4）各一条；
        # M2（A、C 同在 g1 分支）无建议。
        assert len(sugs) == 2
        assert {s.node for s in sugs} == {"M1", "M3"}
        assert {s.splitter for s in sugs} == {"S1", "S2"}
        by_node = {s.node: s for s in sugs}
        assert set(by_node["M1"].producers) == {"A", "B"}
        # branches 内容与逐对实现一致：S1 的 g1 分支 = {A, C}（A、C 各自
        # 在 M1/M2 处截止），g2 分支 = {B}
        assert by_node["M1"].branches == {"g1": ["A", "C"], "g2": ["B"]}

    def test_check_does_not_mutate(self):
        g = _graph()
        before = (g.nodes["M1"].join, g.nodes["M2"].join, g.nodes["M3"].join)
        check(g)
        assert (g.nodes["M1"].join, g.nodes["M2"].join, g.nodes["M3"].join) == before
```

- [ ] **Step 2: 运行确认当前实现通过（等价基线）**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py -q`
Expected: PASS

- [ ] **Step 3: 实现 splitter 预计算**

`tickflow/checker.py` 的 `check()` 改为：

```python
def check(graph: Graph) -> list[DeadlockSuggestion]:
    """Return all deadlock suggestions for ``graph``. Does not mutate."""
    out: list[DeadlockSuggestion] = []
    # 分支可达集只依赖 splitter，与候选 AND-join m 无关——预计算一次，内层
    # 查表（C3）：O(S·(V+E)) 而非 O(K·S·(V+E))。
    splitters: dict[str, dict[str, list[str]]] = {
        b: _branches_of(graph, b) for b in graph.nodes if graph.is_xor_splitter(b)
    }
    for m, node in graph.nodes.items():
        if node.join != "AND":
            continue
        prods = graph.producers(m)
        if len(prods) < 2:
            continue
        # Find any XOR-splitter B such that >=2 of M's producers lie on
        # distinct branches of B.
        for b, branches in splitters.items():
            res = _producers_on_distinct_branches(graph, m, branches)
            if res is not None:
                pair = res[0]
                out.append(
                    DeadlockSuggestion(
                        node=m,
                        producers=pair,
                        splitter=b,
                        branches=branches,
                    )
                )
    # Dedupe by (node, splitter) -- multiple producer pairs on same splitter
    # collapse to one suggestion.
    seen: set[tuple[str, str]] = set()
    deduped: list[DeadlockSuggestion] = []
    for s in out:
        key = (s.node, s.splitter)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tickflow/checker.py
git commit -m "perf(tickflow): checker splitter 分支预计算——O(K·S·(V+E)) 降为 O(S·(V+E))（C3）"
```

---

## Task 3: fireable 透传（C1/D4，tickflow/engine.py + async_runner.py）

**Files:**
- Modify: `tickflow/engine.py:131-149`、`tickflow/async_runner.py:64-76`
- Modify: `tickflow/runner.py:517-538`、`tickflow/async_runner.py:193-214`
- Test: `module_harness/tests/test_tickflow_redundancy.py`（追加）

- [ ] **Step 1: 写透传等价测试（追加到 test_tickflow_redundancy.py）**

```python
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
```

测试文件头部 import 需含 `_join_satisfied`：

```python
from tickflow.checker import check
from tickflow.engine import _join_satisfied, bootstrap, tick
from tickflow.ir import Edge, Graph, InputPolicy, Node
```

- [ ] **Step 2: 运行确认当前实现通过（基线）**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py -q`
Expected: PASS

- [ ] **Step 3: 实现**

`tickflow/engine.py` `tick()` 签名与首段改为：

```python
def tick(
    graph: Graph,
    marking: Marking,
    run_state: RunState,
    t: int,
    registry: Registry,
    fireable: list[str] | None = None,
) -> tuple[Marking, list[NodeState], bool]:
    """One synchronous tick. Returns ``(next_marking, firings, aborted)``.

    ``aborted`` is True iff some node returned an ``infrastructure`` Failure
    this tick -- callers (Runner) should then stop further ticks.

    ``fireable``: precomputed fireable set for ``marking`` (the Runner already
    computes it for tick-start hooks -- passing it avoids recomputing the
    identical value, C1). Default None computes internally; direct callers
    are unaffected.

    Body/guard callables here are *synchronous*. For async bodies use
    :mod:`tickflow.async_runner` which mirrors this logic with ``await`` +
    ``asyncio.gather`` over the fireable set.
    """
    if fireable is None:
        fireable = [n for n in graph.nodes if _join_satisfied(graph, n, marking)]
    if not fireable:
        return marking.copy(), [], False
```

`tickflow/async_runner.py` `async_tick()` 同样修改（签名加 `fireable: list[str] | None = None`，首段 `if fireable is None: fireable = [...]`）。

- [ ] **Step 4: Runner.tick / AsyncRunner.tick 传入预计算值（仅 fireable；`_persist_tick(fired)` 签名改动归 Task 5）**

`tickflow/runner.py` `Runner.tick()` 中两处改动：

```python
        fireable = self.fireable()
        self._run_tick_start_hooks(self.tick_count, fireable)
        next_marking, firings, aborted = tick(
            self.graph, self.marking, self.run_state, self.tick_count, self.registry,
            fireable=fireable,
        )
```

（`self._persist_tick()` 调用保持原样，Task 5 改签名。）

`tickflow/async_runner.py` `AsyncRunner.tick()` 同样：`async_tick(..., fireable=fireable)`。

- [ ] **Step 5: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_tickflow_redundancy.py module_harness/tests/test_module.py module_harness/tests/test_align.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add tickflow/engine.py tickflow/async_runner.py tickflow/runner.py
git commit -m "perf(tickflow): engine fireable 透传——每 tick 重复计算消除（C1）"
```

---

## Task 4: RunState.to_snapshot_data minimal（S1+S3/D1，tickflow/state.py）

**Files:**
- Modify: `tickflow/state.py:341-358`
- Test: `module_harness/tests/test_snapshot_storage.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `module_harness/tests/test_snapshot_storage.py`：

```python
# module_harness/tests/test_snapshot_storage.py
"""最小快照（S1/S3）与 restore 等价性 + SqliteBackend.latest_firings 测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tickflow.ir import Edge, Graph, InputPolicy, Node
from tickflow.persistence import SqliteBackend
from tickflow.registry import Registry
from tickflow.state import NodeState, RunState


class TestToSnapshotDataMinimal:
    def test_minimal_keeps_only_keep_records(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1,
                            mutable_state={"k": "v"}))
        data = rs.to_snapshot_data(include_records=False, minimal=True)
        assert set(data) == {"keep_records"}
        assert data["keep_records"] is True

    def test_default_still_full(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1,
                            mutable_state={"k": "v"}))
        data = rs.to_snapshot_data()
        assert set(data) == {"edges", "fire_counts", "state", "keep_records", "records"}

    def test_include_records_false_strips_records_only(self):
        rs = RunState(keep_records=True)
        rs.record(NodeState(tick=0, node="A", output=1))
        data = rs.to_snapshot_data(include_records=False)
        assert "records" not in data
        assert "edges" in data

    def test_keep_records_false_no_records(self):
        rs = RunState(keep_records=False)
        rs.record(NodeState(tick=0, node="A", output=1))
        data = rs.to_snapshot_data()
        assert "records" not in data


def _chain_graph() -> Graph:
    """A --> B --> C 三节点链（B、C 声明输入）。"""
    g = Graph(
        nodes={
            "A": Node(name="A", is_start=True),
            "B": Node(name="B"),
            "C": Node(name="C"),
        },
        edges=[Edge("A", "B", None), Edge("B", "C", None)],
    )
    g.nodes["B"].inputs = {"A": InputPolicy.latest()}
    g.nodes["C"].inputs = {"B": InputPolicy.latest()}
    return g


def _chain_registry() -> Registry:
    reg = Registry()
    reg.body("echo")(lambda view: {"ok": True})
    return reg


class TestMinimalSnapshotRoundtrip:
    @pytest.fixture
    def runner(self, tmp_path):
        from tickflow.runner import Runner
        backend = SqliteBackend(tmp_path / "t.sqlite")
        r = Runner(_chain_graph(), _chain_registry(), backend=backend, session_id="s")
        yield r
        backend.close()

    def test_persist_tick_writes_minimal_snapshot(self, runner):
        runner.tick()                       # A 执行
        runner.tick()                       # B 执行
        rows = []
        conn = sqlite3.connect(str(runner._backend._db_path))
        try:
            for (data,) in conn.execute("SELECT data FROM snapshots"):
                rows.append(json.loads(data))
        finally:
            conn.close()
        assert [r["tick"] for r in rows] == [1, 2]
        snap = rows[-1]
        assert set(snap["run_state"]) == {"keep_records"}
        assert snap["fired"] == ["B"]
        assert "records" not in snap["run_state"]
        assert "edges" not in snap["run_state"]

    def test_restore_minimal_snapshot_continues_correctly(self, runner):
        runner.tick()                       # tick 1: A
        runner.tick()                       # tick 2: B
        snap = runner.snapshot(include_records=False, minimal=True)
        snap["fired"] = ["B"]
        assert runner.tick_count == 2
        runner.run_until_idle(max_ticks=10) # 跑完 C
        assert runner.tick_count == 4       # tick 3: C，tick 4 空

        # 新 runner（同 backend/session）restore 到 tick 2 → 只重跑 C
        r2 = Runner(_chain_graph(), _chain_registry(),
                    backend=runner._backend, session_id="s")
        r2.restore(snap)
        assert r2.tick_count == 2
        assert "C" in r2.fireable()
        # 窗口/计数/state 已从 firings 重建：B 的输出可被 C 解析
        firings = r2.run_until_idle(max_ticks=10)
        assert [f.node for f in firings] == ["C"]
        assert firings[0].inputs["B"] == {"ok": True}

    def test_restore_legacy_full_snapshot_still_works(self, runner):
        runner.tick()
        snap = runner.snapshot()            # 旧格式全量快照（含 edges/state）
        r2 = Runner(_chain_graph(), _chain_registry(),
                    backend=runner._backend, session_id="s")
        r2.restore(snap)
        assert r2.tick_count == 1
        assert "B" in r2.fireable()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_snapshot_storage.py -q`
Expected: FAIL（`to_snapshot_data()` 不接受 `minimal`/`include_records` 参数）

- [ ] **Step 3: 实现**

`tickflow/state.py` `to_snapshot_data()` 改为：

```python
    def to_snapshot_data(
        self, include_records: bool = True, minimal: bool = False
    ) -> dict:
        """JSON-able dict for ``Runner.snapshot()``.

        ``edges``/``fire_counts``/``state`` 默认包含；``records`` 仅当
        ``keep_records=True`` 且 ``include_records``。``minimal``（持久路径
        每 tick 快照）只保留 ``keep_records``——窗口/计数/state 是死数据
        （S3：restore 的 truncate_after 从 firings 重建），records 由
        firings 表持有（S1）。
        """
        data: dict[str, Any] = {"keep_records": self._keep_records}
        if not minimal:
            data["edges"] = {
                n: [[t, _jsonable(v)] for (t, v) in lst]
                for n, lst in self._edges.items()
            }
            data["fire_counts"] = dict(self._fire_counts)
            data["state"] = {n: dict(s) for n, s in self._state.items()}
        if self._keep_records and include_records and not minimal:
            data["records"] = [ns.to_json() for ns in self._records]
        return data
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_snapshot_storage.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tickflow/state.py module_harness/tests/test_snapshot_storage.py
git commit -m "perf(tickflow): RunState.to_snapshot_data 支持 minimal——快照剥离 records/edges/state（S1+S3）"
```

---

## Task 5: Runner.snapshot/_persist_tick + fired（D1，tickflow/runner.py）

**Files:**
- Modify: `tickflow/runner.py:270-284`、`290-300`、`517-538`、`async_runner.py:193-214`

- [ ] **Step 1: 实现**

`tickflow/runner.py`：

```python
    def _persist_tick(self, fired: list[str]) -> None:
        """Persist this tick's queued firings + lightweight snapshot to the backend.

        ``fired``：本 tick fire 的节点名列表（历史审阅轨迹；空 tick 为 []）。
        """
        # Defensive: __init__ guarantees a backend and session_id.
        if self._backend is None or self._session_id is None:
            return
        try:
            self.run_state.flush_firings()
            if self._persistent:
                # 每 tick 存最小快照：剥离 records（S1——firings 表持有审计）
                # 与 edges/state（S3——restore 时 truncate_after 从 firings
                # 重建，快照副本是死数据），附 fired 轨迹。
                # fast mode（NullBackend）跳过：零持久化，无消费方（D7）。
                snap = self.snapshot(include_records=False, minimal=True)
                snap["fired"] = list(fired)
                self._backend.save_snapshot(self._session_id, self.tick_count, snap)
        except Exception:
            log.exception("backend persistence failed; swallowed")
```

```python
    def snapshot(
        self, include_records: bool = True, minimal: bool = False
    ) -> dict:
        """JSON-able snapshot of (marking, run_state, tick, status, fireable).

        ``include_records``：默认 True（进程内完整快照/手动检查点）；
        持久路径每 tick 快照传 False（records 由 firings 表持有，S1）。
        ``minimal``：run_state 只含 keep_records（窗口/计数/state 由
        restore 的 truncate_after 从 firings 重建，S3）。
        """
        run_data = self.run_state.to_snapshot_data(
            include_records=include_records, minimal=minimal
        )
        return {
            "tick": self.tick_count,
            "marking": self.marking.to_json(),
            "run_state": run_data,
            "status": self.status.value,
            "cancel_reason": self.cancel_reason,
            "fireable": self.fireable(),
        }
```

`Runner.tick()` 与 `AsyncRunner.tick()` 末尾的 `self._persist_tick()` 改为 `self._persist_tick([f.node for f in firings])`（fireable 传参已在 Task 3 完成）。

- [ ] **Step 2: 运行快照存储测试确认通过**

Run: `python -m pytest module_harness/tests/test_snapshot_storage.py -q`
Expected: PASS（test_persist_tick_writes_minimal_snapshot 现在验证真实 runner 落盘）

- [ ] **Step 3: 回归 + 提交**

Run: `python -m pytest module_harness/tests/test_module.py module_harness/tests/test_align.py module_harness/tests/test_integration.py -q`
Expected: PASS（smoke 测试用真实 LLM，不在此回归范围）

```bash
git add tickflow/runner.py tickflow/async_runner.py
git commit -m "feat(tickflow): _persist_tick 存最小快照 + fired 轨迹（D1）"
```

---

## Task 6: SqliteBackend.latest_firings（D2，tickflow/persistence.py）

**Files:**
- Modify: `tickflow/persistence.py`（SqliteBackend，firings 区段后新增方法）
- Test: `module_harness/tests/test_snapshot_storage.py`（追加）

- [ ] **Step 1: 写失败测试（追加到 test_snapshot_storage.py）**

```python
class TestLatestFirings:
    def test_last_firing_per_node_dedup_replay(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.sqlite")
        try:
            from tickflow.state import NodeState
            # A 两次 firing + B 一次；再模拟 restore-replay 写入同 (tick,node)
            for ns in [
                NodeState(tick=1, node="A", output="a1"),
                NodeState(tick=2, node="A", output="a2"),
                NodeState(tick=2, node="B", output="b1"),
            ]:
                backend.save_firing("s", ns)
            replay = NodeState(tick=2, node="A", output="a2_replay")
            backend.save_firing("s", replay)   # 重放重复行：keep-first
            rows = backend.latest_firings("s")
            by_node = {d["node"]: d for d in rows}
            assert set(by_node) == {"A", "B"}
            assert by_node["A"]["output"] == "a2"      # 最新 tick + keep-first
            assert by_node["B"]["output"] == "b1"
        finally:
            backend.close()

    def test_empty_session(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.sqlite")
        try:
            assert backend.latest_firings("s") == []
        finally:
            backend.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_snapshot_storage.py::TestLatestFirings -q`
Expected: FAIL（`latest_firings` 不存在）

- [ ] **Step 3: 实现**

`tickflow/persistence.py` SqliteBackend 在 `firings_of` 之后新增：

```python
    def latest_firings(self, session_id: str) -> list[dict]:
        """每节点最后一 firing（按 tick 去重 keep-first，语义与
        ``firings_of`` 一致），返回 JSON data 列表，按 node 升序。

        监控查询（query_run_status）在快照剥离 edges/state 后（S3）从
        firings 取最新输出/状态——O(节点数)，走 idx_firings_node 索引。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.data FROM firings f
                JOIN (
                    SELECT node, MAX(tick) AS mt FROM (
                        SELECT node, tick FROM firings
                        WHERE session_id = ? GROUP BY node, tick
                    ) GROUP BY node
                ) l ON f.node = l.node AND f.tick = l.mt
                WHERE f.session_id = ?
                  AND f.id = (
                      SELECT MIN(id) FROM firings
                      WHERE session_id = ? AND node = f.node AND tick = f.tick
                  )
                ORDER BY f.node
                """,
                (session_id, session_id, session_id),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_snapshot_storage.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tickflow/persistence.py
git commit -m "feat(tickflow): SqliteBackend.latest_firings——每节点最后一 firing（D2）"
```

---

## Task 7: prompt 常量正则提升（C5，module_harness/prompt.py）

**Files:**
- Modify: `module_harness/prompt.py:6-12,59-82`

- [ ] **Step 1: 实现**

`module_harness/prompt.py`：

```python
import re
from typing import Any

from tickflow.views import DictView, Missing

from .config import HarnessConfig

# C5：常量正则模块级编译一次（render 每 harness 节点每 tick 调用）。
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
```

`_substitute` 内删除 `pattern = re.compile(...)` 行，末行改为：

```python
        return _PLACEHOLDER_RE.sub(_replacer, template)
```

- [ ] **Step 2: 回归 + 提交**

Run: `python -m pytest module_harness/tests/test_prompt.py module_harness/tests/test_harness.py -q`
Expected: PASS

```bash
git add module_harness/prompt.py
git commit -m "perf(prompt): 常量占位符正则模块级编译（C5）"
```

---

## Task 8: Tasklist.to_dict 单一事实源（S4/D6）

**Files:**
- Modify: `module_harness/spec.py`、`module_harness/checkpoint.py:40-45`、`module_harness/consistency.py:82-88`、`module_harness/graph_builder.py:63-68`
- Test: `module_harness/tests/test_spec.py`（追加）

- [ ] **Step 1: 写测试（追加到 test_spec.py）**

```python
class TestTasklistToDict:
    def test_matches_tasklist_to_dict(self):
        from module_harness.checkpoint import tasklist_to_dict
        tl = Tasklist(
            tasks={"A": TaskDefinition(type="script", script="echo",
                                       inputs={"x": "B"})},
            flow="[A] --> B",
        )
        assert tl.to_dict() == tasklist_to_dict(tl) == {
            "Tasks": {"A": {"type": "script", "script": "echo",
                            "harness": None, "command": None, "timeout": None,
                            "cwd": None, "promptmode": None, "prompt": None,
                            "outputformat": None, "notdo": None, "model": None,
                            "temperature": None, "think": None,
                            "api_params": None, "inputs": {"x": "B"}}},
            "Flow": "[A] --> B",
        }
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest module_harness/tests/test_spec.py -q`
Expected: FAIL（`Tasklist.to_dict` 不存在）

- [ ] **Step 3: 实现**

`module_harness/spec.py`：

```python
from dataclasses import dataclass, field
import dataclasses
from typing import Any, Literal
```

`Tasklist` dataclass 内新增：

```python
    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化 dict（与 ``from_json`` 对称）——唯一实现（S4）。"""
        return {
            "Tasks": {k: dataclasses.asdict(v) for k, v in self.tasks.items()},
            "Flow": self.flow,
        }
```

`module_harness/checkpoint.py` `tasklist_to_dict` 改为薄封装：

```python
def tasklist_to_dict(tl: Tasklist) -> dict[str, Any]:
    """Tasklist → JSON 可序列化 dict（``Tasklist.to_dict`` 薄封装，导出兼容）。"""
    return tl.to_dict()
```

`module_harness/consistency.py` 的 `review()` 内联 dict 构造（82-88 行）替换为：

```python
        tasklist_dict = tasklist.to_dict()
```

`module_harness/graph_builder.py` `build()` 内联 dict 构造（63-68 行）替换为：

```python
        tasklist_dict = tasklist.to_dict()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_spec.py module_harness/tests/test_consistency.py module_harness/tests/test_graph_builder.py module_harness/tests/test_checkpoint.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add module_harness/spec.py module_harness/checkpoint.py module_harness/consistency.py module_harness/graph_builder.py
git commit -m "refactor: Tasklist.to_dict 单一事实源——3 处重复实现统一（S4）"
```

---

## Task 9: AutoCheckpointStore → ModuleInputStore（S2/D3，module_harness/checkpoint.py）

**Files:**
- Modify: `module_harness/checkpoint.py:1-12,53-204`
- Modify: `module_harness/__init__.py:50-55,122-126`
- Test: `module_harness/tests/test_checkpoint.py:23-122`（重写）

- [ ] **Step 1: 实现 ModuleInputStore**

`module_harness/checkpoint.py` 文件头 docstring 与 `AutoCheckpointStore` 类替换为：

```python
# module_harness/checkpoint.py
"""Module 层快照/回滚：运行输入存档 + 兼容性校验（roadmap #5）。

- ``ModuleInputStore``：run.sqlite 内 ``module_inputs`` 表（本次运行使用的
  spec/tasklist 存档，供兼容性对比与跨进程查询）。
- ``check_resume_compat``：新 tasklist 与已执行节点的兼容性校验。

零修改 tickflow：全部实现位于 module_harness 层；module_inputs 表独立于
SqliteBackend 的 snapshots/firings/checkpoints 表，通过独立 sqlite3 连接
打开同一 run.sqlite（WAL 模式多连接安全）。

注：自动检查点（auto_checkpoints 表）已退役（S2）——每 tick 快照由
tickflow 的 _persist_tick 直接写入 snapshots 表（最小快照，D1）。
"""

...

class ModuleInputStore:
    """run.sqlite 内运行输入存档（module_inputs 表）。

    ``module_inputs(id INT PK CHECK(id=1), spec TEXT, tasklist TEXT,
    saved_at REAL)``——单行，覆盖式，供兼容性校验（警告 1）与跨进程查询
    "这次 run 用了什么输入"。

    连接策略：构造时打开独立连接（WAL 模式，与 SqliteBackend 并存安全）；
    写失败仅 log 不阻断（对齐 status.json 容错哲学）。
    """

    def __init__(self, module_id: str, base_dir: Path | None = None) -> None:
        self.module_id = module_id
        self.db_path = _run_db_path(module_id, base_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS module_inputs (
                id        INTEGER PRIMARY KEY CHECK (id = 1),
                spec      TEXT NOT NULL,
                tasklist  TEXT NOT NULL,
                saved_at  REAL NOT NULL
            );
            """
        )

    def close(self) -> None:
        """关闭连接。Module 生命周期结束或临时 store 用完后调用。"""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def save_module_inputs(self, spec: dict[str, Any], tasklist: dict[str, Any]) -> None:
        """覆盖式存档本次运行的 spec/tasklist（JSON 深拷贝语义）。"""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO module_inputs(id, spec, tasklist, saved_at) "
                "VALUES (1, ?, ?, ?)",
                (json.dumps(spec, ensure_ascii=False),
                 json.dumps(tasklist, ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()
        except (sqlite3.Error, OSError, TypeError):
            log.exception("module_inputs 存档失败（不阻断）: %s", self.db_path)

    def load_module_inputs(self) -> dict[str, Any] | None:
        """读回存档；无存档或损坏返回 None。返回 ``{"spec": dict, "tasklist": dict}``。"""
        try:
            row = self._conn.execute(
                "SELECT spec, tasklist FROM module_inputs WHERE id = 1"
            ).fetchone()
        except sqlite3.Error:
            log.exception("module_inputs 读取失败: %s", self.db_path)
            return None
        if row is None:
            return None
        try:
            return {"spec": json.loads(row[0]), "tasklist": json.loads(row[1])}
        except json.JSONDecodeError:
            log.warning("module_inputs 存档损坏，忽略")
            return None
```

删除 `AutoCheckpointStore` 类全部（含 `save`/`load`/`list`/`_prune`/`max_auto`）。

- [ ] **Step 2: 更新导出**

`module_harness/__init__.py`：

```python
from .checkpoint import (
    ModuleInputStore,
    ResumeCheck,
    ResumeError,
    check_resume_compat,
)
```

`__all__` 中 `"AutoCheckpointStore"` → `"ModuleInputStore"`。

- [ ] **Step 3: 重写 store 单元测试**

`module_harness/tests/test_checkpoint.py` 顶部改为：

```python
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
```

（删除 `TestAutoCheckpointStore` 类的 save/load/list/ring/corrupt-row/unserializable-snap 7 个测试。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: 其余测试此时可能仍引用旧 API——先跑确认当前失败集合仅在 TestAutoCheckpointHook/TestResume（Task 10 改写），本 Task 目标用例全过：

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestModuleInputStore -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add module_harness/checkpoint.py module_harness/__init__.py module_harness/tests/test_checkpoint.py
git commit -m "refactor(checkpoint): AutoCheckpointStore 退役→ModuleInputStore（S2）"
```

---

## Task 10: Module 编排器适配（D2/D3/D5，module_harness/module.py）

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_checkpoint.py`（TestModuleSnapshotAPI 微调 + TestAutoCheckpointHook/TestResume/TestResumeLoop 重写）

- [ ] **Step 1: 实现 module.py 改动**

`module_harness/module.py` import 块改为：

```python
from .checkpoint import (
    ModuleInputStore,
    ResumeError,
    check_resume_compat,
    tasklist_from_dict,
    tasklist_to_dict,
)
```

`__init__` 中：

```python
        self._runner: AsyncRunner | None = None
        self._last_tasklist: Tasklist | None = None
        self._input_store: ModuleInputStore | None = None
```

（删除 `self._checkpoint_store` 与 `self._auto_cp_hooked`。）

`_build_runner_async` 末尾：

```python
        self._runner = runner
        return runner
```

（删除 `# 新 runner 需要重新注册自动检查点 hook（二次 run()/build 场景）` 与 `self._auto_cp_hooked = False`。）

`run()` docstring 改为：

```python
    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。

        persist=True 时：每 tick 由 tickflow ``_persist_tick`` 落盘最小快照，
        并归档本次 spec/tasklist 到 module_inputs 表（``_archive_module_inputs``）。
        """
```

`close()` 改为：

```python
    def close(self) -> None:
        """释放 Module 持有的 SQLite 连接（``_input_store``，懒创建）。

        run()/resume() 结束后可调用；幂等（重复调用安全）。再次 run()/resume()
        会按需重新创建 store（``_archive_module_inputs`` 懒重建）。

        注：runner 持有的 SqliteBackend 连接属于 runner 生命周期，由调用方
        管理（与既有行为一致），本方法只关闭 Module 自己创建的
        ModuleInputStore 连接，不触碰 runner。
        """
        if self._input_store is not None:
            self._input_store.close()
            self._input_store = None
```

`list_checkpoints()` 改为：

```python
    def list_checkpoints(self) -> list[tuple[int, str | list[str], str]]:
        """全部检查点 (tick, fired 或 label, kind)，按 tick 升序。

        kind ∈ {"tick", "manual"}：tick = snapshots 表每 tick 最小快照
        （fired 节点列表，历史审阅雏形）；manual = checkpoint() 手动检查点
        （label）。不依赖 runner——跨进程场景（新 Module 实例）也可查询。
        """
        out: list[tuple[int, str | list[str], str]] = []
        if self.persist:
            backend = SqliteBackend(_persist_dir(self.module_id))
            try:
                for tick in backend.list_snapshots(self.module_id):
                    snap = backend.load_snapshot(self.module_id, tick)
                    if snap is None:
                        continue
                    out.append((tick, list(snap.get("fired", [])), "tick"))
                out.extend(
                    (tick, label, "manual")
                    for label, tick in backend.list_checkpoints(self.module_id)
                )
            except Exception:
                log.exception("检查点列表读取失败（忽略）")
            finally:
                backend.close()
        return sorted(out, key=lambda item: item[0])
```

删除 `_load_checkpoint`，新增：

```python
    @staticmethod
    def _resolve_target(backend: SqliteBackend, module_id: str, rollback_to: int | str) -> dict | None:
        """解析回退目标：tick 号 → snapshots 表；manual:xxx → checkpoints 表。

        其他（非数字、非 manual 前缀）返回 None——调用方抛 KeyError。
        """
        if isinstance(rollback_to, int) or (
            isinstance(rollback_to, str) and rollback_to.isdigit()
        ):
            return backend.load_snapshot(module_id, int(rollback_to))
        if isinstance(rollback_to, str) and rollback_to.startswith("manual:"):
            return backend.load_checkpoint(module_id, rollback_to)
        return None
```

`_run_with_phases` 中 `self._register_auto_checkpoint()` 改为 `self._archive_module_inputs()`；删除 `_register_auto_checkpoint`，新增：

```python
    def _archive_module_inputs(self) -> None:
        """归档本次运行的 spec/tasklist 到 module_inputs 表（警告 1 对比源）。

        run()/resume() 共用（_run_with_phases 开头调用）；resume 中位于
        兼容性校验与 restore 之后——先读旧存档再覆盖，顺序正确。
        """
        if not self.persist:
            return
        if self._input_store is None:
            self._input_store = ModuleInputStore(self.module_id)
        assert self._last_tasklist is not None
        self._input_store.save_module_inputs(
            self.spec.to_dict(), self._last_tasklist.to_dict()
        )
```

`resume()` 整体替换为：

```python
    async def resume(self, rollback_to: int | str, max_ticks: int = 100):
        """跨进程续跑：从 tick 号/手动检查点恢复 + 用当前 spec/tasklist 重建未执行部分。

        流程：回退目标解析（tick 号 → snapshots 表；manual:xxx → checkpoints
        表）→ 新图全量重建 → 兼容性校验（硬错误拒绝，不触碰 runner）→
        restore → 归档新输入 → 续跑。

        要求 persist=True（快照依赖 SQLite backend）。

        max_ticks 是绝对 tick 上限：从 restore 的 tick 起继续计数（如
        restore 于 tick 95，则默认 100 只剩 5 个 tick 可跑）。
        """
        if not self.persist:
            raise RuntimeError(
                "resume 需要 persist=True（快照依赖 SQLite backend）"
            )

        # 1. 回退目标解析 + 已执行节点（同一连接，避免多次打开 run.sqlite）
        backend = SqliteBackend(_persist_dir(self.module_id))
        try:
            snap = self._resolve_target(backend, self.module_id, rollback_to)
            if snap is None:
                ticks = backend.list_snapshots(self.module_id)
                manual = [label for label, _ in backend.list_checkpoints(self.module_id)]
                raise KeyError(
                    f"回退目标 {rollback_to!r} 不存在"
                    f"（可用 tick: {ticks or '无'}；manual: {manual or '无'}）"
                )
            # 已执行节点：firings 表中 tick ≤ 快照 tick 的去重节点
            # （S3 后快照不再含 edges 窗口）。
            executed_nodes = {
                d["node"] for d in backend.list_firings(self.module_id)
                if d.get("node") and int(d.get("tick", 0)) <= int(snap.get("tick", 0))
            }
        finally:
            backend.close()

        # 2. 旧输入存档（警告 1 对比源；覆盖前读取）
        store = ModuleInputStore(self.module_id)
        try:
            old_inputs = store.load_module_inputs()
        finally:
            store.close()

        # 3. 新 spec/tasklist 全量重建（含校验 + 一致性审核）
        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise

        # 4. 兼容性校验（构造 runner 后、restore 前；硬错误拒绝且不触碰状态）
        marking = snap.get("marking") or {}
        old_tl = tasklist_from_dict(old_inputs["tasklist"]) if old_inputs else None
        check = check_resume_compat(
            self._last_tasklist, runner.graph, executed_nodes,
            old_tasklist=old_tl,
            marking_slots=marking.get("slots"),
            armed_starts=marking.get("armed_starts"),
        )
        for w in check.warnings:
            log.warning("resume 兼容性警告: %s", w)
        if check.hard_errors:
            self._write_phase("aborted", error="resume 兼容性校验失败")
            raise ResumeError(check.hard_errors)

        # 5. restore + 续跑（phase 写盘与 run() 共用；不再 remap_graph——
        #    restore 已设好 marking，同图 remap 是 no-op，C2）
        runner.restore(snap)
        return await self._run_with_phases(runner, max_ticks)
```

- [ ] **Step 2: 重写 TestAutoCheckpointHook → TestPerTickSnapshotPersistence**

`module_harness/tests/test_checkpoint.py` 中 `TestAutoCheckpointHook` 类整体替换为：

```python
class TestPerTickSnapshotPersistence:
    @pytest.fixture(autouse=True)
    def _close_created_modules(self):
        """teardown 关闭本类测试创建的 Module（释放懒创建的 store 连接）。"""
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
    async def test_run_writes_per_tick_snapshots(self, mock_llm, tmp_path, monkeypatch):
        """每 tick 落盘最小快照：snapshots 表含 tick 1..4，含 fired 轨迹。

        tick 编号：_persist_tick 在 tick_count 自增后落盘（快照 tick = 刚
        完成的 tick + 1）——三节点链 tick 0/1/2 各一次 firing + tick 3 空 →
        快照 tick 1..4，fired 分别为 [A]/[B]/[C]/[]。
        """
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        cps = mod.list_checkpoints()
        ticks = [c for c in cps if c[2] == "tick"]
        assert [t for t, _, _ in ticks] == [1, 2, 3, 4]
        # fired 轨迹：快照 tick 2 = tick 1 完成的 B
        assert (2, ["B"], "tick") in cps
        # 空 tick 的 fired 为 []
        assert (4, [], "tick") in cps

    @pytest.mark.asyncio
    async def test_run_archives_module_inputs(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        store = ModuleInputStore("mod_test")
        inputs = store.load_module_inputs()
        store.close()
        assert inputs is not None
        assert inputs["spec"] == {"x": 1}
        assert inputs["tasklist"]["Flow"] == "[A] --> B\nB --> C"

    @pytest.mark.asyncio
    async def test_fast_mode_no_checkpoints(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=False)
        await mod.run()
        assert mod.list_checkpoints() == []

    @pytest.mark.asyncio
    async def test_second_run_overwrites_snapshots(self, mock_llm, tmp_path, monkeypatch):
        """同一实例二次 run()（新 runner）：快照按 (session, tick) 覆盖，新链扩展。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        ticks1 = sorted(t for t, _, k in mod.list_checkpoints() if k == "tick")
        assert ticks1 == [1, 2, 3, 4]
        # 换 4 节点链二次 run：新 runner 快照 tick 1..5（1..4 覆盖 + 5 新增）
        tasks = {
            f"N{i}": TaskDefinition(type="script", script="echo",
                                    inputs={"data": f"N{i-1}"} if i > 0 else None)
            for i in range(4)
        }
        mod.tasklist = Tasklist(
            tasks=tasks, flow="[N0] --> N1\nN1 --> N2\nN2 --> N3"
        )
        await mod.run()
        ticks2 = sorted(t for t, _, k in mod.list_checkpoints() if k == "tick")
        assert ticks2 == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_module_close_releases_store(self, mock_llm, tmp_path, monkeypatch):
        """Module.close() 释放懒创建的 _input_store 连接；幂等。"""
        import sqlite3
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        assert mod._input_store is not None        # run() 后 store 已懒创建
        conn = mod._input_store._conn              # 实证：连接真实关闭
        mod.close()
        assert mod._input_store is None
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")               # 已关闭的连接不可再操作
        mod.close()                                # 幂等：重复调用不抛异常
        # close 后再 run()：store 懒重建，module_inputs 照常写入
        await mod.run()
        store = ModuleInputStore("mod_test")
        try:
            assert store.load_module_inputs() is not None
        finally:
            store.close()
```

- [ ] **Step 3: 重写 TestResume（tick 号回退）**

`TestResume` 中以下测试的 `rollback_to` 值替换（label → tick 号，语义不变）：

| 原调用 | 新调用 | 依据 |
|--------|--------|------|
| `resume("auto:tick:1")` | `resume(2)` | auto:tick:1 的 snapshot.tick=2 = B 已执行、C 未执行 |
| `resume("auto:tick:0")` | `resume(1)` | A 刚执行完 |
| `resume("auto:tick:2")` | `resume(3)` | C 已执行 |
| `resume("nope")` | `resume(999)` + `match="999"` | 不存在目标 |

`test_resume_continues_from_checkpoint` 中 `assert any(c[2] == "auto" and c[1] == 1 for c in mod.list_checkpoints())` 改为：

```python
        assert any(c[2] == "tick" and c[0] == 2 for c in mod.list_checkpoints())
```

`test_resume_deep_rollback_no_false_warning` 中：

```python
        assert any(c[2] == "tick" and c[0] == 1 for c in mod.list_checkpoints())
        ...
            firings = await mod2.resume(rollback_to=1)
```

`test_resume_missing_checkpoint_raises_keyerror` 改为：

```python
    @pytest.mark.asyncio
    async def test_resume_missing_checkpoint_raises_keyerror(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        with pytest.raises(KeyError, match="999"):
            await mod.resume(rollback_to=999)
        with pytest.raises(KeyError, match="abc"):
            await mod.resume(rollback_to="abc")
```

`test_resume_fast_mode_raises` 中 `rollback_to="auto:tick:1"` → `rollback_to=2`（其余不变）。

- [ ] **Step 4: TestResumeLoop 调整**

`test_loop_runs_until_guard_opens` 中 `[c for c in mod.list_checkpoints() if c[2] == "auto"]` → `if c[2] == "tick"`（len == 4 不变）。

`test_resume_mid_loop_continues_state` 中 `rollback_to="auto:tick:1"` → `rollback_to=2`（快照 tick 2 = n=2 处；注释同步更新）。

- [ ] **Step 5: TestModuleSnapshotAPI 微调**

`test_checkpoint_rollback_to`（602-612 行）不变——manual 条目形状 `(tick, label, "manual")` 保持。`test_list_checkpoints_empty_before_run` 不变。其余不变。

- [ ] **Step 6: 运行 test_checkpoint.py 确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS（全绿）

- [ ] **Step 7: 提交**

```bash
git add module_harness/module.py module_harness/tests/test_checkpoint.py
git commit -m "feat(module): resume 精确 tick 回退 + 自动检查点退役 + list_checkpoints 显示 fired（D2/D3/D5）"
```

---

## Task 11: query_run_status 从 firings 读（D2，module_harness/status.py）

**Files:**
- Modify: `module_harness/status.py:23-35,67-90`
- Test: `module_harness/tests/test_run_status.py`

- [ ] **Step 1: 实现**

`module_harness/status.py` `ModuleStatus` 增加字段：

```python
@dataclass
class ModuleStatus:
    """Module 运行状态静态快照。"""

    module_id: str
    phase: str                 # idle/translating/reviewing/building/ready/running/done/aborted/cancelled
    status: str | None = None  # tickflow RunStatus（"running"/"idle"/...；无 DB 时为 None）
    tick: int | None = None    # 最新快照 tick（无 DB 时为 None）
    fireable: list[str] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)      # 最新快照本 tick fire 的节点
    outputs: dict[str, Any] = field(default_factory=dict)     # node → 最新输出
    node_states: dict[str, dict] = field(default_factory=dict)  # node → mutable state
    error: str | None = None
    updated_at: float = 0.0
```

`query_run_status` DB 段改为：

```python
    db_path = run_dir / "run.sqlite"
    if db_path.exists():
        try:
            from tickflow.persistence import SqliteBackend

            backend = SqliteBackend(db_path)
            try:
                tick = backend.latest_tick(module_id)
                if tick is not None:
                    snap = backend.load_snapshot(module_id, tick)
                    st.status = snap.get("status")
                    st.tick = snap.get("tick", tick)
                    st.fireable = list(snap.get("fireable", []))
                    st.fired = list(snap.get("fired", []))
                    # S3：快照不再含 edges/state——最新输出/状态从 firings
                    # 取（每节点最后一 firing，O(节点数)）。
                    latest = backend.latest_firings(module_id)
                    st.outputs = {
                        d["node"]: d.get("output") for d in latest if d.get("node")
                    }
                    st.node_states = {
                        d["node"]: d.get("mutable_state", {})
                        for d in latest if d.get("node")
                    }
            finally:
                backend.close()
        except Exception:
            log.exception("读取 run.sqlite 失败，降级为 phase-only")
    return st
```

- [ ] **Step 2: 改写 test_run_status.py**

`test_full_snapshot_query`（55-76 行）替换为：

```python
    def test_full_snapshot_query(self, tmp_path):
        """最小快照（无 edges/state）+ firings 行 → outputs/node_states 从 firings 读。"""
        run_dir = _write_status(tmp_path, phase="running", updated_at=100.0)
        backend = SqliteBackend(run_dir / "run.sqlite")
        from tickflow.state import NodeState
        backend.save_snapshot("mod_x", 2, {
            "tick": 2,
            "marking": {},
            "run_state": {"keep_records": True},
            "status": "running",
            "fireable": ["B"],
            "fired": ["A"],
        })
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="out1",
                                               mutable_state={"_prompt": "x"}))
        backend.save_firing("mod_x", NodeState(tick=2, node="A", output="out2",
                                               mutable_state={"_prompt": "x"}))
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.status == "running"
        assert st.tick == 2
        assert st.fireable == ["B"]
        assert st.fired == ["A"]
        assert st.outputs == {"A": "out2"}          # 每节点最后一 firing
        assert st.node_states == {"A": {"_prompt": "x"}}
```

`test_real_runner_snapshot_roundtrip` 增加 fired 断言（其余不变）：

```python
        assert st.tick is not None                 # 快照已 persist
        # 最新快照是空 tick（A 跑完后 tick 1 无 fireable）→ fired 为空，
        # 语义正确（fired = 该快照 tick 刚完成的节点列表）
        assert st.fired == []
        assert st.outputs == {"A": '{"ok": true}'}
```

`TestModulePhase.test_persist_mode_end_to_end_query`（230-240 行）不变——真实 runner 路径 outputs/node_states 语义等价（A 的最后一 firing 输出 `{"ok": true}`、mutable_state `{}`）。

新增测试（TestQueryRunStatus 内）：

```python
    def test_replayed_firings_keep_first(self, tmp_path):
        """restore-then-replay 的重复行在 latest_firings 中 keep-first。"""
        run_dir = _write_status(tmp_path, phase="done")
        backend = SqliteBackend(run_dir / "run.sqlite")
        from tickflow.state import NodeState
        backend.save_snapshot("mod_x", 2, {
            "tick": 2, "marking": {}, "run_state": {"keep_records": True},
            "status": "idle", "fireable": [], "fired": ["A"],
        })
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="orig"))
        backend.save_firing("mod_x", NodeState(tick=1, node="A", output="replay"))
        backend.close()

        st = query_run_status("mod_x", base_dir=tmp_path)
        assert st.outputs == {"A": "orig"}
```

- [ ] **Step 3: 运行确认通过**

Run: `python -m pytest module_harness/tests/test_run_status.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add module_harness/status.py module_harness/tests/test_run_status.py
git commit -m "feat(status): query_run_status 从 firings 读最新输出/状态 + fired 字段（D2）"
```

---

## Task 12: 文档更新

**Files:**
- Modify: `docs/progress/module-roadmap.md`、`docs/superpowers/specs/2026-08-06-snapshot-rollback-design.md`、`docs/superpowers/specs/2026-08-07-lightweight-snapshot-design.md`

- [ ] **Step 1: roadmap #5 行更新**

`docs/progress/module-roadmap.md` 第 48 行"快照/回滚封装"描述改为：

```
| **快照/回滚封装** — 每 tick 最小快照（剥离 records/edges/state，O(边数)，tickflow `_persist_tick` 落盘）+ 精确 tick 号回退 `resume(tick)` + 跨进程续跑 + 进程内 `snapshot()`/`restore()`/`checkpoint()`/`rollback_to()` + 兼容性校验（2 硬错误 + 3 警告）+ `list_checkpoints()` 显示 (tick, fired) | `ModuleInputStore` + `check_resume_compat` + `Module.resume` | `checkpoint.py`, `module.py` |
```

- [ ] **Step 2: 旧 spec 标注**

`docs/superpowers/specs/2026-08-06-snapshot-rollback-design.md` 文件头追加：

```markdown
> **退役标注（2026-08-06）**：`auto_checkpoints` 表已退役（冗余清理设计 S2）——
> 每 tick 快照由 tickflow `_persist_tick` 直接写入 snapshots 表（最小快照）。
> 本设计的自动检查点/环形保留部分不再适用。
```

`docs/superpowers/specs/2026-08-07-lightweight-snapshot-design.md` 文件头追加：

```markdown
> **范围扩展（2026-08-06）**：轻量快照进一步最小化——持久路径快照剥离
> `run_state.edges/fire_counts/state`（S3 死数据：restore 的 truncate_after
> 本就从 firings 重建）；`resume()` 改 tick 号回退；`list_checkpoints()` 显示
> (tick, fired)。详见 `2026-08-06-redundancy-cleanup-design.md`。
```

- [ ] **Step 3: 提交**

```bash
git add docs/progress/module-roadmap.md docs/superpowers/specs/2026-08-06-snapshot-rollback-design.md docs/superpowers/specs/2026-08-07-lightweight-snapshot-design.md
git commit -m "docs: 冗余清理落地标注（roadmap #5 + 旧 spec 退役说明）"
```

---

## Task 13: 全量回归 + 收尾

- [ ] **Step 1: 全量测试（排除 smoke——真实 LLM 调用，离线不可跑）**

Run: `python -m pytest module_harness/tests/ -q -m "not smoke"`
Expected: 全绿（预计 ~250+ tests）

- [ ] **Step 2: 手工核对快照落盘形状（一次性验证脚本）**

Run:

```bash
python - <<'EOF'
import asyncio, json, sqlite3, tempfile, os
from unittest.mock import MagicMock
from tickflow.ir import Edge, Graph, Node
from tickflow.registry import Registry
from tickflow.runner import Runner
from tickflow.persistence import SqliteBackend

d = tempfile.mkdtemp()
b = SqliteBackend(os.path.join(d, "t.sqlite"))
reg = Registry()
reg.body("e")(lambda view: {"ok": True})
g = Graph(nodes={"A": Node(name="A", is_start=True), "B": Node(name="B")},
          edges=[Edge("A", "B", None)])
r = Runner(g, reg, backend=b, session_id="s")
r.run_until_idle(max_ticks=10)
conn = sqlite3.connect(os.path.join(d, "t.sqlite"))
for (tick, data) in conn.execute("SELECT tick, data FROM snapshots ORDER BY tick"):
    s = json.loads(data)
    assert set(s["run_state"]) == {"keep_records"}, s["run_state"]
    assert "fired" in s
    print(tick, s["fired"], list(s["run_state"]))
b.close()
EOF
```

Expected: 每行输出 `tick [节点列表] ['keep_records']`（快照 tick 1: ['A']、tick 2: ['B']、tick 3: []）。

- [ ] **Step 3: 收尾提交（如有未提交改动）**

```bash
git status --short
git add -A
git commit -m "chore: 冗余清理收尾"
```

- [ ] **Step 4: 汇报**

向用户汇报：改动文件清单、每项冗余的消除方式、测试结果、tickflow 改动需随 sync 机制同步 Graph 仓库的清单（ir.py/checker.py/engine.py/async_runner.py/state.py/runner.py/persistence.py）。
