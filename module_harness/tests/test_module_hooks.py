# module_harness/tests/test_module_hooks.py
"""Module hooks 透传：on_tick_start / on_fire 注册与调用。"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from module_harness.model.module import Module
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import TaskDefinition, Tasklist


def _registry(llm):
    reg = HarnessRegistry(llm_client=llm, event_bus=None)

    @reg.script("greet")
    async def greet(view):
        return "hello"

    return reg


def _tasklist() -> Tasklist:
    return Tasklist(
        tasks={"Greet": TaskDefinition(type="script", script="greet")},
        flow="[Greet]",
    )


def _module(**kw) -> Module:
    llm = MagicMock()
    return Module(
        spec={},
        tasklist=_tasklist(),
        llm_client=llm,
        registry=_registry(llm),
        persist=False,
        status_file=False,
        review_harness=None,
        **kw,
    )


def test_on_fire_receives_node_state():
    seen = []

    async def on_fire(ns):
        seen.append(ns)

    asyncio.run(_module(hooks={"on_fire": on_fire}).run(max_ticks=10))
    assert len(seen) == 1
    assert seen[0].node == "Greet"
    assert seen[0].status == "ok"
    assert seen[0].output == "hello"


def test_on_tick_start_receives_tick():
    ticks = []

    async def on_tick_start(tick, fireable):
        ticks.append((tick, list(fireable)))

    asyncio.run(_module(hooks={"on_tick_start": on_tick_start}).run(max_ticks=10))
    assert ticks, "on_tick_start 未被调用"
    assert ticks[0][0] == 0
    assert "Greet" in ticks[0][1]


def test_unknown_hook_name_ignored():
    async def on_nope(ns):
        raise AssertionError("不应被调用")

    # 未知 hook 名只 log 警告，不抛错、不运行
    asyncio.run(_module(hooks={"on_nope": on_nope}).run(max_ticks=10))