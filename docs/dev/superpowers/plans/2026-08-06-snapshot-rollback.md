# 快照/回滚 Module 封装 实现计划

> ⚠️ **tickflow 0.2.0 bind 迁移注记（2026-09-05）**：本文档编写于旧视图机制时期——`input_aliases` / producer 名访问（`view["X"].value`、`view.A.value`）/ DictView 构造均已被具名 bind 机制取代：body/guard 经 `view.field()`、`view.output`、`v.named` 消费，字段名即 `task.inputs` 键。文中代码示例为当时形态，勿照抄；当前契约见 `docs/references/spec-harness-syntax.md` 与 `docs/references/tickflow-integration.md`。


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Module 提供自动检查点（每 tick 环形保留 20）、跨进程 `resume()` 续跑、进程内 `snapshot()`/`restore()`/`checkpoint()`/`rollback_to()`、以及"新 task 与已执行节点兼容性校验"。

**Architecture:** 新文件 `module_harness/checkpoint.py` 承载 `AutoCheckpointStore`（run.sqlite 内 `auto_checkpoints` + `module_inputs` 两张表，零修改 tickflow）+ `check_resume_compat` 纯函数校验。`Module` 扩展持有 `_runner`，新增进程内 API 与 `resume()`。`resume()` = 新 spec/tasklist 全量重建新图 → 校验 → `runner.restore(snap)` + `remap_graph(新图)` 移植 marking → 续跑。自动检查点经 `on_tick_end` hook 写入。

**Tech Stack:** Python 3.13, pytest, sqlite3 (WAL), tickflow `AsyncRunner`/`RunState`/`Graph`, `dataclasses.asdict`。

**Spec:** `docs/dev/superpowers/specs/2026-08-06-snapshot-rollback-design.md`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `module_harness/checkpoint.py`（新建） | `AutoCheckpointStore`（auto_checkpoints 表 + module_inputs 表）、`ResumeCheck`、`ResumeError`、`check_resume_compat`、tasklist dict 序列化 helpers |
| `module_harness/module.py`（修改） | `Module._runner` 持有、`snapshot`/`restore`/`checkpoint`/`rollback_to`/`list_checkpoints`/`resume`、自动检查点 hook、module_inputs 存档 |
| `module_harness/__init__.py`（修改） | 导出新符号 |
| `module_harness/tests/test_checkpoint.py`（新建） | 全部新测试（单元 + 集成） |
| `docs/dev/progress/module-roadmap.md`（修改） | 标记 #5 完成（17 → 18/19） |

**关键既有接口（已核实，不要重新发明）：**
- `Runner.snapshot() → dict`（`tickflow/runner.py:290`），`Runner.restore(snap)`（:302）
- `Runner.checkpoint(label)`/`list_checkpoints()`/`rollback_to(label)`（:420/426/431）—— 需 backend + session_id，`NullBackend` 下 `RuntimeError`
- `Runner.remap_graph(new_graph, registry=None, strict_deadlock=True)`（:384）—— slot 按 `(dst,src)` 键移植、`keep_nodes` 裁剪、`reset()`
- `AsyncRunner.on_tick_end(cb)` —— cb 签名 `(tick: int, firings: list[NodeState])`，sync/async 均可；在 `_persist_tick()` **之前**调用
- `Graph.nodes`（dict）、`Graph.starts`（属性）、`Graph.out_edges(node) → list[Edge]`（Edge 有 `.dst`）
- `HarnessRegistry` 继承 tickflow `Registry`，`reg.guard("name")(fn)` 可用（`tickflow/registry.py:57`）
- `RunState.to_snapshot_data()` 的 `edges` 是 `dict[node, list]` —— **已执行节点集合 = `set(snap["run_state"]["edges"].keys())`**
- `Spec` 有 `to_dict()`；`TaskDefinition`/`Tasklist` **没有** `to_dict` —— 用 `dataclasses.asdict` + `Tasklist.from_json` 对称序列化
- `SqliteBackend(db_path)` 构造时自动建表 + WAL（`tickflow/persistence.py:360` 起）；`load_checkpoint(session_id, label) → dict | None`
- `_persist_dir(module_id)` = `Path.cwd() / ".specmodule" / "runs" / module_id / "run.sqlite"`（`module.py:27`）
- 测试隔离模式：`monkeypatch.chdir(tmp_path)` + `module_id="mod_test"`（对齐 `test_run_status.py`）

---

## Task 1: `AutoCheckpointStore` — auto_checkpoints 表 + tasklist helpers

**Files:**
- Create: `module_harness/checkpoint.py`
- Test: `module_harness/tests/test_checkpoint.py`

- [ ] **Step 1: 写失败测试**

创建 `module_harness/tests/test_checkpoint.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'module_harness.checkpoint'`

- [ ] **Step 3: 实现 checkpoint.py（第一版：store + helpers）**

创建 `module_harness/checkpoint.py`：

