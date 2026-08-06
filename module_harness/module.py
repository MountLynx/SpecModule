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
    AutoCheckpointStore,
    ResumeError,
    check_resume_compat,
    tasklist_from_dict,
    tasklist_to_dict,
)

log = logging.getLogger(__name__)


def _persist_dir(module_id: str) -> Path:
    """``<工作目录>/.specmodule/runs/<run_id>/run.sqlite``（D9）。

    run_id = module_id：一个任务一次运行一个子目录、一个独立 SQLite 数据库。
    """
    return Path.cwd() / ".specmodule" / "runs" / module_id / "run.sqlite"


def _status_path(module_id: str) -> Path:
    """``<工作目录>/.specmodule/runs/<module_id>/status.json``（roadmap #7）。

    阶段级运行状态文件：与 run.sqlite 同目录，跨进程查询的轻量通道。
    """
    return Path.cwd() / ".specmodule" / "runs" / module_id / "status.json"


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
        registry: HarnessRegistry | None = None,
        review_harness: str | None = "spec_tasklist_review",
        keep_records: bool = True,
        persist: bool = True,
        status_file: bool = True,
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
        self.review_result: ConsistencyReport | None = None
        self.module_id = module_id or f"mod_{uuid.uuid4().hex[:8]}"

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
        self._checkpoint_store: AutoCheckpointStore | None = None
        self._auto_cp_hooked = False

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
        path = _status_path(self.module_id)
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
            errors = TasklistValidator.validate(tasklist, self._reg)
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
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist, spec=self.spec)
        self._last_tasklist = tasklist
        backend = (
            SqliteBackend(_persist_dir(self.module_id))
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
        # 新 runner 需要重新注册自动检查点 hook（二次 run()/build 场景）
        self._auto_cp_hooked = False
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
        """释放 Module 持有的 SQLite 连接（``_checkpoint_store``，懒创建）。

        run()/resume() 结束后可调用；幂等（重复调用安全）。再次 run()/resume()
        会按需重新创建 store（``_register_auto_checkpoint`` 懒重建）。

        注：runner 持有的 SqliteBackend 连接属于 runner 生命周期，由调用方
        管理（与既有行为一致），本方法只关闭 Module 自己创建的
        AutoCheckpointStore 连接，不触碰 runner。
        """
        if self._checkpoint_store is not None:
            self._checkpoint_store.close()
            self._checkpoint_store = None

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
            backend = SqliteBackend(_persist_dir(self.module_id))
            try:
                out.extend(
                    (label, tick, "manual")
                    for label, tick in backend.list_checkpoints(self.module_id)
                )
            except Exception:
                log.exception("手动检查点列表读取失败（忽略）")
            finally:
                # 显式释放连接（WAL PRAGMA + 建表 + 迁移检查的独立连接），
                # 与 AutoCheckpointStore 的显式 close 一致，不依赖 GC。
                backend.close()
        return sorted(out, key=lambda item: item[1])

    def _load_checkpoint(self, label: str) -> dict | None:
        """查找检查点：先 auto 表（AutoCheckpointStore），后手动表（backend）。

        两个表都没有 → None（调用方决定如何报错）。两表连接均显式关闭
        （手动表查找曾泄漏 SqliteBackend 连接）。
        """
        store = AutoCheckpointStore(self.module_id)
        try:
            snap = store.load(label)
            if snap is not None:
                return snap
            backend = SqliteBackend(_persist_dir(self.module_id))
            try:
                return backend.load_checkpoint(self.module_id, label)
            finally:
                backend.close()
        finally:
            store.close()

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。

        persist=True 时：注册自动检查点 hook（每 tick 存一个，环形保留 20），
        并归档本次 spec/tasklist 到 module_inputs 表。
        """
        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
        return await self._run_with_phases(runner, max_ticks)

    async def _run_with_phases(self, runner: AsyncRunner, max_ticks: int) -> list:
        """注册自动检查点 → 运行 → 按结果映射终态 phase（run/resume 共用）。"""
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
        if not self._auto_cp_hooked:
            runner = self._runner

            def _hook(tick: int, firings: list) -> None:
                store.save(f"auto:tick:{tick}", runner.snapshot())

            runner.on_tick_end(_hook)
            self._auto_cp_hooked = True

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

    async def resume(self, rollback_to: str, max_ticks: int = 100):
        """跨进程续跑：从检查点恢复 + 用当前 spec/tasklist 重建未执行部分。

        流程：检查点查找（auto 表 → 手动表）→ 新图全量重建 → 兼容性校验
        （硬错误拒绝，不触碰 runner）→ restore + remap_graph 移植 marking →
        注册自动检查点 hook → 续跑。

        要求 persist=True（自动检查点依赖 SQLite backend）。

        max_ticks 是绝对 tick 上限：从 restore 的 tick 起继续计数（如
        restore 于 tick 95，则默认 100 只剩 5 个 tick 可跑）。
        """
        if not self.persist:
            raise RuntimeError(
                "resume 需要 persist=True（自动检查点依赖 SQLite backend）"
            )

        # 1. 检查点查找：auto 表 → 手动表（_load_checkpoint 内连接显式关闭）
        snap = self._load_checkpoint(rollback_to)
        if snap is None:
            available = ", ".join(
                label for label, _, _ in self.list_checkpoints()
            ) or "（无）"
            raise KeyError(
                f"检查点 {rollback_to!r} 不存在（可用: {available}）"
            )
        store = AutoCheckpointStore(self.module_id)
        try:
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
        marking = snap.get("marking") or {}
        marking_slots = marking.get("slots")
        # snapshot 的 marking.armed_starts 是排序 list（engine.py:72）
        armed_starts = marking.get("armed_starts")
        old_tl = tasklist_from_dict(old_inputs["tasklist"]) if old_inputs else None
        check = check_resume_compat(
            self._last_tasklist, runner.graph, executed_nodes,
            old_tasklist=old_tl,
            marking_slots=marking_slots,
            armed_starts=armed_starts,
        )
        for w in check.warnings:
            log.warning("resume 兼容性警告: %s", w)
        if check.hard_errors:
            self._write_phase("aborted", error="resume 兼容性校验失败")
            raise ResumeError(check.hard_errors)

        # 4. restore + remap：移植检查点 marking 到新图
        runner.restore(snap)
        runner.remap_graph(runner.graph)

        # 5. 注册自动检查点 + 续跑（phase 写盘与 run() 共用）
        return await self._run_with_phases(runner, max_ticks)
