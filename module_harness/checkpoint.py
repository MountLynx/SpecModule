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
