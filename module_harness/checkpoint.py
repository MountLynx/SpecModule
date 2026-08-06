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

from .graph_builder import _is_constant_ref
from .spec import TaskDefinition, Tasklist
from .translator import prepare_flow

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
        except (sqlite3.Error, OSError, KeyError, TypeError):
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
        """全部自动检查点 (label, tick)，按 tick 升序；snap 损坏的行跳过。"""
        try:
            rows = self._conn.execute(
                "SELECT label, tick, snap FROM auto_checkpoints ORDER BY tick"
            ).fetchall()
        except sqlite3.Error:
            log.exception("自动检查点列表读取失败: %s", self.db_path)
            return []
        result: list[tuple[str, int]] = []
        for label, tick, snap in rows:
            try:
                json.loads(snap)
            except json.JSONDecodeError:
                log.warning("自动检查点 %r 数据损坏，忽略", label)
                continue
            result.append((label, tick))
        return result

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


def _reachable_from_marking(
    graph: Graph,
    executed_nodes: set[str],
    marking_slots: dict[str, bool],
    armed_starts: set[str] | list[str] | None = None,
) -> set[str]:
    """不动点模拟：从检查点 marking 出发，判定哪些未执行非 start 节点最终会 fire。

    警告 3 的核心问题：只看节点自身入边在检查点的 slot 直接值，会误报
    深回退（回退到 ≥2 层上游）场景——入边已满足的上游节点尚未执行，它
    一旦 fire 就会产出下游节点的入边 slot。此处模拟"将 fire"的传播：

    1. 初始 ``satisfied`` = 检查点 marking 中值为 True 的边键集合
       （键格式 ``"dst|src"``，与 Marking.to_json 一致）∪ 武装 start 的
       出边键（见下）。
    2. 迭代：对每个未执行且非 start 的节点 M，若 M 的所有入边（AND join）
       或任一入边（OR join）都在 ``satisfied`` 中（与
       ``engine._join_satisfied`` 的语义一致）→ M 将 fire → M 的所有出边
       加入 ``satisfied``。
    3. 循环至 ``satisfied`` 不再增长（不动点）。

    ``armed_starts`` 分支（``engine._join_satisfied`` 首条分支，
    engine.py:103-106）：武装的 start 在续跑的第一个 tick 无条件 fire 并写
    下游 slot——即使其入边在检查点全部未满足。典型场景是 resume 到"运行前
    手动检查点"（build_runner() 后、run 前打点：armed_starts 非空、slots
    全空）。模拟与之一致：把每个在图中且武装的 start 的所有出边键并入初始
    ``satisfied``（guard 出边同样乐观加入，与下述取舍一致）。格式与
    ``Marking.to_json`` 的 ``armed_starts`` 一致（排序 list，engine.py:72）；
    为 None/空时跳过。

    guard 边（``e.guard is not None``）**乐观加入**：guard 结果运行时才知，
    此处假定为 True。取舍：警告语义是"可能不会执行"的提示性警告——乐观
    会减少误报（深回退是核心工作流，上游重跑后 guard 通常复现原结果，如
    loop 的 guard）；代价是 guard 实际为 False 时可能漏报（节点确实不执行
    但没警告）。提示性警告宁可少误报，故乐观传播。

    返回判定为"将 fire"的节点集合（已执行节点与 start 永不参与）。
    """
    satisfied = {k for k, v in marking_slots.items() if v}
    for n in set(armed_starts or []):
        if n in graph.nodes:
            satisfied.update(f"{e.dst}|{e.src}" for e in graph.out_edges(n))
    reachable: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n in graph.nodes:
            if n in executed_nodes or n in graph.starts or n in reachable:
                continue
            in_edges = [f"{e.dst}|{e.src}" for e in graph.edges if e.dst == n]
            if not in_edges:
                # 无入边的非 start 节点永不 fire（engine 空 producer 规则），跳过
                continue
            if graph.nodes[n].join == "OR":
                fire = any(k in satisfied for k in in_edges)
            else:
                fire = all(k in satisfied for k in in_edges)
            if not fire:
                continue
            reachable.add(n)
            for e in graph.out_edges(n):
                key = f"{e.dst}|{e.src}"
                if key not in satisfied:
                    satisfied.add(key)
                    changed = True
    return reachable


