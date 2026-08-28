# tickflow 双层存储（内存窗口 + SQLite 全量）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 tickflow 的 `_edges` 窗口化（每节点最近 2 条）并把全部历史/审计/快照落盘到 SQLite，使内存占用与触发次数无关，同时保持 `resolve(latest)` 热路径零 I/O。

**Architecture:** 双层存储——内存层只保留"运行必要"数据（每节点窗口 2 条 + 当前 state），冷数据（全量 output、审计、快照）全部经 `Backend` 落盘。`RunState` 成为唯一状态容器（持有 backend 引用）：`record()` 排队、tick 末尾批量 flush；`resolve(index)` 先查窗口（含序号映射）再查库；`audit()`/`firings_of()` 按 backend 分派。`backend=None` 语义迁移为"默认临时 SqliteBackend（自动清理）"，显式 `NullBackend()` 表示快速模式。SpecModule 层新增 `Module(persist=...)` 两态开关与 `SubModule.mode` 类属性。

**Tech Stack:** Python 3.13、stdlib `sqlite3`（WAL）、`tempfile` + `weakref.finalize`（临时库生命周期）、pytest（含 pytest-asyncio）。

**Spec:** `docs/dev/superpowers/specs/2026-08-05-tickflow-storage-design.md`（下文简称"spec"）

---

## 0. 实现裁定（spec 未明示处的设计决策）

这些是 spec 留给实现者裁定的点，本计划固定如下（后续任务全部以此为据）：

1. **D3 落盘时机**：`record()` 把 `NodeState` **引用**排入 `_pending` 队列（不立即序列化），`Runner._persist_tick` 在 tick 末尾调用 `run_state.flush_firings()` 批量写库（一次事务）。原因：`edges_fired` 在 Phase B 才填充（`engine.py` Phase A 先 `record()` 后填），record 时序列化会丢字段；且 spec 性能表要求"按 tick 批量，一次事务"。engine 直接使用者（不经 Runner）在 record 检测到 tick 变化时自动 flush 前一批，队列有界。
2. **`keep_records` 语义**：`audit_log()` 仍受 `keep_records` 门控（False → 恒 `[]`，与现状一致）；**落盘与 `keep_records` 正交**（D11：嵌入模式同样落盘）。内存 `_records` 仅在 `keep_records=True` 且非持久 backend（NullBackend）时维护。
3. **`resolve(index)` 顺序**：先查窗口（`_fire_counts` 序号映射，含同 tick 刚写入的火，与现状顺序语义一致），窗口外再查库（持久）或返回 `Missing`（NullBackend 降级，D7 表）。
4. **审计读取（持久路径）**：`audit()` 查库 = `list_firings` 全量，按 `_audit_ceiling`（本 RunState 实例见到过的最高 tick，restore 时回卷）+ `(tick, node)` 去重（restore 后重放不产生重复记录，保证 `test_restore_replays_identically` 类断言成立）。
5. **`to_json`/`from_json` 携带审计**：持久路径下快照 records 为空，roundtrip 会丢审计——`to_json` 增加顶层 `"audit"` 键（快照格式不变），`from_json` 通过 `inject_audit()` 回灌（内存 `_records` + 写回自身临时库 + 提升 ceiling）。
6. **序号追踪**：新增 `RunState._fire_counts: dict[str, int]`（每节点累计触发数），快照新增可选键 `fire_counts`（旧快照无此键时按 edges 长度回退推导——旧快照 edges 是全量）。
7. **SqliteBackend 模式**：firings 表新增 `node` 列 + `(session_id, node, id)` 索引；旧库迁移用 `ALTER TABLE ADD COLUMN` + `json_extract` 回填（一次性的 4 行代码，非独立迁移框架）。
8. **默认 backend 生命周期**：`tempfile.mkstemp` 建库文件于系统临时目录，`weakref.finalize(self, _cleanup_temp_db, ...)` 在 Runner 被 GC 时 close 连接 + 删 `db/-wal/-shm` 三件套。顺带修复 `SqliteBackend.close()` 的双重 close 笔误。
9. **`firing_at` 的 None 歧义**：spec 固定"不存在返回 None"。output 恰为 None 的 firing 经 index 解析会被当作不存在（`Missing`）——文档化已知限制（LLM 产出为 str/对象，实践中不触发）。

## 仓库与文件结构

**Phase A — Graph 主仓库（`../Graph`，tickflow 唯一改动方）：**

| 文件 | 职责（变更） |
|------|-------------|
| `tickflow/persistence.py` | Backend 协议 + `firing_at`/`firings_of`；NullBackend 降级实现；JsonBackend 复用 `list_firings` 实现；SqliteBackend node 列 + 索引 + 迁移 + 冷查询 |
| `tickflow/state.py` | `RunState` 窗口化、`_fire_counts`、`_pending` 队列、`resolve` 分派、`audit`/`firings_of` 分派、`truncate_after` 库内重建、快照 `fire_counts` |
| `tickflow/runner.py` | 默认临时 backend + 自动 session_id + `weakref.finalize` 清理；`_persist_tick` 走 flush 通道；`to_json`/`from_json` 审计携带；`restore` 传递 backend |
| `tickflow/async_runner.py` | 零改动（共享 `_BaseRunner.__init__`/`_persist_tick`/`from_json`） |
| `tickflow/views.py` | 无改动（docstring 泛称 history，窗口仍是 history，不需要改） |
| `README.md` | 运行状态/持久化章节更新（`_edges` 窗口、查询分派表、默认 backend） |
| `tests/test_storage_window.py` | **新增**——spec §6 全部新测试 |
| `tests/test_checkpoints.py` | 适配 `test_checkpoint_requires_backend`（D6 行为增强：默认即有 backend） |

**Phase B — SpecModule（`../SpecModule`）：**

| 文件 | 职责（变更） |
|------|-------------|
| `tickflow/` | 从 Graph 仓库整目录同步（排除 `__pycache__`），独立 commit |
| `module_harness/module.py` | `persist: bool = True` 参数 + `_persist_dir()` + `_build_runner_async` 构造 backend/session |
| `module_harness/submodule.py` | `mode: Literal["persist", "fast"] = "persist"` 类属性 + run() 透传 + docstring |
| `module_harness/tests/test_storage_persist.py` | **新增**——spec §6 SpecModule 侧 4 项测试 |
| `AGENTS.md` | 架构规则 3：`_edges` 补 "windowed (last 2)"、`_records` 补 backend 落盘 |
| `docs/concepts/SpecModule.md` | 嵌入模式定位更新 + `.specmodule/runs/` 约定说明 |
| `.gitignore` | 追加 `.specmodule/` |

---

# Phase A — Graph 主仓库

> 所有 Graph 仓库命令的 cwd 均为 `../Graph`。

### Task A0: 基线确认

- [ ] **Step 1: 跑全量基线**

```bash
cd "../Graph" && python -m pytest tests -q
```

Expected: `163 passed`（若基线非绿，先修复再继续）。

---

### Task A1: Backend 协议扩展（`firing_at` / `firings_of`）

**Files:**
- Modify: `tickflow/persistence.py`
- Test: `tests/test_storage_window.py`（新建，本任务先放 3 个协议测试）

- [ ] **Step 1: 写失败测试**——新建 `tests/test_storage_window.py`：

