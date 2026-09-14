"""Mock LLM 客户端：--mock / 测试用假客户端（免 key 免网络）。"""

from __future__ import annotations

import base64
import json
from typing import Any

from .client import ImageResult, LLMResponse


# 1x1 透明 PNG（最小合法图像,测试断言 PNG magic 用）
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class MockLLMClient:
    """通用假客户端：output_format=json_object 时返回宽松合法 JSON。

    翻译通道（script 翻译器）不经 LLM，天然可用；json_object 输出可通过
    OutputValidator；text 输出为占位文本。
    """

    async def complete(self, **kwargs: Any) -> LLMResponse:
        fmt = kwargs.get("output_format") or {}
        if fmt.get("type") == "json_object":
            content = json.dumps(
                {"result": "mock output", "summary": "mock", "issues": []}
            )
        else:
            content = "mock output"
        return LLMResponse(content=content)

    async def generate_image(self, **kwargs: Any) -> ImageResult:
        """假生图:返回 1x1 PNG。"""
        return ImageResult(data=_PNG_1X1)
