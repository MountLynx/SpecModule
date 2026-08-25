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
    """``<base>/.specmodule/runs/<module_id>/run.sqlite``（与 Module 对齐）。"""
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
        node = d.get("node")
        if not node:
            continue          # 空 node 行跳过（损坏/异常行不阻断时间线）
        try:
            tick = int(d.get("tick", 0))
        except (TypeError, ValueError):
            log.warning("跳过损坏的 firing 行（tick 非法）: %r", d)
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


def _latest_of(entries: list[ReviewEntry]) -> int | None:
    """过滤后子集中最新 tick（无条目 → None）。"""
    return max((e.tick for e in entries), default=None)


def filter_failed(timeline: ReviewTimeline) -> ReviewTimeline:
    """只看失败/中止节点（定位问题 tick 的核心路径）。

    latest_tick 重算为过滤子集中的最新 tick（避免过滤后仍报全局 tick 误导）。
    """
    entries = [e for e in timeline.entries if e.status != "ok"]
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=entries,
        latest_tick=_latest_of(entries),
    )


def filter_tick(timeline: ReviewTimeline, tick: int) -> ReviewTimeline:
    """只看指定 tick。"""
    entries = [e for e in timeline.entries if e.tick == tick]
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=entries,
        latest_tick=_latest_of(entries),
    )


def filter_node(timeline: ReviewTimeline, node: str) -> ReviewTimeline:
    """只看指定节点的全部 firing（含 loop 多轮）。"""
    entries = [e for e in timeline.entries if e.node == node]
    return ReviewTimeline(
        module_id=timeline.module_id,
        entries=entries,
        latest_tick=_latest_of(entries),
    )


@dataclass
class CheckpointEntry:
    """单个回退点（tick 快照或 manual 检查点）。"""

    target: str            # resume/rollback 直传的目标（"3" 或 "manual:xxx"）
    tick: int
    kind: str              # "tick"（snapshots 表每 tick 快照）| "manual"（checkpoint() 命名点）
    fired: list[str] = field(default_factory=list)  # tick 快照本 tick fire 的节点；manual 为空
    label: str | None = None                         # manual label；tick 快照为 None


@dataclass
class CheckpointList:
    """全部回退点，按 tick 升序（同 tick 的 manual 排 tick 快照后）。"""

    module_id: str
    entries: list[CheckpointEntry] = field(default_factory=list)


def build_checkpoints(module_id: str, base_dir: Path | None = None) -> CheckpointList | None:
    """列出可用回退点（snapshots 表 tick 快照 + checkpoints 表 manual 检查点）。

    数据源：run.sqlite；无 DB / 读失败 → None（容错哲学同 build_timeline——
    监控方绝不被 DB 锁搞崩）。`resume <target>` / `rollback <target>` 的
    target 即条目 ``target`` 字段。
    """
    db_path = _run_db_path(module_id, base_dir)
    if not db_path.exists():
        return None
    try:
        from tickflow.persistence import SqliteBackend

        backend = SqliteBackend(db_path)
        try:
            entries: list[CheckpointEntry] = []
            for tick in backend.list_snapshots(module_id):
                snap = backend.load_snapshot(module_id, tick)
                fired = list(snap.get("fired", [])) if snap else []
                entries.append(
                    CheckpointEntry(
                        target=str(tick), tick=tick, kind="tick", fired=fired
                    )
                )
            entries.extend(
                CheckpointEntry(target=label, tick=tick, kind="manual", label=label)
                for label, tick in backend.list_checkpoints(module_id)
            )
        finally:
            backend.close()
    except Exception:
        log.exception("读取 run.sqlite 失败（返回 None）: %s", db_path)
        return None
    entries.sort(key=lambda e: (e.tick, 0 if e.kind == "tick" else 1))
    return CheckpointList(module_id=module_id, entries=entries)


def checkpoints_to_dict(cl: CheckpointList) -> dict[str, Any]:
    """JSON 出口（MCP/Web 直接消费同一函数）。"""
    return {
        "module_id": cl.module_id,
        "checkpoints": [
            {
                "target": e.target,
                "tick": e.tick,
                "kind": e.kind,
                "fired": list(e.fired),
                "label": e.label,
            }
            for e in cl.entries
        ],
    }


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


@dataclass
class QueryValueResult:
    """细粒度查询结果：tick + 命中值 / 未命中时的可用键（MCP peek 用）。"""

    tick: int | None
    value: Any = None
    found: bool = True
    available: list[str] | None = None


_ROOT_FIELDS = ("phase", "status", "tick", "fireable", "fired", "error", "updated_at")


def _resolve_path(root: Any, segments: list[str]) -> tuple[bool, Any, list[str] | None]:
    """按段导航 dict/list；未命中返回 (False, None, 失败处可用键/下标)。"""
    cur = root
    for seg in segments:
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
                continue
            return False, None, sorted(str(k) for k in cur)
        if isinstance(cur, list):
            if seg.isdigit() and int(seg) < len(cur):
                cur = cur[int(seg)]
                continue
            return False, None, [str(i) for i in range(len(cur))]
        return False, None, []
    return True, cur, None


def query_value(
    module_id: str, path: str, *, base_dir: Path | None = None
) -> QueryValueResult | None:
    """查询运行中 dict 的特定键当前值（MCP peek 工具库侧实现）。

    寻址语法：dot-path，整数段 = list 下标。
    - 顶层标量：phase / status / tick / fireable / fired / error / updated_at
    - 输出：``outputs.<node>.<key...>``（节点最新输出内部键）
    - 可变状态：``state.<node>.<key...>``（含 _llm_raw 等调试字段）
    未开始/无数据 → None（容错哲学同 query_run_status）；空 path → ValueError。
    """
    if not path:
        raise ValueError("path 不能为空")
    from .status import query_run_status

    st = query_run_status(module_id, base_dir)
    if st is None:
        return None
    segments = path.split(".")
    root = segments[0]
    if root in ("outputs", "state"):
        base = st.outputs if root == "outputs" else st.node_states
        found, value, available = _resolve_path(base, segments[1:])
        return QueryValueResult(
            tick=st.tick, value=value, found=found, available=available
        )
    if root in _ROOT_FIELDS:
        if len(segments) > 1:
            return QueryValueResult(tick=st.tick, found=False, available=[])
        return QueryValueResult(tick=st.tick, value=getattr(st, root))
    return QueryValueResult(
        tick=st.tick, found=False,
        available=["outputs", "state", *_ROOT_FIELDS],
    )