```python
"""Dual-layer storage: memory window + SQLite full history (spec 2026-08-05)."""
from __future__ import annotations

import gc
from pathlib import Path

from tickflow import parse, Runner, Registry, SqliteBackend, JsonBackend
from tickflow.persistence import NullBackend
from tickflow.views import Missing


# --------------------------------------------------------------------------
# A1: Backend cold-query protocol
# --------------------------------------------------------------------------

def test_sqlite_firing_at_and_firings_of(tmp_path):
    be = SqliteBackend(tmp_path / "f.db")
    be.save_firings("s1", [
        {"tick": 1, "node": "A", "output": "a1"},
        {"tick": 2, "node": "B", "output": "b1"},
        {"tick": 3, "node": "A", "output": "a2"},
        {"tick": 4, "node": "A", "output": "a3"},
    ])
    assert be.firing_at("s1", "A", 1) == "a1"
    assert be.firing_at("s1", "A", 3) == "a3"
    assert be.firing_at("s1", "A", 4) is None      # only 3 fires
    assert be.firing_at("s1", "B", 1) == "b1"
    assert be.firing_at("s1", "nope", 1) is None
    assert be.firings_of("s1", "A") == [(1, "a1"), (3, "a2"), (4, "a3")]
    assert be.firings_of("s1", "nope") == []


def test_null_backend_cold_queries_degrade():
    be = NullBackend()
    be.save_firings("s1", [{"tick": 1, "node": "A", "output": "a1"}])
    assert be.firing_at("s1", "A", 1) is None      # D7: no cold history
    assert be.firings_of("s1", "A") == []


def test_json_backend_firing_at_and_firings_of(tmp_path):
    be = JsonBackend(tmp_path)
    be.save_firings("s1", [
        {"tick": 1, "node": "A", "output": "a1"},
        {"tick": 3, "node": "A", "output": "a2"},
        {"tick": 2, "node": "B", "output": "b1"},
    ])
    assert be.firing_at("s1", "A", 1) == "a1"
    assert be.firing_at("s1", "A", 2) == "a2"
    assert be.firing_at("s1", "A", 3) is None
    assert be.firings_of("s1", "A") == [(1, "a1"), (3, "a2")]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: FAIL——`AttributeError: 'SqliteBackend' object has no attribute 'firing_at'`。

- [ ] **Step 3: 实现**——`tickflow/persistence.py` 四处修改：

**(a)** `Backend` 协议内、`list_firings` 之后插入：

```python
    def firing_at(self, session_id: str, node: str, k: int) -> Any | None:
        """The output of *node*'s k-th firing (1-based) in this session,
        or None if it has fired fewer than k times.  Cold query backing
        ``resolve(A[k])`` for k outside the in-memory window (D8)."""
        ...

    def firings_of(self, session_id: str, node: str) -> list[tuple[int, Any]]:
        """All ``(tick, output)`` pairs for *node*, in tick order.  Cold
        query backing ``RunState.firings_of`` (D8)."""
        ...
```

**(b)** `NullBackend` 内、`list_firings` 之后插入（D7 降级）：

```python
    def firing_at(self, session_id: str, node: str, k: int) -> Any | None:
        return None  # NullBackend keeps no cold history (fast mode, D7)

    def firings_of(self, session_id: str, node: str) -> list[tuple[int, Any]]:
        return []  # NullBackend keeps no cold history (fast mode, D7)
```

**(c)** `JsonBackend` 内、`list_firings` 之后插入（复用 `list_firings`，spec D8 首选路径）：

```python
    def firing_at(self, session_id: str, node: str, k: int) -> Any | None:
        fs = [d for d in self.list_firings(session_id) if d.get("node") == node]
        if k < 1 or k > len(fs):
            return None
        return fs[k - 1].get("output")

    def firings_of(self, session_id: str, node: str) -> list[tuple[int, Any]]:
        return [
            (d["tick"], d.get("output"))
            for d in self.list_firings(session_id)
            if d.get("node") == node
        ]
```

**(d)** `SqliteBackend`——schema 加 `node` 列与索引（`_init_tables` 的 executescript 内，firings 建表与索引两处）：

```python
            CREATE TABLE IF NOT EXISTS firings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                tick       INTEGER NOT NULL,
                node       TEXT    NOT NULL,
                data       TEXT    NOT NULL
            );
```
```python
            CREATE INDEX IF NOT EXISTS idx_firings_node
                ON firings (session_id, node, id);
```

`_init_tables` 末尾（executescript 之后）追加旧库迁移：

```python
        # Migration for databases created before the `node` column existed.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(firings)")}
        if "node" not in cols:
            self._conn.execute("ALTER TABLE firings ADD COLUMN node TEXT")
            self._conn.execute(
                "UPDATE firings SET node = json_extract(data, '$.node') "
                "WHERE node IS NULL OR node = ''"
            )
            self._conn.commit()
```

`save_firing` 与 `save_firings` 的 INSERT 增加 node 列：

```python
    def save_firing(self, session_id: str, firing: Any) -> None:
        d = firing.to_json() if hasattr(firing, "to_json") else dict(firing)
        tick = d.get("tick", 0)
        node = str(d.get("node", ""))
        with self._lock:
            self._conn.execute(
                "INSERT INTO firings (session_id, tick, node, data) VALUES (?, ?, ?, ?)",
                (session_id, tick, node, json.dumps(d, default=_default)),
            )
            self._conn.commit()
```
```python
    def save_firings(self, session_id: str, firings: list) -> None:
        """Batch-insert all firings in a single transaction (one commit)."""
        if not firings:
            return
        rows = []
        for firing in firings:
            d = firing.to_json() if hasattr(firing, "to_json") else dict(firing)
            rows.append((session_id, d.get("tick", 0), str(d.get("node", "")),
                         json.dumps(d, default=_default)))
        with self._lock:
            self._conn.executemany(
                "INSERT INTO firings (session_id, tick, node, data) VALUES (?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
```

`list_firings` 之后插入冷查询实现：

```python
    def firing_at(self, session_id: str, node: str, k: int) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM firings WHERE session_id = ? AND node = ? "
                "ORDER BY id LIMIT 1 OFFSET ?",
                (session_id, node, k - 1),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]).get("output")

    def firings_of(self, session_id: str, node: str) -> list[tuple[int, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM firings WHERE session_id = ? AND node = ? "
                "ORDER BY id",
                (session_id, node),
            ).fetchall()
        out: list[tuple[int, Any]] = []
        for r in rows:
            d = json.loads(r[0])
            out.append((d["tick"], d.get("output")))
        return out
```

顺带修复 `close()` 的双重 close 笔误（删掉第二个 `self._conn.close()` 及其悬空 docstring）：

```python
    def close(self) -> None:
        with self._lock:
            self._conn.close()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: `3 passed`。再跑既有持久化测试确认协议扩展未破坏：

```bash
cd "../Graph" && python -m pytest tests/test_persistence.py -q
```

Expected: 全部通过（`test_sqlite_backend_*` 走新 schema；旧 `test_sqlite_backend_save_list_firings` 等 dict 调用含 `node` 键，`str(d.get("node",""))` 兜底）。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/persistence.py tests/test_storage_window.py && git commit -m "feat(persistence): add firing_at/firings_of cold-query protocol (D8)"
```

---

### Task A2: RunState 窗口化 + 落盘队列 + 序号追踪

**Files:**
- Modify: `tickflow/state.py`
- Test: `tests/test_storage_window.py`（追加 3 个窗口测试）

- [ ] **Step 1: 写失败测试**——`tests/test_storage_window.py` 追加（共享 helper 放在测试前）：

```python
# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _reg(limit: int = 3) -> Registry:
    r = Registry()
    r.body("seed_zero", lambda v: 0)

    @r.body("passthru")
    def _p(v):
        for _n, val in v.items():
            if val is not Missing:
                return val
        return None

    @r.body("incr")
    def _incr(v):
        return v.A.value + 1

    r.guard("cont_ltN", lambda v: v.B.value < limit)
    return r


def _loop_graph(r: Registry, limit: int = 3):
    return parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: passthru\nA.join: OR\n"
        "A-->B\nB.body: incr\nB--|cont_ltN|-->A",
        registry=r,
    )


# --------------------------------------------------------------------------
# A2: memory window
# --------------------------------------------------------------------------

