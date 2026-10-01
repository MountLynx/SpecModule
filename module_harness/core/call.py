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
from tickflow.views import NodeView, Resolved

from .config import HarnessConfig
from ..infra.events import EventBus
from .harness import Harness


@dataclass
class HarnessCallResult:
    """独立调用结果：校验后输出 + LLM 原始输出 + token 用量。

    图像模式不产生文本 raw（无 _llm_raw），raw 为 None。
    validate_retries > 0 时：usage 为各次尝试之和；raw 为最后一次尝试的原始输出。
    """

    value: Any  # 校验后的输出（json_object → 解析值；text → str）
    raw: str | None    # LLM 原始输出（审计链）
    usage: dict[str, int]  # token 用量


class HarnessCallError(RuntimeError):
    """独立调用失败（LLM 错误 / 输出不合法）。异常即审计：携带诊断链。"""

    def __init__(
        self,
        failure: Failure,
        *,
        prompt: str | None = None,
        raw: str | None = None,
        usage: dict[str, int] | None = None,
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
    view: NodeView | None = None,
) -> HarnessCallResult:
    """独立调用一个 harness：一次函数调用拿到校验后的输出。

    ``values``：prompt 占位符取值 {key: value}。task 层的占位符兜底就是它
    （无 spec_inputs —— 那是图概念）。

    ``event_bus``：传则收全套 harness 事件（PromptRendered / LlmToken /
    OutputValidated / ...），不传零开销（EventBus.null()）。

    ``view``：节点内形态——传 script 节点自己的视图（图执行 body 拿到的
    view）。事件 ``node`` 归属 ``view.node``；body 写入 state 的
    ``_prompt``/``_llm_raw``/``_usage`` 等键在调用完成后合并回
    ``view.state``（进 NodeState.mutable_state 审计链）。state 本调用
    隔离：诊断读回（含 HarnessCallError）严格是本调用的值。不传（缺省）：
    独立调用形态，合成 ``__call__`` 视图 + 一次性局部 state，行为同
    历史版本。

    失败（LLM 错误 / 输出校验不通过）抛 HarnessCallError，携带 failure 与
    渲染 prompt / 原始输出 / usage 诊断链。``validate_retries > 0`` 时校验
    失败在 body 内带反馈重问：``prompt`` 为最后一次尝试的实际 prompt（含
    反馈段）、``raw`` 为最后一次原始输出、``usage`` 为各次尝试之和。LLM
    错误路径：``prompt`` 仍为出错尝试的实际 prompt，``raw``/``usage`` 为
    最后一次有输出的尝试（出错尝试无输出，错误本身见 failure.error）。task
    层没有"下游跳过"概念，Failure 一律翻译为异常；promptmode 缺 key →
    KeyError 原样冒出。
    """
    bus = event_bus or EventBus.null()
    body = Harness(config, llm_client, bus).build_body(
        promptmode=promptmode,
        prompt_extra=prompt_extra,
    )
    # 节点内形态（view 传入）：事件与审计状态归属真实节点；state 本调用
    # 隔离（body 只写不读，隔离保证诊断读回严格是本调用的值，多调用节点
    # 不串上一调用残留），完成后合并回 view.state（NodeState.mutable_state
    # 审计链）。独立调用形态：合成 __call__ 视图 + 一次性局部 state。
    node_name = view.node if view is not None else "__call__"
    sink: dict[str, Any] = {}
    # bind 时代：values 即具名 bind 字段（field → 值），
    # 视图按引擎对具名 bind body 的供数形态构造（v.named 直达占位符）
    body_view = NodeView(
        node=node_name,
        fields=tuple((key, key) for key in values),
        values=tuple(values.values()),
        state=sink,
        resolved={key: Resolved(value=val, k=None) for key, val in values.items()},
    )
    result = await body(body_view)

    if view is not None and view.state is not None:
        for key, val in sink.items():
            view.state[key] = val

    prompt = sink.get("_prompt")
    raw = sink.get("_llm_raw")
    usage = sink.get("_usage")

    if isinstance(result, Failure):
        raise HarnessCallError(result, prompt=prompt, raw=raw, usage=usage)
    return HarnessCallResult(value=result, raw=raw, usage=usage)
