"""Module 编排器 — spec + template/tasklist → tasklist → runner。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend, SqliteBackend

from .spec import Spec, Tasklist
from .consistency import ConsistencyError, ConsistencyReport, ConsistencyReviewer
from .translator import Translator, TemplateLoader, TasklistValidator
from .graph_builder import TasklistTranslator
from .registry import HarnessRegistry
from .events import EventBus, ConsistencyReviewed
from .checkpoint import (
    ModuleInputStore,
    ResumeError,
    check_resume_compat,
    tasklist_from_dict,
    tasklist_to_dict,
)
from .control import clear_control, control_tick_end, control_tick_start

log = logging.getLogger(__name__)


def _persist_dir(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base_dir>/.specmodule/runs/<run_id>/run.sqlite``（D9）。

    run_id = module_id：一个任务一次运行一个子目录、一个独立 SQLite 数据库。
    base_dir 缺省 = 当前工作目录。
    """
    return (base_dir or Path.cwd()) / ".specmodule" / "runs" / module_id / "run.sqlite"


def _status_path(module_id: str, base_dir: Path | None = None) -> Path:
    """``<base_dir>/.specmodule/runs/<module_id>/status.json``（roadmap #7）。

    阶段级运行状态文件：与 run.sqlite 同目录，跨进程查询的轻量通道。
    """
    return (base_dir or Path.cwd()) / ".specmodule" / "runs" / module_id / "status.json"


