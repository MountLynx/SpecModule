"""基础链路冒烟测试：1 harness + 1 script。"""

import pytest
from tickflow import parse
from tickflow.async_runner import AsyncRunner
from module_harness.core.config import HarnessConfig, OutputFormat
from module_harness.core.registry import HarnessRegistry
from module_harness.infra.events import (
    PromptRendered,
    LlmCallStarted,
    LlmToken,
    LlmCallCompleted,
    OutputValidated,
)

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_harness_translate_and_script_echo(llm_client, event_bus):
    """
    [A: harness translate, think=False] --> B: script echo

    验证：LLM 调用成功、JSON 输出正确、事件齐全。
    """
    reg = HarnessRegistry(llm_client=llm_client, event_bus=event_bus)

    reg.harness("translate", HarnessConfig(
        prompt_core="将以下文本翻译为中文，只输出 JSON: {\"translation\": \"你的翻译\"}。文本：{text}",
        output_format=OutputFormat(type="json_object"),
        temperature=0.1,
    ))

    @reg.script("echo")
    def echo(view):
        return view.input()

    graph = parse("[A] --> B\nA.body: translate\nB.body: echo", registry=reg)

    runner = AsyncRunner(graph, registry=reg, keep_records=True)
    firings = await runner.run_until_idle(max_ticks=10)

    # 断言：两个节点都正常完成
    assert len(firings) == 2, f"期望 2 个 firing，实际 {len(firings)}"
    for f in firings:
        assert f.status == "ok", f"节点 {f.node} 状态异常: {f.status}, error={f.error}"

    # 断言：script 拿到了翻译结果
    output = firings[-1].output
    assert isinstance(output, dict), f"输出应为 dict，实际 {type(output)}"
    assert "translation" in output, f"输出缺 translation 字段: {output}"
    assert isinstance(output["translation"], str), f"translation 应为字符串"

    # 断言：事件齐全
    event_types = [type(e).__name__ for e in event_bus.recorded]
    for expected in ["PromptRendered", "LlmCallStarted", "LlmToken",
                     "LlmCallCompleted", "OutputValidated"]:
        assert expected in event_types, f"缺少事件: {expected}"
