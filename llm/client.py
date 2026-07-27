"""LLM 客户端 —— 统一的 LLM 调用接口

支持 Anthropic 和 OpenAI(兼容) 两种后端。

面向 ModuleHarness harness body 的入口是 :meth:`complete`：接收渲染后的 prompt 与按调用
覆盖的参数，流式 token 经 ``on_token`` 回调回传，结构化输出经 ``response_format`` / 强制
tool-use 原生适配，扩展思考(think)经各 provider 原生参数启用。

错误契约
--------
客户端只区分「调用成功 / 调用失败」。调用失败（鉴权失败、模型不存在、超时、网络错误、客户端
未就绪等基础设施故障）抛 :class:`LLMError`，由 harness body 捕获后映射为
``Failure(type="infrastructure")``，使 Runner 进入 ``ABORTED`` 停机——这类故障不可由重试同
一次调用解决，停机交由 agent 决策（回滚/换模型/终止）。

输出格式不合格**不在此处判断**——那是 body 的 outputformat 审查层职责（→
``Failure(type="llm")``，运行续跑）。故客户端成功返回的 ``LLMResponse.content`` 始终是模型
原始输出，校验留给上层。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import LLMConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """LLM 调用的基础设施故障。

    涵盖鉴权失败(403)、模型不存在(404)、超时、网络错误、SDK 未安装/初始化失败等。
    harness body 捕获后映射为 ``Failure(type="infrastructure")`` → Runner ``ABORTED``。
    """


# ---------------------------------------------------------------------------
# 公开类型
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """统一消息格式（chat 多轮接口用；complete 单轮接口不直接使用）。"""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    """LLM 响应。complete 成功时返回；调用失败抛 LLMError 而非返回此对象。"""
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# 共享辅助
# ---------------------------------------------------------------------------


# 已知的 OpenAI SDK 参数（直接入 kwargs，不归入 extra_body）
_KNOWN_OPENAI_PARAMS = frozenset({
    "model", "messages", "temperature", "top_p", "n", "stream",
    "stop", "max_tokens", "max_completion_tokens",
    "presence_penalty", "frequency_penalty", "logit_bias", "user",
    "response_format", "seed", "tools", "tool_choice",
    "reasoning_effort", "logprobs", "top_logprobs",
    "functions", "function_call",
    "stream_options", "extra_headers",
})

# 已知的 Anthropic SDK 参数
_KNOWN_ANTHROPIC_PARAMS = frozenset({
    "model", "messages", "system", "max_tokens", "temperature",
    "thinking", "tools", "tool_choice", "stop_sequences",
    "top_p", "top_k", "metadata",
})


def _apply_api_params(kwargs: dict[str, Any], api_params: dict[str, Any] | None,
                      known: frozenset[str]) -> None:
    """将 api_params 合并到 kwargs：已知字段直接入参，未知入 extra_body。"""
    if not api_params:
        return
    for k, v in api_params.items():
        if k in known:
            kwargs[k] = v
        else:
            kwargs.setdefault("extra_body", {})[k] = v


def _build_system(system: str | None, notdo: list[str] | None) -> str | None:
    """拼装 system prompt：基础 system + 否定性约束(notdo)。

    notdo 是「进一步约束，注入提示词」（见 spec harness 三层 prompt），这里作为 system 的一部分
    原生注入，使模型在生成时就受其约束。
    """
    parts: list[str] = []
    if system:
        parts.append(system)
    if notdo:
        parts.append("不要做以下事项：\n" + "\n".join(f"- {n}" for n in notdo))
    return "\n\n".join(parts) if parts else None


def _safe_on_token(on_token: Callable[[str], None] | None, chunk: str) -> None:
    """调用 on_token，回调异常不得影响主流程（观测者不应破坏调用）。"""
    if on_token is None or not chunk:
        return
    try:
        on_token(chunk)
    except Exception:
        log.exception("on_token 回调异常；已忽略")


# ---------------------------------------------------------------------------
# Anthropic 客户端
# ---------------------------------------------------------------------------


class AnthropicClient:
    """Anthropic Claude API 客户端。

    结构化输出经强制 tool-use 原生实现（Anthropic 无 JSON mode）；扩展思考经 ``thinking``
    参数原生启用。
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=config.api_key)
            self._ready = True
        except ImportError:
            log.error("anthropic 包未安装，请执行: pip install anthropic")
            self._ready = False
            self._client = None
        except Exception as exc:
            log.error("Anthropic 客户端初始化失败: %s", exc)
            self._ready = False
            self._client = None

    @property
    def ready(self) -> bool:
        return self._ready

    def _require_ready(self) -> None:
        if not self._ready:
            raise LLMError("Anthropic 客户端未就绪（anthropic 包未安装或初始化失败）")

    def _thinking_param(self, think: bool | dict | None) -> dict | None:
        """把 think 配置转为 Anthropic ``thinking`` 参数。

        Anthropic 扩展思考要求 ``budget_tokens < max_tokens``；启用思考时 temperature 须为 1
        （由调用处省略 temperature 实现）。
        """
        if not think:
            return None
        if isinstance(think, dict):
            budget = int(think.get("budget_tokens", 4096))
        else:
            # bool True：给保守默认，预留输出空间
            budget = min(self.config.max_tokens - 1024, 8192)
        budget = max(1024, budget)
        if budget >= self.config.max_tokens:
            raise LLMError(
                f"think budget_tokens({budget}) 须小于 max_tokens({self.config.max_tokens})"
            )
        return {"type": "enabled", "budget_tokens": budget}

    def _structured_tool(self, output_format: dict[str, Any]) -> tuple[list[dict], dict]:
        """把 output_format 转为 Anthropic 强制 tool-use 参数。

        output_format 形如 ``{"name": str, "description": str, "schema": <JSON schema>}``。
        强制模型调用该 tool，其 input 即结构化输出（content 取其 JSON）。
        """
        name = output_format.get("name", "structured_output")
        schema = (
            output_format.get("schema")
            or output_format.get("input_schema")
            or output_format
        )
        tool = {
            "name": name,
            "description": output_format.get("description", "Return structured output."),
            "input_schema": schema,
        }
        return [tool], {"type": "tool", "name": name}

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        think: bool | dict | None = None,
        output_format: dict[str, Any] | None = None,
        notdo: list[str] | None = None,
        on_token: Callable[[str], None] | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """单轮调用入口（harness body 用）。

        - ``prompt``：三层渲染后的用户提示词
        - ``model``/``temperature``/``think``：按调用覆盖，缺省回落 config
        - ``output_format``：原生结构化输出（强制 tool-use）
        - ``on_token``：流式 token 回调（提供时走流式接口）
        - ``api_params``：透传给 SDK 的额外参数（已知字段入 kwargs，未知入 extra_body）
        """
        self._require_ready()
        model = model or self.config.model
        temperature = self.config.temperature if temperature is None else temperature
        thinking = self._thinking_param(think if think is not None else self.config.model_info(model).get("think"))
        sys_prompt = _build_system(system, notdo)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if sys_prompt:
            kwargs["system"] = sys_prompt
        # 启用思考时 temperature 必须为 1（省略即默认 1.0）
        if temperature is not None and not thinking:
            kwargs["temperature"] = temperature
        if thinking:
            kwargs["thinking"] = thinking

        forced_tool: str | None = None
        if output_format:
            tools, tool_choice = self._structured_tool(output_format)
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
            forced_tool = tools[0]["name"]

        _apply_api_params(kwargs, api_params, _KNOWN_ANTHROPIC_PARAMS)

        try:
            if on_token:
                content, tool_calls, usage, finish = await self._stream(kwargs, forced_tool, on_token)
            else:
                content, tool_calls, usage, finish = await self._nonstream(kwargs, forced_tool)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Anthropic API 调用失败: {exc}") from exc
        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage, finish_reason=finish)

    async def _nonstream(self, kwargs: dict, forced_tool: str | None) -> tuple:
        response = await self._client.messages.create(**kwargs)
        content = ""
        tool_calls: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
                if forced_tool and block.name == forced_tool:
                    content = json.dumps(block.input, ensure_ascii=False)
        usage = {
            "input_tokens": response.usage.input_tokens or 0,
            "output_tokens": response.usage.output_tokens or 0,
        }
        return content, tool_calls, usage, response.stop_reason

    async def _stream(self, kwargs: dict, forced_tool: str | None, on_token) -> tuple:
        content = ""
        tool_calls: list[dict[str, Any]] = []
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                content += text
                _safe_on_token(on_token, text)
            final = await stream.get_final_message()
        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})
                if forced_tool and block.name == forced_tool:
                    content = json.dumps(block.input, ensure_ascii=False)
        usage = {
            "input_tokens": final.usage.input_tokens or 0,
            "output_tokens": final.usage.output_tokens or 0,
        }
        return content, tool_calls, usage, final.stop_reason

    # --- 多轮底层接口（保留供对齐检查 / spec 翻译等 LLM 调用复用） -------------

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """分离 system 消息并转换其余消息。"""
        system_prompt = None
        api_messages = []
        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                api_messages.append({"role": msg.role, "content": msg.content})
        return system_prompt, api_messages

    def _tools_to_anthropic(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", t.get("parameters", {})),
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """多轮聊天（底层接口）。调用失败抛 LLMError。"""
        self._require_ready()
        system_prompt, api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._tools_to_anthropic(tools)

        try:
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise LLMError(f"Anthropic API 调用失败: {exc}") from exc

        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.input_tokens or 0,
                "output_tokens": response.usage.output_tokens or 0,
            },
            finish_reason=response.stop_reason,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端
