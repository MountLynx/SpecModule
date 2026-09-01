# module_harness/call.py
"""task 级 API 地板 —— 独立调用 harness（嵌入者消费面）。

API 金字塔自此 task → graph → run：嵌入者一次函数调用即得 harness 节点的
全部执行语义（三层 prompt / 输出校验 / 事件），不经图与 run。
零新执行语义：内部即 Harness.build_body + 一次 body 调用，仅一份执行配方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tickflow import Failure
from tickflow.views import DictView, Resolved

from .config import HarnessConfig
from .events import EventBus
from .harness import Harness


@dataclass
class HarnessCallResult:
    """独立调用结果：校验后输出 + LLM 原始输出 + token 用量。"""

    value: Any  # 校验后的输出（json_object → 解析值；text → str）
    raw: str    # LLM 原始输出（审计链）
    usage: dict  # token 用量


class HarnessCallError(RuntimeError):
    """独立调用失败（LLM 错误 / 输出不合法）。异常即审计：携带诊断链。"""

    def __init__(
        self,
        failure: Failure,
        *,
        prompt: str | None = None,
        raw: str | None = None,
        usage: dict | None = None,
    ) -> None:
        self.failure = failure
        self.prompt = prompt
        self.raw = raw
        self.usage = usage
        super().__init__(failure.error)


async def call_harness(
    config: HarnessConfig,
    values: dict[str, Any],
    *,
    llm_client: Any,
    promptmode: str | None = None,
    prompt_extra: str | None = None,
    event_bus: EventBus | None = None,
) -> HarnessCallResult:
    """独立调用一个 harness：一次函数调用拿到校验后的输出。

    ``values``：prompt 占位符取值 {key: value}。task 层的占位符兜底就是它
    （无 spec_inputs / input_aliases —— 那些是图概念）。

    ``event_bus``：传则收全套 harness 事件（PromptRendered / LlmToken /
    OutputValidated / ...），不传零开销（EventBus.null()）。

    失败（LLM 错误 / 输出校验不通过）抛 HarnessCallError，携带 failure 与
    渲染 prompt / 原始输出 / usage 诊断链。task 层没有"下游跳过"概念，
    Failure 一律翻译为异常；promptmode 缺 key → KeyError 原样冒出。
    """
    bus = event_bus or EventBus.null()
    body = Harness(config, llm_client, bus).build_body(
        promptmode=promptmode,
        prompt_extra=prompt_extra,
    )
    state: dict[str, Any] = {}
    view = DictView(
        {key: Resolved(value=val, k=None) for key, val in values.items()},
        state=state,
        node="__call__",
    )
    result = await body(view)

    prompt = state.get("_prompt")
    raw = state.get("_llm_raw")
    usage = state.get("_usage")

    if isinstance(result, Failure):
        raise HarnessCallError(result, prompt=prompt, raw=raw, usage=usage)
    return HarnessCallResult(value=result, raw=raw, usage=usage)