```python
# module_harness/checkpoint.py
"""Module 层快照/回滚：自动检查点存储 + 兼容性校验（roadmap #5）。

- ``AutoCheckpointStore``：run.sqlite 内 ``auto_checkpoints`` 表（每 tick 一个
  自动检查点，环形保留最近 20）与 ``module_inputs`` 表（本次运行使用的
  spec/tasklist 存档，供兼容性对比与跨进程查询）。
- ``check_resume_compat``：新 tasklist 与已执行节点的兼容性校验。

零修改 tickflow：全部实现位于 module_harness 层；自动检查点表独立于
SqliteBackend 的 snapshots/firings/checkpoints 表，通过独立 sqlite3 连接
打开同一 run.sqlite（WAL 模式多连接安全）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tickflow import Graph

from .spec import TaskDefinition, Tasklist

log = logging.getLogger(__name__)


def _run_db_path(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base_dir>/.specmodule/runs/<module_id>/run.sqlite``（与 Module._persist_dir 对齐）。"""
    base = base_dir if base_dir is not None else Path.cwd()
    return base / ".specmodule" / "runs" / module_id / "run.sqlite"


def tasklist_to_dict(tl: Tasklist) -> dict[str, Any]:
    """Tasklist → JSON 可序列化 dict（与 ``Tasklist.from_json`` 对称）。"""
    return {
        "Tasks": {k: asdict(v) for k, v in tl.tasks.items()},
        "Flow": tl.flow,
    }


def tasklist_from_dict(d: dict[str, Any]) -> Tasklist:
    """tasklist_to_dict 的逆操作。"""
    return Tasklist.from_json(d)


class AutoCheckpointStore:
    """run.sqlite 内自动检查点与运行输入存档的存取。

    自动检查点：``auto_checkpoints(label TEXT PK, tick INT, snap TEXT,
    created_at REAL)``——每 tick 一个命名快照，环形保留最近 ``max_auto`` 个
    （超出按 created_at 淘汰最旧）。

    运行输入存档：``module_inputs(id INT PK CHECK(id=1), spec TEXT,
    tasklist TEXT, saved_at REAL)``——单行，覆盖式，供兼容性校验与
    跨进程查询"这次 run 用了什么输入"。

    连接策略：构造时打开独立连接（WAL 模式，与 SqliteBackend 并存安全）；
    写失败仅 log 不阻断（对齐 status.json 容错哲学）。
    """

    def __init__(
        self,
        module_id: str,
        max_auto: int = 20,
        base_dir: Path | None = None,
    ) -> None:
        self.module_id = module_id
        self.max_auto = max_auto
        self.db_path = _run_db_path(module_id, base_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auto_checkpoints (
                label      TEXT PRIMARY KEY,
                tick       INTEGER NOT NULL,
                snap       TEXT    NOT NULL,
                created_at REAL    NOT NULL
            );
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

    # ------------------------------------------------------------------
    # 自动检查点
    # ------------------------------------------------------------------

    def save(self, label: str, snap: dict) -> None:
        """保存一个自动检查点，同名覆盖；超出 max_auto 淘汰最旧。"""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO auto_checkpoints(label, tick, snap, created_at) "
                "VALUES (?, ?, ?, ?)",
                (label, int(snap["tick"]), json.dumps(snap), time.time()),
            )
            self._prune()
            self._conn.commit()
        except (sqlite3.Error, OSError, KeyError):
            log.exception("自动检查点保存失败（不阻断）: %s", self.db_path)

    def _prune(self) -> None:
        """环形保留：删除 created_at 最旧的超出部分（仅影响本表自动检查点）。"""
        self._conn.execute(
            "DELETE FROM auto_checkpoints WHERE label NOT IN ("
            "  SELECT label FROM auto_checkpoints"
            "  ORDER BY created_at DESC LIMIT ?"
            ")",
            (self.max_auto,),
        )

    def load(self, label: str) -> dict | None:
        """按 label 读取自动检查点；不存在或损坏返回 None。"""
        try:
            row = self._conn.execute(
                "SELECT snap FROM auto_checkpoints WHERE label = ?", (label,)
            ).fetchone()
        except sqlite3.Error:
            log.exception("自动检查点读取失败: %s", self.db_path)
            return None
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            log.warning("自动检查点 %r 数据损坏，忽略", label)
            return None

    def list(self) -> list[tuple[str, int]]:
        """全部自动检查点 (label, tick)，按 tick 升序。"""
        try:
            rows = self._conn.execute(
                "SELECT label, tick FROM auto_checkpoints ORDER BY tick"
            ).fetchall()
        except sqlite3.Error:
            log.exception("自动检查点列表读取失败: %s", self.db_path)
            return []
        return [(label, tick) for label, tick in rows]

    # ------------------------------------------------------------------
    # 运行输入存档（module_inputs 表）
    # ------------------------------------------------------------------

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
        except (sqlite3.Error, OSError):
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

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add module_harness/checkpoint.py module_harness/tests/test_checkpoint.py
git commit -m "feat(checkpoint): AutoCheckpointStore 自动检查点表 + module_inputs 存档"
```

---

## Task 2: `check_resume_compat` 兼容性校验

**Files:**
- Modify: `module_harness/checkpoint.py`（追加）
- Test: `module_harness/tests/test_checkpoint.py`（追加）

