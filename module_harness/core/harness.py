"""Harness 类 — 配置持有 + async body 生成。"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
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
    LlmThinking,
    LlmCallCompleted,
    OutputValidated,
    ImageSaved,
    HarnessFailed,
)


# 校验失败反馈段（框架契约文案）：重试时追加到原渲染 prompt 末尾重问
_VALIDATION_FEEDBACK = (
    "\n\n上一次输出未通过校验：{error}\n"
    "请修正该问题后重新输出完整结果，不要复述错误内容，不要解释修改过程。"
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
          2. 调 LLM（流式 token 经 on_token 发射；mode="image" 改走
             generate_image → 落盘 → ImageSaved，无 token 流、无文本校验）
          3. 校验输出格式
          4. 发事件
        """
        config = self.config
        llm = self.llm
        bus = self.bus
        renderer = self._renderer
        validator = OutputValidator(config.output_format) if config.output_format else None

        async def _run_image(view: NodeView, rendered: str,
                             state: dict[str, Any] | None) -> Any:
            """图像模式：渲染好的 prompt → generate_image → 落盘 → 事件 → 返回路径。

            无 token 流（LlmToken 不发）、无文本校验（OutputValidated 不发）；
            LLMError 与落盘 OSError 同归 infrastructure Failure。
            ``state`` 由 body 传入（body 局部变量，闭包不可见）。
            """
            node = view.node
            try:
                from llm.client import LLMError

                result = await llm.generate_image(
                    prompt=rendered,
                    model=config.model,
                    size=config.image_size,
                    api_params=config.api_params if config.api_params else None,
                )
            except LLMError as e:
                if state is not None:
                    state["_llm_error"] = str(e)
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=str(e), failure_type="infrastructure",
                ))
                return Failure(str(e), type="infrastructure")

            # usage 在落盘前写入：已计费的 LLM 调用审计不因落盘失败丢失
            if state is not None:
                state["_usage"] = dict(result.usage)

            out_dir = Path(config.image_dir)
            path = out_dir / f"{node}-{time.monotonic_ns()}.png"
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result.data)
            except OSError as e:
                bus.emit(HarnessFailed(
                    timestamp=time.monotonic(), node=node, tick=0,
                    reason=f"图像落盘失败: {e}", failure_type="infrastructure",
                ))
                return Failure(f"图像落盘失败: {e}", type="infrastructure")

            if state is not None:
                state["_image_path"] = str(path)

            bus.emit(LlmCallCompleted(
                timestamp=time.monotonic(), node=node, tick=0,
                content_chars=len(result.data),
                usage=result.usage,
                finish_reason=None,
            ))
            bus.emit(ImageSaved(
                timestamp=time.monotonic(), node=node, tick=0,
                path=str(path), bytes_len=len(result.data),
            ))
            return str(path)

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

            # 2. 调用 LLM（image 单次；text 带校验重试循环）
            if config.mode == "image":
                bus.emit(LlmCallStarted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    model=config.model or "default",
                    prompt_chars=len(rendered),
                ))
                return await _run_image(view, rendered, state)

            budget = config.validate_retries
            retrying = budget > 0
            attempt_prompt = rendered
            usage_acc: dict[str, int] = {}
            attempts = 0
            retry_errors: list[str] = []

            def on_token(chunk: str) -> None:
                bus.emit(LlmToken(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            def on_thinking(chunk: str) -> None:
                bus.emit(LlmThinking(
                    timestamp=time.monotonic(), node=node, tick=0,
                    chunk=chunk,
                ))

            while True:
                attempts += 1
                bus.emit(LlmCallStarted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    model=config.model or "default",
                    prompt_chars=len(attempt_prompt),
                ))

                try:
                    from llm.client import LLMError

                    # notdo 由 LLM client 内部通过 _build_system() 拼入 system prompt
                    response = await llm.complete(
                        prompt=attempt_prompt,
                        model=config.model,
                        temperature=config.temperature,
                        think=config.think,
                        output_format=dataclasses.asdict(config.output_format) if config.output_format else None,
                        notdo=config.notdo if config.notdo else None,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        api_params=config.api_params if config.api_params else None,
                    )
                except LLMError as e:
                    # 传输层失败不重试（SDK max_retries 已管）、不消耗校验重试预算
                    if state is not None:
                        state["_llm_error"] = str(e)
                    bus.emit(HarnessFailed(
                        timestamp=time.monotonic(), node=node, tick=0,
                        reason=str(e),
                        failure_type="infrastructure",
                    ))
                    return Failure(str(e), type="infrastructure")

                # LLM 原始响应 + usage 写入节点状态（审计链：NodeState.mutable_state）；
                # 重试开启时 usage 累计、_validation_attempts 记实际调用次数
                if state is not None:
                    state["_llm_raw"] = response.content
                    usage_acc = _merge_usage(usage_acc, response.usage)
                    state["_usage"] = dict(usage_acc)
                    if retrying:
                        state["_validation_attempts"] = attempts

                # 3. 校验输出
                bus.emit(LlmCallCompleted(
                    timestamp=time.monotonic(), node=node, tick=0,
                    content_chars=len(response.content),
                    usage=response.usage,
                    finish_reason=response.finish_reason,
                ))

                if validator is None:
                    return response.content

                result = validator.validate(response.content)
                if not isinstance(result, Failure):
                    bus.emit(OutputValidated(
                        timestamp=time.monotonic(), node=node, tick=0,
                        passed=True,
                        extracted=_was_extracted(response.content, result),
                        error=None,
                    ))
                    return result

                bus.emit(OutputValidated(
                    timestamp=time.monotonic(), node=node, tick=0,
                    passed=False,
                    extracted=False,
                    error=result.error,
                ))

                if budget <= 0:
                    return result  # 预算耗尽：最后一次的 Failure(type="llm")

                # 带反馈重问：prompt 追加校验错误反馈段，LLM 参数原样保留
                budget -= 1
                retry_errors.append(result.error)
                if state is not None:
                    state["_validation_retry_errors"] = list(retry_errors)
                attempt_prompt = rendered + _VALIDATION_FEEDBACK.format(error=result.error)
                if state is not None:
                    state["_prompt"] = attempt_prompt
                bus.emit(PromptRendered(
                    timestamp=time.monotonic(), node=node, tick=0,
                    rendered=attempt_prompt,
                ))

        return body


def _was_extracted(raw: str, result: Any) -> bool:
    """简单判断原始内容是否经过了提取处理（内容不直接相等）。"""
    if not isinstance(result, str):
        return True  # JSON 解析必然是提取
    return raw.strip() != result.strip()


def _merge_usage(acc: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    """累计多次尝试的 token 用量：数值键求和，单侧缺键取另一侧。"""
    merged = dict(acc)
    for key, val in new.items():
        prev = merged.get(key)
        if isinstance(prev, int) and isinstance(val, int):
            merged[key] = prev + val
        else:
            merged[key] = val
    return merged
