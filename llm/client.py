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

import base64
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


@dataclass
class ImageResult:
    """图像生成结果。generate_image 成功时返回；调用失败抛 LLMError 而非返回此对象。"""
    data: bytes
    revised_prompt: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


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


def _safe_on_thinking(on_thinking: Callable[[str], None] | None, chunk: str) -> None:
    """调用 on_thinking，回调异常不得影响主流程（同 _safe_on_token）。"""
    if on_thinking is None or not chunk:
        return
    try:
        on_thinking(chunk)
    except Exception:
        log.exception("on_thinking 回调异常；已忽略")


class _ThinkTagStripper:
    """跨 chunk 安全的内联 ``<think>…</think>`` 剥离器。

    部分兼容网关不单设 reasoning 通道，思考文本带标签内联在 content 里。
    feed() 逐 chunk 喂入，返回 (content_delta, thinking_delta)：标签外增量
    归 content、标签内增量归 thinking；hold-back 缓冲处理标签自身被 chunk
    劈开的情况；flush() 在流结束吐出残留（未闭合标签按思考处理）。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._buf = ""      # hold-back：可能是未判定标签前缀的尾部

    def feed(self, chunk: str) -> tuple[str, str]:
        self._buf += chunk
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        while self._buf:
            if self._inside:
                end = self._buf.find(self._CLOSE)
                if end >= 0:
                    thinking_parts.append(self._buf[:end])
                    self._buf = self._buf[end + len(self._CLOSE):]
                    self._inside = False
                    continue
                keep = self._holdback(self._CLOSE)
            else:
                start = self._buf.find(self._OPEN)
                if start >= 0:
                    content_parts.append(self._buf[:start])
                    self._buf = self._buf[start + len(self._OPEN):]
                    self._inside = True
                    continue
                keep = self._holdback(self._OPEN)
            emit = len(self._buf) - keep
            if emit:
                (thinking_parts if self._inside else content_parts).append(self._buf[:emit])
                self._buf = self._buf[emit:]
            break
        return "".join(content_parts), "".join(thinking_parts)

    def flush(self) -> tuple[str, str]:
        """流结束：按当前内/外状态吐出残留 hold-back。"""
        if not self._buf:
            return "", ""
        out = self._buf
        self._buf = ""
        return ("", out) if self._inside else (out, "")

    def _holdback(self, tag: str) -> int:
        """缓冲尾部可能是 tag 真前缀的最大长度（ TagLen-1 向下探）。"""
        for n in range(min(len(self._buf), len(tag) - 1), 0, -1):
            if tag.startswith(self._buf[-n:]):
                return n
        return 0


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
            self._client = AsyncAnthropic(**config.to_client_kwargs())
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

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> ImageResult:
        """Anthropic 无图像生成 API——能力缺失显式暴露,框架不猜测不降级。

        签名与 OpenAIClient.generate_image 对称（参数不消费，仅保持路由委托无 TypeError）。
        """
        raise LLMError("Anthropic 无图像生成 API；图像生成请配置 OpenAI 兼容 provider")

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
        # 框架级 system_rules 注入到 system prompt 最前面
        full_system = None
        if self.config.system_rules:
            full_system = self.config.system_rules
            if system:
                full_system += "\n\n" + system
        elif system:
            full_system = system
        sys_prompt = _build_system(full_system, notdo)

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
            self._client = AsyncOpenAI(**config.to_client_kwargs())
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

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> ImageResult:
        """图像生成(harness 图像模式用)。走 images/generations,统一取 b64。

        - ``model``：生图模型（如 gpt-image-1 / cogview-4），缺省回落 config
        - ``size``：如 "1024x1024"，None = API 默认
        - ``api_params``：透传 SDK 额外参数（已知字段直入，未知入 extra_body）

        不主动发送 ``response_format``：gpt-image-* 不接受该参数（其总是返回
        b64）；dall-e 系 / 默认返 URL 的兼容端点需显式
        ``api_params={"response_format": "b64_json"}``，否则因未拿到 b64 数据
        而 LLMError——显式请求优于猜测模型家族。

        ``api_params`` 可覆盖 ``n``；返回仅取第一张，多余图像将被丢弃。
        """
        self._require_ready()
        kwargs: dict[str, Any] = {
            "model": model or self.config.model,
            "prompt": prompt,
            "n": 1,
        }
        if size:
            kwargs["size"] = size
        _apply_api_params(kwargs, api_params, _KNOWN_OPENAI_PARAMS)
        try:
            response = await self._client.images.generate(**kwargs)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"OpenAI images API 调用失败: {exc}") from exc
        items = getattr(response, "data", None) or []
        item = items[0] if items else None
        b64 = getattr(item, "b64_json", None) if item is not None else None
        if not b64:
            raise LLMError(
                "images API 未返回 b64_json 图像数据"
                "（端点默认返 URL 时，请以 api_params 传 response_format='b64_json'）"
            )
        usage: dict[str, int] = {}
        resp_usage = getattr(response, "usage", None)
        if resp_usage is not None:
            for key in ("input_tokens", "output_tokens"):
                val = getattr(resp_usage, key, None)
                if val is not None:
                    usage[key] = val
        try:
            data = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise LLMError(f"images API 返回的 b64_json 无法解码: {exc}") from exc
        return ImageResult(
            data=data,
            revised_prompt=getattr(item, "revised_prompt", None),
            usage=usage,
        )

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
        on_thinking: Callable[[str], None] | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """单轮调用入口（harness body 用）。

        - ``output_format``：原样作为 ``response_format`` 传入（如 ``{"type": "json_object"}``
          或 ``{"type": "json_schema", "json_schema": {...}}``）
        - ``think``：reasoning 模型映射为 ``reasoning_effort``（"low"/"medium"/"high"，
          dict 可指定 ``effort``；非 reasoning 模型忽略）
        - ``api_params``：透传给 SDK 的额外参数（已知字段入 kwargs，未知入 extra_body）
        - ``on_thinking``：思考/推理增量回调（reasoning_content/reasoning 方言 +
          content 内联 <think> 剥离；Anthropic thinking_delta）；仅传 on_token 时
          思考增量静默丢弃（向后兼容）
        """
        self._require_ready()
        model = model or self.config.model
        temperature = self.config.temperature if temperature is None else temperature
        think = think if think is not None else self.config.model_info(model).get("think")
        # 框架级 system_rules 注入到 system prompt 最前面
        full_system = None
        if self.config.system_rules:
            full_system = self.config.system_rules
            if system:
                full_system += "\n\n" + system
        elif system:
            full_system = system
        sys_prompt = _build_system(full_system, notdo)
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
            if on_token or on_thinking:
                content, tool_calls, usage, finish = await self._stream(kwargs, on_token, on_thinking)
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

    async def _stream(self, kwargs: dict, on_token, on_thinking) -> tuple:
        kwargs["stream"] = True
        # stream_options 仅官方 OpenAI 必然支持；兼容接口（base_url 非空）省略以免被拒
        if not self.config.base_url:
            kwargs["stream_options"] = {"include_usage": True}
        content = ""
        tool_calls: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        finish: str | None = None
        stripper = _ThinkTagStripper()
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
            # 思考增量：DeepSeek/Kimi 的 reasoning_content，部分网关用 reasoning；
            # 无原生通道时由 <think> 剥离器从 content 转移（见下）
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                _safe_on_thinking(on_thinking, reasoning)
            if delta.content:
                c_delta, t_delta = stripper.feed(delta.content)
                content += c_delta        # 返回值用剥离后文本（思考不泄入 JSON 输出）
                if c_delta:
                    _safe_on_token(on_token, c_delta)
                if t_delta:
                    _safe_on_thinking(on_thinking, t_delta)
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
        tail_c, tail_t = stripper.flush()
        if tail_c:
            content += tail_c
            _safe_on_token(on_token, tail_c)
        if tail_t:
            _safe_on_thinking(on_thinking, tail_t)
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
# 路由门面客户端
# ---------------------------------------------------------------------------


class RoutingClient:
    """按模型路由的客户端门面。

    每次调用按「模型 → models 注册表 provider 名 → provider 连接」选择客户端,
    同一 provider 惰性建连一次。模型未注册 / 未指明 provider / 手工配置
    (providers 表为空)→ 默认连接(config 顶层字段)——单 provider 配置行为
    与直连客户端一致。
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._clients: dict[str | None, Any] = {}

    def _client_for(self, model: str | None):
        pc = self.config.provider_for(model)
        key = pc.name if pc is not None else None
        if key not in self._clients:
            cfg = self.config.for_provider(pc) if pc is not None else self.config
            if cfg.provider == "anthropic":
                self._clients[key] = AnthropicClient(cfg)
            else:
                if cfg.provider not in ("openai", "openai-compatible"):
                    log.warning("未知 sdktype '%s'，回退到 OpenAI 兼容客户端", cfg.provider)
                self._clients[key] = OpenAIClient(cfg)
        return self._clients[key]

    @property
    def ready(self) -> bool:
        """默认 provider 客户端就绪(惰性触发建连)。"""
        return self._client_for(None).ready

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
        """单轮调用(harness body 入口),按调用模型路由。"""
        return await self._client_for(model).complete(
            prompt,
            system=system,
            model=model,
            temperature=temperature,
            think=think,
            output_format=output_format,
            notdo=notdo,
            on_token=on_token,
            api_params=api_params,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """多轮聊天（底层接口），始终走默认连接（与原直连行为一致）。"""
        return await self._client_for(None).chat(messages, tools)

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        api_params: dict[str, Any] | None = None,
    ) -> ImageResult:
        """图像生成,按调用模型路由。"""
        return await self._client_for(model).generate_image(
            prompt, model=model, size=size, api_params=api_params,
        )

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


def create_llm_client(config: LLMConfig) -> RoutingClient:
    """创建按模型路由的 LLM 客户端门面(原工厂签名不变,消费方透明切换)。"""
    return RoutingClient(config)