def test_loop_window_bounded():
    r = _reg(limit=100)
    rn = Runner(_loop_graph(r, 100), r)
    rn.run_until_idle(max_ticks=500)
    assert len(rn.run_state._edges["A"]) <= 2
    assert len(rn.run_state._edges["B"]) <= 2
    assert len(rn.audit_log()) >= 100      # full trail still available


def test_linear_flow_window_bounded():
    r = _reg()
    g = parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: passthru\n"
        "A-->B\nB.body: passthru\nB-->C\nC.body: passthru",
        registry=r,
    )
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=50)
    for lst in rn.run_state._edges.values():
        assert len(lst) <= 2


def test_big_output_not_retained_in_memory():
    r = Registry()
    r.body("seed_zero", lambda v: 0)

    @r.body("big")
    def _big(v):
        return {"payload": "x" * 100_000}

    r.guard("always", lambda v: True)
    g = parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: big\nA.join: OR\n"
        "A--|always|-->A",
        registry=r,
    )
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=50)
    assert len(rn.run_state._edges["A"]) <= 2
    assert len(rn.audit_log()) >= 20       # every big firing is on disk
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: FAIL——`len(rn.run_state._edges["A"])` 为 100（无界累积）。

- [ ] **Step 3: 实现**——`tickflow/state.py`：

**(a)** 模块 docstring 中三层描述更新为四层（`_edges` 窗口语义、`_records` backend 分派、`fire_counts`）：

```python
"""Unified state recording: ``NodeState`` + ``RunState``.

``NodeState`` is the single source of truth for everything that happened to
a node at a given tick — inputs, output, edge propagation, status, and the
node's mutable state after the body ran.

``RunState`` manages all ``NodeState`` records and maintains four internal
layers with distinct responsibilities::

    _edges      — output index, always maintained, for ``resolve()``;
                  WINDOWED to the last two firings per node (memory bound
                  is independent of how many times a node fired)
    _fire_counts— per-node firing counters mapping window entries back to
                  ``A[k]`` ordinals
    _state      — current mutable state per node, always maintained, O(1)
    _records    — full audit trail in memory; ONLY when ``keep_records=True``
                  AND no persistent backend (NullBackend path)

With a persistent backend, every firing is queued by :meth:`record` and
flushed in a per-tick batch by :meth:`flush_firings`; the backend holds the
full history and :meth:`audit` / :meth:`firings_of` query it on demand.
"""
```

**(b)** `__init__` 增加 backend 参数与四个新字段：

```python
    def __init__(
        self,
        keep_records: bool = True,
        backend: Any = None,
        session_id: str | None = None,
        persistent: bool = False,
    ) -> None:
        # Layer 1: windowed output index for resolve() — always maintained.
        self._edges: dict[str, list[tuple[int, Any]]] = {}
        # Per-node firing counters (window → A[k] ordinal mapping).
        self._fire_counts: dict[str, int] = {}
        # Layer 2: current mutable state per node — always maintained.
        self._state: dict[str, dict[str, Any]] = {}
        # Layer 3: full audit records — only when keep_records AND not persistent.
        self._records: list[NodeState] = []
        self._keep_records = keep_records
        # -- cold storage (dual-layer) --
        self._backend = backend
        self._session_id = session_id
        self._persistent = persistent      # True: backend owns the full audit
        self._pending: list[NodeState] = []  # firings queued for the next flush
        self._audit_ceiling: int = -1        # highest tick this RunState saw
```

**(c)** `record()` 窗口化 + 排队（替换整个方法体）：

```python
    def record(self, ns: NodeState) -> None:
        """Record a node firing into all active layers.

        The ``_edges`` window keeps only the last two firings per node (D1);
        everything else is released to the backend via the pending queue
        (flushed per tick — D3).
        """
        entries = self._edges.setdefault(ns.node, [])
        entries.append((ns.tick, ns.output))
        if len(entries) > 2:          # window: keep the last two firings only
            del entries[0]
        self._fire_counts[ns.node] = self._fire_counts.get(ns.node, 0) + 1
        self._state[ns.node] = dict(ns.mutable_state)  # defensive copy
        self._audit_ceiling = max(self._audit_ceiling, ns.tick)
        if self._keep_records and not self._persistent:
            self._records.append(ns)          # in-memory audit (NullBackend path)
        if self._backend is not None and self._session_id is not None:
            # Persist is orthogonal to keep_records (D11).  Batch per tick:
            # flush the previous batch when the tick advances (bounds the
            # queue for engine-direct callers that never flush explicitly).
            if self._pending and self._pending[0].tick != ns.tick:
                self.flush_firings()
            self._pending.append(ns)
```

**(d)** `record()` 之后新增 flush 方法：

```python
    def flush_firings(self) -> None:
        """Persist the queued firings to the backend in one batch (D3)."""
        if not self._pending or self._backend is None or self._session_id is None:
            return
        batch, self._pending = self._pending, []
        self._backend.save_firings(self._session_id, batch)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: `6 passed`。**注意**：`audit_log()` 目前还走内存 `_records`（本任务未改 audit），`test_loop_window_bounded` 中 `len(rn.audit_log()) >= 100` 依赖 `keep_records=True` 默认值 + `_persistent` 尚为 False（runner 未传 backend，`persistent=False` → 内存 records 仍全量）——通过。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/state.py tests/test_storage_window.py && git commit -m "feat(state): windowed _edges (last 2) with fire counts + persist queue (D1/D3)"
```

---

### Task A3: `resolve()` 分派（index → 窗口/库，latest → 窗口）

**Files:**
- Modify: `tickflow/state.py`
- Test: `tests/test_storage_window.py`（追加 4 个测试）

- [ ] **Step 1: 写失败测试**——追加：

