"""Module 编排器 — spec + template → tasklist → runner。"""

from __future__ import annotations

import uuid
from typing import Any

from tickflow.async_runner import AsyncRunner

from .spec import Spec
from .translator import Translator, TemplateLoader
from .graph_builder import TasklistTranslator
from .registry import HarnessRegistry
from .events import EventBus


class Module:
    """SpecModule 的核心编排器。

    spec + template → 翻译 → tasklist → tickflow Graph + registry → AsyncRunner。
    """

    def __init__(
        self,
        spec: dict[str, Any],
        template_name: str,
        llm_client: Any,
        *,
        event_bus: EventBus | None = None,
        template_loader: TemplateLoader | None = None,
        module_id: str | None = None,
        registry: HarnessRegistry | None = None,
    ) -> None:
        self.spec = Spec(spec)
        self.template_name = template_name
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
        template = self._loader.get(self.template_name)
        if template is None:
            raise ValueError(f"模板 '{self.template_name}' 未找到")

        tasklist = await self._translator.translate(self.spec, template)
        builder = TasklistTranslator(self._reg, self.module_id)
        graph, reg = builder.build(tasklist)
        return AsyncRunner(graph, registry=reg, keep_records=True)

    async def run(self, max_ticks: int = 100):
        """执行翻译 → 构建 → 运行。一步跑完。"""
        runner = await self._build_runner_async()
        return await runner.run_until_idle(max_ticks=max_ticks)
