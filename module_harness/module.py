"""Module 编排器 — spec + template/tasklist → tasklist → runner。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tickflow.async_runner import AsyncRunner

from .spec import Spec, Tasklist
from .consistency import ConsistencyError, ConsistencyReport, ConsistencyReviewer
from .translator import Translator, TemplateLoader, TasklistValidator
from .graph_builder import TasklistTranslator
from .registry import HarnessRegistry
from .events import EventBus, ConsistencyReviewed


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
    ) -> None:
        if (template_name is None) == (tasklist is None):
            raise ValueError("template_name 与 tasklist 必须且只能传一个")
        self.spec = Spec(spec)
        self.template_name = template_name
        self.tasklist = tasklist
        self.review_harness = review_harness
        self.keep_records = keep_records
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

    def build_runner(self) -> AsyncRunner:
        """执行翻译 → 构建 graph → 返回 AsyncRunner。

        Note: 这是一个同步方法。在 async 上下文中直接使用
        await module._build_runner_async() 或 await module.run()。
        """
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._build_runner_async())
        finally:
            loop.close()

    async def _build_runner_async(self) -> AsyncRunner:
        """异步版 build_runner。"""
        if self.tasklist is not None:
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
            template = self._loader.get(self.template_name)
            if template is None:
                raise ValueError(f"模板 '{self.template_name}' 未找到")
            tasklist = await self._translator.translate(self.spec, template)
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist, spec=self.spec)
        return AsyncRunner(graph, registry=reg, keep_records=self.keep_records)

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        runner = await self._build_runner_async()
        return await runner.run_until_idle(max_ticks=max_ticks)