```python
# --------------------------------------------------------------------------
# A3: resolve() dispatch
# --------------------------------------------------------------------------

def _index_graph(r: Registry, k: str = "A[3]"):
    @r.body("track_k")
    def _track(v):
        return v.A.value

    return parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: passthru\nA.join: OR\n"
        "A-->B\nB.body: incr\nB--|cont_ltN|-->A\n"
        "A-->C\nC.inputs: %s\nC.body: track_k" % k,
        registry=r,
    )


def test_index_resolves_from_backend():
    r = _reg(limit=6)              # A fires 5 times (values 0..4)
    rn = Runner(_index_graph(r), r)   # default backend → persistent
    rn.run_until_idle(max_ticks=500)
    a_outputs = [f.output for f in rn.audit_log() if f.node == "A"]
    assert len(a_outputs) == 5
    c_outputs = [f.output for f in rn.audit_log() if f.node == "C"]
    assert c_outputs[-1] == a_outputs[2]   # A[3] = 3rd fire = 2


def test_index_outside_window_missing_with_null_backend():
    r = _reg(limit=6)
    rn = Runner(_index_graph(r), r, backend=NullBackend())
    rn.run_until_idle(max_ticks=500)
    c_outputs = [f.output for f in rn.audit_log() if f.node == "C"]
    assert c_outputs[-1] is None   # A[3] outside the 2-entry window → Missing


def test_and_or_join_no_same_tick_crosstalk():
    r = _reg()
    g = parse(
        "[seed]-->A\nseed.body: seed_zero\nA.body: passthru\n"
        "A-->C\nA-->D\nC-->D\n"
        "C.body: passthru\nD.join: OR\nD.inputs: C\nD.body: passthru",
        registry=r,
    )
    rn = Runner(g, r)
    rn.run_until_idle(max_ticks=50)
    d_outputs = [f.output for f in rn.audit_log() if f.node == "D"]
    # D fires at tick 1 alongside C (OR: A slot) — must NOT see C's same-tick
    # write (Missing → passthru None); at tick 2 it sees C's previous output.
    assert d_outputs[0] is None
    assert d_outputs[1] == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: `test_index_resolves_from_backend` FAIL（`resolve(index)` 仍读 `_edges` 全量——本阶段 `_edges` 已窗口化但 index 未接库，窗口外返回 Missing → `c_outputs[-1]` 为 None）；`test_index_outside_window_missing_with_null_backend` 通过（窗口化已生效）；`test_and_or_join_no_same_tick_crosstalk` 通过（语义未变，回归护栏）。

- [ ] **Step 3: 实现**——替换 `resolve()` 方法体（含 docstring 更新）：

```python
    def resolve(self, node: str, kind: str, k: int | None, t: int) -> Any:
        """Resolve a producer's output for a consumer firing at tick *t*.

        ``kind`` is ``"latest"`` (most recent fire with tick < t — memory
        window, O(1)) or ``"index"`` (the k-th fire overall, 1-based —
        memory window first, then the backend for older fires; window-external
        reads degrade to ``Missing`` on the NullBackend path, D2/D7).
        """
        if kind == "index":
            # Window first: the last two fires resolve from memory (O(1)),
            # including the current tick's just-recorded fire — preserving the
            # same-tick visibility of the old full-history behaviour.
            entries = self._edges.get(node, [])
            count = self._fire_counts.get(node, 0)
            if entries and k is not None and 1 <= k <= count:
                lo = count - len(entries) + 1
                if k >= lo:
                    return entries[k - lo][1]
            # Older fires: cold query, or explicit degradation.
            if (
                self._persistent
                and self._backend is not None
                and self._session_id is not None
            ):
                v = self._backend.firing_at(self._session_id, node, k or 0)
                return Missing if v is None else v
            return Missing
        # latest_before(t) — window scan (≤ 2 entries, O(1))
        last: tuple[int, Any] | None = None
        for tk, v in self._edges.get(node, []):
            if tk < t:
                last = (tk, v)
            else:
                break
        return last[1] if last is not None else Missing
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py tests/test_loop.py -q
```

Expected: 全部通过（`test_loop.py` 的 `test_index_policy_pins_specific_fire` 验证"窗口优先"路径：C 每轮读 `A[1]`，同 tick 经窗口命中、更早经库命中）。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/state.py tests/test_storage_window.py && git commit -m "feat(state): resolve() dispatch — latest via window, index via window+backend (D2/D7)"
```

---

### Task A4: `audit()` / `firings_of()` backend 分派

**Files:**
- Modify: `tickflow/state.py`
- Test: `tests/test_storage_window.py`（追加 2 个测试）

- [ ] **Step 1: 写失败测试**——追加：

```python
# --------------------------------------------------------------------------
# A4: audit / firings_of dispatch
# --------------------------------------------------------------------------

def test_audit_full_from_backend():
    r = _reg(limit=100)
    rn = Runner(_loop_graph(r, 100), r)   # default temp backend
    rn.run_until_idle(max_ticks=500)
    assert len(rn.audit_log()) >= 100     # full trail read from SQLite
    assert rn.run_state._records == []    # D4: no in-memory accumulation


def test_firings_of_dispatch_backend_vs_window():
    r = _reg(limit=6)
    g = _loop_graph(r, 6)
    rn = Runner(g, r)                      # persistent → full from backend
    rn.run_until_idle(max_ticks=500)
    assert len(rn.firings_of("A")) == 5
    rn2 = Runner(g, r, backend=NullBackend())
    rn2.run_until_idle(max_ticks=500)
    assert len(rn2.firings_of("A")) <= 2   # window (D7)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: FAIL——`rn.run_state._records == []` 不成立（audit 尚未分派，内存仍全量累积）；`len(rn.firings_of("A")) == 5` 不成立（firings_of 仍只返回窗口 ≤2 条；`rn2` 的窗口断言本就成立）。

- [ ] **Step 3: 实现**——`tickflow/state.py`：

**(a)** 文件头加日志器（audit 内 flush 的容错需要）：

```python
import logging
...
log = logging.getLogger(__name__)
```

（保持 `from __future__ import annotations` 为第一行。）

**(b)** 替换 `firings_of()` 与 `audit()`：

```python
    def firings_of(self, node: str) -> list[tuple[int, Any]]:
        """Return ``[(tick, output), ...]`` for *node*, in tick order.

        Persistent backend: full history from the database.  NullBackend
        path: the in-memory window (≤ 2 entries) — D7 degradation.
        """
        if (
            self._persistent
            and self._backend is not None
            and self._session_id is not None
        ):
            return self._backend.firings_of(self._session_id, node)
        return list(self._edges.get(node, []))
```

```python
    def audit(self) -> list[NodeState]:
        """Full audit log. Empty when ``keep_records=False``.

        Persistent path: query the backend (all firings with ``tick`` within
        this RunState's audit ceiling, deduplicated by ``(tick, node)`` so a
        restore-then-replay does not double-count replayed firings).  Memory
        path (NullBackend): ``_records`` — unchanged behaviour.
        """
        if not self._keep_records:
            return []
        if (
            self._persistent
            and self._backend is not None
            and self._session_id is not None
        ):
            try:
                self.flush_firings()
            except Exception:
                log.exception("flush_firings failed; swallowed")
            out: list[NodeState] = []
            seen: set[tuple[int, str]] = set()
            for d in self._backend.list_firings(self._session_id):
                if d.get("tick", 0) > self._audit_ceiling:
                    continue
                key = (d["tick"], d["node"])
                if key in seen:
                    continue          # replayed firing — keep the first
                seen.add(key)
                out.append(NodeState.from_json(d))
            return out
        return list(self._records)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: `12 passed`。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/state.py tests/test_storage_window.py && git commit -m "feat(state): audit/firings_of backend dispatch with ceiling+dedup (D4/D7)"
```

---

### Task A5: 快照/恢复/截断（`truncate_after`、`fire_counts`、`restore` 传 backend）

**Files:**
- Modify: `tickflow/state.py`, `tickflow/runner.py`
- Test: `tests/test_storage_window.py`（追加 2 个测试）

- [ ] **Step 1: 写失败测试**——追加：

```python
# --------------------------------------------------------------------------
# A5: snapshot / restore / truncate
# --------------------------------------------------------------------------

def test_restore_then_index_resolves_from_backend(tmp_path):
    r = _reg(limit=6)
    be = SqliteBackend(tmp_path / "restore.db")
    rn = Runner(_index_graph(r), r, backend=be, session_id="s1")
    rn.run_until_idle(max_ticks=500, pause_at={5})
    snap = rn.snapshot()
    rn.run_until_idle(max_ticks=500)
    rn.restore(snap)
    rn.run_until_idle(max_ticks=500)
    c_outputs = [f.output for f in rn.audit_log() if f.node == "C"]
    assert c_outputs[-1] == 2      # A[3] still resolves after restore (firings on disk)


def test_state_rebuilt_from_backend_after_restore(tmp_path):
    r = Registry()

    @r.body("counter")
    def _counter(v):
        v.state["attempts"] = v.state.get("attempts", 0) + 1
        return v.state["attempts"]

    @r.guard("under_three")
    def _under3(v):
        return v.state.get("attempts", 0) < 3

    # 与 test_node_state.py::test_state_driven_loop_terminates 同构（已知可靠模式）
    g = parse(
        "[A]-->B\nB.body: counter\nB--|under_three|-->B\nB.join: OR",
        registry=r,
    )
    be = SqliteBackend(tmp_path / "state.db")
    rn = Runner(g, r, backend=be, session_id="s1")
    rn.run_until_idle(max_ticks=50, pause_at={3})
    snap = rn.snapshot()
    rn.run_until_idle(max_ticks=50)
    rn.restore(snap)
    assert "B" in rn.run_state.all_mutable_states()   # D5: state rebuilt
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: `test_restore_then_index_resolves_from_backend` FAIL（restore 后新 RunState 未接 backend → index 走窗口，`A[3]` 窗口外 → Missing）；`test_state_rebuilt_from_backend_after_restore` FAIL（`truncate_after` 用空 `_records` 清空 `_state`）。

