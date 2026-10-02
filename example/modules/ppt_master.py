"""ppt_master 模块入口（CLI 发现用）。

modules/ 目录扫描约定：一个 module 一个 py 文件，声明模块级 ``entry``
（ModuleEntry）。CLI ``specmodule run --module ppt_master`` 经
discover_modules() 导入本文件，读取 entry 获取模板/registry 构建方式。
"""

from __future__ import annotations

from typing import Any

from example.ppt_master.module import GENERATE_TEMPLATE, _build_registry
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus


def _registry_for(llm_client: Any, template_name: str, event_bus: EventBus) -> Any:
    """单模板，template_name 无需区分——全部组件一次注册。"""
    return _build_registry(llm_client, event_bus)


entry = ModuleEntry(
    name="ppt_master",
    description=(
        "ppt-master Generate 主线复刻：spec(页册+契约) → 规划"
        "(design_spec/spec_lock) → 图像/图标 → 并行逐页 SVG → 质量门修复环"
        " → 讲者备注 → svg_to_pptx 导出（spec 即确认，非交互）"
    ),
    templates={"generate": GENERATE_TEMPLATE},
    build_registry=_registry_for,
    default_spec={
        "project": "demo_deck",
        "source": {"kind": "topic", "topic": (
            "SpecModule：spec 驱动的 LLM 任务编排框架——自然语言规格翻译成"
            " tick 流程图逐拍执行；checkpoint 断点恢复与产物归档开箱即用。"
        )},
        # placeholder 图像：试跑零图像 API 依赖（省略则规划者裁量可能选 ai，
        # 只配 LLM key 的环境会中途失败）；实战在 spec 里换 ai/user。
        "images": {"sources": ["placeholder"]},
        "roster": [
            {"id": "p01", "title": "SpecModule：spec 驱动的 LLM 任务编排", "role": "cover"},
            {"id": "p02", "title": "方法：spec → tick 流程图",
             "points": ["自然语言规格翻译为 tasklist", "tick 引擎逐拍执行，快照落盘"]},
            {"id": "p03", "title": "实战：断点恢复与产物归档",
             "points": ["checkpoint 从任意 tick/手工标签恢复", "run.sqlite + artifacts.json 开箱即用"]},
            {"id": "p04", "title": "结语与未来工作", "role": "closing"},
        ],
    },   # 参考预填值（webview spec 参考/CLI 无 spec 回落）；全字段契约见 spec_schema.py
    default_template="generate",
    review_harness=None,  # 固定流程模板，发布前已验证
)