class Module:
    """SpecModule 的核心编排器。

    spec + template → 翻译 → tasklist → tickflow Graph + registry → AsyncRunner
    或 spec + tasklist（自定义）→ 校验 + 一致性审核 → AsyncRunner。
    """

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        template_name: str | None = None,
        tasklist: Tasklist | None = None,
        llm_client: Any,
        event_bus: EventBus | None = None,
        template_loader: TemplateLoader | None = None,
        module_id: str | None = None,
        base_dir: Path | None = None,
        registry: HarnessRegistry | None = None,
        review_harness: str | None = "spec_tasklist_review",
        keep_records: bool = True,
        persist: bool = True,
        status_file: bool = True,
        control: bool = True,
        modules: dict[str, Any] | None = None,
        hooks: dict | None = None,
    ) -> None:
        if (template_name is None) == (tasklist is None):
            raise ValueError("template_name 与 tasklist 必须且只能传一个")
        self.spec = Spec(spec)
        self.template_name = template_name
        self.tasklist = tasklist
        self.review_harness = review_harness
        self.keep_records = keep_records
        # True（默认）：构造 .specmodule/runs/<run_id>/run.sqlite 持久 backend（D9）
        # False：快速模式——NullBackend 全内存，零落盘零 I/O（D7 语义正式化）
        self.persist = persist
        # True（默认）：写 .specmodule/runs/<module_id>/status.json
        # （阶段级，跨进程查询通道）；False：零残留（快速模式可用）
        self.status_file = status_file
        # True（默认）：注册控制文件 hook（control.json → cancel/pause，
        # 见 control.py）。跨进程取消/暂停的协作式通道；False 关闭。
        self.control = control
        self.review_result: ConsistencyReport | None = None
        self.module_id = module_id or f"mod_{uuid.uuid4().hex[:8]}"
        self._base_dir = base_dir or Path.cwd()
        self._llm_client = llm_client
        # submodule 引用解析表 {tasklist 名: SubModule 类}：TasklistValidator 校验
        # submodule 节点（T2）与 TasklistTranslator 构建嵌套子图（T6）共用
        self._modules = dict(modules or {})
        # runner hooks 透传（观察通道）：{hook名: async/sync 回调}，构造
        # runner 后注册。CLI 实时显示使用 on_tick_start/on_fire。
        self._hooks = dict(hooks or {})

        if registry is not None:
            self._reg = registry
        else:
            self._reg = HarnessRegistry(
                llm_client=llm_client,
                event_bus=event_bus or EventBus.null(),
            )
        self._loader = template_loader or TemplateLoader()
        self._translator = Translator(self._reg)
        self._write_phase("idle")

        # roadmap #5：runner 由 _build_runner_async 持有；快照/回滚 API 依赖它
        self._runner: AsyncRunner | None = None
        self._last_tasklist: Tasklist | None = None
        self._input_store: ModuleInputStore | None = None

    # ------------------------------------------------------------------
    # 运行状态（roadmap #7）
    # ------------------------------------------------------------------

    def _write_phase(self, phase: str, error: str | None = None) -> None:
        """原子写 status.json（tmp + os.replace）。失败仅 log，不阻断运行。

        phase 取值：idle/translating/reviewing/building/ready/running/
        done/aborted/cancelled。status_file=False 时不写盘（零残留）。
        """
        if not self.status_file:
            return
        path = _status_path(self.module_id, self._base_dir)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({
                    "module_id": self.module_id,
                    "phase": phase,
                    "error": error,
                    "updated_at": time.time(),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            log.exception("写 status.json 失败（不阻断运行）: %s", path)

    def build_runner(self) -> AsyncRunner:
        """执行翻译 → 构建 graph → 返回 AsyncRunner。

        Note: 这是一个同步方法。在 async 上下文中直接使用
        await module._build_runner_async() 或 await module.run()。
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._build_runner_async())
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        finally:
            loop.close()

    async def _build_runner_async(self) -> AsyncRunner:
        """异步版 build_runner。"""
        if self.tasklist is not None:
            self._write_phase("reviewing")
            tasklist = self.tasklist
            errors = TasklistValidator.validate(tasklist, self._reg, self._modules)
            if errors:
                raise ValueError(
                    "tasklist 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)
                )
            if self.review_harness is not None:
                report = await ConsistencyReviewer(
                    self._reg, self.review_harness
                ).review(self.spec, tasklist)
                self.review_result = report
                self._reg._event_bus.emit(ConsistencyReviewed(
                    timestamp=time.monotonic(), node="__review__", tick=0,
                    consistent=report.consistent,
                    suggestions=report.suggestions,
                    raw=report.raw,
                ))
                if not report.consistent:
                    raise ConsistencyError(report)
        else:
            self._write_phase("translating")
            template = self._loader.get(self.template_name)
            if template is None:
                raise ValueError(f"模板 '{self.template_name}' 未找到")
            tasklist = await self._translator.translate(self.spec, template)
        self._write_phase("building")
        builder = TasklistTranslator(
            self._reg, self.module_id,
            modules=self._modules, llm_client=self._llm_client,
        )
        graph, reg = builder.build(tasklist, spec=self.spec)
        self._last_tasklist = tasklist
        backend = (
            SqliteBackend(_persist_dir(self.module_id, self._base_dir))
            if self.persist
            else NullBackend()
        )
        self._write_phase("ready")
        runner = AsyncRunner(
            graph,
            registry=reg,
            keep_records=self.keep_records,
            backend=backend,
            session_id=self.module_id,
        )
        self._runner = runner
        for _hook_name, _cb in self._hooks.items():
            _register = getattr(runner, _hook_name, None)
            if callable(_register):
                _register(_cb)
            else:
                log.warning("Module hooks: 未知 runner hook '%s'（忽略）", _hook_name)
        if self.control:
            # 跨进程控制通道（control.json → cancel/pause）：与用户 hooks
            # 并存——runner 的 hook 注册表是 list，追加不覆盖。cancel 在
            # tick_end 消费（tick_start 期设终态会被引擎同 tick 赋值冲掉）、
            # pause 在 tick_start 挂起，见 control.py 模块 docstring。
            runner.on_tick_start(
                control_tick_start(runner, self.module_id, base_dir=self._base_dir)
            )
            runner.on_tick_end(
                control_tick_end(runner, self.module_id, base_dir=self._base_dir)
            )
        return runner

    # ------------------------------------------------------------------
    # 快照/回滚（roadmap #5）
    # ------------------------------------------------------------------

    def _require_runner(self) -> AsyncRunner:
        """快照/回滚 API 前置守卫：runner 未构建时抛错。"""
        if self._runner is None:
            raise RuntimeError("尚未构建 runner——请先 build_runner() 或 run()")
        return self._runner

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

    def snapshot(self) -> dict:
        """进程内全量快照：{spec, tasklist, runner_snapshot} 三件套。

        深拷贝语义：修改返回的 dict 不影响 Module 状态。
        """
        runner = self._require_runner()
        assert self._last_tasklist is not None
        snap = {
            "spec": self.spec.to_dict(),
            "tasklist": tasklist_to_dict(self._last_tasklist),
            "runner": runner.snapshot(),
        }
        # to_dict/runner 快照均为浅拷贝（嵌套结构共享引用）——整体深拷贝
        # 兑现 docstring 的深拷贝承诺（零 tickflow 修改）。
        return copy.deepcopy(snap)

    def restore(self, snap: dict) -> None:
        """回滚 runner 到快照，并恢复 spec/tasklist 字段。"""
        runner = self._require_runner()
        self.spec = Spec(snap["spec"])
        self.tasklist = tasklist_from_dict(snap["tasklist"])
        self._last_tasklist = self.tasklist
        # 与 __init__ 的"template/tasklist 二选一"不变量一致：restore 后
        # 走 tasklist 通道，template_name 不再持有
        self.template_name = None
        runner.restore(snap["runner"])

    def checkpoint(self, label: str) -> None:
        """手动检查点（backend 表，永久保留）。透传 runner。"""
        runner = self._require_runner()
        if not self.persist:
            raise RuntimeError("检查点需要 persist=True（fast mode 零持久化）")
        runner.checkpoint(label)

    def rollback_to(self, label: str) -> None:
        """进程内回退到命名检查点。透传 runner。"""
        runner = self._require_runner()
        if not self.persist:
            raise RuntimeError("检查点需要 persist=True（fast mode 零持久化）")
        runner.rollback_to(label)

    def list_checkpoints(self) -> list[tuple[int, str | list[str], str]]:
        """全部检查点 (tick, fired 或 label, kind)，按 tick 升序。

        kind ∈ {"tick", "manual"}：tick = snapshots 表每 tick 最小快照
        （fired 节点列表，历史审阅雏形）；manual = checkpoint() 手动检查点
        （label）。不依赖 runner——跨进程场景（新 Module 实例）也可查询。
        """
        out: list[tuple[int, str | list[str], str]] = []
        if self.persist:
            backend = SqliteBackend(_persist_dir(self.module_id, self._base_dir))
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

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。

        persist=True 时：每 tick 由 tickflow ``_persist_tick`` 落盘最小快照，
        并归档本次 spec/tasklist 到 module_inputs 表（``_archive_module_inputs``）。
        """
        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        return await self._run_with_phases(runner, max_ticks)

    async def _run_with_phases(self, runner: AsyncRunner, max_ticks: int) -> list:
        """归档本次输入 → 运行 → 按结果映射终态 phase（run/resume 共用）。"""
        if self.control:
            # 新执行清场：作废陈旧控制请求（崩溃残留的 pause 不拖住新执行）。
            # 位于写 running phase 之前——监控方看到 running 才放开控制按钮，
            # 此时清场已完成，清场与首请求的竞态窗口关闭。
            clear_control(self.module_id, base_dir=self._base_dir)
        self._archive_module_inputs()
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

    def _archive_module_inputs(self) -> None:
        """归档本次运行的 spec/tasklist 到 module_inputs 表（警告 1 对比源）。

        run()/resume() 共用（_run_with_phases 开头调用）；resume 中位于
        兼容性校验与 restore 之后——先读旧存档再覆盖，顺序正确。
        """
        if not self.persist:
            return
        if self._input_store is None:
            self._input_store = ModuleInputStore(self.module_id, self._base_dir)
        assert self._last_tasklist is not None
        self._input_store.save_module_inputs(
            self.spec.to_dict(), self._last_tasklist.to_dict()
        )

    def _finalize_phase(self, runner: AsyncRunner) -> None:
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

    async def resume(self, rollback_to: int | str | None = None, max_ticks: int = 100):
        """跨进程续跑：从 tick 号/手动检查点恢复 + 用当前 spec/tasklist 重建未执行部分。

        rollback_to=None → 最新 tick 快照（"从中断处续跑"缺省）。流程：回退目标
        解析（tick 号 → snapshots 表；manual:xxx → checkpoints 表；None →
        max(ticks)）→ 新图全量重建 → 兼容性校验（硬错误拒绝）→ restore →
        归档新输入 → 续跑。要求 persist=True；max_ticks 是绝对 tick 上限。
        """
        if not self.persist:
            raise RuntimeError(
                "resume 需要 persist=True（快照依赖 SQLite backend）"
            )

        # 1. 回退目标解析 + 已执行节点（同一连接，避免多次打开 run.sqlite）；
        #    解析失败先写 aborted+error 再抛——__init__ 已写 idle，不补写终态
        #    会把先前 run 的状态覆盖成裸 idle（错误信息丢失）
        backend = SqliteBackend(_persist_dir(self.module_id, self._base_dir))
        try:
            if rollback_to is None:
                ticks = backend.list_snapshots(self.module_id)
                if not ticks:
                    raise KeyError(
                        f"无可恢复快照: {self.module_id}（运行未产生任何 tick 快照）"
                    )
                rollback_to = max(ticks)
            snap = self._resolve_target(backend, self.module_id, rollback_to)
            if snap is None:
                ticks = backend.list_snapshots(self.module_id)
                manual = [label for label, _ in backend.list_checkpoints(self.module_id)]
                raise KeyError(
                    f"回退目标 {rollback_to!r} 不存在"
                    f"（可用 tick: {ticks or '无'}；manual: {manual or '无'}）"
                )
            # 已执行节点：firings 表中 tick < 快照 tick 的去重节点（S3 后
            # 快照不再含 edges 窗口）。快照 tick N 在 tick N-1 结束后落盘，
            # tick == N 的 firing 属于 restore 后会被重跑的部分，不算已执行。
            # 注：firings 表按 module_id 累积（跨多次 run），前一轮 run 的
            # 记录也会计入——仅影响提示性警告 1/3 的准确性，不影响硬错误。
            executed_nodes = {
                d["node"] for d in backend.list_firings(self.module_id)
                if d.get("node") and int(d.get("tick", 0)) < int(snap.get("tick", 0))
            }
        except KeyError as e:
            self._write_phase("aborted", error=str(e))
            raise
        finally:
            backend.close()

        # 2. 旧输入存档（警告 1 对比源；覆盖前读取）
        store = ModuleInputStore(self.module_id, self._base_dir)
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