- [ ] **Step 3: 实现**——`tickflow/state.py` 三处 + `tickflow/runner.py` 一处：

**(a)** `to_snapshot_data()` 增加 `fire_counts`（格式其余不变，向后兼容；旧代码读快照时忽略未知键）：

```python
        data: dict[str, Any] = {
            "edges": {
                n: [[t, _jsonable(v)] for (t, v) in lst]
                for n, lst in self._edges.items()
            },
            "fire_counts": dict(self._fire_counts),
            "state": {n: dict(s) for n, s in self._state.items()},
            "keep_records": self._keep_records,
        }
```

**(b)** `from_snapshot_data()` 接受 backend 参数并重建序号（旧快照无 `fire_counts` 键 → 按 edges 长度推导，旧快照 edges 是全量）：

```python
    @classmethod
    def from_snapshot_data(
        cls,
        d: dict,
        backend: Any = None,
        session_id: str | None = None,
        persistent: bool = False,
    ) -> "RunState":
        """Reconstruct from ``to_snapshot_data()`` output."""
        keep_records = d.get("keep_records", True)
        rs = cls(
            keep_records=keep_records,
            backend=backend,
            session_id=session_id,
            persistent=persistent,
        )
        for n, lst in d.get("edges", d.get("outputs", {})).items():
            rs._edges[n] = [(int(t), v) for (t, v) in lst]
        counts = d.get("fire_counts")
        if counts:
            rs._fire_counts = {n: int(c) for n, c in counts.items()}
        else:
            # Legacy snapshot: edges held the FULL history — one entry per fire.
            rs._fire_counts = {n: len(lst) for n, lst in rs._edges.items()}
        for n, s in d.get("state", {}).items():
            rs._state[n] = dict(s)
        for rec in d.get("records", []):
            rs._records.append(NodeState.from_json(rec))
        if rs._records:
            rs._audit_ceiling = max(ns.tick for ns in rs._records)
        return rs
```

**(c)** `truncate_after()` 持久路径从库重建 `_state`，并回卷序号与 ceiling（替换整个方法体）：

```python
    def truncate_after(self, tick: int) -> None:
        """Drop all records with ``tick > tick``.

        Persistent path: the on-disk firings are retained (audit history,
        D5); the window, fire counts and ``_state`` rewind to *tick*, with
        ``_state`` rebuilt from the last firing ≤ *tick* per node in the DB.
        Memory path: prune ``_records`` and rebuild ``_state`` from them.
        """
        for n in list(self._edges):
            kept = [(t, v) for (t, v) in self._edges[n] if t <= tick]
            if kept:
                pruned = len(self._edges[n]) - len(kept)
                self._edges[n] = kept
                if n in self._fire_counts:
                    self._fire_counts[n] -= pruned
            else:
                del self._edges[n]
                self._state.pop(n, None)
                self._fire_counts.pop(n, None)
        self._audit_ceiling = min(self._audit_ceiling, tick)
        if (
            self._persistent
            and self._backend is not None
            and self._session_id is not None
        ):
            rows = self._backend.list_firings(self._session_id)
            if rows:
                latest: dict[str, dict] = {}
                for d in rows:
                    if d.get("tick", 0) > tick:
                        continue
                    node = d["node"]
                    if node not in latest or d["tick"] > latest[node]["tick"]:
                        latest[node] = d
                self._state = {
                    n: d.get("mutable_state", {}) for n, d in latest.items()
                }
            # No persisted data: keep _state as reconstructed (restore path —
            # the snapshot's state is already correct for the truncation tick).
            self._records = []
            return
        if self._keep_records:
            self._records = [ns for ns in self._records if ns.tick <= tick]
            self._state.clear()
            for ns in reversed(self._records):
                if ns.node not in self._state:
                    self._state[ns.node] = ns.mutable_state
        else:
            self._records = []
```

**(d)** `keep_nodes()` 同步清理 `_fire_counts`：

```python
    def keep_nodes(self, node_names: set[str]) -> None:
        """Drop state for nodes not in *node_names*."""
        for n in list(self._edges):
            if n not in node_names:
                del self._edges[n]
                self._state.pop(n, None)
                self._fire_counts.pop(n, None)
        self._records = [ns for ns in self._records if ns.node in node_names]
```

**(e)** `tickflow/runner.py` 的 `restore()` 把 backend 传入重建的 RunState（替换 `run_snap` 分支）：

```python
        run_snap = snap.get("run_state", {})
        if run_snap:
            self.run_state = RunState.from_snapshot_data(
                run_snap,
                backend=self._backend,
                session_id=self._session_id,
                persistent=not isinstance(self._backend, NullBackend),
            )
        else:
            # Legacy snapshot without run_state key.
            h_data = snap.get("history", {})
            rs = RunState(keep_records=False)
            for n, lst in h_data.items():
                for t, v in lst:
                    if int(t) < self.tick_count:
                        rs._edges.setdefault(n, []).append((int(t), v))
            self.run_state = rs
```

（`runner.py` 顶部 import 增加 `from .persistence import NullBackend`。）

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py tests/test_snapshot.py tests/test_node_state.py -q
```

Expected: 全部通过。`test_snapshot.py::test_restore_replays_identically` 是关键验证：restore 后 audit 经 ceiling 回卷为空（`all(f.tick < snap["tick"])` 成立），重放后 DB 去重保证 `replayed == final`。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/state.py tickflow/runner.py tests/test_storage_window.py && git commit -m "feat(state): snapshot fire_counts, restore carries backend, truncate rebuilds state from DB (D5)"
```

---

### Task A6: 默认 backend（临时 SqliteBackend + 生命周期清理）+ `_persist_tick` 走 flush

**Files:**
- Modify: `tickflow/runner.py`
- Test: `tests/test_storage_window.py`（追加 2 个测试）、`tests/test_checkpoints.py`（适配 1 个测试）

- [ ] **Step 1: 写失败测试**——`tests/test_storage_window.py` 追加：

```python
# --------------------------------------------------------------------------
# A6: default backend lifecycle (D6)
# --------------------------------------------------------------------------

def test_default_backend_temp_db_cleaned_up():
    r = _reg()
    rn = Runner(_loop_graph(r), r)      # no backend → temp SqliteBackend
    rn.run_until_idle(max_ticks=50)
    path = Path(rn._temp_db_path)
    assert path.exists()                # lives during the run
    del rn
    gc.collect()
    assert not path.exists()            # cleaned up with the Runner


def test_explicit_backend_file_persists(tmp_path):
    r = _reg()
    be = SqliteBackend(tmp_path / "run.db")
    rn = Runner(_loop_graph(r), r, backend=be, session_id="s1")
    rn.run_until_idle(max_ticks=50)
    del rn
    gc.collect()
    assert (tmp_path / "run.db").exists()
```

`tests/test_checkpoints.py`：删除 `test_checkpoint_requires_backend`（D6 后默认即有 backend，`RuntimeError` 分支不可达），替换为：

