"""think 模式开关对比测试。"""

import pytest
from tickflow import parse
from tickflow.async_runner import AsyncRunner
from module_harness.config import HarnessConfig, OutputFormat
from module_harness.registry import HarnessRegistry

pytestmark = pytest.mark.smoke


async def _run_analyze(llm_client, event_bus, think):
    """运行一次分析任务，返回 (output, usage_dict)。"""
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    reg.harness("analyze", HarnessConfig(
        prompt_core=(
            "分析以下代码的时间复杂度并只输出 JSON: "
            "{\"complexity\": \"O(n) 或 O(n^2) 等\", \"explanation\": \"一句话解释\"}。"
            "\n代码：\n{code}"
        ),
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
        api_params={"thinking": think} if think is not None else {},
    ))

    @reg.script("echo")
    def echo(view):
        return view.A.value

    graph = parse("[A] --> B\nA.body: analyze\nB.body: echo", registry=reg)
    runner = AsyncRunner(graph, registry=reg, keep_records=True)
    firings = await runner.run_until_idle(max_ticks=10)

    # usage 数据在 LlmCallCompleted 事件中（NodeState 不含 usage）
    from module_harness.events import LlmCallCompleted
    usage = {}
    for e in event_bus.recorded:
        if isinstance(e, LlmCallCompleted):
            usage = getattr(e, "usage", {})
            break

    return firings[0].output, usage


@pytest.mark.asyncio
async def test_think_off_vs_on(llm_client, event_bus):
    """think=False vs think={"type":"enabled"} 对比。"""
    code = "def f(n):\n    for i in range(n):\n        for j in range(n):\n            print(i, j)"

    # 普通模式
    out_off, usage_off = await _run_analyze(llm_client, event_bus,
                                            think={"type": "disabled"})
    # 思考模式
    bus2 = event_bus.__class__()
    out_on, usage_on = await _run_analyze(llm_client, bus2,
                                          think={"type": "enabled"})

    # 断言：两次都成功
    assert "complexity" in out_off, f"off 模式输出异常: {out_off}"
    assert "complexity" in out_on, f"on 模式输出异常: {out_on}"

    # 断言：思考模式 tokens 更多（输出 tokens 或总 tokens）
    off_total = usage_off.get("input_tokens", 0) + usage_off.get("output_tokens", 0)
    on_total = usage_on.get("input_tokens", 0) + usage_on.get("output_tokens", 0)
    assert on_total > off_total, (
        f"思考模式 tokens({on_total}) 应多于普通模式({off_total})"
    )