- [ ] **Step 1: 写失败测试**

追加到 `module_harness/tests/test_checkpoint.py`：

```python
from module_harness.checkpoint import (
    ResumeCheck,
    check_resume_compat,
)
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.spec import TaskDefinition, Tasklist
from module_harness.graph_builder import TasklistTranslator
from module_harness.registry import HarnessRegistry
from module_harness.events import EventBus


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
        # A 已执行；C 引用 A（已执行）+ B（未执行但拓扑上游 [A] --> B --> C）。
        # A 是 start 且有历史——但旧 flow 也是 [A]（start 未变）→ 不警告。
        old_tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B --> C",
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
        # A 旧图不是 start（旧 flow 无 [A]），新图成为 start 且有历史 → 硬错误
        old_tl = _tl({"A": {"type": "script", "script": "s"}}, "A")
        tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
        graph = _graph_for(tl)
        check = check_resume_compat(tl, graph, executed_nodes={"A"}, old_tasklist=old_tl)
        assert any("start" in e.lower() for e in check.hard_errors)

    def test_no_hard_error_when_start_unchanged(self):
        # A 新旧图都是 start 且有历史 = 正常 resume 场景 → 不误报
        old_tl = _tl({"A": {"type": "script", "script": "s"}}, "[A]")
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
        # B 未执行但 flow 保证先于 C 执行：[A] --> B --> C
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B --> C",
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

    def test_warning_new_node_in_edges_unmet(self):
        # D 是新增节点（入边 (D,B) 不在旧 marking）→ 永不 fire → 警告
        old_tl = _tl(
            {"A": {"type": "script", "script": "s"},
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
                "D": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B --> C\nB --> D",
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
             "B": {"type": "script", "script": "s"},
             "C": {"type": "script", "script": "s"}},
            "[A] --> B --> C",
        )
        tl = _tl(
            {
                "A": {"type": "script", "script": "s"},
                "B": {"type": "script", "script": "s", "inputs": {"data": "A"}},
                "C": {"type": "script", "script": "s", "inputs": {"data": "B"}},
            },
            "[A] --> B --> C",
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
```

注意：`_graph_for` 中 harness 注册需要有效配置——`OutputFormat(type="text")`。若 `HarnessConfig` 校验失败，改用纯 script tasklist（`_tl` 全用 `script` 类型即可避免 harness 注册）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestCheckResumeCompat -q`
Expected: FAIL — `ImportError: cannot import name 'check_resume_compat'`

- [ ] **Step 3: 实现 check_resume_compat**

追加到 `module_harness/checkpoint.py`：

```python
# ------------------------------------------------------------------
# 兼容性校验
# ------------------------------------------------------------------