```python
def test_checkpoint_works_with_default_backend():
    """D6: the default temp backend makes named checkpoints available without
    any explicit backend/session_id."""
    r = _reg()
    rn = Runner(_loop_graph(r), r)
    rn.run_until_idle(max_ticks=20, pause_at={3})
    rn.checkpoint("cp1")
    assert ("cp1", 3) in rn.list_checkpoints()
    rn.run_until_idle(max_ticks=20)
    rn.rollback_to("cp1")
    assert rn.tick_count == 3
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: FAIL——`test_default_backend_temp_db_cleaned_up`（`Runner` 无 `_temp_db_path` 属性，且 backend=None 不落盘）。

- [ ] **Step 3: 实现**——`tickflow/runner.py`：

**(a)** 顶部 import 增加：

```python
import os
import tempfile
import uuid
import weakref
```

（`import` 组按现有顺序排入；`from .persistence import NullBackend, SqliteBackend` 加入相对导入组。）

**(b)** `_BaseRunner` 类前（`log = logging.getLogger(__name__)` 之后）新增两个模块级 helper：

```python
def _make_temp_backend() -> tuple[SqliteBackend, str]:
    """Create a temp-file SqliteBackend in the system temp dir (D6).

    The file is removed when the Runner is garbage-collected (see
    :func:`_cleanup_temp_db`); explicit ``backend=`` callers keep their file.
    """
    fd, path = tempfile.mkstemp(prefix="tickflow_", suffix=".sqlite")
    os.close(fd)
    return SqliteBackend(path), path


def _cleanup_temp_db(backend: SqliteBackend, path: str) -> None:
    """Close the temp backend and unlink the DB file (+ WAL/SHM siblings)."""
    try:
        backend.close()
    except Exception:
        log.exception("closing temp backend failed; swallowed")
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("unlink %r failed; swallowed", p)
```

**(c)** `_BaseRunner.__init__` 中替换 backend/session/run_state 三行（在 `self.status` 赋值之后）：

```python
        if backend is None:
            # D6: default = temp SqliteBackend, cleaned up with the Runner.
            backend, self._temp_db_path = _make_temp_backend()
            weakref.finalize(self, _cleanup_temp_db, backend, self._temp_db_path)
        else:
            self._temp_db_path = None
        self._backend = backend
        self._session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        persistent = not isinstance(backend, NullBackend)
        self.run_state: RunState = RunState(
            keep_records=keep_records,
            backend=backend,
            session_id=self._session_id,
            persistent=persistent,
        )
```

（删除原来的 `self.run_state: RunState = RunState(keep_records=keep_records)`、`self._backend = backend`、`self._session_id = session_id` 三行。）

**(d)** `_persist_tick()` 改为走 flush 通道（firings 由 `record()` 排队，tick 末尾一次事务落盘）：

```python
    def _persist_tick(self, firings: list[NodeState]) -> None:
        """Persist this tick's queued firings + snapshot to the backend."""
        if self._backend is None or self._session_id is None:
            return
        try:
            self.run_state.flush_firings()
            self._backend.save_snapshot(self._session_id, self.tick_count, self.snapshot())
        except Exception:
            log.exception("backend persistence failed; swallowed")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py tests/test_checkpoints.py tests/test_audit_switch.py tests/test_persistence.py -q
```

Expected: 全部通过。其中 `test_persistence.py::test_runner_without_backend_no_persistence` 仍通过——临时库在系统临时目录，`tmp_path` 为空。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/runner.py tests/test_storage_window.py tests/test_checkpoints.py && git commit -m "feat(runner): default temp SqliteBackend with lifecycle cleanup (D6); persist via flush queue"
```

---

### Task A7: `to_json`/`from_json` 携带审计

**Files:**
- Modify: `tickflow/state.py`, `tickflow/runner.py`
- Test: `tests/test_storage_window.py`（追加 1 个测试）

- [ ] **Step 1: 写失败测试**——追加：

```python
# --------------------------------------------------------------------------
# A7: to_json / from_json audit roundtrip
# --------------------------------------------------------------------------

def test_to_json_from_json_roundtrip_audit():
    r = _reg(limit=6)
    rn = Runner(_loop_graph(r, 6), r)      # default temp backend
    rn.run_until_idle(max_ticks=500)
    s = rn.to_json()
    rn2 = Runner.from_json(s, _loop_graph(_reg(6), 6), _reg(6))
    assert rn2.tick_count == rn.tick_count
    assert [(f.tick, f.node, f.output) for f in rn2.audit_log()] == \
           [(f.tick, f.node, f.output) for f in rn.audit_log()]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py -q
```

Expected: FAIL——`rn2.audit_log()` 为空（持久路径下快照 records 为空，from_json 重建的 RunState 无审计来源）。

- [ ] **Step 3: 实现**——`tickflow/state.py` 增加 `inject_audit`（`keep_nodes` 之后）：

```python
    def inject_audit(self, records: list[NodeState]) -> None:
        """Restore an audit trail captured by ``Runner.to_json`` (from_json).

        Mirrors the records into ``_records`` (memory path) and writes them
        to the backend when one is attached (the DB is the audit source of
        truth in the persistent path), raising the audit ceiling so
        :meth:`audit` sees them immediately.
        """
        if not records:
            return
        self._records = list(records)
        self._audit_ceiling = max(ns.tick for ns in records)
        if self._backend is not None and self._session_id is not None:
            self._backend.save_firings(self._session_id, records)
```

`tickflow/runner.py`：`to_json()` 增加顶层 `"audit"` 键（快照格式不变）：

```python
    def to_json(self) -> str:
        """Full state as a single JSON string.  The audit trail lives under
        ``snapshot.run_state.records`` (memory path) or is carried in the
        top-level ``audit`` key so ``from_json`` restores it for
        backend-backed runs (persistent path keeps no in-memory records)."""
        return json.dumps({
            "snapshot": self.snapshot(),
            "audit": [ns.to_json() for ns in self.audit_log()],
        }, indent=2, default=_jsonable)
```

`from_json()` 回灌审计：

```python
    @classmethod
    def from_json(cls, s: str, graph: Graph, registry: Registry | None = None) -> "Runner":
        """Reconstruct a Runner from a prior :meth:`to_json` dump."""
        d = json.loads(s)
        r = cls(graph, registry, strict_deadlock=False)
        r.restore(d["snapshot"])
        r.run_state.inject_audit([NodeState.from_json(a) for a in d.get("audit", [])])
        return r
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "../Graph" && python -m pytest tests/test_storage_window.py tests/test_snapshot.py tests/test_audit_switch.py -q
```

Expected: 全部通过（含既有 `test_to_json_from_json_roundtrip`、`test_async_runner_to_json_roundtrip`、`test_keep_records_false_to_json_has_empty_audit`——`keep_records=False` 时 audit 为空列表，`records` 键依旧不出现）。

- [ ] **Step 5: Commit**

```bash
cd "../Graph" && git add tickflow/state.py tickflow/runner.py tests/test_storage_window.py && git commit -m "feat(runner): to_json/from_json carry the audit trail (inject_audit)"
```

---

### Task A8: README 更新 + Graph 全量回归

**Files:**
- Modify: `README.md`
- Test: 全量 `tests/`

- [ ] **Step 1: 更新 `README.md`**（约 79-100 行的状态章节）——把三行层表替换为：

```markdown
    _edges   dict[node, list[(tick, output)]]    windowed (last 2 fires/node), for resolve()
    _state   dict[node, dict]                    current mutable state per node, O(1)
    _records list[NodeState]                     audit in memory: keep_records AND no persistent backend
```

对应 bullet（`**`_edges`**`、`**`_records`**`、查询表）替换为：

```markdown
- **`_edges`** — windowed fast-lookup index for `resolve()`: only the last two
  firings per node stay in memory; older firings live in the backend. Memory
  usage is O(nodes × 2 × output size), independent of how many times a node fired.
- **`_records`** — full `NodeState` records in memory. Maintained only when
  `keep_records=True` **and** no persistent backend. With a backend (the
  default), the audit lives on disk and `audit()` queries it.

| Query | Source (persistent backend) | Source (NullBackend) |
|-------|-----------------------------|----------------------|
| `resolve(latest)` — input resolution | `_edges` window (O(1), zero I/O) | `_edges` window (same) |
| `resolve(index)` — k-th fire | window first, then `backend.firing_at` | window; outside → `Missing` |
| `firings_of()` — output history | `backend.firings_of` (full) | `_edges` window |
| `audit()` — full audit log | `backend.list_firings` | `_records` (keep_records) |
| `to_snapshot_data()` — snapshot | window + state (+ `fire_counts`) | same |
```

