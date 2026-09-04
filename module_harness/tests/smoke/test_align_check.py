# module_harness/tests/smoke/test_align_check.py
"""对齐检查 harness 真实 LLM 端到端（2 次 LLM 调用）。

A 翻译 → C align_check：真实 LLM 判断翻译产出是否对齐 spec 目标。
探针模式——通过即说明端到端通，失败记录问题不阻塞。
"""

import pytest

from module_harness.core.builtins import register_builtin_harnesses
from module_harness.core.config import HarnessConfig
from module_harness.orchestrate.graph_builder import TasklistTranslator
from module_harness.core.registry import HarnessRegistry
from module_harness.model.spec import Spec, TaskDefinition, Tasklist
from tickflow.async_runner import AsyncRunner
from tickflow.persistence import NullBackend

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_align_check_real_llm(llm_client):
    """align_check 节点输出结构化对齐结果（真实 LLM）。"""
    reg = HarnessRegistry(llm_client=llm_client)
    register_builtin_harnesses(reg)
    reg.harness("translate", HarnessConfig(
        prompt_core="将以下英文翻译为中文，只输出翻译结果：{text}",
        temperature=0.1,
    ))

    tl = Tasklist(
        tasks={
            "A": TaskDefinition(
                type="harness", harness="translate",
                inputs={"text": "{spec.source_text}"},
            ),
            "C": TaskDefinition(
                type="harness", harness="align_check",
                inputs={
                    "spec": "{spec}", "tasklist": "{tasklist}", "node": "{node}",
                    "output_a": "A",
                },
                prompt=(
                    "节点 A 的输出（翻译结果）：{output_a}\n"
                    "判断该输出是否偏离 spec 目标。"
                ),
            ),
        },
        flow="[A] --> C",
    )

    builder = TasklistTranslator(reg, module_id="smoke_align")
    graph, out_reg = builder.build(
        tl, spec=Spec({"source_text": "Hello world", "target": "你好世界"})
    )
    runner = AsyncRunner(graph, registry=out_reg, backend=NullBackend())
    firings = await runner.run_until_idle(max_ticks=10)

    # 两个节点都成功执行
    assert len(firings) >= 2, f"期望 A + C 两个节点，实际 {len(firings)}"
    for f in firings:
        assert f.status == "ok", f"节点 {f.node}: {f.error}"

    # C 的输出是解析后的结构化结果（json_object 自动提取）
    out = runner.run_state.last_output("C")
    assert isinstance(out, dict), f"align_check 输出应为 dict，实际 {type(out).__name__}: {out!r}"
    assert "aligned" in out and isinstance(out["aligned"], bool), f"缺少 aligned 布尔字段: {out!r}"
    assert "suggestions" in out and isinstance(out["suggestions"], str), (
        f"缺少 suggestions 字符串字段: {out!r}"
    )
