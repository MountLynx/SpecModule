# module_harness/align.py
"""对齐检查 — 内置 align_check harness 节点（roadmap #2）。

普通 harness 节点：模板设计者在 flow 中自行插入（通常放在关键产出节点之后），
框架不额外调度。不插入即不执行。

前置输出注入：prompt_core 不含 {字段} 占位符（无隐式行为）；模板设计者通过
task.prompt（Layer 3）注入前置节点输出，例如模板中
"inputs": {"output_a": "A"} + "prompt": "节点 A 输出：{output_a}"
（具名 bind 机制：task.inputs 写成 field→producer 具名 bind，运行时经
视图 v.named 把 A 的输出渲染进 {output_a}）。
"""

from __future__ import annotations

from ..core.config import HarnessConfig
from ..core.outputfmt import OutputFormat
from ..core.registry import HarnessRegistry


ALIGN_CHECK_CONFIG = HarnessConfig(
    name="align_check",
    prompt_core=(
        "你是对齐检查器。判断当前节点产出是否偏离 spec 目标。\n"
        "spec: {spec}\n"
        "tasklist: {tasklist}\n"
        "当前位置: {node}\n"
        "结合已提供的前置节点输出判断（若有），输出 JSON："
        '{"aligned": true/false, "suggestions": "..."}'
    ),
    output_format=OutputFormat(type="json_object"),
    temperature=0.1,
)


def register_align_check_harness(
    reg: HarnessRegistry, name: str = "align_check"
) -> None:
    """注册内置对齐检查 harness（默认名 align_check）。"""
    reg.harness(name, ALIGN_CHECK_CONFIG)
