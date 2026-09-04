"""教程模块：summarizer——与 docs/guides/tutorial-first-module.md 配套。

从零到第一个模块的案例：
- build_registry：注册 1 个 harness（LLM 总结）+ 2 个 script（输出清洗 / 模板翻译器）
- templates：一个 "summarize" 模板（script 翻译器，固定流水线）
- review_harness=None：固定流程模板，发布前已验证（跳过一致性审核）

运行（见本目录 README.md）：--mock 冒烟免 key；真 LLM 需配置 .env。
"""

from __future__ import annotations

from typing import Any

from module_harness.cli.entry import ModuleEntry
from module_harness.infra.events import EventBus


def build_registry(
    llm_client: Any, template_name: str, event_bus: EventBus
) -> Any:
    """构建 HarnessRegistry：注册执行元件。CLI 传入外部 event_bus。"""
    from module_harness import HarnessConfig, HarnessRegistry, OutputFormat

    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    # 节点 1：harness——LLM 调用（三层 prompt 最小可用：只写 prompt_core）
    reg.harness("summarize", HarnessConfig(
        prompt_core="用不超过 {max_words} 字总结以下文本，输出 JSON {\"summary\": \"...\"}：\n{text}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.3,
    ))

    # 节点 2：script——纯 Python 处理（清洗 harness 输出）
    @reg.script("format_summary")
    def format_summary(view):
        data = view.A.value  # producer 名访问（field 名 data 仅作 prompt 占位符）
        if isinstance(data, dict):
            text = str(data.get("summary", "")) or str(data)
        else:
            text = str(data)
        return {"summary": text.strip()}

    # 模板翻译器：确定性返回固定流水线 tasklist（零 LLM 成本）
    @reg.script("tl_summarize")
    def tl_summarize(view):
        return TASKLIST

    return reg


# 与 examples/tutorial/tasklist.json 同构——直写与模板通道跑同一条流水线
TASKLIST = {
    "Tasks": {
        "A": {
            "type": "harness",
            "harness": "summarize",
            "inputs": {"text": "{spec.text}", "max_words": "{spec.max_words}"},
            "outputformat": {"type": "json_object"},
        },
        "B": {"type": "script", "script": "format_summary", "inputs": {"data": "A"}},
    },
    "Flow": "[A] --> B",
}


entry = ModuleEntry(
    name="summarizer",
    description="教程示例：LLM 总结模块（harness + script 两节点流水线）",
    templates={
        "summarize": {
            "name": "summarize",
            "description": "总结流程（script 翻译器，固定流水线）",
            "translation": {"type": "script", "script": "tl_summarize"},
            "tasklist": TASKLIST,
        },
    },
    build_registry=build_registry,
    default_spec={"text": "SpecModule 是一个可审计、可调试、可完全掌控的 LLM 使用框架。", "max_words": 50},
    default_template="summarize",
    review_harness=None,  # 固定流程模板，发布前已验证
)