class ResumeError(Exception):
    """resume 兼容性硬错误。含全部错误明细（换行分隔）。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("\n".join(errors))


@dataclass
class ResumeCheck:
    """兼容性校验结果。hard_errors 非空则拒绝 resume。"""

    hard_errors: list[str]
    warnings: list[str]


def _is_transitive_upstream(graph: Graph, producer: str, consumer: str) -> bool:
    """BFS：producer 能否沿出边到达 consumer（含 producer == consumer）。"""
    seen = {producer}
    stack = [producer]
    while stack:
        n = stack.pop()
        if n == consumer:
            return True
        for e in graph.out_edges(n):
            if e.dst not in seen:
                seen.add(e.dst)
                stack.append(e.dst)
    return False


def check_resume_compat(
    new_tasklist: Tasklist,
    graph: Graph,
    executed_nodes: set[str],
    old_tasklist: Tasklist | None = None,
    marking_slots: dict[str, bool] | None = None,
) -> ResumeCheck:
    """新 tasklist 与已执行节点的兼容性校验。

    - 硬错误 1：新 task 的 inputs 引用的 producer 不在新图节点集合中
      （``{spec.xxx}`` 常量引用跳过——graph_builder 解析为 spec_inputs）。
    - 硬错误 2：新图中**新成为** start 且有历史输出的节点（armed_starts
      一次性，永不重跑；底层 ``_warn_graph_changes`` 在 remap old==new 时
      不触发，此处补上）。"新成为"判定：与旧 tasklist flow 的 start 集合
      对比（flow 中 ``[A]`` 标记 start，正则 ``\\[(\\w+)\\]`` 提取）——
      正常 resume 里旧图 start 有历史是常态，不能误报。无存档
      （old_tasklist=None）时降级为警告（保守）。
    - 警告 1：已执行节点在新 tasklist 中被修改（对比 module_inputs 存档，
      修改对已执行部分不生效）。
    - 警告 2：inputs 引用的 producer 未执行、且不是 consumer 的拓扑上游
      （运行时 resolve 为 Missing，prompt 占位符保留字面量）。
    - 警告 3：未执行且非 start 的节点，其所有入边在检查点 marking 中均未
      满足——该节点永远不会 fire。``remap_graph`` 移植 slot 时新边取
      ``old_slots.get(key, False)``（runner.py:405）：新节点/改名节点的入边
      在旧 marking 中不存在 → False；旧边已消费也是 False。需回退到更早
      检查点让其上游重新执行，或设为 start。``marking_slots`` 为检查点
      snapshot 的 ``marking.slots``，键格式 ``"dst|src"``（与
      ``Marking.to_json`` 一致，engine.py:71）；为 None 时跳过本检查。

    返回 ResumeCheck；调用方在 hard_errors 非空时 raise ResumeError。
    """
    hard_errors: list[str] = []
    warnings: list[str] = []
    tasks = new_tasklist.tasks

    for key, task in tasks.items():
        for field, producer in (task.inputs or {}).items():
            if isinstance(producer, str) and producer.startswith("{spec."):
                continue
            if producer not in graph.nodes:
                hard_errors.append(
                    f"Task '{key}': inputs 引用 '{producer}' 不在新图中"
                )
            elif producer not in executed_nodes and not _is_transitive_upstream(
                graph, producer, key
            ):
                warnings.append(
                    f"Task '{key}': inputs 引用 '{producer}' 未执行且非其拓扑上游"
                    f"——运行时可能 resolve 为 Missing"
                )

    # 硬错误 2：新图中"新成为" start 且有历史输出。旧图 start 有历史是正常
    # resume 场景（如回退到中途，A 是 start 且已执行），不能误报——用旧
    # tasklist flow 的 start 集合（flow 中 [A] 标记）判定"新成为"。
    old_starts: set[str] = set()
    if old_tasklist is not None:
        old_starts = set(re.findall(r"\[(\w+)\]", old_tasklist.flow))
    for n in graph.starts:
        if n in executed_nodes:
            if old_tasklist is not None and n not in old_starts:
                hard_errors.append(
                    f"Node '{n}' 新成为 start 但已有执行历史——armed_starts "
                    f"一次性，永不重跑。请回退到 tick 0 或改回非 start。"
                )
            elif old_tasklist is None:
                warnings.append(
                    f"Node '{n}' 是 start 且已有执行历史（无存档可对比是否"
                    f"新成为）——若期望其重跑需回退到 tick 0"
                )

    if old_tasklist is not None:
        for n in sorted(executed_nodes & set(tasks)):
            old = old_tasklist.tasks.get(n)
            if old is not None and asdict(old) != asdict(tasks[n]):
                warnings.append(
                    f"已执行节点 '{n}' 的 task 定义被修改——修改对已执行部分不生效，"
                    f"需回退到更早的检查点"
                )

    # 警告 3：未执行非 start 节点，入边在检查点 marking 均未满足 → 永不 fire
    if marking_slots is not None:
        for n in graph.nodes:
            if n in executed_nodes or n in graph.starts:
                continue
            in_edges = [f"{e.dst}|{e.src}" for e in graph.edges if e.dst == n]
            if in_edges and not any(
                marking_slots.get(key, False) for key in in_edges
            ):
                warnings.append(
                    f"Node '{n}' 的入边在检查点均未满足（新边或已消费）——"
                    f"该节点不会自动执行。需回退到更早检查点使其上游重新执行，"
                    f"或将其设为 start。"
                )

    return ResumeCheck(hard_errors=hard_errors, warnings=warnings)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS（Task 1 的 8 个 + Task 2 的 8 个）

- [ ] **Step 5: Commit**

```bash
git add module_harness/checkpoint.py module_harness/tests/test_checkpoint.py
git commit -m "feat(checkpoint): check_resume_compat 兼容性校验（2 硬错误 + 2 警告）"
```

---

## Task 3: Module 进程内 API — snapshot/restore/checkpoint/rollback_to/list_checkpoints

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_checkpoint.py`（追加）

- [ ] **Step 1: 写失败测试**

追加到 `module_harness/tests/test_checkpoint.py`：

```python
import asyncio

from module_harness.module import Module


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
        flow="[A] --> B --> C",
    )


class TestModuleSnapshotAPI:
    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, **kw):
        monkeypatch.chdir(tmp_path)
        kw.setdefault("registry", _script_reg(mock_llm))
        return Module(
            spec={"x": 1},
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
        assert snap["tasklist"]["Flow"] == "[A] --> B --> C"
        assert "marking" in snap["runner"]

        # restore 后 spec/tasklist/runner 状态一致
        mod.restore(snap)
        assert mod.spec.to_dict() == {"x": 1}
        assert mod.tasklist.flow == "[A] --> B --> C"
        assert mod._runner.tick_count == runner.tick_count

    def test_snapshot_deep_copy_independent(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        mod.build_runner()
        snap = mod.snapshot()
        snap["spec"]["x"] = 999
        snap["tasklist"]["Flow"] = "changed"
        assert mod.spec.to_dict() == {"x": 1}
        assert mod.tasklist.flow == "[A] --> B --> C"

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

    def test_list_checkpoints_empty_before_run(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        mod.build_runner()
        assert mod.list_checkpoints() == []

    def test_checkpoint_without_runner_raises(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match="runner"):
            mod.checkpoint("x")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestModuleSnapshotAPI -q`
Expected: FAIL — `AttributeError: 'Module' object has no attribute '_runner'`

- [ ] **Step 3: 实现 Module 扩展**

修改 `module_harness/module.py`：

1. 顶部 import 追加：

```python
from .checkpoint import AutoCheckpointStore, ResumeError, check_resume_compat, tasklist_from_dict, tasklist_to_dict
```

2. `__init__` 尾部（`self._write_phase("idle")` 之后）追加：

```python
        # roadmap #5：runner 由 _build_runner_async 持有；快照/回滚 API 依赖它
        self._runner: AsyncRunner | None = None
        self._last_tasklist: Tasklist | None = None
        self._checkpoint_store: AutoCheckpointStore | None = None
```

3. `_build_runner_async` 中 `builder.build(tasklist, spec=self.spec)` 之后、构造 backend 之前，追加：

```python
        self._last_tasklist = tasklist
```

4. `_build_runner_async` 返回前（`return AsyncRunner(...)` 改为）：

```python
        runner = AsyncRunner(
            graph,
            registry=reg,
            keep_records=self.keep_records,
            backend=backend,
            session_id=self.module_id,
        )
        self._runner = runner
        return runner
```

5. 新增方法（放在 `run()` 之前）：

```python
    # ------------------------------------------------------------------
    # 快照/回滚（roadmap #5）
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """进程内全量快照：{spec, tasklist, runner_snapshot} 三件套。

        深拷贝语义：修改返回的 dict 不影响 Module 状态。
        """
        if self._runner is None:
            raise RuntimeError("尚未构建 runner——请先 build_runner() 或 run()")
        assert self._last_tasklist is not None
        return {
            "spec": self.spec.to_dict(),
            "tasklist": tasklist_to_dict(self._last_tasklist),
            "runner": self._runner.snapshot(),
        }

    def restore(self, snap: dict) -> None:
        """回滚 runner 到快照，并恢复 spec/tasklist 字段。"""
        if self._runner is None:
            raise RuntimeError("尚未构建 runner——请先 build_runner() 或 run()")
        self.spec = Spec(snap["spec"])
        self.tasklist = tasklist_from_dict(snap["tasklist"])
        self._last_tasklist = self.tasklist
        self._runner.restore(snap["runner"])

    def checkpoint(self, label: str) -> None:
        """手动检查点（backend 表，永久保留）。透传 runner。"""
        if self._runner is None:
            raise RuntimeError("尚未构建 runner——请先 build_runner() 或 run()")
        self._runner.checkpoint(label)

    def rollback_to(self, label: str) -> None:
        """进程内回退到命名检查点。透传 runner。"""
        if self._runner is None:
            raise RuntimeError("尚未构建 runner——请先 build_runner() 或 run()")
        self._runner.rollback_to(label)

    def list_checkpoints(self) -> list[tuple[str, int, str]]:
        """全部检查点 (label, tick, kind)，按 tick 升序。kind ∈ {"auto", "manual"}。

        auto：Module 自动检查点（环形保留 20）；manual：checkpoint() 手动检查点。
        不依赖 runner——跨进程场景（新 Module 实例）也可查询。
        """
        out: list[tuple[str, int, str]] = []
        store = AutoCheckpointStore(self.module_id)
        try:
            out.extend((label, tick, "auto") for label, tick in store.list())
        finally:
            store.close()
        if self.persist:
            try:
                backend = SqliteBackend(_persist_dir(self.module_id))
                out.extend(
                    (label, tick, "manual")
                    for label, tick in backend.list_checkpoints(self.module_id)
                )
            except Exception:
                log.exception("手动检查点列表读取失败（忽略）")
        return sorted(out, key=lambda item: item[1])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS

同时跑既有 Module 测试确认无回归：
Run: `python -m pytest module_harness/tests/test_module.py module_harness/tests/test_run_status.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add module_harness/module.py module_harness/tests/test_checkpoint.py
git commit -m "feat(module): 进程内快照/回滚 API（snapshot/restore/checkpoint/rollback_to/list_checkpoints）"
```

---

## Task 4: run() 自动检查点 hook + module_inputs 存档

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_checkpoint.py`（追加）

- [ ] **Step 1: 写失败测试**

追加到 `module_harness/tests/test_checkpoint.py`：

```python
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
        # 三节点链：tick 0/1/2 各一次 firing，tick 3 空。自动检查点 tick 0..3
        assert auto, "应有自动检查点"
        ticks = [t for _, t, _ in auto]
        assert ticks == sorted(ticks)
        assert 0 in ticks and 2 in ticks

    @pytest.mark.asyncio
    async def test_run_archives_module_inputs(self, mock_llm, tmp_path, monkeypatch):
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        await mod.run()
        store = AutoCheckpointStore("mod_test")
        inputs = store.load_module_inputs()
        store.close()
        assert inputs is not None
        assert inputs["spec"] == {"x": 1}
        assert inputs["tasklist"]["Flow"] == "[A] --> B --> C"

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
        flow = "[N0] --> " + " --> ".join(f"N{i}" for i in range(1, 25))
        tl = Tasklist(tasks=tasks, flow=flow)
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True, tasklist=tl)
        await mod.run()
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert len(auto) <= 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestAutoCheckpointHook -q`
Expected: FAIL — `assert auto, "应有自动检查点"`（run() 未注册 hook）

- [ ] **Step 3: 实现 run() 自动检查点**

修改 `module_harness/module.py` 的 `run()`。将整个方法替换为：

```python
    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。

        persist=True 时：注册自动检查点 hook（每 tick 存一个，环形保留 20），
        并归档本次 spec/tasklist 到 module_inputs 表。
        """
        from tickflow.runner import RunStatus

        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        self._register_auto_checkpoint()
        self._write_phase("running")
        try:
            firings = await runner.run_until_idle(max_ticks=max_ticks)
        except asyncio.CancelledError:
            self._write_phase("cancelled", error="cancelled")
            raise
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        else:
            self._finalize_phase(runner)
        return firings

    def _register_auto_checkpoint(self) -> None:
        """persist=True 时：注册 on_tick_end hook 存自动检查点 + 归档 module_inputs。

        幂等：重复调用只注册一次（hook 存于 Module 状态，run/resume 复用）。
        """
        if not self.persist or self._runner is None:
            return
        if self._checkpoint_store is None:
            self._checkpoint_store = AutoCheckpointStore(self.module_id)
        store = self._checkpoint_store
        assert self._last_tasklist is not None
        store.save_module_inputs(
            self.spec.to_dict(), tasklist_to_dict(self._last_tasklist)
        )
        if not getattr(self, "_auto_cp_hooked", False):
            runner = self._runner

            def _hook(tick: int, firings) -> None:
                store.save(f"auto:tick:{tick}", runner.snapshot())

            runner.on_tick_end(_hook)
            self._auto_cp_hooked = True

    def _finalize_phase(self, runner) -> None:
        """按 runner.status 映射终态 phase（run/resume 共用）。"""
        from tickflow.runner import RunStatus
        if runner.status == RunStatus.ABORTED:
            self._write_phase("aborted", error=runner.cancel_reason or "aborted")
        elif runner.status == RunStatus.CANCELLED:
            self._write_phase("cancelled", error=runner.cancel_reason or "cancelled")
        elif runner.status == RunStatus.FAILED:
            self._write_phase("aborted", error="all nodes failed")
        elif runner.status == RunStatus.RUNNING:
            self._write_phase("running")   # max_ticks 截断：仍在运行
        else:
            self._write_phase("done")
