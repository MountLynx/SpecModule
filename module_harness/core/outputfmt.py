"""OutputFormat 输出格式约束定义 + OutputValidator 校验器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from tickflow import Failure


@dataclass
class OutputFormat:
    """输出格式约束定义。

    ``type`` 为 "json_object" 时要求合法 JSON；
    "json_schema" 时还需 ``schema`` 校验通过；
    "text" 时不做校验，直接返回原文本。
    """
    type: Literal["json_object", "json_schema", "text"]
    schema: dict[str, Any] | None = None
    instruction: str | None = None


# ── 内置提取器 ──────────────────────────────────────────────────

def _strip_markdown_fences(raw: str) -> str | None:
    """去除 ```json ... ``` 包裹。"""
    pattern = r'```(?:json)?\s*\n?(.*?)\n?```'
    m = re.search(pattern, raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _extract_first_json(raw: str) -> str | None:
    """匹配第一个完整 JSON 对象或数组。"""
    # 先尝试 match 对象
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
    if m:
        return m.group(0)
    # 再尝试 match 数组
    m = re.search(r'\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]', raw, re.DOTALL)
    if m:
        return m.group(0)
    return None


def _strip_trailing_junk(raw: str) -> str | None:
    """从末尾逐步截断非 JSON 字符，尝试解析。"""
    s = raw.strip()
    while s:
        try:
            json.loads(s)
            return s
        except json.JSONDecodeError:
            pass
        s = s[:-1]
    return None


# ── OutputValidator ──────────────────────────────────────────────

class OutputValidator:
    """输出格式校验器。

    校验流程：先直接解析，失败则逐个尝试提取器；提取再失败 → Failure(type="llm")。
    """

    def __init__(self, fmt: OutputFormat) -> None:
        self.fmt = fmt
        self._extractors: list[Callable[[str], str | None]] = [
            _strip_markdown_fences,
            _extract_first_json,
            _strip_trailing_junk,
        ]

    def prompt_instruction(self) -> str:
        """生成注入到 user prompt 的格式约束文本。"""
        if self.fmt.instruction is not None:
            return self.fmt.instruction
        if self.fmt.type == "text":
            return ""
        if self.fmt.type == "json_object":
            return "请输出合法 JSON，不要包含任何解释或其他文本。"
        if self.fmt.type == "json_schema":
            schema_text = json.dumps(self.fmt.schema, ensure_ascii=False, indent=2)
            return (
                "请严格按照以下 JSON Schema 输出，不要包含任何解释或其他文本：\n"
                f"```json\n{schema_text}\n```"
            )
        return ""

    def validate(self, raw: str) -> Any:
        """校验并返回解析值，或 Failure(type="llm")。"""
        if self.fmt.type == "text":
            return raw

        # Step 1: 直接解析
        parsed, error = self._try_parse(raw)
        if parsed is not None and error is None:
            return parsed

        # Step 2: 逐个尝试提取器
        for extractor in self._extractors:
            extracted = extractor(raw)
            if extracted is not None:
                parsed, error = self._try_parse(extracted)
                if parsed is not None and error is None:
                    return parsed

        return Failure(
            f"输出格式校验失败：{error or '所有提取器均未能修复输出'}",
            type="llm",
        )

    def register_extractor(self, fn: Callable[[str], str | None]) -> None:
        """注册自定义提取策略。插入到内置提取器之前（优先尝试）。"""
        self._extractors.insert(0, fn)

    def _try_parse(self, text: str) -> tuple[Any, str | None]:
        """尝试 json.loads + 可选的 schema 校验。返回 (parsed, error)。"""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return None, str(e)

        if self.fmt.type == "json_schema" and self.fmt.schema is not None:
            try:
                import jsonschema
                jsonschema.validate(parsed, self.fmt.schema)
            except ImportError:
                # jsonschema 未安装时跳过 schema 校验，仅保证是 JSON
                pass
            except jsonschema.ValidationError as e:
                return None, str(e)

        return parsed, None
