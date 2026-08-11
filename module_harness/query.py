# module_harness/query.py
"""共享查询层：review 历史时间线组合（roadmap Phase 0）。

CLI（host + 查询形态）、MCP、Web 三形态共同消费本模块——形态只 import，
绝不重实现。数据源：run.sqlite 的 firings 表；容错哲学同 query_run_status
（DB 读失败返回 None，监控方绝不被 DB 锁搞崩）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ReviewEntry:
    """单个节点 firing 的审阅记录。"""

    tick: int
    node: str
    status: str          # ok | failed | aborted
    output: Any = None
    error: str | None = None


@dataclass
class ReviewTimeline:
    """完整审阅时间线：firings 顺序，同 (tick, node) 去重 keep-first。"""

    module_id: str
    entries: list[ReviewEntry] = field(default_factory=list)
    latest_tick: int | None = None


def _run_db_path(module_id: str, base_dir: Path | None) -> Path:
    """``<base>/<cwd>/.specmodule/runs/<module_id>/run.sqlite``（与 Module 对齐）。"""
    base = base_dir if base_dir is not None else Path.cwd()
    return base / ".specmodule" / "runs" / module_id / "run.sqlite"


def build_timeline(module_id: str, base_dir: Path | None = None) -> ReviewTimeline | None:
    """从 run.sqlite 构建审阅时间线。无 DB / 读失败 → None。"""
    db_path = _run_db_path(module_id, base_dir)
    if not db_path.exists():
        return None
    try:
        from tickflow.persistence import SqliteBackend

        backend = SqliteBackend(db_path)
        try:
            rows = backend.list_firings(module_id)
        finally:
            backend.close()
    except Exception:
        log.exception("读取 run.sqlite 失败（返回 None）: %s", db_path)
        return None

    entries: list[ReviewEntry] = []
    seen: set[tuple[int, str]] = set()
    latest: int | None = None
    for d in rows:
        tick = int(d.get("tick", 0))
        node = d.get("node")
        if not node:
            continue
        if (tick, node) in seen:
            continue          # 与 tickflow audit() 去重语义一致（restore 重放兼容）
        seen.add((tick, node))
        entries.append(
            ReviewEntry(
                tick=tick,
                node=node,
                status=str(d.get("status", "ok")),
                output=d.get("output"),
                error=d.get("error"),
            )
        )
        latest = tick if latest is None else max(latest, tick)
    return ReviewTimeline(module_id=module_id, entries=entries, latest_tick=latest)


def filter_failed(timeline: ReviewTimeline) -> ReviewTimeline:
    """只看失败/中止节点（定位问题 tick 的核心路径）。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.status != "ok"],
        latest_tick=timeline.latest_tick,
    )


def filter_tick(timeline: ReviewTimeline, tick: int) -> ReviewTimeline:
    """只看指定 tick。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.tick == tick],
        latest_tick=timeline.latest_tick,
    )


def filter_node(timeline: ReviewTimeline, node: str) -> ReviewTimeline:
    """只看指定节点的全部 firing（含 loop 多轮）。"""
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=[e for e in timeline.entries if e.node == node],
        latest_tick=timeline.latest_tick,
    )


def timeline_to_dict(timeline: ReviewTimeline) -> dict[str, Any]:
    """JSON 出口（MCP/Web 直接消费同一函数）。"""
    return {
        "module_id": timeline.module_id,
        "latest_tick": timeline.latest_tick,
        "entries": [
            {
                "tick": e.tick,
                "node": e.node,
                "status": e.status,
                "output": e.output,
                "error": e.error,
            }
            for e in timeline.entries
        ],
    }