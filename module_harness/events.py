# module_harness/events.py
"""EventBus 与 harness/script 事件类型定义。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


# ── Harness 事件基类 ──────────────────────────────────────────────

@dataclass
class HarnessEvent:
    timestamp: float
    node: str
    tick: int


@dataclass
class PromptRendered(HarnessEvent):
    rendered: str


@dataclass
class LlmCallStarted(HarnessEvent):
    model: str
    prompt_chars: int


@dataclass
class LlmToken(HarnessEvent):
    chunk: str


@dataclass
class LlmCallCompleted(HarnessEvent):
    content_chars: int
    usage: dict[str, int]
    finish_reason: str | None


@dataclass
class OutputValidated(HarnessEvent):
    passed: bool
    extracted: bool
    error: str | None


@dataclass
class HarnessFailed(HarnessEvent):
    reason: str
    failure_type: str  # "llm" | "infrastructure"


# ── Script 事件基类 ──────────────────────────────────────────────

@dataclass
class ScriptEvent:
    timestamp: float
    node: str
    tick: int


@dataclass
class ScriptStarted(ScriptEvent):
    pass


@dataclass
class ScriptCompleted(ScriptEvent):
    output_type: str


@dataclass
class ScriptFailed(ScriptEvent):
    error: str


# ── EventBus ──────────────────────────────────────────────────────

class EventBus:
    """同步发布/订阅。

    回调异常 → 记录日志并吞掉（与 tickflow hooks 行为一致）。
    使用 ``EventBus.null()`` 获取静默实例（嵌入式场景）。
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable]] = {}

    def subscribe(self, event_type: type, callback: Callable) -> None:
        """为某个事件类型注册回调。"""
        self._subscribers.setdefault(event_type, []).append(callback)

    def emit(self, event: HarnessEvent | ScriptEvent) -> None:
        """发布事件到所有匹配类型的订阅者。"""
        for event_type, callbacks in self._subscribers.items():
            if isinstance(event, event_type):
                for cb in callbacks:
                    try:
                        cb(event)
                    except Exception:
                        log.exception("EventBus callback raised; swallowed")

    def on(self, event_type: type):
        """装饰器方式订阅: ``@bus.on(LlmToken) def handle(e): ...``"""
        def deco(fn: Callable) -> Callable:
            self.subscribe(event_type, fn)
            return fn
        return deco

    @staticmethod
    def null() -> "EventBus":
        """返回一个静默 EventBus，emit 无操作。"""
        bus = EventBus()
        # 直接替换 emit 方法为 no-op，保留 subscribe 语义但无实际操作
        bus.emit = lambda event: None  # type: ignore[method-assign]
        return bus
