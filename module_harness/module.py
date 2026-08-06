"""Module 编排器 — spec + template/tasklist → tasklist → runner。"""

from __future__ import annotations

import asyncio
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
        backend = (
            SqliteBackend(_persist_dir(self.module_id))
            if self.persist
            else NullBackend()
        )
        self._write_phase("ready")
        return AsyncRunner(
            graph,
            registry=reg,
            keep_records=self.keep_records,
            backend=backend,
            session_id=self.module_id,
        )

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        from tickflow.runner import RunStatus

        try:
            runner = await self._build_runner_async()
        except Exception as e:
            self._write_phase("aborted", error=str(e))
            raise
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
            # 正常返回：按 runner.status 映射终态
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
        return firings
