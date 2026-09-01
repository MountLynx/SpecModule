# module_harness/registry.py
"""HarnessRegistry — tickflow Registry 子类，管理 harness/script 注册。"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable

from tickflow import Registry
from tickflow.views import DictView

from .config import HarnessConfig
from .command import Command, CommandConfig
from .harness import Harness
from .events import (
    EventBus,
    ScriptStarted,
    ScriptCompleted,
    ScriptFailed,
)


class HarnessRegistry(Registry):
    """tickflow Registry 子类。tickflow 零修改。

    Runner 只调 ``get_body()``，不感知 harness/script/body 的区别。
    """

    def __init__(
        self,
        *,
        llm_client: Any,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__()
        self._llm_client = llm_client
        self._event_bus = event_bus or EventBus.null()
        self._harness_cfgs: dict[str, HarnessConfig] = {}
        self._script_names: set[str] = set()
        self._command_cfgs: dict[str, CommandConfig] = {}

    # ── harness 注册 ──────────────────────────────────────────────

    def harness(
        self,
        name: str,
        config: HarnessConfig,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
        spec_inputs: dict[str, Any] | None = None,
        input_aliases: dict[str, str] | None = None,
    ) -> "HarnessRegistry":
        """注册一个 harness body。

        ``name`` 是 graph 中 ``node.body`` 引用的名称。
        ``spec_inputs``：spec 字段常量，渲染时作为占位符兜底值。
        ``input_aliases``：跨节点输入别名 {field_name: producer}，
        prompt 的 {field} 占位符在运行时解析 producer 的输出值。
        返回 self，支持链式调用。
        """
        h = Harness(config, self._llm_client, self._event_bus)
        body = h.build_body(
            promptmode=promptmode,
            prompt_extra=prompt_extra,
            spec_inputs=spec_inputs,
            input_aliases=input_aliases,
        )
        self.body(name, body)
        self._harness_cfgs[name] = config
        return self

    # ── script 注册 ───────────────────────────────────────────────

    def script(self, name: str):
        """装饰器：``@reg.script('name')`` — 包裹事件发射后注册为 body。

        body 执行时自动发射 ScriptStarted / ScriptCompleted / ScriptFailed。
        支持 sync 和 async 用户函数。
        """
        bus = self._event_bus

        def deco(fn: Callable) -> Callable:
            is_async = inspect.iscoroutinefunction(fn)

            if is_async:
                @functools.wraps(fn)
                async def wrapped(view: DictView) -> Any:
                    node = view.node
                    bus.emit(ScriptStarted(
                        timestamp=time.monotonic(), node=node, tick=0,
                    ))
                    try:
                        result = await fn(view)
                    except Exception as e:
                        bus.emit(ScriptFailed(
                            timestamp=time.monotonic(), node=node, tick=0,
                            error=str(e),
                        ))
                        raise
                    bus.emit(ScriptCompleted(
                        timestamp=time.monotonic(), node=node, tick=0,
                        output_type=type(result).__name__,
                    ))
                    return result
            else:
                @functools.wraps(fn)
                def wrapped(view: DictView) -> Any:
                    node = view.node
                    bus.emit(ScriptStarted(
                        timestamp=time.monotonic(), node=node, tick=0,
                    ))
                    try:
                        result = fn(view)
                    except Exception as e:
                        bus.emit(ScriptFailed(
                            timestamp=time.monotonic(), node=node, tick=0,
                            error=str(e),
                        ))
                        raise
                    bus.emit(ScriptCompleted(
                        timestamp=time.monotonic(), node=node, tick=0,
                        output_type=type(result).__name__,
                    ))
                    return result

            self.body(name, wrapped)
            self._script_names.add(name)
            return wrapped

        return deco

    # ── command 注册 ───────────────────────────────────────────────

    def command(
        self,
        name: str,
        config: CommandConfig,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> "HarnessRegistry":
        """注册一个 command body。

        ``name`` 是 graph 中 ``node.body`` 引用的名称。
        返回 self，支持链式调用。
        """
        cmd = Command(config, self._event_bus)
        body = cmd.build_body(timeout=timeout, cwd=cwd)
        self.body(name, body)
        self._command_cfgs[name] = config
        return self

    # ── 查询 ──────────────────────────────────────────────────────

    @property
    def llm_client(self) -> Any:
        """注册表持有的 LLM 客户端（只读）。

        供不经图独立调用 harness 的场景使用（如 ConsistencyReviewer
        走 call_harness）。
        """
        return self._llm_client

    def is_harness(self, name: str) -> bool:
        """name 是否通过 harness() 注册。"""
        return name in self._harness_cfgs

    def is_script(self, name: str) -> bool:
        """name 是否通过 script() 注册。"""
        return name in self._script_names

    def is_command(self, name: str) -> bool:
        """name 是否通过 command() 注册。"""
        return name in self._command_cfgs

    def harness_config(self, name: str) -> HarnessConfig | None:
        """返回 harness 的配置，若不是 harness 返回 None。"""
        return self._harness_cfgs.get(name)

    def command_config(self, name: str) -> CommandConfig | None:
        """返回 command 的配置，若不是 command 返回 None。"""
        return self._command_cfgs.get(name)

    def guard_names(self) -> list[str]:
        """已注册 guard 名列表（publish 单文件转化枚举用）。"""
        return list(getattr(self, "_guards", {}).keys())