# ---------------------------------------------------------------------------


class OpenAIClient:
    """OpenAI 及兼容接口客户端。

    结构化输出经 ``response_format`` 原生实现；扩展思考经 ``reasoning_effort`` 原生启用
    （仅 reasoning 模型 o1/o3/o4 系列）。
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            from openai import AsyncOpenAI
            kwargs: dict[str, Any] = {"api_key": config.api_key}
            if config.base_url:
                kwargs["base_url"] = config.base_url
            self._client = AsyncOpenAI(**kwargs)
            self._ready = True
        except ImportError:
            log.error("openai 包未安装，请执行: pip install openai")
            self._ready = False
            self._client = None
        except Exception as exc:
            log.error("OpenAI 客户端初始化失败: %s", exc)
            self._ready = False
            self._client = None

    @property
    def ready(self) -> bool:
        return self._ready

    def _require_ready(self) -> None:
        if not self._ready:
            raise LLMError("OpenAI 客户端未就绪（openai 包未安装或初始化失败）")

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        """o1/o3/o4 系列 reasoning 模型：不支持 temperature，用 max_completion_tokens / reasoning_effort。"""
        return bool(re.match(r"^o[134]", model.lower()))

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        think: bool | dict | None = None,
        output_format: dict[str, Any] | None = None,
        notdo: list[str] | None = None,
        on_token: Callable[[str], None] | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """单轮调用入口（harness body 用）。

        - ``output_format``：原样作为 ``response_format`` 传入（如 ``{"type": "json_object"}``
          或 ``{"type": "json_schema", "json_schema": {...}}``）
        - ``think``：reasoning 模型映射为 ``reasoning_effort``（"low"/"medium"/"high"，
          dict 可指定 ``effort``；非 reasoning 模型忽略）
        - ``api_params``：透传给 SDK 的额外参数（已知字段入 kwargs，未知入 extra_body）
        """
        self._require_ready()
        model = model or self.config.model
        temperature = self.config.temperature if temperature is None else temperature
        think = think if think is not None else self.config.model_info(model).get("think")
        sys_prompt = _build_system(system, notdo)
        reasoning = self._is_reasoning_model(model)

        messages: list[dict[str, Any]] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        # reasoning 模型用 max_completion_tokens，且不支持 temperature
        if reasoning:
            kwargs["max_completion_tokens"] = self.config.max_tokens
        else:
            kwargs["max_tokens"] = self.config.max_tokens
            kwargs["temperature"] = temperature
        if think and reasoning:
            if isinstance(think, dict):
                kwargs["reasoning_effort"] = think.get("effort", "medium")
            else:
                kwargs["reasoning_effort"] = "medium"
        if output_format:
            kwargs["response_format"] = output_format

        _apply_api_params(kwargs, api_params, _KNOWN_OPENAI_PARAMS)

        try:
            if on_token:
                content, tool_calls, usage, finish = await self._stream(kwargs, on_token)
            else:
                content, tool_calls, usage, finish = await self._nonstream(kwargs)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"OpenAI API 调用失败: {exc}") from exc
        return LLMResponse(content=content, tool_calls=tool_calls, usage=usage, finish_reason=finish)

    async def _nonstream(self, kwargs: dict) -> tuple:
        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls: list[dict[str, Any]] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        }
        return content, tool_calls, usage, choice.finish_reason

    async def _stream(self, kwargs: dict, on_token) -> tuple:
        kwargs["stream"] = True
        # stream_options 仅官方 OpenAI 必然支持；兼容接口（base_url 非空）省略以免被拒
        if not self.config.base_url:
            kwargs["stream_options"] = {"include_usage": True}
        content = ""
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        finish: str | None = None
        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage:
                usage = {
                    "input_tokens": chunk.usage.prompt_tokens or 0,
                    "output_tokens": chunk.usage.completion_tokens or 0,
                }
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content += delta.content
                _safe_on_token(on_token, delta.content)
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
        return content, tool_calls, usage, finish

    # --- 多轮底层接口 -------------------------------------------------------

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        api_messages = []
        for msg in messages:
            if msg.role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "assistant" and msg.tool_calls:
                api_messages.append({
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })
            elif msg.role == "tool":
                api_messages.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": msg.content,
                })
            else:
                api_messages.append({"role": msg.role, "content": msg.content})
        return api_messages

    def _tools_to_openai(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """多轮聊天（底层接口）。调用失败抛 LLMError。"""
        self._require_ready()
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
        }
        if self._is_reasoning_model(self.config.model):
            kwargs["max_completion_tokens"] = self.config.max_tokens
        else:
            kwargs["max_tokens"] = self.config.max_tokens
            kwargs["temperature"] = self.config.temperature
        if tools:
            kwargs["tools"] = self._tools_to_openai(tools)

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError(f"OpenAI API 调用失败: {exc}") from exc

        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            finish_reason=choice.finish_reason,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


# ---------------------------------------------------------------------------
# 客户端工厂
# ---------------------------------------------------------------------------


def create_llm_client(config: LLMConfig):
    """根据配置创建合适的 LLM 客户端。"""
    if config.provider == "anthropic":
        return AnthropicClient(config)
    elif config.provider in ("openai", "openai-compatible"):
        return OpenAIClient(config)
    else:
        # 默认尝试 OpenAI 格式（最常见）
        log.warning("未知 provider '%s'，回退到 OpenAI 兼容客户端", config.provider)
        config.provider = "openai-compatible"
        return OpenAIClient(config)