```

`__init__` 中追加 `self._auto_cp_hooked = False`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS

回归：`python -m pytest module_harness/tests/test_module.py module_harness/tests/test_run_status.py -q` — PASS

- [ ] **Step 5: Commit**

```bash
git add module_harness/module.py module_harness/tests/test_checkpoint.py
git commit -m "feat(module): run() 自动检查点 hook + module_inputs 归档"
```

---

## Task 5: `Module.resume()` 跨进程续跑

**Files:**
- Modify: `module_harness/module.py`
- Test: `module_harness/tests/test_checkpoint.py`（追加）

- [ ] **Step 1: 写失败测试**

追加到 `module_harness/tests/test_checkpoint.py`：

```python
class TestResume:
    def _make_module(self, mock_llm, tmp_path, monkeypatch, tasklist=None, persist=True, spec=None):
        monkeypatch.chdir(tmp_path)
        return Module(
            spec=spec if spec is not None else {"x": 1},
            tasklist=tasklist or _chain_tasklist(),
            llm_client=mock_llm,
            review_harness=None,
            persist=persist,
            module_id="mod_test",
            registry=_script_reg(mock_llm),
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
            flow="[A] --> B --> C",
        )
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch, tasklist=new_tl)
        firings = await mod2.resume(rollback_to="auto:tick:1")
        nodes = [f.node for f in firings]
        assert nodes == ["C"], f"resume 应只重跑 C，实际 {nodes}"
        # 运行结束状态
        from tickflow.runner import RunStatus
        assert mod2._runner.status == RunStatus.IDLE

    @pytest.mark.asyncio
    async def test_resume_preserves_executed_outputs(self, mock_llm, tmp_path, monkeypatch):
        """resume 后已执行节点的输出保留，可被新节点通过 inputs 消费。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # 新图：C 换成 record script，读取 B（已执行）的输出
        reg = _script_reg(mock_llm, record=lambda view: {"echo": view["data"].value})
        monkeypatch.chdir(tmp_path)
        new_tl = Tasklist(
            tasks={
                "A": TaskDefinition(type="script", script="echo"),
                "B": TaskDefinition(type="script", script="echo", inputs={"data": "A"}),
                "C": TaskDefinition(type="script", script="record", inputs={"data": "B"}),
            },
            flow="[A] --> B --> C",
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
            flow="[A] --> B --> C; B --> D",
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

    @pytest.mark.asyncio
    async def test_resume_manual_checkpoint(self, mock_llm, tmp_path, monkeypatch):
        """resume 也能回退到手动检查点（backend 表）。"""
        mod = self._make_module(mock_llm, tmp_path, monkeypatch, persist=True)
        mod.build_runner()
        mod.checkpoint("manual:before")
        await mod.run()
        mod2 = self._make_module(mock_llm, tmp_path, monkeypatch)
        firings = await mod2.resume(rollback_to="manual:before")
        assert [f.node for f in firings] == ["A", "B", "C"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestResume -q`
Expected: FAIL — `AttributeError: 'Module' object has no attribute 'resume'`

- [ ] **Step 3: 实现 resume()**

追加到 `module_harness/module.py`（`_finalize_phase` 之后）：

```python
    async def resume(self, rollback_to: str, max_ticks: int = 100):
        """跨进程续跑：从检查点恢复 + 用当前 spec/tasklist 重建未执行部分。

        流程：检查点查找（auto 表 → 手动表）→ 新图全量重建 → 兼容性校验
        （硬错误拒绝，不触碰 runner）→ restore + remap_graph 移植 marking →
        注册自动检查点 hook → 续跑。

        要求 persist=True（自动检查点依赖 SQLite backend）。
        """
        if not self.persist:
            raise RuntimeError(
                "resume 需要 persist=True（自动检查点依赖 SQLite backend）"
            )

        # 1. 检查点查找：auto 表 → 手动表
        store = AutoCheckpointStore(self.module_id)
        try:
            snap = store.load(rollback_to)
            if snap is None:
                backend = SqliteBackend(_persist_dir(self.module_id))
                snap = backend.load_checkpoint(self.module_id, rollback_to)
            if snap is None:
                available = ", ".join(
                    label for label, _, _ in self.list_checkpoints()
                ) or "（无）"
                raise KeyError(
                    f"检查点 {rollback_to!r} 不存在（可用: {available}）"
                )
            old_inputs = store.load_module_inputs()
        finally:
            store.close()

        # 2. 新 spec/tasklist 全量重建（含校验 + 一致性审核）
        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise

        # 3. 兼容性校验（构造 runner 后、restore 前；硬错误拒绝且不触碰状态）
        executed_nodes = set(
            snap.get("run_state", {}).get("edges", {}).keys()
        )
        marking_slots = snap.get("marking", {}).get("slots")
        old_tl = tasklist_from_dict(old_inputs["tasklist"]) if old_inputs else None
        check = check_resume_compat(
            self._last_tasklist, runner.graph, executed_nodes,
            old_tasklist=old_tl,
            marking_slots=marking_slots,
        )
        for w in check.warnings:
            log.warning("resume 兼容性警告: %s", w)
        if check.hard_errors:
            raise ResumeError(check.hard_errors)

        # 4. restore + remap：移植检查点 marking 到新图
        runner.restore(snap)
        runner.remap_graph(runner.graph)

        # 5. 注册自动检查点 + 续跑
        self._register_auto_checkpoint()
        self._write_phase("running")
        try:
            firings = await runner.run_until_idle(max_ticks=max_ticks)
        except asyncio.CancelledError:
            self._write_phase("cancelled", error="cancelled")
            raise
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        else:
            self._finalize_phase(runner)
        return firings
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py -q`
Expected: PASS

回归：`python -m pytest module_harness/tests/ -q --ignore=module_harness/tests/smoke` — PASS

- [ ] **Step 5: Commit**

```bash
git add module_harness/module.py module_harness/tests/test_checkpoint.py
git commit -m "feat(module): resume() 跨进程续跑（检查点恢复 + 兼容性校验 + remap 移植）"
```

---

## Task 6: loop 场景集成测试

**Files:**
- Test: `module_harness/tests/test_checkpoint.py`（追加）

验证 spec 的 loop 结论：回退到循环中途，`view.state` 从该迭代继续（tickflow `truncate_after` 从持久化历史重建 mutable state）。

- [ ] **Step 1: 写测试**

追加到 `module_harness/tests/test_checkpoint.py`：

```python
class TestResumeLoop:
    def _loop_module(self, mock_llm, tmp_path, monkeypatch):
        """counter 节点自循环：n 从 0 递增，n<3 时 guard 放行继续。

        自循环语义（README retry_loop 惯例）：body 在 view.state 计数并返回
        {"n": ...}；guard 读 view["counter"].value（firing 节点输出在自己名下
        可见，engine._guard_view）与 view.state.get（_NodeStateView 有 .get，
        engine.py:285）。
        """
        monkeypatch.chdir(tmp_path)

        def counter(view):
            n = view.state.get("n", 0) + 1
            view.state["n"] = n
            return {"n": n}

        reg = _script_reg(mock_llm, counter=counter)

        def until3(view):
            return view["counter"].value < 3

        reg.guard("until3")(until3)

        tl = Tasklist(
            tasks={"counter": TaskDefinition(type="script", script="counter")},
            flow="[counter] -->|until3| counter",
        )
        return Module(
            spec={"x": 1},
            tasklist=tl,
            llm_client=mock_llm,
            review_harness=None,
            persist=True,
            module_id="mod_loop",
            registry=reg,
        )

    @pytest.mark.asyncio
    async def test_loop_runs_until_guard_opens(self, mock_llm, tmp_path, monkeypatch):
        mod = self._loop_module(mock_llm, tmp_path, monkeypatch)
        await mod.run()
        # n 从 0 递增：tick0→1, tick1→2, tick2→3（guard n<3 在 n=3 时放行退出）
        auto = [c for c in mod.list_checkpoints() if c[2] == "auto"]
        assert auto, "应有自动检查点"

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
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest module_harness/tests/test_checkpoint.py::TestResumeLoop -q`
Expected: PASS（若 `.get` 或 guard 注册报错，按 Step 1 注释修正）

- [ ] **Step 3: Commit**

```bash
git add module_harness/tests/test_checkpoint.py
git commit -m "test(checkpoint): loop 中途 resume 状态续跑集成测试"
```

---

## Task 7: 导出 + 文档 + 全量回归

**Files:**
- Modify: `module_harness/__init__.py`
- Modify: `docs/dev/progress/module-roadmap.md`

- [ ] **Step 1: 导出新符号**

`module_harness/__init__.py`：

1. import 块追加：

```python
from .checkpoint import (
    AutoCheckpointStore,
    ResumeCheck,
    ResumeError,
    check_resume_compat,
)
```

2. `__all__` 追加：

```python
    # 快照/回滚（roadmap #5）
    "AutoCheckpointStore",
    "ResumeCheck",
    "ResumeError",
    "check_resume_compat",
```

- [ ] **Step 2: 更新 roadmap**

`docs/dev/progress/module-roadmap.md`：

1. 顶部计数 "17/19" → "18/19"
2. 从"待实现 🔲"删除 `### 5. 快照/回滚 Module 封装` 整节，在"已完成 ✅"表格追加一行：

```markdown
| **快照/回滚封装** — 自动检查点（每 tick 环形保留 20）+ 跨进程 `resume()` 续跑 + 进程内 `snapshot()`/`restore()`/`checkpoint()`/`rollback_to()` + 兼容性校验（2 硬错误 + 2 警告） | `AutoCheckpointStore` + `check_resume_compat` + `Module.resume` | `checkpoint.py`, `module.py` |
```

3. 更新实现顺序图：`6. 快照/回滚封装 ← 依赖 #1` → `✅ 已完成`

- [ ] **Step 3: 全量回归**

Run: `python -m pytest module_harness/tests/ -q --ignore=module_harness/tests/smoke`
Expected: PASS（224 + 新增全部通过）

Run: `python -m pytest tickflow/tests/ -q`
Expected: PASS（tickflow 未修改，应全绿）

- [ ] **Step 4: Commit**

```bash
git add module_harness/__init__.py docs/dev/progress/module-roadmap.md
git commit -m "feat: 导出快照/回滚 API + roadmap 标记 #5 完成（18/19）"
```

---

## Self-Review 记录

**Spec coverage 对照：**
- ✅ AutoCheckpointStore（auto_checkpoints 表 + 环形 20）→ Task 1
- ✅ module_inputs 存档 → Task 1（store 方法）+ Task 4（run 归档）
- ✅ check_resume_compat 2 硬错误 + 2 警告 → Task 2
- ✅ Module._runner 持有 → Task 3
- ✅ 进程内 snapshot/restore/checkpoint/rollback_to/list_checkpoints → Task 3
- ✅ run() 自动检查点 hook → Task 4
- ✅ resume() 跨进程续跑（检查点查找、重建、校验、restore+remap、续跑）→ Task 5
- ✅ fast mode RuntimeError / KeyError / ResumeError 错误矩阵 → Task 5 测试
- ✅ loop 中途 resume → Task 6
- ✅ 导出 + roadmap 18/19 → Task 7

**计划编写中发现的两个 spec 外边界（已并入实现）：**

1. **新节点挂在已执行节点之后永不执行（新增警告 3）**：`remap_graph` 移植 slot 时新边取 `old_slots.get(key, False)`（runner.py:405）——新图新增节点/改名节点的入边在旧 marking 中不存在 → False → 该节点永远不会 fire。这是"微调加节点"的常见场景，原 spec 4 类校验未覆盖。`check_resume_compat` 增加 `marking_slots` 参数（检查点 snapshot 的 `marking.slots`，键格式 `"dst|src"`，与 `Marking.to_json` 一致 engine.py:71）判定入边未满足 → 警告。Task 5 的 `test_resume_new_node_after_executed_warns` 覆盖。

2. **硬错误 2"新成为 start"判定修正**：原 spec 语义"新图中成为 start 且有历史 → 硬错误"在正常 resume 场景会误报——回退到中途时，旧图 start（如 A）已执行是常态。修正为与旧 tasklist flow 的 start 集合对比（flow 中 `[A]` 标记，正则提取），仅"新成为"且"有历史"才硬错误；无存档时降级为警告。Task 2 的 `test_no_hard_error_when_start_unchanged` 覆盖。

**类型一致性：** `AutoCheckpointStore(module_id, max_auto=20, base_dir=None)` 在 Task 1 定义、Task 3/4/5 使用一致；`list_checkpoints() → list[tuple[str, int, str]]` 三处使用一致；`check_resume_compat(new_tasklist, graph, executed_nodes, old_tasklist=None, marking_slots=None)` 签名在 Task 2 定义、Task 5 调用一致（`marking_slots` 为 `dict[str, bool]`，键 `"dst|src"`）。
