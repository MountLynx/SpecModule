# example/demo_writer.py
"""academic_writer 完整流水线真实运行示例（读 sample_raw_text.txt）。

用法（在仓库根目录）：
    python -m example.demo_writer            # 真实 LLM（.env / 环境变量）
    python -m example.demo_writer --mock     # 假 LLM（无需 key，冒烟演示数据流）
"""

from __future__ import annotations

import sys
from pathlib import Path

from llm.client import LLMResponse

from example.academic_writer import run_writer

SAMPLE = Path(__file__).parent / "sample_raw_text.txt"


def _mock_client():
    """免 key 假客户端：按 prompt 独有引导短语返回预设输出（演示数据流形状）。

    分发键不能用"灵感草稿"等用户文本可能出现的词——{original} 占位符会把
    raw_text 渲染进 Review/Finalize 的 prompt。
    """
    from unittest.mock import AsyncMock, MagicMock

    async def complete(prompt: str | None = None, **kwargs) -> LLMResponse:
        p = prompt or ""
        if "整合输出最终版本" in p:
            content = (
                '{"text": "This paper proposes an LLM-based code review system '
                'that automatically analyzes pull requests.", '
                '"notes": "合并重复表述；将口语化表达改为正式学术句式"}'
            )
        elif "学术英语写作规范" in p:
            content = '{"text": "We propose an LLM-based system for automated code review of pull requests."}'
        elif "整理成逻辑通顺的英文文段" in p:
            content = '{"text": "We propose an LLM-based code review system that automatically reviews pull requests."}'
        elif "原样转发" in p:
            content = '{"text": "We propose an LLM-based code review system that automatically reviews pull requests."}'
        elif "修复者" in p:
            content = '{"text": "We propose an LLM-based code review system that automatically reviews pull requests."}'
        else:  # 审阅
            content = '{"issues": [], "clean": true}'
        return LLMResponse(content=content, usage={}, finish_reason="end_turn")

    client = MagicMock()
    client.complete = AsyncMock(side_effect=complete)
    return client


async def main() -> None:
    raw_text = SAMPLE.read_text(encoding="utf-8")
    firings = await run_writer(
        {"raw_text": raw_text},
        llm_client=_mock_client() if "--mock" in sys.argv else None,
        max_ticks=80,
    )
    out = firings[-1].output
    print("=== final_text ===")
    print(out["final_text"])
    print()
    print("=== modification_notes ===")
    print(out["modification_notes"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
