# module_harness/tests/smoke/test_image_gen.py
"""文生图冒烟 — 真实 bianxie / gpt-image-1-mini 调用（pytest -m smoke 显式运行）。

全链路：HarnessConfig(mode="image") → RoutingClient 按模型路由到 bianxie
provider → images/generations → 落盘 → call_harness 返回路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from module_harness.core.call import call_harness
from module_harness.core.config import HarnessConfig

pytestmark = pytest.mark.smoke


class TestImageGenSmoke:
    @pytest.mark.asyncio
    async def test_generate_image_end_to_end(self, llm_client, tmp_path):
        cfg = HarnessConfig(
            prompt_core="极简主义扁平插画：{subject}，柔和配色，画面中不出现文字",
            mode="image",
            model="gpt-image-1-mini",
            image_size="1024x1024",
            image_dir=str(tmp_path),
        )
        result = await call_harness(
            cfg, {"subject": "星空下的湖边小木屋"}, llm_client=llm_client,
        )
        p = Path(result.value)
        assert p.parent == tmp_path
        assert p.suffix == ".png"
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert p.stat().st_size > 10_000  # 真图不会是几百字节的占位
        print(f"\nimage → {p} ({p.stat().st_size} bytes)")
