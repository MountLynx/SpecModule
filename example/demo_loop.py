# example/demo_loop.py
"""fact_review_loop 真实运行示例。

用法（在仓库根目录）：
    python -m example.demo_loop            # 真实 LLM（.env / 环境变量）
    python -m example.demo_loop --mock     # 假 LLM（无需 key，冒烟演示数据流）
"""

from __future__ import annotations

import sys

from llm.client import LLMResponse

from example.fact_review_loop import FactReviewLoop

ORIGINAL = (
    "The proposed method achieves 85% accuracy on the benchmark, "
    "outperforming the baseline rule-based system by 15 percentage points. "
    "It runs in under 2 seconds per query."
)
DRAFT = (
    "Our method achieves 85% accuracy, which is 15% higher than the baseline. "
    "It is also very fast and can run in less than 5 seconds. "
    "The system is quite impressive."
)


def _mock_client():
    """免 key 假客户端：按 prompt 独有引导短语返回预设输出。"""
    from unittest.mock import AsyncMock, MagicMock

    async def complete(prompt: str | None = None, **kwargs) -> LLMResponse:
        p = prompt or ""
        if "修复者" in p:
            content = (
                '{"text": "Our method achieves 85% accuracy, outperforming the '
                'baseline rule-based system by 15 percentage points. '
                'It runs in under 2 seconds per query."}'
            )
        elif "原样转发" in p:
            content = '{"text": "' + DRAFT.replace('"', "'") + '"}'
        else:  # 审阅
            content = (
                '{"issues": [{"type": "alteration", "detail": "耗时 5 秒与原文'
                ' 2 秒不符", "quote_original": "under 2 seconds", '
                '"quote_draft": "less than 5 seconds"}], "clean": false}'
            )
        return LLMResponse(content=content, usage={}, finish_reason="end_turn")

    client = MagicMock()
    client.complete = AsyncMock(side_effect=complete)
    return client


async def main() -> None:
    loop = FactReviewLoop(llm_client=_mock_client() if "--mock" in sys.argv else None)
    firings = await loop.run(
        {"original_text": ORIGINAL, "draft_text": DRAFT}, max_ticks=50)
    print("=== fact_review_loop 输出 ===")
    for k, v in firings[-1].output.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
