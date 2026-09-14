# llm/tests/test_mock_image.py
"""Mock 客户端生图:返回合法 PNG 字节,测试免网络免 key。"""

from __future__ import annotations

import pytest

from llm.client import ImageResult
from llm.mock import MockLLMClient


@pytest.mark.asyncio
async def test_generate_image_returns_png_bytes():
    result = await MockLLMClient().generate_image(prompt="x", model="m")
    assert isinstance(result, ImageResult)
    assert result.data.startswith(b"\x89PNG")  # PNG magic
    assert result.usage == {}
