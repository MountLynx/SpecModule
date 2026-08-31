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


def run_db_path(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base>/.specmodule/runs/<module_id>/run.sqlite``（与 Module 对齐）。

    公开 API：CLI/MCP/Web 消费（路径规则单一来源）。
    base_dir 缺省 = Path.cwd()（服务器进程 cwd ≠ agent cwd，消费方宜显式传）。
    """
    base = base_dir if base_dir is not None else Path.cwd()
    return base / ".specmodule" / "runs" / module_id / "run.sqlite"


def _executed_nodes(backend: Any, module_id: str, tick: int) -> set[str]:
    """firings 表中 tick < 快照 tick 的去重节点（resume 已执行判定，单一事实源）。

    快照 tick N 在 tick N-1 结束后落盘，tick == N 的 firing 属 restore 后
    会被重跑的部分，不算已执行。firings 按 module_id 累积（跨多次 run），
    前一轮 run 的记录也会计入——仅影响提示性警告 1/3 的准确性，不影响硬错误。
    """
    return {
        d["node"]
        for d in backend.list_firings(module_id)
        if d.get("node") and int(d.get("tick", 0)) < tick
    }


def build_timeline(module_id: str, base_dir: Path | None = None) -> ReviewTimeline | None:
    """从 run.sqlite 构建审阅时间线。无 DB / 读失败 → None。"""
    db_path = run_db_path(module_id, base_dir=base_dir)
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
    db_path = run_db_path(module_id, base_dir=base_dir)
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


def _resolve_tick_snapshot(
    backend: Any, module_id: str, tick: int | None
) -> tuple[int, dict[str, Any]]:
    """tick 快照解析共用块：缺省最新 → 存在性校验 → 读取。

    错误消息为 CLI/MCP 共享契约（create_checkpoint 与 load_snapshot_summary
    逐字一致）：无快照 / tick 不存在（携带可用清单）/ 快照损坏。
    """
    ticks = backend.list_snapshots(module_id)
    if not ticks:
        raise KeyError(f"无可恢复快照: {module_id}（运行未产生任何 tick 快照）")
    target = max(ticks) if tick is None else tick
    if target not in ticks:
        raise KeyError(f"快照 tick {target} 不存在（可用: {sorted(ticks)}）")
    snap = backend.load_snapshot(module_id, target)
    if snap is None:
        raise KeyError(f"快照 tick {target} 读取失败（数据损坏？）")
    return target, snap


def create_checkpoint(
    module_id: str,
    label: str,
    *,
    tick: int | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """给 tick 快照起命名检查点（复制到 checkpoints 表，manual 永久保留）。

    纯数据操作、跨进程：不依赖运行中的 runner——快照已每 tick 落盘，命名 =
    给已有快照加人类标签，之后 ``resume/rollback manual:<label>`` 按名回退。
    label 自动补 ``manual:`` 前缀（目标解析要求）；tick 缺省 = 最新。

    Raises:
        KeyError: 无运行记录 / 无 tick 快照 / tick 不存在（消息携带可用清单）。
    """
    db_path = run_db_path(module_id, base_dir)
    if not db_path.exists():
        raise KeyError(f"无运行记录: {module_id}（先执行 run）")
    from tickflow.persistence import SqliteBackend

    backend = SqliteBackend(db_path)
    try:
        target, snap = _resolve_tick_snapshot(backend, module_id, tick)
        old_labels = [lbl for lbl, _ in backend.list_checkpoints(module_id)]
        label = label if label.startswith("manual:") else "manual:" + label
        backend.save_checkpoint(module_id, label, snap)
    finally:
        backend.close()
    return {"label": label, "tick": target, "overwritten": label in old_labels}


def load_snapshot_summary(
    module_id: str,
    *,
    tick: int | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """检视某 tick 的运行时快照摘要（status/fireable/fired/各节点最新输出）。

    outputs 取 firings 表各节点最新值（非该 tick 时点值——与 CLI snapshot
    文本模式一致；时点输出用 review(tick=N) 查）。db 缺失/读失败 → None
    （查询容错，同 build_timeline——监控方绝不被 DB 锁搞崩）；无快照/tick
    不存在 → KeyError（消息携带可用清单）。
    """
    db_path = run_db_path(module_id, base_dir)
    if not db_path.exists():
        return None
    from tickflow.persistence import SqliteBackend

    backend = SqliteBackend(db_path)
    try:
        target, snap = _resolve_tick_snapshot(backend, module_id, tick)
        latest = backend.latest_firings(module_id)
        outputs = {d["node"]: d.get("output") for d in latest if d.get("node")}
    except KeyError:
        raise
    except Exception:
        log.exception("读取 run.sqlite 失败（返回 None）: %s", db_path)
        return None
    finally:
        backend.close()
    out: dict[str, Any] = {
        "tick": snap.get("tick", target),
        "status": snap.get("status", "?"),
        "fireable": list(snap.get("fireable") or []),
        "fired": list(snap.get("fired") or []),
        "outputs": outputs,
    }
    if snap.get("cancel_reason"):
        out["cancel_reason"] = snap["cancel_reason"]
    return out


def read_module_inputs(
    module_id: str, base_dir: Path | None = None
) -> dict[str, Any] | None:
    """读运行输入存档（module_inputs 表：本次 run 使用的 spec/tasklist）。

    消费场景：resume/rollback 前端预填上次输入（换 spec/tasklist 重传的
    编辑起点）。db 缺失 / 无存档 / 读失败 → None（查询容错，同上）。
    """
    db_path = run_db_path(module_id, base_dir)
    if not db_path.exists():
        return None
    from .checkpoint import ModuleInputStore

    store = ModuleInputStore(module_id, base_dir)
    try:
        return store.load_module_inputs()
    except Exception:
        log.exception("读取 module_inputs 失败（返回 None）: %s", db_path)
        return None
    finally:
        store.close()


def check_resume_compat_from_run(
    module_name: str,
    run_id: str,
    *,
    new_tasklist: Any = None,
    target: int | str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """恢复预检：从运行产物组合材料跑兼容性校验——不 spawn、不写状态。

    resume（module.py）内联组合的第二消费端收编：目标快照解析 →
    executed_nodes（_executed_nodes）→ 旧输入存档（read_module_inputs）→
    建图（build_run_graph 同款 Mock registry 通道，零 LLM；new_tasklist
    给出时走直渲染通道）→ check_resume_compat（marking.slots / armed_starts
    取自目标快照）。

    - ``new_tasklist``：dict 或 Tasklist；None = 用归档 tasklist（纯续跑预检）。
    - ``target``：tick 号 / ``"manual:<label>"`` / None（最新快照）。

    错误契约：run.sqlite 缺失/读失败 → None（查询容错，调用方映射 404）；
    tasklist 非法 / 建图失败 → ValueError（消息可直接面向用户，调用方映射
    400）；目标解析失败**不 raise**——作为 ``hard_errors[0]`` 返回（消息与
    module.py 的 KeyError 文案一致并附可用清单），``target``/``target_tick``
    为 None。

    返回 ``{"target": str | None, "target_tick": int | None,
    "executed_nodes": [str], "hard_errors": [str], "warnings": [str]}``。
    """
    from .checkpoint import check_resume_compat, tasklist_from_dict

    db_path = run_db_path(run_id, base_dir)
    if not db_path.exists():
        return None
    try:
        from tickflow.persistence import SqliteBackend

        backend = SqliteBackend(db_path)
        try:
            hard_errors: list[str] = []
            snap: dict[str, Any] | None = None
            target_tick: int | None = None
            ticks = backend.list_snapshots(run_id)
            manual = [lbl for lbl, _ in backend.list_checkpoints(run_id)]
            if not ticks and not manual:
                hard_errors.append(f"无可恢复快照: {run_id}（运行未产生任何 tick 快照）")
            elif isinstance(target, int) or (
                isinstance(target, str) and target.isdigit()
            ):
                t = int(target)
                if t in ticks:
                    snap = backend.load_snapshot(run_id, t)
                    if snap is None:
                        hard_errors.append(f"快照 tick {t} 读取失败（数据损坏？）")
                    else:
                        target_tick = t
                else:
                    hard_errors.append(
                        f"回退目标 {target!r} 不存在"
                        f"（可用 tick: {ticks or '无'}；manual: {manual or '无'}）"
                    )
            elif isinstance(target, str) and target.startswith("manual:"):
                snap = backend.load_checkpoint(run_id, target)
                if snap is None:
                    hard_errors.append(
                        f"回退目标 {target!r} 不存在"
                        f"（可用 tick: {ticks or '无'}；manual: {manual or '无'}）"
                    )
                else:
                    target_tick = int(snap.get("tick", 0))
            elif target is not None:
                hard_errors.append(
                    f"回退目标 {target!r} 不存在"
                    f"（可用 tick: {ticks or '无'}；manual: {manual or '无'}）"
                )
            else:
                if not ticks:
                    hard_errors.append(
                        f"无可恢复快照: {run_id}（运行未产生任何 tick 快照）"
                    )
                else:
                    target_tick = max(ticks)
                    snap = backend.load_snapshot(run_id, target_tick)

            executed: set[str] = set()
            if snap is not None:
                executed = _executed_nodes(backend, run_id, int(snap.get("tick", 0)))
        finally:
            backend.close()
    except Exception:
        log.exception("读取 run.sqlite 失败（返回 None）: %s", db_path)
        return None

    if hard_errors:
        return {"target": None, "target_tick": None, "executed_nodes": [],
                "hard_errors": hard_errors, "warnings": []}

    old_tl = None
    old_inputs = read_module_inputs(run_id, base_dir=base_dir)
    if old_inputs is not None:
        old_tl = tasklist_from_dict(old_inputs["tasklist"])

    try:
        built = build_run_graph(module_name, run_id, base_dir=base_dir,
                                tasklist=new_tasklist)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"预检建图失败: {e}") from e
    if built is None:
        raise ValueError("无 tasklist 可预检（无归档且未传 new_tasklist）")
    graph, new_tl = built

    marking = (snap or {}).get("marking") or {}
    check = check_resume_compat(
        new_tl, graph, executed,
        old_tasklist=old_tl,
        marking_slots=marking.get("slots"),
        armed_starts=marking.get("armed_starts"),
    )
    target_str = target if isinstance(target, str) else (
        str(target) if target is not None else str(target_tick))
    return {
        "target": target_str,
        "target_tick": target_tick,
        "executed_nodes": sorted(executed),
        "hard_errors": list(check.hard_errors),
        "warnings": list(check.warnings),
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


def graph_to_dict(graph: Any, tasklist: Any) -> dict[str, Any]:
    """tickflow Graph + Tasklist → 前端可视化结构（唯一新数据形状）。

    nodes 的 ``type``/``inputs`` 取 tasklist 原始声明（Graph 节点 inputs 有
    field/producer 双键污染，不直接透出）；``inputs`` 即 ``{field: producer}``。
    Graph 中存在而 tasklist 无对应 task 的节点按 ``type="unknown"`` 降级
    （存档与代码漂移时不阻断渲染）。CLI/MCP/Web 共用。
    """
    tasks = tasklist.tasks
    nodes = []
    for name, n in graph.nodes.items():
        t = tasks.get(name)
        nodes.append({
            "id": name,
            "label": name,
            "type": t.type if t is not None else "unknown",
            "is_start": bool(n.is_start),
            "join": n.join,
            "inputs": dict(t.inputs) if (t is not None and t.inputs) else {},
        })
    return {
        "nodes": nodes,
        "edges": [
            {"from": e.src, "to": e.dst, "guard": e.guard} for e in graph.edges
        ],
        "starts": list(graph.starts),
    }


def build_run_graph(
    module_name: str,
    run_id: str | None = None,
    *,
    base_dir: Path | None = None,
    template: str | None = None,
    tasklist: Any = None,
    src: Any = None,
) -> tuple[Any, Any] | None:
    """从运行存档（或直接给定 tasklist）重建 tickflow Graph——可视化共用。

    原 CLI ``visualize`` 组合的共享层收编（CLI/Web 共用，零 LLM——registry 用
    MockLLMClient 占位）：模块解析 → Mock registry → module_inputs 存档 →
    TasklistTranslator.build。

    - ``tasklist``：dict（{Tasks, Flow}）或 Tasklist 对象；给出时跳过存档
      （直渲染通道）。
    - ``src``：预解析 ModuleSource（CLI 显式 --modules-dir 分支直通）；缺省
      ``store.resolve_module`` 统一搜索路径解析。
    - 返回 ``(Graph, Tasklist)``；无存档且未传 tasklist → None；模块未找到/
      加载失败/tasklist 构建失败 → ValueError（消息可直接面向用户）。
    """
    from llm.mock import MockLLMClient

    from . import store
    from .checkpoint import ModuleInputStore
    from .events import EventBus
    from .graph_builder import TasklistTranslator
    from .registry import HarnessRegistry
    from .spec import Spec, Tasklist

    if src is None:
        src = store.resolve_module(module_name)
        if src is None:
            raise ValueError(f"模块 '{module_name}' 未找到（specmodule list 查看全部）")
    run_id = run_id or module_name
    event_bus = EventBus()

    tl: Tasklist | None = None
    spec_data: dict | None = None
    if isinstance(tasklist, Tasklist):
        tl = tasklist
    elif isinstance(tasklist, dict):
        tl = Tasklist.from_json(tasklist)
    elif tasklist is None:
        istore = ModuleInputStore(run_id, base_dir)
        try:
            inputs = istore.load_module_inputs()
        finally:
            istore.close()
        if inputs is not None:
            tl = Tasklist.from_json(inputs["tasklist"])
            spec_data = inputs["spec"]
    else:
        raise TypeError(f"tasklist 类型不支持: {type(tasklist)!r}")

    if src.is_packed:
        from .loader import ModuleLoader

        try:
            sub = ModuleLoader().load(src.path, lazy_client=True)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"模块 '{module_name}' 加载失败: {e}") from e
        registry = sub._build_registry(
            False, llm_client=MockLLMClient(), event_bus=event_bus
        )
        modules = sub.modules
        if tl is None:
            tl = sub.tasklist
    else:
        from .entry import discover_modules

        entry = discover_modules(src.path.parent).get(module_name)
        if entry is None:
            raise ValueError(f"模块 '{module_name}' 入口解析失败")
        template_name = template or entry.default_template
        if entry.build_registry is not None:
            registry = entry.build_registry(
                MockLLMClient(), template_name, event_bus
            )
        else:
            registry = HarnessRegistry(
                llm_client=MockLLMClient(), event_bus=event_bus
            )
        modules = entry.submodules
    if tl is None:
        return None
    builder = TasklistTranslator(
        registry, module_id=run_id, modules=modules, llm_client=MockLLMClient()
    )
    graph, _ = builder.build(tl, Spec(spec_data) if spec_data is not None else None)
    return graph, tl
