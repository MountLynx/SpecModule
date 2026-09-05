# module_harness/prompt.py
"""三层 prompt 渲染 + 关键词替换。"""

from __future__ import annotations

import re
from typing import Any

from tickflow.views import Missing, NodeView

from .config import HarnessConfig

# 常量正则模块级编译一次（render 每 harness 节点每 tick 调用）。
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class PromptRenderer:
    """三层 prompt 拼接 + 关键词替换。

    数据来源：
      Layer 1: config.prompt_core       — 核心提示词模板，含 {key} 占位符
      Layer 2: config.prompt_modes[mode]  — 由 Task promptmode 选出的动态 prompt
      Layer 3: prompt_extra             — Task prompt 字段，人工注入部分

    关键词替换：模板中的 {key} 从视图的具名 bind 字段取值（v.named），
    非 bind 字段落 extra_values（spec 常量等）。
    未匹配的 key 保留原样（不隐藏问题）。
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def render(
        self,
        view: NodeView,
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
        view: Any,
        extra_values: dict[str, Any] | None = None,
    ) -> str:
        """替换模板中的 {key} 占位符。

        取值顺序：视图的具名 bind 字段（v.named，未点火字段值为 Missing）->
        extra_values（spec 常量等）-> 保留原样（不隐藏问题）。
        """
        # 所有 tickflow 视图（NodeView/GuardView）都带 .named；此守卫仅为容错非 tickflow 对象
        named = view.named if hasattr(view, "named") else {}

        def _replacer(m: re.Match) -> str:
            key = m.group(1)
            val = named.get(key)
            if val is None and key not in named:
                # not a bind field -> constants / literal
                if extra_values and key in extra_values:
                    return str(extra_values[key])
                return m.group(0)
            if val is Missing:
                # declared producer not fired yet -> spec fallback
                if extra_values and key in extra_values:
                    return str(extra_values[key])
                return m.group(0)
            return str(val)

        return _PLACEHOLDER_RE.sub(_replacer, template)
