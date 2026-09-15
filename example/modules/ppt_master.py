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
        "(design_spec/spec_lock) → 并行逐页 SVG → 质量门修复环 → "
        "svg_to_pptx 导出（spec 即确认，非交互）"
    ),
    templates={"generate": GENERATE_TEMPLATE},
    build_registry=_registry_for,
    default_spec=None,   # 页册因项目而异，无零配置缺省（示例 spec 见 fixtures/）
    default_template="generate",
    review_harness=None,  # 固定流程模板，发布前已验证
)
