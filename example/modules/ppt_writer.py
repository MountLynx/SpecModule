"""ppt_writer 模块入口（CLI 发现用）。

modules/ 目录扫描约定：一个 module 一个 py 文件，声明模块级 ``entry``
（ModuleEntry）。CLI ``specmodule run --module ppt_writer`` 经
discover_modules() 导入本文件，读取 entry 获取模板/registry 构建方式。
"""

from __future__ import annotations

from typing import Any

from example.ppt_writer.module import (
    DEFAULT_SPEC,
    PPT_RENDER_TEMPLATE,
    TEMPLATE_REVIEW_TEMPLATE,
    _build_registry,
)
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus


def _registry_for(
    llm_client: Any, template_name: str, event_bus: EventBus
) -> Any:
    """双模板共享同一 registry（全部组件一次注册，template_name 无需区分）。"""
    return _build_registry(llm_client, event_bus)


entry = ModuleEntry(
    name="ppt_writer",
    description=(
        "spec(每页内容+布局) → 可机器校验的 .pptx；模板资产"
        "（manifest + reference/ + 占位符约定）+ 模板制作工作流"
    ),
    templates={
        "ppt_render": PPT_RENDER_TEMPLATE,
        "template_review": TEMPLATE_REVIEW_TEMPLATE,
    },
    build_registry=_registry_for,
    default_spec=DEFAULT_SPEC,
    default_template="ppt_render",
    review_harness=None,  # 固定流程模板，发布前已验证
)
