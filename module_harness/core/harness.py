"""Harness 类 — 配置持有 + async body 生成。"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

from tickflow import Failure
from tickflow.views import NodeView

from .config import HarnessConfig
from .prompt import PromptRenderer
from .outputfmt import OutputValidator
from ..infra.events import (
    EventBus,
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
    HarnessFailed,
)


class Harness:
    """持有 HarnessConfig + LLM 客户端 + EventBus。

    由 HarnessRegistry 管理，用户不直接使用。
    """

    def __init__(
        self,
        config: HarnessConfig,
        llm_client: Any,
        event_bus: EventBus,
    ) -> None:
        self.config = config
        self.llm = llm_client
        self.bus = event_bus
        self._renderer = PromptRenderer(config)

    def build_body(
        self,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
        spec_inputs: dict[str, Any] | None = None,
    ):
        """返回一个 async body callable。

        ``spec_inputs``：spec 字段常量（{field_name: value}），
        渲染时作为占位符兜底值（graph_builder 解析 ``{spec.xxx}`` 后注入）。

        跨节点输入不再经别名传递：graph_builder 把非常量 task.inputs 写成
        具名 bind，body 按字段名经视图的 ``v.named`` 直达（未点火字段值为
        Missing，渲染时走 spec_inputs 兜底）。

        body 执行流程：
          1. 渲染三层 prompt
          2. 调 LLM（流式 token 经 on_token 发射）
          3. 校验输出格式
          4. 发事件
        """
        config = self.config
        llm = self.llm
        bus = self.bus
        renderer = self._renderer
        validator = OutputValidator(config.output_format) if config.output_format else None

        async def body(view: NodeView) -> Any:
            node = view.node
            now = time.monotonic()
            # view.state 可为 None（引擎外合成视图不挂状态）；写入后进入 NodeState.mutable_state 审计
            state = view.state

            # 1. 渲染 prompt
            extra = dict(spec_inputs) if spec_inputs else {}
            rendered = renderer.render(
                view,
                promptmode=promptmode,
                prompt_extra=prompt_extra,
                extra_values=extra,
            )
            if state is not None:
                state["_prompt"] = rendered
            bus.emit(PromptRendered(
                timestamp=time.monotonic(), node=node, tick=0,
                rendered=rendered,
            ))

            # 2. 调用 LLM
            bus.emit(LlmCallStarted(
                timestamp=time.monotonic(), node=node, tick=0,
                model=config.model or "default",
                prompt_chars=len(rendered),
            ))

            def on_token(chunk: str) -> None:
                bus.emit(LlmToken(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            try:
                from llm.client import LLMError

                # notdo 由 LLM client 内部通过 _build_system() 拼入 system prompt
                response = await llm.complete(
                    prompt=rendered,
                    model=config.model,
                    temperature=config.temperature,
                    think=config.think,
                    output_format=dataclasses.asdict(config.output_format) if config.output_format else None,
                    notdo=config.notdo if config.notdo else None,
                    on_token=on_token,
                    api_params=config.api_params if config.api_params else None,
                )
            except LLMError as e:
                if state is not None:
                    state["_llm_error"] = str(e)
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=str(e),
                    failure_type="infrastructure",
                ))
                return Failure(str(e), type="infrastructure")

            # 3. LLM 原始响应 + usage 写入节点状态（审计链：NodeState.mutable_state）
            if state is not None:
                state["_llm_raw"] = response.content
                state["_usage"] = dict(response.usage)

            # 3. 校验输出
            bus.emit(LlmCallCompleted(
                timestamp=time.monotonic(), node=node, tick=0,
                content_chars=len(response.content),
                usage=response.usage,
                finish_reason=response.finish_reason,
            ))

            if validator is not None:
                result = validator.validate(response.content)
                if isinstance(result, Failure):
                    bus.emit(OutputValidated(
                        timestamp=time.monotonic(), node=node, tick=0,
                        passed=False,
                        extracted=False,
                        error=result.error,
                    ))
                    return result
                bus.emit(OutputValidated(
                    timestamp=time.monotonic(), node=node, tick=0,
                    passed=True,
                    extracted=_was_extracted(response.content, result),
                    error=None,
                ))
                return result

            return response.content

        return body


def _was_extracted(raw: str, result: Any) -> bool:
    """简单判断原始内容是否经过了提取处理（内容不直接相等）。"""
    if not isinstance(result, str):
        return True  # JSON 解析必然是提取
    return raw.strip() != result.strip()
