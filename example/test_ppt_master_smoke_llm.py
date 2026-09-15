# example/test_ppt_master_smoke_llm.py
"""真实 LLM 端到端 smoke（付费，默认跳过）。

跑法：SMOKE_LLM=1 python -m pytest example/test_ppt_master_smoke_llm.py -q
前置：.env 配好 LLM_* ；2 页小册子，验证真模型下规划/逐页/门/导出全链。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from example.ppt_master import workspace
from example.ppt_master.module import run_generate

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOKE_LLM") != "1", reason="真实 LLM smoke 需 SMOKE_LLM=1"
)


def test_two_page_real_llm(tmp_path, monkeypatch):
    """2 页小册子真模型全链：规划 → 逐页 SVG → 质量门 → svg_to_pptx 导出。"""
    monkeypatch.setattr(workspace, "_ENVELOPE_DIR", tmp_path)
    spec = {
        "project": "smoke",
        "source": {"kind": "topic", "topic": "Petri 网工作流引擎简介"},
        "production": {"speaker_notes": False},
        "output": {"dir": str(tmp_path / "deck")},
        "roster": [{"id": "p01", "title": "开场", "role": "cover"},
                   {"id": "p02", "title": "核心思想"}],
    }
    firings = asyncio.run(run_generate(spec, persist=False))
    by_node = {f.node: f.output for f in firings}
    assert by_node["Report"]["status"] == "ok"