def check_resume_compat(
    new_tasklist: Tasklist,
    graph: Graph,
    executed_nodes: set[str],
    old_tasklist: Tasklist | None = None,
    marking_slots: dict[str, bool] | None = None,
    armed_starts: set[str] | list[str] | None = None,
) -> ResumeCheck:
    """新 tasklist 与已执行节点的兼容性校验。

    - 硬错误 1：新 task 的 inputs 引用的 producer 不在新图节点集合中
      （常量引用跳过：``{spec.xxx}`` 与裸 token ``{spec}``/``{tasklist}``/
      ``{node}``——graph_builder 注册时解析为 spec_inputs，此处复用
      ``_is_constant_ref`` 保持单一事实源）。
    - 硬错误 2：新图中**新成为** start 且有历史输出的节点（armed_starts
      一次性，永不重跑；底层 ``_warn_graph_changes`` 在 remap old==new 时
      不触发，此处补上）。"新成为"判定：与旧 tasklist flow 的 start 集合
      对比（flow 先经 ``prepare_flow`` 规范化——无 ``[`` 标记时自动把首
      token 包成 start，与 graph 构建一致——再正则 ``\\[(\\w+)\\]`` 提取
      start 集合）——正常 resume 里旧图 start 有历史是常态，不能误报。无存档
      （old_tasklist=None）时降级为警告（保守）。
    - 警告 1：已执行节点在新 tasklist 中被修改（对比 module_inputs 存档，
      修改对已执行部分不生效）。
    - 警告 2：inputs 引用的 producer 未执行、且不是 consumer 的拓扑上游
      （运行时 resolve 为 Missing，prompt 占位符保留字面量）。
    - 警告 3：未执行且非 start 的节点，从检查点 marking 出发经不动点模拟
      仍不可达——该节点永远不会 fire。``remap_graph`` 移植 slot 时新边取
      ``old_slots.get(key, False)``（runner.py:405）：新节点/改名节点的入边
      在旧 marking 中不存在 → False；旧边已消费也是 False。不动点模拟考虑
      "入边已满足的节点将 fire 并产出下游 slot"（深回退场景上游将重跑），
      guard 边乐观传播（见 ``_reachable_from_marking`` 的取舍说明）。需回退
      到更早检查点让其上游重新执行，或设为 start。``marking_slots`` 为检查点
      snapshot 的 ``marking.slots``，键格式 ``"dst|src"``（与
      ``Marking.to_json`` 一致，engine.py:71）；为 None 时跳过本检查。
      ``armed_starts`` 为检查点 snapshot 的 ``marking.armed_starts``（排序
      list，engine.py:72）——武装的 start 续跑时无条件 fire 并写下游 slot
      （``engine._join_satisfied`` 首分支，engine.py:103-106），模拟将其出边
      并入初始 satisfied（如 resume 到"运行前手动检查点"：armed_starts 非空、
      slots 全空）；为 None 时跳过。

    返回 ResumeCheck；调用方在 hard_errors 非空时 raise ResumeError。
    """
    hard_errors: list[str] = []
    warnings: list[str] = []
    tasks = new_tasklist.tasks

    for key, task in tasks.items():
        for field, producer in (task.inputs or {}).items():
            if _is_constant_ref(producer):
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
    # tasklist flow 的 start 集合判定"新成为"。flow 先经 prepare_flow 规范化
    # （无 [ 标记时首 token 自动包成 start，与 graph 构建一致），再提取 [A]。
    old_starts: set[str] = set()
    if old_tasklist is not None:
        old_starts = set(re.findall(r"\[(\w+)\]", prepare_flow(old_tasklist.flow)))
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

    # 警告 3：未执行非 start 节点，从检查点 marking 出发经不动点模拟仍不可达
    # → 永不 fire。模拟考虑"入边已满足的上游节点将 fire 并产出下游 slot"，
    # 消除深回退（回退到 ≥2 层上游）场景的误报——那是核心工作流。
    if marking_slots is not None:
        reachable = _reachable_from_marking(
            graph, executed_nodes, marking_slots, armed_starts
        )
        for n in graph.nodes:
            if n in executed_nodes or n in graph.starts or n in reachable:
                continue
            in_edges = [f"{e.dst}|{e.src}" for e in graph.edges if e.dst == n]
            if not in_edges:
                continue
            warnings.append(
                f"Node '{n}' 的入边在检查点均未满足（新边或已消费）——"
                f"该节点不会自动执行。需回退到更早检查点使其上游重新执行，"
                f"或将其设为 start。"
            )

    return ResumeCheck(hard_errors=hard_errors, warnings=warnings)
