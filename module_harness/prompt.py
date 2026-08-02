# module_harness/prompt.py
"""三层 prompt 渲染 + 关键词替换。"""

from __future__ import annotations

import re
from typing import Any

from tickflow.views import DictView, Missing

from .config import HarnessConfig


class PromptRenderer:
    """三层 prompt 拼接 + 关键词替换。

    数据来源：
      Layer 1: config.prompt_core       — 核心提示词模板，含 {key} 占位符
      Layer 2: config.prompt_modes[mode]  — 由 Task promptmode 选出的动态 prompt
      Layer 3: prompt_extra             — Task prompt 字段，人工注入部分

    关键词替换：模板中的 {key} 从 DictView 取值（view.key.value）。
    未匹配的 key 保留原样（不隐藏问题）。
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def render(
        self,
        view: DictView,
        *,
        promptmode: str | None = None,
        prompt_extra: str | None = None,
        extra_values: dict[str, Any] | None = None,
    ) -> str:
        """渲染最终 user prompt。

        ``extra_values``：占位符兜底值（如 spec 字段常量），
        view 中缺失的 key 从该 dict 取值。
        """
        parts: list[str] = []

        # Layer 1: 核心提示词
        parts.append(self.config.prompt_core)

        # Layer 2: 由 promptmode 选出的动态 prompt
        if promptmode is not None:
            mode_text = self.config.prompt_modes[promptmode]
            parts.append(mode_text)

        # Layer 3: 人工注入
        if prompt_extra:
            parts.append(prompt_extra)

        combined = "\n\n".join(parts)
        return self._substitute(combined, view, extra_values)

    def _substitute(
        self,
        template: str,
        view: DictView,
        extra_values: dict[str, Any] | None = None,
    ) -> str:
        """替换模板中的 {key} 占位符为 view 中的值。"""
        pattern = re.compile(r'\{(\w+)\}')

        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            try:
                val = view[key].value
            except (KeyError, AttributeError):
                if extra_values and key in extra_values:
                    return str(extra_values[key])
                return m.group(0)  # 保留原样
            if val is Missing:
                if extra_values and key in extra_values:
                    return str(extra_values[key])
                return m.group(0)
            return str(val)

        return pattern.sub(_replacer, template)