在 `keep_records` 段落（原 124-128 行附近）之后补默认 backend 说明：

```markdown
By default (`backend=None`) the Runner creates a temporary SQLite backend in
the system temp dir, auto-generates a `session_id`, and removes the database
file when the Runner is garbage-collected — so persistence, audit, checkpoints
and `A[k]` index reads work out of the box. Pass `NullBackend()` explicitly
for a zero-I/O in-memory run, or a concrete backend for a persistent one.
```

- [ ] **Step 2: 全量回归**

```bash
cd "../Graph" && python -m pytest tests -q
```

Expected: 全部通过。若有个别失败：按 spec §6 适配表处理——`_edges` 全量断言改为窗口断言或改走 `audit_log()`/`firings_of`；`test_restore_truncates_history` 的 `any(t >= snap["tick"])` 在窗口下仍成立（窗口最近两条产生于快照后），若失败检查 `truncate_after` 的序号回卷。

- [ ] **Step 3: Commit**

```bash
cd "../Graph" && git add README.md && git commit -m "docs: update state/persistence section for dual-layer storage (D6/D7)"
```

---

# Phase B — SpecModule

> 所有 SpecModule 命令的 cwd 均为 `../SpecModule`。

### Task B1: 同步 tickflow 到 SpecModule

- [ ] **Step 1: 整目录同步（排除 `__pycache__`）**

```bash
cd "../SpecModule" && rm -rf tickflow/__pycache__ && cp -r "../Graph\tickflow" tickflow_tmp && rm -rf tickflow && mv tickflow_tmp tickflow && rm -rf tickflow/__pycache__
```

- [ ] **Step 2: 验证无差异（README.md 除外，SpecModule 独有）**

```bash
cd "../SpecModule" && diff -rq "../Graph\tickflow" tickflow --exclude=__pycache__ --exclude=README.md
```

Expected: 无输出（无差异）。

- [ ] **Step 3: 快速冒烟**

```bash
cd "../SpecModule" && python -m pytest module_harness/tests/test_module.py -q
```

Expected: `13 passed`（tickflow 同步后既有 module 测试不破坏）。

- [ ] **Step 4: Commit**

```bash
cd "../SpecModule" && git add tickflow && git commit -m "chore: sync tickflow from Graph repo (dual-layer storage: memory window + SQLite)"
```

---

### Task B2: `Module(persist=...)` 两态开关

**Files:**
- Modify: `module_harness/module.py`

- [ ] **Step 1: 实现**——`module_harness/module.py` 三处修改：

**(a)** import 区（`from tickflow.async_runner import AsyncRunner` 之后）：

```python
from pathlib import Path

from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend, SqliteBackend
```

**(b)** 模块级 helper（`Module` 类前）：

```python
def _persist_dir(module_id: str) -> Path:
    """``<工作目录>/.specmodule/runs/<run_id>/run.sqlite``（D9）。

    run_id = module_id：一个任务一次运行一个子目录、一个独立 SQLite 数据库。
    """
    return Path.cwd() / ".specmodule" / "runs" / module_id / "run.sqlite"
```

**(c)** `Module.__init__` 签名加 `persist: bool = True`（`keep_records` 之后），并存储：

```python
        keep_records: bool = True,
        persist: bool = True,
    ) -> None:
```
```python
        self.keep_records = keep_records
        # True（默认）：构造 .specmodule/runs/<run_id>/run.sqlite 持久 backend（D9）
        # False：快速模式——NullBackend 全内存，零落盘零 I/O（D7 语义正式化）
        self.persist = persist
```

**(d)** `_build_runner_async()` 末尾构造 backend（替换 return 一行）：

```python
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist, spec=self.spec)
        backend = (
            SqliteBackend(_persist_dir(self.module_id))
            if self.persist
            else NullBackend()
        )
        return AsyncRunner(
            graph,
            registry=reg,
            keep_records=self.keep_records,
            backend=backend,
            session_id=self.module_id,
        )
```

- [ ] **Step 2: 冒烟**

```bash
cd "../SpecModule" && python -m pytest module_harness/tests/test_module.py -q
```

Expected: `13 passed`（默认 persist=True，测试在仓库根目录生成 `.specmodule/runs/<module_id>/run.sqlite`——B5 加 `.gitignore` 收纳）。

- [ ] **Step 3: Commit**

```bash
cd "../SpecModule" && git add module_harness/module.py && git commit -m "feat(module): persist two-state switch — default .specmodule/runs/<run_id>/run.sqlite (D9/D11)"
```

---

### Task B3: `SubModule.mode` 类属性

**Files:**
- Modify: `module_harness/submodule.py`

- [ ] **Step 1: 实现**——两处修改：

**(a)** 类属性声明（`tasklist: Tasklist | None = None` 之后）：

```python
    tasklist: Tasklist | None = None
    mode: Literal["persist", "fast"] = "persist"
    # 发布者声明轻量特性："fast" = 快速模式（NullBackend 全内存，零落盘零 I/O，
    # D11）；默认 "persist" 落盘到 .specmodule/runs/<run_id>/（D9）。
```

（文件头 import 增加 `from typing import Literal`。）

**(b)** `run()` 中构造 Module 时透传（`keep_records=audit` 之后）：

```python
            keep_records=audit,
            persist=(self.mode != "fast"),
        )
```

`run()` docstring 的 audit 行补充落盘语义：

```python
        - audit=False（默认）：嵌入模式，EventBus.null() + keep_records=False；
          注意：除非 mode="fast"，嵌入模式同样落盘（内存不保留 + 落盘，
          完整历史在 .specmodule/runs/ 可查，D11）
```

- [ ] **Step 2: 冒烟**

```bash
cd "../SpecModule" && python -m pytest module_harness/tests/test_submodule.py -q
```

Expected: 全部通过（既有 SubModule 测试默认 mode="persist"，在仓库根目录生成 `.specmodule/runs/<name>_<uuid>/`——B5 的 `.gitignore` 收纳）。

- [ ] **Step 3: Commit**

```bash
cd "../SpecModule" && git add module_harness/submodule.py && git commit -m "feat(submodule): mode Literal['persist','fast'] class attribute (D11)"
```

---

### Task B4: SpecModule 侧持久化测试

**Files:**
- Create: `module_harness/tests/test_storage_persist.py`

- [ ] **Step 1: 写测试**——新建 `module_harness/tests/test_storage_persist.py`：

