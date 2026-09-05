"""Module 编排器全链路冒烟测试（script 翻译器，1 次 LLM 调用）。"""

import pytest
from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.core.registry import HarnessRegistry
from module_harness.infra.events import EventBus
from module_harness.model.translator import TemplateLoader
from module_harness.model.module import Module

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_module_run_with_script_translator(llm_client, event_bus):
    """
    Module.run() 全链路：模板加载 → 翻译 → graph 构建 → 命名空间隔离 → Runner 执行。
    """
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)
    loader = TemplateLoader()

    # 注册执行元件
    reg.harness("translate", HarnessConfig(
        prompt_core="将'{text}'翻译为中文，只输出 JSON: {\"translation\": \"...\"}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("format_output")
    def format_output(view):
        data = view.field("data")
        return {"result": data["translation"].strip()}

    # 注册 script 翻译器：固定返回 tasklist
    @reg.script("smoke_translator")
    def smoke_translator(view):
        return {
            "Tasks": {
                "A": {
                    "type": "harness",
                    "harness": "translate",
                    "inputs": {"text": "{spec.text}"},
                    "outputformat": {"type": "json_object"},
                },
                "B": {
                    "type": "script",
                    "script": "format_output",
                    "inputs": {"data": "A"},
                },
            },
            "Flow": "A --> B",
        }

    # 注册模板
    loader.register("smoke_translate", {
        "name": "smoke_translate",
        "translation": {"type": "script", "script": "smoke_translator"},
        "tasklist": {"Tasks": {}, "Flow": ""},
    })

    # 运行 Module
    mod = Module(
        spec={"text": "Hello world, this is a test."},
        template_name="smoke_translate",
        llm_client=llm_client,
        event_bus=event_bus,
        template_loader=loader,
        registry=reg,
    )

    runner = await mod._build_runner_async()
    firings = await runner.run_until_idle(max_ticks=10)

    # 断言
    assert runner.is_idle()
    assert len(firings) >= 2
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    # 验证输出
    final = firings[-1].output
    assert "result" in final, f"最终输出: {final}"
    assert len(final["result"]) > 0
