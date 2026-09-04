# module_harness/builtins.py
"""内置 harness 集 — requires 的默认提供方。"""

from __future__ import annotations

from ..orchestrate.align import register_align_check_harness
from .config import HarnessConfig, OutputFormat
from ..orchestrate.consistency import register_review_harness
from .registry import HarnessRegistry

BUILTIN_HARNESS_NAMES: frozenset[str] = frozenset({
    "spec_to_tasklist", "spec_tasklist_review", "align_check",
})

# 翻译 harness 最小骨架；模板的 prompt_core 在翻译时覆盖（translator.py）
SPEC_TO_TASKLIST_CONFIG = HarnessConfig(
    name="spec_to_tasklist",
    prompt_core="根据 spec 生成 tasklist JSON。",
    output_format=OutputFormat(type="json_object"),
    temperature=0.3,
)


def register_builtin_harnesses(reg: HarnessRegistry) -> None:
    """注册内置 harness（spec_to_tasklist、spec_tasklist_review、align_check）。
    幂等，可重复调用。"""
    reg.harness("spec_to_tasklist", SPEC_TO_TASKLIST_CONFIG)
    register_review_harness(reg)
    register_align_check_harness(reg)