```python
"""Module 持久化开关测试（spec D9/D11）：persist 两态 + SubModule mode。"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from module_harness.events import EventBus
from module_harness.module import Module
from module_harness.registry import HarnessRegistry
from module_harness.spec import SpecSchema, TaskDefinition, Tasklist
from module_harness.submodule import SubModule, script


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.complete = AsyncMock()
    return client


def _script_reg(llm_client):
    reg = HarnessRegistry(llm_client=llm_client, event_bus=EventBus())

    @reg.script("echo")
    def echo(view):
        return {"ok": True}

    return reg


def _script_tasklist():
    return Tasklist(
        tasks={"A": TaskDefinition(type="script", script="echo")},
        flow="[A]",
    )


def _run_db(root, module_id):
    return root / ".specmodule" / "runs" / module_id / "run.sqlite"


def _firing_count(db_path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM firings").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_persist_default_creates_run_db(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = _script_reg(mock_llm)
    mod = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg,
        review_harness=None,
        module_id="persist_mod",
    )
    await mod.run(max_ticks=10)
    db = _run_db(tmp_path, "persist_mod")
    assert db.exists()
    assert _firing_count(db) >= 1


@pytest.mark.asyncio
async def test_persist_false_fast_mode_no_files(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = _script_reg(mock_llm)
    mod = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg,
        review_harness=None,
        module_id="fast_mod",
        persist=False,
    )
    firings = await mod.run(max_ticks=10)
    assert len(firings) >= 1
    assert not (tmp_path / ".specmodule").exists()   # 快速模式零残留
    # 结果与持久模式一致
    reg2 = _script_reg(mock_llm)
    mod2 = Module(
        spec={"x": 1},
        tasklist=_script_tasklist(),
        llm_client=mock_llm,
        registry=reg2,
        review_harness=None,
        module_id="persist_mod2",
    )
    firings2 = await mod2.run(max_ticks=10)
    assert [(f.node, f.output) for f in firings] == \
           [(f.node, f.output) for f in firings2]


class Dig(SubModule):
    """固定 script tasklist 的轻量子模块（无 LLM 调用）。"""

    name = "dig"
    spec_schema = SpecSchema()
    tasklist = _script_tasklist()

    @script("echo")
    def echo(view):
        return {"ok": True}


@pytest.mark.asyncio
async def test_submodule_each_run_own_dir(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # audit=False（嵌入模式）同样落盘（D11），且每次 run 独立 run_id 目录
    await Dig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    await Dig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    runs_dir = tmp_path / ".specmodule" / "runs"
    dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(dirs) == 2
    for d in dirs:
        assert (d / "run.sqlite").exists()


class FastDig(Dig):
    mode = "fast"


@pytest.mark.asyncio
async def test_submodule_mode_fast_no_persist(tmp_path, mock_llm, monkeypatch):
    monkeypatch.chdir(tmp_path)
    await FastDig(llm_client=mock_llm).run({"x": 1}, max_ticks=10)
    assert not (tmp_path / ".specmodule").exists()
```

- [ ] **Step 2: 跑测试确认通过**

```bash
cd "../SpecModule" && python -m pytest module_harness/tests/test_storage_persist.py -q
```

Expected: `4 passed`。

- [ ] **Step 3: Commit**

```bash
cd "../SpecModule" && git add module_harness/tests/test_storage_persist.py && git commit -m "test: persist two-state + SubModule run dirs + fast mode (D9/D11)"
```

---

### Task B5: 文档更新（AGENTS.md / docs/concepts/SpecModule.md / .gitignore）

**Files:**
- Modify: `AGENTS.md`, `docs/concepts/SpecModule.md`, `.gitignore`

- [ ] **Step 1: `AGENTS.md`**——架构规则 3 替换为：

```markdown
3. **Single source of truth:** `RunState` (tickflow) is the sole runtime state container. Three layers: `_edges` (fast input resolution, windowed to last 2 firings per node), `_state` (per-node mutable state), `_records` (full audit, gated by `keep_records`; persisted via backend when one is attached). Never create parallel state tracking.
```

- [ ] **Step 2: `docs/concepts/SpecModule.md`**——submodule 段落（约 52 行）替换为：

```markdown
submodule是tasklist固定、spec强模板化的一个module，其spec和tasklist是固定的，不能修改。嵌入模式（audit=False）内存不保留审计，但记录仍落盘（`.specmodule/runs/<run_id>/run.sqlite`，完整历史可查）；轻量任务用 `mode = "fast"` 全内存零落盘。形成一个特定输入得到特定输出的固定的“箱子”。
```

"状态记录与控制——快照与回滚" 段落末尾追加持久化约定：

```markdown
**持久化约定**：默认每个 `Module.run` 在 `<工作目录>/.specmodule/runs/<module_id>/run.sqlite` 生成独立 SQLite 数据库（run_id = module_id；SubModule 每次 run() 生成 `{name}_{uuid[:6]}`，互不干扰）。`Module(persist=False)` 或 `SubModule mode="fast"` 关闭落盘（全内存快速模式，无 `.specmodule` 残留）。敏感数据注意：默认落盘意味着 LLM 产出（代码、prompt）持久化到工作目录——persist=False 即关闭开关。
```

- [ ] **Step 3: `.gitignore`** 追加：

```
.specmodule/
```

- [ ] **Step 4: Commit**

```bash
cd "../SpecModule" && git add AGENTS.md docs/concepts/SpecModule.md .gitignore && git commit -m "docs: embedded-mode persistence positioning + .specmodule/runs convention (D9/D11)"
```

---

### Task B6: SpecModule 全量回归

- [ ] **Step 1: 非 smoke 全量回归**

```bash
cd "../SpecModule" && python -m pytest module_harness/tests -m "not smoke" -q
```

Expected: `196 passed, 15 deselected`（192 既有 + 4 新增）。

- [ ] **Step 2: Graph 侧回归（确认无相互影响）**

```bash
cd "../Graph" && python -m pytest tests -q
```

Expected: 全部通过（若 test_checkpoints 适配后数量略变，以通过为准）。

- [ ] **Step 3: 清理仓库根目录测试残留**

```bash
cd "../SpecModule" && rm -rf .specmodule && git status --short
```

Expected: 工作区干净（`.specmodule/` 已被 gitignore，`git status` 无此目录）。

- [ ] **Step 4: Commit（若 Step 3 有意外变更）**

```bash
cd "../SpecModule" && git add -A && git commit -m "chore: clean test residue" || true
```

---

## Self-Review（计划与 spec 对照）

**spec §6 新测试覆盖**：loop 窗口 ✓(A2)、线性流程窗口 ✓(A2)、大 output 不驻留 ✓(A2)、latest 多轮正确性 ✓(既有 test_loop 保留 + A3 回归)、并行污染 ✓(A3)、index 窗口外可解析（有 backend）✓(A3)、index 降级 NullBackend ✓(A3)、audit 查库全量 ✓(A4)、firings_of 分派 ✓(A4)、snapshot/restore 重放 ✓(A5+既有 test_snapshot)、restore 后 index 可解析 ✓(A5)、默认 backend 生命周期 ✓(A6)。SpecModule 侧 4 项 ✓(B4)。

**spec §7 文件清单**：Graph 仓库 6 文件 ✓(A1-A8)；SpecModule 6 文件 ✓(B1-B5)。`tickflow/views.py` 判定无需改动（docstring 泛称 history，窗口仍是 history）。

**既有测试适配**：test_restore_truncates_history → 窗口下运行验证 ✓(A5 Step 4)；_edges 全量断言（test_failure:95、test_remap:162、test_checkpoints:165、test_node_state:79）均为存在性断言，窗口下仍成立 ✓；无 backend 测试（显式 NullBackend）行为不变 ✓；test_persistence.py 协议扩展 ✓(A1)；`test_checkpoint_requires_backend`（破坏面排查漏项）→ 本计划 A6 显式适配 ✓。

**已知限制（spec §9）**：retention 策略、异步批量写、JsonBackend 冷查询优先级、Module backend 精细控制——均不在本次范围；`firing_at` 的 None-output 歧义（实现裁定 9）已文档化。

**类型一致性**：`firing_at(session_id, node, k) -> Any | None`、`firings_of(session_id, node) -> list[tuple[int, Any]]` 在 A1 定义，A3/A4 的调用签名一致；`RunState(keep_records, backend, session_id, persistent)` 在 A2 定义，A5 的 `from_snapshot_data`/`restore` 与 A6 的 runner 构造参数一致；`inject_audit(records)` A7 定义并唯一调用于 `from_json`；`_fire_counts` 在 A2 引入，A3 resolve 与 A5 truncate/keep_nodes/from_snapshot_data 一致维护；`persist`/`mode` 参数名在 B2/B3/B4 三处一致。

---

## Execution Handoff

计划已保存到 `docs/dev/superpowers/plans/2026-08-05-tickflow-storage.md`。两种执行方式：

1. **SubAgent 驱动（推荐）**——每个任务派发独立 subagent，任务间双阶段 review，快速迭代
2. **Inline 执行**——本会话内用 executing-plans 按任务批执行，检查点 review

选哪种？
