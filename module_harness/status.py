# module_harness/status.py
"""运行状态查询 — 跨进程读取 Module 当前运行状态（roadmap #7）。

数据真相源：``.specmodule/runs/<module_id>/status.json``（阶段级，Module 原子写）
+ ``run.sqlite`` 最新快照（tick 级，persist 模式每 tick 写）。
独立于 Module 内部实现——任何进程可直接查询。

并发安全：SQLite WAL 模式下单写者 + 多读者读写互不阻塞（实测 500 次写对撞
读取 0 次 database is locked）。同一 module_id 不可并发（双写者会锁冲突）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ModuleStatus:
    """Module 运行状态静态快照。"""

    module_id: str
    phase: str                 # idle/translating/reviewing/building/ready/running/done/aborted/cancelled
    status: str | None = None  # tickflow RunStatus（"RUNNING"/"IDLE"/...；无 DB 时为 None）
    tick: int | None = None    # 最新快照 tick（无 DB 时为 None）
    fireable: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)     # node → 最新输出
    node_states: dict[str, dict] = field(default_factory=dict)  # node → mutable state
    error: str | None = None
    updated_at: float = 0.0


def _run_dir(module_id: str, base_dir: Path) -> Path:
    return base_dir / ".specmodule" / "runs" / module_id


def query_run_status(
    module_id: str, base_dir: Path | None = None
) -> ModuleStatus | None:
    """查询 Module 当前运行状态。未开始（status.json 缺失）→ None。

    有 ``run.sqlite`` 时叠加最新快照的 tick 级信息；DB 读失败降级为
    phase-only（监控方绝不被 DB 锁搞崩）。
    """
    base = base_dir or Path.cwd()
    run_dir = _run_dir(module_id, base)
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return None
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("status.json 损坏或不可读: %s", status_path)
        return None

    st = ModuleStatus(
        module_id=str(data.get("module_id", module_id)),
        phase=str(data.get("phase", "unknown")),
        error=data.get("error"),
        updated_at=float(data.get("updated_at", 0.0)),
    )

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
                    st.outputs = {
                        n: lst[-1][1] for n, lst in snap.get("edges", {}).items()
                    }
                    st.node_states = dict(snap.get("state", {}))
            finally:
                backend.close()
        except Exception:
            log.exception("读取 run.sqlite 失败，降级为 phase-only")
    return st
