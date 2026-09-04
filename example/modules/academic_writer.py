"""academic_writer 模块入口（CLI 发现用）。

modules/ 目录扫描约定：一个 module 一个 py 文件，声明模块级 ``entry``
（ModuleEntry）。CLI ``specmodule run --module academic_writer`` 经
discover_modules() 导入本文件，读取 entry 获取模板/子模块/registry 构建方式。
"""

from __future__ import annotations

from typing import Any

from example.academic_writer import (
    ACADEMIC_TEMPLATE,
    DETAILED_TEMPLATE,
    FactReviewLoop,
    _build_registry,
)
from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus


def _registry_for(
    llm_client: Any, template_name: str, event_bus: EventBus
) -> Any:
    """ModuleEntry.build_registry 适配：按模板名映射 academic_writer 的模式。

    example.academic_writer._build_registry 以 mode 区分（submodule/detailed），
    ModuleEntry 契约收 template_name——此处按模板名换算 mode。
    """
    mode = "detailed" if template_name == "academic_writer_detailed" else "submodule"
    return _build_registry(llm_client, mode, event_bus)


entry = ModuleEntry(
    name="academic_writer",
    description="灵感式写作 → 学术英语（默认=全文优化，详细=逐段可审计）",
    templates={
        "academic_writer": ACADEMIC_TEMPLATE,
        "academic_writer_detailed": DETAILED_TEMPLATE,
    },
    submodules={"fact_review_loop": FactReviewLoop},
    build_registry=_registry_for,
    default_template="academic_writer",
    review_harness=None,  # 固定流程模板，发布前已验证
)