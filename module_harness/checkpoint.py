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
